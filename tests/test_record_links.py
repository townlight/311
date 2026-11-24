from __future__ import annotations

import io
import json
import secrets
import uuid
import zipfile

import pytest
from sqlalchemy import create_engine

from townlight_311.store import Conflict, InvalidInput
from townlight_311.workflow import ServiceRequestRepository


@pytest.fixture
def repo(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'townlight311-links.sqlite3'}",
        future=True,
    )
    repository = ServiceRequestRepository(engine)
    yield repository
    repository.engine.dispose()


@pytest.fixture
def category(repo):
    repo.save_category("waste", "Waste Services", "Public Works", True, "admin")
    return "waste"


def _token():
    return secrets.token_urlsafe(32)


def _create_request(repo, category):
    token = _token()
    created = repo.create_request(
        category=category,
        description="Broken bin lid",
        location="1 Main Street",
        contact={
            "name": "Resident Example",
            "email": "resident@example.com",
            "phone": "+15550001111",
        },
        resident_token=token,
        idempotency_key=secrets.token_urlsafe(24),
    )
    return created, token


def _record_link_events(staff):
    return [
        event
        for event in staff["events"]
        if event.get("kind") == "record-linked"
    ]


def test_link_record_increments_revision_and_makes_private_staff_event(
    repo, category
):
    created, token = _create_request(repo, category)
    request_id = created["id"]
    revision = created["revision"]
    target_uuid = str(uuid.uuid4())
    label = "Parcel 42 - Oak Street"

    linked = repo.link_record(
        request_id,
        revision,
        "staff-linker",
        "parcel",
        target_uuid,
        label,
    )

    assert linked["revision"] == revision + 1

    staff = repo.get_staff(request_id)
    assert staff["revision"] == revision + 1
    events = _record_link_events(staff)
    assert len(events) == 1
    event = events[0]
    assert event["public"] is False
    assert event["payload"] == {
        "target_type": "parcel",
        "target_id": target_uuid,
        "label": label,
    }
    assert label in event["message"]

    resident = repo.get_resident(request_id, token)
    resident_blob = json.dumps(resident, default=str)
    assert resident["revision"] == revision + 1
    assert _record_link_events(resident) == []
    assert target_uuid not in resident_blob
    assert label not in resident_blob


def test_archive_json_includes_private_record_link_and_label(repo, category):
    created, _ = _create_request(repo, category)
    request_id = created["id"]
    revision = created["revision"]
    target_uuid = str(uuid.uuid4())
    label = "Permit BP-2026-0042"

    repo.link_record(
        request_id,
        revision,
        "staff-linker",
        "permit",
        target_uuid,
        label,
    )

    archive_bytes = repo.archive(request_id)
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive_file:
        request_record = json.loads(archive_file.read("request.json"))

    archived_events = [
        event
        for event in request_record["events"]
        if event.get("kind") == "record-linked"
    ]
    assert len(archived_events) == 1
    archived_event = archived_events[0]
    assert archived_event["public"] is False
    assert archived_event["payload"]["target_type"] == "permit"
    assert archived_event["payload"]["target_id"] == target_uuid
    assert archived_event["payload"]["label"] == label
    assert label in json.dumps(request_record, default=str)


def test_duplicate_same_target_at_current_revision_is_a_noop(repo, category):
    created, _ = _create_request(repo, category)
    request_id = created["id"]
    revision = created["revision"]
    target_uuid = str(uuid.uuid4())
    label = "Inspection INS-100"

    first = repo.link_record(
        request_id,
        revision,
        "staff-linker",
        "inspection",
        target_uuid,
        label,
    )
    duplicate = repo.link_record(
        request_id,
        first["revision"],
        "staff-linker",
        "inspection",
        target_uuid,
        label,
    )

    assert duplicate["revision"] == first["revision"]
    staff = repo.get_staff(request_id)
    assert staff["revision"] == revision + 1
    assert len(_record_link_events(staff)) == 1


def test_link_record_rejects_stale_revision_and_invalid_target_type(repo, category):
    created, _ = _create_request(repo, category)
    request_id = created["id"]
    revision = created["revision"]
    target_uuid = str(uuid.uuid4())
    before = repo.get_staff(request_id)

    with pytest.raises(Conflict):
        repo.link_record(
            request_id,
            revision + 10,
            "staff-linker",
            "parcel",
            target_uuid,
            "Stale parcel",
        )

    with pytest.raises(InvalidInput):
        repo.link_record(
            request_id,
            revision,
            "staff-linker",
            "not-a-record-type",
            target_uuid,
            "Invalid target",
        )

    after = repo.get_staff(request_id)
    assert after["revision"] == before["revision"] == revision
    assert len(after["events"]) == len(before["events"])
    assert _record_link_events(after) == []
