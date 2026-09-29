# Reliable stop and warm capture sessions

Goal: recover stopped processes without losing raw files; keep Unitree cameras and Home-gated gamepad initialized between episodes.

Architecture: retain P450 sensor stack (already separate from rosbag). Add a local Unix-socket camera service that owns CameraReader instances and starts/stops per-episode recording threads. Keep gamepad control alive independently and switch its CSV destination using an acknowledged segment marker. Lightweight Go2 CSV bridge and read-only Piper logger remain per-episode to preserve their existing complete raw formats. Do not change arm motion or enable behavior.

- [x] Verify current orphan markers against process identity, deploy conservative recovery and reconcile with normal joint stop.
- [x] Add shared process identity and segment logging helpers; test exited, zombie and reused PID handling and two sequential segments.
- [x] Refactor recorder to accept resident cameras and external stop event, always finalize status on errors.
- [x] Implement resident camera service with serialized start/stop, bounded waits, freshness checks, exclusive lock and safe path validation.
- [x] Add session-mode gamepad CSV switching with flush acknowledgement, keeping the same controller and Home gate.
- [x] Update collection_ctl prepare/start/stop/status; retain legacy cleanup, preserve evidence, reject foreign processes and repeated overwrites.
- [x] Wire initialization to the warm session command; surface preparation state and meaningful errors.
- [x] Verify with fake hardware two consecutive recordings and failed-stop retry, then back up and deploy. Validate real camera readiness and repeated segmentation when hardware is available; no automatic arm enabling.

Evidence and open quality issues: ../.. /2026-09-26-warm-session-verification.md (under docs). Two real segments completed without restarting camera/gamepad. PNG queue drops and second-episode clock-model inconsistency remain explicitly unaccepted; do not claim the datasets passed full validation.

Execution: inline in the existing dirty workspace, touching only scoped files. Save deployed originals separately; do not commit unrelated user work.
