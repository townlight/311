from __future__ import annotations

import hashlib

import sqlalchemy as sa

from .models import attachments, events, requests
from .store import (
    Conflict,
    InvalidInput,
    NotFound,
    new_id,
    required_text,
    utcnow,
)

_WINDOWS_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{index}" for index in range(1, 10)]
    + [f"LPT{index}" for index in range(1, 10)]
)
_WINDOWS_RESERVED_CHARS = frozenset('<>:"|?*')


def safe_attachment_name(value: object) -> str:
    """Validate a platform-neutral, filesystem-safe attachment basename."""
    if not isinstance(value, str):
        raise InvalidInput("attachment name must be a string")
    if not value or len(value) > 255:
        raise InvalidInput("attachment name must be a non-empty basename")
    if value != value.strip():
        raise InvalidInput("attachment name must not have surrounding whitespace")
    if value in {".", ".."} or ".." in value or "/" in value or "\\" in value:
        raise InvalidInput("attachment name must be a safe basename")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise InvalidInput("attachment name contains control characters")
    if any(char in _WINDOWS_RESERVED_CHARS for char in value):
        raise InvalidInput("attachment name contains reserved characters")
    if value[-1] in ". ":
        raise InvalidInput("attachment name must not end with a dot or space")
    basename = value.split(".", 1)[0].rstrip(" .").upper()
    if basename in _WINDOWS_RESERVED_NAMES:
        raise InvalidInput("attachment name is a reserved device name")
    return value


class AttachmentMixin:
    """Attachment operations for the Townlight 311 repository."""

    _MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
    _MEDIA_TYPES = frozenset(
        {
            "image/jpeg",
            "image/png",
            "image/webp",
            "application/pdf",
            "text/plain",
        }
    )

    @staticmethod
    def _attachment_name(name: object) -> str:
        return safe_attachment_name(name)

    def _attachment_content(
        self, media_type: str, content: object
    ) -> tuple[bytes, int, str]:
        if media_type not in self._MEDIA_TYPES:
            raise InvalidInput("unsupported attachment media type")
        if not isinstance(content, (bytes, bytearray)):
            raise InvalidInput("attachment content must be bytes")

        blob = bytes(content)
        size = len(blob)
        if not 1 <= size <= self._MAX_ATTACHMENT_BYTES:
            raise InvalidInput("attachment size must be between 1 byte and 10 MiB")

        if media_type == "image/jpeg" and not blob.startswith(b"\xff\xd8\xff"):
            raise InvalidInput("content does not match image/jpeg")
        if media_type == "image/png" and not blob.startswith(
            b"\x89PNG\r\n\x1a\n"
        ):
            raise InvalidInput("content does not match image/png")
        if media_type == "image/webp" and not (
            len(blob) >= 12
            and blob[:4] == b"RIFF"
            and blob[8:12] == b"WEBP"
        ):
            raise InvalidInput("content does not match image/webp")
        if media_type == "application/pdf" and not blob.startswith(b"%PDF-"):
            raise InvalidInput("content does not match application/pdf")
        if media_type == "text/plain":
            try:
                blob.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise InvalidInput("text/plain attachment must be UTF-8") from exc

        return blob, size, hashlib.sha256(blob).hexdigest()

    def _fetch_attachment(
        self, conn: sa.Connection, request_id: str, attachment_id: str
    ) -> dict[str, object]:
        request_id = required_text(request_id, 36)
        attachment_id = required_text(attachment_id, 36)

        row = conn.execute(
            sa.select(attachments).where(
                attachments.c.id == attachment_id,
                attachments.c.request_id == request_id,
            )
        ).mappings().first()
        if row is None:
            raise NotFound("attachment not found")

        data = dict(row)
        stored_content = data.get("content")
        if not isinstance(stored_content, (bytes, bytearray)):
            raise Conflict("attachment content is missing")

        blob = bytes(stored_content)
        digest = hashlib.sha256(blob).hexdigest()
        if data.get("size") != len(blob) or data.get("sha256") != digest:
            raise Conflict("attachment integrity check failed")

        return {
            "id": data.get("id"),
            "request_id": data.get("request_id"),
            "name": data.get("name"),
            "media_type": data.get("media_type"),
            "content": blob,
            "sha256": digest,
            "size": len(blob),
            "public": bool(data.get("public")),
            "created_at": data.get("created_at"),
        }

    def add_attachment(
        self,
        request_id: str,
        expected_revision: int,
        actor: str,
        *,
        name: str,
        media_type: str,
        content: bytes,
        public: bool = False,
        resident_token: str | None = None,
    ) -> dict[str, object]:
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision <= 0
        ):
            raise InvalidInput("expected_revision must be a positive integer")
        if not isinstance(public, bool):
            raise InvalidInput("public must be a boolean")

        request_id = required_text(request_id, 36)
        actor = required_text(actor, 255)
        clean_name = safe_attachment_name(name)
        clean_media_type = required_text(media_type, 127)
        blob, size, digest = self._attachment_content(clean_media_type, content)

        with self._transaction() as conn:
            row = self._row(conn, request_id)
            resident = resident_token is not None
            if resident:
                self._resident(row, resident_token)
                # Resident-supplied uploads belong to the owner. They are
                # always visible to that owner and cannot be attributed to a
                # staff actor supplied by the caller.
                actor = "resident"
                public = True

            now = utcnow()
            new_revision = expected_revision + 1
            result = conn.execute(
                sa.update(requests)
                .where(
                    requests.c.id == row["id"],
                    requests.c.revision == expected_revision,
                )
                .values(revision=new_revision, updated_at=now)
            )
            if result.rowcount != 1:
                raise Conflict("request revision changed")

            attachment_id = new_id()
            conn.execute(
                sa.insert(attachments).values(
                    id=attachment_id,
                    request_id=row["id"],
                    name=clean_name,
                    media_type=clean_media_type,
                    content=blob,
                    sha256=digest,
                    size=size,
                    public=public,
                    created_at=now,
                )
            )
            conn.execute(
                sa.insert(events).values(
                    id=new_id(),
                    request_id=row["id"],
                    actor=actor,
                    kind="attachment_added",
                    public=public,
                    message="Attachment added",
                    payload={
                        "attachment_id": attachment_id,
                        "name": clean_name,
                        "media_type": clean_media_type,
                        "size": size,
                        "sha256": digest,
                    },
                    created_at=now,
                )
            )

            return {
                "id": attachment_id,
                "request_id": row["id"],
                "name": clean_name,
                "media_type": clean_media_type,
                "size": size,
                "sha256": digest,
                "public": public,
                "created_at": now,
                "revision": new_revision,
            }

    def get_attachment_staff(
        self, request_id: str, attachment_id: str
    ) -> dict[str, object]:
        """Return verified attachment data. Caller must authenticate staff."""
        with self._transaction() as conn:
            return self._fetch_attachment(conn, request_id, attachment_id)

    def get_attachment_resident(
        self, request_id: str, attachment_id: str, token: str
    ) -> dict[str, object]:
        with self._transaction() as conn:
            row = self._row(conn, required_text(request_id, 36))
            self._resident(row, token)
            attachment = self._fetch_attachment(conn, row["id"], attachment_id)
            if attachment["public"] is not True:
                raise NotFound("attachment not found")
            return {
                "content": attachment["content"],
                "name": attachment["name"],
                "media_type": attachment["media_type"],
                "sha256": attachment["sha256"],
                "size": attachment["size"],
            }


__all__ = ["AttachmentMixin", "safe_attachment_name"]
