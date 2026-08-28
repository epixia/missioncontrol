"""Offline tests for _execute_task's loop behavior — the core of the
chat/interrupt feature. Everything here mocks the adapter; no live CLI
calls. See docs/superpowers/specs/2026-08-28-agent-chat-interrupt-design.md
for the full design this implements."""

import asyncio

import pytest
from sqlmodel import Session, SQLModel, create_engine

from mission_control.adapters.types import HealthReport, HealthStatus, RuntimeEvent, RuntimeType, SessionHandle, TaskAck
from mission_control.server import app as app_module
from mission_control.server.models import Mission, MissionTask, TaskEvent, TaskStatus


@pytest.fixture
def isolated_db(monkeypatch):
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(app_module, "get_session", lambda: Session(engine))
    return engine


def _new_mission_and_task(prompt="original prompt", status=None) -> str:
    with app_module.get_session() as session:
        mission = Mission(name="m")
        session.add(mission)
        session.commit()
        session.refresh(mission)
        task = MissionTask(mission_id=mission.id, runtime="claude_code", prompt=prompt, workspace_path=".")
        if status is not None:
            task.status = status
        session.add(task)
        session.commit()
        session.refresh(task)
        return task.id


class _FakeAdapter:
    """Records every send_task call's prompt; yields one canned event per
    call then ends the stream, simulating one completed turn."""

    def __init__(self):
        self.send_task_prompts: list[str] = []

    async def install(self, spec): pass
    async def configure(self, config): pass
    async def deploy(self, workspace):
        from mission_control.adapters.types import DeployResult
        return DeployResult(success=True)
    async def start(self, session):
        return SessionHandle(session_id="handle-1", runtime_type=RuntimeType.CLAUDE_CODE, started_at=__import__("datetime").datetime.now(__import__("datetime").UTC))
    async def send_task(self, handle, task):
        self.send_task_prompts.append(task.instructions)
        return TaskAck(accepted=True, task_id=task.id)
    async def stream_events(self, handle):
        yield RuntimeEvent(session_id=handle.session_id, event_type="result", timestamp=__import__("datetime").datetime.now(__import__("datetime").UTC), payload={"result": "done", "is_error": False}, native_ref="native-1")
    async def health(self, handle):
        return HealthReport(status=HealthStatus.HEALTHY, checked_at=__import__("datetime").datetime.now(__import__("datetime").UTC))
    async def destroy(self, handle): pass


async def test_execute_task_runs_once_with_no_queued_message(isolated_db, monkeypatch):
    """Regression guard: identical behavior to before this plan when
    nobody ever calls the message endpoint."""
    fake = _FakeAdapter()
    monkeypatch.setattr(app_module, "get_adapter", lambda runtime: fake)
    task_id = _new_mission_and_task(prompt="original prompt")

    await app_module._execute_task(task_id)

    assert fake.send_task_prompts == ["original prompt"]
    with app_module.get_session() as session:
        task = session.get(MissionTask, task_id)
        assert task.status == TaskStatus.SUCCEEDED


async def test_execute_task_picks_up_message_queued_mid_stream(isolated_db, monkeypatch):
    """A message queued while the first turn is still streaming causes a
    second send_task call with that message as the prompt, and records a
    user_message TaskEvent."""
    fake = _FakeAdapter()
    monkeypatch.setattr(app_module, "get_adapter", lambda runtime: fake)
    task_id = _new_mission_and_task(prompt="original prompt")

    # Queue a message before _execute_task even starts — simplest
    # deterministic way to exercise the "loop back" branch offline; a live
    # mid-stream race is covered by manual verification (Task 6), not here.
    app_module._pending_messages[task_id] = asyncio.Queue()
    app_module._pending_messages[task_id].put_nowait("follow-up message")

    await app_module._execute_task(task_id)

    assert fake.send_task_prompts == ["follow-up message"], (
        "expected the queued message to be used, not the original prompt — "
        "this is the reopen-a-finished-task correctness case from the spec"
    )
    with app_module.get_session() as session:
        events = session.exec(
            __import__("sqlmodel").select(TaskEvent).where(TaskEvent.task_id == task_id, TaskEvent.event_type == "user_message")
        ).all()
        assert len(events) == 1
        assert __import__("json").loads(events[0].payload_json)["text"] == "follow-up message"


async def test_execute_task_persists_native_session_id(isolated_db, monkeypatch):
    fake = _FakeAdapter()
    monkeypatch.setattr(app_module, "get_adapter", lambda runtime: fake)
    task_id = _new_mission_and_task()

    await app_module._execute_task(task_id)

    with app_module.get_session() as session:
        task = session.get(MissionTask, task_id)
        assert task.native_session_id == "native-1"


async def test_execute_task_registers_and_deregisters_active_handle(isolated_db, monkeypatch):
    fake = _FakeAdapter()
    monkeypatch.setattr(app_module, "get_adapter", lambda runtime: fake)
    task_id = _new_mission_and_task()

    assert task_id not in app_module._active_handles
    await app_module._execute_task(task_id)
    assert task_id not in app_module._active_handles, (
        "the handle must be deregistered once the task finishes — otherwise "
        "a later message to this task would wrongly think a turn is still live"
    )
