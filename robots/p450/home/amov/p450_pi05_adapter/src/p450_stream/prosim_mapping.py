"""Installed AMOV ProSim and official Prometheus ROS message mappings.

The ProSim velocity and accumulated-position mappings coexist with the
official BODY-frame ``XYZ_POS_BODY`` position-increment mapping. Policy
actions can only create movement messages through these movement factories;
arm, takeoff, command-control, and landing factories remain separate so a
remote policy cannot acquire lifecycle authority.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from p450_stream.kinematics import Pose, apply_body_action
from p450_stream.ros_adapter import PrometheusMapping


COMMAND_TOPIC = "/uav1/prometheus/command"
SETUP_TOPIC = "/uav1/prometheus/setup"


def _positive_command_id(command_id: int) -> int:
    value = int(command_id)
    if value <= 0:
        raise ValueError("command_id must be positive")
    return value


def _command(command_type: type, command_id: int, *, frame_id: str) -> Any:
    message = command_type()
    message.header.frame_id = frame_id
    message.Command_ID = _positive_command_id(command_id)
    return message


def build_prometheus_mapping(
    command_type: type, *, dt_s: float = 0.1
) -> PrometheusMapping:
    period = float(dt_s)
    if not math.isfinite(period) or period <= 0.0:
        raise ValueError("dt_s must be positive and finite")

    def move_message(action: np.ndarray, command_id: int) -> Any:
        checked = np.asarray(action, dtype=np.float32)
        if checked.shape != (4,) or not np.isfinite(checked).all():
            raise ValueError("action must be finite with shape (4,)")
        message = _command(command_type, command_id, frame_id="BODY")
        message.Agent_CMD = command_type.Move
        message.Control_Level = command_type.DEFAULT_CONTROL
        message.Move_mode = command_type.XYZ_VEL_BODY
        message.velocity_ref = [float(value / period) for value in checked[:3]]
        message.Yaw_Rate_Mode = True
        message.yaw_rate_ref = float(checked[3] / period)
        return message

    def hold_message(command_id: int) -> Any:
        message = _command(command_type, command_id, frame_id="ENU")
        message.Agent_CMD = command_type.Current_Pos_Hover
        message.Control_Level = command_type.ABSOLUTE_CONTROL
        return message

    def exit_hold_message(command_id: int) -> Any:
        message = _command(command_type, command_id, frame_id="ENU")
        message.Agent_CMD = command_type.Current_Pos_Hover
        message.Control_Level = command_type.EXIT_ABSOLUTE_CONTROL
        return message

    return PrometheusMapping(
        topic=COMMAND_TOPIC,
        message_type=command_type,
        move_message=move_message,
        hold_message=hold_message,
        exit_hold_message=exit_hold_message,
        verified=True,
    )


def build_prometheus_body_position_mapping(command_type: type) -> PrometheusMapping:
    """Build the official BODY-frame position-increment command mapping."""

    def move_message(action: np.ndarray, command_id: int) -> Any:
        raw = np.asarray(action)
        if (
            raw.shape != (4,)
            or not np.issubdtype(raw.dtype, np.number)
            or np.iscomplexobj(raw)
            or not np.isfinite(raw).all()
        ):
            raise ValueError("action must be finite with shape (4,)")
        checked = np.asarray(raw, dtype=np.float32)
        if not np.isfinite(checked).all():
            raise ValueError("action must be finite with shape (4,)")
        message = _command(command_type, command_id, frame_id="BODY")
        message.Agent_CMD = command_type.Move
        message.Control_Level = command_type.DEFAULT_CONTROL
        message.Move_mode = command_type.XYZ_POS_BODY
        message.position_ref = [float(value) for value in checked[:3]]
        message.velocity_ref = [0.0, 0.0, 0.0]
        message.acceleration_ref = [0.0, 0.0, 0.0]
        message.Yaw_Rate_Mode = False
        message.yaw_ref = float(checked[3])
        return message

    velocity_mapping = build_prometheus_mapping(command_type)
    return PrometheusMapping(
        topic=COMMAND_TOPIC,
        message_type=command_type,
        move_message=move_message,
        hold_message=velocity_mapping.hold_message,
        exit_hold_message=velocity_mapping.exit_hold_message,
        verified=True,
    )


def build_prometheus_position_mapping(
    command_type: type,
    *,
    initial_position: tuple[float, float, float],
    initial_yaw: float,
) -> PrometheusMapping:
    """Accumulate body deltas into inertial position targets for simulation."""
    if len(initial_position) != 3 or not all(
        math.isfinite(float(value)) for value in initial_position
    ):
        raise ValueError("initial_position must contain three finite values")
    if not math.isfinite(float(initial_yaw)):
        raise ValueError("initial_yaw must be finite")
    target = Pose(
        x=float(initial_position[0]),
        y=float(initial_position[1]),
        z=float(initial_position[2]),
        yaw=float(initial_yaw),
    )

    def move_message(action: np.ndarray, command_id: int) -> Any:
        nonlocal target
        checked = np.asarray(action, dtype=np.float32)
        if checked.shape != (4,) or not np.isfinite(checked).all():
            raise ValueError("action must be finite with shape (4,)")
        target = apply_body_action(target, checked)
        message = _command(command_type, command_id, frame_id="ENU")
        message.Agent_CMD = command_type.Move
        message.Control_Level = command_type.DEFAULT_CONTROL
        message.Move_mode = command_type.XYZ_POS
        message.position_ref = [target.x, target.y, target.z]
        message.Yaw_Rate_Mode = False
        message.yaw_ref = target.yaw
        return message

    velocity_mapping = build_prometheus_mapping(command_type)
    return PrometheusMapping(
        topic=COMMAND_TOPIC,
        message_type=command_type,
        move_message=move_message,
        hold_message=velocity_mapping.hold_message,
        exit_hold_message=velocity_mapping.exit_hold_message,
        verified=True,
    )


def make_takeoff(command_type: type, command_id: int) -> Any:
    message = _command(command_type, command_id, frame_id="ENU")
    message.Agent_CMD = command_type.Init_Pos_Hover
    message.Control_Level = command_type.DEFAULT_CONTROL
    return message


def make_land(command_type: type, command_id: int) -> Any:
    message = _command(command_type, command_id, frame_id="ENU")
    message.Agent_CMD = command_type.Land
    message.Control_Level = command_type.DEFAULT_CONTROL
    return message


def make_arm_setup(setup_type: type) -> Any:
    message = setup_type()
    message.cmd = 0
    message.arming = True
    message.px4_mode = ""
    message.control_state = ""
    return message


def make_command_control_setup(setup_type: type) -> Any:
    message = setup_type()
    message.cmd = 3
    message.arming = True
    message.px4_mode = ""
    message.control_state = "COMMAND_CONTROL"
    return message
