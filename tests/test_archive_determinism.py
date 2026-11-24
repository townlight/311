from __future__ import annotations

import hashlib
import io
import json
import secrets
import zipfile

import pytest
from sqlalchemy import create_engine

from townlight_311.workflow import ServiceRequestRepository

FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


@pytest.fixture
def repo(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'archive.sqlite3'}", future=True
    )
    repository = ServiceRequestRepository(engine)
    repository.save_category("waste", "Waste Services", "Public Works", True, "admin")
    yield repository
    repository.engine.dispose()


def test_archive_is_byte_deterministic_and_tracks_updates(repo):
    created = repo.create_request(
        category="waste",
        description="Broken bin lid",
        location="1 Main Street",
        contact={
            "name": "Resident Example",
            "email": "resident@example.com",
            "phone": "+15550001111",
        },
        resident_token=secrets.token_urlsafe(32),
        idempotency_key=secrets.token_urlsafe(24),
    )
    request_id = created["id"]
    repo.add_attachment(
        request_id,
        created["revision"],
        "staff",
        name="evidence.txt",
        media_type="text/plain",
        content=b"deterministic evidence",
        public=True,
    )

    first = repo.archive(request_id)
    second = repo.archive(request_id)

    assert first == second
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()

    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        infos = archive.infolist()
        assert infos
        for info in infos:
            assert info.date_time == FIXED_TIMESTAMP
            assert info.compress_type == zipfile.ZIP_DEFLATED

        record = json.loads(archive.read("request.json"))
        assert record["id"] == request_id
        manifest = json.loads(archive.read("manifest.json"))
        assert {entry["name"] for entry in manifest} == {
            name for name in archive.namelist() if name != "manifest.json"
        }

    revision = repo.get_staff(request_id)["revision"]
    repo.add_comment(request_id, revision, "staff", "follow-up note", True)

    updated = repo.archive(request_id)
    assert updated != first

    with zipfile.ZipFile(io.BytesIO(updated)) as archive:
        record = json.loads(archive.read("request.json"))
        assert any(
            event["message"] == "follow-up note" for event in record["events"]
        )
