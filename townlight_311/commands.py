"""Atomic resident command boundary for Townlight 311.

The resident command API is deliberately narrow. Staff authentication and all
staff operations stay in Suite; this module exposes only resident-owned
operations and records their outcome in an idempotency receipt inside the same
database transaction as the operation itself.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from .models import command_receipts
from .store import Conflict, InvalidInput, json_safe, required_text, utcnow

_RESIDENT_OPERATIONS = frozenset(
    {
        "create_request",
        "read_request",
        "comment",
        "reopen",
        "upload_attachment",
        "download_attachment",
        "begin_recovery",
        "complete_recovery",
    }
)

_RECOVERY_FIELDS = {
    "begin_recovery": frozenset({"tracking", "email"}),
    "complete_recovery": frozenset({"challenge_id", "code", "new_token"}),
}


def command_fingerprint(operation: str, payload: dict) -> str:
    canonical = json.dumps(
        {"operation": operation, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CommandMixin:
    """Execute the small set of resident commands with an idempotency receipt."""

    def execute_resident_command(self, command_id, operation, payload):
        """Run one resident command atomically and replay its safe result.

        ``command_id`` identifies a resident action across a host restart. The
        receipt, request mutations, events and outbox notification rows commit
        together. A duplicate command identifier either returns the receipt
        result (same canonical payload) or raises ``Conflict``.
        """
        command_id = required_text(command_id, 36)
        if not isinstance(operation, str) or operation not in _RESIDENT_OPERATIONS:
            raise InvalidInput("unsupported resident command operation")
        if not isinstance(payload, dict):
            raise InvalidInput("command payload must be an object")
        try:
            fingerprint = command_fingerprint(operation, payload)
        except (TypeError, ValueError) as exc:
            raise InvalidInput("command payload must be JSON serializable") from exc

        try:
            # The placeholder, operation mutations and final result share one
            # transaction. Nested repository calls reuse this bound connection.
            with self._bind_transaction() as conn:
                conn.execute(
                    sa.insert(command_receipts).values(
                        command_id=command_id,
                        fingerprint=fingerprint,
                        result={},
                        created_at=utcnow(),
                    )
                )
                result = json_safe(
                    self._run_resident_operation(command_id, operation, payload)
                )
                conn.execute(
                    sa.update(command_receipts)
                    .where(command_receipts.c.command_id == command_id)
                    .values(result=result)
                )
                return result
        except IntegrityError:
            # The losing writer must observe the committed receipt outside its
            # rolled-back transaction.
            with self._transaction() as conn:
                existing = conn.execute(
                    sa.select(command_receipts).where(
                        command_receipts.c.command_id == command_id
                    )
                ).mappings().first()
            if existing is None:
                raise
            if not hmac.compare_digest(existing["fingerprint"], fingerprint):
                raise Conflict(
                    "command id reused with a different operation or payload"
                )
            return json_safe(existing["result"])

    def _run_resident_operation(self, command_id, operation, payload):
        if operation in _RECOVERY_FIELDS:
            if set(payload) != _RECOVERY_FIELDS[operation]:
                raise InvalidInput("command payload has unexpected or missing fields")
            if operation == "begin_recovery":
                return self.begin_recovery(
                    payload["tracking"],
                    payload["email"],
                )
            return self.complete_recovery(
                payload["challenge_id"],
                payload["code"],
                payload["new_token"],
            )

        if operation == "create_request":
            return self.create_request(
                category=payload.get("category"),
                description=payload.get("description"),
                location=payload.get("location"),
                contact=payload.get("contact"),
                resident_token=payload.get("resident_token"),
                # Each command owns its create identity. Any caller-supplied
                # key is ignored so different commands cannot share a request.
                idempotency_key=command_id,
                actor="resident",
            )

        if operation == "read_request":
            request_id = required_text(payload.get("request_id"), 36)
            return self.get_resident(
                request_id,
                payload.get("resident_token"),
            )

        if operation == "comment":
            request_id = required_text(payload.get("request_id"), 36)
            return self.add_comment(
                request_id,
                payload.get("expected_revision"),
                "resident",
                payload.get("message"),
                True,
                resident_token=payload.get("resident_token"),
            )

        if operation == "reopen":
            request_id = required_text(payload.get("request_id"), 36)
            return self.reopen_request(
                request_id,
                payload.get("expected_revision"),
                payload.get("resident_token"),
                payload.get("reason"),
            )

        if operation == "upload_attachment":
            request_id = required_text(payload.get("request_id"), 36)
            encoded = payload.get("content_base64")
            if not isinstance(encoded, str):
                raise InvalidInput("content_base64 must be a string")
            try:
                blob = base64.b64decode(encoded.encode("ascii"), validate=True)
            except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
                raise InvalidInput("content_base64 is not valid base64") from exc
            if len(blob) > self._MAX_ATTACHMENT_BYTES:
                raise InvalidInput("attachment exceeds the maximum size")
            return self.add_attachment(
                request_id,
                payload.get("expected_revision"),
                "resident",
                name=payload.get("name"),
                media_type=payload.get("media_type"),
                content=blob,
                public=True,
                resident_token=payload.get("resident_token"),
            )

        if operation == "download_attachment":
            request_id = required_text(payload.get("request_id"), 36)
            attachment_id = required_text(payload.get("attachment_id"), 36)
            attachment = self.get_attachment_resident(
                request_id,
                attachment_id,
                payload.get("resident_token"),
            )
            return {
                "name": attachment["name"],
                "media_type": attachment["media_type"],
                "sha256": attachment["sha256"],
                "size": attachment["size"],
                "content_base64": base64.b64encode(
                    attachment["content"]
                ).decode("ascii"),
            }

        # Unreachable because the caller validates the allow-list first.
        raise InvalidInput("unsupported resident command operation")


__all__ = ["CommandMixin", "command_fingerprint"]
