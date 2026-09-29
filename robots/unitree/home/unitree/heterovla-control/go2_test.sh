#!/usr/bin/env bash
# One-shot Go2 motion test: stand -> move 0.5 m/s for 2 s -> stop -> down.
# Run this in YOUR OWN terminal; watch the dog and keep the e-stop ready.
set -euo pipefail

cd ~/heterovla-control
clean_env=(env -i HOME="$HOME" PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")

BRIDGE_PID=""
cleanup() {
  echo ">> cleanup: stopping bridge"
  if [[ -n "$BRIDGE_PID" ]]; then
    kill -TERM "$BRIDGE_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "[1/4] standing up"
"${clean_env[@]}" ./go2_command_executor eth0 stand_up
sleep 3

echo "[2/4] starting velocity bridge"
rm -f go2.sock go2.log
"${clean_env[@]}" ./go2_velocity_bridge eth0 go2.sock > go2.log 2>&1 &
BRIDGE_PID=$!
sleep 2
grep -q ready go2.log || { echo "bridge failed:"; cat go2.log; exit 1; }

echo "[3/4] moving forward 0.5 m/s for 2 s"
python3 go2_send.py --vx 0.5 --seconds 2
sleep 3

echo "[4/4] stopping bridge, lying down"
kill -TERM "$BRIDGE_PID" 2>/dev/null || true
BRIDGE_PID=""
sleep 1
"${clean_env[@]}" ./go2_command_executor eth0 stand_down

echo "done. bridge log:"
tail -5 go2.log
