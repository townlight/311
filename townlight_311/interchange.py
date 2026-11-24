"""Operational CSV/JSON interchange for service requests."""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
from typing import Any

from sqlalchemy import select

from .intake import _validated_contact
from .models import requests
from .store import InvalidInput, required_text

INPUT_FIELDS = (
    "external_id",
    "category",
    "description",
    "location",
    "contact_name",
    "email",
    "phone",
)
_MAX_BYTES = 2 * 1024 * 1024
_MAX_ROWS = 1000
_MAX_EXPORT = 10_000
_EXPORT_PAGE = 200


def _format(value: Any) -> str:
    if not isinstance(value, str) or value.strip().lower() not in ("csv", "json"):
        raise InvalidInput("format must be csv or json")
    return value.strip().lower()


def _decode(content: Any) -> str:
    if not isinstance(content, (bytes, bytearray)):
        raise InvalidInput("content must be bytes")
    data = bytes(content)
    if not data or len(data) > _MAX_BYTES:
        raise InvalidInput("content is empty or exceeds the 2 MiB limit")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidInput("content must be valid UTF-8") from None


def _idempotency_key(source_key: str, external_id: str) -> str:
    # The column is 128 characters: 7 + 60 + 1 + 60, so both digests are
    # truncated to 60 hex characters while retaining 240 bits overall.
    source_hash = hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:60]
    external_hash = hashlib.sha256(external_id.encode("utf-8")).hexdigest()[:60]
    return f"import:{source_hash}:{external_hash}"


class Interchange:
    """CSV/JSON preview, commit, and export over a repository instance."""

    def __init__(self, repository: Any, *, import_key: bytes):
        if not isinstance(import_key, (bytes, bytearray)) or len(import_key) < 32:
            raise InvalidInput("import_key must be at least 32 bytes")
        self.repository = repository
        self._import_key = bytes(import_key)

    def _resident_token(self, external_id: str) -> str:
        return hmac.new(
            self._import_key, external_id.encode("utf-8"), hashlib.sha256).hexdigest()

    def _text(self, value: Any, field: str, maxlen: int, required: bool) -> str:
        value = "" if value is None else value
        if not isinstance(value, str):
            raise InvalidInput(f"{field} must be a string")
        text = value.strip()
        if required and not text:
            raise InvalidInput(f"{field} is required")
        if len(text) > maxlen:
            raise InvalidInput(f"{field} is too long")
        if field in ("external_id", "category") and ("\r" in text or "\n" in text):
            raise InvalidInput(f"{field} must not contain line breaks")
        return text

    def _raw_rows(self, text: str, fmt: str) -> list[Any]:
        if fmt == "csv":
            reader = csv.DictReader(io.StringIO(text, newline=""))
            header = list(reader.fieldnames or [])
            if len(header) != len(INPUT_FIELDS) or set(header) != set(INPUT_FIELDS):
                raise InvalidInput("CSV header must be the exact interchange columns")
            rows = list(reader)
            if any(None in raw for raw in rows):
                raise InvalidInput("CSV row has extra fields")
            return rows
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            raise InvalidInput("content is not valid JSON") from None
        if isinstance(payload, dict):
            if set(payload) - {"schema_version", "requests"}:
                raise InvalidInput("JSON contains unknown top-level keys")
            if payload.get("schema_version", 1) != 1:
                raise InvalidInput("unsupported schema_version")
            rows = payload.get("requests")
        else:
            rows = payload
        if not isinstance(rows, list):
            raise InvalidInput("JSON requests must be an array")
        for raw in rows:
            if not isinstance(raw, dict) or set(raw) != set(INPUT_FIELDS):
                raise InvalidInput("JSON row must be an object with exact fields")
        return rows

    def _parse(self, content: Any, fmt: str) -> list[dict[str, Any]]:
        rows = self._raw_rows(_decode(content), fmt)
        if not rows:
            raise InvalidInput("batch contains no rows")
        if len(rows) > _MAX_ROWS:
            raise InvalidInput("batch exceeds the 1000 row limit")
        active = {row["code"] for row in self.repository.categories()}
        parsed, seen = [], set()
        for raw in rows:
            external_id = self._text(raw.get("external_id"), "external_id", 255, True)
            if external_id in seen:
                raise InvalidInput("duplicate external_id within batch")
            seen.add(external_id)
            category = self._text(raw.get("category"), "category", 64, True)
            if category not in active:
                raise InvalidInput("unknown or inactive category")
            description = self._text(raw.get("description"), "description", 8000, True)
            location = self._text(raw.get("location"), "location", 1000, True)
            contact = {}
            for column, key, limit in (
                ("contact_name", "name", 255),
                ("email", "email", 320),
                ("phone", "phone", 64),
            ):
                value = self._text(raw.get(column), column, limit, False)
                if value:
                    contact[key] = value
            parsed.append(
                {
                    "external_id": external_id,
                    "category": category,
                    "description": description,
                    "location": location,
                    "contact": _validated_contact(contact),
                }
            )
        return parsed

    def preview(self, content: bytes, format: str) -> dict[str, Any]:
        parsed = self._parse(content, _format(format))
        rows = [
            {
                **{name: row[name] for name in INPUT_FIELDS[:4]},
                "contact_name": row["contact"].get("name", ""),
                "email": row["contact"].get("email", ""),
                "phone": row["contact"].get("phone", ""),
            }
            for row in parsed
        ]
        return {"count": len(parsed), "rows": rows}

    def commit(
        self, content: bytes, format: str, source_key: str, actor: str
    ) -> dict[str, Any]:
        fmt = _format(format)
        actor = required_text(actor, 255)
        source_key = required_text(source_key, 2048)
        parsed = self._parse(content, fmt)
        results = []
        with self.repository._bind_transaction() as conn:
            for row in parsed:
                key = _idempotency_key(source_key, row["external_id"])
                existing = conn.execute(
                    select(requests.c.id).where(requests.c.idempotency_key == key)
                ).first()
                created = self.repository.create_request(
                    category=row["category"],
                    description=row["description"],
                    location=row["location"],
                    contact=row["contact"],
                    resident_token=self._resident_token(row["external_id"]),
                    idempotency_key=key,
                    actor=actor,
                )
                results.append(
                    {
                        "external_id": row["external_id"],
                        "tracking": created["tracking"],
                        "request_id": created["id"],
                        "status": "existing" if existing is not None else "created",
                    }
                )
        return {"count": len(results), "results": results}

    @staticmethod
    def _export_record(row: dict[str, Any], include_contact: bool) -> dict[str, str]:
        contact = (row.get("contact") if include_contact else {}) or {}
        return {
            "external_id": str(row.get("id", "")),
            "category": str(row.get("category", "")),
            "description": str(row.get("description", "")),
            "location": str(row.get("location", "")),
            "contact_name": str(contact.get("name", "")),
            "email": str(contact.get("email", "")),
            "phone": str(contact.get("phone", "")),
        }

    def export(
        self,
        format: str,
        department: str | None = None,
        state: str | None = None,
        *,
        include_contact: bool = False,
    ) -> bytes:
        fmt = _format(format)
        if not isinstance(include_contact, bool):
            raise InvalidInput("include_contact must be a boolean")
        if department is not None:
            department = required_text(department, 255)
        rows, offset = [], 0
        while len(rows) < _MAX_EXPORT:
            page = self.repository.list_requests(
                department=department, state=state, limit=_EXPORT_PAGE, offset=offset
            )
            if not page:
                break
            rows.extend(page)
            offset += len(page)
            if len(page) < _EXPORT_PAGE:
                break
        if len(rows) > _MAX_EXPORT or (
            len(rows) == _MAX_EXPORT
            and self.repository.list_requests(
                department=department, state=state, limit=1, offset=_MAX_EXPORT
            )
        ):
            raise InvalidInput(
                f"export exceeds the {_MAX_EXPORT} row maximum; filter the export"
            )
        records = [self._export_record(row, include_contact) for row in rows]
        if fmt == "json":
            payload = {"schema_version": 1, "requests": records}
            return json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        output = io.StringIO(newline="")
        writer = csv.DictWriter(
            output, fieldnames=list(INPUT_FIELDS), lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(records)
        return output.getvalue().encode("utf-8")


__all__ = ["INPUT_FIELDS", "Interchange"]
