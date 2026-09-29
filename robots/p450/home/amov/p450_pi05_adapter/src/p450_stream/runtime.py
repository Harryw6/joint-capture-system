from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DeadlineDecision:
    due: bool
    execute: bool
    step: int
    deadline_ns: int
    lateness_ns: int
    hold_reason: str | None = None


class MonotonicStepClock:
    """Absolute-deadline executor clock that advances at most one step per tick."""

    def __init__(
        self, *, period_ns: int = 100_000_000, max_lateness_ns: int = 50_000_000
    ) -> None:
        if period_ns <= 0 or max_lateness_ns < 0:
            raise ValueError("invalid deadline configuration")
        self.period_ns = int(period_ns)
        self.max_lateness_ns = int(max_lateness_ns)
        self.next_step: int | None = None
        self.next_deadline_ns: int | None = None

    def reset(self, *, start_ns: int, start_step: int = 0) -> None:
        if start_ns < 0 or start_step < 0:
            raise ValueError("start values must be nonnegative")
        self.next_step = int(start_step)
        self.next_deadline_ns = int(start_ns) + self.period_ns

    def check(self, *, now_ns: int) -> DeadlineDecision:
        if self.next_step is None or self.next_deadline_ns is None:
            raise RuntimeError("clock_not_started")
        step = self.next_step
        deadline = self.next_deadline_ns
        lateness = int(now_ns) - deadline
        if lateness < 0:
            return DeadlineDecision(False, False, step, deadline, lateness)

        self.next_step += 1
        self.next_deadline_ns += self.period_ns
        if lateness > self.max_lateness_ns:
            return DeadlineDecision(
                True, False, step, deadline, lateness, "executor_late"
            )
        return DeadlineDecision(True, True, step, deadline, lateness)


@dataclass(frozen=True)
class VehicleReadiness:
    connected: bool
    armed: bool
    flight_mode: str
    odometry_valid: bool
    control_state: str
    controller: str
    failsafe: bool
    location_source: int
    expected_location_source: int
    state_received_monotonic_ns: int
    control_received_monotonic_ns: int
    unexpected_command_publishers: int


def readiness_failures(
    status: VehicleReadiness,
    *,
    now_ns: int,
    max_state_age_ns: int = 100_000_000,
    max_control_age_ns: int = 200_000_000,
) -> tuple[str, ...]:
    failures: list[str] = []
    if not status.connected:
        failures.append("not_connected")
    if not status.armed:
        failures.append("not_armed")
    if status.flight_mode != "OFFBOARD":
        failures.append("flight_mode")
    if not status.odometry_valid:
        failures.append("odometry_invalid")
    if status.control_state != "COMMAND_CONTROL":
        failures.append("control_state")
    if status.controller != "PX4_ORIGIN":
        failures.append("controller")
    if status.failsafe:
        failures.append("failsafe")
    if status.location_source != status.expected_location_source:
        failures.append("location_source")
    state_age = int(now_ns) - status.state_received_monotonic_ns
    control_age = int(now_ns) - status.control_received_monotonic_ns
    if state_age < 0 or state_age > max_state_age_ns:
        failures.append("state_stale")
    if control_age < 0 or control_age > max_control_age_ns:
        failures.append("control_stale")
    if status.unexpected_command_publishers != 0:
        failures.append("unexpected_command_publishers")
    return tuple(failures)
