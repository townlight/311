from __future__ import annotations

import secrets

import pytest
import sqlalchemy as sa

from townlight_311.migrations import (
    REVISION,
    VERSION_TABLE,
    ready,
    schema_versions,
    upgrade,
)
from townlight_311.models import SCHEMA, metadata
from townlight_311.workflow import ServiceRequestRepository


@pytest.fixture
def engine(tmp_path):
    database = tmp_path / "migration.sqlite3"
    created = sa.create_engine(f"sqlite+pysqlite:///{database}", future=True)
    yield created
    created.dispose()


def _translated(engine):
    return engine.execution_options(schema_translate_map={SCHEMA: None})


def _create_request(repo):
    repo.save_category("waste", "Waste Services", "Public Works", True, "admin")
    token = secrets.token_urlsafe(32)
    created = repo.create_request(
        category="waste",
        description="Broken bin lid",
        location="1 Main Street",
        contact={"name": "Resident Example", "email": "resident@example.com"},
        resident_token=token,
        idempotency_key=secrets.token_urlsafe(24),
    )
    return created, token


def test_ready_is_false_before_upgrade_without_creating_tables(engine):
    assert ready(engine) is False

    with engine.connect() as conn:
        assert sa.inspect(conn).get_table_names() == []


def test_upgrade_creates_all_tables_and_revision_one(engine):
    upgrade(engine)

    assert ready(engine) is True
    expected_tables = {table.name for table in metadata.tables.values()}
    expected_tables.add(VERSION_TABLE)

    with engine.connect() as conn:
        assert expected_tables.issubset(set(sa.inspect(conn).get_table_names()))

    with _translated(engine).connect() as conn:
        revisions = set(
            conn.execute(sa.select(schema_versions.c.revision)).scalars()
        )
    assert revisions == {REVISION}


def test_second_upgrade_preserves_request_category_and_attachment_bytes(engine):
    upgrade(engine)
    repo = ServiceRequestRepository(engine)
    created, _ = _create_request(repo)
    request_id = created["id"]
    content = b"resident evidence\x00bytes"

    attachment = repo.add_attachment(
        request_id,
        created["revision"],
        "staff",
        name="evidence.txt",
        media_type="text/plain",
        content=content,
        public=True,
    )

    upgrade(engine)

    assert ready(engine) is True
    staff = repo.get_staff(request_id)
    assert staff["id"] == request_id
    assert staff["category"] == "waste"
    assert [category["code"] for category in repo.categories(include_inactive=True)] == [
        "waste"
    ]
    stored = repo.get_attachment_staff(request_id, attachment["id"])
    assert stored["content"] == content


def test_upgrade_refuses_a_newer_schema_revision(engine):
    upgrade(engine)

    with _translated(engine).begin() as conn:
        conn.execute(sa.insert(schema_versions).values(revision=2))

    with pytest.raises(RuntimeError, match="newer"):
        upgrade(engine)

    assert ready(engine) is False
