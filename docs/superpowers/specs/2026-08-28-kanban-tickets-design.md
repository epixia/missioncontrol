# Agent-Driven Kanban Board — Design

Status: approved via brainstorming, ready for implementation planning
Date: 2026-08-28

## Purpose

Today, coordination between the pipeline's agents (Orchestrator, Coder,
Reviewer) is invisible except through the free-text handoff already passed
between stages and the transcript feed. This spec adds a lightweight,
per-mission kanban board — `backlog` / `todo` / `doing` / `done` — that any
agent running as part of a mission can create tickets on, move across
columns, and comment on, giving a live, structured view of what's being
worked on. It is a visualization and coordination aid, not a new execution
engine.

## Decisions made during brainstorming (do not re-litigate without cause)

- **Native to Mission Control, no Trello.** The board's source of truth is
  Mission Control's own SQLite DB — no external API, no credentials, works
  offline. (The user's original ask mentioned Trello; after discussion, a
  custom board was chosen instead. Trello sync, if ever wanted, is a
  separate future feature, not part of this spec.)
- **Layered on top of the existing fixed pipeline, not a replacement.** The
  Orchestrator → Coder → Reviewer sequence, and the plain-text
  plan/result handoff between them, is completely unchanged by this spec.
  Tickets are something agents *additionally* create/move/comment on during
  their run, purely for visibility and lightweight coordination — the
  pipeline's actual control flow does not read from or depend on the ticket
  board in any way. A more autonomous, ticket-driven multi-agent mode (agents
  self-organizing without a fixed sequence) was explicitly considered and
  rejected as out of scope — it would need its own scheduler and is a
  candidate for a later, separate sub-project.
- **Agents interact via `curl` against a local REST API**, using their
  existing Bash tool — not a new MCP tool/server, not free-text parsing.
  This matches how every other Mission Control ↔ agent integration in this
  codebase works today (prompt engineering + a local API), and needs no new
  plumbing in the Claude Code CLI invocation.
- **Read-only UI for v1.** The board reflects what agents do; there is no
  drag-and-drop or manual editing by the human user in this spec. This
  matches the existing pipeline transcript view, which is also read-only.
  Manual editing can be added later if it turns out to be needed.
- **Scope: pipeline-created tasks only.** The Orchestrator/Coder/Reviewer
  persona prompts get kanban instructions; ad-hoc tasks (created via `POST
  /api/missions/{id}/tasks` with a raw prompt, no persona wrapper) do not.
  Nothing stops a user from telling an ad-hoc task's prompt to also use the
  API, but Mission Control does not proactively instruct it to.

## Data model

Two new tables, same SQLModel/SQLite pattern as `MissionTask`/`TaskEvent`,
added via the existing idempotent `db.py::_migrate_add_columns`-style
migration for any new columns added later, and a new
`_CREATE_TABLE`-equivalent step (see `db.py`'s existing table-creation code
for `MissionTask`/`TaskEvent` — new tables follow the same
`SQLModel.metadata.create_all` path, so no custom migration code is needed
for their initial creation; only *later* column additions to these tables
would need the `_migrate_add_columns` treatment):

Matches the exact style already in `models.py` — `StrEnum`, the existing
`_uuid`/`_now` factory helpers (not inline lambdas), string primary/foreign
keys:

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
    created_by_role: str | None = Field(default=None)  # "orchestrator" | "coder" | "reviewer" | None
    position: int = Field(default=0)  # ordering within its column, ascending
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class TicketComment(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    ticket_id: str = Field(foreign_key="ticket.id", index=True)
    author_role: str | None = Field(default=None)
    text: str
    created_at: datetime = Field(default_factory=_now)
```

`mission_id` references the mission (not a specific task), so tickets are
shared across every task/pipeline-stage in that mission — any pipeline run
within a mission sees and can add to the same board.

`position` exists so column ordering is stable and explicit (new tickets in
a column append at the end: `position = max existing position in that
column + 1`, computed server-side on create) rather than relying on
insertion order or `created_at` sort, which would silently reorder if two
tickets are created in the same millisecond.

## API

New routes in `app.py`, no authentication — same local, single-user trust
model as every existing route.

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


@app.post("/api/missions/{mission_id}/tickets", response_model=Ticket)
def create_ticket(mission_id: str, req: CreateTicketRequest, author_role: str | None = None) -> Ticket: ...
    # 404 if mission doesn't exist. position = (max position in that
    # mission+column) + 1, or 0 if none yet.

@app.get("/api/missions/{mission_id}/tickets", response_model=list[Ticket])
def list_tickets(mission_id: str) -> list[Ticket]: ...
    # ordered by column, then position — the exact shape the board renders.

@app.patch("/api/tickets/{ticket_id}", response_model=Ticket)
def update_ticket(ticket_id: str, req: UpdateTicketRequest) -> Ticket: ...
    # 404 if ticket doesn't exist. Only provided fields change. Moving to a
    # new column appends at the end of that column (same position rule as
    # create) unless the column is unchanged, in which case position is
    # left alone (this spec does not support explicit re-ordering within a
    # column — only which column a ticket is in).
    # Always bumps updated_at.

@app.get("/api/tickets/{ticket_id}")
def get_ticket(ticket_id: str) -> dict: ...
    # {**ticket.model_dump(), "comments": [...]} — full detail + comment thread.

@app.post("/api/tickets/{ticket_id}/comments", response_model=TicketComment)
def add_comment(ticket_id: str, req: CreateCommentRequest, author_role: str | None = None) -> TicketComment: ...
    # 404 if ticket doesn't exist.
```

`author_role`/`created_by_role`: passed as a query parameter (e.g. `POST
.../tickets?author_role=coder`), not part of the JSON body — this keeps the
curl examples embedded in agent prompts short, and mirrors how role is
conceptually "who is calling," similar to metadata rather than ticket
content. Optional; `None` if omitted (covers the case of an ad-hoc task
manually told to use the API, or a human debugging via curl by hand).

## Agent integration

`pipeline_prompts.py` gains a new constant and each `build_*_prompt`
function gains a `mission_id: str` parameter:

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

The port (`8420`) is hardcoded the same way `__main__.py` already hardcodes
it for `uvicorn.run` — not made configurable by this spec, since nothing in
the codebase makes it configurable today either.

`_execute_pipeline` in `app.py` (the only caller of these three functions)
already has `mission_id` in scope at every call site — this is a pure
additional-argument change, not a new lookup.

"This is optional but encouraged" is deliberate: an agent that never touches
the API is exactly today's behavior (empty board), and there is no
enforcement or detection of whether an agent used it — consistent with "no
auto-detection" decisions elsewhere in this project.

## UI changes

`dashboard.html` gains a new panel, `#kanbanPanel`, placed between the
existing `#pipelinesPanel` ("Agents") and `#detailGrid`, so it reads as part
of the mission-detail view alongside everything else already there (no tab
system exists in this dashboard today — panels are stacked; this follows
that existing convention rather than introducing a new one).

Structure: 4 columns (`Backlog`, `Todo`, `Doing`, `Done`), each a vertical
list of cards. A card shows title, a comment-count badge if `comments.length
> 0`, and the creating role as a small tag (reusing the existing per-role
color convention already used for the pipeline's agent tabs). Clicking a
card expands an inline detail area (or a simple modal — implementer's
choice, consistent with the rest of this file's plain-DOM style, no new
dependencies) showing the full description and comment thread.

Live updates: poll `GET /api/missions/{mission_id}/tickets` every 2 seconds
while a mission is open (slower than the existing 0.3s task-transcript SSE
poll — a kanban board changes far less often than a token stream, and 2s is
imperceptible for this use case). Plain `setInterval` + `fetchJSON`, cleared
in `closeMission()` alongside the existing `state.eventSource` cleanup — no
new SSE endpoint needed for v1. Re-rendering fully replaces the column
contents each poll (same "rebuild from scratch" pattern already used for
`pipelineRuns` and `taskList`), which is simple and correct as long as this
project's typical ticket counts per mission stay small (tens, not
thousands) — reasonable for an agent's own self-created work breakdown.

## Error handling

- Unknown `mission_id`/`ticket_id`: HTTP 404, matching every existing
  task-scoped route.
- A malformed `curl` call from an agent (bad JSON, missing required field)
  gets FastAPI's standard 422/400 response, visible to the agent in its own
  Bash tool output — the agent can retry or ignore it. No special handling
  or retry logic on Mission Control's side; this mirrors how any other
  local API failure the agent's own tools might hit is already just part of
  its normal working context.
- No column-transition validation (e.g. `done` → `backlog` is allowed) —
  this is a lightweight coordination aid, not a workflow-enforcement system.

## Testing

- **Offline, in default `pytest` run**, using this project's established
  in-memory-DB pattern (no live adapter/agent calls needed for any of
  these):
  - `POST .../tickets`: creates with correct defaults (`column=backlog`,
    `position=0` for the first ticket in a mission+column), 404 on unknown
    mission.
  - `POST .../tickets` twice into the same mission+column: second ticket's
    `position` is `1`, not `0` or a duplicate.
  - `GET .../tickets`: returns tickets ordered by column then position.
  - `PATCH /api/tickets/{id}`: partial update (only `column` provided
    leaves `title`/`description` unchanged); moving to a new column appends
    at the end of it (position rule); 404 on unknown ticket.
  - `POST /api/tickets/{id}/comments`: creates a comment, 404 on unknown
    ticket; `GET /api/tickets/{id}` includes it in `comments`.
  - `build_orchestrator_prompt`/`build_coder_prompt`/`build_reviewer_prompt`:
    each now includes the mission's ticket-board URL and the given
    `mission_id` in its output (pure function tests, no I/O — same style as
    the existing tests for these functions).
- **Not in default suite (real, cost-incurring), live verification only:**
  run an actual pipeline with a real goal and confirm at least one agent
  creates a ticket, and that it shows up on the dashboard's kanban panel
  within one poll interval — the same kind of manual live check already
  done for this project's other agent-facing features (the chat/interrupt
  spec's live verification, the original pipeline's live verification).

## Explicitly out of scope (do not implement as part of this spec)

- Trello (or any external service) sync — this board is Mission-Control-only.
- Human drag-and-drop or manual editing of tickets.
- Ad-hoc (non-pipeline) tasks receiving kanban instructions automatically.
- A more autonomous, ticket-driven agent-scheduling mode (agents claiming
  and picking up backlog items themselves without the fixed pipeline
  sequence) — a candidate for its own future sub-project, not this one.
- Explicit within-column re-ordering via drag or API (position is
  append-only, server-assigned).
- Real-time push (SSE/WebSocket) for the board — polling is sufficient at
  this scale; can be revisited if ticket volume or responsiveness needs
  grow.
