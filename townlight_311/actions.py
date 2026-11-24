"""Bounded state-transition action for internal Townlight 311 requests."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa

from .models import REQUEST_STATES, requests
from .store import Conflict, InvalidInput, required_text, utcnow

_ALLOWED_TRANSITIONS = {
    "submitted": frozenset({"triaged", "rejected", "duplicate"}),
    "triaged": frozenset({"assigned", "rejected", "duplicate"}),
    "assigned": frozenset({"assigned", "in_progress", "triaged", "duplicate"}),
    "in_progress": frozenset({"resolved", "assigned"}),
    "resolved": frozenset({"closed", "reopened"}),
    "closed": frozenset({"reopened"}),
    "rejected": frozenset({"reopened"}),
    "duplicate": frozenset({"reopened"}),
    "reopened": frozenset({"triaged", "assigned"}),
}
_PRIORITIES = frozenset({"low", "normal", "high", "urgent"})


class ActionMixin:
    def transition(
        self,
        request_id,
        expected_revision,
        actor,
        target,
        reason,
        *,
        department=None,
        assignee=None,
        priority=None,
        due_at=None,
        duplicate_of=None,
    ):
        request_id = required_text(request_id, 36)
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision <= 0
        ):
            raise InvalidInput("expected_revision must be a positive integer")
        actor = required_text(actor, 255)
        reason = required_text(reason, 10000)
        if target not in REQUEST_STATES:
            raise InvalidInput("invalid target state")

        if department is not None:
            department = required_text(department, 255)
        if assignee is not None:
            assignee = required_text(assignee, 255)
        if priority is not None and priority not in _PRIORITIES:
            raise InvalidInput("invalid priority")
        if due_at is not None and (
            not isinstance(due_at, datetime)
            or due_at.tzinfo is None
            or due_at.utcoffset() is None
        ):
            raise InvalidInput("due_at must be a timezone-aware datetime")

        if target == "duplicate":
            if duplicate_of is None:
                raise InvalidInput("duplicate_of is required")
            duplicate_of = required_text(duplicate_of, 36)

        with self._transaction() as conn:
            row = self._row(conn, request_id)
            if row["revision"] != expected_revision:
                raise Conflict("request revision has changed")

            current = row["state"]
            if target not in _ALLOWED_TRANSITIONS.get(current, frozenset()):
                raise InvalidInput(f"illegal transition from {current} to {target}")

            changes: dict[str, Any] = {"state": target}
            if department is not None:
                changes["department"] = department
            if assignee is not None:
                changes["assignee"] = assignee
            if priority is not None:
                changes["priority"] = priority
            if due_at is not None:
                changes["due_at"] = due_at

            effective_assignee = (
                assignee if assignee is not None else row.get("assignee")
            )
            if target in {"assigned", "in_progress"} and (
                not isinstance(effective_assignee, str)
                or not effective_assignee.strip()
            ):
                raise InvalidInput("assignee required for assigned or in_progress")

            if target == "duplicate":
                changes["duplicate_of"] = self._link_duplicate(
                    conn, row, duplicate_of
                )
            elif target == "reopened":
                changes["duplicate_of"] = None

            changes["revision"] = expected_revision + 1
            changes["updated_at"] = utcnow()

            result = conn.execute(
                sa.update(requests)
                .where(
                    requests.c.id == request_id,
                    requests.c.revision == expected_revision,
                )
                .values(**changes)
            )
            if result.rowcount != 1:
                raise Conflict("request revision has changed")

            updated = self._row(conn, request_id)
            self._event(
                conn,
                updated,
                actor,
                "state_changed",
                reason,
                True,
                payload={
                    "from": current,
                    "to": target,
                    "department": updated.get("department"),
                    "assignee": updated.get("assignee"),
                    "priority": updated.get("priority"),
                    "due_at": updated.get("due_at"),
                },
            )
            return self._project(conn, updated, resident=False)


__all__ = ["ActionMixin"]
