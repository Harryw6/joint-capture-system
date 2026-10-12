"""Durable desktop timestamps for tasks within one continuous raw capture.

The fsynced JSON-lines journal is authoritative. JSON and CSV are replaceable
exports; replay never trusts them. This module does not contact recorders or
modify their files. A completed marker means timestamps were paired, not that
the underlying recording passed data validation.
"""
from __future__ import annotations

import copy
import csv
import io
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import uuid


_VERSION = 1
_MAX_NS = (1 << 63) - 1
_CLOCK_JUMP_NS = 1_000_000_000
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z")
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", "CLOCK$"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
}
_LOCKS: dict[Path, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class TaskMarkerError(RuntimeError):
    """A marker transition cannot safely be applied."""


class TaskMarkerStorageError(TaskMarkerError):
    """The authoritative journal is unavailable or invalid."""


def _identifier(value, name):
    if (not isinstance(value, str) or not _IDENTIFIER.fullmatch(value)
            or value.endswith(".") or value.split(".", 1)[0].upper() in _RESERVED_NAMES):
        raise ValueError(f"invalid {name}")
    return value


def _integer(value, name, *, minimum=0):
    if type(value) is not int or not minimum <= value <= _MAX_NS:
        raise ValueError(f"invalid {name}: expected an exact nonnegative int64")
    return value


def _request_id(value):
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise ValueError("invalid request_id: expected a UUID")
    return str(uuid.UUID(value))


def _json_copy(value):
    try:
        return json.loads(json.dumps(value, allow_nan=False, ensure_ascii=False))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("context must contain finite JSON values") from exc


def _context(value):
    if not isinstance(value, dict):
        raise ValueError("invalid capture context")
    value = _json_copy(value)
    _identifier(value.get("key"), "context key")
    if value.get("episode_id") is not None:
        _identifier(value["episode_id"], "episode_id")
    if value.get("mode") is not None:
        _identifier(value["mode"], "mode")
    if "healthy" in value and type(value["healthy"]) is not bool:
        raise ValueError("invalid context healthy")
    if "ready" in value and type(value["ready"]) is not bool:
        raise ValueError("invalid context ready")
    if value.get("reason") is not None and not isinstance(value["reason"], str):
        raise ValueError("invalid context reason")
    if value.get("capture_started_desktop_ns") is not None:
        _integer(value["capture_started_desktop_ns"], "capture_started_desktop_ns")
    if value.get("clock_session_id") is not None:
        session = value["clock_session_id"]
        if not isinstance(session, str) or not session or len(session) > 200 or "\0" in session:
            raise ValueError("invalid clock_session_id")
    if value.get("clock_mappings") is not None and not isinstance(value["clock_mappings"], dict):
        raise ValueError("invalid clock_mappings")
    hosts = value.get("hosts")
    if hosts is not None:
        if not isinstance(hosts, dict):
            raise ValueError("invalid context hosts")
        for host, details in hosts.items():
            _identifier(host, "host")
            if not isinstance(details, dict):
                raise ValueError("invalid host mapping")
            if details.get("episode_id") is not None:
                _identifier(details["episode_id"], "host episode_id")
            directory = details.get("directory")
            if directory is not None and (
                    not isinstance(directory, str) or not directory or "\0" in directory
                    or len(directory) > 4096):
                raise ValueError("invalid remote directory")
    return value


def _merge_context(saved, incoming):
    """Keep known remote mappings while refusing to rebind a storage key."""
    merged = copy.deepcopy(saved or {})
    for name in ("key", "episode_id", "mode", "capture_started_desktop_ns"):
        old, new = merged.get(name), incoming.get(name)
        if old is not None and new is not None and old != new:
            raise ValueError(f"capture identity changed: {name}")
    for name, value in incoming.items():
        if name in ("hosts", "clock_mappings"):
            if value is None:
                continue
            existing = merged.setdefault(name, {})
            for key, details in value.items():
                if name == "hosts":
                    previous = existing.setdefault(key, {})
                    for identity in ("episode_id", "directory"):
                        old, new = previous.get(identity), details.get(identity)
                        if old is not None and new is not None and old != new:
                            raise ValueError(f"capture identity changed: hosts.{key}.{identity}")
                    previous.update({field: copy.deepcopy(item) for field, item in details.items()
                                     if item is not None or field not in previous})
                else:
                    existing[key] = copy.deepcopy(details)
        elif value is not None or name not in (
                "episode_id", "mode", "capture_started_desktop_ns", "clock_session_id"):
            merged[name] = copy.deepcopy(value)
    return merged


def _capture_identity(context):
    return {
        "key": context["key"], "episode_id": context.get("episode_id"),
        "mode": context.get("mode"),
    }


def _fingerprint(event):
    # Receive times, health, clock probes and newly resolved host mappings can
    # differ on a network retry. The capture identity itself cannot change.
    return {"action": event["action"], "expected_revision": event["expected_revision"],
            "client_wall_ms": event["client_wall_ms"],
            "capture": _capture_identity(event["context"])}


def _new_state():
    return {"context": None, "segments": [], "revision": 0, "requests": {}, "last": None}


def _open_segment(state):
    return state["segments"][-1] if state["segments"] and state["segments"][-1]["state"] == "open" else None


def _transition(state, event):
    action, context = event["action"], event["context"]
    wall, monotonic = event["wall_ns"], event["monotonic_ns"]
    current = _open_segment(state)
    if action in ("start", "reset") and (
            context.get("healthy") is not True or context.get("ready") is False):
        raise TaskMarkerError(f"capture is not healthy: {context.get('reason') or 'no fresh capture health'}")
    if action == "start" and current is not None:
        raise TaskMarkerError("a task is already open")
    if action in ("stop", "reset") and current is None:
        raise TaskMarkerError("no task is open")
    capture_start = context.get("capture_started_desktop_ns")
    if action in ("start", "reset") and capture_start is not None and wall < capture_start:
        raise TaskMarkerError("task start is before capture start")
    if current is not None and action in ("stop", "reset"):
        if wall < current["start_desktop_ns"]:
            raise TaskMarkerError("timestamp is before the current task start")
        same_session = (context.get("clock_session_id") is not None and
                        context.get("clock_session_id") == current["start_context"].get("clock_session_id"))
        if same_session and monotonic < current["start_monotonic_ns"]:
            raise TaskMarkerError("monotonic timestamp is before the current task start")
    else:
        same_session = False
    if action == "start":
        index = len(state["segments"]) + 1
        state["segments"].append({
            "id": f"{context['key']}-task-{index:04d}", "index": index, "state": "open",
            "created_desktop_ns": wall, "created_monotonic_ns": monotonic,
            "created_request_id": event["request_id"],
            "start_desktop_ns": wall, "start_monotonic_ns": monotonic,
            "start_request_id": event["request_id"], "start_context": copy.deepcopy(context),
            "end_desktop_ns": None, "end_monotonic_ns": None, "end_request_id": None,
            "end_context": None, "duration_ns": None, "duration_source": None,
            "reset_count": 0, "reset_history": [], "quality": None, "quality_reasons": [],
        })
    elif action == "reset":
        current["reset_history"].append({
            "start_desktop_ns": current["start_desktop_ns"],
            "start_monotonic_ns": current["start_monotonic_ns"],
            "start_request_id": current["start_request_id"],
            "start_context": copy.deepcopy(current["start_context"]),
            "reset_desktop_ns": wall, "reset_monotonic_ns": monotonic,
            "reset_request_id": event["request_id"], "reset_context": copy.deepcopy(context),
        })
        current.update(start_desktop_ns=wall, start_monotonic_ns=monotonic,
                       start_request_id=event["request_id"], start_context=copy.deepcopy(context),
                       reset_count=current["reset_count"] + 1)
    else:
        wall_duration = wall - current["start_desktop_ns"]
        reasons = []
        if context.get("healthy") is not True or context.get("ready") is False:
            reasons.append(context.get("reason") or "no fresh capture health")
        if same_session:
            duration = monotonic - current["start_monotonic_ns"]
            source = "monotonic"
            if abs(wall_duration - duration) > _CLOCK_JUMP_NS:
                reasons.append("desktop_clock_changed")
        else:
            duration, source = wall_duration, "wall"
            reasons.append("clock_session_changed")
        current.update(state="closed", end_desktop_ns=wall, end_monotonic_ns=monotonic,
                       end_request_id=event["request_id"], end_context=copy.deepcopy(context),
                       duration_ns=duration, duration_source=source,
                       quality="review_required" if reasons else "completed", quality_reasons=reasons)
    state.update(context=copy.deepcopy(context), revision=event["revision"], last=copy.deepcopy(event))
    state["requests"][event["request_id"]] = {
        "fingerprint": _fingerprint(event), "revision": event["revision"], "action": action,
    }


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON field {key}")
        value[key] = item
    return value


class TaskMarkerStore:
    """Thread-safe append-only marker store, isolated by capture context key.

    ``apply`` raises ValueError for malformed requests and TaskMarkerError for
    stale revisions or refused transitions. An invalid journal is reported by
    ``snapshot()['error']`` and blocks all mutations; no record is silently
    discarded, including an unterminated final record after a interrupted write.
    """

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.base = self.root / "task_markers"
        self.clock_session_id = str(uuid.uuid4())
        with _LOCKS_GUARD:
            self.lock = _LOCKS.setdefault(self.root, threading.RLock())

    def _directory(self, key):
        _identifier(key, "context key")
        directory = self.base / key
        if self.base.resolve() != self.base or directory.resolve() != directory:
            raise TaskMarkerStorageError("task marker journal directory must not be a symlink or junction")
        if self.base.exists() and not self.base.is_dir():
            raise TaskMarkerStorageError("task marker journal base is not a directory")
        if directory.exists() and not directory.is_dir():
            raise TaskMarkerStorageError("task marker journal path is not a directory")
        for name in ("events.jsonl", "task_segments.json", "task_segments.csv"):
            path = directory / name
            if path.is_symlink() or path.resolve() != path:
                raise TaskMarkerStorageError("task marker journal or export path must not be a symlink")
        return directory

    def _load(self, directory, key):
        state = _new_state()
        journal = directory / "events.jsonl"
        if not journal.exists():
            return state
        line_number = 0
        try:
            with journal.open("rb") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.endswith(b"\n"):
                        raise ValueError("incomplete final journal record (missing newline)")
                    event = json.loads(line, object_pairs_hook=_unique_object)
                    if not isinstance(event, dict) or event.get("version") != _VERSION:
                        raise ValueError("invalid journal event version")
                    required = {"version", "revision", "action", "request_id", "expected_revision",
                                "wall_ns", "monotonic_ns", "client_wall_ms", "context"}
                    if set(event) != required:
                        raise ValueError("invalid journal event fields")
                    if event["action"] not in ("start", "stop", "reset"):
                        raise ValueError("invalid journal action")
                    _integer(event["revision"], "revision", minimum=1)
                    _integer(event["expected_revision"], "expected_revision")
                    if event["revision"] != state["revision"] + 1 or event["expected_revision"] != state["revision"]:
                        raise ValueError("nonconsecutive journal revision")
                    event["request_id"] = _request_id(event["request_id"])
                    if event["request_id"] in state["requests"]:
                        raise ValueError("duplicate journal request_id")
                    _integer(event["wall_ns"], "wall_ns")
                    _integer(event["monotonic_ns"], "monotonic_ns")
                    if event["client_wall_ms"] is not None:
                        _integer(event["client_wall_ms"], "client_wall_ms")
                    event["context"] = _context(event["context"])
                    if event["context"]["key"] != key:
                        raise ValueError("journal capture identity does not match its directory")
                    event["context"] = _merge_context(state["context"], event["context"])
                    _transition(state, event)
        except (OSError, ValueError, UnicodeError, TypeError, KeyError, TaskMarkerError) as exc:
            raise TaskMarkerStorageError(f"task marker journal error at record {line_number}: {exc}") from exc
        return state

    @staticmethod
    def _snapshot(state, context, directory):
        result = {"context": copy.deepcopy(context), "current": copy.deepcopy(_open_segment(state)),
                  "segments": copy.deepcopy(state["segments"]), "revision": state["revision"],
                  "local_directory": str(directory) if directory is not None else None}
        if state["last"]:
            result.update(last_request_id=state["last"]["request_id"], last_action=state["last"]["action"])
        return result

    @staticmethod
    def _sync_directory(directory):
        if os.name == "nt":
            return
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _append(self, directory, event):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self._directory(event["context"]["key"])
            data = (json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
            with (directory / "events.jsonl").open("ab") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            self._sync_directory(directory)
        except OSError as exc:
            raise TaskMarkerStorageError(f"task marker journal append failed: {exc}") from exc

    def _atomic_bytes(self, path, data):
        if path.exists() and path.read_bytes() == data:
            return
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._sync_directory(path.parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _confirm_journal(directory):
        # A previous write may have completed but failed at fsync. An
        # idempotent retry must establish durability before acknowledging it.
        try:
            # Windows _commit requires a writable descriptor; r+b opens the
            # existing journal without creating, appending or truncating it.
            with (directory / "events.jsonl").open("r+b") as handle:
                os.fsync(handle.fileno())
        except OSError as exc:
            raise TaskMarkerStorageError(f"task marker journal flush failed: {exc}") from exc

    def _export(self, directory, state):
        snapshot = self._snapshot(state, state["context"], directory)
        data = (json.dumps({"version": _VERSION, **snapshot}, ensure_ascii=False, allow_nan=False,
                           indent=2, sort_keys=True) + "\n").encode("utf-8")
        columns = ("id", "index", "state", "start_desktop_ns", "end_desktop_ns", "duration_ns",
                   "duration_source", "start_monotonic_ns", "end_monotonic_ns", "reset_count",
                   "quality", "quality_reasons", "start_context", "end_context", "reset_history")
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for segment in state["segments"]:
            row = {name: segment[name] for name in columns}
            for name in ("quality_reasons", "start_context", "end_context", "reset_history"):
                row[name] = json.dumps(row[name], ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            writer.writerow(row)
        for name, contents in (("task_segments.json", data), ("task_segments.csv", output.getvalue().encode("utf-8"))):
            path = directory / name
            if not path.exists() or path.read_bytes() != contents:
                self._atomic_bytes(path, contents)

    def _export_error(self, directory, state):
        try:
            self._export(directory, state)
        except OSError as exc:
            return f"task marker journal is durable; derived export failed and will be retried: {exc}"
        return None

    def snapshot(self, context=None):
        if context is None:
            return self._snapshot(_new_state(), None, None)
        incoming = _context(context)
        with self.lock:
            directory = self.base / incoming["key"]
            try:
                directory = self._directory(incoming["key"])
                state = self._load(directory, incoming["key"])
            except TaskMarkerStorageError as exc:
                return {**self._snapshot(_new_state(), incoming, directory),
                        "error": str(exc), "error_type": "journal_unavailable"}
            merged = _merge_context(state["context"], incoming)
            result = self._snapshot(state, merged, directory)
            if state["revision"]:
                error = self._export_error(directory, state)
                if error:
                    result.update(error=error, error_type="export_failed")
            return result

    def _check_other_captures(self, request_id, key):
        # UUIDs are globally distinct, including after a server restart or a
        # capture switch. Do not let an old network retry open a different task.
        if not self.base.exists():
            return
        for journal in self.base.glob("*/events.jsonl"):
            other_key = journal.parent.name
            if other_key == key:
                continue
            try:
                self._directory(other_key)
                with journal.open("rb") as handle:
                    for line in handle:
                        try:
                            event = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue
                        if isinstance(event, dict) and event.get("request_id") == request_id:
                            raise ValueError("request_id already belongs to a different capture identity")
            except (OSError, TaskMarkerStorageError):
                continue

    def apply(self, action, context, *, request_id, expected_revision, wall_ns,
              monotonic_ns, client_wall_ms=None):
        if action not in ("start", "stop", "reset"):
            raise ValueError("unsupported task marker action")
        incoming = _context(context)
        incoming.setdefault("clock_session_id", self.clock_session_id)
        request_id = _request_id(request_id)
        _integer(expected_revision, "expected_revision")
        _integer(wall_ns, "wall_ns")
        _integer(monotonic_ns, "monotonic_ns")
        if client_wall_ms is not None:
            _integer(client_wall_ms, "client_wall_ms")
        with self.lock:
            directory = self._directory(incoming["key"])
            state = self._load(directory, incoming["key"])
            merged = _merge_context(state["context"], incoming)
            event = {"version": _VERSION, "revision": state["revision"] + 1,
                     "action": action, "request_id": request_id, "expected_revision": expected_revision,
                     "wall_ns": wall_ns, "monotonic_ns": monotonic_ns,
                     "client_wall_ms": client_wall_ms, "context": merged}
            previous = state["requests"].get(request_id)
            if previous:
                if previous["fingerprint"] != _fingerprint(event):
                    raise ValueError("conflicting reuse of request_id")
                self._confirm_journal(directory)
                result = self._snapshot(state, merged, directory)
                result.update(last_request_id=request_id, last_action=previous["action"],
                              duplicate_request=True, request_revision=previous["revision"])
            else:
                self._check_other_captures(request_id, incoming["key"])
                if expected_revision != state["revision"]:
                    raise TaskMarkerError(f"stale task marker revision: expected {expected_revision}, current {state['revision']}")
                _transition(state, event)
                self._append(directory, event)
                result = self._snapshot(state, merged, directory)
                result.update(duplicate_request=False, request_revision=state["revision"])
            error = self._export_error(directory, state)
            if error:
                result.update(error=error, error_type="export_failed")
            return result
