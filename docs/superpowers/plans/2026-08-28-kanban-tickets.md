# Agent-Driven Kanban Board Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each mission a lightweight kanban board (backlog/todo/doing/done) that Orchestrator/Coder/Reviewer agents populate themselves via a local REST API, visible read-only on the dashboard.

**Architecture:** Two new SQLModel tables (`Ticket`, `TicketComment`) with a thin REST CRUD layer in `app.py`; the three pipeline persona prompts gain a short paragraph telling agents how to call that API with `curl` (their existing Bash tool); the dashboard adds a 4-column read-only board refreshed on the same poll cadence it already uses for everything else in the mission-detail view.

**Tech Stack:** FastAPI, SQLModel/SQLite, vanilla JS (no build step) — same stack as the rest of Mission Control, no new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-28-kanban-tickets-design.md`

## Global Constraints

- Native to Mission Control — no Trello or other external service integration.
- Layered on top of the existing fixed Orchestrator → Coder → Reviewer pipeline. The pipeline's actual control flow (the plain-text plan/result handoff between stages) is not read from or written to by anything in this plan.
- Agents interact with tickets via `curl` against the new local REST API, from their existing Bash tool — no new MCP tooling, no free-text parsing of agent output.
- Read-only UI for v1 — no drag-and-drop, no manual editing by the human user.
- Only pipeline-created tasks (Orchestrator/Coder/Reviewer) get kanban instructions injected into their prompts. Ad-hoc tasks (`POST /api/missions/{id}/tasks`) are unchanged.
- No authentication on any new route — matches every existing route in this local, single-user tool.
- Ticket `position` is server-assigned and append-only within a column; there is no explicit re-ordering endpoint.
- The API base port is `127.0.0.1:8420`, hardcoded — matches how `src/mission_control/server/__main__.py` already hardcodes it for `uvicorn.run`.

## Planning deviation from the spec (read before implementing Task 4)

The spec called for a dedicated `setInterval` polling the ticket list every 2 seconds. While writing this plan, a simpler fit was found: `dashboard.html` already has one global 4-second interval (`setInterval(() => { state.currentMissionId ? refreshDetail() : loadMissions(); }, 4000);`) that drives every other live element in the mission-detail view (`refreshDetail()` already calls `loadPipelines()` at its end). Task 4 adds `loadTickets()` as one more call at the end of `refreshDetail()`, riding the same cadence and the same trigger points (mission open, task select, message send, etc.) instead of introducing a second, separately-managed interval with its own start/stop lifecycle. This keeps the "live, refreshes on its own" intent of the spec with less code and no new cleanup path to get wrong.

---

### Task 1: Ticket data model

**Files:**
- Modify: `src/mission_control/server/models.py`
- Test: `tests/test_ticket_models.py`

**Interfaces:**
- Consumes: the existing `_uuid`, `_now` helpers and `Field`/`SQLModel`/`StrEnum` imports already at the top of `models.py` — nothing new to import there.
- Produces: `TicketColumn` (`StrEnum`: `BACKLOG`, `TODO`, `DOING`, `DONE`), `Ticket`, `TicketComment` — used by every later task.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ticket_models.py
"""Pure model-level tests for the new kanban tables — no DB I/O, matching
the pattern of testing SQLModel defaults directly on an instance."""

from mission_control.server.models import Ticket, TicketColumn, TicketComment


def test_ticket_defaults_to_backlog_column_and_position_zero():
    ticket = Ticket(mission_id="m1", title="Do the thing")
    assert ticket.column == TicketColumn.BACKLOG
    assert ticket.position == 0
    assert ticket.description == ""
    assert ticket.created_by_role is None


def test_ticket_table_name_is_lowercase_class_name():
    # Later tasks' foreign_key="ticket.id" strings depend on this exact name,
    # the same convention already used for mission.id/missiontask.id.
    assert Ticket.__tablename__ == "ticket"


def test_ticket_comment_defaults():
    comment = TicketComment(ticket_id="t1", text="looks good")
    assert comment.author_role is None
    assert comment.text == "looks good"
    assert TicketComment.__tablename__ == "ticketcomment"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ticket_models.py -v`
Expected: FAIL with `ImportError: cannot import name 'Ticket'` (or `TicketColumn`/`TicketComment`).

- [ ] **Step 3: Add the models**

In `src/mission_control/server/models.py`, add after the existing `TaskEvent` class (end of file):

```python
class TicketColumn(StrEnum):
    BACKLOG = "backlog"
    TODO = "todo"
    DOING = "doing"
    DONE = "done"


class Ticket(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    mission_id: str = Field(foreign_key="mission.id", index=True)
    title: str
    description: str = Field(default="")
    column: TicketColumn = Field(default=TicketColumn.BACKLOG)
    created_by_role: str | None = Field(default=None)
    position: int = Field(default=0)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class TicketComment(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    ticket_id: str = Field(foreign_key="ticket.id", index=True)
    author_role: str | None = Field(default=None)
    text: str
    created_at: datetime = Field(default_factory=_now)
```

No changes are needed in `db.py`. These are brand-new tables, not new columns on an existing table — `SQLModel.metadata.create_all(engine)` (already called by `init_db()`) creates them automatically once they're defined here and `models.py` has been imported (it already is, by `app.py`, before `init_db()` runs in the lifespan handler). The `_migrate_add_columns`/`_NEW_COLUMNS` machinery in `db.py` is only for adding columns to a table that already exists — do not touch it for this task.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ticket_models.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/mission_control/server/models.py tests/test_ticket_models.py
git commit -m "feat: add Ticket/TicketComment kanban data model"
```

---

### Task 2: Ticket REST API

**Files:**
- Modify: `src/mission_control/server/app.py`
- Test: `tests/test_ticket_routes.py`

**Interfaces:**
- Consumes: `Ticket`, `TicketColumn`, `TicketComment` from Task 1; the existing `Mission`, `get_session`, `select`, `HTTPException`, `BaseModel` already imported in `app.py`.
- Produces: `CreateTicketRequest`, `UpdateTicketRequest`, `CreateCommentRequest` (Pydantic request models); route functions `create_ticket`, `list_tickets`, `update_ticket`, `get_ticket`, `add_comment` — Task 3 does not call these directly (it only builds prompt text), but Task 4's dashboard JS calls their HTTP routes by URL, so the exact paths below must match verbatim: `POST/GET /api/missions/{mission_id}/tickets`, `PATCH/GET /api/tickets/{ticket_id}`, `POST /api/tickets/{ticket_id}/comments`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ticket_routes.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ticket_routes.py -v`
Expected: FAIL with `AttributeError: module 'mission_control.server.app' has no attribute 'create_ticket'`.

- [ ] **Step 3: Add the imports**

In `src/mission_control/server/app.py`, change the existing models import (currently `from mission_control.server.models import AgentRole, Mission, MissionTask, TaskEvent, TaskStatus`) to:

```python
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
```

Add a new top-level import (this file currently has no `datetime` import at all):

```python
from datetime import UTC, datetime
```

- [ ] **Step 4: Add the request models**

Add near the existing `SendMessageRequest` class:

```python
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
```

- [ ] **Step 5: Add the routes**

Append to the end of `app.py` (after `list_pipelines`):

```python
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
```

Note the `_next_ticket_position` call in `update_ticket` reads `ticket.column` (the *old* column) in its own comparison just above, then reassigns `ticket.column = req.column` immediately after computing the new position — order matters here: compute the position for the *new* column before overwriting `ticket.column`, otherwise `_next_ticket_position` would count the ticket being moved as already being in its destination column if the session had auto-flushed. As written above, the position is computed first, then the column is assigned, so this is safe.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_ticket_routes.py -v`
Expected: PASS (11 tests)

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest`
Expected: PASS (all previous tests still pass — this task only adds new routes, touches no existing route or function)

- [ ] **Step 8: Commit**

```bash
git add src/mission_control/server/app.py tests/test_ticket_routes.py
git commit -m "feat: add ticket CRUD + comment API for the kanban board"
```

---

### Task 3: Agent integration — kanban instructions in pipeline prompts

**Files:**
- Modify: `src/mission_control/server/pipeline_prompts.py`
- Modify: `src/mission_control/server/app.py:437,496,506` (the three `build_*_prompt` call sites)
- Test: `tests/test_pipeline_prompts.py`

**Interfaces:**
- Consumes: nothing from Tasks 1-2 (this task only builds prompt text; it never imports `Ticket`/routes).
- Produces: `build_orchestrator_prompt(goal: str, mission_id: str) -> str`, `build_coder_prompt(goal: str, orchestrator_result: str, mission_id: str) -> str`, `build_reviewer_prompt(goal: str, coder_result: str, mission_id: str) -> str` — each function's signature gains a required `mission_id` parameter. Every existing caller of these three functions must be updated in the same commit or the app will not import.

- [ ] **Step 1: Update the failing tests**

Replace the contents of `tests/test_pipeline_prompts.py` with:

```python
"""Pure-function tests for prompt composition and result extraction — no
DB, no adapters, no network. These are the building blocks the pipeline
wires into the live pipeline."""

from mission_control.server.pipeline_prompts import (
    build_coder_prompt,
    build_orchestrator_prompt,
    build_reviewer_prompt,
    extract_claude_code_result_text,
)


def test_build_orchestrator_prompt_includes_goal():
    prompt = build_orchestrator_prompt("Add a health check endpoint", "mission-1")
    assert "Add a health check endpoint" in prompt
    assert "Orchestrator" in prompt


def test_build_coder_prompt_includes_goal_and_orchestrator_output():
    prompt = build_coder_prompt("Add a health check endpoint", "Plan: add GET /healthz", "mission-1")
    assert "Add a health check endpoint" in prompt
    assert "Plan: add GET /healthz" in prompt
    assert "Coder" in prompt


def test_build_reviewer_prompt_includes_goal_and_coder_output():
    prompt = build_reviewer_prompt("Add a health check endpoint", "Added GET /healthz returning 200", "mission-1")
    assert "Add a health check endpoint" in prompt
    assert "Added GET /healthz returning 200" in prompt
    assert "Reviewer" in prompt


def test_build_orchestrator_prompt_includes_kanban_instructions_for_its_mission():
    prompt = build_orchestrator_prompt("Add a health check endpoint", "mission-42")
    assert "/api/missions/mission-42/tickets" in prompt


def test_build_coder_prompt_includes_kanban_instructions_for_its_mission():
    prompt = build_coder_prompt("goal", "plan", "mission-42")
    assert "/api/missions/mission-42/tickets" in prompt


def test_build_reviewer_prompt_includes_kanban_instructions_for_its_mission():
    prompt = build_reviewer_prompt("goal", "work", "mission-42")
    assert "/api/missions/mission-42/tickets" in prompt


def test_extract_claude_code_result_text_from_result_event():
    assert extract_claude_code_result_text("result", {"result": "OK", "is_error": False}) == "OK"


def test_extract_claude_code_result_text_ignores_non_result_events():
    assert extract_claude_code_result_text("assistant", {"result": "should not matter"}) is None


def test_extract_claude_code_result_text_handles_missing_field():
    assert extract_claude_code_result_text("result", {"is_error": True}) is None


def test_extract_claude_code_result_text_handles_empty_string():
    assert extract_claude_code_result_text("result", {"result": ""}) is None


def test_extract_claude_code_result_text_treats_is_error_result_as_failure():
    """An error_max_turns-style result can still carry a `result` string and
    exit 0 — is_error: true must still mean 'no result_text', per the spec's
    'on failure, result_text stays None'."""
    assert extract_claude_code_result_text(
        "result", {"type": "result", "is_error": True, "result": "Reached max turns"}
    ) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_pipeline_prompts.py -v`
Expected: FAIL — `build_orchestrator_prompt() takes 1 positional argument but 2 were given` (and similarly for coder/reviewer).

- [ ] **Step 3: Update `pipeline_prompts.py`**

In `src/mission_control/server/pipeline_prompts.py`, replace the three `build_*_prompt` functions at the bottom with:

```python
def kanban_instructions(mission_id: str) -> str:
    base = f"http://127.0.0.1:8420/api/missions/{mission_id}/tickets"
    return (
        "\n\nYou have a shared kanban board for this mission, visible to "
        "the user and to the other agents in this pipeline. Use it to "
        "break work into tickets and show progress. You have network "
        "access to Mission Control's own local API via curl:\n"
        f"- Create a ticket: curl -s -X POST '{base}?author_role=<your-role>' "
        "-H 'Content-Type: application/json' "
        '-d \'{"title": "...", "description": "..."}\'\n'
        "- Move a ticket (column is one of backlog/todo/doing/done): "
        "curl -s -X PATCH 'http://127.0.0.1:8420/api/tickets/<ticket_id>' "
        "-H 'Content-Type: application/json' -d '{\"column\": \"doing\"}'\n"
        "- Comment on a ticket: "
        "curl -s -X POST "
        "'http://127.0.0.1:8420/api/tickets/<ticket_id>/comments?author_role=<your-role>' "
        "-H 'Content-Type: application/json' -d '{\"text\": \"...\"}'\n"
        "This is optional but encouraged — use it to make your work "
        "visible, not as a substitute for your actual output."
    )


def build_orchestrator_prompt(goal: str, mission_id: str) -> str:
    return f"{ORCHESTRATOR_PROMPT}{goal}{kanban_instructions(mission_id)}"


def build_coder_prompt(goal: str, orchestrator_result: str, mission_id: str) -> str:
    return f"{CODER_PROMPT}{goal}\n\nOrchestrator's plan:\n{orchestrator_result}{kanban_instructions(mission_id)}"


def build_reviewer_prompt(goal: str, coder_result: str, mission_id: str) -> str:
    return f"{REVIEWER_PROMPT}{goal}\n\nCoder's work:\n{coder_result}{kanban_instructions(mission_id)}"
```

- [ ] **Step 4: Update the three call sites in `app.py`**

`app.py:437`, inside `_start_pipeline` (which already has `mission_id` as a parameter), change:

```python
        build_orchestrator_prompt(goal), workspace_path,
```

to:

```python
        build_orchestrator_prompt(goal, mission_id), workspace_path,
```

`app.py:496`, inside `_execute_pipeline` (which already has `mission_id` as a parameter), change:

```python
            build_coder_prompt(goal, orchestrator.result_text), workspace_path,
```

to:

```python
            build_coder_prompt(goal, orchestrator.result_text, mission_id), workspace_path,
```

`app.py:506`, same function, change:

```python
            build_reviewer_prompt(goal, coder.result_text), workspace_path,
```

to:

```python
            build_reviewer_prompt(goal, coder.result_text, mission_id), workspace_path,
```

(Line numbers are as of this plan's authoring — if Tasks 1-2's edits shifted them, find the call sites by the `build_orchestrator_prompt`/`build_coder_prompt`/`build_reviewer_prompt` text instead of trusting the exact numbers.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_pipeline_prompts.py -v`
Expected: PASS (11 tests)

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest`
Expected: PASS. This is the step that proves the three call-site updates were both necessary and sufficient — if any call site were missed, `app.py` would fail to even import (wrong argument count), and every test in the suite would error at collection.

- [ ] **Step 7: Commit**

```bash
git add src/mission_control/server/pipeline_prompts.py src/mission_control/server/app.py tests/test_pipeline_prompts.py
git commit -m "feat: give pipeline agents kanban-board instructions in their prompts"
```

---

### Task 4: Dashboard kanban panel

**Files:**
- Modify: `src/mission_control/server/static/dashboard.html`

**Interfaces:**
- Consumes: Task 2's exact routes — `GET /api/missions/{mission_id}/tickets` (returns a list of ticket objects: `id`, `title`, `description`, `column`, `created_by_role`, `position`, `created_at`, `updated_at`) and `GET /api/tickets/{ticket_id}` (same fields plus `comments`, a list of `{id, ticket_id, author_role, text, created_at}`).
- Produces: nothing consumed by a later task — this is the last task in the plan.

No automated test exists for this file (plain JS, no build step, no test runner configured in this project — the same situation Task 5 of the prior chat/interrupt plan was in). Verify by extracting the `<script>` block and running `node --check` on it, then a manual check against a running server (Step 5 below).

- [ ] **Step 1: Add the panel markup**

In `src/mission_control/server/static/dashboard.html`, between the existing `#pipelinesPanel` div and `#detailGrid` div (currently around lines 198-203), insert:

```html
  <div class="panel" id="kanbanPanel" style="margin-bottom:0.6rem;">
    <h2>Kanban <span class="count" id="kanbanCount"></span></h2>
    <div id="kanbanBoard"><span class="t-empty">no tickets yet</span></div>
  </div>
```

- [ ] **Step 2: Add the CSS**

Add near the existing `.pipeline-run`/`.agent-tab` rules (around line 96, after the `.agent-tab .icon.*` block):

```css
  #kanbanBoard { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.6rem; }
  .kanban-col { display: flex; flex-direction: column; gap: 0.4rem; }
  .kanban-col-header { color: var(--dim); text-transform: uppercase; font-size: 0.75em; letter-spacing: 0.05em;
                        border-bottom: 1px solid var(--border); padding-bottom: 0.3rem; margin-bottom: 0.2rem; }
  .ticket-card { border: 1px solid var(--border); border-radius: 3px; padding: 0.4rem 0.55rem; cursor: pointer;
                 font-size: 0.9em; }
  .ticket-card:hover { border-color: var(--border-lit); background: var(--panel); }
  .ticket-card .title { color: var(--text); }
  .ticket-card .meta { color: var(--dimmer); font-size: 0.85em; margin-top: 0.2rem; }
  .ticket-card .role-tag { text-transform: capitalize; }
  .ticket-detail { border: 1px solid var(--border-lit); border-radius: 3px; padding: 0.6rem 0.7rem;
                   margin-top: 0.4rem; grid-column: 1 / -1; }
  .ticket-detail .desc { color: var(--text); margin-bottom: 0.5rem; white-space: pre-wrap; }
  .ticket-comment { color: var(--dim); padding: 0.15rem 0; font-size: 0.88em; }
  .ticket-comment b { color: var(--white); font-weight: normal; text-transform: capitalize; }
```

(`#kanbanBoard`'s `grid-template-columns` is set here in CSS; the JS in Step 3 does not need to set it inline — that avoids the JS and CSS fighting over the same property.)

- [ ] **Step 3: Add the rendering JS**

Add after the existing `loadPipelines()` function (around line 441):

```javascript
async function loadTickets() {
  if (!state.currentMissionId) return;
  const tickets = await fetchJSON(`/api/missions/${state.currentMissionId}/tickets`);
  document.getElementById("kanbanCount").textContent = tickets.length;
  const board = document.getElementById("kanbanBoard");
  if (!tickets.length) { board.innerHTML = '<span class="t-empty">no tickets yet</span>'; return; }

  board.innerHTML = "";
  for (const col of ["backlog", "todo", "doing", "done"]) {
    const colDiv = document.createElement("div");
    colDiv.className = "kanban-col";
    const colTickets = tickets.filter(t => t.column === col);
    const header = document.createElement("div");
    header.className = "kanban-col-header";
    header.textContent = `${col} (${colTickets.length})`;
    colDiv.appendChild(header);
    for (const ticket of colTickets) {
      const card = document.createElement("div");
      card.className = "ticket-card";
      card.innerHTML = `<div class="title">${escapeHtml(ticket.title)}</div>` +
        (ticket.created_by_role ? `<div class="meta role-tag">${escapeHtml(ticket.created_by_role)}</div>` : "");
      card.addEventListener("click", () => toggleTicketDetail(ticket.id));
      colDiv.appendChild(card);
    }
    board.appendChild(colDiv);
  }
  if (state.openTicketId) await renderTicketDetail(state.openTicketId);
}

async function toggleTicketDetail(ticketId) {
  state.openTicketId = state.openTicketId === ticketId ? null : ticketId;
  document.getElementById("ticketDetail")?.remove();
  if (state.openTicketId) await renderTicketDetail(state.openTicketId);
}

async function renderTicketDetail(ticketId) {
  const ticket = await fetchJSON(`/api/tickets/${ticketId}`);
  document.getElementById("ticketDetail")?.remove();
  const detail = document.createElement("div");
  detail.id = "ticketDetail";
  detail.className = "ticket-detail";
  const comments = ticket.comments.map(c =>
    `<div class="ticket-comment"><b>${escapeHtml(c.author_role || "?")}:</b> ${escapeHtml(c.text)}</div>`
  ).join("");
  detail.innerHTML = `<div class="desc">${escapeHtml(ticket.description || "(no description)")}</div>${comments}`;
  document.getElementById("kanbanBoard").appendChild(detail);
}
```

- [ ] **Step 4: Wire it into the existing refresh cycle**

`state` is declared near the top of the `<script>` block as `const state = { missions: [], currentMissionId: null, currentTaskId: null, eventSource: null };` — add `openTicketId: null` to that object literal.

In `refreshDetail()`, the last line is `await loadPipelines();` — add `loadTickets()` right after it:

```javascript
  await loadPipelines();
  await loadTickets();
```

In `closeMission()`, add `state.openTicketId = null;` alongside the existing `state.currentTaskId = null;` line, so switching missions doesn't carry over a stale open ticket detail panel.

- [ ] **Step 5: Verify**

Extract and syntax-check the script block:

```bash
python -c "
import re
content = open('src/mission_control/server/static/dashboard.html', encoding='utf-8').read()
m = re.search(r'<script>(.*)</script>', content, re.S)
open('_dash_check.js', 'w', encoding='utf-8').write(m.group(1))
"
node --check _dash_check.js
```

Expected: no output (syntax OK). Delete `_dash_check.js` afterward.

Then, against a running server (restart it first so it picks up all of Tasks 1-4): create a mission, run the agent pipeline on a real goal, and confirm at least one ticket appears on the new Kanban panel within one refresh cycle (≤4 seconds), and that clicking a card shows its description/comments.

- [ ] **Step 6: Commit**

```bash
git add src/mission_control/server/static/dashboard.html
git commit -m "feat: add read-only kanban board panel to the dashboard"
```
