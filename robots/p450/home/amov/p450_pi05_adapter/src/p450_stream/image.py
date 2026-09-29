from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np


class ImageError(ValueError):
    """Raised when a camera frame cannot satisfy the policy image contract."""


def to_policy_rgb(
    image: Any, encoding: str, *, target_hw: tuple[int, int] = (224, 224)
) -> np.ndarray:
    if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[2] != 3:
        raise ImageError("image_shape")
    if image.dtype != np.uint8:
        raise ImageError("image_dtype")
    if encoding not in {"rgb8", "bgr8"}:
        raise ImageError("image_encoding")
    if (
        len(target_hw) != 2
        or not all(isinstance(value, int) for value in target_hw)
        or min(target_hw) <= 0
    ):
        raise ImageError("target_shape")

    rgb = image if encoding == "rgb8" else image[:, :, ::-1]
    source_h, source_w = rgb.shape[:2]
    target_h, target_w = target_hw
    row_index = np.floor(np.arange(target_h) * source_h / target_h).astype(int)
    col_index = np.floor(np.arange(target_w) * source_w / target_w).astype(int)
    return np.ascontiguousarray(rgb[row_index[:, None], col_index[None, :], :])


@dataclass(frozen=True)
class TimingResult:
    valid: bool
    failures: tuple[str, ...]
    image_age_ns: int
    state_age_ns: int
    source_skew_ns: int


def validate_image_state_timing(
    *,
    now_monotonic_ns: int,
    image_received_monotonic_ns: int,
    state_received_monotonic_ns: int,
    image_stamp_ns: int,
    state_stamp_ns: int,
    max_image_age_ns: int = 200_000_000,
    max_state_age_ns: int = 100_000_000,
    max_source_skew_ns: int = 50_000_000,
    require_source_stamps: bool = False,
) -> TimingResult:
    image_age = int(now_monotonic_ns) - int(image_received_monotonic_ns)
    state_age = int(now_monotonic_ns) - int(state_received_monotonic_ns)
    source_skew = abs(int(image_stamp_ns) - int(state_stamp_ns))
    failures: list[str] = []
    if require_source_stamps and (image_stamp_ns <= 0 or state_stamp_ns <= 0):
        failures.append("source_stamp_missing")
    if image_age < 0 or image_age > max_image_age_ns:
        failures.append("image_stale")
    if state_age < 0 or state_age > max_state_age_ns:
        failures.append("state_stale")
    if source_skew > max_source_skew_ns:
        failures.append("source_skew")
    return TimingResult(
        valid=not failures,
        failures=tuple(failures),
        image_age_ns=image_age,
        state_age_ns=state_age,
        source_skew_ns=source_skew,
    )
