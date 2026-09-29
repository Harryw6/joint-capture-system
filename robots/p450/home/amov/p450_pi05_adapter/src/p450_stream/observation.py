from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Any, Mapping
from uuid import UUID

import numpy as np

from p450_stream.image import ImageError, to_policy_rgb
from p450_stream.kinematics import Pose


class ObservationError(ValueError):
    """Raised when a robot observation cannot be passed to the policy."""


@dataclass(frozen=True)
class P450Observation:
    image: np.ndarray
    state: np.ndarray
    prompt: str
    session_id: str
    request_seq: int
    base_step: int
    image_stamp_ns: int
    state_stamp_ns: int
    image_state_skew_ns: int
    client_send_monotonic_ns: int


def _nonnegative_int(value: Any, reason: str) -> int:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise ObservationError(reason)
    result = int(value)
    if result < 0:
        raise ObservationError(reason)
    return result


def parse_observation(value: Mapping[str, Any]) -> P450Observation:
    try:
        image = value["observation/image"]
        state = value["observation/state"]
        prompt = value["prompt"]
        session_id = value["session_id"]
        request_seq = value["request_seq"]
        base_step = value["base_step"]
        image_stamp_ns = value["image_stamp_ns"]
        state_stamp_ns = value["state_stamp_ns"]
        image_state_skew_ns = value["image_state_skew_ns"]
        client_send_monotonic_ns = value["client_send_monotonic_ns"]
    except (KeyError, TypeError) as error:
        raise ObservationError("missing_field") from error

    if not isinstance(image, np.ndarray) or image.shape != (224, 224, 3):
        raise ObservationError("image_shape")
    if image.dtype != np.uint8:
        raise ObservationError("image_dtype")
    if not isinstance(state, np.ndarray) or state.shape != (9,):
        raise ObservationError("state_shape")
    if state.dtype != np.float32:
        raise ObservationError("state_dtype")
    if not np.isfinite(state).all():
        raise ObservationError("state_finite")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ObservationError("prompt_empty")
    if not isinstance(session_id, str):
        raise ObservationError("session_id")
    try:
        UUID(session_id)
    except (ValueError, AttributeError) as error:
        raise ObservationError("session_id") from error
    request_seq = _nonnegative_int(request_seq, "request_seq")
    base_step = _nonnegative_int(base_step, "base_step")
    image_stamp_ns = _nonnegative_int(image_stamp_ns, "image_stamp_ns")
    state_stamp_ns = _nonnegative_int(state_stamp_ns, "state_stamp_ns")
    if isinstance(image_state_skew_ns, (bool, np.bool_)) or not isinstance(
        image_state_skew_ns, (int, np.integer)
    ):
        raise ObservationError("image_state_skew_ns")
    image_state_skew_ns = int(image_state_skew_ns)
    client_send_monotonic_ns = _nonnegative_int(
        client_send_monotonic_ns, "client_send_monotonic_ns"
    )

    return P450Observation(
        image=image,
        state=state,
        prompt=prompt,
        session_id=session_id,
        request_seq=request_seq,
        base_step=base_step,
        image_stamp_ns=image_stamp_ns,
        state_stamp_ns=state_stamp_ns,
        image_state_skew_ns=image_state_skew_ns,
        client_send_monotonic_ns=client_send_monotonic_ns,
    )


def build_live_observation(
    *,
    camera_frame: Any,
    pose: Pose,
    velocity: tuple[float, float, float],
    yaw_rate: float,
    state_source_stamp_ns: int,
    session_id: str,
    request_seq: int,
    base_step: int,
    client_send_monotonic_ns: int | None = None,
    prompt: str = "execute the safe box return trajectory",
) -> dict[str, Any]:
    """Build a policy observation from a live camera frame and vehicle state.

    The image is converted from the AirSim ROS encoding to the contract's
    224x224 uint8 RGB plane, and both source stamps are preserved so the
    freshness gate can compare them on the same epoch clock.
    """
    if camera_frame is None:
        raise ObservationError("camera_frame_missing")
    encoding = str(getattr(camera_frame, "encoding", ""))
    if encoding not in {"rgb8", "bgr8"}:
        raise ObservationError("camera_encoding")
    source_stamp_ns = int(getattr(camera_frame, "source_stamp_ns", 0))
    if source_stamp_ns <= 0:
        raise ObservationError("camera_source_stamp")
    state_stamp_ns = int(state_source_stamp_ns)
    if state_stamp_ns <= 0:
        raise ObservationError("state_source_stamp")
    try:
        image = to_policy_rgb(camera_frame.image, encoding)
    except ImageError as error:
        raise ObservationError(f"camera_image_{error}") from error

    state = np.asarray(
        [
            pose.x,
            pose.y,
            pose.z,
            math.sin(pose.yaw),
            math.cos(pose.yaw),
            velocity[0],
            velocity[1],
            velocity[2],
            yaw_rate,
        ],
        dtype=np.float32,
    )
    return {
        "observation/image": image,
        "observation/state": state,
        "prompt": prompt,
        "session_id": session_id,
        "request_seq": int(request_seq),
        "base_step": int(base_step),
        "image_stamp_ns": source_stamp_ns,
        "state_stamp_ns": state_stamp_ns,
        "image_state_skew_ns": source_stamp_ns - state_stamp_ns,
        "client_send_monotonic_ns": int(
            time.monotonic_ns()
            if client_send_monotonic_ns is None
            else client_send_monotonic_ns
        ),
    }
