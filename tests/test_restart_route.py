"""Offline test for POST /api/restart. Mocks subprocess.Popen and
threading.Timer so the test process itself is never actually spawned-over
or exited — calling the real function would kill the pytest process via
os._exit(0) about a second later."""

from mission_control.server import app as app_module


def test_restart_server_spawns_detached_copy_and_schedules_exit(monkeypatch):
    calls = {}

    class FakePopen:
        def __init__(self, args, **kwargs):
            calls["args"] = args
            calls["kwargs"] = kwargs

    class FakeTimer:
        def __init__(self, interval, fn):
            calls["timer_interval"] = interval
            calls["timer_fn"] = fn

        def start(self):
            calls["timer_started"] = True

    monkeypatch.setattr(app_module.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(app_module.threading, "Timer", FakeTimer)

    result = app_module.restart_server()

    assert result == {"restarting": True}
    assert calls["args"] == [app_module.sys.executable, "-m", "mission_control.server"]
    assert calls["timer_interval"] == 1.0
    assert calls["timer_started"] is True
    # Never actually called — proves this test didn't exit the process.
    assert callable(calls["timer_fn"])
