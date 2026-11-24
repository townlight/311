"""Explicit schema migration for the Townlight 311 repository."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.schema import CreateSchema
from sqlalchemy.sql import quoted_name

from .models import SCHEMA, metadata

REVISION = 1
VERSION_TABLE = "schema_versions"

_version_metadata = sa.MetaData(schema=SCHEMA)
schema_versions = sa.Table(
    VERSION_TABLE,
    _version_metadata,
    sa.Column("revision", sa.Integer(), primary_key=True),
)

_REQUIRED_COLUMNS = {
    table.name: tuple(column.name for column in table.columns)
    for table in metadata.tables.values()
}


def _engine_for_dialect(engine: sa.Engine) -> sa.Engine:
    if engine.dialect.name == "sqlite":
        return engine.execution_options(schema_translate_map={SCHEMA: None})
    return engine


def _schema_name(engine: sa.Engine) -> str | None:
    return None if engine.dialect.name == "sqlite" else SCHEMA


def _current_revisions(conn: sa.Connection) -> set[int] | None:
    inspector = sa.inspect(conn)
    schema = _schema_name(conn.engine)
    if VERSION_TABLE not in inspector.get_table_names(schema=schema):
        return None
    if "revision" not in {
        column["name"]
        for column in inspector.get_columns(VERSION_TABLE, schema=schema)
    }:
        raise RuntimeError("schema_versions is missing its revision column")
    return {
        int(revision)
        for revision in conn.execute(sa.select(schema_versions.c.revision)).scalars()
    }


def upgrade(engine: sa.Engine) -> None:
    """Create revision 1 of the Townlight 311 schema.

    The schema and all table DDL run in one transaction. Existing data is never
    dropped or renamed. Calling this function again after a successful upgrade
    is a no-op.
    """
    work_engine = _engine_for_dialect(engine)

    with work_engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(
                CreateSchema(quoted_name(SCHEMA, True), if_not_exists=True)
            )

        revisions = _current_revisions(conn)
        if revisions is not None and any(revision > REVISION for revision in revisions):
            raise RuntimeError("database schema is newer than supported revision 1")

        metadata.create_all(conn)
        schema_versions.create(conn, checkfirst=True)

        revisions = _current_revisions(conn)
        if revisions is None:
            raise RuntimeError("schema_versions was not created")
        if revisions == {REVISION}:
            return
        if any(revision > REVISION for revision in revisions):
            raise RuntimeError("database schema is newer than supported revision 1")
        if not conn.execute(
            sa.select(schema_versions.c.revision).where(
                schema_versions.c.revision == REVISION
            )
        ).first():
            conn.execute(sa.insert(schema_versions).values(revision=REVISION))


def ready(engine: sa.Engine) -> bool:
    """Return whether revision 1 and every required table/column are present.

    This function only inspects the database. It never creates schemas, tables,
    columns, or rows.
    """
    work_engine = _engine_for_dialect(engine)
    schema = _schema_name(engine)

    try:
        with work_engine.connect() as conn:
            inspector = sa.inspect(conn)
            table_names = set(inspector.get_table_names(schema=schema))

            for table_name, required_columns in _REQUIRED_COLUMNS.items():
                if table_name not in table_names:
                    return False
                actual_columns = {
                    column["name"]
                    for column in inspector.get_columns(table_name, schema=schema)
                }
                if not set(required_columns).issubset(actual_columns):
                    return False

            if VERSION_TABLE not in table_names:
                return False
            version_columns = {
                column["name"]
                for column in inspector.get_columns(VERSION_TABLE, schema=schema)
            }
            if "revision" not in version_columns:
                return False

            revisions = set(
                conn.execute(sa.select(schema_versions.c.revision)).scalars()
            )
            return revisions == {REVISION}
    except sa.exc.SQLAlchemyError:
        return False


__all__ = ["REVISION", "VERSION_TABLE", "ready", "upgrade"]
