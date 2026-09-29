from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import numpy as np


class ActionRejected(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ActionLimits:
    max_step_xy_m: float = 0.02
    max_step_z_m: float = 0.0
    max_step_yaw_rad: float = math.radians(1.0)
    max_chunk_xy_m: float = 0.15
    max_chunk_z_m: float = 0.0
    max_chunk_yaw_rad: float = math.radians(10.0)
    min_altitude_m: float = 0.3
    max_altitude_m: float = 2.0
    tolerance: float = 1e-7


def validate_action_chunk(
    actions: Any,
    *,
    mode: str,
    limits: ActionLimits | None = None,
    current_altitude_m: float | None = None,
    check_envelope: bool = True,
) -> np.ndarray:
    """Validate a complete action chunk and return an immutable-source copy."""
    if mode not in {"dry_run", "sim", "real"}:
        raise ActionRejected("bad_mode")
    limits = limits or ActionLimits()
    array = np.asarray(actions)
    if array.ndim != 2 or array.shape[0] == 0 or array.shape[1] != 4:
        raise ActionRejected("bad_shape")
    if array.dtype.kind not in "iuf":
        raise ActionRejected("non_numeric")

    numeric = array.astype(np.float64, copy=False)
    if not np.isfinite(numeric).all():
        raise ActionRejected("non_finite")

    horizontal_steps = np.linalg.norm(numeric[:, :2], axis=1)
    if np.any(horizontal_steps > limits.max_step_xy_m + limits.tolerance):
        raise ActionRejected("step_xy_limit")
    vertical_steps = np.abs(numeric[:, 2])
    if np.any(vertical_steps > limits.max_step_z_m + limits.tolerance):
        raise ActionRejected("step_z_limit")
    if np.any(np.abs(numeric[:, 3]) > limits.max_step_yaw_rad + limits.tolerance):
        raise ActionRejected("step_yaw_limit")
    if float(horizontal_steps.sum()) > limits.max_chunk_xy_m + limits.tolerance:
        raise ActionRejected("chunk_xy_limit")
    if float(vertical_steps.sum()) > limits.max_chunk_z_m + limits.tolerance:
        raise ActionRejected("chunk_z_limit")
    if float(np.abs(numeric[:, 3]).sum()) > limits.max_chunk_yaw_rad + limits.tolerance:
        raise ActionRejected("chunk_yaw_limit")

    if check_envelope:
        if current_altitude_m is None:
            if mode in {"sim", "real"}:
                raise ActionRejected("current_altitude_required")
        else:
            try:
                altitude = float(current_altitude_m)
            except (TypeError, ValueError) as error:
                raise ActionRejected("current_altitude_non_finite") from error
            if not math.isfinite(altitude):
                raise ActionRejected("current_altitude_non_finite")
            predicted = altitude + np.concatenate(
                (np.zeros(1, dtype=np.float64), np.cumsum(numeric[:, 2]))
            )
            if np.any(predicted < limits.min_altitude_m - limits.tolerance) or np.any(
                predicted > limits.max_altitude_m + limits.tolerance
            ):
                raise ActionRejected("altitude_envelope")
    return np.array(array, dtype=np.float32, copy=True)
