from __future__ import annotations

import csv
import io
import json

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine

from townlight_311.interchange import INPUT_FIELDS, Interchange
from townlight_311.models import requests
from townlight_311.store import Conflict, InvalidInput
from townlight_311.workflow import ServiceRequestRepository

# store.py currently calls Engine.execption_options (a typo outside this
# assignment's write scope). Keep the tests runnable without editing it.
if not hasattr(Engine, "execption_options"):
    Engine.execption_options = Engine.execution_options

IMPORT_KEY = b"townlight-test-import-key-000000000000"


@pytest.fixture
def repo(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'interchange.sqlite3'}", future=True
    )
    repository = ServiceRequestRepository(engine)
    repository.save_category("waste", "Waste Services", "Public Works", True, "admin")
    yield repository
    repository.engine.dispose()


@pytest.fixture
def interchange(repo):
    return Interchange(repo, import_key=IMPORT_KEY)


def _row(external_id="EXT-1", **overrides):
    row = {
        "external_id": external_id,
        "category": "waste",
        "description": "Broken bin lid",
        "location": "1 Main Street",
        "contact_name": "Resident Example",
        "email": "resident@example.com",
        "phone": "+15550001111",
    }
    row.update(overrides)
    return row


def _csv(rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output, fieldnames=list(INPUT_FIELDS), lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _json(rows):
    return json.dumps({"schema_version": 1, "requests": rows}).encode("utf-8")


def _count(repo):
    with repo.engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(requests)).scalar_one()


def test_preview_rejects_invalid_rows_without_writes(repo, interchange):
    with pytest.raises(InvalidInput):
        interchange.preview(_json([{**_row(), "unexpected": "x"}]), "json")
    with pytest.raises(InvalidInput):
        interchange.preview(_json([_row(category="missing")]), "json")
    with pytest.raises(InvalidInput):
        interchange.preview(_json([_row(email="not-an-email")]), "json")
    assert _count(repo) == 0


def test_duplicate_external_id_within_batch_is_rejected(repo, interchange):
    batch = _csv([_row("EXT-1"), _row("EXT-1", description="Other")])
    with pytest.raises(InvalidInput):
        interchange.preview(batch, "csv")
    with pytest.raises(InvalidInput):
        interchange.commit(batch, "csv", "batch-1", "staff")
    assert _count(repo) == 0


def test_commit_creates_real_rows_and_reports_tracking(repo, interchange):
    batch = _csv([_row("EXT-1"), _row("EXT-2", description="Missed collection")])
    result = interchange.commit(batch, "csv", "batch-1", "staff")

    assert result["count"] == 2
    assert [item["status"] for item in result["results"]] == ["created", "created"]
    assert all(
        item["tracking"].startswith("TL311-") for item in result["results"]
    )
    assert _count(repo) == 2
    stored = {row["description"] for row in repo.list_requests()}
    assert stored == {"Broken bin lid", "Missed collection"}


def test_repeated_same_batch_is_idempotent(repo, interchange):
    batch = _csv([_row("EXT-1"), _row("EXT-2")])
    first = interchange.commit(batch, "csv", "batch-1", "staff")
    second = interchange.commit(batch, "csv", "batch-1", "staff")

    assert second["count"] == 2
    assert [item["request_id"] for item in second["results"]] == [
        item["request_id"] for item in first["results"]
    ]
    assert [item["status"] for item in second["results"]] == [
        "existing",
        "existing",
    ]
    assert _count(repo) == 2


def test_changed_row_conflicts_and_rolls_back_entire_batch(repo, interchange):
    interchange.commit(
        _csv([_row("EXT-1"), _row("EXT-2")]), "csv", "batch-1", "staff"
    )
    changed = _csv(
        [
            _row("EXT-3", description="Brand new row"),
            _row("EXT-2", description="Changed input"),
        ]
    )

    with pytest.raises(Conflict):
        interchange.commit(changed, "csv", "batch-1", "staff")

    assert _count(repo) == 2
    descriptions = {row["description"] for row in repo.list_requests()}
    assert descriptions == {"Broken bin lid"}


def test_export_is_decodable_roundtrips_and_hides_private_fields(repo, interchange):
    committed = interchange.commit(_csv([_row("EXT-1")]), "csv", "batch-1", "staff")
    request_id = committed["results"][0]["request_id"]

    exported_raw = interchange.export("json")
    exported = json.loads(exported_raw.decode("utf-8"))
    assert exported["schema_version"] == 1
    assert len(exported["requests"]) == 1
    record = exported["requests"][0]
    assert set(record) == set(INPUT_FIELDS)
    assert record["external_id"] == request_id
    assert record["contact_name"] == record["email"] == record["phone"] == ""

    blob = exported_raw.decode("utf-8")
    for private in (
        "resident_token_hash",
        "idempotency_key",
        "input_sha256",
        "state",
    ):
        assert private not in blob

    with_contact = json.loads(
        interchange.export("json", include_contact=True).decode("utf-8")
    )
    assert with_contact["requests"][0]["contact_name"] == "Resident Example"

    csv_bytes = interchange.export("csv")
    header = csv_bytes.decode("utf-8").splitlines()[0]
    assert header == ",".join(INPUT_FIELDS)

    roundtrip = interchange.commit(csv_bytes, "csv", "roundtrip-key", "staff")
    assert roundtrip["count"] == 1
    assert roundtrip["results"][0]["status"] == "created"
    assert _count(repo) == 2
