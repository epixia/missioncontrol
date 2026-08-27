"""One long-lived RuntimeAdapter instance per runtime type, shared across
requests for the life of the server process. Session state (native process
handles) is adapter-internal and does not survive a server restart — that's
acceptable for the v0 platform; crash-recovery of in-flight sessions is a
scheduler-level concern not yet built (see architecture doc §13).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from mission_control.adapters.base import RuntimeAdapter
from mission_control.adapters.claude_code import ClaudeCodeRuntimeAdapter
from mission_control.adapters.codex import CodexRuntimeAdapter
from mission_control.adapters.hermes import HermesRuntimeAdapter
from mission_control.adapters.openclaw import OpenClawRuntimeAdapter
from mission_control.adapters.types import RuntimeSource, RuntimeSpec, RuntimeType

_CONFIGS_DIR = Path(__file__).resolve().parents[3] / "configs" / "runtimes"

_ADAPTER_CLASSES: dict[RuntimeType, type[RuntimeAdapter]] = {
    RuntimeType.HERMES: HermesRuntimeAdapter,
    RuntimeType.OPENCLAW: OpenClawRuntimeAdapter,
    RuntimeType.CLAUDE_CODE: ClaudeCodeRuntimeAdapter,
    RuntimeType.CODEX: CodexRuntimeAdapter,
}

_instances: dict[RuntimeType, RuntimeAdapter] = {}


def get_adapter(runtime: RuntimeType) -> RuntimeAdapter:
    if runtime not in _instances:
        _instances[runtime] = _ADAPTER_CLASSES[runtime]()
    return _instances[runtime]


def load_pinned_spec(runtime: RuntimeType) -> RuntimeSpec:
    path = _CONFIGS_DIR / f"{runtime.value}.yaml"
    data = yaml.safe_load(path.read_text())["runtime"]
    source = data["source"]
    return RuntimeSpec(
        type=runtime,
        source=RuntimeSource(
            repository=source["repository"],
            version=source["version"],
            commit=source.get("commit"),
            adapter_version=data.get("adapter_version", 1),
            date_validated=str(data["date_validated"]),
        ),
    )
