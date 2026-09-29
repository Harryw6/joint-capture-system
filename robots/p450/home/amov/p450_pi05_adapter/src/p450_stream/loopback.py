from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

import numpy as np
from openpi_client.websocket_client_policy import WebsocketClientPolicy

from p450_stream.kinematics import Pose, apply_body_action
from p450_stream.protocol import validate_server_metadata
from p450_stream.scheduler import ActionBuffer


def make_observation(
    *,
    session_id: str,
    request_seq: int,
    base_step: int,
    pose: Pose | None = None,
    velocity: tuple[float, float, float] = (0.0, 0.0, 0.0),
    yaw_rate: float = 0.0,
    image_stamp_ns: int = 0,
    state_stamp_ns: int = 0,
    client_send_monotonic_ns: int | None = None,
) -> dict[str, Any]:
    pose = pose or Pose(x=0.0, y=0.0, z=0.4, yaw=0.0)
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
        "observation/image": np.zeros((224, 224, 3), dtype=np.uint8),
        "observation/state": state,
        "prompt": "execute the safe box return trajectory",
        "session_id": session_id,
        "request_seq": int(request_seq),
        "base_step": int(base_step),
        "image_stamp_ns": int(image_stamp_ns),
        "state_stamp_ns": int(state_stamp_ns),
        "image_state_skew_ns": int(image_stamp_ns - state_stamp_ns),
        "client_send_monotonic_ns": int(
            time.monotonic_ns()
            if client_send_monotonic_ns is None
            else client_send_monotonic_ns
        ),
    }


def close_policy_client(client: Any) -> None:
    """Close OpenPI clients across versions while isolating compatibility access."""
    close = getattr(client, "close", None)
    if callable(close):
        close()
        return
    websocket = getattr(client, "_ws", None)
    if websocket is not None:
        websocket.close()


def _write_artifacts(
    artifact_dir: Path,
    events: list[dict[str, Any]],
    trajectory: list[dict[str, float | int]],
    summary: dict[str, Any],
) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    with (artifact_dir / "events.jsonl").open("w", encoding="utf-8") as stream:
        for event in events:
            stream.write(json.dumps(event, sort_keys=True) + "\n")
    with (artifact_dir / "trajectory.csv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["step", "x", "y", "z", "yaw"],
        )
        writer.writeheader()
        writer.writerows(trajectory)
    (artifact_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def run_loopback(
    *,
    host: str,
    port: int,
    execute_steps: int,
    artifact_dir: Path,
    realtime: bool,
) -> dict[str, Any]:
    if execute_steps <= 0:
        raise ValueError("execute_steps must be positive")
    client = WebsocketClientPolicy(host=host, port=port)
    try:
        validate_server_metadata(client.get_server_metadata())
        session_id = str(uuid4())
        pose = Pose(x=0.0, y=0.0, z=0.4, yaw=0.0)
        initial = pose
        executed_step = 0
        request_seq = 0
        scenario_length = 160
        events: list[dict[str, Any]] = []
        trajectory: list[dict[str, float | int]] = [
            {"step": 0, "x": pose.x, "y": pose.y, "z": pose.z, "yaw": pose.yaw}
        ]
        buffer = ActionBuffer(max_age_s=0.5, mode="dry_run")
        buffer.begin_session(session_id)

        while executed_step < scenario_length:
            base_step = executed_step
            observation = make_observation(
                session_id=session_id,
                request_seq=request_seq,
                base_step=base_step,
                pose=pose,
            )
            requested_at = time.monotonic()
            result = client.infer(observation)
            if result.get("session_id") != session_id:
                raise RuntimeError("session mismatch")
            if result.get("request_seq") != request_seq:
                raise RuntimeError("request sequence mismatch")
            if result.get("base_step") != base_step:
                raise RuntimeError("response base step mismatch")
            scenario_length = int(result["mock_metadata"]["scenario_length"])
            delivery = buffer.accept(
                session_id=session_id,
                seq=request_seq,
                base_step=int(result["base_step"]),
                current_step=executed_step,
                requested_at=requested_at,
                actions=result["actions"],
            )
            events.append(
                {
                    "type": "chunk",
                    "session_id": session_id,
                    "request_seq": request_seq,
                    "base_step": base_step,
                    "response_base_step": int(result["base_step"]),
                    "trimmed_steps": delivery.trimmed_steps,
                    "infer_ms": float(result["server_timing"]["infer_ms"]),
                }
            )
            count = min(execute_steps, scenario_length - executed_step)
            for _ in range(count):
                action = buffer.next_action(step=executed_step)
                pose = apply_body_action(pose, action)
                executed_step += 1
                events.append(
                    {
                        "type": "action",
                        "step": executed_step,
                        "action": [float(value) for value in action],
                    }
                )
                trajectory.append(
                    {
                        "step": executed_step,
                        "x": pose.x,
                        "y": pose.y,
                        "z": pose.z,
                        "yaw": pose.yaw,
                    }
                )
                if realtime:
                    time.sleep(0.1)
            request_seq += 1
    finally:
        close_policy_client(client)

    horizontal_error = math.hypot(pose.x - initial.x, pose.y - initial.y)
    yaw_error = abs(
        math.atan2(math.sin(pose.yaw - initial.yaw), math.cos(pose.yaw - initial.yaw))
    )
    summary = {
        "passed": horizontal_error <= 0.001 and yaw_error <= math.radians(0.1),
        "session_id": session_id,
        "executed_steps": executed_step,
        "requests": request_seq,
        "horizontal_error_m": horizontal_error,
        "yaw_error_deg": math.degrees(yaw_error),
        "final_pose": {
            "x": pose.x,
            "y": pose.y,
            "z": pose.z,
            "yaw": pose.yaw,
        },
    }
    _write_artifacts(Path(artifact_dir), events, trajectory, summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Execute the mock P450 stream")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--execute-steps", type=int, default=5)
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=Path("artifacts/box_return_v1"),
    )
    parser.add_argument("--no-realtime", action="store_true")
    args = parser.parse_args()
    summary = run_loopback(
        host=args.host,
        port=args.port,
        execute_steps=args.execute_steps,
        artifact_dir=args.artifact_dir,
        realtime=not args.no_realtime,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    if not summary["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
