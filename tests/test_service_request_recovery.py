from __future__ import annotations

import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, func, select

from townlight_311.models import (
    command_receipts,
    events,
    notifications,
    recovery_challenges,
)
from townlight_311.notifications import NotificationWorker
from townlight_311.store import InvalidInput, Unauthorized
from townlight_311.workflow import ServiceRequestRepository

_CODE_RE = re.compile(r"recovery code is: (\d{6})")


class CapturingTransport:
    sender = "city@example.test"

    def __init__(self) -> None:
        self.messages = []
        self.calls = 0

    def send(self, message) -> None:
        self.calls += 1
        self.messages.append(message)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "townlight311-recovery.sqlite3"


@pytest.fixture
def repo(db_path: Path):
    repository = ServiceRequestRepository(
        create_engine(
            f"sqlite+pysqlite:///{db_path}",
            future=True,
            connect_args={"timeout": 30},
        )
    )
    repository.save_category("waste", "Waste Services", "Public Works", True, "admin")
    repository.recovery_key = Fernet.generate_key()
    yield repository
    repository.engine.dispose()


def _token() -> str:
    return secrets.token_urlsafe(32)


def _create(repo, token, *, email: str | None = "resident@example.com"):
    contact = {"name": "Resident Example", "phone": "+15550001111"}
    if email is not None:
        contact["email"] = email
    created = repo.create_request(
        category="waste",
        description="Broken bin lid",
        location="1 Main Street",
        contact=contact,
        resident_token=token,
        idempotency_key=str(uuid.uuid4()),
    )
    # The submitted-request notification is unrelated to recovery tests. Mark
    # it sent so the worker claims only the recovery outbox row.
    with repo.engine.begin() as conn:
        conn.execute(
            sa.update(notifications).values(
                state="sent", sent_at=datetime.now(UTC)
            )
        )
    return created


def _count(repo, table) -> int:
    with repo.engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def _pending_count(repo) -> int:
    with repo.engine.connect() as conn:
        return conn.execute(
            select(func.count())
            .select_from(notifications)
            .where(notifications.c.state == "pending")
        ).scalar_one()


def _challenge(repo, challenge_id: str):
    with repo.engine.connect() as conn:
        return (
            conn.execute(
                select(recovery_challenges).where(
                    recovery_challenges.c.id == challenge_id
                )
            )
            .mappings()
            .one()
        )


def _all_challenges(repo):
    with repo.engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(select(recovery_challenges)).mappings().all()
        ]


def _send_code(repo, transport, *, recovery_key=None):
    worker = NotificationWorker(
        repo.engine,
        transport,
        recovery_key=repo.recovery_key if recovery_key is None else recovery_key,
    )
    result = worker.process_one()
    assert result is not None
    return result


def _code_from(message) -> str:
    match = _CODE_RE.search(message.get_content())
    assert match is not None
    return match.group(1)


def _wrong(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


def test_recovery_code_is_emailed_but_not_stored_plaintext(repo, db_path):
    old_token = _token()
    created = _create(repo, old_token)
    command_id = str(uuid.uuid4())

    begun = repo.execute_resident_command(
        command_id,
        "begin_recovery",
        {"tracking": created["tracking"], "email": "Resident@Example.com"},
    )
    assert begun["message"] == "If the details match, a recovery code will be emailed."
    challenge_id = begun["challenge_id"]

    challenge = _challenge(repo, challenge_id)
    assert challenge["request_id"] == created["id"]
    assert challenge["encrypted_code"] is None
    assert _count(repo, command_receipts) == 1
    assert _pending_count(repo) == 1

    with repo.engine.connect() as conn:
        event = (
            conn.execute(
                select(events).where(
                    events.c.request_id == created["id"],
                    events.c.kind == "recovery_requested",
                )
            )
            .mappings()
            .one()
        )
        outbox = conn.execute(
            select(notifications).where(notifications.c.state == "pending")
        ).mappings().one()
    assert event["public"] is False
    assert outbox["payload"]["kind"] == "recovery"
    assert outbox["payload"]["challenge_id"] == challenge_id
    assert "expires_at" in outbox["payload"]

    transport = CapturingTransport()
    assert _send_code(repo, transport)["state"] == "sent"
    code = _code_from(transport.messages[0])
    assert transport.messages[0]["To"] == "resident@example.com"
    assert code not in transport.messages[0]["Subject"]
    assert code not in transport.messages[0]["Message-ID"]
    assert code.encode("ascii") not in db_path.read_bytes()


def test_wrong_email_tracking_and_phone_only_are_generic_without_email(repo):
    token = _token()
    created = _create(repo, token)
    _create(repo, _token(), email=None)

    responses = [
        repo.begin_recovery(created["tracking"], "someone-else@example.com"),
        repo.begin_recovery("TL311-does-not-exist", "resident@example.com"),
        repo.begin_recovery(created["tracking"], "resident@example.com"),
    ]
    for response in responses:
        assert (
            response["message"]
            == "If the details match, a recovery code will be emailed."
        )

    # The last response matched the real request and has an outbox item; the
    # two failed lookups must not disclose or email anything.
    transport = CapturingTransport()
    assert _send_code(repo, transport)["state"] == "sent"
    assert len(transport.messages) == 1
    assert _pending_count(repo) == 0
    fake = [row for row in _all_challenges(repo) if row["request_id"] is None]
    assert len(fake) == 2


def test_begin_rate_limit_is_five_per_lookup_hash_hour(repo):
    for _ in range(5):
        result = repo.begin_recovery("TL311-missing", "resident@example.com")
        assert result["challenge_id"] is not None

    blocked = repo.begin_recovery("TL311-missing", "resident@example.com")
    assert blocked["challenge_id"] is None
    assert blocked["message"] == "If the details match, a recovery code will be emailed."
    assert _count(repo, recovery_challenges) == 5
    assert _count(repo, notifications) == 0


def test_begin_fails_closed_without_key(repo):
    token = _token()
    created = _create(repo, token)
    repo.recovery_key = None

    with pytest.raises(InvalidInput):
        repo.begin_recovery(created["tracking"], "resident@example.com")
    assert _count(repo, recovery_challenges) == 0
    assert _pending_count(repo) == 0


def test_worker_without_recovery_key_fails_private_recovery_permanently(repo):
    token = _token()
    created = _create(repo, token)
    repo.begin_recovery(created["tracking"], "resident@example.com")

    transport = CapturingTransport()
    result = NotificationWorker(repo.engine, transport).process_one()
    assert result is not None and result["state"] == "failed"
    assert transport.calls == 0
    with repo.engine.connect() as conn:
        row = (
            conn.execute(
                select(notifications).where(notifications.c.state == "failed")
            )
            .mappings()
            .one()
        )
    assert "code" not in (row["last_error"] or "").lower()


def test_five_wrong_attempts_are_durable_and_exhaust_challenge(repo):
    old_token = _token()
    created = _create(repo, old_token)
    begun = repo.begin_recovery(created["tracking"], "resident@example.com")
    challenge_id = begun["challenge_id"]

    transport = CapturingTransport()
    _send_code(repo, transport)
    correct_code = _code_from(transport.messages[0])
    wrong_code = _wrong(correct_code)
    replacement = _token()

    for attempt in range(5):
        result = repo.execute_resident_command(
            str(uuid.uuid4()),
            "complete_recovery",
            {
                "challenge_id": challenge_id,
                "code": wrong_code,
                "new_token": replacement,
            },
        )
        assert result == {
            "recovered": False,
            "detail": "Recovery code is invalid or expired.",
        }
        assert _challenge(repo, challenge_id)["attempts"] == attempt + 1

    assert _count(repo, command_receipts) == 5
    exhausted = repo.complete_recovery(challenge_id, correct_code, replacement)
    assert exhausted["recovered"] is False
    # The old token still works because the challenge was exhausted, not used.
    assert repo.get_resident(created["id"], old_token)["revision"] == created["revision"]


def test_expired_challenge_is_not_sent_and_cannot_complete(repo):
    token = _token()
    created = _create(repo, token)
    begun = repo.begin_recovery(created["tracking"], "resident@example.com")
    challenge_id = begun["challenge_id"]

    with repo.engine.begin() as conn:
        conn.execute(
            sa.update(recovery_challenges)
            .where(recovery_challenges.c.id == challenge_id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    transport = CapturingTransport()
    result = _send_code(repo, transport)
    assert result["state"] == "failed"
    assert transport.calls == 0
    assert (
        repo.complete_recovery(challenge_id, "000000", _token())["recovered"]
        is False
    )


def test_valid_recovery_rotates_token_once_and_returns_private_projection(repo, db_path):
    old_token = _token()
    created = _create(repo, old_token)
    begun = repo.begin_recovery(created["tracking"], "resident@example.com")
    challenge_id = begun["challenge_id"]

    transport = CapturingTransport()
    _send_code(repo, transport)
    code = _code_from(transport.messages[0])
    new_token = _token()

    result = repo.complete_recovery(challenge_id, code, new_token)
    assert result["recovered"] is True
    assert result["request"]["revision"] == created["revision"] + 1
    assert "resident_token_hash" not in result["request"]
    assert "department" not in result["request"]
    assert all(event["kind"] != "recovery" for event in result["request"]["events"])

    with pytest.raises(Unauthorized):
        repo.get_resident(created["id"], old_token)
    current = repo.get_resident(created["id"], new_token)
    assert current["revision"] == created["revision"] + 1

    # A successful code is single-use.
    assert (
        repo.complete_recovery(challenge_id, code, _token())["recovered"] is False
    )

    staff = repo.get_staff(created["id"])
    recovery_events = [event for event in staff["events"] if event["kind"] == "recovery"]
    assert len(recovery_events) == 1
    assert recovery_events[0]["public"] is False
    assert code not in str(recovery_events[0]["payload"])

    dump = db_path.read_bytes()
    assert code.encode("ascii") not in dump
    assert new_token.encode("ascii") not in dump
