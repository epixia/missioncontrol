"""Every RuntimeAdapter must satisfy the same shape.

Not a smoke test of any particular runtime — this guards the anti-corruption
boundary itself: if an adapter drifts from the RuntimeAdapter ABC, this fails
at collection time (ABC instantiation), before any runtime-specific test runs.
"""

import pytest

from mission_control.adapters.base import RuntimeAdapter
from mission_control.adapters.claude_code import ClaudeCodeRuntimeAdapter
from mission_control.adapters.codex import CodexRuntimeAdapter
from mission_control.adapters.hermes import HermesRuntimeAdapter
from mission_control.adapters.openclaw import OpenClawRuntimeAdapter
from mission_control.adapters.types import ErrorFamily, RuntimeAdapterError, RuntimeSpec, RuntimeSource, RuntimeType

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
