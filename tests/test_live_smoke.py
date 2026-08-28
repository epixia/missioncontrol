"""Optional live smoke test against the actually-installed Claude Code and
Codex CLIs on this host. Skipped by default — it makes real API calls and
consumes API credits/quota. Run explicitly with:

    MC_LIVE_TESTS=1 uv run pytest tests/test_live_smoke.py -v
"""

import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mission_control.adapters.claude_code import ClaudeCodeRuntimeAdapter
from mission_control.adapters.codex import CodexRuntimeAdapter
from mission_control.adapters.types import (
    RuntimeSource,
    RuntimeSpec,
    RuntimeType,
    SessionRequest,
    Task,
    Workspace,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("MC_LIVE_TESTS"),
    reason="hits real Claude Code / Codex CLIs and consumes API credits; set MC_LIVE_TESTS=1 to run",
)


async def _run_trivial_task(adapter, runtime_type: RuntimeType, version: str):
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Workspace(path=Path(tmp))
        spec = RuntimeSpec(
            type=runtime_type,
            source=RuntimeSource(
                repository="local",
                version=version,
                date_validated=datetime.now(UTC).date().isoformat(),
            ),
        )
        await adapter.install(spec)
        deploy_result = await adapter.deploy(workspace)
        assert deploy_result.success

        handle = await adapter.start(SessionRequest(mission_id="m1", task_id="t1", workspace=workspace))
        ack = await adapter.send_task(handle, Task(id="t1", mission_id="m1", instructions="Say only the word OK."))
        assert ack.accepted

        events = []
        async for event in adapter.stream_events(handle):
            events.append(event)
        await adapter.destroy(handle)
        return events


async def test_claude_code_live_roundtrip():
    adapter = ClaudeCodeRuntimeAdapter()
    events = await _run_trivial_task(adapter, RuntimeType.CLAUDE_CODE, "2.1.250")
    assert events, "expected at least one stream-json event from claude -p"
    assert not any(e.payload.get("is_error") for e in events if e.event_type == "result"), (
        "claude -p reported is_error on its terminal result event"
    )


async def test_codex_live_roundtrip():
    """A non-empty event list is not success — `error`/`turn.failed` events
    (e.g. an exhausted-credits account) are events too, and an earlier
    version of this test wrongly passed on exactly that failure mode."""
    adapter = CodexRuntimeAdapter()
    events = await _run_trivial_task(adapter, RuntimeType.CODEX, "0.122.0")
    assert events, "expected at least one --json event from codex exec"
    failures = [e for e in events if e.event_type in ("error", "turn.failed")]
    if any("no credits remaining" in str(f.payload) for f in failures):
        pytest.skip(
            "the OpenAI account behind this Codex CLI install has no API credits — "
            "an account limitation, not an adapter defect; success-path event "
            "schema (item.completed/turn.completed) remains unverified"
        )
    assert not failures, f"codex exec reported failure event(s): {failures}"
