"""Every RuntimeAdapter must satisfy the same shape.

Not a smoke test of any particular runtime — this guards the anti-corruption
boundary itself: if an adapter drifts from the RuntimeAdapter ABC, this fails
at collection time (ABC instantiation), before any runtime-specific test runs.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from mission_control.adapters.base import RuntimeAdapter
from mission_control.adapters.claude_code import ClaudeCodeRuntimeAdapter
from mission_control.adapters.codex import CodexRuntimeAdapter
from mission_control.adapters.hermes import HermesRuntimeAdapter
from mission_control.adapters.openclaw import OpenClawRuntimeAdapter
from mission_control.adapters.types import (
    ErrorFamily,
    RuntimeAdapterError,
    RuntimeSpec,
    RuntimeSource,
    RuntimeType,
    SessionHandle,
    SessionRequest,
    Task,
    Workspace,
)

ALL_ADAPTERS = [
    HermesRuntimeAdapter,
    OpenClawRuntimeAdapter,
    ClaudeCodeRuntimeAdapter,
    CodexRuntimeAdapter,
]


@pytest.mark.parametrize("adapter_cls", ALL_ADAPTERS)
def test_adapter_satisfies_contract(adapter_cls):
    adapter = adapter_cls()
    assert isinstance(adapter, RuntimeAdapter)


@pytest.mark.parametrize("adapter_cls", ALL_ADAPTERS)
async def test_install_rejects_missing_binary(adapter_cls, monkeypatch):
    """Every adapter must fail with ErrorFamily.NOT_FOUND, never a bare
    exception, when its CLI isn't on PATH — this is what lets the scheduler
    react uniformly regardless of which runtime is missing."""
    monkeypatch.setattr("shutil.which", lambda _name: None)
    adapter = adapter_cls()
    spec = RuntimeSpec(
        type=RuntimeType(adapter_cls.__module__.split(".")[-2]),
        source=RuntimeSource(
            repository="https://example.invalid/repo",
            version="0.0.0",
            date_validated="2026-08-27",
        ),
    )
    with pytest.raises(RuntimeAdapterError) as excinfo:
        await adapter.install(spec)
    assert excinfo.value.family == ErrorFamily.NOT_FOUND


@pytest.mark.parametrize("adapter_cls", ALL_ADAPTERS)
async def test_health_on_unknown_session_is_unknown_not_a_crash(adapter_cls):
    from mission_control.adapters.types import HealthStatus, RuntimeType, SessionHandle
    from datetime import UTC, datetime

    adapter = adapter_cls()
    bogus_handle = SessionHandle(
        session_id="does-not-exist",
        runtime_type=RuntimeType(adapter_cls.__module__.split(".")[-2]),
        started_at=datetime.now(UTC),
    )
    report = await adapter.health(bogus_handle)
    assert report.status in (HealthStatus.UNKNOWN, HealthStatus.UNHEALTHY)


async def test_claude_code_send_task_passes_prompt_via_stdin():
    """Verify that send_task passes the prompt via stdin, not as a positional
    CLI argument. This is critical on Windows where cmd.exe's /c tokenizer
    will truncate arguments containing embedded newlines at the first \n,
    silently dropping flags like --output-format stream-json.

    The fix passes the prompt via stdin (which cmd.exe never tokenizes) and
    omits task.instructions from the args list.
    """
    from datetime import UTC, datetime

    adapter = ClaudeCodeRuntimeAdapter()

    # Create a minimal session
    workspace = Workspace(path=Path("/tmp/test"))
    handle = await adapter.start(SessionRequest(
        mission_id="m1",
        task_id="t1",
        workspace=workspace,
    ))

    # Multi-line prompt that would be truncated on Windows if passed as arg
    multi_line_prompt = "Say only the word OK.\n\nThis is a second line."
    task = Task(id="t1", mission_id="m1", instructions=multi_line_prompt)

    # Capture what create_subprocess is called with
    captured_args = None
    captured_stdin_writes = []

    async def mock_create_subprocess(*args, **kwargs):
        nonlocal captured_args, captured_stdin_writes
        captured_args = args

        # Create a mock process with a fake stdin
        mock_process = AsyncMock()
        mock_stdin = Mock()
        mock_stdin.write = Mock(return_value=None)
        mock_stdin.drain = AsyncMock()
        mock_stdin.close = Mock()
        mock_process.stdin = mock_stdin
        mock_process.stdout = AsyncMock()
        mock_process.returncode = 0

        # Track stdin writes
        def track_write(data):
            captured_stdin_writes.append(data)
            return None
        mock_stdin.write.side_effect = track_write

        # Create an async iterator that returns nothing (empty stream)
        async def empty_stream():
            return
            yield  # make it a generator
        mock_process.stdout.__aiter__ = Mock(return_value=empty_stream())
        mock_process.wait = AsyncMock()

        return mock_process

    with patch("mission_control.adapters.claude_code.adapter.create_subprocess", side_effect=mock_create_subprocess):
        ack = await adapter.send_task(handle, task)
        assert ack.accepted

    # Verify the fix's contract:
    # 1. task.instructions should NOT be in the args list (it was before the fix)
    assert multi_line_prompt not in captured_args, (
        "BUG: prompt is still being passed as a positional argument! "
        "This will be truncated by cmd.exe on Windows."
    )

    # 2. The prompt should be passed via stdin instead
    assert len(captured_stdin_writes) > 0, "prompt was not written to stdin"
    written_prompt = captured_stdin_writes[0]
    assert written_prompt == multi_line_prompt.encode(), (
        f"Expected prompt to be written to stdin as encoded bytes, "
        f"got {written_prompt!r}"
    )

    # 3. Verify the args list still has all the critical flags
    args_str = " ".join(captured_args)
    assert "--output-format" in args_str, "missing --output-format flag"
    assert "stream-json" in args_str, "missing stream-json value"
    assert "--verbose" in args_str, "missing --verbose flag"
    assert "--include-partial-messages" in args_str, "missing --include-partial-messages flag"
    assert "--permission-mode" in args_str, "missing --permission-mode flag"
    assert "acceptEdits" in args_str, "missing acceptEdits value"

    # Clean up
    await adapter.destroy(handle)


async def test_claude_code_start_with_resume_native_ref_causes_resume_flag():
    """A task reopened after finishing (or after a server restart) has no
    live adapter session, but the DB remembers Claude's own session id.
    start() must seed that into the new session so the *next* send_task
    call passes --resume, without needing any turn to have run first."""
    from datetime import UTC, datetime

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


async def test_claude_code_pump_events_attaches_native_ref():
    """Once a stream-json line reveals Claude's session_id, every
    subsequently-emitted RuntimeEvent must carry it as native_ref — this is
    what lets _execute_task persist a durable, resumable session id."""
    from datetime import UTC, datetime

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
