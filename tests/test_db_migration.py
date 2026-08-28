"""Offline test for db.py's column migration — simulates an existing
pre-migration SQLite file (the shape the real ~/.mission-control database
has today) and confirms the new columns get added without touching any
live data. No server, no adapters, no network."""

from sqlmodel import create_engine


def test_migrate_adds_missing_columns(tmp_path):
    from mission_control.server.db import _migrate_add_columns

    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE missiontask (id TEXT PRIMARY KEY, mission_id TEXT, runtime TEXT, "
            "prompt TEXT, workspace_path TEXT, status TEXT, session_id TEXT, error_detail TEXT, "
            "total_cost_usd REAL, total_input_tokens INTEGER, total_output_tokens INTEGER, "
            "created_at TEXT, updated_at TEXT)"
        )
        conn.commit()

    _migrate_add_columns(engine)

    with engine.connect() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(missiontask)")}
    assert {"role", "pipeline_run_id", "result_text"} <= columns


def test_migrate_is_idempotent(tmp_path):
    from mission_control.server.db import _migrate_add_columns

    db_path = tmp_path / "test2.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        conn.exec_driver_sql("CREATE TABLE missiontask (id TEXT PRIMARY KEY)")
        conn.commit()

    _migrate_add_columns(engine)
    _migrate_add_columns(engine)  # must not raise "duplicate column name"

    with engine.connect() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(missiontask)")}
    assert {"role", "pipeline_run_id", "result_text"} <= columns
