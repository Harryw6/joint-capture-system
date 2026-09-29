from __future__ import annotations

import math
from typing import Any, Mapping

import numpy as np

from p450_stream.observation import parse_observation
from p450_stream.scenarios import box_return_v1, six_axis_0p2m_v1, six_axis_0p4m_v1


SCENARIOS = {
    "box_return_v1": box_return_v1,
    "six_axis_0p2m_v1": six_axis_0p2m_v1,
    "six_axis_0p4m_v1": six_axis_0p4m_v1,
}


class MockP450Policy:
    """Stateless deterministic policy with the OpenPI ``infer`` contract."""

    def __init__(
        self,
        action_horizon: int = 10,
        *,
        scenario_name: str = "box_return_v1",
        terminal_feedback_start_step: int | None = None,
        terminal_hold_start_step: int = 150,
        return_gain_per_s: float = 0.8,
        return_velocity_damping_s: float = 0.8,
    ) -> None:
        if action_horizon <= 0:
            raise ValueError("action_horizon must be positive")
        if scenario_name not in SCENARIOS:
            raise ValueError(f"unknown scenario: {scenario_name}")
        scenario = SCENARIOS[scenario_name]()
        if scenario_name != "box_return_v1":
            terminal_feedback_start_step = None
        if terminal_feedback_start_step is not None and not (
            0 <= terminal_feedback_start_step < terminal_hold_start_step
        ):
            raise ValueError("terminal feedback step range is invalid")
        if terminal_hold_start_step > len(scenario):
            raise ValueError("terminal hold step is outside the scenario")
        if not math.isfinite(return_gain_per_s) or return_gain_per_s <= 0.0:
            raise ValueError("return_gain_per_s must be positive")
        if (
            not math.isfinite(return_velocity_damping_s)
            or return_velocity_damping_s < 0.0
        ):
            raise ValueError("return_velocity_damping_s must be nonnegative")
        self._action_horizon = int(action_horizon)
        self._scenario_name = scenario_name
        self._scenario = scenario
        self._terminal_feedback_start_step = terminal_feedback_start_step
        self._terminal_hold_start_step = int(terminal_hold_start_step)
        self._return_gain_per_s = float(return_gain_per_s)
        self._return_velocity_damping_s = float(return_velocity_damping_s)
        self._session_origins: dict[str, tuple[float, float]] = {}

    def infer(self, observation: Mapping[str, Any]) -> dict[str, Any]:
        parsed = parse_observation(observation)
        actions = np.zeros((self._action_horizon, 4), dtype=np.float32)
        available = self._scenario[
            parsed.base_step : parsed.base_step + self._action_horizon
        ]
        actions[: len(available)] = available
        origin = self._session_origins.setdefault(
            parsed.session_id,
            (float(parsed.state[0]), float(parsed.state[1])),
        )
        feedback_enabled = self._terminal_feedback_start_step is not None
        if feedback_enabled and parsed.base_step >= self._terminal_feedback_start_step:
            correction_count = max(
                0,
                min(
                    self._action_horizon,
                    self._terminal_hold_start_step - parsed.base_step,
                ),
            )
            actions.fill(0.0)
            if correction_count:
                world_dx = origin[0] - float(parsed.state[0])
                world_dy = origin[1] - float(parsed.state[1])
                world_vx = float(parsed.state[5])
                world_vy = float(parsed.state[6])
                yaw = math.atan2(float(parsed.state[3]), float(parsed.state[4]))
                cosine = math.cos(yaw)
                sine = math.sin(yaw)
                world_command_x = (
                    self._return_gain_per_s * world_dx
                    - self._return_velocity_damping_s * world_vx
                )
                world_command_y = (
                    self._return_gain_per_s * world_dy
                    - self._return_velocity_damping_s * world_vy
                )
                body_command_x = cosine * world_command_x + sine * world_command_y
                body_command_y = -sine * world_command_x + cosine * world_command_y
                step = np.asarray(
                    [body_command_x, body_command_y], dtype=np.float64
                ) * 0.1
                magnitude = float(np.linalg.norm(step))
                # Ten-step chunks must remain below the profile's 0.15 m
                # aggregate horizontal limit as well as its per-step limit.
                if magnitude > 0.014:
                    step *= 0.014 / magnitude
                actions[:correction_count, :2] = step.astype(np.float32)

        metadata = {
            "scenario": self._scenario_name,
            "base_step": parsed.base_step,
            "scenario_length": len(self._scenario),
        }
        if feedback_enabled:
            metadata["terminal_feedback"] = True
        return {
            "actions": actions,
            "session_id": parsed.session_id,
            "request_seq": parsed.request_seq,
            "base_step": parsed.base_step,
            "mock_metadata": metadata,
        }
