from __future__ import annotations

import json
import secrets
from datetime import datetime

import pytest
from sqlalchemy import create_engine, func, select

from townlight_311.models import events, notifications, requests
from townlight_311.store import Conflict, InvalidInput, Unauthorized
from townlight_311.workflow import ServiceRequestRepository


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "townlight311.sqlite3"


@pytest.fixture
def repo(db_path):
    engine = create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    repository = ServiceRequestRepository(engine)
    yield repository
    repository.engine.dispose()


@pytest.fixture
def category(repo):
    repo.save_category("waste", "Waste Services", "Public Works", True, "admin")
    return "waste"


def _token():
    return secrets.token_urlsafe(32)


def _contact(email="resident@example.com"):
    return {
        "name": "Resident Example",
        "email": email,
        "phone": "+15550001111",
    }


def _create(repo, category, *, token=None, key=None, **overrides):
    token = token or _token()
    key = key or secrets.token_urlsafe(24)
    payload = {
        "category": category,
        "description": "Broken bin lid",
        "location": "1 Main Street",
        "contact": _contact(),
        "resident_token": token,
        "idempotency_key": key,
    }
    payload.update(overrides)
    return repo.create_request(**payload), token, key


def _count(repo, table):
    with repo.engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def test_create_persists_across_dispose_and_secure_token_read(db_path, repo, category):
    created, token, _ = _create(repo, category)
    request_id = created["id"]
    expected_description = created["description"]

    repo.engine.dispose()
    restarted = ServiceRequestRepository(
        create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    )
    try:
        resident = restarted.get_resident(request_id, token)
        assert resident["description"] == expected_description
        assert resident["state"] == "submitted"
        with pytest.raises(Unauthorized):
            restarted.get_resident(request_id, _token())
    finally:
        restarted.engine.dispose()


def test_idempotency_replays_same_id_and_rejects_changed_input(repo, category):
    token = _token()
    key = secrets.token_urlsafe(24)

    first, _, _ = _create(repo, category, token=token, key=key, description="Original")
    second, _, _ = _create(repo, category, token=token, key=key, description="Original")
    assert second["id"] == first["id"]

    with pytest.raises(Conflict):
        _create(repo, category, token=token, key=key, description="Changed description")

    with pytest.raises(Conflict):
        _create(repo, category, token=_token(), key=key, description="Original")

    with pytest.raises(InvalidInput):
        repo.create_request(
            category=category,
            description="Mandatory key",
            location="1 Main Street",
            contact=_contact(),
            resident_token=_token(),
            idempotency_key=None,
        )


def test_private_fields_are_absent_from_resident_projection(repo, category):
    token = _token()
    contact = _contact(email="secret.resident@example.com")
    contact["name"] = "Secret Resident Name"
    contact["phone"] = "+19998887777"

    created, _, _ = _create(repo, category, token=token, contact=contact)
    request_id = created["id"]
    revision = created["revision"]

    repo.transition(
        request_id,
        revision,
        "internal-secret-actor",
        "triaged",
        "internal secret triage note",
    )
    repo.add_comment(
        request_id,
        revision + 1,
        "internal-secret-actor",
        "private note body",
        False,
    )

    resident = repo.get_resident(request_id, token)
    blob = json.dumps(resident, default=str)

    for secret in (
        token,
        contact["name"],
        contact["email"],
        contact["phone"],
        "internal-secret-actor",
        "private note body",
        "resident_token_hash",
        "input_sha256",
    ):
        assert secret not in blob

    assert "Request submitted." in blob


def test_public_comment_visible_private_comment_hidden_and_revision_increments(
    repo, category
):
    created, token, _ = _create(repo, category)
    request_id = created["id"]
    revision = created["revision"]

    repo.add_comment(request_id, revision, "staff", "Public progress note", True)
    repo.add_comment(request_id, revision + 1, "staff", "Private staff note", False)

    staff = repo.get_staff(request_id)
    resident = repo.get_resident(request_id, token)

    assert staff["revision"] == revision + 2
    assert resident["revision"] == revision + 2

    staff_messages = [event["message"] for event in staff["events"]]
    resident_messages = [event["message"] for event in resident["events"]]

    assert "Public progress note" in resident_messages
    assert "Private staff note" not in resident_messages
    assert "Private staff note" in staff_messages


def test_inactive_category_rejects_intake(repo):
    repo.save_category("inactive", "Inactive Category", "Operations", False, "admin")

    with pytest.raises(InvalidInput):
        _create(repo, "inactive")


def test_invalid_contact_keys_and_line_breaks_rejected(repo, category):
    with pytest.raises(InvalidInput):
        _create(repo, category, contact={"ssn": "123-45-6789"})

    with pytest.raises(InvalidInput):
        _create(
            repo,
            category,
            contact={"email": "resident@example.com\r\nBcc: other@example.com"},
        )


def test_invalid_transition_and_stale_revision_rollback_without_append(
    repo, category
):
    created, _, _ = _create(repo, category)
    request_id = created["id"]
    revision = created["revision"]

    before_events = _count(repo, events)
    before_notifications = _count(repo, notifications)

    with pytest.raises(InvalidInput):
        repo.transition(request_id, revision, "staff", "resolved", "skip ahead")

    with pytest.raises(Conflict):
        repo.transition(request_id, revision + 5, "staff", "triaged", "stale revision")

    assert _count(repo, events) == before_events
    assert _count(repo, notifications) == before_notifications

    staff = repo.get_staff(request_id)
    assert staff["state"] == "submitted"
    assert staff["revision"] == revision


def test_assigned_requires_assignee(repo, category):
    created, _, _ = _create(repo, category)
    request_id = created["id"]
    revision = created["revision"]

    repo.transition(request_id, revision, "staff", "triaged", "triage accepted")
    before_events = _count(repo, events)
    before_notifications = _count(repo, notifications)

    with pytest.raises(InvalidInput):
        repo.transition(request_id, revision + 1, "staff", "assigned", "assign")

    staff = repo.get_staff(request_id)
    assert staff["state"] == "triaged"
    assert staff["revision"] == revision + 1
    assert _count(repo, events) == before_events
    assert _count(repo, notifications) == before_notifications


def test_naive_due_at_rejected_without_state_change(repo, category):
    created, _, _ = _create(repo, category)
    request_id = created["id"]
    revision = created["revision"]

    repo.transition(request_id, revision, "staff", "triaged", "triage accepted")

    with pytest.raises(InvalidInput):
        repo.transition(
            request_id,
            revision + 1,
            "staff",
            "assigned",
            "assign",
            assignee="crew-7",
            due_at=datetime(2025, 1, 1, 12, 0),
        )

    staff = repo.get_staff(request_id)
    assert staff["state"] == "triaged"
    assert staff["revision"] == revision + 1


def test_duplicate_self_and_cycle_are_rejected_preserving_history(repo, category):
    first, _, _ = _create(repo, category)
    second, _, _ = _create(repo, category)
    first_id = first["id"]
    second_id = second["id"]
    first_revision = first["revision"]
    second_revision = second["revision"]

    with pytest.raises((InvalidInput, Conflict)):
        repo.transition(
            first_id,
            first_revision,
            "staff",
            "duplicate",
            "cannot duplicate itself",
            duplicate_of=first_id,
        )

    repo.transition(
        first_id,
        first_revision,
        "staff",
        "duplicate",
        "duplicate detected",
        duplicate_of=second_id,
    )

    with pytest.raises((InvalidInput, Conflict)):
        repo.transition(
            second_id,
            second_revision,
            "staff",
            "duplicate",
            "cycle must fail",
            duplicate_of=first_id,
        )

    first_staff = repo.get_staff(first_id)
    second_staff = repo.get_staff(second_id)

    assert first_staff["state"] == "duplicate"
    assert first_staff["duplicate_of"] == second_id
    assert len(first_staff["events"]) == 2
    assert second_staff["state"] == "submitted"
    assert second_staff["revision"] == second_revision
    assert second_staff["duplicate_of"] is None
    assert len(second_staff["events"]) == 1


def test_wrong_token_rejected_for_reader_and_resident_comment(repo, category):
    created, _resident_token, _ = _create(repo, category)
    request_id = created["id"]
    revision = created["revision"]
    wrong_token = _token()

    with pytest.raises(Unauthorized):
        repo.get_resident(request_id, wrong_token)

    with pytest.raises(Unauthorized):
        repo.add_comment(
            request_id,
            revision,
            "resident",
            "not allowed",
            True,
            resident_token=wrong_token,
        )

    staff = repo.get_staff(request_id)
    assert staff["revision"] == revision
    assert all(event["message"] != "not allowed" for event in staff["events"])


def test_full_lifecycle_history_and_outbox_survive_restart(db_path, repo, category):
    token = _token()
    created, _, _ = _create(repo, category, token=token)
    request_id = created["id"]
    reasons = []

    def transition(target, reason, **kwargs):
        current = repo.get_staff(request_id)
        result = repo.transition(
            request_id,
            current["revision"],
            "staff",
            target,
            reason,
            **kwargs,
        )
        reasons.append(reason)
        return result

    transition("triaged", "triage accepted")
    transition("assigned", "assigned to field crew", assignee="crew-7")
    transition("in_progress", "work started")
    transition("resolved", "repair complete")
    transition("closed", "closed after confirmation")

    repo.reopen_request(
        request_id,
        repo.get_staff(request_id)["revision"],
        token,
        "resident says issue returned",
    )
    reasons.append("resident says issue returned")

    transition("assigned", "reopened and reassigned", assignee="crew-9")

    before = repo.get_staff(request_id)
    assert before["state"] == "assigned"
    assert before["assignee"] == "crew-9"
    assert len(before["events"]) >= len(reasons) + 1

    history_blob = json.dumps(before["events"], default=str)
    for reason in reasons:
        assert reason in history_blob

    notification_count = _count(repo, notifications)
    request_count = _count(repo, requests)
    assert notification_count > 0

    repo.engine.dispose()
    restarted = ServiceRequestRepository(
        create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    )
    try:
        after = restarted.get_staff(request_id)
        assert after["state"] == "assigned"
        assert after["assignee"] == "crew-9"
        assert after["revision"] == before["revision"]
        assert len(after["events"]) == len(before["events"])

        with restarted.engine.connect() as conn:
            rows = conn.execute(select(notifications)).mappings().all()

        assert len(rows) == notification_count
        assert all(row["state"] == "pending" and row["attempts"] == 0 for row in rows)
        assert _count(restarted, requests) == request_count
    finally:
        restarted.engine.dispose()
