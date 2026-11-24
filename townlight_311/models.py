"""SQLAlchemy Core metadata and tables for the Townlight 311 schema."""

from __future__ import annotations

import sqlalchemy as sa

SCHEMA = "townlight_311"
UUID_LENGTH = 36
HASH_LENGTH = 64

metadata = sa.MetaData(schema=SCHEMA)

REQUEST_STATES = (
    "submitted",
    "triaged",
    "assigned",
    "in_progress",
    "resolved",
    "closed",
    "rejected",
    "duplicate",
    "reopened",
)
NOTIFICATION_STATES = ("pending", "sending", "sent", "failed", "uncertain")


def _allowed_values(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


service_categories = sa.Table(
    "service_categories",
    metadata,
    sa.Column("code", sa.String(64), primary_key=True),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("department", sa.String(255), nullable=False),
    sa.Column("active", sa.Boolean(), nullable=False),
)

requests = sa.Table(
    "requests",
    metadata,
    sa.Column("id", sa.String(UUID_LENGTH), primary_key=True),
    sa.Column("tracking", sa.String(64), nullable=False),
    sa.Column("resident_token_hash", sa.String(HASH_LENGTH), nullable=False),
    sa.Column(
        "category",
        sa.String(64),
        sa.ForeignKey(f"{SCHEMA}.service_categories.code"),
        nullable=False,
    ),
    sa.Column("description", sa.Text(), nullable=False),
    sa.Column("location", sa.Text(), nullable=False),
    sa.Column("contact", sa.JSON(), nullable=False),
    sa.Column("state", sa.String(32), nullable=False),
    sa.Column("department", sa.String(255), nullable=False),
    sa.Column("assignee", sa.String(255), nullable=True),
    sa.Column("priority", sa.String(32), nullable=False),
    sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("revision", sa.Integer(), nullable=False),
    sa.Column(
        "duplicate_of",
        sa.String(UUID_LENGTH),
        sa.ForeignKey(f"{SCHEMA}.requests.id"),
        nullable=True,
    ),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("idempotency_key", sa.String(128), nullable=True),
    sa.Column("input_sha256", sa.String(HASH_LENGTH), nullable=False),
    sa.UniqueConstraint("tracking", name="uq_requests_tracking"),
    sa.UniqueConstraint("idempotency_key", name="uq_requests_idempotency_key"),
    sa.CheckConstraint("revision > 0", name="ck_requests_revision_positive"),
    sa.CheckConstraint(
        "duplicate_of IS NULL OR duplicate_of <> id",
        name="ck_requests_duplicate_of_not_self",
    ),
    sa.CheckConstraint(
        _allowed_values("state", REQUEST_STATES),
        name="ck_requests_state_allowed",
    ),
    sa.Index("ix_requests_queue", "state", "department", "priority", "due_at"),
    sa.Index("ix_requests_category_state", "category", "state"),
)

events = sa.Table(
    "events",
    metadata,
    sa.Column("id", sa.String(UUID_LENGTH), primary_key=True),
    sa.Column(
        "request_id",
        sa.String(UUID_LENGTH),
        sa.ForeignKey(f"{SCHEMA}.requests.id"),
        nullable=False,
    ),
    sa.Column("actor", sa.String(255), nullable=False),
    sa.Column("kind", sa.String(64), nullable=False),
    sa.Column("public", sa.Boolean(), nullable=False),
    sa.Column("message", sa.Text(), nullable=False),
    sa.Column("payload", sa.JSON(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index("ix_events_request_created", "request_id", "created_at"),
)

attachments = sa.Table(
    "attachments",
    metadata,
    sa.Column("id", sa.String(UUID_LENGTH), primary_key=True),
    sa.Column(
        "request_id",
        sa.String(UUID_LENGTH),
        sa.ForeignKey(f"{SCHEMA}.requests.id"),
        nullable=False,
    ),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("media_type", sa.String(127), nullable=False),
    sa.Column("content", sa.LargeBinary(), nullable=False),
    sa.Column("sha256", sa.String(HASH_LENGTH), nullable=False),
    sa.Column("size", sa.Integer(), nullable=False),
    sa.Column("public", sa.Boolean(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(
        "size >= 1 AND size <= 10485760",
        name="ck_attachments_size_positive",
    ),
    sa.Index("ix_attachments_request", "request_id"),
)

notifications = sa.Table(
    "notifications",
    metadata,
    sa.Column("id", sa.String(UUID_LENGTH), primary_key=True),
    sa.Column(
        "request_id",
        sa.String(UUID_LENGTH),
        sa.ForeignKey(f"{SCHEMA}.requests.id"),
        nullable=False,
    ),
    sa.Column(
        "event_id",
        sa.String(UUID_LENGTH),
        sa.ForeignKey(f"{SCHEMA}.events.id"),
        nullable=False,
    ),
    sa.Column("recipient", sa.String(320), nullable=False),
    sa.Column("payload", sa.JSON(), nullable=False),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("attempts", sa.Integer(), nullable=False),
    sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("last_error", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("claim_id", sa.String(UUID_LENGTH), nullable=True),
    sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("attempts >= 0", name="ck_notifications_attempts_nonnegative"),
    sa.CheckConstraint(
        _allowed_values("state", NOTIFICATION_STATES),
        name="ck_notifications_state_allowed",
    ),
    sa.Index("ix_notifications_queue", "state", "next_attempt_at"),
    sa.Index("ix_notifications_request", "request_id"),
)

recovery_challenges = sa.Table(
    "recovery_challenges",
    metadata,
    sa.Column("id", sa.String(UUID_LENGTH), primary_key=True),
    sa.Column(
        "request_id",
        sa.String(UUID_LENGTH),
        sa.ForeignKey(f"{SCHEMA}.requests.id"),
        nullable=True,
    ),
    sa.Column("lookup_hash", sa.String(HASH_LENGTH), nullable=False),
    sa.Column("code_hash", sa.String(HASH_LENGTH), nullable=False),
    sa.Column("encrypted_code", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("attempts", sa.Integer(), nullable=False),
    sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint(
        "attempts >= 0", name="ck_recovery_challenges_attempts_nonnegative"
    ),
    sa.Index("ix_recovery_challenges_lookup", "lookup_hash", "created_at"),
    sa.Index("ix_recovery_challenges_request", "request_id"),
)

command_receipts = sa.Table(
    "command_receipts",
    metadata,
    sa.Column("command_id", sa.String(UUID_LENGTH), primary_key=True),
    sa.Column("fingerprint", sa.String(HASH_LENGTH), nullable=False),
    sa.Column("result", sa.JSON(), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)

__all__ = [
    "NOTIFICATION_STATES",
    "REQUEST_STATES",
    "SCHEMA",
    "attachments",
    "command_receipts",
    "events",
    "metadata",
    "notifications",
    "recovery_challenges",
    "requests",
    "service_categories",
]
