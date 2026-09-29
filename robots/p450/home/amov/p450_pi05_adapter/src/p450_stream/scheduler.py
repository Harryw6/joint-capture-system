from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Callable

import numpy as np

from p450_stream.safety import ActionLimits, ActionRejected, validate_action_chunk
from p450_stream.protocol import DEFAULT_PROFILE, PolicyProfile


@dataclass(frozen=True)
class ActionDelivery:
    session_id: str
    seq: int
    base_step: int
    first_step: int
    trimmed_steps: int
    action_count: int
    expires_at: float


class ActionBuffer:
    """Thread-safe mailbox that binds every action to one global executor step."""

    def __init__(
        self,
        *,
        max_age_s: float,
        clock: Callable[[], float] = time.monotonic,
        mode: str = "dry_run",
        profile: PolicyProfile = DEFAULT_PROFILE,
        limits: ActionLimits | None = None,
    ) -> None:
        if max_age_s <= 0:
            raise ValueError("max_age_s must be positive")
        self._max_age_s = float(max_age_s)
        self._clock = clock
        self._mode = mode
        self._profile = profile
        self._limits = limits
        self._lock = threading.Lock()
        self._session_id: str | None = None
        self._last_seq = -1
        self._actions: np.ndarray | None = None
        self._first_step = 0
        self._index = 0
        self._expires_at = 0.0
        self._last_heartbeat: float | None = None
        self.state = "HOLD"

    def _clear_unlocked(self) -> None:
        self._actions = None
        self._first_step = 0
        self._index = 0
        self._expires_at = 0.0
        self.state = "HOLD"

    def _reject_unlocked(self, reason: str) -> None:
        self._clear_unlocked()
        raise ActionRejected(reason)

    def begin_session(self, session_id: str) -> None:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be non-empty")
        with self._lock:
            self._session_id = session_id
            self._last_seq = -1
            self._last_heartbeat = None
            self._clear_unlocked()

    def accept(
        self,
        *,
        session_id: str,
        seq: int,
        base_step: int,
        current_step: int,
        requested_at: float,
        actions: np.ndarray,
        current_altitude_m: float | None = None,
        check_envelope: bool = True,
    ) -> ActionDelivery:
        with self._lock:
            if session_id != self._session_id:
                self._reject_unlocked("session")
            if (
                isinstance(seq, (bool, np.bool_))
                or not isinstance(seq, (int, np.integer))
                or int(seq) <= self._last_seq
            ):
                self._reject_unlocked("sequence")
            if not isinstance(base_step, (int, np.integer)) or int(base_step) < 0:
                self._reject_unlocked("base_step")
            if not isinstance(current_step, (int, np.integer)) or int(current_step) < 0:
                self._reject_unlocked("current_step")
            base_step = int(base_step)
            current_step = int(current_step)
            if base_step > current_step:
                self._reject_unlocked("future_base_step")
            try:
                requested_at = float(requested_at)
            except (TypeError, ValueError):
                self._reject_unlocked("requested_at")
            now = self._clock()
            expires_at = requested_at + self._max_age_s
            if not np.isfinite(requested_at) or requested_at > now:
                self._reject_unlocked("requested_at")
            if now >= expires_at:
                self._reject_unlocked("request_expired")
            if not isinstance(actions, np.ndarray) or actions.shape != (
                self._profile.action_horizon,
                self._profile.action_dim,
            ):
                self._reject_unlocked("response_shape")
            if actions.dtype != np.float32:
                self._reject_unlocked("response_dtype")
            try:
                validated = validate_action_chunk(
                    actions,
                    mode=self._mode,
                    limits=self._limits,
                    current_altitude_m=current_altitude_m,
                    check_envelope=check_envelope,
                )
            except ActionRejected:
                self._clear_unlocked()
                raise

            trimmed_steps = current_step - base_step
            if trimmed_steps >= len(validated):
                self._reject_unlocked("fully_stale")
            suffix = validated[trimmed_steps:].copy()
            self._last_seq = int(seq)
            self._actions = suffix
            self._first_step = current_step
            self._index = 0
            self._expires_at = expires_at
            self.state = "EXECUTING"
            return ActionDelivery(
                session_id=session_id,
                seq=int(seq),
                base_step=base_step,
                first_step=current_step,
                trimmed_steps=trimmed_steps,
                action_count=len(suffix),
                expires_at=expires_at,
            )

    def note_heartbeat(self) -> None:
        with self._lock:
            self._last_heartbeat = self._clock()

    def next_action(self, *, step: int) -> np.ndarray:
        with self._lock:
            expected_step = self._first_step + self._index
            if (
                self._actions is None
                or self._clock() >= self._expires_at
                or self._index >= len(self._actions)
                or step != expected_step
            ):
                self._clear_unlocked()
                return np.zeros(4, dtype=np.float32)
            action = self._actions[self._index].copy()
            self._index += 1
            return action
