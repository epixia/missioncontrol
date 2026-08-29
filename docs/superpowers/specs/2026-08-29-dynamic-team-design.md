# Orchestrator-Composed Dynamic Team — Design

Status: approved via brainstorming, ready for implementation planning
Date: 2026-08-29

## Purpose

Today, every multi-agent run is exactly three fixed roles — Orchestrator,
Coder, Reviewer — always `claude_code`. This spec adds a second, alternate
way to run a multi-agent goal: the Orchestrator itself decides, upfront, how
many agents the goal needs, what each one's specialty is, and which runtime
(`claude_code` or `hermes`, today) each should run under — letting the team
grow or shrink per goal instead of always being exactly three fixed roles.

## Decisions made during brainstorming (do not re-litigate without cause)

- **The Orchestrator invents roles freely** — no predefined catalog of
  specialist personas to maintain. It writes each team member's persona and
  task itself, tailored to the goal.
- **Decided upfront, not adaptively.** The Orchestrator produces one
  complete team roster before anyone starts working; the roster's size is
  fixed for that run once decided. Re-planning mid-run (an agent's result
  causing a *new* agent to be added on the fly) is explicitly out of scope
  — a candidate for a future spec, not this one.
- **A new alternate mode, not a replacement.** The existing fixed
  Orchestrator/Coder/Reviewer pipeline (`POST /api/missions/{id}/pipeline`,
  `_execute_pipeline`, `_create_pipeline_task`) is completely unchanged and
  keeps working exactly as it does today. This is a new, separate endpoint
  and execution path a user opts into per run.
- **Team declared via a JSON block in the Orchestrator's own output**, not
  a new curl endpoint or kanban tickets. The Orchestrator's prompt asks it
  to end its response with a fenced JSON block; the backend parses it out
  of the already-stored result text — no extra network round-trip, no new
  curl syntax for the model to get wrong.
- **Runtime is per-agent.** Each team member specifies `claude_code` or
  `hermes` (any `RuntimeType` value is accepted and validated the same way
  `CreateTaskRequest` already validates it — not artificially restricted to
  just these two — but a `codex`/`openclaw` slot will fail exactly the same
  way an ad-hoc task with an unavailable runtime already fails today).
  Kanban ticket instructions and chat/interrupt messaging remain
  `claude_code`-only, matching those features' existing scope — a Hermes
  slot simply doesn't get the kanban paragraph appended to its prompt.
- **Halts on first failure**, matching `_execute_pipeline`'s existing
  behavior — no reason for a dynamic team to behave differently here.
- **A hard cap on team size** (8 agents) protects against a malformed or
  overly ambitious plan spawning an unbounded, unboundedly expensive chain.

## A real migration gap this design surfaces (read before implementing)

`MissionTask.role` is typed `AgentRole | None` — a strict 3-member
`StrEnum`. SQLAlchemy's default behavior for a native `Enum` column is to
store the member's **name**, not its `.value`, unless configured
otherwise. Confirmed directly against the live database: existing rows
store `'ORCHESTRATOR'`, `'CODER'`, `'REVIEWER'` (uppercase member names) —
not `'orchestrator'`/`'coder'`/`'reviewer'` (the lowercase `.value` every
API response and every piece of dashboard JS actually consumes:
`AGENT_ROLE_ORDER = ["orchestrator", "coder", "reviewer"]`,
`kanban_instructions`'s per-role directives, `byRole[task.role]`, etc.).
Today this works because SQLAlchemy's `Enum` type transparently converts
between the stored name and the Python `AgentRole` member (whose `.value`
is what actually gets serialized to JSON) — the uppercase storage is
invisible at every layer above the column type itself.

This spec needs `MissionTask.role` to hold arbitrary strings the
Orchestrator invents (e.g. `"database-specialist"`), which cannot be
members of a fixed enum. Naively relaxing the column's Python type from
`AgentRole` to `str` would make SQLAlchemy treat existing stored values
(`'ORCHESTRATOR'`) as **literal role strings** instead of enum member names
— every existing task's role would suddenly read back and serialize as
`"ORCHESTRATOR"` (uppercase) instead of `"orchestrator"`, silently breaking
every fixed-pipeline role comparison across the entire app (the Agents
panel's tabs, the kanban prompt directives, `list_pipelines`' role
ordering) for every mission created before this change ships.

**Fix, part of Task 1 below:** a one-time idempotent migration
(`db.py`, alongside the existing `_migrate_add_columns` machinery) that
rewrites existing `missiontask.role` values from the enum member name to
its lowercase `.value` equivalent (`UPDATE missiontask SET role = LOWER(role) WHERE role IN ('ORCHESTRATOR', 'CODER', 'REVIEWER')`)
**before** the column's Python type changes to plain `str`. Run once, safe
to run on every startup (a row already holding a lowercase value is
unaffected by the `WHERE` clause).

## Data model

`src/mission_control/server/models.py`:

```python
role: str | None = Field(default=None)  # was: AgentRole | None
```

`AgentRole` itself is unchanged and still used by the fixed pipeline
(`AgentRole.ORCHESTRATOR` etc. are still valid strings to assign to this
now-relaxed field — StrEnum members already *are* strings, so every
existing call site in `_create_pipeline_task`'s fixed-pipeline callers
keeps working with zero changes). No new table — a dynamic team's roster
lives only in the Orchestrator task's own `result_text` (as the JSON block)
plus each spawned task's own row (`role`, `pipeline_run_id`, ordinary
fields) — the same shape the fixed pipeline already uses, just with
however many rows the roster specified instead of always two more after
the Orchestrator.

`db.py`, alongside `_migrate_add_columns`:

```python
def _migrate_lowercase_role_values(target_engine) -> None:
    """AgentRole.role used to be a native Enum column (name-based storage:
    'ORCHESTRATOR') before this plan relaxed it to a plain string column
    (value-based comparison everywhere else in the app: 'orchestrator').
    Idempotent — a row already holding a lowercase value is untouched."""
    with target_engine.connect() as conn:
        conn.exec_driver_sql(
            "UPDATE missiontask SET role = LOWER(role) "
            "WHERE role IN ('ORCHESTRATOR', 'CODER', 'REVIEWER')"
        )
        conn.commit()
```

Called from `init_db()` right after the existing `_migrate_add_columns(engine)` call.

## Orchestrator prompt and JSON contract

A new prompt-building function in `pipeline_prompts.py`, alongside the
existing `ORCHESTRATOR_PROMPT`/`build_orchestrator_prompt`:

```python
DYNAMIC_TEAM_ORCHESTRATOR_PROMPT = (
    "You are the Orchestrator agent. Read the goal below and decide what "
    "team of specialist agents is needed to accomplish it — invent "
    "whatever roles fit the goal (e.g. \"database-specialist\", "
    "\"frontend-stylist\", \"security-reviewer\"); the team can be as "
    "small as one agent or as large as needed, up to 8. For each agent, "
    "choose \"claude_code\" (works inside this workspace: reads/writes "
    "files, runs shell commands) or \"hermes\" (autonomous, can act "
    "outside this workspace — use it for anything an internal, "
    "workspace-bound agent cannot do, like researching something live on "
    "the web). Each agent will receive the original goal, its own persona "
    "and task below, and the previous agent's output — in the order you "
    "list them.\n\n"
    "End your response with exactly one fenced JSON block in this shape:\n"
    "```json\n"
    "{\"team\": [\n"
    "  {\"role\": \"short-slug\", \"runtime\": \"claude_code\", "
    "\"persona\": \"You are a ...\", \"task\": \"...\"}\n"
    "]}\n"
    "```\n\nGoal:\n"
)


def build_dynamic_team_orchestrator_prompt(goal: str, mission_id: str) -> str:
    return f"{DYNAMIC_TEAM_ORCHESTRATOR_PROMPT}{goal}{kanban_instructions(mission_id, _ORCHESTRATOR_KANBAN_DIRECTIVE)}"


def build_dynamic_team_agent_prompt(persona: str, goal: str, task: str, previous_result: str | None) -> str:
    prompt = f"{persona}\n\nGoal:\n{goal}\n\nYour task:\n{task}"
    if previous_result:
        prompt += f"\n\nThe previous agent's output:\n{previous_result}"
    return prompt
```

(`build_dynamic_team_agent_prompt` deliberately does **not** call
`kanban_instructions()` — that's appended separately, only for
`claude_code`-runtime team members, in the execution code below, keeping
the Hermes-vs-claude_code distinction in one place rather than duplicated
across every prompt builder.)

Parsing, in `app.py` (pure function, unit-testable without I/O):

```python
import re


def _parse_dynamic_team(orchestrator_result: str) -> list[dict] | None:
    """Extract and validate the fenced JSON team block from the
    Orchestrator's result text. Returns None (not raises) on anything
    malformed — the caller treats that exactly like the existing fixed
    pipeline's halt condition (no result_text / wrong status), not as a
    500. Deliberately permissive on the *outer* wrapping (agents don't
    always wrap things in ```json exactly): finds the last {...} block in
    the text and tries to parse that if no fenced block is found."""
    match = re.search(r"```json\s*(\{.*?\})\s*```", orchestrator_result, re.DOTALL)
    raw = match.group(1) if match else None
    if raw is None:
        brace_match = re.search(r"\{.*\}", orchestrator_result, re.DOTALL)
        raw = brace_match.group(0) if brace_match else None
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    team = parsed.get("team") if isinstance(parsed, dict) else None
    if not isinstance(team, list) or not team or len(team) > 8:
        return None
    valid_runtimes = {r.value for r in RuntimeType}
    for member in team:
        if not isinstance(member, dict):
            return None
        if not all(isinstance(member.get(k), str) and member.get(k) for k in ("role", "persona", "task")):
            return None
        if member.get("runtime") not in valid_runtimes:
            return None
    return team
```

## Execution model

New function in `app.py`, mirroring `_execute_pipeline`'s exact shape:

```python
async def _execute_dynamic_team(
    run_id: str, mission_id: str, goal: str, workspace_path: str, orchestrator_task_id: str
) -> None:
    current_task_id = orchestrator_task_id
    try:
        await _execute_task(orchestrator_task_id)
        orchestrator = _get_task(orchestrator_task_id)
        if orchestrator is None or orchestrator.status != TaskStatus.SUCCEEDED or not orchestrator.result_text:
            return

        team = _parse_dynamic_team(orchestrator.result_text)
        if team is None:
            # _finish overwrites result_text unconditionally (see the
            # chat/interrupt spec's own noted deferred minor on this same
            # function) -- pass the orchestrator's already-produced text
            # back through explicitly, or a parse failure would silently
            # erase the one thing that would help debug *why* it failed.
            _finish(
                orchestrator_task_id, TaskStatus.FAILED,
                error_detail="could not parse a valid team roster from the Orchestrator's output",
                result_text=orchestrator.result_text,
            )
            return

        previous_result: str | None = None
        for member in team:
            prompt = build_dynamic_team_agent_prompt(member["persona"], goal, member["task"], previous_result)
            if member["runtime"] == RuntimeType.CLAUDE_CODE.value:
                prompt += kanban_instructions(mission_id, _CODER_KANBAN_DIRECTIVE)
            task_id = _create_pipeline_task_for_runtime(
                mission_id, run_id, member["role"], member["runtime"], prompt, workspace_path,
            )
            current_task_id = task_id
            await _execute_task(task_id)
            task = _get_task(task_id)
            if task is None or task.status != TaskStatus.SUCCEEDED or not task.result_text:
                return
            previous_result = task.result_text
    except Exception as exc:
        _finish(current_task_id, TaskStatus.FAILED, error_detail=repr(exc))
```

`_create_pipeline_task_for_runtime` is `_create_pipeline_task` widened by
two changes — `role: str` instead of `role: AgentRole`, and a new
`runtime: str` parameter instead of the hardcoded `runtime="claude_code"`
literal:

```python
def _create_pipeline_task_for_runtime(
    mission_id: str, pipeline_run_id: str, role: str, runtime: str, prompt: str, workspace_path: str
) -> str:
    with get_session() as session:
        task = MissionTask(
            mission_id=mission_id,
            runtime=runtime,
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
```

This is byte-for-byte `_create_pipeline_task`'s existing body — the fixed
pipeline's own `_create_pipeline_task(mission_id, pipeline_run_id, role, prompt, workspace_path)`
becomes a thin wrapper calling this with `runtime="claude_code"` hardcoded,
so there is exactly one implementation of "create and persist a
pipeline-style task row," not two copies to keep in sync:

```python
def _create_pipeline_task(mission_id: str, pipeline_run_id: str, role: AgentRole, prompt: str, workspace_path: str) -> str:
    return _create_pipeline_task_for_runtime(mission_id, pipeline_run_id, role.value, "claude_code", prompt, workspace_path)
```

## New endpoint

```python
class RunDynamicTeamRequest(BaseModel):
    goal: str
    workspace_path: str | None = None


@app.post("/api/missions/{mission_id}/dynamic-team")
async def run_dynamic_team(mission_id: str, req: RunDynamicTeamRequest) -> dict:
    with get_session() as session:
        mission = session.get(Mission, mission_id)
        if mission is None:
            raise HTTPException(404, "mission not found")

    run_id = str(uuid.uuid4())
    workspace_path = req.workspace_path or _mission_workspace_dir(mission)
    orchestrator_task_id = _create_pipeline_task_for_runtime(
        mission_id, run_id, "orchestrator", "claude_code",
        build_dynamic_team_orchestrator_prompt(req.goal, mission_id), workspace_path,
    )
    task = asyncio.create_task(
        _execute_dynamic_team(run_id, mission_id, req.goal, workspace_path, orchestrator_task_id)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    with get_session() as session:
        orchestrator_task = session.get(MissionTask, orchestrator_task_id)
        return {"run_id": run_id, "orchestrator_task": orchestrator_task}
```

Note the dynamic team's Orchestrator task itself is created with the
literal role string `"orchestrator"` (not `AgentRole.ORCHESTRATOR`) — same
resulting value, written directly since this path has no reason to import
`AgentRole` for a role it's inventing fresh each time.

## UI changes

`dashboard.html`:

- A new form next to the existing `#pipelineForm` (same fields: goal,
  workspace path), submitting to `POST /api/missions/{id}/dynamic-team`
  instead of `/pipeline`. Button label: "Run dynamic team".
- `AGENT_ROLE_ORDER` (currently the hardcoded
  `["orchestrator", "coder", "reviewer"]` array driving the Agents panel's
  tab order) becomes dynamic: derived from whatever distinct roles are
  actually present across a mission's tasks, ordered by each role's first
  appearance (`created_at`) rather than a fixed array. This is the one
  piece of already-shipped code this spec touches — everywhere else is
  additive. Concretely, in `loadPipelines()`:

```javascript
// Was: const rolesPresent = AGENT_ROLE_ORDER.filter(r => byRole[r] && byRole[r].length);
const rolesPresent = Object.keys(byRole).sort(
  (a, b) => byRole[a][0].task.created_at.localeCompare(byRole[b][0].task.created_at)
);
```

  and the `runTasks.sort(...)` line that currently orders a single run's
  own tasks by `AGENT_ROLE_ORDER.indexOf(...)` changes to sort by each
  task's own `created_at` instead (order-of-creation already matches
  intended execution order for both the fixed pipeline and a dynamic
  team, so this is a strictly equivalent, more general ordering rule, not
  a behavior change for existing fixed-pipeline runs).

## Testing

- **Offline, in default `pytest` run:**
  - `_parse_dynamic_team`: valid fenced JSON block parses correctly;
    valid bare `{...}` without fencing also parses (permissive-outer-
    wrapping case); missing `team` key, empty team, team exceeding 8
    members, a member missing a required field, and an invalid `runtime`
    value each return `None`; malformed JSON returns `None` (not a raised
    exception).
  - `_migrate_lowercase_role_values`: a fresh DB with a raw `'ORCHESTRATOR'`
    row (inserted via raw SQL to simulate a pre-migration row) gets
    lowercased; a row already `'orchestrator'` is untouched; running the
    migration twice is a no-op the second time.
  - `_create_pipeline_task_for_runtime`/`_create_pipeline_task`: the
    existing fixed-pipeline function still creates a row with
    `runtime="claude_code"` and the given `AgentRole`'s value — a
    regression guard proving the wrapper-around-the-generalized-function
    refactor didn't change fixed-pipeline behavior.
  - `_execute_dynamic_team` with a fake multi-runtime adapter registry:
    a 2-member team (one `claude_code`, one `hermes`) runs both in
    order, each receiving the previous one's `result_text`; halts and
    marks the in-flight member `FAILED` if a member fails; halts cleanly
    with the orchestrator itself marked `FAILED` (with the parse-failure
    `error_detail`) when `_parse_dynamic_team` returns `None`.
  - `RunDynamicTeamRequest`/route: 404 on unknown mission; workspace_path
    defaults to `_mission_workspace_dir(mission)` when omitted, matching
    the existing pipeline route's already-tested behavior.
- **Not in default suite, live verification only:** a real goal that
  plausibly warrants more than 2 non-reviewer specialists (to prove the
  Orchestrator will actually vary team size, not just always emit 3), and
  a goal that plausibly warrants a `hermes` slot (something needing
  information from outside the workspace).

## Explicitly out of scope (do not implement as part of this spec)

- Adaptive/mid-stream re-planning (adding an agent after seeing an
  earlier one's result).
- A predefined catalog of specialist personas — the Orchestrator always
  invents its own.
- Kanban instructions or chat/interrupt support for non-`claude_code`
  team members — both stay scoped exactly as they already are.
- Replacing or modifying the existing fixed pipeline's behavior in any way
  beyond the one shared-helper refactor described above.
- Raising the 8-agent cap, or making it configurable.
