"""Thin real-aircraft supervisor over official Prometheus command semantics.

Official provenance: ``uav_controller.cpp`` owns RC state priority, odometry
failsafe, ``XYZ_POS_BODY`` conversion, absolute-control HOLD, and Land.  This
module only gates access to those official commands and deliberately exposes
no setup, arming, or Kill operation.
"""

from __future__ import annotations

import math
import threading
from typing import Any

import numpy as np

from p450_stream.prosim_mapping import (
    build_prometheus_body_position_mapping,
    make_land,
    make_takeoff,
)
from p450_stream.ros_adapter import CommandIdSequence, PrometheusExecutor
from p450_stream.safety import ActionLimits, ActionRejected, validate_action_chunk


OFFICIAL_LAND_CYCLE_S = 0.1


class _BackendPublisher:
    def __init__(self, backend: Any) -> None:
        self._backend = backend

    def publish(self, message: Any) -> None:
        self._backend.publish_command(message)


class OfficialRealSupervisor:
    """Serialize bounded policy motion with official RC and failsafe states."""

    def __init__(
        self,
        backend: Any,
        *,
        command_ids: CommandIdSequence | None = None,
        policy_timeout_s: float = 0.5,
        controller_cycle_s: float = 0.1,
    ) -> None:
        policy_timeout = float(policy_timeout_s)
        controller_cycle = float(controller_cycle_s)
        if not math.isfinite(policy_timeout) or policy_timeout <= 0.0:
            raise ValueError("policy_timeout_s must be finite and positive")
        if not math.isfinite(controller_cycle) or controller_cycle <= 0.0:
            raise ValueError("controller_cycle_s must be finite and positive")
        self.backend = backend
        self.command_ids = command_ids or CommandIdSequence()
        self.policy_timeout_s = policy_timeout
        self.controller_cycle_s = controller_cycle
        self.mapping = build_prometheus_body_position_mapping(backend.command_type)
        self._command_lock = threading.RLock()
        self._action_limits = ActionLimits(
            max_step_xy_m=0.02,
            max_step_z_m=0.0,
            max_chunk_xy_m=0.15,
            max_chunk_z_m=0.0,
            min_altitude_m=0.05,
            max_altitude_m=0.30,
        )
        self._executor = PrometheusExecutor(
            self.mapping,
            publisher_factory=lambda _topic, _type, _queue: _BackendPublisher(
                backend
            ),
            sleep=backend.sleep,
            controller_cycle_s=self.controller_cycle_s,
            command_ids=self.command_ids,
            mode="real",
            limits=self._action_limits,
        )
        self.phase = "HOLD"
        self.last_policy_ns: int | None = None

    @property
    def action_limits(self) -> ActionLimits:
        """The immutable admission/execution contract shared with stream runners."""
        return self._action_limits

    def _official_priority(self, snapshot: Any) -> str | None:
        """Mirror the ordering in the official controller before local gates."""
        if snapshot.control_state == "LAND_CONTROL":
            self.phase = "LANDING"
            return "rc_land"
        if snapshot.control_state == "RC_POS_CONTROL":
            self.phase = "RC_TAKEOVER"
            return "rc_takeover"
        if not snapshot.odometry_valid:
            self.phase = "LOCALIZATION_FAULT"
            return "official_odom_failsafe"
        return None

    @staticmethod
    def _unsafe_takeoff_parameter(snapshot: Any) -> bool:
        value = float(snapshot.takeoff_height_m)
        return not math.isfinite(value) or abs(value - 0.25) > 1e-6

    @staticmethod
    def _require_ready(snapshot: Any, *, now_ns: int) -> None:
        failures = snapshot.ready_failures(now_ns=now_ns)
        if failures:
            raise RuntimeError(",".join(failures))

    def authorize(self, now_ns: int) -> str | None:
        with self._command_lock:
            snapshot = self.backend.snapshot()
            priority = self._official_priority(snapshot)
            if priority is not None:
                return priority
            if self.phase not in {"HOLD", "RC_TAKEOVER"}:
                raise RuntimeError("authorization_not_allowed")
            self._require_ready(snapshot, now_ns=now_ns)
            if self._unsafe_takeoff_parameter(snapshot):
                raise RuntimeError("unsafe_takeoff_height")
            epoch = self._executor.hold_epoch
            self._executor.recover(
                local_authorized=True,
                ready=True,
                expected_hold_epoch=epoch,
            )
            self.phase = "AUTHORIZED"

    def takeoff(self, now_ns: int) -> str | None:
        with self._command_lock:
            snapshot = self.backend.snapshot()
            priority = self._official_priority(snapshot)
            if priority is not None:
                return priority
            if self.phase != "AUTHORIZED":
                raise RuntimeError("not_authorized")
            self._require_ready(snapshot, now_ns=now_ns)
            if self._unsafe_takeoff_parameter(snapshot):
                raise RuntimeError("unsafe_takeoff_height")
            self.backend.publish_command(
                make_takeoff(self.backend.command_type, self.command_ids.next())
            )
            self.phase = "TAKEOFF"

    def refresh(self, now_ns: int) -> str:
        with self._command_lock:
            snapshot = self.backend.snapshot()
            priority = self._official_priority(snapshot)
            if priority is not None:
                return priority

            failures = snapshot.ready_failures(now_ns=now_ns)
            if failures:
                if self.phase == "ACTIVE":
                    self._executor.enter_hold(f"readiness:{','.join(failures)}")
                    self.phase = "HOLD"
                return failures[0]

            if self.phase == "TAKEOFF":
                settled = (
                    0.20 <= float(snapshot.position[2]) <= 0.30
                    and abs(float(snapshot.velocity[2])) < 0.05
                )
                if not settled:
                    return "takeoff_not_settled"
                self.phase = "ACTIVE"
                self.last_policy_ns = int(now_ns)
                return "active"

            return self.phase.lower()

    def execute_action(
        self,
        action: Any,
        request_seq: int,
        action_step: int,
        now_ns: int,
    ) -> str:
        with self._command_lock:
            snapshot = self.backend.snapshot()
            priority = self._official_priority(snapshot)
            if priority is not None:
                return priority
            if self.phase != "ACTIVE":
                raise RuntimeError("not_active")

            failures = snapshot.ready_failures(now_ns=now_ns)
            if failures:
                self._executor.enter_hold(f"readiness:{','.join(failures)}")
                self.phase = "HOLD"
                raise RuntimeError(",".join(failures))

            try:
                raw = np.asarray(action)
                checked = validate_action_chunk(
                    raw[np.newaxis, ...],
                    mode="real",
                    limits=self._action_limits,
                    current_altitude_m=snapshot.position[2],
                )[0]
            except ActionRejected as error:
                self._executor.enter_hold(f"action_rejected:{error.reason}")
                self.phase = "HOLD"
                raise
            except (TypeError, ValueError) as error:
                self._executor.enter_hold("action_rejected:invalid_action")
                self.phase = "HOLD"
                raise ActionRejected("invalid_action") from error

            try:
                self._executor.execute(
                    checked,
                    request_seq=request_seq,
                    action_step=action_step,
                    current_altitude_m=snapshot.position[2],
                )
            except ActionRejected:
                if not self._executor.held:
                    self._executor.enter_hold("action_rejected")
                self.phase = "HOLD"
                raise
            except (TypeError, ValueError) as error:
                if not self._executor.held:
                    self._executor.enter_hold("action_rejected:invalid_action")
                self.phase = "HOLD"
                raise ActionRejected("invalid_action") from error
            self.last_policy_ns = int(now_ns)
            return "executed"

    def watchdog(self, now_ns: int) -> str:
        with self._command_lock:
            status = self.refresh(now_ns)
            if self.phase != "ACTIVE":
                return status
            if self.last_policy_ns is None:
                raise RuntimeError("policy_clock_not_started")
            elapsed_ns = int(now_ns) - self.last_policy_ns
            if elapsed_ns > int(self.policy_timeout_s * 1_000_000_000):
                self._executor.enter_hold("policy_timeout")
                self.phase = "HOLD"
                return "policy_timeout"
            return "active"

    def hold(self, reason: str) -> str | None:
        with self._command_lock:
            snapshot = self.backend.snapshot()
            priority = self._official_priority(snapshot)
            if priority is not None:
                return priority
            if self.phase in {
                "LANDING",
                "RC_TAKEOVER",
                "LOCALIZATION_FAULT",
            }:
                return None
            if self.phase != "HOLD":
                self._executor.enter_hold(reason)
                self.phase = "HOLD"

    def land(self, now_ns: int) -> str | None:
        del now_ns
        with self._command_lock:
            if self.phase == "LANDING":
                return None
            snapshot = self.backend.snapshot()
            if snapshot.control_state == "LAND_CONTROL":
                self.phase = "LANDING"
                return "rc_land"
            if not snapshot.odometry_valid:
                self.phase = "LOCALIZATION_FAULT"
                return "official_odom_failsafe"

            self.backend.publish_command(
                self.mapping.exit_hold_message(self.command_ids.next())
            )
            self.backend.sleep(OFFICIAL_LAND_CYCLE_S)
            self.backend.publish_command(
                make_land(self.backend.command_type, self.command_ids.next())
            )
            # Commit to LANDING only after both official commands went out:
            # a failed publish (e.g. command ownership never acquired) must
            # leave the phase untouched so a retry cannot silently succeed.
            self.phase = "LANDING"
