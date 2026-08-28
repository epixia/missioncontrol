"""Offline tests for the two synchronous error paths of
POST /api/tasks/{id}/message — 404 and 400. The "actually sends a message"
paths require a live or heavily-mocked adapter session and are covered by
tests/test_chat_interrupt_loop.py (the loop itself) plus manual live
verification (see the spec) — this file only checks routing/validation."""

import pytest
from sqlmodel import Session, SQLModel, create_engine

from mission_control.server import app as app_module
from mission_control.server.models import Mission, MissionTask


@pytest.fixture
def isolated_db(monkeypatch):
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(app_module, "get_session", lambda: Session(engine))
    return engine


async def test_send_message_404_on_unknown_task(isolated_db):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        await app_module.send_task_message("does-not-exist", app_module.SendMessageRequest(text="hi"))
    assert excinfo.value.status_code == 404


async def test_send_message_400_on_non_claude_code_task(isolated_db):
    from fastapi import HTTPException

    with app_module.get_session() as session:
        mission = Mission(name="m")
        session.add(mission)
        session.commit()
        session.refresh(mission)
        task = MissionTask(mission_id=mission.id, runtime="codex", prompt="p", workspace_path=".")
        session.add(task)
        session.commit()
        session.refresh(task)
        task_id = task.id

    with pytest.raises(HTTPException) as excinfo:
        await app_module.send_task_message(task_id, app_module.SendMessageRequest(text="hi"))
    assert excinfo.value.status_code == 400
