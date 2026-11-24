"""Resident intake and service-category operations for Townlight 311."""

from __future__ import annotations

import hashlib
import json
import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .models import requests, service_categories
from .store import (
    Conflict,
    InvalidInput,
    json_safe,
    new_id,
    required_text,
    token_digest,
    utcnow,
)

_CONTACT_LIMITS = {"name": 255, "email": 320, "phone": 64}
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validated_contact(contact):
    if not isinstance(contact, dict):
        raise InvalidInput("contact must be an object")
    cleaned = {}
    for field, value in contact.items():
        if field not in _CONTACT_LIMITS:
            raise InvalidInput(f"unexpected contact field: {field}")
        if not isinstance(value, str):
            raise InvalidInput(f"contact {field} must be a string")
        if "\r" in value or "\n" in value:
            raise InvalidInput(f"contact {field} must not contain line breaks")
        if len(value) > _CONTACT_LIMITS[field]:
            raise InvalidInput(
                f"contact {field} must be at most {_CONTACT_LIMITS[field]} characters"
            )
        if field == "email" and value and not _EMAIL_SHAPE.fullmatch(value):
            raise InvalidInput("contact email is not a valid address")
        cleaned[field] = value
    return cleaned


def _fingerprint(payload):
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IntakeMixin:
    """Category management and resident request intake."""

    def save_category(self, code, name, department, active, actor):
        code = required_text(code, 64)
        name = required_text(name, 255)
        department = required_text(department, 255)
        required_text(actor, 255)
        if not isinstance(active, bool):
            raise InvalidInput("active must be a boolean")

        values = {
            "code": code,
            "name": name,
            "department": department,
            "active": active,
        }
        try:
            with self._transaction() as conn:
                existing = conn.execute(
                    select(service_categories.c.code).where(
                        service_categories.c.code == code
                    )
                ).first()
                if existing is None:
                    conn.execute(service_categories.insert().values(**values))
                else:
                    conn.execute(
                        service_categories.update()
                        .where(service_categories.c.code == code)
                        .values(name=name, department=department, active=active)
                    )
        except IntegrityError as exc:
            raise Conflict("category could not be saved") from exc
        return dict(values)

    def categories(self, *, include_inactive=False):
        if not isinstance(include_inactive, bool):
            raise InvalidInput("include_inactive must be a boolean")
        query = select(service_categories)
        if not include_inactive:
            query = query.where(service_categories.c.active.is_(True))
        query = query.order_by(
            service_categories.c.name, service_categories.c.code
        )
        with self._transaction() as conn:
            rows = conn.execute(query).mappings().all()
        return [dict(row) for row in rows]

    def create_request(
        self,
        *,
        category,
        description,
        location,
        contact,
        resident_token,
        idempotency_key,
        actor="resident",
    ):
        category = required_text(category, 64)
        description = required_text(description, 8000)
        location = required_text(location, 1000)
        contact = _validated_contact(contact)
        required_text(actor, 255)
        key = required_text(idempotency_key, 128)

        token_hash = token_digest(resident_token)
        fingerprint = _fingerprint(
            {
                "category": category,
                "description": description,
                "location": location,
                "contact": contact,
                "resident_token_hash": token_hash,
            }
        )

        try:
            with self._transaction() as conn:
                if key is not None:
                    existing = conn.execute(
                        select(requests).where(
                            requests.c.idempotency_key == key
                        )
                    ).mappings().first()
                    if existing is not None:
                        if existing["input_sha256"] == fingerprint:
                            return self._project(conn, existing, resident=True)
                        raise Conflict(
                            "idempotency key reused with different input"
                        )

                category_row = conn.execute(
                    select(service_categories).where(
                        service_categories.c.code == category,
                        service_categories.c.active.is_(True),
                    )
                ).mappings().first()
                if category_row is None:
                    raise InvalidInput("unknown or inactive category")

                request_id = new_id()
                now = utcnow()
                tracking = "TL311-" + request_id.replace("-", "")
                conn.execute(
                    requests.insert().values(
                        id=request_id,
                        tracking=tracking,
                        resident_token_hash=token_hash,
                        category=category,
                        description=description,
                        location=location,
                        contact=contact,
                        state="submitted",
                        department=category_row["department"],
                        assignee=None,
                        priority="normal",
                        due_at=None,
                        revision=1,
                        duplicate_of=None,
                        created_at=now,
                        updated_at=now,
                        idempotency_key=key,
                        input_sha256=fingerprint,
                    )
                )
                row = self._row(conn, request_id)
                self._event(
                    conn,
                    row,
                    actor,
                    "submitted",
                    "Request submitted.",
                    True,
                    payload=json_safe({"tracking": tracking, "category": category}),
                )
                return self._project(conn, row, resident=True)
        except IntegrityError:
            if key is None:
                raise
            with self._transaction() as conn:
                existing = conn.execute(
                    select(requests).where(
                        requests.c.idempotency_key == key
                    )
                ).mappings().first()
                if existing is None:
                    raise
                if existing["input_sha256"] == fingerprint:
                    return self._project(conn, existing, resident=True)
            raise Conflict("idempotency key reused with different input")


__all__ = ["IntakeMixin"]
