"""Executor boundary between validated actions and Prometheus ROS publishing.

The generic bridge intentionally contains no guessed ``UAVCommand`` field
mapping.  A mapping must be verified on the target P450 and injected before a
``PrometheusExecutor`` can import ROS or create a publisher.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import threading
import time
from typing import Any, Callable

import numpy as np

from p450_stream.safety import ActionLimits, ActionRejected, validate_action_chunk


def _one_action(action: np.ndarray) -> np.ndarray:
    array = np.asarray(action)
    if array.shape != (4,):
        raise ValueError(f"action must have shape (4,), got {array.shape}")
    if not np.issubdtype(array.dtype, np.number):
        raise ValueError("action must be numeric")
    array = array.astype(np.float32, copy=True)
    if not np.isfinite(array).all():
        raise ValueError("action must contain only finite values")
    return array


class DryRunExecutor:
    """Record actions without importing ROS or changing vehicle state."""

    def __init__(self, log_path: str | Path):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def execute(
        self, action: np.ndarray, *, request_seq: int, action_step: int
    ) -> dict[str, Any]:
        checked = _one_action(action)
        record = {
            "time_ns": time.time_ns(),
            "mode": "dry_run",
            "request_seq": int(request_seq),
            "action_step": int(action_step),
            "action": [float(value) for value in checked],
        }
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")
        return record


class CommandIdSequence:
    """Thread-safe nonzero monotonically increasing Prometheus command IDs."""

    def __init__(self, start: int = 1) -> None:
        if start <= 0:
            raise ValueError("command ID must start above zero")
        self._next = int(start)
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            value = self._next
            self._next += 1
            return value


@dataclass(frozen=True)
class PrometheusMapping:
    """Target-verified conversion from one body-frame delta to a ROS message."""

    topic: str
    message_type: type
    move_message: Callable[[np.ndarray, int], Any]
    hold_message: Callable[[int], Any]
    exit_hold_message: Callable[[int], Any]
    verified: bool = False


class PrometheusExecutor:
    """Publish through ROS only after an explicit target-specific mapping."""

    def __init__(
        self,
        mapping: PrometheusMapping | None = None,
        *,
        publisher_factory: Callable[[str, type, int], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        controller_cycle_s: float = 0.1,
        command_ids: CommandIdSequence | None = None,
        mode: str = "real",
        limits: ActionLimits | None = None,
    ) -> None:
        if controller_cycle_s <= 0:
            raise ValueError("controller_cycle_s must be positive")
        self.mapping = mapping
        self._publisher: Any | None = None
        self._publisher_factory = publisher_factory
        self._sleep = sleep
        self._controller_cycle_s = float(controller_cycle_s)
        self._command_ids = command_ids or CommandIdSequence()
        self._mode = mode
        self._limits = limits
        self._transition_lock = threading.RLock()
        self._hold_epoch = 0
        self._recovering = False
        self.held = True
        self.hold_reason = "startup"

    def _require_mapping(self) -> PrometheusMapping:
        mapping = self.mapping
        if (
            mapping is None
            or not mapping.verified
            or not mapping.topic
            or not callable(mapping.move_message)
            or not callable(mapping.hold_message)
            or not callable(mapping.exit_hold_message)
        ):
            raise RuntimeError(
                "a verified Prometheus mapping is required before ROS publishing"
            )
        return mapping

    def _get_publisher(self, mapping: PrometheusMapping) -> Any:
        if self._publisher is None:
            factory = self._publisher_factory
            if factory is None:
                import rospy  # type: ignore[import-not-found]  # Lazy by design.

                factory = rospy.Publisher
            self._publisher = factory(mapping.topic, mapping.message_type, 1)
        return self._publisher

    def _publish(self, message: Any, mapping: PrometheusMapping) -> Any:
        self._get_publisher(mapping).publish(message)
        return message

    @property
    def hold_epoch(self) -> int:
        """Token callers snapshot before evaluating READY for recovery."""
        with self._transition_lock:
            return self._hold_epoch

    def _latch_hold_unlocked(self, reason: str, mapping: PrometheusMapping) -> Any:
        self._hold_epoch += 1
        self.held = True
        self.hold_reason = str(reason)
        message = mapping.hold_message(self._command_ids.next())
        return self._publish(message, mapping)

    def enter_hold(self, reason: str) -> Any:
        with self._transition_lock:
            mapping = self._require_mapping()
            return self._latch_hold_unlocked(reason, mapping)

    def recover(
        self,
        *,
        local_authorized: bool,
        ready: bool,
        expected_hold_epoch: int,
    ) -> tuple[Any, Any]:
        if not local_authorized:
            raise RuntimeError("local_authorization_required")
        if not ready:
            raise RuntimeError("vehicle_not_ready")
        with self._transition_lock:
            if expected_hold_epoch != self._hold_epoch:
                raise RuntimeError("recovery_superseded")
            if self._recovering:
                raise RuntimeError("recovery_in_progress")
            mapping = self._require_mapping()
            self._recovering = True
            recovery_epoch = self._hold_epoch
            try:
                exit_message = mapping.exit_hold_message(self._command_ids.next())
                prime_message = mapping.move_message(
                    np.zeros(4, dtype=np.float32), self._command_ids.next()
                )
                self._publish(exit_message, mapping)
                self._publish(prime_message, mapping)
            except BaseException:
                self._recovering = False
                self._latch_hold_unlocked("recovery_publish_failed", mapping)
                raise
        try:
            self._sleep(self._controller_cycle_s)
        except BaseException:
            with self._transition_lock:
                self._recovering = False
            raise
        with self._transition_lock:
            self._recovering = False
            if recovery_epoch != self._hold_epoch:
                raise RuntimeError("recovery_superseded")
            self.held = False
            self.hold_reason = None
            return exit_message, prime_message

    def execute(
        self,
        action: np.ndarray,
        *,
        request_seq: int,
        action_step: int,
        current_altitude_m: float | None = None,
    ) -> Any:
        del request_seq, action_step
        with self._transition_lock:
            if self.held:
                raise RuntimeError("hold_latched")
            mapping = self._require_mapping()
            checked = _one_action(action)
            try:
                checked = validate_action_chunk(
                    checked.reshape(1, 4),
                    mode=self._mode,
                    limits=self._limits,
                    current_altitude_m=current_altitude_m,
                )[0]
            except ActionRejected as error:
                self._latch_hold_unlocked(
                    f"action_rejected:{error.reason}", mapping
                )
                raise
            message = mapping.move_message(checked.copy(), self._command_ids.next())
            return self._publish(message, mapping)
