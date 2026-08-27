"""The RuntimeAdapter contract — the only interface Mission Control uses to
talk to an external agent runtime.

See docs/architecture/00-foundational-architecture.md section 3. Every
runtime-specific concept must stay inside a `mission_control.adapters.<name>`
package; nothing outside an adapter module may depend on a runtime's CLI
flags, config format, or wire protocol directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from mission_control.adapters.types import (
    DeployResult,
    HealthReport,
    LogLine,
    RuntimeConfig,
    RuntimeEvent,
    RuntimeSpec,
    SessionHandle,
    SessionRequest,
    Task,
    TaskAck,
    Workspace,
)


class RuntimeAdapter(ABC):
    """Anti-corruption layer for one external agent runtime.

    Implementations must translate every runtime-native error into a
    `RuntimeAdapterError` carrying an `ErrorFamily` (see types.py) rather
    than letting a runtime-specific exception escape the adapter boundary.
    """

    @abstractmethod
    async def install(self, spec: RuntimeSpec) -> None:
        """Ensure the pinned runtime version is present on this host."""

    @abstractmethod
    async def configure(self, config: RuntimeConfig) -> None:
        """Write the runtime's native config for an isolated instance."""

    @abstractmethod
    async def deploy(self, workspace: Workspace) -> DeployResult:
        """Prepare the runtime to operate against a given workspace."""

    @abstractmethod
    async def start(self, session: SessionRequest) -> SessionHandle:
        """Start a session and return an opaque handle."""

    @abstractmethod
    async def stop(self, handle: SessionHandle) -> None:
        """Stop a session gracefully."""

    @abstractmethod
    async def restart(self, handle: SessionHandle) -> SessionHandle:
        """Restart a session, returning a (possibly new) handle."""

    @abstractmethod
    async def health(self, handle: SessionHandle) -> HealthReport:
        """Check liveness/readiness of a running session."""

    @abstractmethod
    async def send_task(self, handle: SessionHandle, task: Task) -> TaskAck:
        """Hand a task to a running session."""

    @abstractmethod
    def stream_events(self, handle: SessionHandle) -> AsyncIterator[RuntimeEvent]:
        """Stream normalized events (progress, cost, approval requests)."""

    @abstractmethod
    def get_logs(self, handle: SessionHandle) -> AsyncIterator[LogLine]:
        """Stream raw stdout/stderr for debugging."""

    @abstractmethod
    async def destroy(self, handle: SessionHandle) -> None:
        """Tear down a session and release any runtime-held resources."""
