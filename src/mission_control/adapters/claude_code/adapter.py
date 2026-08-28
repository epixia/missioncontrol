"""Adapter for Anthropic's Claude Code CLI (@anthropic-ai/claude-code).

Integration surface, per docs/architecture/00-foundational-architecture.md
section 4.3: `claude -p --output-format stream-json` for task execution,
`--settings` for hooks/permissions, `--resume`/`--session-id` for session
continuity. The npm package ships no public source, so nothing here may
depend on anything beyond the documented CLI flags (verified live against
`claude --help` on the installed 2.1.247 build) and the Agent SDK's wire
format.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from mission_control.adapters._proc import create_subprocess
from mission_control.adapters.base import RuntimeAdapter
from mission_control.adapters.types import (
    CostEvent,
    DeployResult,
    ErrorFamily,
    HealthReport,
    HealthStatus,
    LogLine,
    RuntimeAdapterError,
    RuntimeConfig,
    RuntimeEvent,
    RuntimeSpec,
    RuntimeType,
    SessionHandle,
    SessionRequest,
    Task,
    TaskAck,
    Workspace,
)

_CLAUDE_BIN = "claude"


class _SessionState:
    def __init__(self, workspace: Workspace, settings_path: Path | None) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.queue: asyncio.Queue[RuntimeEvent | None] = asyncio.Queue()
        self.workspace = workspace
        self.settings_path = settings_path
        self.native_ref: str | None = None


class ClaudeCodeRuntimeAdapter(RuntimeAdapter):
    def __init__(self) -> None:
        self._sessions: dict[str, _SessionState] = {}
        self._pending_settings_path: Path | None = None

    async def install(self, spec: RuntimeSpec) -> None:
        if shutil.which(_CLAUDE_BIN) is None:
            raise RuntimeAdapterError(
                ErrorFamily.NOT_FOUND,
                f"'{_CLAUDE_BIN}' not on PATH; install with "
                f"`npm install -g @anthropic-ai/claude-code@{spec.source.version}`",
            )
        proc = await create_subprocess(
            _CLAUDE_BIN,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        installed = out.decode().strip()
        if spec.source.version not in installed:
            raise RuntimeAdapterError(
                ErrorFamily.VALIDATION,
                f"installed Claude Code '{installed}' does not match pinned "
                f"version '{spec.source.version}' — see the adapter's runtime.yaml",
            )

    async def configure(self, config: RuntimeConfig) -> None:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        settings_path = config.config_path or (config.state_dir / "settings.json")
        settings_path.write_text(json.dumps(config.extra.get("settings", {}), indent=2))
        self._pending_settings_path = settings_path

    async def deploy(self, workspace: Workspace) -> DeployResult:
        if not workspace.path.is_dir():
            return DeployResult(success=False, message=f"workspace {workspace.path} does not exist")
        return DeployResult(success=True)

    async def start(self, session: SessionRequest) -> SessionHandle:
        state = _SessionState(session.workspace, self._pending_settings_path)
        session_id = str(uuid.uuid4())
        self._sessions[session_id] = state
        return SessionHandle(
            session_id=session_id,
            runtime_type=RuntimeType.CLAUDE_CODE,
            started_at=datetime.now(UTC),
        )

    async def stop(self, handle: SessionHandle) -> None:
        state = self._require(handle)
        if state.process is not None and state.process.returncode is None:
            state.process.terminate()
            await state.process.wait()

    async def restart(self, handle: SessionHandle) -> SessionHandle:
        await self.stop(handle)
        state = self._sessions.pop(handle.session_id)
        return await self.start(SessionRequest(mission_id="", task_id="", workspace=state.workspace))

    async def health(self, handle: SessionHandle) -> HealthReport:
        now = datetime.now(UTC)
        state = self._sessions.get(handle.session_id)
        if state is None:
            return HealthReport(status=HealthStatus.UNKNOWN, detail="unknown session", checked_at=now)
        if state.process is None:
            return HealthReport(status=HealthStatus.HEALTHY, detail="idle, no task running", checked_at=now)
        if state.process.returncode is None:
            return HealthReport(status=HealthStatus.HEALTHY, detail="task running", checked_at=now)
        status = HealthStatus.HEALTHY if state.process.returncode == 0 else HealthStatus.UNHEALTHY
        return HealthReport(status=status, detail=f"exited {state.process.returncode}", checked_at=now)

    async def send_task(self, handle: SessionHandle, task: Task) -> TaskAck:
        state = self._require(handle)
        args = [
            _CLAUDE_BIN,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-mode",
            "acceptEdits",
        ]
        if state.settings_path is not None:
            args += ["--settings", str(state.settings_path)]
        if state.native_ref:
            args += ["--resume", state.native_ref]
        try:
            state.process = await create_subprocess(
                *args,
                cwd=str(state.workspace.path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            state.process.stdin.write(task.instructions.encode())
            await state.process.stdin.drain()
            state.process.stdin.close()
        except OSError as exc:
            raise RuntimeAdapterError(ErrorFamily.RUNTIME_CRASH, str(exc)) from exc
        asyncio.create_task(self._pump_events(handle.session_id, state))
        return TaskAck(accepted=True, task_id=task.id)

    async def _pump_events(self, session_id: str, state: _SessionState) -> None:
        assert state.process is not None and state.process.stdout is not None
        async for raw_line in state.process.stdout:
            line = raw_line.decode(errors="replace").strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
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
        await state.process.wait()  # reap the process so .returncode is set for health()
        await state.queue.put(None)

    @staticmethod
    def _extract_cost(payload: dict) -> CostEvent | None:
        """Only the final `type: result` event carries authoritative,
        whole-turn totals (`total_cost_usd`, `usage.*_tokens`) — confirmed
        against live `claude -p --output-format stream-json` output.
        `assistant` events also carry a `message.usage` block, but it's a
        partial/in-progress snapshot of the *same* turn; summing both would
        double-count tokens and cost, so only `result` is treated as a cost
        source here."""
        if payload.get("type") != "result":
            return None
        usage = payload.get("usage") or {}
        return CostEvent(
            model=payload.get("model"),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cost_usd=payload.get("total_cost_usd") or 0.0,
        )

    async def stream_events(self, handle: SessionHandle) -> AsyncIterator[RuntimeEvent]:
        state = self._require(handle)
        while True:
            event = await state.queue.get()
            if event is None:
                return
            yield event

    def get_logs(self, handle: SessionHandle) -> AsyncIterator[LogLine]:
        state = self._require(handle)
        return self._read_stderr(state)

    @staticmethod
    async def _read_stderr(state: _SessionState) -> AsyncIterator[LogLine]:
        if state.process is None or state.process.stderr is None:
            return
        async for raw_line in state.process.stderr:
            yield LogLine(
                timestamp=datetime.now(UTC),
                stream="stderr",
                text=raw_line.decode(errors="replace").rstrip(),
            )

    async def destroy(self, handle: SessionHandle) -> None:
        await self.stop(handle)
        self._sessions.pop(handle.session_id, None)

    def _require(self, handle: SessionHandle) -> _SessionState:
        state = self._sessions.get(handle.session_id)
        if state is None:
            raise RuntimeAdapterError(ErrorFamily.NOT_FOUND, f"unknown session {handle.session_id}")
        return state
