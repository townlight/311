from __future__ import annotations

import base64
import json
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, func, select

from townlight_311.models import (
    attachments,
    command_receipts,
    events,
    notifications,
    requests,
)
from townlight_311.store import Conflict, InvalidInput, Unauthorized
from townlight_311.workflow import ServiceRequestRepository


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "townlight311.sqlite3"


@pytest.fixture
def repo(db_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{db_path}",
        future=True,
        connect_args={"timeout": 30},
    )
    repository = ServiceRequestRepository(engine)
    repository.save_category("waste", "Waste Services", "Public Works", True, "admin")
    yield repository
    repository.engine.dispose()


def _token():
    return secrets.token_urlsafe(32)


def _contact():
    return {
        "name": "Resident Example",
        "email": "resident@example.com",
        "phone": "+15550001111",
    }


def _create_payload(token, **overrides):
    payload = {
        "category": "waste",
        "description": "Broken bin lid",
        "location": "1 Main Street",
        "contact": _contact(),
        "resident_token": token,
    }
    payload.update(overrides)
    return payload


def _count(repo, table):
    with repo.engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def _create(repo, command_id, token, **overrides):
    return repo.execute_resident_command(
        command_id,
        "create_request",
        _create_payload(token, **overrides),
    )


def _move_to_closed(repo, request_id, token):
    repo.add_comment(
        request_id,
        repo.get_staff(request_id)["revision"],
        "internal-secret-actor",
        "private staff note",
        False,
    )
    for target, kwargs in (
        ("triaged", {}),
        ("assigned", {"assignee": "crew-7"}),
        ("in_progress", {}),
        ("resolved", {}),
        ("closed", {}),
    ):
        repo.transition(
            request_id,
            repo.get_staff(request_id)["revision"],
            "staff",
            target,
            f"staff moved to {target}",
            **kwargs,
        )


def test_create_replay_returns_same_result_and_writes_one_event_and_outbox(repo):
    command_id = str(uuid.uuid4())
    token = _token()
    payload = _create_payload(token)

    first = repo.execute_resident_command(command_id, "create_request", payload)
    second = repo.execute_resident_command(command_id, "create_request", payload)

    assert second["id"] == first["id"]
    assert second["revision"] == first["revision"] == 1
    assert _count(repo, requests) == 1
    assert _count(repo, events) == 1
    assert _count(repo, notifications) == 1
    assert _count(repo, command_receipts) == 1

    with repo.engine.connect() as conn:
        receipt = conn.execute(select(command_receipts)).mappings().one()
    assert len(receipt["fingerprint"]) == 64
    assert token not in json.dumps(receipt["result"])
    assert token not in receipt["fingerprint"]


def test_comment_replay_changes_revision_once(repo):
    token = _token()
    created = _create(repo, str(uuid.uuid4()), token)
    request_id = created["id"]
    command_id = str(uuid.uuid4())
    payload = {
        "request_id": request_id,
        "expected_revision": created["revision"],
        "message": "Resident follow-up",
        "resident_token": token,
        # Even if supplied, resident comments are forced public.
        "public": False,
    }

    first = repo.execute_resident_command(command_id, "comment", payload)
    second = repo.execute_resident_command(command_id, "comment", payload)

    assert first["revision"] == second["revision"] == 2
    staff = repo.get_staff(request_id)
    assert staff["revision"] == 2
    comments = [event for event in staff["events"] if event["kind"] == "comment"]
    assert len(comments) == 1
    assert comments[0]["public"] is True
    assert comments[0]["actor"] == "resident"


def test_reopen_replay_is_private_and_revision_changes_once(repo):
    token = _token()
    created = _create(repo, str(uuid.uuid4()), token)
    request_id = created["id"]
    _move_to_closed(repo, request_id, token)
    closed = repo.get_staff(request_id)
    assert closed["state"] == "closed"

    command_id = str(uuid.uuid4())
    payload = {
        "request_id": request_id,
        "expected_revision": closed["revision"],
        "reason": "The problem returned.",
        "resident_token": token,
    }
    first = repo.execute_resident_command(command_id, "reopen", payload)
    second = repo.execute_resident_command(command_id, "reopen", payload)

    assert first["state"] == second["state"] == "reopened"
    assert first["revision"] == second["revision"] == closed["revision"] + 1
    blob = json.dumps(first, default=str)
    for private in (
        "resident_token_hash",
        "input_sha256",
        "department",
        "assignee",
        "internal-secret-actor",
        "private staff note",
        token,
    ):
        assert private not in blob


def test_same_command_id_with_different_payload_conflicts(repo):
    command_id = str(uuid.uuid4())
    token = _token()
    _create(repo, command_id, token)

    with pytest.raises(Conflict):
        _create(repo, command_id, token, description="Different description")

    assert _count(repo, requests) == 1
    assert _count(repo, command_receipts) == 1


def test_unknown_operation_is_rejected_without_receipt(repo):
    with pytest.raises(InvalidInput):
        repo.execute_resident_command(
            str(uuid.uuid4()),
            "transition",
            {"request_id": "anything", "target": "closed"},
        )
    assert _count(repo, command_receipts) == 0


def test_failed_operation_rolls_back_receipt_and_changes(repo):
    command_id = str(uuid.uuid4())
    before_events = _count(repo, events)
    before_requests = _count(repo, requests)

    with pytest.raises(InvalidInput):
        _create(repo, command_id, _token(), category="missing-category")

    assert _count(repo, command_receipts) == 0
    assert _count(repo, requests) == before_requests
    assert _count(repo, events) == before_events

    # The failed command id is not poisoned; the same id can commit later.
    created = _create(repo, command_id, _token())
    assert created["id"]
    assert _count(repo, command_receipts) == 1


def test_upload_returns_metadata_only_and_checks_token(repo):
    token = _token()
    created = _create(repo, str(uuid.uuid4()), token)
    command_id = str(uuid.uuid4())
    content = b"resident attachment"
    payload = {
        "request_id": created["id"],
        "expected_revision": created["revision"],
        "name": "note.txt",
        "media_type": "text/plain",
        "content_base64": base64.b64encode(content).decode("ascii"),
        "resident_token": token,
    }

    result = repo.execute_resident_command(command_id, "upload_attachment", payload)
    assert result["size"] == len(content)
    assert "content" not in result
    assert "content_base64" not in result
    assert _count(repo, attachments) == 1

    with pytest.raises(Unauthorized):
        repo.execute_resident_command(
            str(uuid.uuid4()),
            "upload_attachment",
            {**payload, "resident_token": _token()},
        )
    assert _count(repo, attachments) == 1


def test_concurrent_same_command_commits_at_most_one_receipt_and_request(db_path):
    first_repo = ServiceRequestRepository(
        create_engine(
            f"sqlite+pysqlite:///{db_path}",
            future=True,
            connect_args={"timeout": 30},
        )
    )
    second_repo = ServiceRequestRepository(
        create_engine(
            f"sqlite+pysqlite:///{db_path}",
            future=True,
            connect_args={"timeout": 30},
        )
    )
    try:
        first_repo.save_category(
            "waste", "Waste Services", "Public Works", True, "admin"
        )
        command_id = str(uuid.uuid4())
        token = _token()
        payload = _create_payload(token)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    repo.execute_resident_command,
                    command_id,
                    "create_request",
                    payload,
                )
                for repo in (first_repo, second_repo)
            ]
            results = [future.result() for future in futures]

        assert results[0]["id"] == results[1]["id"]
        for repository in (first_repo, second_repo):
            assert _count(repository, command_receipts) == 1
            assert _count(repository, requests) == 1
            assert _count(repository, events) == 1
    finally:
        first_repo.engine.dispose()
        second_repo.engine.dispose()
