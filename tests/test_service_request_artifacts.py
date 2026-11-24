from __future__ import annotations

import hashlib
import io
import json
import secrets
import zipfile

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, func, select

from townlight_311.models import attachments, events
from townlight_311.store import Conflict, InvalidInput, NotFound, Unauthorized
from townlight_311.workflow import ServiceRequestRepository

PNG_PREFIX = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "artifacts.sqlite3"


@pytest.fixture
def repo(db_path):
    engine = create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    repository = ServiceRequestRepository(engine)
    repository.save_category("waste", "Waste Services", "Public Works", True, "admin")
    yield repository
    repository.engine.dispose()


def _token():
    return secrets.token_urlsafe(32)


def _create(repo, *, token=None, key=None, contact=None):
    token = token or _token()
    key = key or secrets.token_urlsafe(24)
    created = repo.create_request(
        category="waste",
        description="Broken bin lid",
        location="1 Main Street",
        contact=contact
        or {
            "name": "Resident Example",
            "email": "resident@example.com",
            "phone": "+15550001111",
        },
        resident_token=token,
        idempotency_key=key,
    )
    return created, token, key


def _count(repo, table):
    with repo.engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def test_resident_attachment_roundtrip_survives_restart(db_path, repo):
    created, token, _ = _create(repo)
    request_id = created["id"]
    blob = PNG_PREFIX + b"roundtrip-payload"
    digest = hashlib.sha256(blob).hexdigest()

    added = repo.add_attachment(
        request_id,
        created["revision"],
        "resident",
        name="photo.png",
        media_type="image/png",
        content=blob,
        public=False,
        resident_token=token,
    )
    assert added["public"] is True
    assert added["sha256"] == digest

    repo.engine.dispose()
    restarted = ServiceRequestRepository(
        create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
    )
    try:
        downloaded = restarted.get_attachment_resident(
            request_id, added["id"], token
        )
        assert downloaded["content"] == blob
        assert downloaded["sha256"] == digest
        assert downloaded["size"] == len(blob)
    finally:
        restarted.engine.dispose()


def test_private_attachment_denied_and_wrong_token_rejected(repo):
    created, token, _ = _create(repo)
    request_id = created["id"]

    private = repo.add_attachment(
        request_id,
        created["revision"],
        "staff",
        name="private.txt",
        media_type="text/plain",
        content=b"private evidence",
        public=False,
    )

    with pytest.raises(NotFound):
        repo.get_attachment_resident(request_id, private["id"], token)
    with pytest.raises(Unauthorized):
        repo.get_attachment_resident(request_id, private["id"], _token())

    staff = repo.get_attachment_staff(request_id, private["id"])
    assert staff["content"] == b"private evidence"


def test_stale_upload_rolls_back_attachment_and_event_rows(repo):
    created, _, _ = _create(repo)
    request_id = created["id"]
    before_attachments = _count(repo, attachments)
    before_events = _count(repo, events)

    with pytest.raises(Conflict):
        repo.add_attachment(
            request_id,
            created["revision"] + 3,
            "staff",
            name="stale.txt",
            media_type="text/plain",
            content=b"stale",
        )

    assert _count(repo, attachments) == before_attachments
    assert _count(repo, events) == before_events
    staff = repo.get_staff(request_id)
    assert staff["revision"] == created["revision"]


def test_oversized_unsafe_and_wrong_magic_uploads_rejected(repo):
    created, _, _ = _create(repo)
    request_id = created["id"]
    revision = created["revision"]
    before_attachments = _count(repo, attachments)

    with pytest.raises(InvalidInput):
        repo.add_attachment(
            request_id,
            revision,
            "staff",
            name="too-big.txt",
            media_type="text/plain",
            content=b"a" * (10 * 1024 * 1024 + 1),
        )

    for unsafe_name in (
        "../escape.txt",
        "..\\escape.txt",
        "bad<name>.txt",
        'bad:name.txt',
        'bad"name.txt',
        "bad|name.txt",
        "bad?name.txt",
        "bad*name.txt",
        "trailing.",
        "trailing ",
        "CON.txt",
        "com1.txt",
        "LPT9.pdf",
        "line\nbreak.txt",
    ):
        with pytest.raises(InvalidInput):
            repo.add_attachment(
                request_id,
                revision,
                "staff",
                name=unsafe_name,
                media_type="text/plain",
                content=b"text",
            )

    with pytest.raises(InvalidInput):
        repo.add_attachment(
            request_id,
            revision,
            "staff",
            name="not-really.png",
            media_type="image/png",
            content=b"plain text",
        )

    assert _count(repo, attachments) == before_attachments
    assert repo.get_staff(request_id)["revision"] == revision


def test_resident_actor_and_visibility_cannot_be_spoofed(repo):
    created, token, _ = _create(repo)
    request_id = created["id"]

    added = repo.add_attachment(
        request_id,
        created["revision"],
        "admin",
        name="resident.txt",
        media_type="text/plain",
        content=b"resident owned",
        public=False,
        resident_token=token,
    )
    assert added["public"] is True

    staff = repo.get_staff(request_id)
    attachment_events = [
        event for event in staff["events"] if event["kind"] == "attachment_added"
    ]
    assert len(attachment_events) == 1
    assert attachment_events[0]["actor"] == "resident"
    assert attachment_events[0]["public"] is True


def test_archive_manifest_matches_members_and_excludes_secrets(repo):
    token = _token()
    key = secrets.token_urlsafe(24)
    created, _, _ = _create(repo, token=token, key=key)
    request_id = created["id"]

    repo.add_comment(
        request_id,
        created["revision"],
        "auditor",
        "internal private audit",
        False,
    )
    revision = repo.get_staff(request_id)["revision"]
    blob = PNG_PREFIX + b"archived-attachment"
    repo.add_attachment(
        request_id,
        revision,
        "staff",
        name="evidence.png",
        media_type="image/png",
        content=blob,
        public=True,
    )

    archive_bytes = repo.archive(request_id)
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
        assert len(manifest) == len(names) - 1

        for entry in manifest:
            data = archive.read(entry["name"])
            assert hashlib.sha256(data).hexdigest() == entry["sha256"]
            assert len(data) == entry["size"]

        attachment_members = [
            name for name in names if name.startswith("attachments/")
        ]
        assert len(attachment_members) == 1
        assert archive.read(attachment_members[0]) == blob

        request_bytes = archive.read("request.json")
        record = json.loads(request_bytes)
        messages = [event["message"] for event in record["events"]]
        assert "internal private audit" in messages
        assert "resident_token_hash" not in record
        assert "idempotency_key" not in record
        assert "input_sha256" not in record

        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for name in names:
            member = archive.read(name)
            assert token_hash.encode("utf-8") not in member
            assert token.encode("utf-8") not in member
            assert key.encode("utf-8") not in member


def test_corrupt_attachment_blob_fails_archive_export(repo):
    created, _, _ = _create(repo)
    request_id = created["id"]
    added = repo.add_attachment(
        request_id,
        created["revision"],
        "staff",
        name="evidence.txt",
        media_type="text/plain",
        content=b"good content",
        public=True,
    )

    with repo.engine.begin() as conn:
        conn.execute(
            sa.update(attachments)
            .where(attachments.c.id == added["id"])
            .values(content=b"tampered")
        )

    with pytest.raises(Conflict):
        repo.archive(request_id)


def test_open311_requires_token_and_omits_contact(repo):
    contact = {
        "name": "Secret Resident Name",
        "email": "secret@example.com",
        "phone": "+19998887777",
    }
    created, token, _ = _create(repo, contact=contact)
    request_id = created["id"]

    result = repo.open311(request_id, token)
    assert "contact" not in result
    serialized = json.dumps(result, default=str)
    for value in contact.values():
        assert value not in serialized

    with pytest.raises(Unauthorized):
        repo.open311(request_id, _token())
