from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

_DB_PATH = Path.home() / ".mission-control" / "mission-control.db"

_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(f"sqlite:///{_DB_PATH}", connect_args={"check_same_thread": False})

_NEW_COLUMNS = (("role", "TEXT"), ("pipeline_run_id", "TEXT"), ("result_text", "TEXT"), ("native_session_id", "TEXT"))


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _migrate_add_columns(engine)


def _migrate_add_columns(target_engine) -> None:
    """Idempotently add columns that create_all() won't add to an existing
    table. Safe to call on every startup, and safe to call twice."""
    with target_engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(missiontask)")}
        for column, coltype in _NEW_COLUMNS:
            if column not in existing:
                conn.exec_driver_sql(f"ALTER TABLE missiontask ADD COLUMN {column} {coltype}")
        conn.commit()


def get_session() -> Session:
    return Session(engine)
