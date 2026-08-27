# Agent Pipeline (Orchestrator → Coder → Reviewer) — Design

Status: approved via brainstorming, ready for implementation planning
Date: 2026-08-27

## Purpose

Today a "task" in Mission Control is one single-shot call to one runtime
adapter — no session continuity between task rows, no messaging between
them. This spec adds a second, additive way to work within a mission: a
fixed three-role agent pipeline (Orchestrator → Coder → Reviewer) that
chains three Claude Code sessions together, where each role's output
becomes the next role's input, with the handoffs visible as a communication
feed and each role's own activity inspectable independently.

The existing single-task ad-hoc flow (`POST /api/missions/{id}/tasks`) is
untouched. This is purely additive.

## Decisions made during brainstorming (do not re-litigate without cause)

- **Fixed pipeline, one pass.** Orchestrator plans once → Coder implements
  once → Reviewer critiques once → done. No iteration loop, no round cap
  logic, no re-delegation back to an earlier role. Simplest correct version
  of "agents discussing" that has a predictable cost and a clear stop
  condition.
- **All three roles run on Claude Code.** It's the only runtime verified
  working end-to-end in this environment (Codex has no API credits, Hermes
  has no model provider configured, OpenClaw's `send_task` isn't
  implemented). Roles are differentiated by system-prompt persona only, not
  by runtime. (Runtime-per-role can be revisited later; out of scope here.)
- **Three roles, fixed.** No configurable/custom role sets in this version.
- **Both flows coexist.** Ad-hoc single tasks and agent pipelines are two
  separate, independently-triggerable ways to create work inside a mission.
- **Accepted limitations, not solved here:** no workspace isolation between
  concurrent pipeline runs sharing the same `workspace_path` (Git
  Coordination subsystem, still open work per
  `docs/architecture/00-foundational-architecture.md` §14); no budget cap
  per pipeline run (governance subsystem, also still open work).

## Data model

No new tables. Three new nullable columns on the existing `MissionTask`
(`src/mission_control/server/models.py`):

```python
class AgentRole(StrEnum):
    ORCHESTRATOR = "orchestrator"
    CODER = "coder"
    REVIEWER = "reviewer"

class MissionTask(SQLModel, table=True):
    # ...existing fields unchanged...
    role: AgentRole | None = Field(default=None)
    pipeline_run_id: str | None = Field(default=None, index=True)
    result_text: str | None = Field(default=None)
```

- `role` is `None` for today's ad-hoc tasks; set for pipeline-created tasks.
- `pipeline_run_id` groups the (up to) three `MissionTask` rows belonging
  to one pipeline execution. Generated once per `POST .../pipeline` call
  (a `uuid4`, not a new table — a pipeline run's existence and status are
  entirely derived by querying tasks with that id).
- `result_text` is the extracted final plain-text output of a *completed*
  task. It is both (a) what's shown in the communication feed and (b) the
  literal text spliced into the next role's prompt. Extraction rule: for a
  `claude_code` task that succeeds, `result_text = payload["result"]` off
  the terminal `type: "result"` event (confirmed field, seen live:
  `{"result": "OK", ...}`). For any other runtime or on failure,
  `result_text` stays `None`. Mechanism is an implementation choice — most
  naturally, `_execute_task`'s existing `async for event in
  adapter.stream_events(handle):` loop already sees every event once, so
  capturing `payload["result"]` there and passing it into `_finish()` as a
  new parameter is simpler than having `_finish()` re-query `TaskEvent`
  rows afterward; either is acceptable as long as the populated value is
  correct.

## System prompts (persona framing)

Three module-level constants in `app.py` (or a new
`server/pipeline_prompts.py` if `app.py` is getting crowded — implementer's
call), each a short paragraph prepended to the composed prompt:

- `ORCHESTRATOR_PROMPT` — frames the agent as producing a concise plan for
  the stated goal and handing off to a Coder; explicitly told it is not
  writing code itself.
- `CODER_PROMPT` — frames the agent as implementing the Orchestrator's plan
  against the given workspace; told to report concisely what it changed.
- `REVIEWER_PROMPT` — frames the agent as critiquing the Coder's work
  against the original goal; told to give a clear verdict (approve /
  concerns) and cite specifics.

Exact wording is an implementation detail, not a design decision — the
implementer should write these directly rather than treating this spec as
needing another approval round for prompt copy.

## API

### `POST /api/missions/{mission_id}/pipeline`

Request: `{goal: str, workspace_path: str}`.

Behavior: generates `pipeline_run_id = uuid4()`, creates the Orchestrator
`MissionTask` (`role=orchestrator`, `pipeline_run_id` set, `prompt =
ORCHESTRATOR_PROMPT + goal`), fires `asyncio.create_task(_execute_pipeline(pipeline_run_id,
goal, workspace_path))` (same fire-and-forget pattern `create_task` already
uses), and returns immediately with `{pipeline_run_id, orchestrator_task:
<MissionTask>}`.

### `GET /api/missions/{mission_id}/pipelines`

Returns pipeline runs for the mission, grouped by `pipeline_run_id`: for
each, the list of its (1–3) `MissionTask` rows in role order
(orchestrator, coder, reviewer — coder/reviewer may be absent if the
pipeline hasn't reached them yet or failed earlier), plus a derived overall
status (`running` if any task is pending/running, `failed` if any task
failed, `succeeded` if all present tasks succeeded and reviewer is present
and succeeded).

Existing endpoints (`GET /api/missions`, `GET /api/tasks/{id}`, `GET
/api/tasks/{id}/events`, `GET /api/missions/{id}/log`) are unchanged and
work as-is for pipeline-created tasks too, since they're still just
`MissionTask` rows.

## Execution flow — `_execute_pipeline`

```python
async def _execute_pipeline(pipeline_run_id: str, mission_id: str, goal: str, workspace_path: str) -> None:
    orchestrator_id = ...  # the task already created by the route handler
    await _execute_task(orchestrator_id)
    orchestrator = _load_task(orchestrator_id)
    if orchestrator.status != TaskStatus.SUCCEEDED or not orchestrator.result_text:
        return  # pipeline halts; no coder/reviewer task is ever created

    coder_id = _create_pipeline_task(mission_id, pipeline_run_id, AgentRole.CODER,
        prompt=CODER_PROMPT + goal + "\n\nOrchestrator's plan:\n" + orchestrator.result_text,
        workspace_path=workspace_path)
    await _execute_task(coder_id)
    coder = _load_task(coder_id)
    if coder.status != TaskStatus.SUCCEEDED or not coder.result_text:
        return

    reviewer_id = _create_pipeline_task(mission_id, pipeline_run_id, AgentRole.REVIEWER,
        prompt=REVIEWER_PROMPT + goal + "\n\nCoder's work:\n" + coder.result_text,
        workspace_path=workspace_path)
    await _execute_task(reviewer_id)
```

`_execute_task` is reused as-is (it's already an `async def` that runs one
task to completion end-to-end: `install → configure → deploy → start →
send_task → stream_events → destroy`, updating the DB as it goes). The only
change needed to `_execute_task` itself is populating `result_text` inside
`_finish()` per the extraction rule above. `_create_pipeline_task` is a
small new helper mirroring the existing task-creation logic in the `POST
.../tasks` route (including the `status_changed` event insert), parameterized
with `role` and `pipeline_run_id`.

Because each stage is `await`ed in sequence, "sequential pipeline" and "one
task fully finishes before the next starts" are the same thing here — no
separate scheduler or turn-taking logic is needed for this fixed-pipeline
version.

## UI

Mission-detail screen gets a new **Agents** section, alongside (not
replacing) the existing task list and its ad-hoc task form:

- **"Run agent pipeline" form** — `goal` + `workspace_path` inputs, submits
  to `POST .../pipeline`.
- **Pipeline runs list** — each run shown as three **tabs**: `Orchestrator`
  / `Coder` / `Reviewer`, each with the same status iconography already
  used for the task list (✓ succeeded, ● running, ○ pending, ✗ failed, and
  a tab is simply absent/greyed if that stage hasn't been reached because
  an earlier stage failed). Clicking a tab selects that role's underlying
  `MissionTask` and reuses the **exact existing transcript panel and
  `appendClaudeCodeLine` rendering** built for the flat task list — no new
  transcript-rendering code, since each role is a `MissionTask` under the
  hood and Claude Code is guaranteed to give it a structured transcript.
- **Communication Feed panel** — separate from the tabs, shows the handoffs
  in sequence: `Orchestrator → Coder: <result_text, truncated>`, `Coder →
  Reviewer: <result_text, truncated>`, `Reviewer → done: <result_text,
  truncated>`. This is "what did they say to each other," distinct from a
  tab's "what is this one agent doing internally."

## Error handling

- Orchestrator/Coder failure (adapter error, unhealthy exit, or a
  "succeeded" status with unexpectedly empty `result_text`) halts the
  pipeline at that stage. Later-stage tasks are simply never created — the
  UI shows only the tabs for stages that were actually reached, with the
  failed one marked ✗.
- No workspace isolation between concurrent pipeline runs against the same
  `workspace_path` — accepted v0 limitation, not solved here (see
  Decisions).
- No per-pipeline budget cap — accepted v0 limitation, not solved here.

## Testing

- **Offline, in default `pytest` run:** unit tests for
  `result_text` extraction given a sample Claude Code `result` event
  payload (including the "missing/malformed result field" case), and for
  the prompt-composition helpers (pure string building, no adapter calls).
- **Not in default suite:** an actual live 3-stage pipeline run is real,
  cost-incurring Claude Code usage. Verify manually against the running
  server the same way the dashboard and adapters were verified earlier in
  this project (real `curl`/browser requests against a live server), not as
  an automated live-smoke test this time — three chained live agent calls
  per test run is a meaningfully larger cost than the existing single-call
  smoke tests.

## Explicitly out of scope (do not implement as part of this spec)

- Iterative/multi-round discussion, round caps, re-delegation to an earlier
  role.
- Runtime-per-role configurability (Codex/Hermes/OpenClaw as Coder/Reviewer).
- Configurable/custom role sets beyond the fixed three.
- Workspace isolation (git worktrees) between concurrent pipeline runs.
- Budget caps or approval gates on pipeline execution.
