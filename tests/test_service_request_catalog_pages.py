from __future__ import annotations

import secrets

import pytest
from sqlalchemy import create_engine

from townlight_311.store import InvalidInput
from townlight_311.workflow import ServiceRequestRepository


@pytest.fixture
def repo(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{(tmp_path / 'catalog.sqlite3').as_posix()}",
        future=True,
    )
    repository = ServiceRequestRepository(engine)
    yield repository
    repository.engine.dispose()


def _token():
    return secrets.token_urlsafe(32)


def _create(repo, category):
    return repo.create_request(
        category=category,
        description="Pothole near the library entrance",
        location="100 Main Street",
        contact={
            "name": "Resident Example",
            "email": "resident@example.com",
            "phone": "+15550001111",
        },
        resident_token=_token(),
        idempotency_key=secrets.token_urlsafe(24),
    )


def test_deactivated_category_hidden_by_default_and_visible_for_admin(repo):
    repo.save_category("waste", "Waste Services", "Public Works", True, "admin")

    assert [row["code"] for row in repo.categories()] == ["waste"]

    repo.save_category("waste", "Waste Services", "Public Works", False, "admin")

    assert repo.categories() == []
    inclusive = repo.categories(include_inactive=True)
    assert [row["code"] for row in inclusive] == ["waste"]
    assert inclusive[0]["active"] is False

    repo.save_category("waste", "Waste Services", "Public Works", True, "admin")
    assert [row["code"] for row in repo.categories()] == ["waste"]


def test_catalog_pages_cover_all_requests_without_overlap(repo):
    repo.save_category("waste", "Waste Services", "Public Works", True, "admin")
    created = [_create(repo, "waste") for _ in range(3)]
    created_ids = {request["id"] for request in created}

    first_page = repo.list_requests(limit=2, offset=0)
    second_page = repo.list_requests(limit=2, offset=2)

    assert len(first_page) == 2
    assert len(second_page) == 1
    first_ids = [request["id"] for request in first_page]
    second_ids = [request["id"] for request in second_page]
    assert set(first_ids).isdisjoint(second_ids)
    assert set(first_ids) | set(second_ids) == created_ids


@pytest.mark.parametrize("offset", [-1, True, False, 10_000_001, "1", 1.5])
def test_invalid_offset_rejected(repo, offset):
    with pytest.raises(InvalidInput):
        repo.list_requests(limit=2, offset=offset)


@pytest.mark.parametrize("include_inactive", [1, 0, "true", None])
def test_invalid_include_inactive_rejected(repo, include_inactive):
    with pytest.raises(InvalidInput):
        repo.categories(include_inactive=include_inactive)
