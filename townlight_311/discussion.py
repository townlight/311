"""Discussion operations for internal Townlight 311 requests."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from .models import events, requests
from .store import Conflict, InvalidInput, required_text, utcnow

_DUPLICATE_LOCK_KEY = 0x744C3331445550


class DiscussionMixin:
    """Comments and resident-owned reopen actions."""

    def _link_duplicate(
        self,
        conn: sa.Connection,
        row: dict[str, Any],
        target_id: str,
    ) -> str:
        """Validate a duplicate link and serialize concurrent link changes.

        The helper only acquires a transaction-scoped lock and validates the
        target chain; it never mutates request rows. The caller performs the
        optimistic update.
        """
        source_id = row["id"]
        if not isinstance(target_id, str):
            raise InvalidInput("duplicate target is required")
        if target_id == source_id:
            raise InvalidInput("a request cannot be a duplicate of itself")

        if conn.dialect.name == "postgresql":
            conn.execute(
                sa.text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _DUPLICATE_LOCK_KEY},
            )
        elif conn.dialect.name == "sqlite":
            # A zero-delta UPDATE on the source row acquires SQLite's write
            # lock before the target chain is inspected.
            conn.execute(
                sa.update(requests)
                .where(requests.c.id == source_id)
                .values(revision=requests.c.revision)
            )

        visited: set[str] = set()
        current: str | None = target_id
        while current is not None:
            if current == source_id:
                raise InvalidInput("duplicate chain would contain a loop")
            if current in visited:
                raise Conflict("duplicate chain already contains a cycle")
            visited.add(current)
            target_row = self._row(conn, current)
            current = target_row.get("duplicate_of")
        return target_id

    def add_comment(
        self,
        request_id,
        expected_revision,
        actor,
        message,
        public,
        *,
        resident_token=None,
    ):
        """Add a comment to a request.

        Staff callers must be authenticated by the surrounding endpoint before
        invoking this method; ``actor`` is descriptive only and is never used
        for authorization. Resident callers prove ownership with
        ``resident_token``; their comment is forced public and attributed to
        ``resident`` regardless of the supplied ``actor`` value.
        """
        request_id = required_text(request_id, 36)
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision <= 0
        ):
            raise InvalidInput("expected_revision must be a positive integer")
        actor = required_text(actor, 255)
        message = required_text(message, 10000)
        if not isinstance(public, bool):
            raise InvalidInput("public must be a boolean")

        with self._transaction() as conn:
            row = self._row(conn, request_id)
            resident = resident_token is not None
            if resident:
                self._resident(row, resident_token)
                actor = "resident"
                public = True
            if row["revision"] != expected_revision:
                raise Conflict("request revision has changed")

            now = utcnow()
            result = conn.execute(
                sa.update(requests)
                .where(
                    requests.c.id == request_id,
                    requests.c.revision == expected_revision,
                )
                .values(revision=expected_revision + 1, updated_at=now)
            )
            if result.rowcount != 1:
                raise Conflict("request revision has changed")

            updated = self._row(conn, request_id)
            self._event(conn, updated, actor, "comment", message, public)
            return self._project(conn, updated, resident=resident)

    def link_record(
        self,
        request_id,
        expected_revision,
        actor,
        target_type,
        target_id,
        label,
    ):
        """Link a verified external record to a request.

        Staff callers must authenticate before invoking this method and must
        verify the target record prior to calling. This method validates only
        the allowed target namespace and does not create target records.
        """
        request_id = required_text(request_id, 36)
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision <= 0
        ):
            raise InvalidInput("expected_revision must be a positive integer")
        actor = required_text(actor, 255)
        target_type = required_text(target_type, 255)
        if target_type not in {"parcel", "permit", "inspection", "parks"}:
            raise InvalidInput("invalid target_type")
        target_id = required_text(target_id, 255)
        label = required_text(label, 500)

        with self._transaction() as conn:
            row = self._row(conn, request_id)
            if row["revision"] != expected_revision:
                raise Conflict("request revision has changed")

            existing = conn.execute(
                sa.select(events).where(
                    events.c.request_id == request_id,
                    events.c.kind == "record-linked",
                )
            ).mappings().all()
            for event in existing:
                payload = event["payload"]
                if (
                    isinstance(payload, dict)
                    and payload.get("target_type") == target_type
                    and payload.get("target_id") == target_id
                ):
                    return self._project(conn, row, resident=False)

            now = utcnow()
            result = conn.execute(
                sa.update(requests)
                .where(
                    requests.c.id == request_id,
                    requests.c.revision == expected_revision,
                )
                .values(revision=expected_revision + 1, updated_at=now)
            )
            if result.rowcount != 1:
                raise Conflict("request revision has changed")

            updated = self._row(conn, request_id)
            self._event(
                conn,
                updated,
                actor,
                "record-linked",
                f"Linked {target_type}: {label}",
                False,
                payload={
                    "target_type": target_type,
                    "target_id": target_id,
                    "label": label,
                },
            )
            return self._project(conn, updated, resident=False)

    def reopen_request(self, request_id, expected_revision, resident_token, reason):
        """Reopen a request as its resident owner.

        The resident token is verified privately before delegating to
        ``transition``. The transition's conditional UPDATE enforces
        ``expected_revision``, preventing a stale resident action from
        overwriting a concurrent staff change. Endpoints must not accept a
        caller-supplied role for this operation.
        """
        request_id = required_text(request_id, 36)
        with self._transaction() as conn:
            row = self._row(conn, request_id)
            self._resident(row, resident_token)
        self.transition(
            request_id,
            expected_revision,
            "resident",
            "reopened",
            reason,
        )
        return self.get_resident(request_id, resident_token)


__all__ = ["DiscussionMixin"]
