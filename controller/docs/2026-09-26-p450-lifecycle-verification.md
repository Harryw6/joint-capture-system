# P450 process lifecycle repair — 2026-09-26

## Findings and changes

- Historical camera logs explicitly report shutdown because a new node registered with the same name.
- Failed prepare previously sent SIGINT, immediately discarded ownership records, and did not verify child exit. Ownership was only saved after all launches.
- Save ownership after each launch, retain cleanup_pending on failure, track Linux boot ID/start ticks and descendants, and refuse ambiguous/untracked process cleanup.
- Match complete launch command tokens and enumerate duplicates. Serialize prepare/start/shutdown. Do not prepare while recording.
- Wait for owned parent and child exit before forgetting ownership. A real Prometheus bridge needed about 15 seconds for roslaunch shutdown; the bounded cleanup wait is now 25 seconds.
- Keep normal stop-fast sensor processes warm. Treat Linux zombie processes as exited in both orchestration and recorder status; this fixes a same-process start/stop false timeout observed during testing.
- No global pkill, raw-data deletion, video export, arm command, or clock-quality threshold change.

## Verification

- Targeted orchestrator, process-tracking and capture CLI tests: **42 passed**.
- Local controller regression suite: **257 passed**.
- Full remote-package tests could not collect on Windows because ROS rospy is absent; do not interpret targeted passing tests as full-package coverage.
- Real P450 cold prepare succeeded; a second prepare reused all four PIDs unchanged.
- Real shutdown completed in **15.66 seconds**, all four launch groups absent and ownership state removed; subsequent prepare succeeded in **14.17 seconds**.
- Short recording after final fix stopped in **0.51 seconds**, keeping all four sensor launch PIDs unchanged.
- Bag `/home/amov/p450_data/19700101_082604_diagnostic_20260926_camera_lifecycle_v2/raw/flight_0.bag`: 1,129,792 bytes, readable, 851 messages including 38 compressed color frames and 25 odometry samples.
- Final state: no active recorder, no capture marker; sensors ready. Diagnostic raw data retained. The P450 local 1970 date is unchanged; this isolated P450 test does not validate joint clock alignment.

## Deployment and limits

Deployed `orchestrator.py`, `process_tracking.py`, `manager.py` to `/home/amov/p450_recording/p450_recording/`.
Original modified files backed up at `/home/amov/p450_recording/backups/20260926-process-lifecycle/`.

Untracked legacy processes or changed process identities are deliberately not killed automatically. Live recovery requires connected/disarmed evidence. Snapshot-based descendant tracking cannot promise recovery from every abrupt failure before a child is observed. USB/depth-stream hardware problems and long-duration capture reliability are separate from this lifecycle fix.
