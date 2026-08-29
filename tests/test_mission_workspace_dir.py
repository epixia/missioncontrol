"""Offline tests for the auto-created per-mission workspace folder — used
whenever a task doesn't specify its own workspace_path. Uses this project's
established in-memory-DB pattern plus pytest's tmp_path (monkeypatched over
_MISSIONS_ROOT so tests never touch the real repo's missions/ folder)."""

from sqlmodel import Session, SQLModel, create_engine

from mission_control.server import app as app_module
from mission_control.server.models import Mission


def _make_engine(monkeypatch, missions_root):
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(app_module, "get_session", lambda: Session(engine))
    monkeypatch.setattr(app_module, "_MISSIONS_ROOT", missions_root)
    return engine


def test_mission_workspace_dir_creates_a_slugified_folder(tmp_path, monkeypatch):
    _make_engine(monkeypatch, tmp_path)
    mission = Mission(name="Tic Tac Toe!")

    workspace = app_module._mission_workspace_dir(mission)

    assert workspace.startswith(str(tmp_path / f"tic-tac-toe-{mission.id[:8]}"))
    from pathlib import Path

    assert Path(workspace).is_dir()


def test_mission_workspace_dir_disambiguates_same_named_missions(tmp_path, monkeypatch):
    _make_engine(monkeypatch, tmp_path)
    mission_a = Mission(name="same name")
    mission_b = Mission(name="same name")

    workspace_a = app_module._mission_workspace_dir(mission_a)
    workspace_b = app_module._mission_workspace_dir(mission_b)

    assert workspace_a != workspace_b


async def test_create_task_defaults_to_mission_workspace_when_omitted(tmp_path, monkeypatch):
    _make_engine(monkeypatch, tmp_path)

    async def fake_execute_task(task_id: str) -> None:
        pass

    monkeypatch.setattr(app_module, "_execute_task", fake_execute_task)

    with app_module.get_session() as session:
        mission = Mission(name="m")
        session.add(mission)
        session.commit()
        session.refresh(mission)
        mission_id = mission.id

    task = await app_module.create_task(
        mission_id, app_module.CreateTaskRequest(runtime="claude_code", prompt="p", workspace_path=None)
    )

    assert task.workspace_path.startswith(str(tmp_path))


async def test_create_task_keeps_explicit_workspace_path(tmp_path, monkeypatch):
    _make_engine(monkeypatch, tmp_path)

    async def fake_execute_task(task_id: str) -> None:
        pass

    monkeypatch.setattr(app_module, "_execute_task", fake_execute_task)

    with app_module.get_session() as session:
        mission = Mission(name="m")
        session.add(mission)
        session.commit()
        session.refresh(mission)
        mission_id = mission.id

    task = await app_module.create_task(
        mission_id,
        app_module.CreateTaskRequest(runtime="claude_code", prompt="p", workspace_path="/some/explicit/path"),
    )

    assert task.workspace_path == "/some/explicit/path"
