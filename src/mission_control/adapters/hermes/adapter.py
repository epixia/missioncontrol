"""Adapter for NousResearch/hermes-agent.

Integration surface, per docs/architecture/00-foundational-architecture.md
section 4.1, corrected after investigating NousResearch/hermes-paperclip-adapter
(MIT, npm `hermes-paperclip-adapter`, commit `937ea71a34f5efcaa3834b11fdd08cfc1c99cb2c`)
— an existing, actively-used Hermes adapter for Paperclip. It does NOT use
Hermes's MCP server; it spawns `hermes chat -q "<prompt>" -Q` (quiet,
single-query mode) as a child process and parses stdout for a
`session_id: <id>` line, token usage, and a `$<cost>` figure — the same
subprocess-and-parse shape already used by `ClaudeCodeRuntimeAdapter` and
`CodexRuntimeAdapter` in this codebase, not the MCP-client design originally
assumed here. `--resume <id>` continues a session; `--source tool` marks the
invocation as non-interactive.

Deliberate deviation from hermes-paperclip-adapter: that adapter always
passes `--yolo` to bypass Hermes's dangerous-command approval prompts
("agents have no TTY"). Mission Control does not inherit that default —
approval bypass must be an explicit, policy-gated opt-in via
`RuntimeConfig.extra["bypass_approvals"]`, never implicit, per this
project's own governance/approval model (see architecture doc section 2).

Exact stdout parsing (regexes below) has not been validated against a live
Hermes install in this environment — `hermes` is not on PATH here. Reconcile
against `hermes-paperclip-adapter`'s `src/server/execute.ts` before relying
on this in production.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import yaml

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

_HERMES_BIN = "hermes"
_SESSION_ID_RE = re.compile(r"session_id:\s*(\S+)")
_COST_RE = re.compile(r"\$(\d+(?:\.\d+)?)")
_USAGE_RE = re.compile(r"(\d+)\s*input.*?(\d+)\s*output", re.IGNORECASE)


class _SessionState:
    def __init__(self, workspace: Workspace, bypass_approvals: bool) -> None:
        self.workspace = workspace
        self.bypass_approvals = bypass_approvals
        self.process: asyncio.subprocess.Process | None = None
        self.queue: asyncio.Queue[RuntimeEvent | None] = asyncio.Queue()
        self.native_ref: str | None = None


class HermesRuntimeAdapter(RuntimeAdapter):
    def __init__(self) -> None:
        self._sessions: dict[str, _SessionState] = {}
        self._pending_bypass_approvals = False

    async def install(self, spec: RuntimeSpec) -> None:
        if shutil.which(_HERMES_BIN) is None:
            raise RuntimeAdapterError(
                ErrorFamily.NOT_FOUND,
                f"'{_HERMES_BIN}' not on PATH; install the pinned "
                f"'{spec.source.version}' release from {spec.source.repository}",
            )

    async def configure(self, config: RuntimeConfig) -> None:
        """Write `~/.hermes/config.yaml`-shaped config for an isolated instance.

        Hermes has no documented per-instance state-dir/port isolation flags
        (unlike OpenClaw); Mission Control owns isolation here by pointing
        config at `config.state_dir` rather than depending on a runtime-native
        mechanism.
        """
        config.state_dir.mkdir(parents=True, exist_ok=True)
        config_path = config.config_path or (config.state_dir / "config.yaml")
        config_path.write_text(yaml.safe_dump(config.extra.get("hermes_config", {})))
        self._pending_bypass_approvals = bool(config.extra.get("bypass_approvals", False))

    async def deploy(self, workspace: Workspace) -> DeployResult:
        if not workspace.path.is_dir():
            return DeployResult(success=False, message=f"workspace {workspace.path} does not exist")
        return DeployResult(success=True)

    async def start(self, session: SessionRequest) -> SessionHandle:
        session_id = str(uuid.uuid4())
        self._sessions[session_id] = _SessionState(session.workspace, self._pending_bypass_approvals)
        return SessionHandle(
            session_id=session_id,
            runtime_type=RuntimeType.HERMES,
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
        if shutil.which(_HERMES_BIN) is None:
            return HealthReport(status=HealthStatus.UNHEALTHY, detail="hermes CLI not on PATH", checked_at=now)
        if state.process is None:
            return HealthReport(status=HealthStatus.HEALTHY, detail="idle, no task running", checked_at=now)
        if state.process.returncode is None:
            return HealthReport(status=HealthStatus.HEALTHY, detail="task running", checked_at=now)
        status = HealthStatus.HEALTHY if state.process.returncode == 0 else HealthStatus.UNHEALTHY
        return HealthReport(status=status, detail=f"exited {state.process.returncode}", checked_at=now)

    async def send_task(self, handle: SessionHandle, task: Task) -> TaskAck:
        state = self._require(handle)
        args = [_HERMES_BIN, "chat", "-q", task.instructions, "-Q", "--source", "tool"]
        if state.native_ref:
            args += ["--resume", state.native_ref]
        if state.bypass_approvals:
            # Explicit, policy-gated opt-in only — never the default. See
            # module docstring: hermes-paperclip-adapter defaults this on,
            # Mission Control deliberately does not.
            args += ["--yolo"]
        try:
            state.process = await create_subprocess(
                *args,
                cwd=str(state.workspace.path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise RuntimeAdapterError(ErrorFamily.RUNTIME_CRASH, str(exc)) from exc
        asyncio.create_task(self._pump_events(handle.session_id, state))
        return TaskAck(accepted=True, task_id=task.id)

    async def _pump_events(self, session_id: str, state: _SessionState) -> None:
        assert state.process is not None and state.process.stdout is not None
        async for raw_line in state.process.stdout:
            line = raw_line.decode(errors="replace").rstrip()
            if not line:
                continue
            if sid_match := _SESSION_ID_RE.search(line):
                state.native_ref = sid_match.group(1)
            await state.queue.put(
                RuntimeEvent(
                    session_id=session_id,
                    event_type="stdout_line",
                    timestamp=datetime.now(UTC),
                    payload={"line": line},
                    cost=self._extract_cost(line),
                )
            )
        await state.process.wait()  # reap the process so .returncode is set for health()
        await state.queue.put(None)

    @staticmethod
    def _extract_cost(line: str) -> CostEvent | None:
        cost_match = _COST_RE.search(line)
        usage_match = _USAGE_RE.search(line)
        if not cost_match and not usage_match:
            return None
        return CostEvent(
            cost_usd=float(cost_match.group(1)) if cost_match else 0.0,
            input_tokens=int(usage_match.group(1)) if usage_match else 0,
            output_tokens=int(usage_match.group(2)) if usage_match else 0,
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
