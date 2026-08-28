"""Offline tests for the two synchronous error paths of
POST /api/tasks/{id}/message — 404 and 400. The "actually sends a message"
paths require a live or heavily-mocked adapter session and are covered by
tests/test_chat_interrupt_loop.py (the loop itself) plus manual live
verification (see the spec) — this file only checks routing/validation."""

import asyncio

import pytest
from sqlmodel import Session, SQLModel, create_engine

from mission_control.server import app as app_module
from mission_control.server.models import Mission, MissionTask, TaskStatus


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


async def test_message_to_pending_task_does_not_spawn_duplicate_execution(isolated_db, monkeypatch):
    """A PENDING task already has (or is about to have) an _execute_task
    invocation starting for it, from create_task. Sending it a message must
    not start a second one — that would produce two concurrent agent
    processes racing on the same task row. The message should just be
    queued for the invocation already underway (see _TERMINAL_TASK_STATUSES
    in app.py)."""
    execute_task_calls = []

    async def fake_execute_task(task_id: str) -> None:
        execute_task_calls.append(task_id)

    monkeypatch.setattr(app_module, "_execute_task", fake_execute_task)

    with app_module.get_session() as session:
        mission = Mission(name="m")
        session.add(mission)
        session.commit()
        session.refresh(mission)
        task = MissionTask(mission_id=mission.id, runtime="claude_code", prompt="p", workspace_path=".")
        assert task.status == TaskStatus.PENDING  # the default, and the status under test
        session.add(task)
        session.commit()
        session.refresh(task)
        task_id = task.id

    result = await app_module.send_task_message(task_id, app_module.SendMessageRequest(text="hi"))

    assert result == {"accepted": True}
    # asyncio.create_task only schedules; give any wrongly-spawned background
    # task a turn to actually run before asserting it didn't happen.
    await asyncio.sleep(0)
    assert execute_task_calls == [], (
        "sending a message to a PENDING task must not spawn a second "
        "_execute_task"
    )
    with app_module.get_session() as session:
        task = session.get(MissionTask, task_id)
        assert task.status == TaskStatus.PENDING, "status must be left untouched, not bumped to RUNNING"


def test_terminal_task_statuses_excludes_pending_and_running():
    """Regression guard for the exact invariant Fix 1 restores: PENDING and
    RUNNING must never be treated as terminal, or the message route will
    spawn a duplicate _execute_task for a task that already has one live."""
    assert TaskStatus.PENDING not in app_module._TERMINAL_TASK_STATUSES
    assert TaskStatus.RUNNING not in app_module._TERMINAL_TASK_STATUSES
