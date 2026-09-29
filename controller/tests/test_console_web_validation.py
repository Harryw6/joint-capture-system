"""Browser-level regression: a failed recheck must show its actual reason."""

import shutil
import subprocess
import threading

import pytest

from jointctl.console import make_server


CHROME = shutil.which("chrome") or r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def browser_html(tmp_path, snapshot):
    class State:
        root = tmp_path
        config_path = tmp_path / "config.json"

        def snapshot(self):
            return snapshot

    server = make_server(State(), 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run([
            CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", f"--user-data-dir={tmp_path / 'chrome'}",
            "--virtual-time-budget=3000", "--dump-dom",
            f"http://127.0.0.1:{server.server_port}/",
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        assert result.returncode == 0, result.stderr[-1000:]
        return result.stdout
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.skipif(not __import__("os").path.isfile(CHROME), reason="Chrome not installed")
def test_failed_validation_reason_visible_without_opening_logs(tmp_path):
    html = browser_html(tmp_path, {
                "active": False,
                "operation_busy": False,
                "hosts": {name: {"status": {"stale": False, "active": False,
                                              "state": "idle", "reachable": True},
                                 "resources": {"stale": True}}
                          for name in ("p450", "unitree")},
                "episode": {"episode_id": "joint_test", "state": "complete",
                            "metadata": {"postprocess": "failed"}},
                "job": None,
                "allowed": {name: False for name in
                            ("start", "stop", "recover", "finalize", "align", "prepare")},
                "postprocess": {"pending_count": 1},
                "clock": {"degraded": True, "reasons": []},
                "validation": {"state": "failed", "report": None, "path": None,
                               "error": "summary.json frames_dropped must be zero"},
                "elapsed_s": None, "remaining_s": None,
                "disk_warning_bytes": 10 * 1024 ** 3,
            })
    assert "summary.json frames_dropped must be zero" in html


@pytest.mark.skipif(not __import__("os").path.isfile(CHROME), reason="Chrome not installed")
def test_legacy_recorder_drops_warn_during_recording(tmp_path):
    hosts = {name: {"status": {"stale": False, "active": True,
                               "state": "recording", "reachable": True,
                               "episode_id": "joint_test"},
                    "resources": {"stale": True}, "write_stalled": False}
             for name in ("p450", "unitree")}
    hosts["unitree"]["frames_dropped"] = 12
    hosts["unitree"]["save_errors"] = 0
    html = browser_html(tmp_path, {
        "active": True, "operation_busy": False, "hosts": hosts,
        "episode": {"episode_id": "joint_test", "state": "recording",
                    "t0_desktop_ns": "1790431394494818200", "metadata": {}},
        "job": None, "allowed": {name: False for name in
                             ("start", "stop", "recover", "finalize", "align", "prepare")},
        "postprocess": {"pending_count": 0},
        "clock": {"degraded": False, "reasons": [], "estimated_error_ms": 3.5},
        "validation": {"state": "pending", "report": None, "path": None},
        "elapsed_s": 10, "remaining_s": 1700, "disk_warning_bytes": 10 * 1024 ** 3,
    })
    assert "Dropped: 12" in html
    assert 'id="session-pill" class="pill green"' not in html
