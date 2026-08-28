"""Offline tests for pipeline task creation and the run-status aggregation
in list_pipelines — an isolated in-memory DB, no live adapter/API calls.
The actual live 3-stage execution is verified manually (see the spec)."""

import unittest.mock

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from mission_control.server import app as app_module
from mission_control.server.models import AgentRole, Mission, MissionTask, TaskStatus


@pytest.fixture
def isolated_db(monkeypatch):
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(app_module, "get_session", lambda: Session(engine))
    return engine


def _new_mission(name="m") -> str:
    with app_module.get_session() as session:
        mission = Mission(name=name)
        session.add(mission)
        session.commit()
        session.refresh(mission)
        return mission.id


def test_create_pipeline_task_sets_role_and_pipeline_run_id(isolated_db):
    mission_id = _new_mission()
    task_id = app_module._create_pipeline_task(mission_id, "run-1", AgentRole.ORCHESTRATOR, "prompt text", ".")
    with app_module.get_session() as session:
        task = session.get(MissionTask, task_id)
        assert task.role == AgentRole.ORCHESTRATOR
        assert task.pipeline_run_id == "run-1"
        assert task.runtime == "claude_code"
        assert task.status == TaskStatus.PENDING


def test_list_pipelines_groups_and_orders_by_role(isolated_db):
    mission_id = _new_mission()
    with app_module.get_session() as session:
        # inserted out of role order on purpose, to prove list_pipelines sorts them
        session.add(MissionTask(mission_id=mission_id, runtime="claude_code", prompt="p", workspace_path=".",
                                 role=AgentRole.REVIEWER, pipeline_run_id="run-2", status=TaskStatus.SUCCEEDED))
        session.add(MissionTask(mission_id=mission_id, runtime="claude_code", prompt="p", workspace_path=".",
                                 role=AgentRole.ORCHESTRATOR, pipeline_run_id="run-2", status=TaskStatus.SUCCEEDED))
        session.add(MissionTask(mission_id=mission_id, runtime="claude_code", prompt="p", workspace_path=".",
                                 role=AgentRole.CODER, pipeline_run_id="run-2", status=TaskStatus.SUCCEEDED))
        session.commit()

    runs = app_module.list_pipelines(mission_id)

    assert len(runs) == 1
    assert runs[0]["status"] == "succeeded"
    assert [t.role for t in runs[0]["tasks"]] == [AgentRole.ORCHESTRATOR, AgentRole.CODER, AgentRole.REVIEWER]


def test_list_pipelines_reports_failed_when_any_stage_failed(isolated_db):
    mission_id = _new_mission()
    with app_module.get_session() as session:
        session.add(MissionTask(mission_id=mission_id, runtime="claude_code", prompt="p", workspace_path=".",
                                 role=AgentRole.ORCHESTRATOR, pipeline_run_id="run-3", status=TaskStatus.FAILED))
        session.commit()

    runs = app_module.list_pipelines(mission_id)

    assert runs[0]["status"] == "failed"
    assert len(runs[0]["tasks"]) == 1  # coder/reviewer were never created


def test_list_pipelines_ignores_ad_hoc_tasks_without_pipeline_run_id(isolated_db):
    mission_id = _new_mission()
    with app_module.get_session() as session:
        session.add(MissionTask(mission_id=mission_id, runtime="claude_code", prompt="p", workspace_path="."))
        session.commit()

    runs = app_module.list_pipelines(mission_id)

    assert runs == []


async def test_execute_pipeline_halts_when_orchestrator_fails(isolated_db, monkeypatch):
    """Verify that _execute_pipeline stops and does not create coder/reviewer tasks
    when the orchestrator fails (no result_text)."""
    mission_id = _new_mission()
    pipeline_run_id = "run-halt-test"

    # Create orchestrator task
    orchestrator_task_id = app_module._create_pipeline_task(
        mission_id, pipeline_run_id, AgentRole.ORCHESTRATOR, "goal", "."
    )

    # Mock _execute_task to fail the orchestrator (set status=FAILED, no result_text)
    async def mock_execute_task(task_id: str) -> None:
        with app_module.get_session() as session:
            task = session.get(MissionTask, task_id)
            task.status = TaskStatus.FAILED
            task.result_text = None
            session.add(task)
            session.commit()

    monkeypatch.setattr(app_module, "_execute_task", mock_execute_task)

    # Execute pipeline - should halt after orchestrator
    await app_module._execute_pipeline(pipeline_run_id, mission_id, "goal", ".", orchestrator_task_id)

    # Verify no coder or reviewer tasks were created
    with app_module.get_session() as session:
        tasks = session.exec(
            select(MissionTask)
            .where(MissionTask.pipeline_run_id == pipeline_run_id)
        ).all()
        roles = [t.role for t in tasks]

    assert roles == [AgentRole.ORCHESTRATOR], f"Expected only ORCHESTRATOR, got {roles}"
    assert tasks[0].status == TaskStatus.FAILED
