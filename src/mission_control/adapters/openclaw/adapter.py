"""Adapter for openclaw/openclaw.

Integration surface, per docs/architecture/00-foundational-architecture.md
section 4.2: OpenClaw's confirmed multi-Gateway isolation model
(`OPENCLAW_CONFIG_PATH`, `OPENCLAW_STATE_DIR`, `agents.defaults.workspace`,
`gateway.port`, `--profile <name>`) for lifecycle, and `openclaw health
--json` / `openclaw status --deep` for health — both confirmed against
docs/gateway/health.md. Task execution should go through the `openclaw acp`
ACP bridge or the `acpx` harness runner (docs/tools/acp-agents.md), which is
not yet wired here: `openclaw` is not installed in this environment, so the
exact process-launch subcommand for the Gateway itself (as opposed to the
documented health/status/acp subcommands) has not been verified live and is
marked accordingly below.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from mission_control.adapters._proc import create_subprocess
from mission_control.adapters.base import RuntimeAdapter
from mission_control.adapters.types import (
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

_OPENCLAW_BIN = "openclaw"


class _SessionState:
    def __init__(self, workspace: Workspace, profile: str, env: dict[str, str]) -> None:
        self.workspace = workspace
        self.profile = profile
        self.env = env
        self.process: asyncio.subprocess.Process | None = None


class OpenClawRuntimeAdapter(RuntimeAdapter):
    def __init__(self) -> None:
        self._sessions: dict[str, _SessionState] = {}
        self._pending_env: dict[str, str] = {}
        self._pending_profile = "default"

    async def install(self, spec: RuntimeSpec) -> None:
        if shutil.which(_OPENCLAW_BIN) is None:
            raise RuntimeAdapterError(
                ErrorFamily.NOT_FOUND,
                f"'{_OPENCLAW_BIN}' not on PATH; install the pinned "
                f"'{spec.source.version}' release from {spec.source.repository}",
            )

    async def configure(self, config: RuntimeConfig) -> None:
        """Isolate this instance via OpenClaw's documented env-var model.

        Confirmed in docs/gateway/multiple-gateways.md: OPENCLAW_CONFIG_PATH,
        OPENCLAW_STATE_DIR, and gateway.port together give non-colliding
        state even when multiple OpenClaw instances run side by side.
        """
        config.state_dir.mkdir(parents=True, exist_ok=True)
        self._pending_profile = str(config.extra.get("profile", "default"))
        self._pending_env = {
            "OPENCLAW_STATE_DIR": str(config.state_dir),
            **({"OPENCLAW_CONFIG_PATH": str(config.config_path)} if config.config_path else {}),
            **({"gateway.port": str(config.port)} if config.port else {}),
            **config.env,
        }

    async def deploy(self, workspace: Workspace) -> DeployResult:
        if not workspace.path.is_dir():
            return DeployResult(success=False, message=f"workspace {workspace.path} does not exist")
        return DeployResult(success=True)

    async def start(self, session: SessionRequest) -> SessionHandle:
        session_id = str(uuid.uuid4())
        self._sessions[session_id] = _SessionState(session.workspace, self._pending_profile, dict(self._pending_env))
        return SessionHandle(
            session_id=session_id,
            runtime_type=RuntimeType.OPENCLAW,
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
        """Shell out to `openclaw health --json`, confirmed by docs/gateway/health.md."""
        now = datetime.now(UTC)
        state = self._sessions.get(handle.session_id)
        if state is None:
            return HealthReport(status=HealthStatus.UNKNOWN, detail="unknown session", checked_at=now)
        if shutil.which(_OPENCLAW_BIN) is None:
            return HealthReport(status=HealthStatus.UNHEALTHY, detail="openclaw CLI not on PATH", checked_at=now)
        proc = await create_subprocess(
            _OPENCLAW_BIN,
            "health",
            "--json",
            "--profile",
            state.profile,
            env={**os.environ, **state.env},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            return HealthReport(status=HealthStatus.UNHEALTHY, detail=err.decode(errors="replace"), checked_at=now)
        try:
            payload = json.loads(out.decode())
        except json.JSONDecodeError:
            return HealthReport(status=HealthStatus.UNKNOWN, detail="unparseable health output", checked_at=now)
        healthy = bool(payload.get("healthy", payload.get("status") == "ok"))
        return HealthReport(
            status=HealthStatus.HEALTHY if healthy else HealthStatus.DEGRADED,
            detail=json.dumps(payload),
            checked_at=now,
        )

    async def send_task(self, handle: SessionHandle, task: Task) -> TaskAck:
        raise NotImplementedError(
            "OpenClawRuntimeAdapter.send_task requires wiring the `openclaw acp` "
            "ACP bridge (or the acpx harness runner) — not yet exercised against "
            "a live OpenClaw install in this environment"
        )

    def stream_events(self, handle: SessionHandle) -> AsyncIterator[RuntimeEvent]:
        raise NotImplementedError(
            "OpenClawRuntimeAdapter.stream_events requires the ACP event ledger "
            "(src/acp/event-ledger.ts) — not yet wired against a live install"
        )

    def get_logs(self, handle: SessionHandle) -> AsyncIterator[LogLine]:
        raise NotImplementedError(
            "OpenClawRuntimeAdapter.get_logs has no confirmed CLI log-tail "
            "command yet — revisit against a live OpenClaw install"
        )

    async def destroy(self, handle: SessionHandle) -> None:
        await self.stop(handle)
        self._sessions.pop(handle.session_id, None)

    def _require(self, handle: SessionHandle) -> _SessionState:
        state = self._sessions.get(handle.session_id)
        if state is None:
            raise RuntimeAdapterError(ErrorFamily.NOT_FOUND, f"unknown session {handle.session_id}")
        return state
