"""ROS boundary for the official-first P450 command supervisor.

The installed Prometheus controller remains authoritative for command
semantics, RC priority, and failsafes.  This module only adapts its official
state messages and local Trigger services to :class:`OfficialRealSupervisor`.
It is deliberately importable without a ROS installation.
"""

from __future__ import annotations

import argparse
import json
import math
import threading
import time
from typing import Any, Callable, Sequence

from p450_stream.prosim_ros import VehicleSnapshot
from p450_stream.real_official import OfficialRealSupervisor


COMMAND_TOPIC = "/uav1/prometheus/command"
STATE_TOPIC = "/uav1/prometheus/state"
CONTROL_TOPIC = "/uav1/prometheus/control_state"
SERVICE_NAMES = (
    "/p450_real_guard/authorize",
    "/p450_real_guard/takeoff",
    "/p450_real_guard/hold",
    "/p450_real_guard/land",
    "/p450_real_guard/status",
)


class _RosCommandBackend:
    """Translate official ROS state messages into the shared snapshot model."""

    def __init__(self, ros: Any, *, command_output_enabled: bool) -> None:
        self.ros = ros
        self.command_type = ros.command_type
        self.control_type = ros.control_type
        # Official provenance: prometheus_msgs/UAVState.msg defines MID360, and
        # p450_onboard_mid360_safe.launch includes the official mid360 chain
        # (p450_onboard_mid360.launch, msg_MID360.launch, mapping_mid360.launch,
        # switch_location_source_mid360_d435i.launch). The deployed airframe
        # reports location_source=10 (MID360) live since 2026-08-25.
        # Read the generated message constant so GPS or a changed message ABI
        # cannot silently satisfy readiness through a local magic number.
        mid360_source = getattr(ros.state_type, "MID360", None)
        if (
            isinstance(mid360_source, bool)
            or not isinstance(mid360_source, int)
            or mid360_source < 0
        ):
            raise ValueError("invalid_official_mid360_location_source")
        self.expected_location_source = int(mid360_source)
        expected_control_constants = {
            "INIT": 0,
            "RC_POS_CONTROL": 1,
            "COMMAND_CONTROL": 2,
            "LAND_CONTROL": 3,
            "PX4_ORIGIN": 0,
            "PID": 1,
            "UDE": 2,
            "NE": 3,
        }
        control_constants: dict[str, int] = {}
        for name, expected in expected_control_constants.items():
            value = getattr(self.control_type, name, None)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value != expected
            ):
                raise ValueError("invalid_official_control_constants")
            control_constants[name] = int(value)
        if len(
            {
                control_constants[name]
                for name in (
                    "INIT",
                    "RC_POS_CONTROL",
                    "COMMAND_CONTROL",
                    "LAND_CONTROL",
                )
            }
        ) != 4 or len(
            {
                control_constants[name]
                for name in ("PX4_ORIGIN", "PID", "UDE", "NE")
            }
        ) != 4:
            raise ValueError("invalid_official_control_constants")
        self._control_states = {
            control_constants["INIT"]: "INIT",
            control_constants["RC_POS_CONTROL"]: "RC_POS_CONTROL",
            control_constants["COMMAND_CONTROL"]: "COMMAND_CONTROL",
            control_constants["LAND_CONTROL"]: "LAND_CONTROL",
        }
        self._controllers = {
            control_constants["PX4_ORIGIN"]: "PX4_ORIGIN",
            control_constants["PID"]: "PID",
            control_constants["UDE"]: "UDE",
            control_constants["NE"]: "NE",
        }
        self._command_output_enabled = command_output_enabled is True
        self._lock = threading.Lock()
        self._state = None
        self._control = None
        self._state_received_ns = 0
        self._control_received_ns = 0
        self._ownership_lock = threading.Lock()
        self._command_publisher = None
        self.state_subscriber = ros.Subscriber(
            STATE_TOPIC, ros.state_type, self._state_callback, queue_size=10
        )
        self.control_subscriber = ros.Subscriber(
            CONTROL_TOPIC,
            ros.control_type,
            self._control_callback,
            queue_size=10,
        )

    def now_ns(self) -> int:
        return int(self.ros.now_ns())

    def sleep(self, seconds: float) -> None:
        self.ros.sleep(seconds)

    def publish_command(self, message: Any) -> None:
        if self._command_publisher is None:
            raise RuntimeError("command_ownership_not_acquired")
        self.ros.stamp(message)
        self._command_publisher.publish(message)

    def acquire_command_ownership(self) -> None:
        """Create the sole output only after a publisher-free master check."""
        if not self._command_output_enabled:
            raise RuntimeError("command_output_disabled")
        with self._ownership_lock:
            if self._command_publisher is not None:
                return
            if self._unexpected_command_publishers() != 0:
                raise RuntimeError("unexpected_command_publishers")
            self._command_publisher = self.ros.Publisher(
                COMMAND_TOPIC, self.command_type, queue_size=10
            )

    def _state_callback(self, message: Any) -> None:
        with self._lock:
            self._state = message
            self._state_received_ns = self.now_ns()

    def _control_callback(self, message: Any) -> None:
        with self._lock:
            self._control = message
            self._control_received_ns = self.now_ns()

    @staticmethod
    def _name(value: Any, choices: dict[int, str]) -> str:
        try:
            return choices.get(int(value), "UNKNOWN")
        except (TypeError, ValueError):
            return "UNKNOWN"

    def _unexpected_command_publishers(self) -> int:
        try:
            value = int(self.ros.count_unexpected_publishers(COMMAND_TOPIC))
        except Exception:
            return 1
        return value if value >= 0 else 1

    def snapshot(self) -> VehicleSnapshot:
        with self._lock:
            state = self._state
            control = self._control
            state_received_ns = self._state_received_ns
            control_received_ns = self._control_received_ns

        def vector(
            message: Any, name: str
        ) -> tuple[tuple[float, float, float], bool]:
            if message is None:
                return (0.0, 0.0, 0.0), False
            try:
                raw = getattr(message, name)
                values = tuple(float(value) for value in raw)
            except Exception:
                return (0.0, 0.0, 0.0), False
            valid = len(values) == 3 and all(math.isfinite(value) for value in values)
            if not valid:
                return (0.0, 0.0, 0.0), False
            return (values[0], values[1], values[2]), True

        position, position_valid = vector(state, "position")
        velocity, velocity_valid = vector(state, "velocity")
        attitude, attitude_valid = vector(state, "attitude")
        attitude_rate, attitude_rate_valid = vector(state, "attitude_rate")
        all_vectors_valid = (
            position_valid
            and velocity_valid
            and attitude_valid
            and attitude_rate_valid
        )
        try:
            takeoff_height_m = float(
                self.ros.get_param(
                    "/uav_control_main_1/control/Takeoff_height", 1.5
                )
            )
        except Exception:
            takeoff_height_m = float("nan")
        if not math.isfinite(takeoff_height_m):
            takeoff_height_m = float("nan")
        return VehicleSnapshot(
            connected=bool(getattr(state, "connected", False)),
            armed=bool(getattr(state, "armed", False)),
            flight_mode=str(getattr(state, "mode", "")),
            odometry_valid=(
                bool(getattr(state, "odom_valid", False))
                and all_vectors_valid
            ),
            control_state=self._name(
                getattr(control, "control_state", -1), self._control_states
            ),
            controller=self._name(
                getattr(control, "pos_controller", -1), self._controllers
            ),
            failsafe=bool(getattr(control, "failsafe", True)),
            location_source=int(getattr(state, "location_source", -1)),
            expected_location_source=self.expected_location_source,
            state_received_monotonic_ns=state_received_ns,
            control_received_monotonic_ns=control_received_ns,
            unexpected_command_publishers=self._unexpected_command_publishers(),
            position=position,
            velocity=velocity,
            attitude=attitude,
            attitude_rate=attitude_rate,
            takeoff_height_m=takeoff_height_m,
        )


class OfficialRealNode:
    """Expose local operations while defaulting to a read-only ROS boundary."""

    def __init__(self, *, ros: Any, command_output_enabled: bool = False) -> None:
        self.ros = ros
        self.command_output_enabled = command_output_enabled is True
        self.backend = _RosCommandBackend(
            ros, command_output_enabled=self.command_output_enabled
        )
        self.supervisor = OfficialRealSupervisor(self.backend)
        self._node_lock = threading.RLock()
        self.last_fault: str | None = None
        handlers = (
            self.handle_authorize,
            self.handle_takeoff,
            self.handle_hold,
            self.handle_land,
            self.handle_status,
        )
        self.services = [
            ros.Service(name, ros.trigger_type, handler)
            for name, handler in zip(SERVICE_NAMES, handlers)
        ]

    def _response(self, success: bool, reason: str) -> Any:
        message = f"phase={self.supervisor.phase} reason={reason}"
        return self.ros.trigger_response_type(bool(success), message)

    def _disabled_response(self) -> Any | None:
        if self.command_output_enabled:
            return None
        self.last_fault = "command_output_disabled"
        return self._response(False, self.last_fault)

    def _operate(
        self,
        operation: Callable[[], str | None],
        *,
        success_reason: str,
    ) -> Any:
        with self._node_lock:
            disabled = self._disabled_response()
            if disabled is not None:
                return disabled
            try:
                rejection = operation()
                if rejection is not None:
                    self.last_fault = str(rejection)
                    return self._response(False, self.last_fault)
            except Exception as error:
                self.last_fault = str(error).strip() or type(error).__name__
                return self._response(False, self.last_fault)
            self.last_fault = None
            return self._response(True, success_reason)

    def handle_authorize(self, _request: Any) -> Any:
        def authorize() -> str | None:
            self.backend.acquire_command_ownership()
            return self.supervisor.authorize(self.backend.now_ns())

        return self._operate(
            authorize,
            success_reason="authorized",
        )

    def handle_takeoff(self, _request: Any) -> Any:
        return self._operate(
            lambda: self.supervisor.takeoff(self.backend.now_ns()),
            success_reason="takeoff_started",
        )

    def handle_hold(self, _request: Any) -> Any:
        return self._operate(
            lambda: self.supervisor.hold("desktop_hold"),
            success_reason="hold_latched",
        )

    def handle_land(self, _request: Any) -> Any:
        return self._operate(
            lambda: self.supervisor.land(self.backend.now_ns()),
            success_reason="landing_started",
        )

    def handle_status(self, _request: Any) -> Any:
        with self._node_lock:
            try:
                snapshot = self.backend.snapshot()
                failures = list(
                    snapshot.ready_failures(now_ns=self.backend.now_ns())
                )
                payload = {
                    "command_output_enabled": self.command_output_enabled,
                    "fault": self.last_fault,
                    "phase": self.supervisor.phase,
                    "readiness": failures,
                    "unexpected_command_publishers": (
                        snapshot.unexpected_command_publishers
                    ),
                }
                message = json.dumps(
                    payload, sort_keys=True, separators=(",", ":")
                )
                return self.ros.trigger_response_type(True, message)
            except Exception as error:
                self.last_fault = str(error).strip() or type(error).__name__
                payload = {
                    "command_output_enabled": self.command_output_enabled,
                    "fault": self.last_fault,
                    "phase": self.supervisor.phase,
                    "readiness": ["status_unavailable"],
                    "unexpected_command_publishers": None,
                }
                message = json.dumps(
                    payload, sort_keys=True, separators=(",", ":")
                )
                return self.ros.trigger_response_type(False, message)

    def tick(self) -> str:
        """Refresh official priority and watchdog state without creating motion."""
        with self._node_lock:
            if not self.command_output_enabled:
                return "read_only"
            try:
                result = self.supervisor.watchdog(self.backend.now_ns())
            except Exception as error:
                self.last_fault = str(error).strip() or type(error).__name__
                return self.last_fault
            return result

    def shutdown(self) -> None:
        """Leave the official controller/RC path untouched during process exit."""
        with self._node_lock:
            return None


class _LiveRosFacade:
    """Small adapter around lazily imported live ROS modules and message types."""

    def __init__(
        self,
        *,
        rospy_module: Any,
        rosgraph_module: Any,
        command_type: type,
        state_type: type,
        control_type: type,
        trigger_type: type,
        trigger_response_type: type,
    ) -> None:
        self._rospy = rospy_module
        self._rosgraph = rosgraph_module
        self.command_type = command_type
        self.state_type = state_type
        self.control_type = control_type
        self.trigger_type = trigger_type
        self.trigger_response_type = trigger_response_type
        self._timers: list[Any] = []

    def Publisher(self, *args: Any, **kwargs: Any) -> Any:
        return self._rospy.Publisher(*args, **kwargs)

    def Subscriber(self, *args: Any, **kwargs: Any) -> Any:
        return self._rospy.Subscriber(*args, **kwargs)

    def Service(self, *args: Any, **kwargs: Any) -> Any:
        return self._rospy.Service(*args, **kwargs)

    def schedule_periodic(
        self, period_s: float, callback: Callable[[], Any]
    ) -> Any:
        def guarded_callback(_event: Any) -> None:
            try:
                callback()
            except Exception:
                return None

        timer = self._rospy.Timer(
            self._rospy.Duration.from_sec(float(period_s)), guarded_callback
        )
        self._timers.append(timer)
        return timer

    def on_shutdown(self, callback: Callable[[], Any]) -> None:
        self._rospy.on_shutdown(callback)

    def shutdown(self, reason: str) -> None:
        # Stops rospy's non-daemon threads (timers, tcpros, xmlrpc); without
        # this the process hangs in threading._shutdown after main returns.
        self._rospy.signal_shutdown(reason)

    def spin(self) -> None:
        self._rospy.spin()

    def now_ns(self) -> int:
        return time.monotonic_ns()

    def sleep(self, seconds: float) -> None:
        self._rospy.sleep(seconds)

    def stamp(self, message: Any) -> None:
        if hasattr(message, "header"):
            message.header.stamp = self._rospy.Time.now()

    def get_param(self, name: str, default: Any) -> Any:
        return self._rospy.get_param(name, default)

    def count_unexpected_publishers(self, topic: str) -> int:
        publishers, _, _ = self._rosgraph.Master(
            self._rospy.get_name()
        ).getSystemState()
        nodes = []
        for published_topic, topic_nodes in publishers:
            if published_topic == topic:
                nodes = list(topic_nodes)
                break
        own_name = self._rospy.get_name()
        return len([node for node in nodes if node != own_name])


def _create_live_facade() -> _LiveRosFacade:
    import rosgraph  # type: ignore[import-not-found]
    import rospy  # type: ignore[import-not-found]
    from prometheus_msgs.msg import (  # type: ignore[import-not-found]
        UAVCommand,
        UAVControlState,
        UAVState,
    )
    from std_srvs.srv import Trigger, TriggerResponse  # type: ignore[import-not-found]

    if not rospy.core.is_initialized():
        rospy.init_node("p450_real_guard", anonymous=False, disable_signals=True)
    return _LiveRosFacade(
        rospy_module=rospy,
        rosgraph_module=rosgraph,
        command_type=UAVCommand,
        state_type=UAVState,
        control_type=UAVControlState,
        trigger_type=Trigger,
        trigger_response_type=TriggerResponse,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Official-first P450 Pi05 ROS boundary"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-check", action="store_true")
    mode.add_argument("--enable-command-output", action="store_true")
    args = parser.parse_args(argv)

    if not args.enable_command_output:
        report = {
            "command_output_enabled": False,
            "mode": "dry-check",
            "ros_initialized": False,
        }
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 0

    ros = _create_live_facade()
    node = OfficialRealNode(ros=ros, command_output_enabled=True)
    ros.schedule_periodic(0.1, node.tick)
    ros.on_shutdown(node.shutdown)
    ros.spin()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
