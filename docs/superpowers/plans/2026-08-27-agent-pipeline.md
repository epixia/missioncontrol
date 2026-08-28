# Agent Pipeline (Orchestrator → Coder → Reviewer) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an additive "agent pipeline" to Mission Control: one goal prompt fans out into three sequential Claude Code tasks (Orchestrator → Coder → Reviewer), each role's output feeding the next, with the handoffs visible as a communication feed and each role's own activity inspectable via a tab.

**Architecture:** Three new nullable columns on the existing `MissionTask` row (`role`, `pipeline_run_id`, `result_text`) — no new tables. A pure-function module (`pipeline_prompts.py`) builds each role's prompt and extracts Claude Code's terminal result text. `app.py` gains two routes and a small `_execute_pipeline` coroutine that `await`s each role's existing `_execute_task` in sequence, halting on the first failure. The dashboard reuses its existing per-task transcript rendering for each role's tab and adds a new communication-feed panel.

**Tech Stack:** Python 3.12, FastAPI, SQLModel/SQLite, vanilla JS (no build step) — all already in use, no new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-27-agent-pipeline-design.md`

## Global Constraints

- Fixed pipeline, one pass — no iteration, no round caps, no re-delegation.
- All three roles run on `claude_code` only (hardcoded `runtime="claude_code"` on pipeline-created tasks).
- Fixed three roles: `orchestrator`, `coder`, `reviewer` — no configurable role sets.
- The existing ad-hoc single-task flow (`POST /api/missions/{id}/tasks`) must be left working exactly as it is today.
- No workspace isolation between concurrent pipeline runs, no budget cap — explicitly out of scope, do not add.
- A pipeline halts (later-role tasks are never created) if an earlier role's task doesn't reach `SUCCEEDED` with a non-empty `result_text`.

---

### Task 1: Data model + idempotent SQLite migration

**Files:**
- Modify: `src/mission_control/server/models.py`
- Modify: `src/mission_control/server/db.py`
- Test: `tests/test_db_migration.py` (new)

**Interfaces:**
- Produces: `AgentRole` enum (`mission_control.server.models.AgentRole`, values `"orchestrator"`/`"coder"`/`"reviewer"`); `MissionTask.role: AgentRole | None`, `MissionTask.pipeline_run_id: str | None`, `MissionTask.result_text: str | None`; `mission_control.server.db._migrate_add_columns(target_engine)`.

The live `~/.mission-control/mission-control.db` already has a `missiontask` table without these columns. `SQLModel.metadata.create_all()` only creates missing *tables*, not missing *columns* on existing tables — without a migration, the app breaks the next time it starts. This task adds one.

- [ ] **Step 1: Write the failing test**

Create `tests/test_db_migration.py`:

```python
"""Offline test for db.py's column migration — simulates an existing
pre-migration SQLite file (the shape the real ~/.mission-control database
has today) and confirms the new columns get added without touching any
live data. No server, no adapters, no network."""

from sqlmodel import create_engine


def test_migrate_adds_missing_columns(tmp_path):
    from mission_control.server.db import _migrate_add_columns

    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE missiontask (id TEXT PRIMARY KEY, mission_id TEXT, runtime TEXT, "
            "prompt TEXT, workspace_path TEXT, status TEXT, session_id TEXT, error_detail TEXT, "
            "total_cost_usd REAL, total_input_tokens INTEGER, total_output_tokens INTEGER, "
            "created_at TEXT, updated_at TEXT)"
        )
        conn.commit()

    _migrate_add_columns(engine)

    with engine.connect() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(missiontask)")}
    assert {"role", "pipeline_run_id", "result_text"} <= columns


def test_migrate_is_idempotent(tmp_path):
    from mission_control.server.db import _migrate_add_columns

    db_path = tmp_path / "test2.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        conn.exec_driver_sql("CREATE TABLE missiontask (id TEXT PRIMARY KEY)")
        conn.commit()

    _migrate_add_columns(engine)
    _migrate_add_columns(engine)  # must not raise "duplicate column name"

    with engine.connect() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(missiontask)")}
    assert {"role", "pipeline_run_id", "result_text"} <= columns
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_db_migration.py -v`
Expected: FAIL with `ImportError: cannot import name '_migrate_add_columns'` (it doesn't exist yet).

- [ ] **Step 3: Add the model fields**

In `src/mission_control/server/models.py`, add the enum after `TaskStatus` and the three fields to `MissionTask`:

```python
class AgentRole(StrEnum):
    ORCHESTRATOR = "orchestrator"
    CODER = "coder"
    REVIEWER = "reviewer"


class MissionTask(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    mission_id: str = Field(foreign_key="mission.id", index=True)
    runtime: str
    prompt: str
    workspace_path: str
    status: TaskStatus = Field(default=TaskStatus.PENDING)
    session_id: str | None = None
    error_detail: str | None = None
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    role: AgentRole | None = Field(default=None)
    pipeline_run_id: str | None = Field(default=None, index=True)
    result_text: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
```

(Insert `AgentRole` class right after `TaskStatus`'s closing line; insert the three new fields into `MissionTask` right after `total_output_tokens: int = 0` and before `created_at`.)

- [ ] **Step 4: Add the migration function**

Replace the contents of `src/mission_control/server/db.py` with:

```python
from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

_DB_PATH = Path.home() / ".mission-control" / "mission-control.db"

_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(f"sqlite:///{_DB_PATH}", connect_args={"check_same_thread": False})

_NEW_COLUMNS = (("role", "TEXT"), ("pipeline_run_id", "TEXT"), ("result_text", "TEXT"))


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _migrate_add_columns(engine)


def _migrate_add_columns(target_engine) -> None:
    """Idempotently add columns that create_all() won't add to an existing
    table. Safe to call on every startup, and safe to call twice."""
    with target_engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(missiontask)")}
        for column, coltype in _NEW_COLUMNS:
            if column not in existing:
                conn.exec_driver_sql(f"ALTER TABLE missiontask ADD COLUMN {column} {coltype}")
        conn.commit()


def get_session() -> Session:
    return Session(engine)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_db_migration.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Run the full offline suite to check for regressions**

Run: `uv run pytest -q`
Expected: all previously-passing tests still pass (12 passed, 2 skipped, plus the 2 new migration tests = 14 passed, 2 skipped)

- [ ] **Step 7: Commit**

```bash
git add src/mission_control/server/models.py src/mission_control/server/db.py tests/test_db_migration.py
git commit -m "feat: add AgentRole/pipeline_run_id/result_text columns with idempotent migration"
```

---

### Task 2: Pipeline prompts and result-text extraction (pure functions)

**Files:**
- Create: `src/mission_control/server/pipeline_prompts.py`
- Test: `tests/test_pipeline_prompts.py` (new)

**Interfaces:**
- Consumes: nothing (pure functions, no imports from other new code).
- Produces: `build_orchestrator_prompt(goal: str) -> str`, `build_coder_prompt(goal: str, orchestrator_result: str) -> str`, `build_reviewer_prompt(goal: str, coder_result: str) -> str`, `extract_claude_code_result_text(event_type: str, payload: dict) -> str | None` — all consumed by Task 3 and Task 4.

- [ ] **Step 1: Write the failing test**

Create `tests/test_pipeline_prompts.py`:

```python
"""Pure-function tests for prompt composition and result extraction — no
DB, no adapters, no network. These are the building blocks Task 3/4 wire
into the live pipeline."""

from mission_control.server.pipeline_prompts import (
    build_coder_prompt,
    build_orchestrator_prompt,
    build_reviewer_prompt,
    extract_claude_code_result_text,
)


def test_build_orchestrator_prompt_includes_goal():
    prompt = build_orchestrator_prompt("Add a health check endpoint")
    assert "Add a health check endpoint" in prompt
    assert "Orchestrator" in prompt


def test_build_coder_prompt_includes_goal_and_orchestrator_output():
    prompt = build_coder_prompt("Add a health check endpoint", "Plan: add GET /healthz")
    assert "Add a health check endpoint" in prompt
    assert "Plan: add GET /healthz" in prompt
    assert "Coder" in prompt


def test_build_reviewer_prompt_includes_goal_and_coder_output():
    prompt = build_reviewer_prompt("Add a health check endpoint", "Added GET /healthz returning 200")
    assert "Add a health check endpoint" in prompt
    assert "Added GET /healthz returning 200" in prompt
    assert "Reviewer" in prompt


def test_extract_claude_code_result_text_from_result_event():
    assert extract_claude_code_result_text("result", {"result": "OK", "is_error": False}) == "OK"


def test_extract_claude_code_result_text_ignores_non_result_events():
    assert extract_claude_code_result_text("assistant", {"result": "should not matter"}) is None


def test_extract_claude_code_result_text_handles_missing_field():
    assert extract_claude_code_result_text("result", {"is_error": True}) is None


def test_extract_claude_code_result_text_handles_empty_string():
    assert extract_claude_code_result_text("result", {"result": ""}) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pipeline_prompts.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mission_control.server.pipeline_prompts'`

- [ ] **Step 3: Write the implementation**

Create `src/mission_control/server/pipeline_prompts.py`:

```python
"""Pure functions for the agent pipeline: persona prompts, prompt
composition, and Claude Code result-text extraction. No I/O, no DB, no
adapter calls — kept separate from app.py's orchestration so these are
unit-testable without a running server or live API calls.

See docs/superpowers/specs/2026-08-27-agent-pipeline-design.md.
"""

from __future__ import annotations

from typing import Any

ORCHESTRATOR_PROMPT = (
    "You are the Orchestrator agent in a three-agent pipeline "
    "(Orchestrator -> Coder -> Reviewer). You do not write code yourself. "
    "Read the goal below and produce a concise, actionable plan for the "
    "Coder agent to follow. Be specific about what files or areas of the "
    "codebase are likely involved and what \"done\" looks like.\n\nGoal:\n"
)

CODER_PROMPT = (
    "You are the Coder agent in a three-agent pipeline "
    "(Orchestrator -> Coder -> Reviewer). The Orchestrator has produced a "
    "plan for the goal below. Implement it against the current workspace. "
    "When you finish, concisely report what you changed and why.\n\nGoal:\n"
)

REVIEWER_PROMPT = (
    "You are the Reviewer agent in a three-agent pipeline "
    "(Orchestrator -> Coder -> Reviewer). The Coder has reported the work "
    "below in response to the original goal. Critique it against the goal: "
    "does it actually achieve it? Note anything wrong, missing, or risky. "
    "End with a clear verdict: either \"Approved\" or \"Concerns:\" "
    "followed by specifics.\n\nGoal:\n"
)


def build_orchestrator_prompt(goal: str) -> str:
    return f"{ORCHESTRATOR_PROMPT}{goal}"


def build_coder_prompt(goal: str, orchestrator_result: str) -> str:
    return f"{CODER_PROMPT}{goal}\n\nOrchestrator's plan:\n{orchestrator_result}"


def build_reviewer_prompt(goal: str, coder_result: str) -> str:
    return f"{REVIEWER_PROMPT}{goal}\n\nCoder's work:\n{coder_result}"


def extract_claude_code_result_text(event_type: str, payload: dict[str, Any]) -> str | None:
    """Given one already-stored TaskEvent's (event_type, payload), return
    the final plain-text result if this is Claude Code's terminal `result`
    event, else None. Callers should call this for every event in a task's
    stream and keep the last non-None value — only the `result` event ever
    returns non-None, so the "last" one is also the only one."""
    if event_type != "result":
        return None
    result = payload.get("result")
    return result if isinstance(result, str) and result else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_pipeline_prompts.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/mission_control/server/pipeline_prompts.py tests/test_pipeline_prompts.py
git commit -m "feat: add pipeline prompt composition and result-text extraction"
```

---

### Task 3: Capture `result_text` in `_execute_task`/`_finish`

**Files:**
- Modify: `src/mission_control/server/app.py:161-253` (`_execute_task`, `_finish`)
- Test: `tests/test_finish_persists_result_text.py` (new)

**Interfaces:**
- Consumes: `extract_claude_code_result_text` from Task 2.
- Produces: `_finish(task_id, status, error_detail=None, result_text=None)` (new optional param, existing callers unaffected since it's keyword-optional).

- [ ] **Step 1: Write the failing test**

Create `tests/test_finish_persists_result_text.py`:

```python
"""Offline test for _finish()'s result_text persistence. Everything else
in app.py's _execute_task requires a live adapter/API call and is verified
manually per docs/superpowers/specs/2026-08-27-agent-pipeline-design.md.

get_session is a plain module-level name imported into app.py's namespace
(`from mission_control.server.db import get_session, ...`), so monkeypatching
mission_control.server.app.get_session replaces exactly what _finish() calls."""

import pytest
from sqlmodel import Session, SQLModel, create_engine

from mission_control.server import app as app_module
from mission_control.server.models import Mission, MissionTask, TaskStatus


@pytest.fixture
def isolated_db(monkeypatch):
    engine = create_engine("sqlite://")  # fresh in-memory DB per test
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(app_module, "get_session", lambda: Session(engine))
    return engine


def test_finish_persists_result_text(isolated_db):
    with app_module.get_session() as session:
        mission = Mission(name="m")
        session.add(mission)
        session.commit()
        session.refresh(mission)
        task = MissionTask(mission_id=mission.id, runtime="claude_code", prompt="p", workspace_path=".")
        session.add(task)
        session.commit()
        session.refresh(task)
        task_id = task.id

    app_module._finish(task_id, TaskStatus.SUCCEEDED, error_detail=None, result_text="OK")

    with app_module.get_session() as session:
        task = session.get(MissionTask, task_id)
        assert task.result_text == "OK"
        assert task.status == TaskStatus.SUCCEEDED


def test_finish_without_result_text_leaves_it_none(isolated_db):
    with app_module.get_session() as session:
        mission = Mission(name="m")
        session.add(mission)
        session.commit()
        session.refresh(mission)
        task = MissionTask(mission_id=mission.id, runtime="claude_code", prompt="p", workspace_path=".")
        session.add(task)
        session.commit()
        session.refresh(task)
        task_id = task.id

    app_module._finish(task_id, TaskStatus.FAILED, error_detail="boom")

    with app_module.get_session() as session:
        task = session.get(MissionTask, task_id)
        assert task.result_text is None
        assert task.error_detail == "boom"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_finish_persists_result_text.py -v`
Expected: FAIL with `TypeError: _finish() got an unexpected keyword argument 'result_text'`

- [ ] **Step 3: Update `_finish` and `_execute_task`**

In `src/mission_control/server/app.py`, add the import (near the other `mission_control.server` imports):

```python
from mission_control.server.pipeline_prompts import extract_claude_code_result_text
```

Replace the `_finish` function:

```python
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

In `_execute_task`, add `captured_result_text: str | None = None` right before the `saw_error = False` line, extract it inside the event loop, and pass it to the final `_finish()` call. Replace this block:

```python
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
```

with:

```python
    saw_error = False
    captured_result_text: str | None = None
    async for event in adapter.stream_events(handle):
        if event.error_family is not None:
            saw_error = True
        extracted = extract_claude_code_result_text(event.event_type, event.payload)
        if extracted is not None:
            captured_result_text = extracted
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
    _finish(
        task_id,
        final_status,
        error_detail=None if final_status == TaskStatus.SUCCEEDED else health.detail,
        result_text=captured_result_text if final_status == TaskStatus.SUCCEEDED else None,
    )
    await adapter.destroy(handle)
```

The other three `_finish(...)` call sites earlier in `_execute_task` (install-not-found, deploy-failure, send_task-not-implemented, ack-not-accepted) are unaffected — they already omit `result_text`, which now defaults to `None`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_finish_persists_result_text.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the full offline suite to check for regressions**

Run: `uv run pytest -q`
Expected: all prior tests still pass, plus these 2 new ones

- [ ] **Step 6: Commit**

```bash
git add src/mission_control/server/app.py tests/test_finish_persists_result_text.py
git commit -m "feat: capture and persist Claude Code result_text on task completion"
```

---

### Task 4: Pipeline orchestration + API routes

**Files:**
- Modify: `src/mission_control/server/app.py` (imports, two new routes, `_create_pipeline_task`, `_execute_pipeline`, `_get_task`)
- Test: `tests/test_pipeline_routes.py` (new)

**Interfaces:**
- Consumes: `AgentRole` (Task 1), `build_orchestrator_prompt`/`build_coder_prompt`/`build_reviewer_prompt` (Task 2), `_execute_task`/`_finish`/`get_session` (existing/Task 3).
- Produces: `POST /api/missions/{mission_id}/pipeline` → `{pipeline_run_id: str, orchestrator_task: MissionTask}`; `GET /api/missions/{mission_id}/pipelines` → `list[{pipeline_run_id: str, status: "running"|"succeeded"|"failed", tasks: list[MissionTask]}]`, used by Task 5's UI.

- [ ] **Step 1: Write the failing test**

Create `tests/test_pipeline_routes.py`:

```python
"""Offline tests for pipeline task creation and the run-status aggregation
in list_pipelines — an isolated in-memory DB, no live adapter/API calls.
The actual live 3-stage execution is verified manually (see the spec)."""

import pytest
from sqlmodel import Session, SQLModel, create_engine

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pipeline_routes.py -v`
Expected: FAIL with `AttributeError: module 'mission_control.server.app' has no attribute '_create_pipeline_task'`

- [ ] **Step 3: Write the implementation**

In `src/mission_control/server/app.py`:

Add `import uuid` to the top import block (alongside `asyncio`, `json`, `shutil`).

Add to the `mission_control.server.models` import line — change:
```python
from mission_control.server.models import Mission, MissionTask, TaskEvent, TaskStatus
```
to:
```python
from mission_control.server.models import AgentRole, Mission, MissionTask, TaskEvent, TaskStatus
```

Change the `pipeline_prompts` import line (added in Task 3) to also bring in the builders:
```python
from mission_control.server.pipeline_prompts import (
    build_coder_prompt,
    build_orchestrator_prompt,
    build_reviewer_prompt,
    extract_claude_code_result_text,
)
```

Append the following to the end of `app.py` (after `mission_log`):

```python
class RunPipelineRequest(BaseModel):
    goal: str
    workspace_path: str


@app.post("/api/missions/{mission_id}/pipeline")
async def run_pipeline(mission_id: str, req: RunPipelineRequest) -> dict:
    with get_session() as session:
        mission = session.get(Mission, mission_id)
        if mission is None:
            raise HTTPException(404, "mission not found")

    pipeline_run_id = str(uuid.uuid4())
    orchestrator_task_id = _create_pipeline_task(
        mission_id, pipeline_run_id, AgentRole.ORCHESTRATOR,
        build_orchestrator_prompt(req.goal), req.workspace_path,
    )
    asyncio.create_task(
        _execute_pipeline(pipeline_run_id, mission_id, req.goal, req.workspace_path, orchestrator_task_id)
    )

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
    await _execute_task(orchestrator_task_id)
    orchestrator = _get_task(orchestrator_task_id)
    if orchestrator is None or orchestrator.status != TaskStatus.SUCCEEDED or not orchestrator.result_text:
        return  # pipeline halts here; coder/reviewer tasks are never created

    coder_task_id = _create_pipeline_task(
        mission_id, pipeline_run_id, AgentRole.CODER,
        build_coder_prompt(goal, orchestrator.result_text), workspace_path,
    )
    await _execute_task(coder_task_id)
    coder = _get_task(coder_task_id)
    if coder is None or coder.status != TaskStatus.SUCCEEDED or not coder.result_text:
        return

    reviewer_task_id = _create_pipeline_task(
        mission_id, pipeline_run_id, AgentRole.REVIEWER,
        build_reviewer_prompt(goal, coder.result_text), workspace_path,
    )
    await _execute_task(reviewer_task_id)


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
        if any(s == TaskStatus.FAILED for s in statuses):
            overall = "failed"
        elif any(s in (TaskStatus.PENDING, TaskStatus.RUNNING) for s in statuses):
            overall = "running"
        elif len(run_tasks) == 3 and statuses[-1] == TaskStatus.SUCCEEDED:
            overall = "succeeded"
        else:
            overall = "running"
        out.append({"pipeline_run_id": run_id, "status": overall, "tasks": run_tasks})

    out.sort(key=lambda r: r["tasks"][0].created_at, reverse=True)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_pipeline_routes.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full offline suite to check for regressions**

Run: `uv run pytest -q`
Expected: all prior tests still pass, plus these 4 new ones

- [ ] **Step 6: Verify the app still imports and routes register**

Run: `uv run python -c "from mission_control.server.app import app; print([r.path for r in app.routes if hasattr(r,'path')])"`
Expected: the printed list includes `/api/missions/{mission_id}/pipeline` and `/api/missions/{mission_id}/pipelines`

- [ ] **Step 7: Commit**

```bash
git add src/mission_control/server/app.py tests/test_pipeline_routes.py
git commit -m "feat: add agent pipeline orchestration and API routes"
```

---

### Task 5: Dashboard UI — pipeline form, agent tabs, communication feed

**Files:**
- Modify: `src/mission_control/server/static/dashboard.html`

**Interfaces:**
- Consumes: `POST /api/missions/{id}/pipeline`, `GET /api/missions/{id}/pipelines` (Task 4); reuses the existing `selectTask(taskId)` and `watchTranscript(taskId)` functions already defined in this file — a pipeline role IS a `MissionTask`, so clicking its tab just calls `selectTask` exactly like clicking a row in the existing flat task list does.

No JS test runner exists in this project. Verification for this task is: (a) a Node syntax check of the embedded script, (b) a cross-check that every new `getElementById` call has a matching `id=` in the HTML, (c) a manual browser check — both techniques were already used and worked for the previous dashboard build in this project.

- [ ] **Step 1: Add the pipeline form and Agents panel markup**

In `src/mission_control/server/static/dashboard.html`, insert this block between the closing `</form>` of `#taskForm` and the `<div id="detailGrid">` line:

```html
  <form class="inline" id="pipelineForm">
    <input type="text" name="goal" placeholder="Pipeline goal (Orchestrator -> Coder -> Reviewer)" required />
    <input type="text" name="workspace_path" placeholder="Workspace path" value="." required />
    <button type="submit">Run agent pipeline</button>
  </form>

  <div class="panel" id="pipelinesPanel" style="margin-bottom:0.6rem;">
    <h2>Agents <span class="count" id="pipelinesCount"></span></h2>
    <div id="pipelineRuns"><span class="t-empty">no pipeline runs yet</span></div>
    <div id="commFeed" style="margin-top:0.6rem;"></div>
  </div>
```

- [ ] **Step 2: Add CSS for agent tabs**

In the `<style>` block, add after the existing `.task-item` rules (right before the `#logList` rules):

```css
  .agent-tab { background: transparent; color: var(--dim); border: 1px solid var(--border);
               border-radius: 3px; padding: 0.25rem 0.6rem; cursor: pointer; font-weight: normal; }
  .agent-tab.selected { border-color: var(--accent); color: var(--white); }
  .agent-tab:hover { border-color: var(--border-lit); }
  .comm-line { color: var(--dim); padding: 0.15rem 0; font-size: 0.88em; }
  .comm-line b { color: var(--white); font-weight: normal; }
```

- [ ] **Step 3: Add the JS logic**

In the `<script>` block, add this submit handler right after the existing `taskForm` submit handler (after its closing `});`):

```javascript
document.getElementById("pipelineForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  await fetchJSON(`/api/missions/${state.currentMissionId}/pipeline`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(Object.fromEntries(fd)),
  });
  e.target.querySelector('[name=goal]').value = "";
  await loadPipelines();
});

async function loadPipelines() {
  if (!state.currentMissionId) return;
  const runs = await fetchJSON(`/api/missions/${state.currentMissionId}/pipelines`);
  document.getElementById("pipelinesCount").textContent = runs.length;
  const container = document.getElementById("pipelineRuns");
  const feed = document.getElementById("commFeed");
  container.innerHTML = runs.length ? "" : '<span class="t-empty">no pipeline runs yet</span>';
  feed.innerHTML = "";

  const icons = { succeeded: "✓", failed: "✗", running: "●", pending: "○", unsupported: "–" };

  for (const run of runs) {
    const tabs = document.createElement("div");
    tabs.style.display = "flex";
    tabs.style.gap = "0.4rem";
    tabs.style.marginBottom = "0.4rem";
    for (const task of run.tasks) {
      const tab = document.createElement("button");
      tab.className = "agent-tab" + (task.id === state.currentTaskId ? " selected" : "");
      tab.textContent = `${icons[task.status] || "?"} ${task.role}`;
      tab.addEventListener("click", () => selectTask(task.id));
      tabs.appendChild(tab);
    }
    container.appendChild(tabs);

    for (let i = 0; i < run.tasks.length; i++) {
      const from = run.tasks[i];
      const to = run.tasks[i + 1] ? run.tasks[i + 1].role : "done";
      const working = from.status === "running" || from.status === "pending";
      const text = (from.result_text || (working ? "…working…" : from.error_detail || "")).slice(0, 160);
      const line = document.createElement("div");
      line.className = "comm-line";
      line.innerHTML = `<b>${from.role} \u2192 ${to}:</b> ${escapeHtml(text)}`;
      feed.appendChild(line);
    }
  }
}
```

Then wire `loadPipelines()` into the existing refresh cycle by calling it inside `refreshDetail()`. Find this line near the end of `refreshDetail()`:

```javascript
  window._detailTasks = tasks;
}
```

and change it to:

```javascript
  window._detailTasks = tasks;
  await loadPipelines();
}
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

- [ ] **Step 5: Cross-check every `getElementById` reference resolves to a real element**

Run:
```bash
python -c "
import re
html = open('src/mission_control/server/static/dashboard.html', encoding='utf-8').read()
referenced = set(re.findall(r'getElementById\(\"([^\"]+)\"\)', html))
declared = set(re.findall(r'id=\"([^\"]+)\"', html))
missing = referenced - declared
print('MISSING:', missing if missing else 'none')
"
```
Expected: `MISSING: none`

- [ ] **Step 6: Manual browser verification**

Restart the server (see Task 6 for the exact restart procedure — killing by port ownership, not by a saved launcher PID, per the process-management gotcha already documented in the architecture doc). Open `http://127.0.0.1:8420`, go into an existing mission, and confirm:
- The "Run agent pipeline" form appears above the Agents panel, next to the existing ad-hoc task form.
- Submitting a goal creates an Orchestrator tab that goes ● running, then ✓ succeeded.
- The Communication Feed shows `orchestrator → coder: <text>` once the Orchestrator finishes.
- Clicking each tab shows that role's own transcript in the existing Active Worker panel (same rendering as the flat task list already had).

- [ ] **Step 7: Commit**

```bash
git add src/mission_control/server/static/dashboard.html
git commit -m "feat: add agent pipeline UI (tabs + communication feed) to dashboard"
```

---

### Task 6: Manual end-to-end live pipeline verification

**Files:** none (verification only, no code changes)

This is real, cost-incurring Claude Code usage (3 live calls per pipeline run) — intentionally not part of the automated `pytest` suite, per the spec's testing section.

- [ ] **Step 1: Restart the server with the final code**

Kill by actual port ownership (not a saved PID — `Start-Process` on `uv` tracks the wrapper, not the child; this project now launches the venv's `python.exe` directly to avoid that):

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
Expected: `{"status":"ok","runtimes":{...}}` with `"claude_code":true`

- [ ] **Step 3: Create a mission and run a real pipeline**

```bash
MISSION=$(curl -s -X POST http://127.0.0.1:8420/api/missions -H "Content-Type: application/json" -d '{"name":"pipeline-test"}')
MISSION_ID=$(echo "$MISSION" | python -c "import sys,json;print(json.load(sys.stdin)['id'])")
curl -s -X POST "http://127.0.0.1:8420/api/missions/$MISSION_ID/pipeline" -H "Content-Type: application/json" \
  -d '{"goal":"Add a one-line docstring to mission_control/__init__.py explaining what the package is.","workspace_path":"D:/Tools/MissionControl"}'
```

- [ ] **Step 4: Poll until all three stages complete**

```bash
sleep 30
curl -s "http://127.0.0.1:8420/api/missions/$MISSION_ID/pipelines" | python -m json.tool
```

Expected: one pipeline run with `"status": "succeeded"` and exactly 3 tasks (`orchestrator`, `coder`, `reviewer`, in that order), each with a non-empty `result_text` and `status: "succeeded"`.

- [ ] **Step 5: Confirm the halt-on-failure behavior with a deliberately bad workspace**

```bash
curl -s -X POST "http://127.0.0.1:8420/api/missions/$MISSION_ID/pipeline" -H "Content-Type: application/json" \
  -d '{"goal":"test","workspace_path":"D:/does/not/exist"}'
sleep 5
curl -s "http://127.0.0.1:8420/api/missions/$MISSION_ID/pipelines" | python -m json.tool
```

Expected: the newest pipeline run shows `"status": "failed"` with exactly 1 task (orchestrator — since a nonexistent workspace fails `deploy()`, coder/reviewer are never created).

- [ ] **Step 6: No commit for this task** (verification only — if any step above fails, go back to the relevant earlier task, fix, and re-commit there).
