from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine

from townlight_311.models import notifications
from townlight_311.notifications import (
    NotificationWorker,
    RejectionError,
    SafeRetryError,
    SMTPTransport,
    UncertainDeliveryError,
)
from townlight_311.store import RepositoryBase


class FakeTransport:
    def __init__(self, sender: str = "city@example.test") -> None:
        self.sender = sender
        self.messages: list[EmailMessage] = []
        self.calls = 0

    def send(self, message: EmailMessage) -> None:
        self.calls += 1
        self.messages.append(message)


class FailingTransport(FakeTransport):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def send(self, message: EmailMessage) -> None:
        self.calls += 1
        raise self.error


def _engine(path: Path) -> sa.Engine:
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    return RepositoryBase(engine).engine


def _add_notification(
    engine: sa.Engine,
    notification_id: str = "notification-1",
    recipient: str = "resident@example.test",
    payload: dict[str, str] | None = None,
) -> None:
    if payload is None:
        payload = {
            "tracking": "TL-100",
            "state": "triaged",
            "event_message": "Your request was reviewed.",
            "not_allowlisted": "must not appear",
        }
    with engine.begin() as conn:
        conn.execute(
            notifications.insert().values(
                id=notification_id,
                request_id="request-1",
                event_id="event-1",
                recipient=recipient,
                payload=payload,
                state="pending",
                attempts=0,
                created_at=datetime.now(UTC),
            )
        )


def _state(engine: sa.Engine, notification_id: str = "notification-1") -> dict[str, object]:
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(notifications).where(notifications.c.id == notification_id)
        ).mappings().one()
    return dict(row)


def _make_due(engine: sa.Engine, notification_id: str = "notification-1") -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.update(notifications)
            .where(notifications.c.id == notification_id)
            .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
        )


class BlockingTransport(FakeTransport):
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self.started_event = started
        self.release_event = release

    def send(self, message: EmailMessage) -> None:
        self.calls += 1
        self.started_event.set()
        assert self.release_event.wait(timeout=5)


def test_pending_notification_persists_across_restart_then_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "notifications.db"
    engine = _engine(path)
    _add_notification(engine)
    engine.dispose()

    restarted = _engine(path)
    transport = FakeTransport()
    result = NotificationWorker(restarted, transport).process_one()
    row = _state(restarted)
    restarted.dispose()

    assert result is not None
    assert result["state"] == "sent"
    assert row["state"] == "sent"
    assert row["sent_at"] is not None
    assert row["claim_id"] is None
    assert transport.calls == 1


def test_connect_failure_is_safely_retried_when_due(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "retry.db")
    _add_notification(engine)
    failing = FailingTransport(SafeRetryError("SMTP connection or authentication failed"))
    worker = NotificationWorker(engine, failing)

    first = worker.process_one()
    row = _state(engine)
    assert first is not None and first["state"] == "pending"
    assert row["state"] == "pending"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] is not None
    assert "password" not in str(row["last_error"]).lower()

    assert worker.process_one() is None
    _make_due(engine)
    accepted = FakeTransport()
    second = NotificationWorker(engine, accepted).process_one()
    assert second is not None and second["state"] == "sent"
    assert accepted.calls == 1
    engine.dispose()


def test_explicit_smtp_rejection_retries_transient_and_fails_permanent(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "rejection.db")
    _add_notification(engine)
    transient = FailingTransport(RejectionError("SMTP rejected", retryable=True, code=451))
    worker = NotificationWorker(engine, transient)

    assert worker.process_one() is not None
    assert _state(engine)["state"] == "pending"

    _make_due(engine)
    permanent = FailingTransport(RejectionError("SMTP rejected", retryable=False, code=550))
    result = NotificationWorker(engine, permanent).process_one()
    row = _state(engine)
    assert result is not None and result["state"] == "failed"
    assert row["state"] == "failed"
    assert row["attempts"] == 2
    assert worker.process_one() is None
    engine.dispose()


def test_ambiguous_delivery_is_uncertain_and_never_auto_resent(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "uncertain.db")
    _add_notification(engine)
    transport = FailingTransport(UncertainDeliveryError("Delivery status unknown"))
    worker = NotificationWorker(engine, transport)

    result = worker.process_one()
    row = _state(engine)
    assert result is not None and result["state"] == "uncertain"
    assert row["state"] == "uncertain"
    assert row["next_attempt_at"] is None
    assert worker.process_one() is None
    assert transport.calls == 1
    engine.dispose()


def test_expired_sending_lease_becomes_uncertain_without_resend(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "expired.db")
    _add_notification(engine)
    worker = NotificationWorker(engine, FakeTransport(), lease_seconds=300)
    claimed = worker._claim()
    assert claimed is not None

    with engine.begin() as conn:
        conn.execute(
            sa.update(notifications)
            .where(notifications.c.id == "notification-1")
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )

    transport = FakeTransport()
    recovered = NotificationWorker(engine, transport)
    assert recovered.recover_expired() == 1
    row = _state(engine)
    assert row["state"] == "uncertain"
    assert row["claim_id"] is None
    assert recovered.process_one() is None
    assert transport.calls == 0
    engine.dispose()


def test_two_workers_compete_for_one_claim_and_send_once(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "competition.db")
    _add_notification(engine)
    started = threading.Event()
    release = threading.Event()
    transport = BlockingTransport(started, release)
    first = NotificationWorker(engine, transport)
    second = NotificationWorker(engine, transport)

    results: list[dict[str, object] | None] = []
    thread = threading.Thread(target=lambda: results.append(first.process_one()))
    thread.start()
    assert started.wait(timeout=5)
    second_result = second.process_one()
    release.set()
    thread.join(timeout=5)

    assert second_result is None
    assert len(results) == 1 and results[0] is not None
    assert results[0]["state"] == "sent"
    assert transport.calls == 1
    assert _state(engine)["state"] == "sent"
    engine.dispose()


def test_tls_required_except_explicit_localhost_and_crlf_recipient_rejected(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="TLS is required"):
        SMTPTransport("mail.example.test", 587, "city@example.test", use_tls=False)
    SMTPTransport(
        "localhost",
        1025,
        "city@example.test",
        use_tls=False,
        allow_insecure_localhost=True,
    )

    engine = _engine(tmp_path / "crlf.db")
    _add_notification(engine, recipient="resident@example.test\r\nBcc: attacker@example.test")
    transport = FakeTransport()
    result = NotificationWorker(engine, transport).process_one()
    row = _state(engine)
    assert result is not None and result["state"] == "failed"
    assert row["state"] == "failed"
    assert transport.calls == 0
    engine.dispose()


def test_allowlisted_content_stable_message_id_and_mock_smtp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = _engine(tmp_path / "message.db")
    _add_notification(engine)
    transport = FakeTransport()
    result = NotificationWorker(engine, transport).process_one()
    engine.dispose()

    assert result is not None and result["state"] == "sent"
    message = transport.messages[0]
    assert message["Message-ID"] == "<notification-1@townlight.local>"
    assert message["To"] == "resident@example.test"
    assert "TL-100" in message["Subject"]
    assert "triaged" in message["Subject"]
    body = message.get_content()
    assert "Your request was reviewed." in body
    assert "must not appear" not in body

    calls: dict[str, object] = {}

    class MockSMTP:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            calls["host"] = host
            calls["port"] = port
            calls["timeout"] = timeout

        def ehlo(self) -> None:
            calls["ehlo"] = int(calls.get("ehlo", 0)) + 1

        def starttls(self, context: object) -> None:
            calls["starttls"] = context is not None

        def login(self, username: str, password: str) -> None:
            calls["login"] = (username, password)

        def send_message(
            self, message: EmailMessage, from_addr: str, to_addrs: list[str]
        ) -> None:
            calls["message_id"] = message["Message-ID"]
            calls["from"] = from_addr
            calls["to"] = to_addrs

        def quit(self) -> None:
            calls["quit"] = True

    monkeypatch.setattr("townlight_311.notifications.smtplib.SMTP", MockSMTP)
    message = EmailMessage()
    message["To"] = "resident@example.test"
    message["Message-ID"] = "<stable-id@townlight.local>"
    message.set_content("body")
    transport = SMTPTransport(
        "mail.example.test",
        587,
        "city@example.test",
        ("smtp-user", "smtp-password"),
    )
    transport.send(message)

    assert calls["starttls"] is True
    assert calls["login"] == ("smtp-user", "smtp-password")
    assert calls["message_id"] == "<stable-id@townlight.local>"
    assert calls["to"] == ["resident@example.test"]
