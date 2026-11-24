from __future__ import annotations

import hashlib
import json
import secrets

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine

from townlight_311.models import notifications
from townlight_311.store import token_digest, utcnow
from townlight_311.workflow import ServiceRequestRepository

PNG_PREFIX = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "projection.sqlite3"


@pytest.fixture
def repo(db_path):
    engine = create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    repository = ServiceRequestRepository(engine)
    repository.save_category("waste", "Waste Services", "Public Works", True, "admin")
    yield repository
    repository.engine.dispose()


def test_request_projections_expose_only_safe_attachment_metadata(db_path, repo):
    token = secrets.token_urlsafe(32)
    created = repo.create_request(
        category="waste",
        description="Broken bin lid",
        location="1 Main Street",
        contact={"email": "resident@example.com"},
        resident_token=token,
        idempotency_key=secrets.token_urlsafe(24),
    )
    request_id = created["id"]

    public_blob = PNG_PREFIX + b"public projection payload"
    private_blob = b"private projection payload"
    public_added = repo.add_attachment(
        request_id,
        created["revision"],
        "staff",
        name="public.png",
        media_type="image/png",
        content=public_blob,
        public=True,
    )
    private_added = repo.add_attachment(
        request_id,
        public_added["revision"],
        "staff",
        name="private.txt",
        media_type="text/plain",
        content=private_blob,
        public=False,
    )

    staff = repo.get_staff(request_id)
    resident = repo.get_resident(request_id, token)

    expected_fields = {
        "id",
        "name",
        "media_type",
        "size",
        "sha256",
        "public",
        "created_at",
    }
    staff_by_id = {item["id"]: item for item in staff["attachments"]}
    assert set(staff_by_id) == {public_added["id"], private_added["id"]}
    assert all(
        set(item) == expected_fields for item in staff["attachments"]
    )
    assert staff_by_id[private_added["id"]]["public"] is False
    assert staff_by_id[public_added["id"]]["public"] is True

    resident_by_id = {item["id"]: item for item in resident["attachments"]}
    assert set(resident_by_id) == {public_added["id"]}
    assert set(resident_by_id[public_added["id"]]) == expected_fields
    assert resident_by_id[public_added["id"]]["public"] is True

    serialized_staff = json.dumps(staff, default=str)
    serialized_resident = json.dumps(resident, default=str)
    for blob in (public_blob, private_blob):
        assert blob.decode("latin-1") not in serialized_staff
        assert blob.decode("latin-1") not in serialized_resident
        assert "content" not in serializable_keys(staff)
        assert "content" not in serializable_keys(resident)

    resident_hash = token_digest(token)
    assert resident_hash not in serialized_staff
    assert resident_hash not in serialized_resident
    assert "resident_token_hash" not in serialized_staff
    assert "resident_token_hash" not in serialized_resident

    repo.engine.dispose()
    reopened = ServiceRequestRepository(
        create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    )
    try:
        reloaded_staff = reopened.get_staff(request_id)
        reloaded_resident = reopened.get_resident(request_id, token)
    finally:
        reopened.engine.dispose()

    assert reloaded_staff["attachments"] == staff["attachments"]
    assert reloaded_resident["attachments"] == resident["attachments"]
    assert reloaded_staff["attachments"][0]["sha256"] == hashlib.sha256(
        public_blob
    ).hexdigest()


def test_staff_notification_projection_is_allowlisted_and_reads_delivery_state(repo):
    token = secrets.token_urlsafe(32)
    contact_email = "notify-resident@example.com"
    created = repo.create_request(
        category="waste",
        description="Missed collection",
        location="2 Main Street",
        contact={"email": contact_email},
        resident_token=token,
        idempotency_key=secrets.token_urlsafe(24),
    )
    request_id = created["id"]

    staff = repo.get_staff(request_id)
    projected_notifications = staff["notifications"]
    assert projected_notifications

    allowed_notification_fields = {
        "id",
        "event_id",
        "state",
        "attempts",
        "next_attempt_at",
        "sent_at",
        "created_at",
    }
    forbidden_notification_fields = {
        "recipient",
        "payload",
        "recovery_code",
        "encrypted_code",
        "lookup_hash",
        "code_hash",
        "token",
        "claim_id",
        "lease_until",
        "last_error",
    }
    for notification in projected_notifications:
        assert set(notification) == allowed_notification_fields
        assert not set(notification) & forbidden_notification_fields

    resident = repo.get_resident(request_id, token)
    assert "notifications" not in resident
    serialized_resident = json.dumps(resident, default=str)
    for forbidden in (
        "notifications",
        "recipient",
        "payload",
        "last_error",
        "claim_id",
        "lease_until",
        "next_attempt_at",
        "sent_at",
    ):
        assert forbidden not in serialized_resident

    target_id = projected_notifications[0]["id"]
    last_error = "smtp temporary failure"
    claim_id = secrets.token_hex(16)
    next_attempt_at = utcnow()
    with repo.engine.begin() as conn:
        updated = conn.execute(
            sa.update(notifications)
            .where(notifications.c.id == target_id)
            .values(
                state="failed",
                attempts=3,
                next_attempt_at=next_attempt_at,
                last_error=last_error,
                claim_id=claim_id,
            )
        )
        assert updated.rowcount == 1

    reloaded = repo.get_staff(request_id)
    by_id = {item["id"]: item for item in reloaded["notifications"]}
    assert target_id in by_id
    target = by_id[target_id]
    assert set(target) == allowed_notification_fields
    assert target["state"] == "failed"
    assert target["attempts"] == 3
    assert target["next_attempt_at"] is not None
    assert not set(target) & forbidden_notification_fields

    serialized_notifications = json.dumps(reloaded["notifications"], default=str)
    assert contact_email not in serialized_notifications
    assert token not in serialized_notifications
    assert claim_id not in serialized_notifications
    assert last_error not in serialized_notifications


def serializable_keys(payload):
    keys = set(payload)
    for item in payload.get("attachments", []):
        keys.update(item)
    return keys
