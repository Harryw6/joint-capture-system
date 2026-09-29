from __future__ import annotations

import math

import numpy as np


def _segment(steps: int, action: tuple[float, float, float, float]) -> np.ndarray:
    return np.repeat(np.asarray([action], dtype=np.float32), repeats=steps, axis=0)


def box_return_v1(dt_s: float = 0.1) -> np.ndarray:
    """Return the fixed 160-step acceptance trajectory."""
    if not math.isclose(dt_s, 0.1, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("dt_s must be 0.1 for box_return_v1")

    hold = (0.0, 0.0, 0.0, 0.0)
    one_degree = math.radians(1.0)
    return np.concatenate(
        [
            _segment(10, hold),
            _segment(20, (0.01, 0.0, 0.0, 0.0)),
            _segment(10, hold),
            _segment(20, (0.0, 0.01, 0.0, 0.0)),
            _segment(10, hold),
            _segment(15, (0.0, 0.0, 0.0, one_degree)),
            _segment(10, hold),
            _segment(15, (0.0, 0.0, 0.0, -one_degree)),
            _segment(20, (0.0, -0.01, 0.0, 0.0)),
            _segment(20, (-0.01, 0.0, 0.0, 0.0)),
            _segment(10, hold),
        ],
        axis=0,
    ).astype(np.float32, copy=False)


def _six_axis_trajectory(step_m: float) -> np.ndarray:
    """Build the 200-step XYZ calibration at ``step_m`` metres per step.

    Each direction moves 20 steps then holds 10, so the amplitude per
    direction is ``20 * step_m`` metres at ``10 * step_m`` m/s.
    """
    hold = (0.0, 0.0, 0.0, 0.0)
    return np.concatenate(
        [
            _segment(10, hold),
            _segment(20, (step_m, 0.0, 0.0, 0.0)),
            _segment(10, hold),
            _segment(20, (-step_m, 0.0, 0.0, 0.0)),
            _segment(10, hold),
            _segment(20, (0.0, step_m, 0.0, 0.0)),
            _segment(10, hold),
            _segment(20, (0.0, -step_m, 0.0, 0.0)),
            _segment(10, hold),
            _segment(20, (0.0, 0.0, step_m, 0.0)),
            _segment(10, hold),
            _segment(20, (0.0, 0.0, -step_m, 0.0)),
            _segment(20, hold),
        ],
        axis=0,
    ).astype(np.float32, copy=False)


def six_axis_0p2m_v1(dt_s: float = 0.1) -> np.ndarray:
    """Return a balanced 200-step XYZ calibration trajectory in SI units."""
    if not math.isclose(dt_s, 0.1, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("dt_s must be 0.1 for six_axis_0p2m_v1")

    return _six_axis_trajectory(0.01)


def six_axis_0p4m_v1(dt_s: float = 0.1) -> np.ndarray:
    """Return the 0.4 m-per-direction calibration with a slow vertical axis.

    Horizontal directions move 20 steps at 0.02 m (0.4 m in 2 s, 0.2 m/s).
    The vertical axis keeps the proven 0.01 m/step pace because the installed
    PX4/Prometheus stack lags roughly 2 s behind 0.2 m/s vertical commands
    (run 20260824-203758 measured a 0.119 m down leg), and a single sustained
    4 s z ramp winds the vertical cascade up into a late overshoot burst (run
    20260824-204711: commanded 0.4 m, measured up/down legs of 0.693/0.664 m
    with vz spiking to 0.39 m/s during the hold).  Each 0.4 m vertical leg is
    therefore split into two 0.2 m sub-legs -- the exact shape the 0.2 m
    calibration tracks accurately -- separated by 10-step settle windows,
    giving 250 steps total.
    """
    if not math.isclose(dt_s, 0.1, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("dt_s must be 0.1 for six_axis_0p4m_v1")

    hold = (0.0, 0.0, 0.0, 0.0)
    return np.concatenate(
        [
            _segment(10, hold),
            _segment(20, (0.02, 0.0, 0.0, 0.0)),
            _segment(10, hold),
            _segment(20, (-0.02, 0.0, 0.0, 0.0)),
            _segment(10, hold),
            _segment(20, (0.0, 0.02, 0.0, 0.0)),
            _segment(10, hold),
            _segment(20, (0.0, -0.02, 0.0, 0.0)),
            _segment(10, hold),
            _segment(20, (0.0, 0.0, 0.01, 0.0)),
            _segment(10, hold),
            _segment(20, (0.0, 0.0, 0.01, 0.0)),
            _segment(10, hold),
            _segment(20, (0.0, 0.0, -0.01, 0.0)),
            _segment(10, hold),
            _segment(20, (0.0, 0.0, -0.01, 0.0)),
            _segment(10, hold),
        ],
        axis=0,
    ).astype(np.float32, copy=False)
