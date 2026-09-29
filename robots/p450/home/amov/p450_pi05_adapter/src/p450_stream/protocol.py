from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping


class ProtocolError(ValueError):
    """Raised when a policy server does not implement the P450 contract."""


@dataclass(frozen=True)
class PolicyProfile:
    protocol: str = "openpi-websocket-msgpack"
    schema_version: str = "p450.body_delta.v1"
    action_dim: int = 4
    action_horizon: int = 10
    control_dt_s: float = 0.1
    action_repr: str = "body_delta_pose"
    action_fields: tuple[str, ...] = ("dx", "dy", "dz", "dyaw")
    action_units: tuple[str, ...] = ("m", "m", "m", "rad")
    body_frame: str = "FLU"
    yaw_positive: str = "CCW"
    image_encoding: str = "RGB"
    image_shape: tuple[int, ...] = (224, 224, 3)

    def as_metadata(self, policy_name: str) -> dict[str, Any]:
        if not isinstance(policy_name, str) or not policy_name.strip():
            raise ValueError("policy_name must be non-empty")
        return {
            "protocol": self.protocol,
            "schema_version": self.schema_version,
            "policy_name": policy_name,
            "action_dim": self.action_dim,
            "action_horizon": self.action_horizon,
            "control_dt_s": self.control_dt_s,
            "action_repr": self.action_repr,
            "action_fields": list(self.action_fields),
            "action_units": list(self.action_units),
            "body_frame": self.body_frame,
            "yaw_positive": self.yaw_positive,
            "image_encoding": self.image_encoding,
            "image_shape": list(self.image_shape),
        }


DEFAULT_PROFILE = PolicyProfile()


def validate_server_metadata(
    metadata: Mapping[str, Any], profile: PolicyProfile = DEFAULT_PROFILE
) -> dict[str, Any]:
    if not isinstance(metadata, Mapping):
        raise ProtocolError("metadata_mapping")

    expected = profile.as_metadata("expected")
    extra_fields = sorted(set(metadata) - set(expected))
    if extra_fields:
        raise ProtocolError(f"metadata_extra_{extra_fields[0]}")
    for field, expected_value in expected.items():
        if field == "policy_name":
            value = metadata.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ProtocolError("metadata_policy_name")
            continue
        try:
            value = metadata[field]
        except KeyError as error:
            raise ProtocolError(f"metadata_{field}") from error
        if isinstance(expected_value, list):
            try:
                value = list(value)
            except TypeError as error:
                raise ProtocolError(f"metadata_{field}") from error
            if len(value) != len(expected_value) or any(
                type(item) is not type(expected_item) or item != expected_item
                for item, expected_item in zip(value, expected_value)
            ):
                raise ProtocolError(f"metadata_{field}")
            continue
        if type(value) is not type(expected_value) or value != expected_value:
            raise ProtocolError(f"metadata_{field}")

    return deepcopy(dict(metadata))
