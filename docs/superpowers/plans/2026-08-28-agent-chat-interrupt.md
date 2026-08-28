# Human-in-the-Loop Chat/Interrupt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn any Claude Code agent's task into a persistent, chattable session — the user can send it a message at any time, interrupting a live turn (kill-and-resume) or reopening a finished one, with the reply appearing in the same transcript.

**Architecture:** `_execute_task` becomes a loop instead of a single `send_task`/`stream_events` pass. A durable `native_session_id` column plus a new `RuntimeEvent.native_ref` field let any future invocation resume the right Claude Code session, even across a server restart. Two small module-level registries in `app.py` (`_active_handles` for interrupting a live turn, `_pending_messages` for queuing what to say next) connect a new `POST /api/tasks/{id}/message` endpoint to the loop without the two ever racing to create adapter sessions independently.

**Tech Stack:** Python 3.12, FastAPI, SQLModel/SQLite, vanilla JS — all already in use, no new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-28-agent-chat-interrupt-design.md`

## Global Constraints

- `claude_code` only — messaging a `codex`/`hermes`/`openclaw` task returns HTTP 400.
- No auto-detection of "the agent wants input" — no such UI state exists anywhere in this plan.
- "Interrupt" means kill-and-resume (via the existing `adapter.stop()` + `--resume`), never literal live-stream injection — no such capability exists in the underlying CLI.
- No cross-stage pipeline retriggering — messaging a task never causes any other task to re-run.
- The existing single-turn behavior (ad-hoc tasks, pipeline tasks, when nobody ever calls the new endpoint) must be provably unchanged — every existing test must keep passing without modification.

---

### Task 1: Data model + adapter contract fields

**Files:**
- Modify: `src/mission_control/server/models.py`
- Modify: `src/mission_control/server/db.py`
- Modify: `src/mission_control/adapters/types.py`
- Test: `tests/test_db_migration.py` (extend existing file)
- Test: `tests/test_runtime_event_native_ref.py` (new)

**Interfaces:**
- Produces: `MissionTask.native_session_id: str | None`; `RuntimeEvent.native_ref: str | None`; `SessionRequest.resume_native_ref: str | None` — all consumed by Task 2 and Task 3.

- [ ] **Step 1: Write the failing test for the migration**

Add to `tests/test_db_migration.py` (a third test function, alongside the two already there):

```python
def test_migrate_adds_native_session_id_column(tmp_path):
    from mission_control.server.db import _migrate_add_columns

    db_path = tmp_path / "test3.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE missiontask (id TEXT PRIMARY KEY, role TEXT, pipeline_run_id TEXT, result_text TEXT)"
        )
        conn.commit()

    _migrate_add_columns(engine)

    with engine.connect() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(missiontask)")}
    assert "native_session_id" in columns
```

(This file already has `from sqlmodel import create_engine` at the top from the prior plan — no new import needed.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_db_migration.py::test_migrate_adds_native_session_id_column -v`
Expected: FAIL — `native_session_id` not in columns (migration doesn't add it yet).

- [ ] **Step 3: Add the model field and migration entry**

In `src/mission_control/server/models.py`, add the field to `MissionTask` right after `result_text`:

```python
    result_text: str | None = Field(default=None)
    native_session_id: str | None = Field(default=None)
```

In `src/mission_control/server/db.py`, extend the tuple:

```python
_NEW_COLUMNS = (("role", "TEXT"), ("pipeline_run_id", "TEXT"), ("result_text", "TEXT"), ("native_session_id", "TEXT"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_db_migration.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Write the failing test for the new type fields**

Create `tests/test_runtime_event_native_ref.py`:

```python
"""Offline tests for the two new adapter-contract fields this plan adds:
RuntimeEvent.native_ref and SessionRequest.resume_native_ref. Pure pydantic
model construction — no DB, no adapters, no network."""

from datetime import UTC, datetime

from mission_control.adapters.types import RuntimeEvent, RuntimeType, SessionRequest, Workspace


def test_runtime_event_native_ref_defaults_to_none():
    event = RuntimeEvent(session_id="s1", event_type="assistant", timestamp=datetime.now(UTC))
    assert event.native_ref is None


def test_runtime_event_native_ref_can_be_set():
    event = RuntimeEvent(session_id="s1", event_type="result", timestamp=datetime.now(UTC), native_ref="claude-native-abc")
    assert event.native_ref == "claude-native-abc"


def test_session_request_resume_native_ref_defaults_to_none():
    req = SessionRequest(mission_id="m1", task_id="t1", workspace=Workspace(path="."))
    assert req.resume_native_ref is None


def test_session_request_resume_native_ref_can_be_set():
    req = SessionRequest(mission_id="m1", task_id="t1", workspace=Workspace(path="."), resume_native_ref="claude-native-abc")
    assert req.resume_native_ref == "claude-native-abc"
```

- [ ] **Step 6: Run test to verify it fails**

Run: `uv run pytest tests/test_runtime_event_native_ref.py -v`
Expected: FAIL — `TypeError: RuntimeEvent() got an unexpected keyword argument 'native_ref'` (and similarly for `resume_native_ref`).

- [ ] **Step 7: Add the two fields**

In `src/mission_control/adapters/types.py`, add to `RuntimeEvent` (right after `error_family`):

```python
class RuntimeEvent(BaseModel):
    """A single item from `RuntimeAdapter.stream_events()`.

    Exactly one of `cost`, `approval_request`, or `payload` is typically
    populated per event; all three fields exist on every event so consumers
    don't need per-runtime event-type branching.
    """

    session_id: str
    event_type: str
    timestamp: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    cost: CostEvent | None = None
    approval_request: ApprovalRequest | None = None
    error_family: ErrorFamily | None = None
    native_ref: str | None = None
    """The runtime-native session id this event belongs to, when the
    adapter can determine it (e.g. Claude Code's `session_id` field on
    stream-json events). Lets callers persist a durable, resumable session
    reference without depending on adapter-internal state."""
```

Add to `SessionRequest` (right after `extra`):

```python
class SessionRequest(BaseModel):
    mission_id: str
    task_id: str
    workspace: Workspace
    model_route: str | None = None
    """Optional hint passed through to the Model Gateway (e.g. 'qwen-local',
    'claude-sonnet-5'). Adapters never resolve this themselves."""
    extra: dict[str, Any] = Field(default_factory=dict)
    resume_native_ref: str | None = None
    """A previously-seen runtime-native session id (see `RuntimeEvent.native_ref`)
    to resume, when starting a session for a task that has run before —
    even in an earlier server process. Adapters that don't support
    resuming from a bare native ref (i.e. everything but Claude Code today)
    ignore this field."""
```

- [ ] **Step 8: Run test to verify it passes**

Run: `uv run pytest tests/test_runtime_event_native_ref.py -v`
Expected: PASS (4 tests)

- [ ] **Step 9: Run the full offline suite to check for regressions**

Run: `uv run pytest -q`
Expected: all prior tests still pass, plus these 7 new ones (32 + 7 = 39 passed, 2 skipped)

- [ ] **Step 10: Commit**

```bash
git add src/mission_control/server/models.py src/mission_control/server/db.py src/mission_control/adapters/types.py tests/test_db_migration.py tests/test_runtime_event_native_ref.py
git commit -m "feat: add native_session_id column and native_ref/resume_native_ref adapter fields"
```

---

### Task 2: `ClaudeCodeRuntimeAdapter` — seed and surface the native session ref

**Files:**
- Modify: `src/mission_control/adapters/claude_code/adapter.py:93-101` (`start`), `src/mission_control/adapters/claude_code/adapter.py:158-180` (`_pump_events`)
- Test: `tests/test_adapter_contract.py` (extend existing file)

**Interfaces:**
- Consumes: `SessionRequest.resume_native_ref`, `RuntimeEvent.native_ref` (Task 1).
- Produces: `ClaudeCodeRuntimeAdapter.start()` seeds `_SessionState.native_ref` when given `resume_native_ref`; `_pump_events` attaches `native_ref` to every emitted `RuntimeEvent` once known — both consumed by Task 3.

- [ ] **Step 1: Write the failing test for seeding**

Add to `tests/test_adapter_contract.py`. This project's existing `test_claude_code_send_task_passes_prompt_via_stdin` test (from the prior plan) already shows the mocking pattern for `create_subprocess` — follow it:

```python
async def test_claude_code_start_with_resume_native_ref_causes_resume_flag():
    """A task reopened after finishing (or after a server restart) has no
    live adapter session, but the DB remembers Claude's own session id.
    start() must seed that into the new session so the *next* send_task
    call passes --resume, without needing any turn to have run first."""
    from datetime import UTC, datetime
    from pathlib import Path
    from unittest.mock import AsyncMock, Mock, patch

    adapter = ClaudeCodeRuntimeAdapter()
    workspace = Workspace(path=Path("/tmp/test"))

    handle = await adapter.start(SessionRequest(
        mission_id="m1", task_id="t1", workspace=workspace,
        resume_native_ref="claude-native-existing-session",
    ))

    captured_args = None

    async def mock_create_subprocess(*args, **kwargs):
        nonlocal captured_args
        captured_args = args
        mock_process = AsyncMock()
        mock_stdin = Mock()
        mock_stdin.write = Mock(return_value=None)
        mock_stdin.drain = AsyncMock()
        mock_stdin.close = Mock()
        mock_process.stdin = mock_stdin
        mock_process.stdout = AsyncMock()
        mock_process.returncode = 0

        async def empty_stream():
            return
            yield
        mock_process.stdout.__aiter__ = Mock(return_value=empty_stream())
        mock_process.wait = AsyncMock()
        return mock_process

    with patch("mission_control.adapters.claude_code.adapter.create_subprocess", side_effect=mock_create_subprocess):
        await adapter.send_task(handle, Task(id="t1", mission_id="m1", instructions="continue please"))

    args_str = " ".join(captured_args)
    assert "--resume" in args_str
    assert "claude-native-existing-session" in args_str

    await adapter.destroy(handle)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_adapter_contract.py::test_claude_code_start_with_resume_native_ref_causes_resume_flag -v`
Expected: FAIL — `--resume` is not in the built args, because `start()` doesn't seed `native_ref` from `resume_native_ref` yet.

- [ ] **Step 3: Update `start()`**

In `src/mission_control/adapters/claude_code/adapter.py`, replace:

```python
    async def start(self, session: SessionRequest) -> SessionHandle:
        state = _SessionState(session.workspace, self._pending_settings_path)
        session_id = str(uuid.uuid4())
        self._sessions[session_id] = state
        return SessionHandle(
            session_id=session_id,
            runtime_type=RuntimeType.CLAUDE_CODE,
            started_at=datetime.now(UTC),
        )
```

with:

```python
    async def start(self, session: SessionRequest) -> SessionHandle:
        state = _SessionState(session.workspace, self._pending_settings_path)
        state.native_ref = session.resume_native_ref
        session_id = str(uuid.uuid4())
        self._sessions[session_id] = state
        return SessionHandle(
            session_id=session_id,
            runtime_type=RuntimeType.CLAUDE_CODE,
            started_at=datetime.now(UTC),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_adapter_contract.py::test_claude_code_start_with_resume_native_ref_causes_resume_flag -v`
Expected: PASS

- [ ] **Step 5: Write the failing test for surfacing `native_ref` on events**

Add to `tests/test_adapter_contract.py`:

```python
async def test_claude_code_pump_events_attaches_native_ref():
    """Once a stream-json line reveals Claude's session_id, every
    subsequently-emitted RuntimeEvent must carry it as native_ref — this is
    what lets _execute_task persist a durable, resumable session id."""
    from datetime import UTC, datetime
    from pathlib import Path
    from unittest.mock import AsyncMock, Mock, patch

    adapter = ClaudeCodeRuntimeAdapter()
    workspace = Workspace(path=Path("/tmp/test"))
    handle = await adapter.start(SessionRequest(mission_id="m1", task_id="t1", workspace=workspace))

    lines = [
        b'{"type":"system","subtype":"init","session_id":"claude-native-xyz"}\n',
        b'{"type":"result","result":"OK","session_id":"claude-native-xyz","total_cost_usd":0.01,"usage":{"input_tokens":1,"output_tokens":1}}\n',
    ]

    async def mock_create_subprocess(*args, **kwargs):
        mock_process = AsyncMock()
        mock_stdin = Mock()
        mock_stdin.write = Mock(return_value=None)
        mock_stdin.drain = AsyncMock()
        mock_stdin.close = Mock()
        mock_process.stdin = mock_stdin

        async def line_stream():
            for line in lines:
                yield line
        mock_process.stdout = AsyncMock()
        mock_process.stdout.__aiter__ = Mock(return_value=line_stream())
        mock_process.wait = AsyncMock()
        mock_process.returncode = 0
        return mock_process

    with patch("mission_control.adapters.claude_code.adapter.create_subprocess", side_effect=mock_create_subprocess):
        await adapter.send_task(handle, Task(id="t1", mission_id="m1", instructions="hi"))
        events = []
        async for event in adapter.stream_events(handle):
            events.append(event)

    assert len(events) == 2
    assert events[0].native_ref == "claude-native-xyz"
    assert events[1].native_ref == "claude-native-xyz"

    await adapter.destroy(handle)
```

- [ ] **Step 6: Run test to verify it fails**

Run: `uv run pytest tests/test_adapter_contract.py::test_claude_code_pump_events_attaches_native_ref -v`
Expected: FAIL — `events[0].native_ref` is `None` (not attached yet).

- [ ] **Step 7: Update `_pump_events`**

In `src/mission_control/adapters/claude_code/adapter.py`, replace:

```python
            if sid := payload.get("session_id"):
                state.native_ref = sid
            await state.queue.put(
                RuntimeEvent(
                    session_id=session_id,
                    event_type=payload.get("type", "unknown"),
                    timestamp=datetime.now(UTC),
                    payload=payload,
                    cost=self._extract_cost(payload),
                )
            )
```

with:

```python
            if sid := payload.get("session_id"):
                state.native_ref = sid
            await state.queue.put(
                RuntimeEvent(
                    session_id=session_id,
                    event_type=payload.get("type", "unknown"),
                    timestamp=datetime.now(UTC),
                    payload=payload,
                    cost=self._extract_cost(payload),
                    native_ref=state.native_ref,
                )
            )
```

- [ ] **Step 8: Run test to verify it passes**

Run: `uv run pytest tests/test_adapter_contract.py::test_claude_code_pump_events_attaches_native_ref -v`
Expected: PASS

- [ ] **Step 9: Run the full offline suite to check for regressions**

Run: `uv run pytest -q`
Expected: all prior tests still pass, plus these 2 new ones

- [ ] **Step 10: Commit**

```bash
git add src/mission_control/adapters/claude_code/adapter.py tests/test_adapter_contract.py
git commit -m "feat: seed and surface Claude Code's native session ref for durable resume"
```

---

### Task 3: `_execute_task` becomes a loop; interrupt/reopen infrastructure

**Files:**
- Modify: `src/mission_control/server/app.py:180-282` (`_execute_task`, `_finish`), plus new module-level state near `_background_tasks` (line 49)
- Test: `tests/test_chat_interrupt_loop.py` (new)

**Interfaces:**
- Consumes: `RuntimeEvent.native_ref`, `SessionRequest.resume_native_ref` (Tasks 1-2).
- Produces: `_active_handles: dict[str, SessionHandle]`, `_pending_messages: dict[str, asyncio.Queue[str]]`, `_record_user_message_event(task_id, text)` — all consumed by Task 4 (the new endpoint).

This is the most delicate task in this plan — read the spec's "New execution model" and "Unifying continuity" sections in full before starting, not just this brief.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_chat_interrupt_loop.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_chat_interrupt_loop.py -v`
Expected: FAIL — `AttributeError: module 'mission_control.server.app' has no attribute '_pending_messages'` (and similarly for `_active_handles`), since none of this exists yet.

- [ ] **Step 3: Add the module-level registries**

In `src/mission_control/server/app.py`, right after the existing `_background_tasks` declaration:

```python
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
```

Add `SessionHandle` to the import from `mission_control.adapters.types` (it's not currently imported in `app.py`):

```python
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
```

- [ ] **Step 4: Replace `_execute_task` and `_finish`**

Replace the entire current `_execute_task` and `_finish` functions (from `async def _execute_task(task_id: str) -> None:` through the end of `_finish`, i.e. lines 180-282 of the current file) with:

```python
async def _execute_task(task_id: str) -> None:
    with get_session() as session:
        task = session.get(MissionTask, task_id)
        runtime = RuntimeType(task.runtime)
        prompt = task.prompt
        native_session_id = task.native_session_id

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
        handle = await adapter.start(SessionRequest(
            mission_id=task.mission_id, task_id=task_id, workspace=workspace,
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
                ack = await adapter.send_task(handle, Task(id=task_id, mission_id=task.mission_id, instructions=prompt))
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
```

Notes on this rewrite:
- `adapter.destroy(handle)` moved into the `finally` block (previously it was called individually after each early-return branch) — this guarantees destroy always runs exactly once no matter which branch exits the loop, and pairs naturally with deregistering `_active_handles` right beside it.
- The `NotImplementedError` and `ack.accepted` branches now `return` directly from inside the loop/`try` instead of also calling `adapter.destroy(handle)` themselves — the `finally` block handles that now. Removing the old standalone `await adapter.destroy(handle)` lines from those two branches is required, not optional — calling destroy twice would double-pop an already-gone session.
- `native_session_id` is now read once at the top (for the initial `resume_native_ref`) and written inside the same per-event DB transaction that already updates cost — no extra DB round trip.

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `uv run pytest tests/test_chat_interrupt_loop.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Run the full offline suite to check for regressions**

Run: `uv run pytest -q`

This is the step most likely to reveal a problem — the rewritten `_execute_task` is used by both the ad-hoc flow and the pipeline flow. Pay particular attention to `tests/test_pipeline_routes.py` and `tests/test_finish_persists_result_text.py`, which exercise `_finish` and `_execute_task`-adjacent behavior directly.

Expected: all prior tests still pass, plus the 4 new ones.

- [ ] **Step 7: Commit**

```bash
git add src/mission_control/server/app.py tests/test_chat_interrupt_loop.py
git commit -m "feat: restructure _execute_task into an interruptible/resumable loop"
```

---

### Task 4: `POST /api/tasks/{task_id}/message` endpoint

**Files:**
- Modify: `src/mission_control/server/app.py` (new request model, new route)
- Test: `tests/test_task_message_route.py` (new)

**Interfaces:**
- Consumes: `_active_handles`, `_pending_messages`, `_background_tasks`, `_execute_task` (Task 3).
- Produces: `POST /api/tasks/{task_id}/message` → `{accepted: true}` on success, 404/400 on the documented error cases.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_task_message_route.py`:

```python
"""Offline tests for the two synchronous error paths of
POST /api/tasks/{id}/message — 404 and 400. The "actually sends a message"
paths require a live or heavily-mocked adapter session and are covered by
tests/test_chat_interrupt_loop.py (the loop itself) plus manual live
verification (see the spec) — this file only checks routing/validation."""

import pytest
from sqlmodel import Session, SQLModel, create_engine

from mission_control.server import app as app_module
from mission_control.server.models import Mission, MissionTask


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_task_message_route.py -v`
Expected: FAIL — `AttributeError: module 'mission_control.server.app' has no attribute 'send_task_message'`

- [ ] **Step 3: Add `SendMessageRequest` and the route**

In `src/mission_control/server/app.py`, add near the other request models (e.g. right after `RunPipelineRequest`):

```python
class SendMessageRequest(BaseModel):
    text: str
```

Add the route (placement doesn't matter functionally — put it near the other `/api/tasks/...` routes, e.g. right after `stream_task_events`):

```python
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
    elif current_status != TaskStatus.RUNNING:
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_task_message_route.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Verify the route registers**

Run: `uv run python -c "from mission_control.server.app import app; print('/api/tasks/{task_id}/message' in [r.path for r in app.routes if hasattr(r,'path')])"`
Expected: `True`

- [ ] **Step 6: Run the full offline suite to check for regressions**

Run: `uv run pytest -q`
Expected: all prior tests still pass, plus these 2 new ones

- [ ] **Step 7: Commit**

```bash
git add src/mission_control/server/app.py tests/test_task_message_route.py
git commit -m "feat: add POST /api/tasks/{id}/message endpoint"
```

---

### Task 5: Dashboard UI — message box + user_message transcript rendering

**Files:**
- Modify: `src/mission_control/server/static/dashboard.html`

**Interfaces:**
- Consumes: `POST /api/tasks/{id}/message` (Task 4).
- Reuses: `watchTranscript(taskId)`, `appendTranscriptLine`, `escapeHtml`, `state.currentTaskId` — all already defined in this file.

No JS test runner exists in this project — verification is the same Node syntax check + id cross-check technique already used for every prior dashboard change in this project, plus a manual browser check deferred to the controller.

- [ ] **Step 1: Add the message box markup**

In `src/mission_control/server/static/dashboard.html`, insert this block between the closing `</div>` of `#transcriptPanel` and the `<div id="footer">` line:

```html
  <form class="inline" id="messageForm">
    <input type="text" name="text" placeholder="Message this agent (works even after it finishes)" required />
    <button type="submit">Send</button>
  </form>
```

- [ ] **Step 2: Add the JS submit handler and user_message transcript rendering**

Add this submit handler right after the existing `pipelineForm` submit handler (after its closing `});`):

```javascript
document.getElementById("messageForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!state.currentTaskId) return;
  const fd = new FormData(e.target);
  const text = fd.get("text");
  e.target.reset();
  await fetchJSON(`/api/tasks/${state.currentTaskId}/message`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }),
  });
  watchTranscript(state.currentTaskId);  // re-open the SSE connection — it may have closed on a prior "done"
});
```

In `appendTranscriptLine`, add the `user_message` check before the per-runtime dispatch:

```javascript
function appendTranscriptLine(box, runtime, eventType, payload) {
  if (eventType === "user_message") return appendLine(box, "t-user", `<b>you:</b> ${escapeHtml(payload.text || "")}`);
  if (runtime === "claude_code") return appendClaudeCodeLine(box, eventType, payload);
  if (runtime === "codex") return appendCodexLine(box, eventType, payload);
  if (runtime === "hermes") return appendHermesLine(box, eventType, payload);
  appendLine(box, "t-raw", escapeHtml(JSON.stringify(payload)).slice(0, 400));
}
```

- [ ] **Step 3: Add CSS for the new line style**

In the `<style>` block, add near the other `.t-*` transcript line classes (e.g. right after `.t-final`):

```css
  .t-user { color: var(--blue); }
  .t-user b { color: var(--white); font-weight: normal; }
```

- [ ] **Step 4: Syntax-check the embedded JavaScript**

Run:
```bash
python -c "
import re
html = open('src/mission_control/server/static/dashboard.html', encoding='utf-8').read()
m = re.search(r'<script>(.*)</script>', html, re.S)
open('dashboard_script_check.js', 'w', encoding='utf-8').write(m.group(1))
"
node --check dashboard_script_check.js && echo "JS SYNTAX OK"
rm -f dashboard_script_check.js
```
Expected: `JS SYNTAX OK`

- [ ] **Step 5: Cross-check every `getElementById` reference resolves**

Run:
```bash
python -c "
import re
html = open('src/mission_control/server/static/dashboard.html', encoding='utf-8').read()
referenced = set(re.findall(r'getElementById\(\"([^\"]+)\"\)', html))
declared = set(re.findall(r'id=\"([^\"]+)\"', html))
print('MISSING:', (referenced - declared) or 'none')
"
```
Expected: `MISSING: none`

- [ ] **Step 6: Manual browser verification (deferred to controller)**

Not performed by the implementer — no browser available. The controller will restart the server and verify: the message box appears under the transcript for any selected task; sending a message to a running task is followed by new events appearing; sending a message to a finished task flips it back to a visible "running" state and eventually back to a terminal one; `you:`-prefixed lines render distinctly from agent output.

- [ ] **Step 7: Commit**

```bash
git add src/mission_control/server/static/dashboard.html
git commit -m "feat: add chat/interrupt message box and user_message transcript rendering"
```

---

### Task 6: Manual end-to-end live verification

**Files:** none (verification only, no code changes)

Real, cost-incurring Claude Code usage — not part of the automated `pytest` suite, per the spec's testing section.

- [ ] **Step 1: Restart the server with the final code**

```powershell
Get-NetTCPConnection -LocalPort 8420 -State Listen -ErrorAction SilentlyContinue |
  Select-Object -ExpandProperty OwningProcess -Unique |
  ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1
$python = "D:\Tools\MissionControl\.venv\Scripts\python.exe"
$proc = Start-Process -FilePath $python -ArgumentList "-m","mission_control.server" -WorkingDirectory "D:\Tools\MissionControl" -WindowStyle Hidden -PassThru
$proc.Id | Out-File -FilePath "D:\Tools\MissionControl\.server.pid" -Encoding ascii
```

- [ ] **Step 2: Confirm health**

Run: `curl -s http://127.0.0.1:8420/api/health`
Expected: `"claude_code":true`

- [ ] **Step 3: Reopen an already-finished task**

Pick any existing `claude_code` task in a terminal status (e.g. from a mission created earlier). Send it a message:

```bash
curl -s -X POST "http://127.0.0.1:8420/api/tasks/<task_id>/message" -H "Content-Type: application/json" -d '{"text":"One more thing: also mention the current date is not relevant to your answer."}'
```

Poll `GET /api/tasks/<task_id>` until `status` returns to a terminal value. Expected: `status` briefly shows `"running"` then a terminal status again; `result_text` reflects a *new* response (not the original one), showing this was a genuine continuation of the same conversation, not a fresh unrelated one — check the transcript (`GET /api/tasks/<task_id>/events`, or the dashboard) for a `user_message` event containing your text, followed by real new agent output.

- [ ] **Step 4: Interrupt a task genuinely mid-turn**

Start a new task with a prompt likely to take a few seconds (e.g. asking it to read and summarize several files), then send it a message within ~1-2 seconds:

```bash
TASK_ID=$(curl -s -X POST http://127.0.0.1:8420/api/missions -H "Content-Type: application/json" -d '{"name":"interrupt-test","goal":"Read every file under src/mission_control/server/ and summarize what each one does.","workspace_path":"D:/Tools/MissionControl"}' | python -c "import sys,json;print(json.load(sys.stdin)['id'])")
# find the orchestrator task id via GET /api/missions/$TASK_ID/pipelines, then:
curl -s -X POST "http://127.0.0.1:8420/api/tasks/<orchestrator_task_id>/message" -H "Content-Type: application/json" -d '{"text":"Actually, skip the summary — just say OK."}'
```

Expected: the task's transcript shows the original turn's events, then a `user_message` event, then a *new* turn's events, and the task finishes with a short "OK"-style result rather than a full multi-file summary — demonstrating the redirect actually took effect, not that both turns ran independently to completion.

- [ ] **Step 5: No commit for this task** (verification only — if any step fails, go back to the relevant earlier task, fix, and re-commit there).
