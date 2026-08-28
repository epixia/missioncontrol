"""Offline tests for the two new adapter-contract fields this plan adds:
RuntimeEvent.native_ref and SessionRequest.resume_native_ref. Pure pydantic
model construction — no DB, no adapters, no network."""

from datetime import UTC, datetime

from mission_control.adapters.types import RuntimeEvent, RuntimeType, SessionRequest, Workspace


def test_runtime_event_native_ref_defaults_to_none():
    event = RuntimeEvent(session_id="s1", event_type="assistant", timestamp=datetime.now(UTC))
    assert event.native_ref is None


def test_runtime_event_native_ref_can_be_set():
    event = RuntimeEvent(session_id="s1", event_type="result", timestamp=datetime.now(UTC), native_ref="claude-native-abc")
    assert event.native_ref == "claude-native-abc"


def test_session_request_resume_native_ref_defaults_to_none():
    req = SessionRequest(mission_id="m1", task_id="t1", workspace=Workspace(path="."))
    assert req.resume_native_ref is None


def test_session_request_resume_native_ref_can_be_set():
    req = SessionRequest(mission_id="m1", task_id="t1", workspace=Workspace(path="."), resume_native_ref="claude-native-abc")
    assert req.resume_native_ref == "claude-native-abc"
