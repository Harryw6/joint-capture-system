"""Incremental, machine-readable evidence for ProSim runs."""

from __future__ import annotations

import csv
import json
from pathlib import Path
import queue
import threading
from typing import Any, Dict, Iterable, Tuple


class EvidenceWriter:
    def __init__(self, artifact_dir: str | Path) -> None:
        self.artifact_dir = Path(artifact_dir)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self._state_rows = []
        self._line_queue = queue.Queue()
        self._writer_error = None
        self._finished = False
        self._worker = threading.Thread(
            target=self._write_jsonl_loop,
            name="prosim-evidence-writer",
            daemon=True,
        )
        self._worker.start()

    def _write_jsonl_loop(self) -> None:
        filenames = (
            "policy_events.jsonl",
            "buffer_events.jsonl",
            "published_commands.jsonl",
        )
        streams = {}
        try:
            streams = {
                filename: (self.artifact_dir / filename).open(
                    "a", encoding="utf-8", buffering=1
                )
                for filename in filenames
            }
            while True:
                item = self._line_queue.get()
                try:
                    if item is None:
                        return
                    kind = item[0]
                    if kind == "image":
                        _, sequence, base_step, rgb = item
                        self._write_observation_image(
                            sequence=int(sequence), base_step=int(base_step), rgb=rgb
                        )
                    else:
                        _, filename, line = item
                        streams[filename].write(line)
                        streams[filename].write("\n")
                finally:
                    self._line_queue.task_done()
        except BaseException as error:
            self._writer_error = error
        finally:
            for stream in streams.values():
                stream.close()

    def _write_observation_image(self, *, sequence: int, base_step: int, rgb: Any) -> None:
        """Encode and archive one policy-ready observation PNG.

        Runs on the background writer thread: PNG compression and the
        artifact-directory write must never stall the control loop.
        """
        from PIL import Image

        image = Image.fromarray(rgb, mode="RGB")
        image.save(
            self.artifact_dir
            / f"observation_{int(sequence):03d}_step{int(base_step):03d}.png"
        )

    def _append(self, filename: str, record: Dict[str, Any]) -> None:
        if self._finished:
            raise RuntimeError("evidence writer is already finished")
        line = json.dumps(record, sort_keys=True, separators=(",", ":"))
        self._line_queue.put_nowait(("line", filename, line))

    def record_policy(self, record: Dict[str, Any]) -> None:
        self._append("policy_events.jsonl", record)

    def record_buffer(self, record: Dict[str, Any]) -> None:
        self._append("buffer_events.jsonl", record)

    def record_command(self, record: Dict[str, Any]) -> None:
        self._append("published_commands.jsonl", record)

    def write_environment(self, environment: Dict[str, Any]) -> None:
        (self.artifact_dir / "environment.json").write_text(
            json.dumps(environment, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def warm_image_pipeline(self) -> None:
        """Pay the first PNG's fixed costs before any timing gate is active.

        The first observation PNG otherwise pays a one-time PIL import plus
        the artifact directory's first filesystem entry; both hold the GIL
        long enough to starve ROS state callbacks past the 100 ms freshness
        gate (measured in flight 20260822-231032).  Call this synchronously
        before arming so the cost lands where no gate is running.
        """
        import numpy as np
        from PIL import Image

        rgb = np.zeros((8, 8, 3), dtype=np.uint8)
        Image.fromarray(rgb, mode="RGB").save(
            self.artifact_dir / "observation_pipeline_warmup.png"
        )

    def save_observation_image(self, *, sequence: int, base_step: int, rgb: Any) -> None:
        """Queue one observation PNG for the background writer thread."""
        if self._finished:
            raise RuntimeError("evidence writer is already finished")
        self._line_queue.put_nowait(("image", int(sequence), int(base_step), rgb))

    def record_state(
        self,
        *,
        step: int,
        monotonic_ns: int,
        position: Iterable[float],
        velocity: Iterable[float],
        yaw: float,
        yaw_rate: float,
    ) -> None:
        px, py, pz = (float(value) for value in position)
        vx, vy, vz = (float(value) for value in velocity)
        self._state_rows.append(
            {
                "step": int(step),
                "monotonic_ns": int(monotonic_ns),
                "x": px,
                "y": py,
                "z": pz,
                "yaw": float(yaw),
                "vx": vx,
                "vy": vy,
                "vz": vz,
                "yaw_rate": float(yaw_rate),
            }
        )

    def _write_states(self) -> None:
        fields = [
            "step",
            "monotonic_ns",
            "x",
            "y",
            "z",
            "yaw",
            "vx",
            "vy",
            "vz",
            "yaw_rate",
        ]
        with (self.artifact_dir / "uav_state.csv").open(
            "w", encoding="utf-8", newline=""
        ) as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(self._state_rows)

    def _write_plot(self) -> None:
        from PIL import Image, ImageDraw

        size = 840
        margin = 70
        x = [row["x"] for row in self._state_rows]
        y = [row["y"] for row in self._state_rows]
        image = Image.new("RGB", (size, size), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((margin, margin, size - margin, size - margin), outline="#b0bec5")
        draw.text((margin, 24), "ProSim P450 measured XY trajectory", fill="#263238")
        if x:
            min_x, max_x = min(x), max(x)
            min_y, max_y = min(y), max(y)
            span = max(max_x - min_x, max_y - min_y, 0.1)
            center_x = (min_x + max_x) / 2.0
            center_y = (min_y + max_y) / 2.0
            scale = (size - 2 * margin) / (span * 1.2)

            def point(px, py):
                return (
                    size / 2.0 + (px - center_x) * scale,
                    size / 2.0 - (py - center_y) * scale,
                )

            points = [point(px, py) for px, py in zip(x, y)]
            if len(points) > 1:
                draw.line(points, fill="#1565c0", width=4)
            for location, color in ((points[0], "#2e7d32"), (points[-1], "#c62828")):
                px, py = location
                draw.ellipse((px - 7, py - 7, px + 7, py + 7), fill=color)
        image.save(self.artifact_dir / "trajectory.png")

    def finish(self, summary: Dict[str, Any]) -> None:
        if self._finished:
            raise RuntimeError("evidence writer is already finished")
        self._finished = True
        self._line_queue.put(None)
        self._worker.join()
        if self._writer_error is not None:
            raise RuntimeError("background evidence write failed") from self._writer_error
        self._write_states()
        self._write_plot()
        (self.artifact_dir / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
