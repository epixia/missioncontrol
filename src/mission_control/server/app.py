"""Mission Control's v0 platform: a local API + dashboard in front of the
RuntimeAdapter layer. This is the first piece of the "core owned subsystems"
from the architecture doc (§2) — mission/task persistence and observability —
though budgets, approvals, and a real scheduler are still not built (§13).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
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
    SessionHandle,
    SessionRequest,
    Task,
    Workspace,
)
from mission_control.server.db import get_session, init_db
from mission_control.server.models import (
    AgentRole,
    Mission,
    MissionTask,
    TaskEvent,
    TaskStatus,
    Ticket,
    TicketColumn,
    TicketComment,
)
from mission_control.server.pipeline_prompts import (
    build_coder_prompt,
    build_orchestrator_prompt,
    build_reviewer_prompt,
    extract_claude_code_result_text,
)
from mission_control.server.runtime_registry import get_adapter, load_pinned_spec

_STATIC_DIR = Path(__file__).parent / "static"
_RUNTIME_STATE_ROOT = Path.home() / ".mission-control" / "runtimes"

# Strong references to fire-and-forget background tasks (e.g. the pipeline
# runner) so they aren't garbage-collected mid-execution — the event loop
# only holds a weak reference to a Task, per the asyncio docs' documented
# footgun. Discarded automatically once the task finishes.
_background_tasks: set[asyncio.Task] = set()

# Chat/interrupt infrastructure (see docs/superpowers/specs/2026-08-28-agent-chat-interrupt-design.md).
# _active_handles: which tasks have a live adapter session in THIS process
# right now — populated only by _execute_task, for the duration of its own
# execution. Lets POST /api/tasks/{id}/message know whether there's a live
# turn to interrupt.
_active_handles: dict[str, SessionHandle] = {}
# _pending_messages: messages the user has sent that _execute_task's loop
# hasn't consumed yet, one queue per task.
_pending_messages: dict[str, asyncio.Queue[str]] = {}
# _TERMINAL_TASK_STATUSES: the statuses from which a task will never resume
# on its own — safe to start a fresh _execute_task for. PENDING and RUNNING
# are NOT terminal: PENDING means an _execute_task invocation is already
# starting (or about to), and RUNNING means one is already live.
_TERMINAL_TASK_STATUSES = (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.UNSUPPORTED)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(title="Mission Control", lifespan=_lifespan)


class CreateMissionRequest(BaseModel):
    name: str
    goal: str | None = None
    workspace_path: str | None = None


class CreateTaskRequest(BaseModel):
    runtime: RuntimeType
    prompt: str
    workspace_path: str


class SendMessageRequest(BaseModel):
    text: str


class CreateTicketRequest(BaseModel):
    title: str
    description: str = ""
    column: TicketColumn = TicketColumn.BACKLOG


class UpdateTicketRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    column: TicketColumn | None = None


class CreateCommentRequest(BaseModel):
    text: str


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
async def create_mission(req: CreateMissionRequest) -> Mission:
    with get_session() as session:
        mission = Mission(name=req.name)
        session.add(mission)
        session.commit()
        session.refresh(mission)

    if req.goal:
        _start_pipeline(mission.id, req.goal, req.workspace_path or ".")

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


@app.delete("/api/missions/{mission_id}")
def delete_mission(mission_id: str) -> dict:
    with get_session() as session:
        mission = session.get(Mission, mission_id)
        if mission is None:
            raise HTTPException(404, "mission not found")
        tasks = session.exec(select(MissionTask).where(MissionTask.mission_id == mission_id)).all()
        if any(t.status == TaskStatus.RUNNING for t in tasks):
            raise HTTPException(409, "cannot delete a mission with a running task — stop it first")
        task_ids = [t.id for t in tasks]
        if task_ids:
            for event in session.exec(select(TaskEvent).where(TaskEvent.task_id.in_(task_ids))).all():
                session.delete(event)
            for task in tasks:
                session.delete(task)
        session.delete(mission)
        session.commit()
    for task_id in task_ids:
        _pending_messages.pop(task_id, None)
        _active_handles.pop(task_id, None)
    return {"deleted": True}


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


@app.post("/api/tasks/{task_id}/message")
async def send_task_message(task_id: str, req: SendMessageRequest) -> dict:
    with get_session() as session:
        task = session.get(MissionTask, task_id)
        if task is None:
            raise HTTPException(404, "task not found")
        if task.runtime != "claude_code":
            raise HTTPException(400, f"messaging is only supported for claude_code tasks, not {task.runtime!r}")
        current_status = task.status

    _pending_messages.setdefault(task_id, asyncio.Queue()).put_nowait(req.text)

    live_handle = _active_handles.get(task_id)
    if current_status == TaskStatus.RUNNING and live_handle is not None:
        adapter = get_adapter(RuntimeType(task.runtime))
        await adapter.stop(live_handle)
    elif current_status in _TERMINAL_TASK_STATUSES:
        with get_session() as session:
            task = session.get(MissionTask, task_id)
            task.status = TaskStatus.RUNNING
            session.add(task)
            session.commit()
        bg = asyncio.create_task(_execute_task(task_id))
        _background_tasks.add(bg)
        bg.add_done_callback(_background_tasks.discard)
    # else: current_status == RUNNING but live_handle is None — a data-race
    # edge case (status flipped between the two reads above). The message
    # is already queued; it'll be picked up whenever that invocation's loop
    # next checks, rather than erroring here.

    return {"accepted": True}


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
            terminal = task.status in _TERMINAL_TASK_STATUSES
        if terminal and not new_events:
            yield f"event: done\ndata: {task.status.value}\n\n"
            return
        await asyncio.sleep(0.3)


async def _execute_task(task_id: str) -> None:
    with get_session() as session:
        task = session.get(MissionTask, task_id)
        runtime = RuntimeType(task.runtime)
        prompt = task.prompt
        native_session_id = task.native_session_id
        mission_id = task.mission_id

    adapter = get_adapter(runtime)
    spec = load_pinned_spec(runtime)

    try:
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
            handle = await adapter.start(SessionRequest(
                mission_id=mission_id, task_id=task_id, workspace=workspace,
                resume_native_ref=native_session_id,
            ))
            task.session_id = handle.session_id
            task.status = TaskStatus.RUNNING
            session.add(task)
            session.add(TaskEvent(task_id=task_id, event_type="status_changed", payload_json=json.dumps({"status": "running"})))
            session.commit()

        _active_handles[task_id] = handle
        try:
            while True:
                pending = _pending_messages.get(task_id)
                if pending is not None and not pending.empty():
                    prompt = await pending.get()
                    _record_user_message_event(task_id, prompt)

                try:
                    ack = await adapter.send_task(handle, Task(id=task_id, mission_id=mission_id, instructions=prompt))
                except NotImplementedError as exc:
                    _finish(task_id, TaskStatus.UNSUPPORTED, error_detail=str(exc))
                    return

                if not ack.accepted:
                    _finish(task_id, TaskStatus.FAILED, error_detail=ack.reason or "task rejected")
                    return

                saw_error = False
                captured_result_text: str | None = None
                captured_native_ref: str | None = None
                async for event in adapter.stream_events(handle):
                    if event.error_family is not None:
                        saw_error = True
                    extracted = extract_claude_code_result_text(event.event_type, event.payload)
                    if extracted is not None:
                        captured_result_text = extracted
                    if event.native_ref is not None:
                        captured_native_ref = event.native_ref
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
                        if captured_native_ref is not None:
                            task.native_session_id = captured_native_ref
                        session.add(task)
                        session.commit()

                pending = _pending_messages.get(task_id)
                if pending is not None and not pending.empty():
                    continue  # top-of-loop check above picks up the message

                health = await adapter.health(handle)
                final_status = TaskStatus.SUCCEEDED if health.status == HealthStatus.HEALTHY and not saw_error else TaskStatus.FAILED
                _finish(
                    task_id,
                    final_status,
                    error_detail=None if final_status == TaskStatus.SUCCEEDED else health.detail,
                    result_text=captured_result_text if final_status == TaskStatus.SUCCEEDED else None,
                )
                return
        finally:
            _active_handles.pop(task_id, None)
            await adapter.destroy(handle)
    except Exception as exc:
        # Defensive boundary: without this, an unhandled exception anywhere
        # above (adapter.configure/deploy/start, send_task, or mid-stream)
        # would leave the task's DB row RUNNING forever with no
        # status_changed event — and because the message endpoint only
        # restarts a task that is in a terminal status, the task would
        # become permanently unmessageable. Mirrors _execute_pipeline's own
        # boundary below.
        _finish(task_id, TaskStatus.FAILED, error_detail=repr(exc))


def _record_user_message_event(task_id: str, text: str) -> None:
    with get_session() as session:
        session.add(TaskEvent(task_id=task_id, event_type="user_message", payload_json=json.dumps({"text": text})))
        session.commit()


def _finish(task_id: str, status: TaskStatus, error_detail: str | None = None, result_text: str | None = None) -> None:
    with get_session() as session:
        task = session.get(MissionTask, task_id)
        if task is None:
            return
        task.status = status
        task.error_detail = error_detail
        task.result_text = result_text
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
        task_meta = {t.id: {"runtime": t.runtime, "role": t.role.value if t.role else None} for t in tasks}
        if not task_meta:
            return []
        events = session.exec(
            select(TaskEvent)
            .where(TaskEvent.task_id.in_(task_meta.keys()), TaskEvent.event_type == "status_changed")
            .order_by(TaskEvent.timestamp.desc())
            .limit(200)
        ).all()
        return [
            {
                "task_id": e.task_id,
                **task_meta.get(e.task_id, {"runtime": "?", "role": None}),
                "timestamp": e.timestamp.isoformat(),
                **json.loads(e.payload_json),
            }
            for e in events
        ]


class RunPipelineRequest(BaseModel):
    goal: str
    workspace_path: str


def _start_pipeline(mission_id: str, goal: str, workspace_path: str) -> tuple[str, str]:
    """Create the Orchestrator task and kick off the pipeline in the
    background. Returns (pipeline_run_id, orchestrator_task_id). Shared by
    the explicit `POST .../pipeline` route and mission creation's optional
    auto-start (`CreateMissionRequest.goal`)."""
    pipeline_run_id = str(uuid.uuid4())
    orchestrator_task_id = _create_pipeline_task(
        mission_id, pipeline_run_id, AgentRole.ORCHESTRATOR,
        build_orchestrator_prompt(goal, mission_id), workspace_path,
    )
    task = asyncio.create_task(
        _execute_pipeline(pipeline_run_id, mission_id, goal, workspace_path, orchestrator_task_id)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return pipeline_run_id, orchestrator_task_id


@app.post("/api/missions/{mission_id}/pipeline")
async def run_pipeline(mission_id: str, req: RunPipelineRequest) -> dict:
    with get_session() as session:
        mission = session.get(Mission, mission_id)
        if mission is None:
            raise HTTPException(404, "mission not found")

    pipeline_run_id, orchestrator_task_id = _start_pipeline(mission_id, req.goal, req.workspace_path)

    with get_session() as session:
        orchestrator_task = session.get(MissionTask, orchestrator_task_id)
        return {"pipeline_run_id": pipeline_run_id, "orchestrator_task": orchestrator_task}


def _create_pipeline_task(
    mission_id: str, pipeline_run_id: str, role: AgentRole, prompt: str, workspace_path: str
) -> str:
    with get_session() as session:
        task = MissionTask(
            mission_id=mission_id,
            runtime="claude_code",
            prompt=prompt,
            workspace_path=workspace_path,
            role=role,
            pipeline_run_id=pipeline_run_id,
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        task_id = task.id
        session.add(
            TaskEvent(task_id=task_id, event_type="status_changed", payload_json=json.dumps({"status": "pending"}))
        )
        session.commit()
        return task_id


async def _execute_pipeline(
    pipeline_run_id: str, mission_id: str, goal: str, workspace_path: str, orchestrator_task_id: str
) -> None:
    current_task_id = orchestrator_task_id
    try:
        await _execute_task(orchestrator_task_id)
        orchestrator = _get_task(orchestrator_task_id)
        if orchestrator is None or orchestrator.status != TaskStatus.SUCCEEDED or not orchestrator.result_text:
            return  # pipeline halts here; coder/reviewer tasks are never created

        coder_task_id = _create_pipeline_task(
            mission_id, pipeline_run_id, AgentRole.CODER,
            build_coder_prompt(goal, orchestrator.result_text, mission_id), workspace_path,
        )
        current_task_id = coder_task_id
        await _execute_task(coder_task_id)
        coder = _get_task(coder_task_id)
        if coder is None or coder.status != TaskStatus.SUCCEEDED or not coder.result_text:
            return

        reviewer_task_id = _create_pipeline_task(
            mission_id, pipeline_run_id, AgentRole.REVIEWER,
            build_reviewer_prompt(goal, coder.result_text, mission_id), workspace_path,
        )
        current_task_id = reviewer_task_id
        await _execute_task(reviewer_task_id)
    except Exception as exc:
        # Defensive boundary: without this, an unhandled exception from
        # _execute_task/_get_task/_create_pipeline_task would escape into
        # this fire-and-forget background task, leaving the in-flight
        # stage's DB row RUNNING forever with no status_changed event.
        _finish(current_task_id, TaskStatus.FAILED, error_detail=repr(exc))


def _get_task(task_id: str) -> MissionTask | None:
    with get_session() as session:
        return session.get(MissionTask, task_id)


_ROLE_ORDER = {AgentRole.ORCHESTRATOR: 0, AgentRole.CODER: 1, AgentRole.REVIEWER: 2}


@app.get("/api/missions/{mission_id}/pipelines")
def list_pipelines(mission_id: str) -> list[dict]:
    with get_session() as session:
        tasks = session.exec(
            select(MissionTask)
            .where(MissionTask.mission_id == mission_id, MissionTask.pipeline_run_id.is_not(None))
            .order_by(MissionTask.created_at)
        ).all()

    runs: dict[str, list[MissionTask]] = {}
    for task in tasks:
        runs.setdefault(task.pipeline_run_id, []).append(task)

    out = []
    for run_id, run_tasks in runs.items():
        run_tasks.sort(key=lambda t: _ROLE_ORDER.get(t.role, 99))
        statuses = [t.status for t in run_tasks]
        if any(s in (TaskStatus.FAILED, TaskStatus.UNSUPPORTED) for s in statuses):
            overall = "failed"
        elif any(s in (TaskStatus.PENDING, TaskStatus.RUNNING) for s in statuses):
            overall = "running"
        elif len(run_tasks) == 3 and statuses[-1] == TaskStatus.SUCCEEDED:
            overall = "succeeded"
        elif len(run_tasks) < 3 and statuses[-1] == TaskStatus.SUCCEEDED and not run_tasks[-1].result_text:
            # A stage SUCCEEDED but produced no result_text: per the halt
            # condition in _execute_pipeline, that halts the pipeline for
            # good (no later-stage task will ever be created), so this is
            # a terminal failure, not "still running".
            overall = "failed"
        else:
            overall = "running"
        out.append({"pipeline_run_id": run_id, "status": overall, "tasks": run_tasks})

    out.sort(key=lambda r: r["tasks"][0].created_at, reverse=True)
    return out


def _next_ticket_position(session, mission_id: str, column: TicketColumn) -> int:
    existing = session.exec(
        select(Ticket).where(Ticket.mission_id == mission_id, Ticket.column == column)
    ).all()
    return max((t.position for t in existing), default=-1) + 1


@app.post("/api/missions/{mission_id}/tickets", response_model=Ticket)
def create_ticket(mission_id: str, req: CreateTicketRequest, author_role: str | None = None) -> Ticket:
    with get_session() as session:
        mission = session.get(Mission, mission_id)
        if mission is None:
            raise HTTPException(404, "mission not found")
        ticket = Ticket(
            mission_id=mission_id,
            title=req.title,
            description=req.description,
            column=req.column,
            created_by_role=author_role,
            position=_next_ticket_position(session, mission_id, req.column),
        )
        session.add(ticket)
        session.commit()
        session.refresh(ticket)
        return ticket


@app.get("/api/missions/{mission_id}/tickets", response_model=list[Ticket])
def list_tickets(mission_id: str) -> list[Ticket]:
    with get_session() as session:
        tickets = session.exec(select(Ticket).where(Ticket.mission_id == mission_id)).all()
    column_order = {c: i for i, c in enumerate(TicketColumn)}
    return sorted(tickets, key=lambda t: (column_order[t.column], t.position))


@app.patch("/api/tickets/{ticket_id}", response_model=Ticket)
def update_ticket(ticket_id: str, req: UpdateTicketRequest) -> Ticket:
    with get_session() as session:
        ticket = session.get(Ticket, ticket_id)
        if ticket is None:
            raise HTTPException(404, "ticket not found")
        if req.title is not None:
            ticket.title = req.title
        if req.description is not None:
            ticket.description = req.description
        if req.column is not None and req.column != ticket.column:
            ticket.position = _next_ticket_position(session, ticket.mission_id, req.column)
            ticket.column = req.column
        ticket.updated_at = datetime.now(UTC)
        session.add(ticket)
        session.commit()
        session.refresh(ticket)
        return ticket


@app.get("/api/tickets/{ticket_id}")
def get_ticket(ticket_id: str) -> dict:
    with get_session() as session:
        ticket = session.get(Ticket, ticket_id)
        if ticket is None:
            raise HTTPException(404, "ticket not found")
        comments = session.exec(
            select(TicketComment).where(TicketComment.ticket_id == ticket_id).order_by(TicketComment.created_at)
        ).all()
        return {**ticket.model_dump(), "comments": [c.model_dump() for c in comments]}


@app.post("/api/tickets/{ticket_id}/comments", response_model=TicketComment)
def add_comment(ticket_id: str, req: CreateCommentRequest, author_role: str | None = None) -> TicketComment:
    with get_session() as session:
        ticket = session.get(Ticket, ticket_id)
        if ticket is None:
            raise HTTPException(404, "ticket not found")
        comment = TicketComment(ticket_id=ticket_id, author_role=author_role, text=req.text)
        session.add(comment)
        session.commit()
        session.refresh(comment)
        return comment
