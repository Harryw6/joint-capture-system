"""ROS state and lifecycle boundary for the installed ProSim environment."""

from __future__ import annotations

from dataclasses import dataclass
import math
import threading
import time
from typing import Any, Callable, Tuple

import numpy as np

from p450_stream.prosim_mapping import (
    build_prometheus_mapping,
    make_arm_setup,
    make_command_control_setup,
    make_land,
    make_takeoff,
)
from p450_stream.ros_adapter import CommandIdSequence
from p450_stream.runtime import VehicleReadiness, readiness_failures


class ProSimLifecycleError(RuntimeError):
    pass


SUPPORTED_IMAGE_ENCODINGS = ("rgb8", "bgr8")


@dataclass(frozen=True)
class CameraFrame:
    """One decoded AirSim camera frame with its source and receipt stamps."""

    image: np.ndarray
    encoding: str
    source_stamp_ns: int
    received_monotonic_ns: int


def header_stamp_ns(message: Any) -> int:
    """Return a ROS message header stamp in epoch nanoseconds, or zero."""
    header = getattr(message, "header", None)
    stamp = getattr(header, "stamp", None)
    if stamp is None:
        return 0
    to_nsec = getattr(stamp, "to_nsec", None)
    if callable(to_nsec):
        try:
            return int(to_nsec())
        except Exception:
            return 0
    seconds = getattr(stamp, "sec", None)
    nanoseconds = getattr(stamp, "nsec", None)
    if isinstance(seconds, int) and isinstance(nanoseconds, int):
        return int(seconds) * 1_000_000_000 + int(nanoseconds)
    return 0


def parse_image_message(message: Any, received_monotonic_ns: int) -> CameraFrame | None:
    """Decode a sensor_msgs/Image from the installed airsim_ros_pkgs node.

    The vendor wrapper publishes ``bgr8`` (or ``rgb8`` on a Vulkan renderer)
    with the AirSim capture time in ``header.stamp``.  Anything else, or a
    message whose payload does not match its declared geometry, is ignored so
    a malformed publisher can never reach the policy observation path.
    """
    encoding = str(getattr(message, "encoding", ""))
    if encoding not in SUPPORTED_IMAGE_ENCODINGS:
        return None
    height = int(getattr(message, "height", 0))
    width = int(getattr(message, "width", 0))
    if height <= 0 or width <= 0:
        return None
    data = getattr(message, "data", b"")
    try:
        payload = bytes(data)
    except (TypeError, ValueError):
        return None
    if len(payload) != height * width * 3:
        return None
    image = np.frombuffer(payload, dtype=np.uint8).reshape(height, width, 3).copy()
    return CameraFrame(
        image=image,
        encoding=encoding,
        source_stamp_ns=header_stamp_ns(message),
        received_monotonic_ns=int(received_monotonic_ns),
    )


@dataclass(frozen=True)
class ProSimIdentity:
    node_name: str
    command_topic: str
    state_topic: str
    control_topic: str
    expected_location_source: int


@dataclass(frozen=True)
class VehicleSnapshot:
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
    position: Tuple[float, float, float]
    velocity: Tuple[float, float, float]
    attitude: Tuple[float, float, float]
    attitude_rate: Tuple[float, float, float]
    takeoff_height_m: float
    state_source_stamp_ns: int = 0

    def ready_failures(self, *, now_ns: int) -> Tuple[str, ...]:
        status = VehicleReadiness(
            connected=self.connected,
            armed=self.armed,
            flight_mode=self.flight_mode,
            odometry_valid=self.odometry_valid,
            control_state=self.control_state,
            controller=self.controller,
            failsafe=self.failsafe,
            location_source=self.location_source,
            expected_location_source=self.expected_location_source,
            state_received_monotonic_ns=self.state_received_monotonic_ns,
            control_received_monotonic_ns=self.control_received_monotonic_ns,
            unexpected_command_publishers=self.unexpected_command_publishers,
        )
        return readiness_failures(status, now_ns=now_ns)

    def is_ready(self, *, now_ns: int) -> bool:
        return not self.ready_failures(now_ns=now_ns)

    @property
    def horizontal_speed_mps(self) -> float:
        return math.hypot(self.velocity[0], self.velocity[1])

    @property
    def yaw_rate_rad_s(self) -> float:
        return float(self.attitude_rate[2])


class SimulationLifecycleSupervisor:
    """Own arm, command-control, takeoff, HOLD, landing, and disarm."""

    def __init__(
        self,
        backend: Any,
        *,
        command_ids: CommandIdSequence | None = None,
        poll_s: float = 0.05,
    ) -> None:
        if poll_s <= 0.0:
            raise ValueError("poll_s must be positive")
        self.backend = backend
        self.command_ids = command_ids or CommandIdSequence()
        self.poll_s = float(poll_s)
        self.mapping = build_prometheus_mapping(backend.command_type)

    def _wait(
        self,
        predicate: Callable[[VehicleSnapshot], bool],
        *,
        timeout_s: float,
        reason: str,
    ) -> VehicleSnapshot:
        deadline = self.backend.now_ns() + int(timeout_s * 1_000_000_000)
        last = self.backend.snapshot()
        while self.backend.now_ns() <= deadline:
            last = self.backend.snapshot()
            if predicate(last):
                return last
            self.backend.sleep(self.poll_s)
        raise ProSimLifecycleError(reason)

    def _publish_setup_until(
        self,
        message: Any,
        predicate: Callable[[VehicleSnapshot], bool],
        *,
        timeout_s: float,
        reason: str,
    ) -> VehicleSnapshot:
        """Republish setup transitions because ROS publishers are asynchronous.

        The installed Prometheus tutorial uses this same level-triggered setup
        pattern: ARMING and SET_CONTROL_MODE are sent until state confirms the
        transition.  This also handles a newly-created publisher whose first
        message precedes subscriber negotiation.
        """
        deadline = self.backend.now_ns() + int(timeout_s * 1_000_000_000)
        last = self.backend.snapshot()
        retry_s = max(self.poll_s, 0.25)
        while self.backend.now_ns() <= deadline:
            self.backend.publish_setup(message)
            last = self.backend.snapshot()
            if predicate(last):
                return last
            self.backend.sleep(retry_s)
        raise ProSimLifecycleError(reason)

    def arm_enter_command_takeoff(self, *, timeout_s: float = 60.0) -> VehicleSnapshot:
        self._publish_setup_until(
            make_arm_setup(self.backend.setup_type),
            lambda state: state.armed,
            timeout_s=timeout_s,
            reason="arm_timeout",
        )

        self._publish_setup_until(
            make_command_control_setup(self.backend.setup_type),
            lambda state: state.flight_mode == "OFFBOARD"
            and state.control_state == "COMMAND_CONTROL",
            timeout_s=timeout_s,
            reason="command_control_timeout",
        )

        self.backend.publish_command(
            make_takeoff(self.backend.command_type, self.command_ids.next())
        )

        def takeoff_ready(state: VehicleSnapshot) -> bool:
            altitude_ok = abs(state.position[2] - state.takeoff_height_m) <= 0.1
            vertical_settled = abs(state.velocity[2]) < 0.05
            return (
                altitude_ok
                and vertical_settled
                and state.is_ready(now_ns=self.backend.now_ns())
            )

        return self._wait(
            takeoff_ready, timeout_s=timeout_s, reason="takeoff_ready_timeout"
        )

    def publish_hold(self, reason: str) -> Any:
        del reason
        message = self.mapping.hold_message(self.command_ids.next())
        self.backend.publish_command(message)
        return message

    def hold_settle_land(
        self,
        *,
        hold_reason: str,
        settle_s: float = 1.0,
        timeout_s: float = 30.0,
    ) -> VehicleSnapshot:
        if settle_s <= 0.0 or timeout_s <= 0.0:
            raise ValueError("settle_s and timeout_s must be positive")
        self.publish_hold(hold_reason)
        deadline = self.backend.now_ns() + int(timeout_s * 1_000_000_000)
        settled_since = None
        while self.backend.now_ns() <= deadline:
            state = self.backend.snapshot()
            settled = (
                state.horizontal_speed_mps < 0.05
                and abs(state.yaw_rate_rad_s) < math.radians(3.0)
            )
            if settled:
                if settled_since is None:
                    settled_since = self.backend.now_ns()
                if self.backend.now_ns() - settled_since >= int(
                    settle_s * 1_000_000_000
                ):
                    break
            else:
                settled_since = None
            self.backend.sleep(self.poll_s)
        else:
            raise ProSimLifecycleError("settle_timeout")

        # ABSOLUTE_CONTROL intentionally rejects ordinary commands. Release
        # that latch before Land, matching the installed controller's
        # uav_cmd_cb state machine.
        self.backend.publish_command(
            self.mapping.exit_hold_message(self.command_ids.next())
        )
        self.backend.sleep(self.poll_s)
        self.backend.publish_command(
            make_land(self.backend.command_type, self.command_ids.next())
        )
        return self._wait(
            lambda state: not state.armed,
            timeout_s=timeout_s,
            reason="land_disarm_timeout",
        )


class RosProSimBackend:
    """Thin lazy-import ROS backend; safe to import in non-ROS unit tests."""

    def __init__(
        self,
        *,
        rospy: Any,
        rosgraph: Any,
        command_type: type,
        setup_type: type,
        state_type: type,
        control_type: type,
        expected_location_source: int = 4,
        camera_topic: str | None = None,
        image_type: type | None = None,
        publisher_poll_s: float = 1.0,
    ) -> None:
        self.rospy = rospy
        self.rosgraph = rosgraph
        self.command_type = command_type
        self.setup_type = setup_type
        self.state_type = state_type
        self.control_type = control_type
        self.expected_location_source = int(expected_location_source)
        self._lock = threading.Lock()
        self._state = None
        self._control = None
        self._state_received_ns = 0
        self._state_source_stamp_ns = 0
        self._control_received_ns = 0
        self._camera_frame = None
        self._takeoff_height_m: float | None = None
        # XML-RPC to the ROS master can stall for hundreds of milliseconds
        # under WSL2 load, so neither master query may sit on the control
        # tick path: the takeoff height is fetched once (the launch file
        # fixes it before this node starts) and the command-publisher watch
        # refreshes on a background thread from a seed query at init.
        self._publisher_poll_s = float(publisher_poll_s)
        self._publisher_cache = (0, self._query_unexpected_publishers())
        self._publisher_thread = threading.Thread(
            target=self._refresh_publishers_loop,
            name="prosim-publisher-watch",
            daemon=True,
        )
        self._publisher_thread.start()
        self.command_publisher = rospy.Publisher(
            "/uav1/prometheus/command", command_type, queue_size=10
        )
        self.setup_publisher = rospy.Publisher(
            "/uav1/prometheus/setup", setup_type, queue_size=10
        )
        self.state_subscriber = rospy.Subscriber(
            "/uav1/prometheus/state", state_type, self._state_callback, queue_size=10
        )
        self.control_subscriber = rospy.Subscriber(
            "/uav1/prometheus/control_state",
            control_type,
            self._control_callback,
            queue_size=10,
        )
        self.camera_subscriber = None
        if camera_topic and image_type is not None:
            self.camera_subscriber = rospy.Subscriber(
                camera_topic, image_type, self._camera_callback, queue_size=1
            )

    @classmethod
    def create(
        cls,
        *,
        node_name: str = "p450_pi05_prosim",
        expected_location_source: int = 4,
        camera_topic: str | None = None,
    ) -> "RosProSimBackend":
        import rosgraph  # type: ignore[import-not-found]
        import rospy  # type: ignore[import-not-found]
        from prometheus_msgs.msg import (  # type: ignore[import-not-found]
            UAVCommand,
            UAVControlState,
            UAVSetup,
            UAVState,
        )

        image_type = None
        if camera_topic:
            from sensor_msgs.msg import Image  # type: ignore[import-not-found]

            image_type = Image
        if not rospy.core.is_initialized():
            rospy.init_node(node_name, anonymous=False, disable_signals=True)
        return cls(
            rospy=rospy,
            rosgraph=rosgraph,
            command_type=UAVCommand,
            setup_type=UAVSetup,
            state_type=UAVState,
            control_type=UAVControlState,
            expected_location_source=expected_location_source,
            camera_topic=camera_topic,
            image_type=image_type,
        )

    def now_ns(self) -> int:
        return time.monotonic_ns()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def _state_callback(self, message: Any) -> None:
        with self._lock:
            self._state = message
            self._state_received_ns = self.now_ns()
            self._state_source_stamp_ns = header_stamp_ns(message)

    def _control_callback(self, message: Any) -> None:
        with self._lock:
            self._control = message
            self._control_received_ns = self.now_ns()

    def _camera_callback(self, message: Any) -> None:
        frame = parse_image_message(message, self.now_ns())
        if frame is None:
            return
        with self._lock:
            self._camera_frame = frame

    def camera_frame(self) -> CameraFrame | None:
        """Return the most recent decoded camera frame, if one has arrived."""
        with self._lock:
            return self._camera_frame

    def _stamp(self, message: Any) -> None:
        if hasattr(message, "header"):
            message.header.stamp = self.rospy.Time.now()

    def publish_setup(self, message: Any) -> None:
        self._stamp(message)
        self.setup_publisher.publish(message)

    def publish_command(self, message: Any) -> None:
        self._stamp(message)
        self.command_publisher.publish(message)

    def _query_unexpected_publishers(self) -> int:
        """Count foreign publishers on the command topic; failures fail closed."""
        try:
            publishers, _, _ = self.rosgraph.Master(
                self.rospy.get_name()
            ).getSystemState()
            nodes = []
            for topic, topic_nodes in publishers:
                if topic == "/uav1/prometheus/command":
                    nodes = list(topic_nodes)
                    break
            own_name = self.rospy.get_name()
            return len([node for node in nodes if node != own_name])
        except Exception:
            return 1

    def _refresh_publishers_loop(self) -> None:
        while True:
            time.sleep(self._publisher_poll_s)
            value = self._query_unexpected_publishers()
            with self._lock:
                self._publisher_cache = (self.now_ns(), value)

    def _unexpected_publishers(self, now_ns: int) -> int:
        return self._publisher_cache[1]

    @staticmethod
    def _name(value: int, choices: dict) -> str:
        return choices.get(int(value), "UNKNOWN")

    def snapshot(self) -> VehicleSnapshot:
        now_ns = self.now_ns()
        if self._takeoff_height_m is None:
            # Fixed by the control launch before this node starts; reading it
            # here once keeps every later tick off the ROS master.
            self._takeoff_height_m = float(
                self.rospy.get_param(
                    "/uav_control_main_1/control/Takeoff_height", 1.5
                )
            )
        with self._lock:
            state = self._state
            control = self._control
            state_received = self._state_received_ns
            control_received = self._control_received_ns
        state_source_stamp_ns = 0
        if state is None:
            position = velocity = attitude = attitude_rate = (0.0, 0.0, 0.0)
        else:
            position = tuple(float(value) for value in state.position)
            velocity = tuple(float(value) for value in state.velocity)
            attitude = tuple(float(value) for value in state.attitude)
            attitude_rate = tuple(float(value) for value in state.attitude_rate)
            state_source_stamp_ns = self._state_source_stamp_ns
        control_states = {
            getattr(self.control_type, "INIT", 0): "INIT",
            getattr(self.control_type, "RC_POS_CONTROL", 1): "RC_POS_CONTROL",
            getattr(self.control_type, "COMMAND_CONTROL", 2): "COMMAND_CONTROL",
            getattr(self.control_type, "LAND_CONTROL", 3): "LAND_CONTROL",
        }
        controllers = {
            getattr(self.control_type, "PX4_ORIGIN", 0): "PX4_ORIGIN",
            getattr(self.control_type, "PID", 1): "PID",
            getattr(self.control_type, "UDE", 2): "UDE",
            getattr(self.control_type, "NE", 3): "NE",
        }
        return VehicleSnapshot(
            connected=bool(getattr(state, "connected", False)),
            armed=bool(getattr(state, "armed", False)),
            flight_mode=str(getattr(state, "mode", "")),
            odometry_valid=bool(getattr(state, "odom_valid", False)),
            control_state=self._name(
                getattr(control, "control_state", 0), control_states
            ),
            controller=self._name(
                getattr(control, "pos_controller", 0), controllers
            ),
            failsafe=bool(getattr(control, "failsafe", True)),
            location_source=int(getattr(state, "location_source", -1)),
            expected_location_source=self.expected_location_source,
            state_received_monotonic_ns=state_received,
            control_received_monotonic_ns=control_received,
            unexpected_command_publishers=self._unexpected_publishers(now_ns),
            position=position,
            velocity=velocity,
            attitude=attitude,
            attitude_rate=attitude_rate,
            takeoff_height_m=self._takeoff_height_m,
            state_source_stamp_ns=state_source_stamp_ns,
        )
