"""Offline test for _finish()'s result_text persistence. Everything else
in app.py's _execute_task requires a live adapter/API call and is verified
manually per docs/superpowers/specs/2026-08-27-agent-pipeline-design.md.

get_session is a plain module-level name imported into app.py's namespace
(`from mission_control.server.db import get_session, ...`), so monkeypatching
mission_control.server.app.get_session replaces exactly what _finish() calls."""

import pytest
from sqlmodel import Session, SQLModel, create_engine

from mission_control.server import app as app_module
from mission_control.server.models import Mission, MissionTask, TaskStatus


@pytest.fixture
def isolated_db(monkeypatch):
    engine = create_engine("sqlite://")  # fresh in-memory DB per test
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(app_module, "get_session", lambda: Session(engine))
    return engine


def test_finish_persists_result_text(isolated_db):
    with app_module.get_session() as session:
        mission = Mission(name="m")
        session.add(mission)
        session.commit()
        session.refresh(mission)
        task = MissionTask(mission_id=mission.id, runtime="claude_code", prompt="p", workspace_path=".")
        session.add(task)
        session.commit()
        session.refresh(task)
        task_id = task.id

    app_module._finish(task_id, TaskStatus.SUCCEEDED, error_detail=None, result_text="OK")

    with app_module.get_session() as session:
        task = session.get(MissionTask, task_id)
        assert task.result_text == "OK"
        assert task.status == TaskStatus.SUCCEEDED


def test_finish_without_result_text_leaves_it_none(isolated_db):
    with app_module.get_session() as session:
        mission = Mission(name="m")
        session.add(mission)
        session.commit()
        session.refresh(mission)
        task = MissionTask(mission_id=mission.id, runtime="claude_code", prompt="p", workspace_path=".")
        session.add(task)
        session.commit()
        session.refresh(task)
        task_id = task.id

    app_module._finish(task_id, TaskStatus.FAILED, error_detail="boom")

    with app_module.get_session() as session:
        task = session.get(MissionTask, task_id)
        assert task.result_text is None
        assert task.error_detail == "boom"
