"""Offline tests for GET /api/missions/{id}/log — status_changed and
user_message events, each tagged with event_type so the dashboard's new
Activity feed can tell them apart from the existing Progress Log."""

from sqlmodel import Session, SQLModel, create_engine

from mission_control.server import app as app_module
from mission_control.server.models import AgentRole, Mission, MissionTask, TaskEvent


def _make_engine(monkeypatch):
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(app_module, "get_session", lambda: Session(engine))
    return engine


def test_mission_log_includes_status_changed_and_user_message_events(monkeypatch):
    _make_engine(monkeypatch)
    with app_module.get_session() as session:
        mission = Mission(name="m")
        session.add(mission)
        session.commit()
        session.refresh(mission)
        task = MissionTask(
            mission_id=mission.id, runtime="claude_code", prompt="p", workspace_path=".", role=AgentRole.CODER
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        session.add(
            TaskEvent(task_id=task.id, event_type="status_changed", payload_json='{"status": "succeeded"}')
        )
        session.add(TaskEvent(task_id=task.id, event_type="user_message", payload_json='{"text": "hello"}'))
        session.add(TaskEvent(task_id=task.id, event_type="assistant", payload_json="{}"))
        session.commit()
        mission_id = mission.id

    log = app_module.mission_log(mission_id)

    event_types = {e["event_type"] for e in log}
    assert event_types == {"status_changed", "user_message"}, "assistant events must not leak into this log"
    user_message_entry = next(e for e in log if e["event_type"] == "user_message")
    assert user_message_entry["text"] == "hello"
    assert user_message_entry["role"] == "coder"
