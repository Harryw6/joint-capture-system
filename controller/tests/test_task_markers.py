"""Durable manual task intervals never control or modify the raw capture."""
import copy
import csv
import importlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import uuid

import pytest


WALL = 1789364402353640400
MONO = 1_000_000_000


def context(key="capture-one", **changes):
    value = {
        "key": key,
        "episode_id": key,
        "mode": "joint",
        "healthy": True,
        "reason": None,
        "hosts": {
            "p450": {"episode_id": key, "directory": f"/data/p450/{key}"},
            "unitree": {"episode_id": key, "directory": f"/data/unitree/{key}"},
        },
        "capture_started_desktop_ns": WALL - 1_000_000_000,
        "clock_session_id": "desktop-session-one",
        "timestamp_basis": "desktop_receive_wall_ns",
        "clock_mappings": {"unitree": {"offset_ns": 123456789012345678}},
    }
    value.update(changes)
    return value


def make_store(tmp_path):
    return importlib.import_module("jointctl.task_markers").TaskMarkerStore(tmp_path)


def mark(store, action, ctx=None, *, revision=0, delta=0, request_id=None, **changes):
    return store.apply(
        action, context() if ctx is None else ctx,
        request_id=request_id or str(uuid.uuid4()), expected_revision=revision,
        wall_ns=WALL + delta, monotonic_ns=MONO + delta, **changes,
    )


def test_empty_snapshot_does_not_create_directory(tmp_path):
    store = make_store(tmp_path)
    assert store.snapshot() == {
        "context": None, "current": None, "segments": [], "revision": 0,
        "local_directory": None,
    }
    assert not (tmp_path / "task_markers").exists()
    empty_capture = store.snapshot(context())
    assert empty_capture["revision"] == 0
    assert empty_capture["current"] is None
    assert not (tmp_path / "task_markers").exists()


def test_multiple_task_intervals_share_capture_and_export_exact_nanoseconds(tmp_path):
    store = make_store(tmp_path)
    opened = mark(store, "start")
    assert opened["revision"] == 1
    assert opened["current"]["state"] == "open"
    assert opened["current"]["start_desktop_ns"] == WALL
    assert opened["current"]["start_context"]["clock_mappings"] == context()["clock_mappings"]
    closed = mark(store, "stop", revision=1, delta=2_345_678_901)
    first = copy.deepcopy(closed["segments"][0])
    assert closed["current"] is None
    assert first["state"] == "closed"
    assert first["quality"] == "completed"
    assert first["end_desktop_ns"] == WALL + 2_345_678_901
    assert first["duration_ns"] == 2_345_678_901
    assert first["duration_source"] == "monotonic"
    assert first["end_context"]["hosts"] == context()["hosts"]
    mark(store, "start", revision=2, delta=3_000_000_000)
    final = mark(store, "stop", revision=3, delta=4_000_000_000)
    assert final["segments"][0] == first
    assert [segment["index"] for segment in final["segments"]] == [1, 2]
    assert len({segment["id"] for segment in final["segments"]}) == 2
    directory = Path(final["local_directory"])
    saved = json.loads((directory / "task_segments.json").read_text(encoding="utf-8"))
    assert saved["segments"] == final["segments"]
    assert saved["revision"] == 4
    with (directory / "task_segments.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2
    assert rows[0]["start_desktop_ns"] == str(WALL)
    assert rows[0]["end_desktop_ns"] == str(WALL + 2_345_678_901)
    assert rows[0]["duration_ns"] == "2345678901"
    assert json.loads(rows[0]["start_context"]) == first["start_context"]


def test_reset_replaces_only_current_start_and_retains_audit(tmp_path):
    store = make_store(tmp_path)
    raw = tmp_path / "raw.bin"
    raw.write_bytes(b"continuous original recording")
    mark(store, "start")
    done = mark(store, "stop", revision=1, delta=1_000_000_000)["segments"][0]
    mark(store, "start", revision=2, delta=2_000_000_000)
    revised_context = context(clock_mappings={"unitree": {"offset_ns": 456}})
    reset = mark(store, "reset", revised_context, revision=3, delta=3_000_000_000)
    current = reset["current"]
    assert reset["segments"][0] == done
    assert current["id"] == reset["segments"][1]["id"]
    assert current["index"] == 2
    assert current["reset_count"] == 1
    assert current["start_desktop_ns"] == WALL + 3_000_000_000
    assert current["start_context"] == revised_context
    assert current["reset_history"][0]["start_desktop_ns"] == WALL + 2_000_000_000
    assert current["reset_history"][0]["reset_desktop_ns"] == WALL + 3_000_000_000
    assert current["reset_history"][0]["start_context"] == context()
    final = mark(store, "stop", revision=4, delta=4_000_000_000)
    assert final["segments"][1]["duration_ns"] == 1_000_000_000
    assert raw.read_bytes() == b"continuous original recording"
    events = [json.loads(line) for line in (Path(final["local_directory"]) / "events.jsonl").read_text().splitlines()]
    assert [event["action"] for event in events] == ["start", "stop", "start", "reset", "stop"]


def test_identical_duplicate_is_idempotent_after_time_and_health_change(tmp_path):
    store = make_store(tmp_path)
    request_id = str(uuid.uuid4())
    first = mark(store, "start", request_id=request_id, client_wall_ms=1789364402353)
    duplicate = mark(
        store, "start", context(healthy=False, reason="status expired"), delta=100,
        request_id=request_id, client_wall_ms=1789364402353,
    )
    assert duplicate["revision"] == first["revision"] == 1
    assert duplicate["current"] == first["current"]
    assert duplicate["duplicate_request"] is True
    assert duplicate["last_request_id"] == request_id
    assert duplicate["request_revision"] == 1
    assert len((Path(first["local_directory"]) / "events.jsonl").read_text().splitlines()) == 1
    restarted = make_store(tmp_path)
    replayed = mark(restarted, "start", request_id=request_id, client_wall_ms=1789364402353, delta=200)
    assert replayed["revision"] == 1
    assert replayed["duplicate_request"] is True


@pytest.mark.parametrize("change", [
    {"action": "reset"}, {"revision": 1}, {"client_wall_ms": 456},
])
def test_conflicting_reuse_of_request_id_is_rejected(tmp_path, change):
    store = make_store(tmp_path)
    request_id = str(uuid.uuid4())
    mark(store, "start", request_id=request_id, client_wall_ms=123)
    action = change.get("action", "start")
    revision = change.get("revision", 0)
    client_wall_ms = change.get("client_wall_ms", 123)
    with pytest.raises(ValueError, match="request_id"):
        mark(store, action, request_id=request_id, revision=revision, client_wall_ms=client_wall_ms)
    assert store.snapshot(context())["revision"] == 1


def test_stale_revision_and_invalid_transitions_leave_journal_unchanged(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(RuntimeError, match="open"):
        mark(store, "stop")
    with pytest.raises(RuntimeError, match="open"):
        mark(store, "reset")
    mark(store, "start")
    with pytest.raises(RuntimeError, match="revision"):
        mark(store, "stop", revision=0, delta=1_000_000_000)
    with pytest.raises(RuntimeError, match="open"):
        mark(store, "start", revision=1)
    assert store.snapshot(context())["revision"] == 1


def test_health_blocks_start_and_reset_but_unhealthy_stop_retains_review_interval(tmp_path):
    store = make_store(tmp_path)
    unhealthy = context(healthy=False, reason="no fresh capture health")
    with pytest.raises(RuntimeError, match="healthy"):
        mark(store, "start", unhealthy)
    mark(store, "start")
    with pytest.raises(RuntimeError, match="healthy"):
        mark(store, "reset", unhealthy, revision=1, delta=1_000_000_000)
    final = mark(store, "stop", unhealthy, revision=1, delta=2_000_000_000)
    segment = final["segments"][0]
    assert segment["quality"] == "review_required"
    assert "no fresh capture health" in segment["quality_reasons"]
    assert segment["duration_ns"] == 2_000_000_000


def test_capture_switch_does_not_carry_open_task_to_new_capture(tmp_path):
    store = make_store(tmp_path)
    original = mark(store, "start")
    new_capture = context("capture-two")
    empty = store.snapshot(new_capture)
    assert empty["current"] is None
    assert empty["segments"] == []
    assert empty["revision"] == 0
    new_task = mark(store, "start", new_capture)
    assert new_task["revision"] == 1
    assert new_task["local_directory"] != original["local_directory"]
    assert store.snapshot(context())["current"] == original["current"]


def test_replay_uses_journal_and_rebuilds_derived_exports(tmp_path):
    store = make_store(tmp_path)
    mark(store, "start")
    expected = mark(store, "stop", revision=1, delta=2_000_000_000)
    directory = Path(expected["local_directory"])
    (directory / "task_segments.json").write_text('{"revision":999,"segments":[]}', encoding="utf-8")
    (directory / "task_segments.csv").unlink()
    recovered = make_store(tmp_path).snapshot(context())
    assert recovered["segments"] == expected["segments"]
    assert recovered["revision"] == 2
    assert json.loads((directory / "task_segments.json").read_text())["revision"] == 2
    assert (directory / "task_segments.csv").exists()


def test_snapshot_preserves_saved_remote_mapping_when_current_context_is_sparse(tmp_path):
    store = make_store(tmp_path)
    mark(store, "start")
    sparse = {"key": "capture-one", "episode_id": "capture-one", "mode": "joint", "healthy": False}
    snap = make_store(tmp_path).snapshot(sparse)
    assert snap["context"]["hosts"] == context()["hosts"]
    assert snap["context"]["clock_mappings"] == context()["clock_mappings"]
    assert snap["context"]["healthy"] is False


def test_same_key_cannot_rebind_to_different_capture_identity(tmp_path):
    store = make_store(tmp_path)
    mark(store, "start")
    changed = context(episode_id="other-episode")
    with pytest.raises(ValueError, match="identity"):
        mark(store, "stop", changed, revision=1, delta=1_000_000_000)
    assert store.snapshot(context())["revision"] == 1


@pytest.mark.parametrize("key", ["..", "../elsewhere", "x/y", "x\\y", "C:drive", "CON", "x.", ""])
def test_invalid_capture_keys_cannot_escape_storage_root(tmp_path, key):
    store = make_store(tmp_path)
    with pytest.raises(ValueError, match="key"):
        mark(store, "start", context(key))
    assert not (tmp_path / "task_markers").exists()


def test_invalid_request_fields_do_not_create_storage(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError, match="request_id"):
        mark(store, "start", request_id="not-a-uuid")
    with pytest.raises(ValueError, match="expected_revision"):
        mark(store, "start", revision=True)
    with pytest.raises(ValueError, match="wall_ns"):
        store.apply("start", context(), request_id=str(uuid.uuid4()), expected_revision=0,
                    wall_ns=float(WALL), monotonic_ns=MONO)
    with pytest.raises(ValueError, match="episode_id"):
        mark(store, "start", context(episode_id="../other"))
    assert not (tmp_path / "task_markers").exists()


@pytest.mark.parametrize("tail", [b'{"action":"stop"', b'{"invalid":true}\n'])
def test_journal_corruption_is_visible_and_blocks_new_mutations(tmp_path, tail):
    store = make_store(tmp_path)
    opened = mark(store, "start")
    journal = Path(opened["local_directory"]) / "events.jsonl"
    with journal.open("ab") as handle:
        handle.write(tail)
    original_bytes = journal.read_bytes()
    recovered = make_store(tmp_path)
    snap = recovered.snapshot(context())
    assert snap["error"]
    assert snap["current"] is None
    assert snap["segments"] == []
    with pytest.raises(RuntimeError, match="journal"):
        mark(recovered, "stop", revision=1, delta=1_000_000_000)
    assert journal.read_bytes() == original_bytes


def test_wall_clock_jump_marks_interval_for_review_and_uses_monotonic_duration(tmp_path):
    store = make_store(tmp_path)
    mark(store, "start")
    final = store.apply("stop", context(), request_id=str(uuid.uuid4()), expected_revision=1,
                        wall_ns=WALL + 60_000_000_000, monotonic_ns=MONO + 2_000_000_000)
    segment = final["segments"][0]
    assert segment["duration_ns"] == 2_000_000_000
    assert segment["duration_source"] == "monotonic"
    assert segment["quality"] == "review_required"
    assert "desktop_clock_changed" in segment["quality_reasons"]


def test_negative_timestamp_intervals_are_rejected_without_closing_task(tmp_path):
    store = make_store(tmp_path)
    mark(store, "start")
    with pytest.raises(RuntimeError, match="before"):
        mark(store, "stop", revision=1, delta=-1)
    assert store.snapshot(context())["current"]["state"] == "open"
    assert store.snapshot(context())["revision"] == 1


def test_clock_session_change_uses_wall_duration_and_marks_review(tmp_path):
    store = make_store(tmp_path)
    mark(store, "start")
    new_context = context(clock_session_id="desktop-session-two")
    final = make_store(tmp_path).apply(
        "stop", new_context, request_id=str(uuid.uuid4()), expected_revision=1,
        wall_ns=WALL + 2_000_000_000, monotonic_ns=123,
    )
    segment = final["segments"][0]
    assert segment["duration_ns"] == 2_000_000_000
    assert segment["duration_source"] == "wall"
    assert segment["quality"] == "review_required"
    assert "clock_session_changed" in segment["quality_reasons"]


def test_concurrent_clicks_commit_one_revision(tmp_path):
    store = make_store(tmp_path)
    def start(_):
        try:
            return mark(store, "start")
        except RuntimeError:
            return None
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(start, range(8)))
    assert sum(result is not None for result in results) == 1
    assert store.snapshot(context())["revision"] == 1
    assert len(store.snapshot(context())["segments"]) == 1


def test_duplicate_retry_still_works_after_remote_mapping_is_enriched(tmp_path):
    store = make_store(tmp_path)
    sparse = context(hosts={})
    request_id = str(uuid.uuid4())
    mark(store, "start", sparse, request_id=request_id)
    final = mark(store, "stop", revision=1, delta=1_000_000_000)
    duplicate = mark(store, "start", sparse, request_id=request_id)
    assert duplicate["duplicate_request"] is True
    assert duplicate["request_revision"] == 1
    assert duplicate["revision"] == 2
    assert duplicate["segments"] == final["segments"]


def test_request_id_cannot_be_reused_after_capture_switch_or_restart(tmp_path):
    request_id = str(uuid.uuid4())
    store = make_store(tmp_path)
    mark(store, "start", request_id=request_id)
    with pytest.raises(ValueError, match="request_id"):
        mark(make_store(tmp_path), "start", context("capture-two"), request_id=request_id)
    assert not (tmp_path / "task_markers" / "capture-two").exists()


def test_export_failure_does_not_lose_committed_event_and_retry_repairs_exports(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    writer = store._atomic_bytes
    def unavailable(*_args):
        raise PermissionError("export destination unavailable")
    monkeypatch.setattr(store, "_atomic_bytes", unavailable)
    request_id = str(uuid.uuid4())
    result = mark(store, "start", request_id=request_id)
    assert result["revision"] == 1
    assert result["current"]["state"] == "open"
    assert result["error_type"] == "export_failed"
    assert "journal is durable" in result["error"]
    journal = Path(result["local_directory"]) / "events.jsonl"
    assert len(journal.read_text().splitlines()) == 1
    monkeypatch.setattr(store, "_atomic_bytes", writer)
    retried = mark(store, "start", request_id=request_id)
    assert retried["revision"] == 1
    assert retried["duplicate_request"] is True
    assert "error" not in retried
    assert (journal.parent / "task_segments.json").exists()
    assert make_store(tmp_path).snapshot(context())["current"] == result["current"]


def test_symlink_capture_directory_is_rejected_without_touching_target(tmp_path):
    store = make_store(tmp_path)
    target = tmp_path / "outside"
    target.mkdir()
    base = tmp_path / "task_markers"
    base.mkdir()
    try:
        (base / "capture-one").symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is not permitted on this machine: {exc}")
    with pytest.raises(RuntimeError, match="symlink"):
        mark(store, "start")
    assert not list(target.iterdir())
    assert store.snapshot(context())["error_type"] == "journal_unavailable"


def test_nonconsecutive_or_impossible_journal_event_is_not_accepted(tmp_path):
    store = make_store(tmp_path)
    opened = mark(store, "start")
    journal = Path(opened["local_directory"]) / "events.jsonl"
    first = json.loads(journal.read_text())
    first.update(revision=3, expected_revision=2, request_id=str(uuid.uuid4()), action="stop")
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(first) + "\n")
    snap = make_store(tmp_path).snapshot(context())
    assert "nonconsecutive" in snap["error"]
    assert snap["segments"] == []


def test_negative_monotonic_duration_is_rejected_even_if_wall_clock_advances(tmp_path):
    store = make_store(tmp_path)
    mark(store, "start")
    with pytest.raises(RuntimeError, match="monotonic.*before"):
        store.apply("stop", context(), request_id=str(uuid.uuid4()), expected_revision=1,
                    wall_ns=WALL + 2_000_000_000, monotonic_ns=MONO - 1)
    assert store.snapshot(context())["current"]["state"] == "open"


def test_separate_store_instances_share_thread_lock(tmp_path):
    stores = [make_store(tmp_path) for _ in range(8)]
    def start(store):
        try:
            return mark(store, "start")
        except RuntimeError:
            return None
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(start, stores))
    assert sum(result is not None for result in results) == 1
    assert stores[0].snapshot(context())["revision"] == 1


def test_retry_after_journal_fsync_failure_confirms_durability_before_ack(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    original_fsync = os.fsync
    def unavailable(_descriptor):
        raise OSError("journal device temporarily unavailable")
    monkeypatch.setattr(os, "fsync", unavailable)
    request_id = str(uuid.uuid4())
    with pytest.raises(RuntimeError, match="journal append failed"):
        mark(store, "start", request_id=request_id)
    journal = tmp_path / "task_markers" / "capture-one" / "events.jsonl"
    journal_inode = journal.stat().st_ino
    confirmed = []
    def available(descriptor):
        if os.fstat(descriptor).st_ino == journal_inode:
            confirmed.append(True)
        original_fsync(descriptor)
    monkeypatch.setattr(os, "fsync", available)
    recovered = mark(store, "start", request_id=request_id)
    assert recovered["revision"] == 1
    assert recovered["duplicate_request"] is True
    assert confirmed, "retry must fsync the journal, not only its derived exports"
    assert len(journal.read_text().splitlines()) == 1


def test_repeated_snapshot_does_not_rewrite_unchanged_exports(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    opened = mark(store, "start")
    directory = Path(opened["local_directory"])
    files = [directory / name for name in ("events.jsonl", "task_segments.json", "task_segments.csv")]
    before = {path: path.stat().st_mtime_ns for path in files}
    replacements = []
    original_replace = os.replace
    def replace(source, target):
        replacements.append(target)
        original_replace(source, target)
    monkeypatch.setattr(os, "replace", replace)
    for _ in range(3):
        assert store.snapshot(context())["revision"] == 1
    assert {path: path.stat().st_mtime_ns for path in files} == before
    assert not replacements
