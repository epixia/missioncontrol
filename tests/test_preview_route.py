"""Offline tests for GET /api/missions/{id}/preview/{file_path} — serves a
project's own index.html and assets out of the workspace directory of
whichever task in the mission was created most recently. Uses this
project's established in-memory-DB pattern plus pytest's tmp_path for real
files on disk (the only route in this app that needs both)."""

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session, SQLModel, create_engine

from mission_control.server import app as app_module
from mission_control.server.models import Mission, MissionTask


def _make_engine(monkeypatch):
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(app_module, "get_session", lambda: Session(engine))
    return engine


def _make_mission_with_task(monkeypatch, workspace_path: str) -> str:
    _make_engine(monkeypatch)
    with app_module.get_session() as session:
        mission = Mission(name="m")
        session.add(mission)
        session.commit()
        session.refresh(mission)
        task = MissionTask(mission_id=mission.id, runtime="claude_code", prompt="p", workspace_path=workspace_path)
        session.add(task)
        session.commit()
        return mission.id


def test_preview_serves_index_html_at_bare_path(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<h1>hi</h1>")
    mission_id = _make_mission_with_task(monkeypatch, str(tmp_path))

    response = app_module.preview_file(mission_id, "")

    assert isinstance(response, FileResponse)
    assert response.path == str(tmp_path / "index.html")


def test_preview_serves_a_nested_asset(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<h1>hi</h1>")
    (tmp_path / "style.css").write_text("body { color: red; }")
    mission_id = _make_mission_with_task(monkeypatch, str(tmp_path))

    response = app_module.preview_file(mission_id, "style.css")

    assert response.path == str(tmp_path / "style.css")


def test_preview_404_on_path_traversal_outside_workspace(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<h1>hi</h1>")
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("do not serve me")
    mission_id = _make_mission_with_task(monkeypatch, str(tmp_path))

    with pytest.raises(HTTPException) as excinfo:
        app_module.preview_file(mission_id, f"../{secret.name}")
    assert excinfo.value.status_code == 404


def test_preview_404_when_no_index_html(tmp_path, monkeypatch):
    mission_id = _make_mission_with_task(monkeypatch, str(tmp_path))

    with pytest.raises(HTTPException) as excinfo:
        app_module.preview_file(mission_id, "")
    assert excinfo.value.status_code == 404


def test_preview_404_on_unknown_mission(monkeypatch):
    _make_engine(monkeypatch)
    with pytest.raises(HTTPException) as excinfo:
        app_module.preview_file("does-not-exist", "")
    assert excinfo.value.status_code == 404


def test_preview_404_when_mission_has_no_tasks(monkeypatch):
    _make_engine(monkeypatch)
    with app_module.get_session() as session:
        mission = Mission(name="m")
        session.add(mission)
        session.commit()
        session.refresh(mission)
        mission_id = mission.id

    with pytest.raises(HTTPException) as excinfo:
        app_module.preview_file(mission_id, "")
    assert excinfo.value.status_code == 404


def test_preview_uses_most_recently_created_tasks_workspace(tmp_path, monkeypatch):
    older_dir = tmp_path / "older"
    older_dir.mkdir()
    (older_dir / "index.html").write_text("old")
    newer_dir = tmp_path / "newer"
    newer_dir.mkdir()
    (newer_dir / "index.html").write_text("new")

    _make_engine(monkeypatch)
    with app_module.get_session() as session:
        mission = Mission(name="m")
        session.add(mission)
        session.commit()
        session.refresh(mission)
        older_task = MissionTask(
            mission_id=mission.id, runtime="claude_code", prompt="p", workspace_path=str(older_dir)
        )
        session.add(older_task)
        session.commit()
        mission_id = mission.id

    with app_module.get_session() as session:
        newer_task = MissionTask(
            mission_id=mission_id, runtime="claude_code", prompt="p", workspace_path=str(newer_dir)
        )
        session.add(newer_task)
        session.commit()

    response = app_module.preview_file(mission_id, "")
    assert response.path == str(newer_dir / "index.html")
