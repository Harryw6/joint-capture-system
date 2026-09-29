# Local Capture Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship an offline, loopback-only Windows control console for the existing joint recorder.

**Architecture:** Browser consumes cached state; independent background Python service runs bounded SSH telemetry and asynchronous existing CLI jobs. CLI operations share a crash-released OS lock with other CLI processes. Existing recorder and timing algorithms remain authoritative.

**Tech Stack:** Python standard library, Windows OpenSSH, plain HTML/CSS/JavaScript.

**Spec:** docs/superpowers/specs/2026-09-14-local-capture-console-design.md (approved by user).

## Global Constraints

- No robot motion, enable, clock adjustment or automatic capture on service startup.
- Only 127.0.0.1; Host/Origin/token validation; fixed commands; no arbitrary paths or shells.
- Existing default configuration and manifest root; preserve original recordings.
- Status/resources target 2 seconds; stale after 6 seconds; unknown is not zero/idle.
- No external assets, runtime npm dependencies, preview video or automatic watchdog.
- Namespace all files under jointctl; preserve existing uncommitted audit fixes in place.

## Task 1: Telemetry and operation exclusion

Files: src/jointctl/operation_lock.py, telemetry.py; modify cli.py; tests/test_console_core.py.
Interfaces: operation_lock(root: Path) context manager raises OperationBusy; parse_meminfo(text)->dict; resource_script(directory)->str; safe_json(value)->JSON-compatible tree (unsafe integers become strings).

- [ ] Write tests for known RAM fixture, invalid/missing RAM fields, large integer serialization, and competing real OS locks.
```python
assert parse_meminfo('MemTotal: 1000 kB\nMemAvailable: 250 kB\n')['used_bytes'] == 768000
assert safe_json({'t0':1789364402353640400}) == {'t0':'1789364402353640400'}
with operation_lock(tmp_path):
    with pytest.raises(OperationBusy):
        with operation_lock(tmp_path): pass
```
- [ ] Run `python -m pytest tests/test_console_core.py -q`, observe missing-feature failures.
- [ ] Implement nonblocking msvcrt/fcntl lock released on handle close; wrap CLI start/stop/recover/align once in main (stop's internal align stays inside same lock). Nonmutation commands remain unlocked.
- [ ] Implement RAM/disk SSH JSON telemetry with finite timeout, strict numeric validation and directory quoting; test parse and lock behavior; run full baseline regression.

## Task 2: Cached service state and async jobs

Files: src/jointctl/console_state.py; tests/test_console_state.py.
Interfaces: ConsoleState(config_path, root), snapshot()->dict, submit(action,payload)->job dict, start_polling(), close(); operation jobs persisted under root/.console/jobs; no secret in logs.

- [ ] Tests use temporary manifests and injected external probes/runner: unknown initial state, staleness, one active job, command allowlist, restart evidence, per-device freshness, valid report vs complete manifest.
```python
assert state.snapshot()['hosts']['p450']['status']['stale'] is True
with pytest.raises(ValueError): state.submit('shell', {})
```
- [ ] Observe failing tests; implement one independent probe loop per host/category, cache under threading lock, capped backoff, fixed subprocess argv, job metadata plus stdout/stderr files.
- [ ] Derive current/last episode, report phase, paths, safe integer JSON, RAM usage, remaining limit hint and action availability. Readiness/staleness must gate start but not prevent owned stop.
- [ ] Run targeted tests and full regression; self-review async cleanup and no automatic replay.

## Task 3: Local HTTP boundary and user interface

Files: src/jointctl/console.py, console_web/index.html, app.js, style.css; tests/test_console_http.py.
Interfaces: make_server(state,port=0)->HTTPServer; GET /api/state, GET /api/session token, POST /api/actions with action payload; static whitelist only.

- [ ] Real localhost HTTP tests: non-loopback Host rejected; foreign Origin and absent token rejected; unknown path rejected; bounded JSON; safe state; actions return 202 without blocking.
- [ ] Observe test failures, implement handler and headers (no CORS, CSP, no-store, nosniff), thread server, security token.
- [ ] Build Chinese instrument-panel UI: cream/charcoal palette, precise green/amber indicators, large duration, separate host cards, resource bars, paths/copy, error log, report details, responsive layout and accessible buttons. All changing text uses textContent.
- [ ] Browser tests on a test-only fake backend: start, pending state, stop/validate, failed result, long paths, stale network, refresh; no live mutation during mock UI tests.

## Task 4: Launcher, installation and delivery

Files: scripts/JointConsole.bat, Start-JointConsole.ps1; modify installer, pyproject.toml and README; tests/test_console_delivery.py.

- [ ] Test temporary installation includes entry point and web resources and retains backup/data; confirm failed launcher does not open an unrelated service.
- [ ] Implement hidden independent backend process with single-instance bind, fixed Python path and script arguments; launcher health-check identity/config/root before reuse; open browser only once ready.
- [ ] Run complete test suite, browser smoke test and package verification. Install with backup only after local checks. Read-only verify live telemetry. If devices idle and user authorization covers acceptance, run one bounded static UI start/stop test; confirm no active recorder remains.
- [ ] Independent code review; address actionable safety/correctness findings with regressions; write delivery report and provide desktop link.

## Progress

- Plan reviewed against spec: tasks 1/2 share safe_json and lock; 2/3 share snapshot/action schema; 3/4 share launcher/server identity. No conflicting interfaces; exact UI fields will be defined by ConsoleState snapshot tests before UI wiring.
- Existing audit fixes are uncommitted but already installed and live-validated. Do not discard or stash them. No new worktree is created without user direction; implementation stays in the dedicated feature checkout.
