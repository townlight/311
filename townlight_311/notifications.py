"""Durable notification processing and reusable SMTP transport for Townlight 311."""

from __future__ import annotations

import re
import smtplib
import ssl
import uuid
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any

import sqlalchemy as sa
from cryptography.fernet import Fernet, InvalidToken

from .models import SCHEMA, notifications, recovery_challenges

_LOCALHOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_SAFE_CONNECT_ERROR = "SMTP connection or authentication failed"
_SAFE_RETRY_ERROR = "SMTP delivery failed before message was sent"
_SAFE_REJECTION_ERROR = "SMTP rejected the message"
_SAFE_UNCERTAIN_ERROR = "Delivery status unknown after the message was sent"
_SAFE_INVALID_RECIPIENT = "Notification recipient is invalid"
_SAFE_MAX_ATTEMPTS = "Maximum delivery attempts reached"
_SAFE_LEASE_EXPIRED = "Delivery status unknown after worker lease expired"
_SAFE_RECOVERY_INVALID = "Recovery notification is invalid or expired"
_RECOVERY_CODE = re.compile(r"^\d{6}$")


class DeliveryError(Exception):
    """Base class for sanitized transport failures."""


class SafeRetryError(DeliveryError):
    """The message was not sent and a later retry is safe."""


class RejectionError(DeliveryError):
    """SMTP explicitly rejected the message; retry depends on the SMTP code."""

    def __init__(self, message: str, *, retryable: bool, code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code


class UncertainDeliveryError(DeliveryError):
    """SMTP acceptance is unknown; automatic retry is forbidden."""


class InvalidRecipientError(DeliveryError):
    """The recipient address cannot safely be used in a message header."""


def _valid_recipient(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidRecipientError(_SAFE_INVALID_RECIPIENT)
    recipient = value.strip()
    if not recipient or "\r" in recipient or "\n" in recipient:
        raise InvalidRecipientError(_SAFE_INVALID_RECIPIENT)
    display_name, address = parseaddr(recipient)
    if display_name or address != recipient or "@" not in recipient:
        raise InvalidRecipientError(_SAFE_INVALID_RECIPIENT)
    local, _, domain = recipient.partition("@")
    if not local or not domain or any(char.isspace() for char in recipient):
        raise InvalidRecipientError(_SAFE_INVALID_RECIPIENT)
    return recipient


def _safe_header(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())


def _retryable_codes(codes: list[int]) -> bool:
    return any(400 <= code < 500 for code in codes)


class SMTPTransport:
    """Small SMTP transport. The caller supplies host, sender, and credentials.

    TLS is always required unless the caller explicitly enables the insecure
    localhost test mode. Credentials are never included in raised messages.
    """

    def __init__(
        self,
        host: str,
        port: int,
        sender: str,
        credentials: tuple[str, str] | dict[str, str] | None = None,
        *,
        use_tls: bool = True,
        allow_insecure_localhost: bool = False,
        timeout: float = 30.0,
    ) -> None:
        if not isinstance(host, str) or not host.strip() or "\r" in host or "\n" in host:
            raise ValueError("SMTP host is required")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("SMTP port is invalid")
        if not isinstance(sender, str) or "\r" in sender or "\n" in sender:
            raise ValueError("SMTP sender is invalid")
        if not use_tls and not (allow_insecure_localhost and host.strip() in _LOCALHOSTS):
            raise ValueError("TLS is required except for explicit localhost testing")
        username = password = None
        if isinstance(credentials, dict):
            username = credentials.get("username")
            password = credentials.get("password")
        elif isinstance(credentials, tuple) and len(credentials) == 2:
            username, password = credentials
        elif credentials is not None:
            raise ValueError("SMTP credentials must be a username/password pair")
        if (username is None) != (password is None):
            raise ValueError("SMTP credentials are incomplete")
        self.host = host.strip()
        self.port = port
        self.sender = sender.strip()
        self._username = username
        self._password = password
        self.use_tls = use_tls
        self.timeout = timeout

    def _connect(self) -> smtplib.SMTP:
        client: smtplib.SMTP | None = None
        try:
            context = ssl.create_default_context()
            if self.use_tls and self.port == 465:
                client = smtplib.SMTP_SSL(
                    self.host, self.port, timeout=self.timeout, context=context
                )
            else:
                client = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
                if self.use_tls:
                    client.ehlo()
                    client.starttls(context=context)
                    client.ehlo()
            if self._username is not None:
                client.login(self._username, self._password or "")
            return client
        except (smtplib.SMTPException, OSError, TimeoutError):
            if client is not None:
                try:
                    client.close()
                except (smtplib.SMTPException, OSError):
                    pass
            raise SafeRetryError(_SAFE_CONNECT_ERROR) from None

    def send(self, message: EmailMessage) -> str:
        """Send one message. A return value means SMTP accepted it, not delivery."""
        recipient = _valid_recipient(message.get("To"))
        if "\r" in self.sender or "\n" in self.sender or not self.sender:
            raise InvalidRecipientError("SMTP sender is invalid")

        client = self._connect()
        try:
            client.send_message(message, from_addr=self.sender, to_addrs=[recipient])
        except smtplib.SMTPRecipientsRefused as exc:
            codes = [code for code, _response in exc.recipients.values()]
            raise RejectionError(
                _SAFE_REJECTION_ERROR,
                retryable=_retryable_codes(codes),
                code=codes[0] if codes else None,
            ) from None
        except smtplib.SMTPSenderRefused as exc:
            code = getattr(exc, "smtp_code", None)
            raise RejectionError(
                _SAFE_REJECTION_ERROR,
                retryable=isinstance(code, int) and 400 <= code < 500,
                code=code if isinstance(code, int) else None,
            ) from None
        except smtplib.SMTPDataError as exc:
            code = getattr(exc, "smtp_code", None)
            raise RejectionError(
                _SAFE_REJECTION_ERROR,
                retryable=isinstance(code, int) and 400 <= code < 500,
                code=code if isinstance(code, int) else None,
            ) from None
        except (smtplib.SMTPServerDisconnected, OSError, TimeoutError):
            raise UncertainDeliveryError(_SAFE_UNCERTAIN_ERROR) from None
        except smtplib.SMTPException:
            raise UncertainDeliveryError(_SAFE_UNCERTAIN_ERROR) from None
        finally:
            try:
                client.quit()
            except (smtplib.SMTPException, OSError):
                pass
        return "accepted"


class NotificationWorker:
    """Finite notification worker with an atomic claim and no daemon loop."""

    def __init__(
        self,
        engine: sa.Engine,
        transport: Any,
        *,
        max_attempts: int = 5,
        lease_seconds: int = 300,
        backoff_base: int = 30,
        backoff_cap: int = 3600,
        recovery_key: bytes | None = None,
    ) -> None:
        if max_attempts < 1 or lease_seconds < 1 or backoff_base < 1 or backoff_cap < 1:
            raise ValueError("worker timing and attempt values must be positive")
        self.engine = (
            engine.execution_options(schema_translate_map={SCHEMA: None})
            if engine.dialect.name == "sqlite"
            else engine
        )
        self.transport = transport
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.recovery_key = recovery_key
        self._recovery_fernet = None
        if recovery_key is not None:
            try:
                self._recovery_fernet = Fernet(recovery_key)
            except (ValueError, TypeError) as exc:
                raise ValueError("recovery_key is not a valid Fernet key") from exc

    def _due_filter(self, now: datetime) -> tuple[Any, ...]:
        return (
            notifications.c.state == "pending",
            sa.or_(
                notifications.c.next_attempt_at.is_(None),
                notifications.c.next_attempt_at <= now,
            ),
        )

    def _claim(self) -> dict[str, Any] | None:
        now = datetime.now(UTC)
        claim_id = str(uuid.uuid4())
        lease_until = now + timedelta(seconds=self.lease_seconds)
        with self.engine.begin() as conn:
            if self.engine.dialect.name == "postgresql":
                row = conn.execute(
                    sa.select(notifications.c.id)
                    .where(*self._due_filter(now))
                    .order_by(notifications.c.created_at, notifications.c.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                ).first()
                if row is None:
                    return None
                result = conn.execute(
                    sa.update(notifications)
                    .where(
                        notifications.c.id == row.id,
                        notifications.c.state == "pending",
                    )
                    .values(
                        state="sending",
                        attempts=notifications.c.attempts + 1,
                        claim_id=claim_id,
                        lease_until=lease_until,
                    )
                )
            else:
                candidate = (
                    sa.select(notifications.c.id)
                    .where(*self._due_filter(now))
                    .order_by(notifications.c.created_at, notifications.c.id)
                    .limit(1)
                    .scalar_subquery()
                )
                result = conn.execute(
                    sa.update(notifications)
                    .where(
                        notifications.c.id == candidate,
                        notifications.c.state == "pending",
                    )
                    .values(
                        state="sending",
                        attempts=notifications.c.attempts + 1,
                        claim_id=claim_id,
                        lease_until=lease_until,
                    )
                )
            if result.rowcount != 1:
                return None
            claimed = conn.execute(
                sa.select(notifications).where(notifications.c.claim_id == claim_id)
            ).mappings().first()
            return dict(claimed) if claimed is not None else None

    def _set_outcome(
        self,
        notification_id: str,
        claim_id: str,
        values: dict[str, Any],
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                sa.update(notifications)
                .where(
                    notifications.c.id == notification_id,
                    notifications.c.claim_id == claim_id,
                    notifications.c.state == "sending",
                )
                .values(**values)
            )

    def _current_state(self, notification_id: str) -> str | None:
        with self.engine.connect() as conn:
            state = conn.execute(
                sa.select(notifications.c.state).where(notifications.c.id == notification_id)
            ).scalar_one_or_none()
        return state

    def _backoff(self, attempts: int) -> timedelta:
        delay = self.backoff_base * (2 ** max(0, attempts - 1))
        return timedelta(seconds=min(delay, self.backoff_cap))

    def _recovery_challenge_active(self, challenge_id: Any) -> bool:
        if not isinstance(challenge_id, str):
            return False
        now = datetime.now(UTC)
        with self.engine.connect() as conn:
            row = conn.execute(
                sa.select(recovery_challenges).where(
                    recovery_challenges.c.id == challenge_id
                )
            ).mappings().first()
        if row is None or row["request_id"] is None or row["used_at"] is not None:
            return False
        expires_at = row["expires_at"]
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at > now

    def _recovery_message(
        self, row: dict[str, Any], payload: dict[str, Any], recipient: str
    ) -> EmailMessage:
        if self._recovery_fernet is None:
            raise RejectionError(_SAFE_RECOVERY_INVALID, retryable=False)
        if not self._recovery_challenge_active(payload.get("challenge_id")):
            raise RejectionError(_SAFE_RECOVERY_INVALID, retryable=False)

        raw_expiry = payload.get("expires_at")
        if not isinstance(raw_expiry, str):
            raise RejectionError(_SAFE_RECOVERY_INVALID, retryable=False)
        try:
            expires_at = datetime.fromisoformat(raw_expiry)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= datetime.now(UTC):
                raise RejectionError(_SAFE_RECOVERY_INVALID, retryable=False)
        except RejectionError:
            raise
        except (TypeError, ValueError):
            raise RejectionError(_SAFE_RECOVERY_INVALID, retryable=False) from None

        encrypted_code = payload.get("encrypted_code")
        if not isinstance(encrypted_code, str):
            raise RejectionError(_SAFE_RECOVERY_INVALID, retryable=False)
        try:
            code = self._recovery_fernet.decrypt(
                encrypted_code.encode("ascii")
            ).decode("ascii")
        except (InvalidToken, UnicodeError, ValueError, TypeError):
            raise RejectionError(_SAFE_RECOVERY_INVALID, retryable=False) from None
        if not _RECOVERY_CODE.fullmatch(code):
            raise RejectionError(_SAFE_RECOVERY_INVALID, retryable=False)

        tracking = _safe_header(payload.get("tracking"))
        message = EmailMessage()
        message["To"] = recipient
        sender = getattr(self.transport, "sender", "")
        if sender:
            message["From"] = sender
        message["Subject"] = f"Townlight 311 recovery code {tracking}".strip()
        message["Message-ID"] = f"<{row['id']}@townlight.local>"
        body = (
            f"Tracking: {tracking}\n\n"
            f"Your recovery code is: {code}\n\n"
            "This code expires in 10 minutes. If you did not request it, ignore "
            "this message."
        )
        message.set_content(body)
        return message

    def _message(self, row: dict[str, Any]) -> EmailMessage:
        payload = row.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        recipient = _valid_recipient(row.get("recipient"))
        if payload.get("kind") == "recovery":
            return self._recovery_message(row, payload, recipient)

        tracking = _safe_header(payload.get("tracking"))
        state = _safe_header(payload.get("state"))
        event_message = payload.get("event_message")
        body_event = event_message if isinstance(event_message, str) else ""

        message = EmailMessage()
        message["To"] = recipient
        sender = getattr(self.transport, "sender", "")
        if sender:
            message["From"] = sender
        message["Subject"] = f"Townlight 311 {tracking} update: {state}".strip()
        message["Message-ID"] = f"<{row['id']}@townlight.local>"
        body = f"Tracking: {tracking}\nState: {state}\n\n{body_event}".strip()
        message.set_content(body)
        return message

    def _retry_or_fail(
        self, notification_id: str, claim_id: str, attempts: int, reason: str
    ) -> str:
        if attempts >= self.max_attempts:
            self._set_outcome(
                notification_id,
                claim_id,
                {
                    "state": "failed",
                    "next_attempt_at": None,
                    "last_error": _SAFE_MAX_ATTEMPTS,
                    "claim_id": None,
                    "lease_until": None,
                },
            )
            return "failed"
        self._set_outcome(
            notification_id,
            claim_id,
            {
                "state": "pending",
                "next_attempt_at": datetime.now(UTC) + self._backoff(attempts),
                "last_error": reason,
                "claim_id": None,
                "lease_until": None,
            },
        )
        return "pending"

    def process_one(self) -> dict[str, Any] | None:
        """Claim and process at most one due notification. Returns its outcome."""
        row = self._claim()
        if row is None:
            return None
        notification_id = row["id"]
        claim_id = row["claim_id"]
        attempts = row["attempts"]
        final_state: str
        try:
            message = self._message(row)
            self.transport.send(message)
        except InvalidRecipientError:
            self._set_outcome(
                notification_id,
                claim_id,
                {
                    "state": "failed",
                    "next_attempt_at": None,
                    "last_error": _SAFE_INVALID_RECIPIENT,
                    "claim_id": None,
                    "lease_until": None,
                },
            )
            final_state = "failed"
        except RejectionError as exc:
            if exc.retryable:
                final_state = self._retry_or_fail(
                    notification_id, claim_id, attempts, _SAFE_REJECTION_ERROR
                )
            else:
                self._set_outcome(
                    notification_id,
                    claim_id,
                    {
                        "state": "failed",
                        "next_attempt_at": None,
                        "last_error": _SAFE_REJECTION_ERROR,
                        "claim_id": None,
                        "lease_until": None,
                    },
                )
                final_state = "failed"
        except SafeRetryError:
            final_state = self._retry_or_fail(
                notification_id, claim_id, attempts, _SAFE_RETRY_ERROR
            )
        except UncertainDeliveryError:
            self._set_outcome(
                notification_id,
                claim_id,
                {
                    "state": "uncertain",
                    "next_attempt_at": None,
                    "last_error": _SAFE_UNCERTAIN_ERROR,
                    "claim_id": None,
                    "lease_until": None,
                },
            )
            final_state = "uncertain"
        except Exception:  # noqa: BLE001 - unknown SMTP acceptance must be uncertain
            self._set_outcome(
                notification_id,
                claim_id,
                {
                    "state": "uncertain",
                    "next_attempt_at": None,
                    "last_error": _SAFE_UNCERTAIN_ERROR,
                    "claim_id": None,
                    "lease_until": None,
                },
            )
            final_state = "uncertain"
        else:
            self._set_outcome(
                notification_id,
                claim_id,
                {
                    "state": "sent",
                    "sent_at": datetime.now(UTC),
                    "next_attempt_at": None,
                    "last_error": None,
                    "claim_id": None,
                    "lease_until": None,
                },
            )
            final_state = "sent"

        return {
            "id": notification_id,
            "state": self._current_state(notification_id) or final_state,
            "attempts": attempts,
        }

    def recover_expired(self) -> int:
        """Mark expired in-flight sends uncertain. They are never retried."""
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            result = conn.execute(
                sa.update(notifications)
                .where(
                    notifications.c.state == "sending",
                    sa.or_(
                        notifications.c.lease_until.is_(None),
                        notifications.c.lease_until <= now,
                    ),
                )
                .values(
                    state="uncertain",
                    next_attempt_at=None,
                    last_error=_SAFE_LEASE_EXPIRED,
                    claim_id=None,
                    lease_until=None,
                )
            )
        return int(result.rowcount or 0)


__all__ = [
    "DeliveryError",
    "InvalidRecipientError",
    "NotificationWorker",
    "RejectionError",
    "SMTPTransport",
    "SafeRetryError",
    "UncertainDeliveryError",
]
