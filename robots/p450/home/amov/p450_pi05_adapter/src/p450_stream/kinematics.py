from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence


@dataclass(frozen=True)
class Pose:
    x: float
    y: float
    z: float
    yaw: float


def apply_body_action(pose: Pose, action: Sequence[float]) -> Pose:
    """Apply one yaw-aligned body-frame delta to a world-frame pose."""
    dx_body, dy_body, dz_up, dyaw = (float(value) for value in action)
    cosine = math.cos(pose.yaw)
    sine = math.sin(pose.yaw)
    world_dx = cosine * dx_body - sine * dy_body
    world_dy = sine * dx_body + cosine * dy_body
    yaw = math.atan2(math.sin(pose.yaw + dyaw), math.cos(pose.yaw + dyaw))
    return Pose(
        x=pose.x + world_dx,
        y=pose.y + world_dy,
        z=pose.z + dz_up,
        yaw=yaw,
    )
