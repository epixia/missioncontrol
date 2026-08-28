"""Mission Control's own persisted data model (§2/§6 of the architecture doc:
mission model + observability are core subsystems Mission Control owns —
never delegated to a runtime adapter). SQLite via SQLModel for the v0
platform; nothing here is runtime-specific.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlmodel import Field, SQLModel


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


class AgentRole(StrEnum):
    ORCHESTRATOR = "orchestrator"
    CODER = "coder"
    REVIEWER = "reviewer"


class Mission(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    name: str
    created_at: datetime = Field(default_factory=_now)


class MissionTask(SQLModel, table=True):
    id: str = Field(default_factory=_uuid, primary_key=True)
    mission_id: str = Field(foreign_key="mission.id", index=True)
    runtime: str
    prompt: str
    workspace_path: str
    status: TaskStatus = Field(default=TaskStatus.PENDING)
    session_id: str | None = None
    error_detail: str | None = None
    total_cost_usd: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    role: AgentRole | None = Field(default=None)
    pipeline_run_id: str | None = Field(default=None, index=True)
    result_text: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class TaskEvent(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    task_id: str = Field(foreign_key="missiontask.id", index=True)
    event_type: str
    payload_json: str
    error_family: str | None = None
    timestamp: datetime = Field(default_factory=_now)
