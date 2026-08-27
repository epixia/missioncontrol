"""Mission Control's v0 platform: a local API + dashboard in front of the
RuntimeAdapter layer. This is the first piece of the "core owned subsystems"
from the architecture doc (§2) — mission/task persistence and observability —
though budgets, approvals, and a real scheduler are still not built (§13).
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlmodel import select

from mission_control.adapters.types import (
    ErrorFamily,
    HealthStatus,
    RuntimeAdapterError,
    RuntimeConfig,
    RuntimeType,
    SessionRequest,
    Task,
    Workspace,
)
from mission_control.server.db import get_session, init_db
from mission_control.server.models import Mission, MissionTask, TaskEvent, TaskStatus
from mission_control.server.runtime_registry import get_adapter, load_pinned_spec

_STATIC_DIR = Path(__file__).parent / "static"
_RUNTIME_STATE_ROOT = Path.home() / ".mission-control" / "runtimes"


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(title="Mission Control", lifespan=_lifespan)


class CreateMissionRequest(BaseModel):
    name: str


class CreateTaskRequest(BaseModel):
    runtime: RuntimeType
    prompt: str
    workspace_path: str


@app.get("/")
def dashboard() -> FileResponse:
    return FileResponse(_STATIC_DIR / "dashboard.html")


_RUNTIME_BINARIES = {
    RuntimeType.HERMES: "hermes",
    RuntimeType.OPENCLAW: "openclaw",
    RuntimeType.CLAUDE_CODE: "claude",
    RuntimeType.CODEX: "codex",
}


@app.get("/api/health")
def platform_health() -> dict:
    return {
        "status": "ok",
        "runtimes": {r.value: shutil.which(binary) is not None for r, binary in _RUNTIME_BINARIES.items()},
    }


@app.post("/api/missions", response_model=Mission)
def create_mission(req: CreateMissionRequest) -> Mission:
    with get_session() as session:
        mission = Mission(name=req.name)
        session.add(mission)
        session.commit()
        session.refresh(mission)
        return mission


@app.get("/api/missions")
def list_missions() -> list[dict]:
    with get_session() as session:
        missions = session.exec(select(Mission).order_by(Mission.created_at.desc())).all()
        out = []
        for mission in missions:
            tasks = session.exec(
                select(MissionTask).where(MissionTask.mission_id == mission.id).order_by(MissionTask.created_at)
            ).all()
            out.append({"mission": mission, "tasks": tasks})
        return out


@app.post("/api/missions/{mission_id}/tasks", response_model=MissionTask)
async def create_task(mission_id: str, req: CreateTaskRequest) -> MissionTask:
    with get_session() as session:
        mission = session.get(Mission, mission_id)
        if mission is None:
            raise HTTPException(404, "mission not found")
        task = MissionTask(
            mission_id=mission_id,
            runtime=req.runtime.value,
            prompt=req.prompt,
            workspace_path=req.workspace_path,
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        task_id = task.id
        session.add(TaskEvent(task_id=task_id, event_type="status_changed", payload_json=json.dumps({"status": "pending"})))
        session.commit()
        session.refresh(task)  # re-populate attributes expired by the commit above, before the session closes

    asyncio.create_task(_execute_task(task_id))
    return task


@app.get("/api/tasks/{task_id}", response_model=MissionTask)
def get_task(task_id: str) -> MissionTask:
    with get_session() as session:
        task = session.get(MissionTask, task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        return task


@app.get("/api/tasks/{task_id}/events")
async def stream_task_events(task_id: str) -> StreamingResponse:
    return StreamingResponse(_sse_generator(task_id), media_type="text/event-stream")


async def _sse_generator(task_id: str) -> AsyncIterator[str]:
    last_id = 0
    while True:
        with get_session() as session:
            task = session.get(MissionTask, task_id)
            if task is None:
                yield "event: error\ndata: task not found\n\n"
                return
            new_events = session.exec(
                select(TaskEvent).where(TaskEvent.task_id == task_id, TaskEvent.id > last_id).order_by(TaskEvent.id)
            ).all()
            for event in new_events:
                last_id = event.id
                yield f"data: {json.dumps({'event_type': event.event_type, 'payload': json.loads(event.payload_json)})}\n\n"
            terminal = task.status in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.UNSUPPORTED)
        if terminal and not new_events:
            yield f"event: done\ndata: {task.status.value}\n\n"
            return
        await asyncio.sleep(0.3)


async def _execute_task(task_id: str) -> None:
    with get_session() as session:
        task = session.get(MissionTask, task_id)
        runtime = RuntimeType(task.runtime)

    adapter = get_adapter(runtime)
    spec = load_pinned_spec(runtime)

    try:
        await adapter.install(spec)
    except RuntimeAdapterError as exc:
        if exc.family is ErrorFamily.NOT_FOUND:
            _finish(task_id, TaskStatus.FAILED, error_detail=exc.message)
            return
        # Non-fatal version mismatch (see architecture doc §12) — proceed.

    state_dir = _RUNTIME_STATE_ROOT / runtime.value
    await adapter.configure(RuntimeConfig(runtime_type=runtime, state_dir=state_dir))

    with get_session() as session:
        task = session.get(MissionTask, task_id)
        workspace = Workspace(path=Path(task.workspace_path))

    deploy = await adapter.deploy(workspace)
    if not deploy.success:
        _finish(task_id, TaskStatus.FAILED, error_detail=deploy.message)
        return

    with get_session() as session:
        task = session.get(MissionTask, task_id)
        handle = await adapter.start(SessionRequest(mission_id=task.mission_id, task_id=task_id, workspace=workspace))
        task.session_id = handle.session_id
        task.status = TaskStatus.RUNNING
        session.add(task)
        session.add(TaskEvent(task_id=task_id, event_type="status_changed", payload_json=json.dumps({"status": "running"})))
        session.commit()
        prompt = task.prompt

    try:
        ack = await adapter.send_task(handle, Task(id=task_id, mission_id=task.mission_id, instructions=prompt))
    except NotImplementedError as exc:
        _finish(task_id, TaskStatus.UNSUPPORTED, error_detail=str(exc))
        await adapter.destroy(handle)
        return

    if not ack.accepted:
        _finish(task_id, TaskStatus.FAILED, error_detail=ack.reason or "task rejected")
        await adapter.destroy(handle)
        return

    saw_error = False
    async for event in adapter.stream_events(handle):
        if event.error_family is not None:
            saw_error = True
        with get_session() as session:
            session.add(
                TaskEvent(
                    task_id=task_id,
                    event_type=event.event_type,
                    payload_json=json.dumps(event.payload),
                    error_family=event.error_family.value if event.error_family else None,
                )
            )
            task = session.get(MissionTask, task_id)
            if event.cost is not None:
                task.total_cost_usd += event.cost.cost_usd
                task.total_input_tokens += event.cost.input_tokens
                task.total_output_tokens += event.cost.output_tokens
                session.add(task)
            session.commit()

    health = await adapter.health(handle)
    final_status = TaskStatus.SUCCEEDED if health.status == HealthStatus.HEALTHY and not saw_error else TaskStatus.FAILED
    _finish(task_id, final_status, error_detail=None if final_status == TaskStatus.SUCCEEDED else health.detail)
    await adapter.destroy(handle)


def _finish(task_id: str, status: TaskStatus, error_detail: str | None) -> None:
    with get_session() as session:
        task = session.get(MissionTask, task_id)
        if task is None:
            return
        task.status = status
        task.error_detail = error_detail
        session.add(task)
        session.add(
            TaskEvent(
                task_id=task_id,
                event_type="status_changed",
                payload_json=json.dumps({"status": status.value, "error_detail": error_detail}),
            )
        )
        session.commit()


@app.get("/api/missions/{mission_id}/log")
def mission_log(mission_id: str) -> list[dict]:
    with get_session() as session:
        tasks = session.exec(select(MissionTask).where(MissionTask.mission_id == mission_id)).all()
        task_runtime = {t.id: t.runtime for t in tasks}
        if not task_runtime:
            return []
        events = session.exec(
            select(TaskEvent)
            .where(TaskEvent.task_id.in_(task_runtime.keys()), TaskEvent.event_type == "status_changed")
            .order_by(TaskEvent.timestamp.desc())
            .limit(200)
        ).all()
        return [
            {
                "task_id": e.task_id,
                "runtime": task_runtime.get(e.task_id, "?"),
                "timestamp": e.timestamp.isoformat(),
                **json.loads(e.payload_json),
            }
            for e in events
        ]
