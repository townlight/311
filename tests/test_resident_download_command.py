from __future__ import annotations

import base64
import hashlib
import json
import secrets
import uuid

import pytest
from sqlalchemy import create_engine, func, select

from townlight_311.models import attachments, command_receipts
from townlight_311.store import NotFound, Unauthorized
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


def _upload(repo, request_id, revision, token, content):
    return repo.execute_resident_command(
        str(uuid.uuid4()),
        "upload_attachment",
        {
            "request_id": request_id,
            "expected_revision": revision,
            "name": "resident-note.txt",
            "media_type": "text/plain",
            "content_base64": base64.b64encode(content).decode("ascii"),
            "resident_token": token,
        },
    )


def test_download_attachment_returns_actual_bytes_hash_and_replays(repo):
    token = _token()
    created = _create(repo, str(uuid.uuid4()), token)
    content = b"Downloaded resident attachment bytes.\n"
    uploaded = _upload(repo, created["id"], created["revision"], token, content)

    command_id = str(uuid.uuid4())
    payload = {
        "request_id": created["id"],
        "attachment_id": uploaded["id"],
        "resident_token": token,
    }

    first = repo.execute_resident_command(command_id, "download_attachment", payload)
    second = repo.execute_resident_command(command_id, "download_attachment", payload)

    assert second == first
    assert base64.b64decode(first["content_base64"]) == content
    assert first["sha256"] == hashlib.sha256(content).hexdigest()
    assert first["size"] == len(content)
    assert first["name"] == "resident-note.txt"
    assert first["media_type"] == "text/plain"
    assert "content" not in first
    assert _count(repo, attachments) == 1

    with repo.engine.connect() as conn:
        receipt = conn.execute(
            select(command_receipts).where(
                command_receipts.c.command_id == command_id
            )
        ).mappings().one()
    assert receipt["result"] == first
    assert "content" not in receipt["result"]


def test_download_attachment_wrong_owner_token_is_denied(repo):
    token = _token()
    created = _create(repo, str(uuid.uuid4()), token)
    uploaded = _upload(
        repo,
        created["id"],
        created["revision"],
        token,
        b"Owner-only attachment bytes.",
    )
    receipts_before = _count(repo, command_receipts)

    with pytest.raises(Unauthorized):
        repo.execute_resident_command(
            str(uuid.uuid4()),
            "download_attachment",
            {
                "request_id": created["id"],
                "attachment_id": uploaded["id"],
                "resident_token": _token(),
            },
        )

    assert _count(repo, command_receipts) == receipts_before


def test_download_attachment_denies_staff_private_attachment(repo):
    token = _token()
    created = _create(repo, str(uuid.uuid4()), token)
    private_content = b"Staff-private attachment bytes."
    private = repo.add_attachment(
        created["id"],
        created["revision"],
        "staff",
        name="private-note.txt",
        media_type="text/plain",
        content=private_content,
        public=False,
    )
    receipts_before = _count(repo, command_receipts)

    with pytest.raises(NotFound):
        repo.execute_resident_command(
            str(uuid.uuid4()),
            "download_attachment",
            {
                "request_id": created["id"],
                "attachment_id": private["id"],
                "resident_token": token,
            },
        )

    assert _count(repo, command_receipts) == receipts_before
    with repo.engine.connect() as conn:
        stored_receipts = conn.execute(select(command_receipts)).mappings().all()
    serialized = json.dumps([dict(row) for row in stored_receipts], default=str)
    assert private_content.decode() not in serialized
