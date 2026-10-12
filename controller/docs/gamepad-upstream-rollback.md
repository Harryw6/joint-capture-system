# Gamepad_PiPER runtime

The collection adapter loads `main.Teleop` directly from
[`kehuanjack/Gamepad_PiPER`](https://github.com/kehuanjack/Gamepad_PiPER)
at revision `aa00c80e100d687a5f4a386b79b83ece06b3a30f`.
It no longer uses the custom `safe_teleop.safe_controller` wrapper.
Original per-update integration, button actions, mode switches, 0.1 deadzone,
1.0 speed factor and 100% movement-speed default are restored. The loop waits
5 ms after each update, as in upstream; CSV/snapshot work adds execution time.

Two runtime compatibility settings are retained: the README-supported cuRobo
backend (the installed environment is Python 3.8), and Zikway HID axis/button
indices. These are not motion-behavior modifications. Generic upstream Linux
indices do not match this controller's physical axes.

The adapter retains collection CSV/snapshot output and suppresses outgoing
motion commands when USB gamepad input is unavailable. SDK joint/gripper
limits remain enabled. Existing capture data is not changed.

Startup does not enable the arm. **Home enables and returns the arm to zero**
using upstream behavior; keep the arm's motion area clear. A second Home
returns to zero and disables. Start and Back retain upstream mode switches.
Button actions can block the loop, and EnablePiper has no upstream timeout.
These upstream limitations are not covered by the rollback verification.

Reload is done only while no recording is selected, using the owned process
identity and collection operation lock. Backups are stored under
`/home/unitree/heterovla-collection/backups/teleop-upstream-<timestamp>/`.
