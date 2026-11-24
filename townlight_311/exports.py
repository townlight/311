from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
from typing import Any

import sqlalchemy as sa

from .attachments import safe_attachment_name
from .models import attachments, events, requests
from .store import Conflict, NotFound, json_safe, required_text

_PRIVATE_EXPORT_FIELDS = ("resident_token_hash", "idempotency_key", "input_sha256")
_OPEN311_OPEN_STATES = frozenset(
    {"submitted", "triaged", "assigned", "in_progress", "reopened"}
)


def _attachment_uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise Conflict("stored attachment id is missing")
    try:
        return str(uuid.UUID(value))
    except (AttributeError, ValueError):
        raise Conflict("stored attachment id is invalid") from None


def _zip_info(name: str) -> zipfile.ZipInfo:
    """Return a ZipInfo with a fixed timestamp for deterministic archives."""
    info = zipfile.ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


class ExportMixin:
    """Export helpers for staff archives and resident Open311 projections."""

    def archive(self, request_id: str) -> bytes:
        """Return a consistent ZIP snapshot for a staff-authorized caller.

        The request row is locked before the audit trail and attachments are
        read. On PostgreSQL this is a ``SELECT ... FOR UPDATE``; on SQLite the
        fresh connection begins with ``BEGIN IMMEDIATE`` so that concurrent
        mutators (all of which update the request row) cannot interleave.
        """
        request_id = required_text(request_id, 36)

        with self.engine.connect() as conn:
            try:
                if conn.dialect.name == "sqlite":
                    conn.exec_driver_sql("BEGIN IMMEDIATE")
                    lock_query = sa.select(requests).where(
                        requests.c.id == request_id
                    )
                else:
                    lock_query = (
                        sa.select(requests)
                        .where(requests.c.id == request_id)
                        .with_for_update()
                    )

                row = conn.execute(lock_query).mappings().first()
                if row is None:
                    raise NotFound("request not found")
                row = dict(row)

                event_rows = conn.execute(
                    sa.select(events)
                    .where(events.c.request_id == row["id"])
                    .order_by(events.c.created_at)
                ).mappings().all()
                attachment_rows = conn.execute(
                    sa.select(attachments)
                    .where(attachments.c.request_id == row["id"])
                    .order_by(attachments.c.created_at)
                ).mappings().all()

                record = {
                    key: value
                    for key, value in row.items()
                    if key not in _PRIVATE_EXPORT_FIELDS
                }
                record["events"] = [dict(event) for event in event_rows]
                request_bytes = json.dumps(
                    json_safe(record),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")

                members: list[tuple[str, bytes]] = [("request.json", request_bytes)]
                seen_members = {"request.json"}

                for attachment in attachment_rows:
                    attachment = dict(attachment)
                    content = attachment.get("content")
                    if not isinstance(content, (bytes, bytearray)):
                        raise Conflict("stored attachment content is corrupt")
                    content = bytes(content)

                    stored_size = attachment.get("size")
                    if (
                        isinstance(stored_size, bool)
                        or not isinstance(stored_size, int)
                        or stored_size != len(content)
                    ):
                        raise Conflict("stored attachment size is corrupt")

                    stored_sha256 = attachment.get("sha256")
                    digest = hashlib.sha256(content).hexdigest()
                    if (
                        not isinstance(stored_sha256, str)
                        or stored_sha256.lower() != digest
                    ):
                        raise Conflict("stored attachment hash is corrupt")

                    member_name = (
                        f"attachments/{_attachment_uuid(attachment.get('id'))}"
                        f"/{safe_attachment_name(attachment.get('name'))}"
                    )
                    if member_name in seen_members:
                        raise Conflict("archive member name is duplicated")
                    seen_members.add(member_name)
                    members.append((member_name, content))

                manifest = [
                    {
                        "name": name,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "size": len(data),
                    }
                    for name, data in members
                ]
                manifest_bytes = json.dumps(
                    manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")

                output = io.BytesIO()
                with zipfile.ZipFile(
                    output, "w", compression=zipfile.ZIP_DEFLATED
                ) as archive:
                    for name, data in members:
                        archive.writestr(_zip_info(name), data)
                    archive.writestr(_zip_info("manifest.json"), manifest_bytes)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        return output.getvalue()

    def open311(self, request_id: str, resident_token: str) -> dict[str, Any]:
        """Return the resident-visible Open311 representation."""
        data = self.get_resident(request_id, resident_token)
        state = data.get("state")
        return {
            "service_request_id": data.get("tracking") or data.get("id"),
            "service_code": data.get("category"),
            "status": "open" if state in _OPEN311_OPEN_STATES else "closed",
            "description": data.get("description"),
            "requested_datetime": data.get("created_at"),
            "updated_datetime": data.get("updated_at"),
            "address": data.get("location"),
        }


__all__ = ["ExportMixin"]
