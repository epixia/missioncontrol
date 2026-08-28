# Human-in-the-Loop: Chat/Interrupt With Any Agent — Design

Status: approved via brainstorming, ready for implementation planning
Date: 2026-08-28

## Purpose

Today, an agent's task is a single, fire-and-forget conversation: one prompt
in, the adapter runs to completion, done. This spec turns any agent's task
(ad-hoc or pipeline-created) into a persistent, chattable session: the user
can send it a follow-up message at any time — while it's actively working
(a genuine interrupt) or after it's already finished (reopening the
conversation) — and see the reply in the same transcript.

## Decisions made during brainstorming (do not re-litigate without cause)

- **No auto-detection of "the agent wants input."** The system never tries
  to infer that an agent's output is a question. The user decides when to
  intervene, by reading the transcript themselves. (Sub-project note: this
  rules out any "waiting for you" UI state in this spec — none is built.)
- **True mid-turn interrupt, via kill-and-resume, not live injection.**
  No Claude Code CLI (or any adapter's CLI) supports injecting text into an
  actively-streaming turn — that capability doesn't exist. What Claude Code
  *does* support is killing a running turn and resuming that same session
  with new input (`--resume <session_id>`), which this project already uses
  for continuity. "Interrupting mid-turn" is implemented as: kill the
  current process now, immediately start a new turn on the same session
  with the user's message. From the model's perspective its previous turn
  simply ended and a new one began with the user's input included. This is
  fast (process termination is near-instant) and is the same primitive
  real interactive coding-agent tools use.
- **Scope: `claude_code` only.** It's the only adapter with real session
  continuity today. Sending a message to a `codex`/`hermes`/`openclaw` task
  returns an HTTP 400.
- **No cross-stage retriggering.** Redirecting an agent after a later
  pipeline stage has already consumed its original output does not
  retroactively re-run that later stage. That's a separate, more invasive
  feature (would mean re-opening an already-closed pipeline stage) and is
  explicitly out of scope here.

## A real gap this design surfaces (read before implementing)

`MissionTask.session_id` today stores `SessionHandle.session_id` — a UUID
`ClaudeCodeRuntimeAdapter.start()` generates itself, used only as the key
into that adapter instance's in-memory `_sessions: dict[str, _SessionState]`
(`src/mission_control/adapters/claude_code/adapter.py`). It is **not**
Claude Code's own session id (the adapter calls that `native_ref`, populated
only inside `_pump_events` from stream-json's `session_id` field, and never
surfaced back to the caller). Two consequences:

1. `app.py` currently has no way to durably persist the value needed for
   `--resume` to work.
2. `_sessions` is in-memory only — if the server process restarts, every
   adapter's session state is gone, even though the DB still has the task
   row and (after this spec) its native session id.

This spec fixes both, because messaging a task that finished even slightly
in the past (let alone across a restart) requires them.

## Data model changes

New nullable column on `MissionTask` (idempotent migration, same pattern as
the pipeline columns in `db.py::_migrate_add_columns`):

```python
native_session_id: str | None = Field(default=None)
```

This is Claude Code's actual resumable session id — durable, meaningful
across server restarts. The existing `session_id` field is unchanged (it
keeps meaning "this server process's in-memory adapter handle key" for the
life of the process that created it).

## Adapter contract changes

`mission_control/adapters/types.py`:

```python
class RuntimeEvent(BaseModel):
    # ...existing fields...
    native_ref: str | None = None  # new
```

`ClaudeCodeRuntimeAdapter._pump_events` already extracts the native session
id into `state.native_ref` from each event's `session_id` field — it just
needs to also attach it to the `RuntimeEvent` it emits, so the caller
(`_execute_task`) can see it and persist it, following the exact pattern
already used for `result_text` extraction (see `pipeline_prompts.py` and
Task 3 of the prior plan).

`ClaudeCodeRuntimeAdapter` needs one more capability: **seeding a session
with a known native ref before any turn has run in this process**, so a
task whose `_SessionState` doesn't exist in the current process's memory
(first message since a restart, or first message to a task this process
never previously touched) can still resume correctly. Add an optional field
to `SessionRequest`:

```python
class SessionRequest(BaseModel):
    # ...existing fields...
    resume_native_ref: str | None = None  # new
```

`ClaudeCodeRuntimeAdapter.start()` seeds `_SessionState.native_ref` from
`session.resume_native_ref` when provided, instead of leaving it `None`.
Every other adapter ignores this field (no behavior change for them).

## New execution model: `_execute_task` becomes a loop

Today, `_execute_task` is (simplified): `send_task` once → drain
`stream_events` once → compute final status → `_finish`. It becomes:

```python
async def _execute_task(task_id: str) -> None:
    # ...install/configure/deploy exactly as today; start() gains
    # resume_native_ref and _active_handles registration — see
    # "Unifying continuity through _execute_task's own preamble" below...
    prompt = task.prompt
    while True:
        # Checked BEFORE every send_task call, including the very first —
        # not just after a turn ends. This matters for the "reopen an
        # already-finished task" path (see the new endpoint below): that
        # path re-enters this function fresh, and without this check here,
        # the first thing it would do is resend the task's ORIGINAL prompt
        # again before ever looking at the new message the user actually
        # sent. Checking here means a re-entered task goes straight to the
        # queued message instead.
        pending = _pending_messages.get(task_id)
        if pending is not None and not pending.empty():
            prompt = await pending.get()
            _record_user_message_event(task_id, prompt)  # new TaskEvent, event_type="user_message"

        ack = await adapter.send_task(handle, Task(id=task_id, mission_id=..., instructions=prompt))
        # ...ack-not-accepted handling exactly as today...
        saw_error = False
        captured_result_text = None
        captured_native_ref = None
        async for event in adapter.stream_events(handle):
            # ...existing per-event persistence (TaskEvent, cost accumulation)...
            # ...existing result_text capture...
            if event.native_ref is not None:
                captured_native_ref = event.native_ref
        if captured_native_ref is not None:
            _persist_native_session_id(task_id, captured_native_ref)  # small helper, own DB write

        pending = _pending_messages.get(task_id)
        if pending is not None and not pending.empty():
            continue  # loop back — the top-of-loop check above picks up the message

        health = await adapter.health(handle)
        final_status = ...  # exactly as today
        _finish(task_id, final_status, ..., result_text=captured_result_text if final_status == SUCCEEDED else None)
        await adapter.destroy(handle)
        return
```

Why this is safe for the existing single-turn case: for a brand-new task
nobody has ever messaged, `_pending_messages.get(task_id)` is always
`None`/empty at the top of the loop too, so `prompt` stays `task.prompt` and
the loop runs exactly once — identical behavior to today, byte-for-byte,
for the ad-hoc flow, the pipeline flow, and every existing test.

Why this correctly implements "interrupt": if `POST .../message` calls
`adapter.stop(handle)` while `_execute_task`'s `async for event in
adapter.stream_events(handle)` is actively awaiting the next event, killing
the process causes the adapter's own `_pump_events` to see stdout EOF, call
`process.wait()`, and push the `None` sentinel — which is exactly what
already makes that `async for` loop end today when a task finishes
normally. No changes to the adapter's internal event-pumping are needed;
this behavior already exists and already does the right thing when a
process is killed mid-stream.

## Unifying continuity through `_execute_task`'s own preamble

Rather than have both the new endpoint *and* `_execute_task` independently
try to reconstruct/create adapter sessions (which would race and clobber
each other — the endpoint creating one handle just before `_execute_task`'s
own `start()` call unconditionally creates a *different* one), continuity
is handled in exactly one place: `_execute_task`'s existing `start()` call,
which already runs once at the top of every invocation. It becomes:

```python
handle = await adapter.start(SessionRequest(
    mission_id=task.mission_id, task_id=task_id, workspace=workspace,
    resume_native_ref=task.native_session_id,  # None for a brand-new task; set for a reopened one
))
with get_session() as session:
    task = session.get(MissionTask, task_id)
    task.session_id = handle.session_id
    session.add(task)
    session.commit()
```

This is a one-line addition to existing code (`resume_native_ref=...`) plus
the pre-existing `task.session_id = handle.session_id` assignment — every
`_execute_task` invocation, whether it's a task's very first run or a
reopening long after it finished, ends up with a correctly-seeded handle
with no special-casing needed at the call site.

This leaves exactly one thing the new endpoint needs help with:
**interrupting a turn that's live *right now*, in this same process.**
`_execute_task` registers its handle for the duration of its own execution
so the endpoint can find it:

```python
_active_handles: dict[str, SessionHandle] = {}  # module-level, app.py

# inside _execute_task, right after the start() call above:
_active_handles[task_id] = handle
try:
    ...  # the while-loop from above
finally:
    _active_handles.pop(task_id, None)
```

## New endpoint: `POST /api/tasks/{task_id}/message`

Request body: `{text: str}`.

```python
_pending_messages: dict[str, asyncio.Queue[str]] = {}  # module-level, app.py


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
        await adapter.stop(live_handle)  # ends the current turn now; _execute_task's loop (still running,
                                          # inside the same try/finally above) picks up the queued message
                                          # and continues the SAME _execute_task invocation — no new task needed.
    elif current_status != TaskStatus.RUNNING:
        # Idle (terminal status) — nothing to interrupt. Start a fresh
        # _execute_task invocation; its own preamble (see above) resumes
        # via task.native_session_id, and the loop's top-of-iteration check
        # (see "New execution model") picks up the queued message before
        # ever touching task.prompt again.
        with get_session() as session:
            task = session.get(MissionTask, task_id)
            task.status = TaskStatus.RUNNING
            session.add(task)
            session.commit()
        bg = asyncio.create_task(_execute_task(task_id))
        _background_tasks.add(bg)
        bg.add_done_callback(_background_tasks.discard)
    # else: current_status == RUNNING but live_handle is None — a data-race
    # edge case (status says running but this process has no record of it,
    # e.g. it flipped between the two reads above). The message is already
    # queued; do nothing further and let it be picked up whenever that
    # invocation's loop next checks, rather than erroring.

    return {"accepted": True}
```

This removes the separate `_reconstruct_handle`/`find_live_handle` concept
entirely — `_active_handles` (populated only by `_execute_task` itself,
only for its own lifetime) is sufficient for the one thing the endpoint
actually needs from outside: knowing whether *this process* currently has
a live turn to interrupt.

## Transcript rendering

New synthetic `TaskEvent.event_type == "user_message"`, `payload = {"text":
...}` — not a real adapter event, inserted directly by
`_record_user_message_event`. The dashboard's transcript dispatcher
(`appendTranscriptLine` in `dashboard.html`) checks for this event type
*before* delegating to the per-runtime renderer, and renders it visibly
distinct from agent output (e.g. a `"you"`-styled line, right-leaning or a
different accent color) so a transcript reads as an actual conversation.

## UI changes

`dashboard.html`: a persistent message box (text input + "Send" button)
under the transcript panel (`#transcriptPanel`), enabled whenever a task is
selected — regardless of that task's current status (running, succeeded,
failed all get the same box; only a non-`claude_code` task disables it,
matching the backend's 400). Submitting calls `POST /api/tasks/{id}/message`
then re-invokes `watchTranscript(taskId)` to (re)open the SSE connection —
necessary because the existing code closes the `EventSource` on a `"done"`
event, and a task that was finished may be about to become `RUNNING` again.

## Error handling

- `adapter.stop()` on an already-stopped process is already a safe no-op
  (`ClaudeCodeRuntimeAdapter.stop` checks `state.process.returncode is None`
  first) — a message arriving in the narrow race window right as a turn
  finishes naturally is handled correctly by the existing code, no new
  guard needed.
- Non-`claude_code` tasks: HTTP 400, per the stated scope boundary.
- Unknown `task_id`: HTTP 404, matching every other task-scoped endpoint.

## Testing

- **Offline, in default `pytest` run:**
  - `_execute_task`'s loop: a test that queues a message before the (mocked)
    event stream ends, and asserts `send_task`/the adapter is invoked a
    second time with that message as the prompt, and a `user_message`
    `TaskEvent` is persisted.
  - Regression guard: a test with no queued message asserts the loop runs
    exactly once and finalizes exactly as today (protects the ad-hoc and
    pipeline flows from any behavior change).
  - **Reopen-a-finished-task correctness**: a test that simulates a task
    already in a terminal status with a message queued *before*
    `_execute_task` is invoked (mirroring what the endpoint does), and
    asserts the mocked adapter's `send_task` is called with the *queued
    message* as the prompt — never with `task.prompt` (the original). This
    is the exact bug caught during this spec's self-review (the top-of-loop
    check existing at all, not just after a turn ends) — worth a named test
    precisely because it's easy to silently regress.
  - `RuntimeEvent.native_ref` extraction and persistence, following the
    existing `result_text` test pattern.
  - `SessionRequest.resume_native_ref` seeding behavior in
    `ClaudeCodeRuntimeAdapter.start()`.
  - `POST /api/tasks/{id}/message` route: 404 on unknown task, 400 on
    non-claude_code task, both via the isolated in-memory DB pattern already
    used throughout this project's tests — no live adapter calls needed for
    these two paths.
- **Not in default suite (real, cost-incurring):** an actual live interrupt
  — start a real task, send it a message while genuinely mid-turn, confirm
  the turn ends and a new one starts incorporating the message, confirm the
  final `result_text` reflects it. Verified manually the same way prior
  live verification in this project was done, not as an automated
  `MC_LIVE_TESTS` addition (a live interrupt test is materially slower and
  more expensive than the existing single-call smoke tests).

## Explicitly out of scope (do not implement as part of this spec)

- Auto-detecting that an agent's output is a question.
- Runtime support beyond `claude_code` (Codex, Hermes, OpenClaw messaging).
- Retroactively re-running a later pipeline stage after an earlier one is
  redirected post-hoc.
- Any approval/governance gating on sending a message (anyone with access
  to this local dashboard can message any agent — same trust model as
  everything else in this local, single-user tool today).
