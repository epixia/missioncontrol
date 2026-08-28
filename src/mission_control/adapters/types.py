"""Shared schemas for the RuntimeAdapter contract.

These types are the anti-corruption boundary between Mission Control's own
mission/policy/budget model and whatever a given runtime (Hermes, OpenClaw,
Claude Code, Codex) natively speaks. Every adapter method in
`mission_control.adapters.base.RuntimeAdapter` takes and returns only these
types — never a runtime-specific object.

Modeled on (not vendored from) paperclipai/paperclip's
packages/adapter-utils/src/types.ts (AdapterExecutionContext,
AdapterExecutionResult, AdapterRuntimeEvent), which already proved the need
for structured error families and cost/usage accounting on every event.
See docs/architecture/00-foundational-architecture.md section 3.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class RuntimeType(StrEnum):
    HERMES = "hermes"
    OPENCLAW = "openclaw"
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"


class ErrorFamily(StrEnum):
    """Coarse-grained, runtime-agnostic error classification.

    Every adapter must map its runtime's native errors onto this set so that
    Mission Control's policy/scheduler layer can react without knowing which
    runtime raised the error (e.g. AUTH and RATE_LIMIT both suspend a
    session; SANDBOX_DENIED and APPROVAL_DENIED both route to governance).
    """

    AUTH = "auth"
    SANDBOX_DENIED = "sandbox_denied"
    APPROVAL_DENIED = "approval_denied"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    RUNTIME_CRASH = "runtime_crash"
    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


class RuntimeSource(BaseModel):
    """Repository pinning record — see architecture doc section 10."""

    repository: str
    version: str
    commit: str | None = None
    adapter_version: int = 1
    date_validated: str


class RuntimeSpec(BaseModel):
    """What to install: which runtime, which pinned version."""

    type: RuntimeType
    source: RuntimeSource
    install_options: dict[str, Any] = Field(default_factory=dict)


class Workspace(BaseModel):
    """A Git-coordinated working directory handed to a runtime.

    Mission Control's Git Coordination subsystem owns worktree/branch
    allocation; adapters only ever receive an already-allocated workspace and
    must never push to remote themselves (the "no-remote-git" invariant,
    adopted from paperclipai/paperclip's packages/adapters/AUTHORING.md).
    """

    path: Path
    git_ref: str | None = None
    worktree_id: str | None = None


class RuntimeConfig(BaseModel):
    """Per-runtime configuration, written by `configure()`.

    `state_dir` / `config_path` / `port` mirror OpenClaw's proven isolation
    model (OPENCLAW_STATE_DIR / OPENCLAW_CONFIG_PATH / gateway.port) so every
    adapter gets non-colliding state even when multiple instances of the same
    runtime type run side by side.
    """

    runtime_type: RuntimeType
    state_dir: Path
    config_path: Path | None = None
    workspace: Workspace | None = None
    port: int | None = None
    env: dict[str, str] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)


class DeployResult(BaseModel):
    success: bool
    message: str = ""
    endpoint: str | None = None


class SessionRequest(BaseModel):
    mission_id: str
    task_id: str
    workspace: Workspace
    model_route: str | None = None
    """Optional hint passed through to the Model Gateway (e.g. 'qwen-local',
    'claude-sonnet-5'). Adapters never resolve this themselves."""
    extra: dict[str, Any] = Field(default_factory=dict)
    resume_native_ref: str | None = None
    """A previously-seen runtime-native session id (see `RuntimeEvent.native_ref`)
    to resume, when starting a session for a task that has run before —
    even in an earlier server process. Adapters that don't support
    resuming from a bare native ref (i.e. everything but Claude Code today)
    ignore this field."""


class SessionHandle(BaseModel):
    session_id: str
    runtime_type: RuntimeType
    started_at: datetime
    native_ref: str | None = None
    """Runtime-native session identifier (e.g. Claude Code's `session_id`,
    Codex's `SESSION_ID`, an OpenClaw gateway session key). Opaque outside
    the adapter that produced it."""


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class HealthReport(BaseModel):
    status: HealthStatus
    detail: str = ""
    checked_at: datetime


class Task(BaseModel):
    id: str
    mission_id: str
    instructions: str
    context_refs: list[str] = Field(default_factory=list)


class TaskAck(BaseModel):
    accepted: bool
    task_id: str
    reason: str | None = None


class CostEvent(BaseModel):
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class ApprovalRequest(BaseModel):
    """Raised by an adapter when the underlying runtime wants a human gate.

    Mission Control's own governance subsystem owns resolution; the adapter
    only translates the runtime-native approval prompt (e.g. Claude Code's
    `permissionDecision: allow|deny|escalate` hook, Hermes's approval
    transport, Codex's approval policy) into this shape and back.
    """

    id: str
    description: str
    risk_level: Literal["low", "medium", "high"] = "medium"


class RuntimeEvent(BaseModel):
    """A single item from `RuntimeAdapter.stream_events()`.

    Exactly one of `cost`, `approval_request`, or `payload` is typically
    populated per event; all three fields exist on every event so consumers
    don't need per-runtime event-type branching.
    """

    session_id: str
    event_type: str
    timestamp: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    cost: CostEvent | None = None
    approval_request: ApprovalRequest | None = None
    error_family: ErrorFamily | None = None
    native_ref: str | None = None
    """The runtime-native session id this event belongs to, when the
    adapter can determine it (e.g. Claude Code's `session_id` field on
    stream-json events). Lets callers persist a durable, resumable session
    reference without depending on adapter-internal state."""


class LogLine(BaseModel):
    timestamp: datetime
    stream: Literal["stdout", "stderr"]
    text: str


class RuntimeAdapterError(Exception):
    """Raised by adapter methods; always carries a structured ErrorFamily."""

    def __init__(self, family: ErrorFamily, message: str) -> None:
        super().__init__(message)
        self.family = family
        self.message = message
