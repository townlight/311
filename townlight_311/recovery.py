"""Bounded resident access-token recovery for Townlight 311.

Recovery deliberately reveals nothing about whether a tracking number or
contact email matched a request. A real challenge creates a private event and
a pending outbox notification containing only Fernet ciphertext; the plaintext
six-digit code exists only in memory and in the delivered message body.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from cryptography.fernet import Fernet

from .models import notifications, recovery_challenges, requests
from .store import (
    InvalidInput,
    json_safe,
    new_id,
    required_text,
    token_digest,
    utcnow,
)

_GENERIC_MESSAGE = "If the details match, a recovery code will be emailed."
_INVALID_DETAIL = "Recovery code is invalid or expired."
_MAX_BEGINS_PER_HOUR = 5
_MAX_ATTEMPTS = 5
_CHALLENGE_LIFETIME = timedelta(minutes=10)


def _normalized_email(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _lookup_hash(tracking: str, email: str) -> str:
    material = f"{tracking}\x00{email}".encode()
    return hashlib.sha256(material).hexdigest()


def _code_hash(challenge_id: str, code: str) -> str:
    material = f"{challenge_id}:{code}".encode()
    return hashlib.sha256(material).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class RecoveryMixin:
    """Issue and redeem bounded recovery challenges for resident requests."""

    # Runtime startup supplies a Fernet key (base64 bytes). The library never
    # generates a fallback key.
    recovery_key: bytes | None = None

    def _recovery_fernet(self) -> Fernet:
        key = self.recovery_key
        if not isinstance(key, (bytes, str)) or not key:
            raise InvalidInput("recovery is not configured")
        try:
            return Fernet(key)
        except (ValueError, TypeError):
            raise InvalidInput("recovery is not configured") from None

    def begin_recovery(self, tracking, email):
        """Begin recovery and always return the same generic public result."""
        tracking = required_text(tracking, 64)
        if not isinstance(email, str):
            raise InvalidInput("email must be a string")
        if len(email) > 320 or "\r" in email or "\n" in email:
            raise InvalidInput("email is invalid")
        fernet = self._recovery_fernet()
        normalized_email = _normalized_email(email)
        lookup_hash = _lookup_hash(tracking, normalized_email)
        now = utcnow()
        challenge_id = new_id()
        code = f"{secrets.randbelow(1_000_000):06d}"
        code_hash = _code_hash(challenge_id, code)
        expires_at = now + _CHALLENGE_LIFETIME
        generic = {"challenge_id": challenge_id, "message": _GENERIC_MESSAGE}

        with self._transaction() as conn:
            recent = conn.execute(
                sa.select(sa.func.count())
                .select_from(recovery_challenges)
                .where(
                    recovery_challenges.c.lookup_hash == lookup_hash,
                    recovery_challenges.c.created_at >= now - timedelta(hours=1),
                )
            ).scalar_one()
            if int(recent) >= _MAX_BEGINS_PER_HOUR:
                # Keep the public response indistinguishable from a normal
                # lookup while declining to persist another challenge.
                return {"challenge_id": None, "message": _GENERIC_MESSAGE}

            request_row = conn.execute(
                sa.select(requests).where(requests.c.tracking == tracking)
            ).mappings().first()

            matched = None
            if request_row is not None:
                contact = request_row.get("contact")
                stored_email = _normalized_email(
                    contact.get("email") if isinstance(contact, dict) else None
                )
                if normalized_email and stored_email == normalized_email:
                    matched = dict(request_row)

            conn.execute(
                sa.insert(recovery_challenges).values(
                    id=challenge_id,
                    request_id=matched["id"] if matched is not None else None,
                    lookup_hash=lookup_hash,
                    code_hash=code_hash,
                    # Ciphertext belongs only to the outbox payload.
                    encrypted_code=None,
                    created_at=now,
                    expires_at=expires_at,
                    attempts=0,
                    used_at=None,
                )
            )

            if matched is None:
                return generic

            event_id = self._event(
                conn,
                matched,
                "resident",
                "recovery_requested",
                "Recovery code requested.",
                False,
                payload={"challenge_id": challenge_id},
            )
            recipient = _normalized_email(
                (matched.get("contact") or {}).get("email")
                if isinstance(matched.get("contact"), dict)
                else None
            )
            encrypted_code = fernet.encrypt(code.encode("utf-8")).decode("ascii")
            conn.execute(
                sa.insert(notifications).values(
                    id=new_id(),
                    request_id=matched["id"],
                    event_id=event_id,
                    recipient=recipient,
                    payload=json_safe(
                        {
                            "kind": "recovery",
                            "challenge_id": challenge_id,
                            "encrypted_code": encrypted_code,
                            "tracking": matched["tracking"],
                            "expires_at": expires_at.isoformat(),
                        }
                    ),
                    state="pending",
                    attempts=0,
                    created_at=now,
                )
            )
            return generic

    def complete_recovery(self, challenge_id, code, new_token):
        """Redeem a recovery code, committing failed attempt counts as well."""
        challenge_id = required_text(challenge_id, 36)
        # Validate the replacement token before mutating the challenge. A bad
        # caller token is a programming error rather than a recovery attempt.
        new_token_hash = token_digest(new_token)
        invalid = {"recovered": False, "detail": _INVALID_DETAIL}
        if not isinstance(code, str):
            code = ""
        now = utcnow()

        with self._transaction() as conn:
            challenge = conn.execute(
                sa.select(recovery_challenges)
                .where(recovery_challenges.c.id == challenge_id)
                .with_for_update()
            ).mappings().first()
            if challenge is None:
                return invalid
            if (
                challenge["request_id"] is None
                or challenge["used_at"] is not None
                or _as_utc(challenge["expires_at"]) <= now
                or challenge["attempts"] >= _MAX_ATTEMPTS
            ):
                return invalid

            expected_hash = _code_hash(challenge_id, code)
            if not hmac.compare_digest(challenge["code_hash"], expected_hash):
                # This update is intentionally not conditional on the final
                # return value: a wrong attempt must persist even though the
                # command itself returns a normal failure result.
                conn.execute(
                    sa.update(recovery_challenges)
                    .where(
                        recovery_challenges.c.id == challenge_id,
                        recovery_challenges.c.used_at.is_(None),
                        recovery_challenges.c.attempts < _MAX_ATTEMPTS,
                    )
                    .values(attempts=recovery_challenges.c.attempts + 1)
                )
                return invalid

            # Exactly one concurrent accepted code may claim the challenge.
            claimed = conn.execute(
                sa.update(recovery_challenges)
                .where(
                    recovery_challenges.c.id == challenge_id,
                    recovery_challenges.c.used_at.is_(None),
                    recovery_challenges.c.attempts == challenge["attempts"],
                    recovery_challenges.c.expires_at > now,
                )
                .values(used_at=now)
            )
            if claimed.rowcount != 1:
                return invalid

            request_row = conn.execute(
                sa.select(requests)
                .where(requests.c.id == challenge["request_id"])
                .with_for_update()
            ).mappings().first()
            if request_row is None:
                return invalid

            conn.execute(
                sa.update(requests)
                .where(requests.c.id == request_row["id"])
                .values(
                    resident_token_hash=new_token_hash,
                    revision=requests.c.revision + 1,
                    updated_at=now,
                )
            )
            updated = self._row(conn, request_row["id"])
            self._event(
                conn,
                updated,
                "resident",
                "recovery",
                "Resident access token recovered.",
                False,
                payload={"challenge_id": challenge_id},
            )
            return {
                "recovered": True,
                "request": self._project(conn, updated, resident=True),
            }


__all__ = ["RecoveryMixin"]
