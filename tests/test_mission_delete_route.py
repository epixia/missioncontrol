"""Offline tests for DELETE /api/missions/{id} — cascades to the mission's
tasks, task events, and kanban tickets/comments, refuses while a task is
running, 404 on unknown."""

import pytest
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine, select

from mission_control.server import app as app_module
from mission_control.server.models import Mission, MissionTask, TaskEvent, TaskStatus, Ticket, TicketComment


def _make_engine(monkeypatch):
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(app_module, "get_session", lambda: Session(engine))
    return engine


def test_delete_mission_removes_mission_tasks_and_events(monkeypatch):
    engine = _make_engine(monkeypatch)
    with app_module.get_session() as session:
        mission = Mission(name="m")
        session.add(mission)
        session.commit()
        session.refresh(mission)
        task = MissionTask(mission_id=mission.id, runtime="claude_code", prompt="p", workspace_path=".")
        session.add(task)
        session.commit()
        session.refresh(task)
        session.add(TaskEvent(task_id=task.id, event_type="status_changed", payload_json="{}"))
        session.commit()
        mission_id, task_id = mission.id, task.id

    result = app_module.delete_mission(mission_id)

    assert result == {"deleted": True}
    with Session(engine) as session:
        assert session.get(Mission, mission_id) is None
        assert session.get(MissionTask, task_id) is None
        assert session.exec(select(TaskEvent).where(TaskEvent.task_id == task_id)).all() == []


def test_delete_mission_removes_its_tickets_and_their_comments(monkeypatch):
    """Tickets are mission-scoped, so deleting a mission must take them with
    it — otherwise agent-authored ticket rows outlive the mission forever
    (SQLite's foreign keys are off by default, so nothing else catches it)."""
    engine = _make_engine(monkeypatch)
    with app_module.get_session() as session:
        mission = Mission(name="m")
        session.add(mission)
        session.commit()
        session.refresh(mission)
        mission_id = mission.id
    ticket = app_module.create_ticket(mission_id, app_module.CreateTicketRequest(title="t"))
    app_module.add_comment(ticket.id, app_module.CreateCommentRequest(text="c"))

    app_module.delete_mission(mission_id)

    with Session(engine) as session:
        assert session.exec(select(Ticket).where(Ticket.mission_id == mission_id)).all() == []
        assert session.exec(select(TicketComment).where(TicketComment.ticket_id == ticket.id)).all() == []


def test_delete_mission_leaves_another_missions_tickets_alone(monkeypatch):
    engine = _make_engine(monkeypatch)
    with app_module.get_session() as session:
        doomed, keeper = Mission(name="doomed"), Mission(name="keeper")
        session.add(doomed)
        session.add(keeper)
        session.commit()
        session.refresh(doomed)
        session.refresh(keeper)
        doomed_id, keeper_id = doomed.id, keeper.id
    app_module.create_ticket(doomed_id, app_module.CreateTicketRequest(title="goes away"))
    survivor = app_module.create_ticket(keeper_id, app_module.CreateTicketRequest(title="stays"))
    app_module.add_comment(survivor.id, app_module.CreateCommentRequest(text="still here"))

    app_module.delete_mission(doomed_id)

    with Session(engine) as session:
        assert session.get(Ticket, survivor.id) is not None
        assert session.exec(select(TicketComment).where(TicketComment.ticket_id == survivor.id)).all() != []


def test_delete_mission_404_on_unknown_mission(monkeypatch):
    _make_engine(monkeypatch)
    with pytest.raises(HTTPException) as excinfo:
        app_module.delete_mission("does-not-exist")
    assert excinfo.value.status_code == 404


def test_delete_mission_409_when_task_running(monkeypatch):
    engine = _make_engine(monkeypatch)
    with app_module.get_session() as session:
        mission = Mission(name="m")
        session.add(mission)
        session.commit()
        session.refresh(mission)
        task = MissionTask(
            mission_id=mission.id, runtime="claude_code", prompt="p", workspace_path=".", status=TaskStatus.RUNNING
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        mission_id, task_id = mission.id, task.id

    with pytest.raises(HTTPException) as excinfo:
        app_module.delete_mission(mission_id)
    assert excinfo.value.status_code == 409

    with Session(engine) as session:
        assert session.get(Mission, mission_id) is not None, "nothing should be deleted when blocked"
        assert session.get(MissionTask, task_id) is not None


import pytest  # noqa: E402  (placed after fixtures for readability; pytest itself needed above)
