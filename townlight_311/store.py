from __future__ import annotations

import contextlib
import contextvars
import hashlib
import hmac
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import sqlalchemy as sa

from .models import (
    REQUEST_STATES,
    SCHEMA,
    attachments,
    events,
    metadata,
    notifications,
    requests,
)

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_RESIDENT_FIELDS = (
    "id",
    "tracking",
    "category",
    "description",
    "location",
    "state",
    "priority",
    "revision",
    "created_at",
    "updated_at",
)

# Attachment metadata columns safe to expose in request projections. Raw
# content bytes are deliberately excluded.
_ATTACHMENT_FIELDS = (
    "id",
    "name",
    "media_type",
    "size",
    "sha256",
    "public",
    "created_at",
)

# A repository-scoped connection may be bound for the duration of a resident
# command. ContextVars keep the scope nested and thread/task local.
_BOUND_CONNECTION: contextvars.ContextVar[
    tuple[object, sa.Connection] | None
] = contextvars.ContextVar("townlight_bound_connection", default=None)


class InvalidInput(ValueError):
    pass


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


class Unauthorized(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


def required_text(value: Any, maxlen: int) -> str:
    if not isinstance(value, str):
        raise InvalidInput("text value required")
    text = value.strip()
    if not text or len(text) > maxlen:
        raise InvalidInput("text value is empty or too long")
    return text


def token_digest(token: Any) -> str:
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise InvalidInput("token must be 32-128 URL-safe characters")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


class RepositoryBase:
    """SQLAlchemy Core repository for the internal Townlight 311 domain."""

    def __init__(self, engine: sa.Engine, *, bootstrap: bool = True):
        dialect = engine.dialect.name
        if dialect == "sqlite":
            self.engine = engine.execution_options(schema_translate_map={SCHEMA: None})
        else:
            self.engine = engine
        self._connection_identity = object()

        if bootstrap:
            # Constructor DDL runs outside any command-scoped connection.
            with self.engine.begin() as conn:
                if dialect == "postgresql":
                    conn.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))
                metadata.create_all(conn)

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sa.Connection]:
        """Yield the command-bound connection, otherwise a fresh transaction.

        Nested repository operations started by a resident command reuse the
        same connection without committing or closing it. Operations invoked
        outside a command scope retain their normal transaction behaviour.
        """
        bound = _BOUND_CONNECTION.get()
        if bound is not None and bound[0] is self._connection_identity:
            yield bound[1]
            return
        with self.engine.begin() as conn:
            yield conn

    @contextlib.contextmanager
    def _bind_transaction(self) -> Iterator[sa.Connection]:
        """Open one transaction and bind it for nested repository calls."""
        with self.engine.begin() as conn:
            token = _BOUND_CONNECTION.set((self._connection_identity, conn))
            try:
                yield conn
            finally:
                _BOUND_CONNECTION.reset(token)

    def _row(self, conn: sa.Connection, request_id: str) -> dict[str, Any]:
        row = conn.execute(
            sa.select(requests).where(requests.c.id == request_id)
        ).mappings().first()
        if row is None:
            raise NotFound("request not found")
        return dict(row)

    def _resident(self, row: dict[str, Any], token: str) -> None:
        digest = token_digest(token)
        expected = row.get("resident_token_hash") or ""
        if not hmac.compare_digest(expected, digest):
            raise Unauthorized("resident token does not match")

    def _event(
        self,
        conn: sa.Connection,
        row: dict[str, Any],
        actor: str,
        kind: str,
        message: str,
        public: bool,
        payload: dict[str, Any] | None = None,
    ) -> str:
        actor = required_text(actor, 255)
        kind = required_text(kind, 64)
        message = required_text(message, 10000)
        if not isinstance(public, bool):
            raise InvalidInput("public must be a boolean")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise InvalidInput("payload must be an object")
        now = utcnow()
        event_id = new_id()
        conn.execute(
            sa.insert(events).values(
                id=event_id,
                request_id=row["id"],
                actor=actor,
                kind=kind,
                public=public,
                message=message,
                payload=json_safe(payload),
                created_at=now,
            )
        )
        contact = row.get("contact") or {}
        email = contact.get("email") if isinstance(contact, dict) else None
        if public and isinstance(email, str) and email.strip():
            try:
                recipient = required_text(email, 320)
            except InvalidInput:
                recipient = None
            if recipient is not None:
                notification_payload = {
                    "tracking": row["tracking"],
                    "state": row["state"],
                    "event_message": message,
                }
                conn.execute(
                    sa.insert(notifications).values(
                        id=new_id(),
                        request_id=row["id"],
                        event_id=event_id,
                        recipient=recipient,
                        payload=json_safe(notification_payload),
                        state="pending",
                        attempts=0,
                        created_at=now,
                    )
                )
        return event_id

    def _project(
        self,
        conn: sa.Connection,
        row: dict[str, Any],
        resident: bool = False,
    ) -> dict[str, Any]:
        event_query = sa.select(events).where(events.c.request_id == row["id"])
        if resident:
            event_query = event_query.where(events.c.public.is_(True))
        event_rows = conn.execute(
            event_query.order_by(events.c.created_at)
        ).mappings().all()

        attachment_query = sa.select(
            *(attachments.c[field] for field in _ATTACHMENT_FIELDS)
        ).where(attachments.c.request_id == row["id"])
        if resident:
            attachment_query = attachment_query.where(
                attachments.c.public.is_(True)
            )
        attachment_query = attachment_query.order_by(
            attachments.c.created_at,
            attachments.c.id,
        )
        attachment_rows = conn.execute(attachment_query).mappings().all()

        if resident:
            request_data = {key: row.get(key) for key in _RESIDENT_FIELDS if key in row}
            projected_events = [
                {
                    "message": event["message"],
                    "kind": event["kind"],
                    "created_at": event["created_at"],
                }
                for event in event_rows
            ]
        else:
            request_data = {
                key: value
                for key, value in row.items()
                if key != "resident_token_hash"
            }
            projected_events = [dict(event) for event in event_rows]

        projected_attachments = [
            {
                "id": attachment["id"],
                "name": attachment["name"],
                "media_type": attachment["media_type"],
                "size": attachment["size"],
                "sha256": attachment["sha256"],
                "public": bool(attachment["public"]),
                "created_at": attachment["created_at"],
            }
            for attachment in attachment_rows
        ]

        projected_notifications = None
        if not resident:
            notification_rows = conn.execute(
                sa.select(
                    notifications.c.id,
                    notifications.c.event_id,
                    notifications.c.state,
                    notifications.c.attempts,
                    notifications.c.next_attempt_at,
                    notifications.c.sent_at,
                    notifications.c.created_at,
                )
                .where(notifications.c.request_id == row["id"])
                .order_by(
                    notifications.c.created_at,
                    notifications.c.id,
                )
            ).mappings().all()
            projected_notifications = [
                dict(notification) for notification in notification_rows
            ]

        result = {
            **request_data,
            "events": projected_events,
            "attachments": projected_attachments,
        }
        if not resident:
            result["notifications"] = projected_notifications
        return json_safe(result)

    def get_staff(self, request_id: str) -> dict[str, Any]:
        """Return a staff projection. Caller must authenticate staff."""
        with self._transaction() as conn:
            row = self._row(conn, request_id)
            return self._project(conn, row, resident=False)

    def get_resident(self, request_id: str, token: str) -> dict[str, Any]:
        with self._transaction() as conn:
            row = self._row(conn, request_id)
            self._resident(row, token)
            return self._project(conn, row, resident=True)

    def list_requests(
        self,
        department: str | None = None,
        state: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List staff projections. Caller must authenticate staff."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise InvalidInput("limit must be between 1 and 200")
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or not 0 <= offset <= 10_000_000
        ):
            raise InvalidInput("offset must be between 0 and 10000000")
        query = sa.select(requests)
        if department is not None:
            query = query.where(requests.c.department == required_text(department, 255))
        if state is not None:
            if state not in REQUEST_STATES:
                raise InvalidInput("invalid request state")
            query = query.where(requests.c.state == state)
        query = (
            query.order_by(
                requests.c.created_at.desc(),
                requests.c.id.asc(),
            )
            .offset(offset)
            .limit(limit)
        )
        with self._transaction() as conn:
            rows = conn.execute(query).mappings().all()
            return [self._project(conn, dict(row), resident=False) for row in rows]


__all__ = [
    "Conflict",
    "InvalidInput",
    "NotFound",
    "RepositoryBase",
    "Unauthorized",
    "json_safe",
    "new_id",
    "required_text",
    "token_digest",
    "utcnow",
]
