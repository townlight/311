from __future__ import annotations

import json
import secrets

import pytest
from sqlalchemy import create_engine, func, select

from townlight_311.models import events, requests
from townlight_311.store import Unauthorized
from townlight_311.workflow import ServiceRequestRepository


@pytest.fixture
def repo(tmp_path):
    db_path = tmp_path / "resident-reopen-privacy.sqlite3"
    engine = create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    repository = ServiceRequestRepository(engine)
    yield repository
    repository.engine.dispose()


def _count(repo, table):
    with repo.engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def test_resident_reopen_response_is_private_and_wrong_token_is_inert(repo):
    repo.save_category("waste", "Waste Services", "Public Works", True, "admin")

    token = secrets.token_urlsafe(32)
    wrong_token = secrets.token_urlsafe(32)
    contact_email = "private.reopen.resident@example.com"
    contact_phone = "+15550001999"
    staff_actor = "secret-staff-actor-4477"
    private_comment = "secret internal reopen comment"

    created = repo.create_request(
        category="waste",
        description="Overflowing public bin",
        location="12 Reopen Road",
        contact={
            "name": "Private Reopen Resident",
            "email": contact_email,
            "phone": contact_phone,
        },
        resident_token=token,
        idempotency_key=secrets.token_urlsafe(24),
    )
    request_id = created["id"]

    repo.add_comment(
        request_id,
        created["revision"],
        staff_actor,
        private_comment,
        False,
    )
    repo.transition(
        request_id,
        created["revision"] + 1,
        staff_actor,
        "rejected",
        "rejected after internal review",
    )

    staff_before = repo.get_staff(request_id)
    assert staff_before["state"] == "rejected"

    reopened = repo.reopen_request(
        request_id,
        staff_before["revision"],
        token,
        "resident asks for another review",
    )

    assert reopened["state"] == "reopened"
    assert reopened["revision"] == staff_before["revision"] + 1
    assert reopened["events"], "public history must be present"
    assert all(event["kind"] for event in reopened["events"])
    assert any(
        event["kind"] == "state_changed"
        and event["message"] == "resident asks for another review"
        for event in reopened["events"]
    )
    assert any(event["message"] == "Request submitted." for event in reopened["events"])

    blob = json.dumps(reopened, default=str)
    for secret in (
        contact_email,
        contact_phone,
        "Private Reopen Resident",
        private_comment,
        staff_actor,
        "resident_token_hash",
        "input_sha256",
        token,
    ):
        assert secret not in blob

    before_events = _count(repo, events)
    before_requests = _count(repo, requests)
    state_before = repo.get_staff(request_id)

    with pytest.raises(Unauthorized):
        repo.reopen_request(
            request_id,
            state_before["revision"],
            wrong_token,
            "wrong token must not reopen",
        )

    state_after = repo.get_staff(request_id)
    assert state_after["state"] == state_before["state"] == "reopened"
    assert state_after["revision"] == state_before["revision"]
    assert len(state_after["events"]) == len(state_before["events"])
    assert _count(repo, events) == before_events
    assert _count(repo, requests) == before_requests
    assert all(
        event["message"] != "wrong token must not reopen"
        for event in state_after["events"]
    )
