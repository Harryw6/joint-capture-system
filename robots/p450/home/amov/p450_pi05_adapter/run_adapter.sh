#!/bin/bash
# Vehicle-side launcher for the p450_pi05_bridge official adapter (Stage B bench).
# Usage:
#   ./run_adapter.sh dry      # B4.1: one-line JSON, no ROS init, no output
#   ./run_adapter.sh live     # B4.2: subscribes state, registers the five
#                            # /p450_real_guard/* services; still no arming,
#                            # no auto-authorize, no policy connection.
#   ./run_adapter.sh session --policy-endpoint ws://<ground-ip>:8000 [opts...]
#                            # #19: live node + policy warmup, then BLOCKS
#                            # until the operator runs authorize + takeoff via
#                            # /p450_real_guard/* in another terminal; then
#                            # executes bounded action steps. This launcher
#                            # never arms, takes off, or lands by itself.
_cli_args=("$@")
set --
set +u
source /opt/ros/noetic/setup.bash
source /home/amov/p450_experiment/devel/setup.bash
set -u
set -- "${_cli_args[@]}"
ADAPTER=/home/amov/p450_pi05_adapter
export PYTHONPATH="$ADAPTER/src:$PYTHONPATH"
cd "$ADAPTER" || exit 1
case "${1:-dry}" in
  dry)     exec python3 -m p450_stream.real_official_node --dry-check ;;
  live)    exec python3 -m p450_stream.real_official_node --enable-command-output ;;
  session) shift
           # session mode only: vendored openpi_client/websockets/msgpack (py3.8 x86_64)
           export PYTHONPATH="$ADAPTER/vendor:$PYTHONPATH"
           exec python3 -m p450_stream.real_official_session "$@" ;;
  *) echo "usage: run_adapter.sh [dry|live|session --policy-endpoint ws://ip:port ...]" >&2; exit 2 ;;
esac
