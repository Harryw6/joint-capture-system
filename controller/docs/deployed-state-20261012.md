# Deployment snapshot — 2026-10-12 (UTC+08:00)

This snapshot contains the installed desktop task-marker UI/API and the Unitree
teleop changes. It contains no capture data, credentials or private keys.

## Included behavior

- Continuous recording supports manual task start, stop and reset-start markers;
  the server receipt timestamp is authoritative. See `task-markers.md`.
- The recording adapter now loads the original `main.Teleop` from
  Gamepad_PiPER revision `aa00c80e100d687a5f4a386b79b83ece06b3a30f` without
  `safe_controller`. See `gamepad-upstream-rollback.md`.
- Runtime compatibility retains the cuRobo backend and Zikway HID mappings.
  The inactive `safe_teleop.py` is preserved as deployed, not as an active gate.
- `remote/unitree/resume-gamepad-20261012.py` is the installed one-time
  restart adapter. Invoke with the original runner path followed by its normal
  arguments. It validates the selected CSV header and appends without replacing
  earlier rows; subsequent new segments use the original exclusive-create path.
  The restart must use the existing collection operation lock and owned PID
  helpers. It does not initialize CAN, enable, home or move the arm automatically.

## Important limitations

- Home in upstream enables **and returns to zero**. Defaults after process
  restart are 1.0x and 100%; deadzone is 0.1. LB/RB use short/long press and
  cyclic speed settings. Lower them deliberately before moving the arm.
- USB gamepad re-enumerations and a CAN adapter disconnection were observed.
  The old controller could report enabled despite failed CAN sends. Process
  restart was completed, but physical motion must be verified separately.
- Restarting during recording creates a telemetry gap and resets the process
  sequence counter. Existing data is not overwritten; this does not make the
  episode's integrity or alignment checks pass.
- All robot-side 2026-10-12 capture directories were deleted at user request
  after both recorders stopped. Desktop operation logs were retained. Historical
  manifests referring to deleted raw data are not usable datasets.
- Full-history alignment can falsely reject out-of-order SSH probes. This
  known issue is not fixed in this snapshot; live startup checks alone do not
  certify final alignment.

## Verification scope

Focused marker, UI, direct-upstream-loader and USB-disconnect tests:
54 passed, 1 skipped. JavaScript syntax and Git whitespace checks passed.
No automated Home/arm-motion test was performed. This is not a claim that every
test in the repository or every physical acquisition scenario passes.
