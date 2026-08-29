"""Offline tests for the ticket (kanban) API, using this project's
established in-memory-DB pattern — see tests/test_task_message_route.py
for the same fixture shape."""

import pytest
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine

from mission_control.server import app as app_module
from mission_control.server.models import Mission, TicketColumn


def _make_engine(monkeypatch):
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(app_module, "get_session", lambda: Session(engine))
    return engine


def _make_mission(monkeypatch) -> str:
    _make_engine(monkeypatch)
    with app_module.get_session() as session:
        mission = Mission(name="m")
        session.add(mission)
        session.commit()
        session.refresh(mission)
        return mission.id


def test_create_ticket_defaults_to_backlog_and_position_zero(monkeypatch):
    mission_id = _make_mission(monkeypatch)
    ticket = app_module.create_ticket(mission_id, app_module.CreateTicketRequest(title="Break down the goal"))
    assert ticket.column == TicketColumn.BACKLOG
    assert ticket.position == 0
    assert ticket.created_by_role is None


def test_create_ticket_404_on_unknown_mission(monkeypatch):
    _make_engine(monkeypatch)
    with pytest.raises(HTTPException) as excinfo:
        app_module.create_ticket("does-not-exist", app_module.CreateTicketRequest(title="x"))
    assert excinfo.value.status_code == 404


def test_second_ticket_in_same_column_gets_next_position(monkeypatch):
    mission_id = _make_mission(monkeypatch)
    app_module.create_ticket(mission_id, app_module.CreateTicketRequest(title="first"))
    second = app_module.create_ticket(mission_id, app_module.CreateTicketRequest(title="second"))
    assert second.position == 1


def test_create_ticket_records_author_role(monkeypatch):
    mission_id = _make_mission(monkeypatch)
    ticket = app_module.create_ticket(mission_id, app_module.CreateTicketRequest(title="x"), author_role="coder")
    assert ticket.created_by_role == "coder"


def test_list_tickets_orders_by_column_then_position(monkeypatch):
    mission_id = _make_mission(monkeypatch)
    app_module.create_ticket(mission_id, app_module.CreateTicketRequest(title="doing-1", column=TicketColumn.DOING))
    app_module.create_ticket(mission_id, app_module.CreateTicketRequest(title="backlog-1"))
    app_module.create_ticket(mission_id, app_module.CreateTicketRequest(title="backlog-2"))
    tickets = app_module.list_tickets(mission_id)
    assert [t.title for t in tickets] == ["backlog-1", "backlog-2", "doing-1"]


def test_update_ticket_partial_update_leaves_other_fields(monkeypatch):
    mission_id = _make_mission(monkeypatch)
    ticket = app_module.create_ticket(mission_id, app_module.CreateTicketRequest(title="x", description="orig"))
    updated = app_module.update_ticket(ticket.id, app_module.UpdateTicketRequest(column=TicketColumn.DOING))
    assert updated.column == TicketColumn.DOING
    assert updated.title == "x"
    assert updated.description == "orig"


def test_update_ticket_moving_column_appends_at_end(monkeypatch):
    mission_id = _make_mission(monkeypatch)
    app_module.create_ticket(
        mission_id, app_module.CreateTicketRequest(title="already-doing", column=TicketColumn.DOING)
    )
    ticket = app_module.create_ticket(mission_id, app_module.CreateTicketRequest(title="moving-in"))
    moved = app_module.update_ticket(ticket.id, app_module.UpdateTicketRequest(column=TicketColumn.DOING))
    assert moved.position == 1


def test_update_ticket_404_on_unknown_ticket(monkeypatch):
    _make_engine(monkeypatch)
    with pytest.raises(HTTPException) as excinfo:
        app_module.update_ticket("does-not-exist", app_module.UpdateTicketRequest(title="x"))
    assert excinfo.value.status_code == 404


def test_add_comment_and_get_ticket_includes_it(monkeypatch):
    mission_id = _make_mission(monkeypatch)
    ticket = app_module.create_ticket(mission_id, app_module.CreateTicketRequest(title="x"))
    app_module.add_comment(ticket.id, app_module.CreateCommentRequest(text="looks good"), author_role="reviewer")
    detail = app_module.get_ticket(ticket.id)
    assert len(detail["comments"]) == 1
    assert detail["comments"][0]["text"] == "looks good"
    assert detail["comments"][0]["author_role"] == "reviewer"


def test_add_comment_404_on_unknown_ticket(monkeypatch):
    _make_engine(monkeypatch)
    with pytest.raises(HTTPException) as excinfo:
        app_module.add_comment("does-not-exist", app_module.CreateCommentRequest(text="x"))
    assert excinfo.value.status_code == 404


def test_get_ticket_404_on_unknown_ticket(monkeypatch):
    _make_engine(monkeypatch)
    with pytest.raises(HTTPException) as excinfo:
        app_module.get_ticket("does-not-exist")
    assert excinfo.value.status_code == 404
