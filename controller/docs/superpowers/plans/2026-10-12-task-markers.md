# Continuous capture task timestamps

Goal: record many operator-selected task intervals inside one uninterrupted capture,
including resetting the current start after an unsuccessful attempt.

Implementation uses three independent pieces: a durable event store, a console
service that identifies the real recording and copies annotation sidecars, and a
compact task panel. Existing recorder and teleoperation processes keep running.

- [x] Store start/stop/reset events with request deduplication, revision checks,
  exact desktop wall/monotonic timestamps, reset history and JSON/CSV exports.
- [x] Resolve current capture from fresh device status. Refuse starts/resets on
  failed/starting/stalled recordings; allow an open task to be closed with an
  explicit review flag if the recorder is no longer confirmed.
- [x] Protect POST with the existing local origin/token boundary. Bind each
  request to a capture key so a delayed click cannot mark another recording.
- [x] Copy only annotation files to the selected recording directory in a
  background worker. Persist pending/error status and clock probe samples.
- [x] Add task start/end/reset buttons, elapsed time and interval history, with
  nanoseconds handled as decimal strings in the browser.
- [x] Test failures, replay/double-clicks, reset, persistence, HTTP boundaries,
  single Unitree capture outside an old joint manifest, and browser behavior.
- [x] Deploy the console files with recoverable originals, restart only its
  Windows HTTP server, and verify the active Unitree capture remains unchanged.

Annotation location on desktop: <manifest_root>/task_markers/<capture_key>.
Robot sidecars: <actual_recording_directory>/annotations/task_markers.
Completed marker pairs describe selected time intervals; they do not certify
image/state completeness or training suitability.
