#!/usr/bin/env python3
"""Persistent hold daemon: keeps Piper holding one pose at 200 Hz.

The arm goes limp when no client streams ModeCtrl/JointCtrl (STANDBY sag,
or EmergencyStop disables motors).  This daemon streams the last commanded
pose so the arm stays enabled and holding after the policy streamer exits.

To release: support the arm first, then SIGTERM the daemon, then disable via
robotctl (piper.disable).
"""

import argparse
import logging
import signal
import sys
import time

from piper_sdk import C_PiperInterface_V2


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pose", required=True,
                        help="6 joint targets in 0.001 deg, space separated")
    parser.add_argument("--gripper", type=int, default=0,
                        help="gripper target in 0.001 mm")
    parser.add_argument("--can", default="can0")
    return parser.parse_args()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    joints_ctl = [int(v) for v in args.pose.split()]
    if len(joints_ctl) != 6:
        raise ValueError("--pose must have 6 values")

    piper = C_PiperInterface_V2(args.can, start_sdk_joint_limit=True,
                                start_sdk_gripper_limit=True)
    piper.ConnectPort()
    time.sleep(0.5)
    if not piper.isOk():
        raise RuntimeError("Piper CAN receive thread is not healthy")
    piper.EmergencyStop(0x02)
    time.sleep(0.1)
    while not piper.EnablePiper():
        time.sleep(0.01)

    def release(signum, frame):
        logging.warning("signal %d: exiting; arm stays enabled but will sag - support it, "
                        "then disable via robotctl", signum)
        sys.exit(0)

    signal.signal(signal.SIGTERM, release)
    signal.signal(signal.SIGINT, release)

    logging.info("holding pose %s gripper=%d (SIGTERM to release)", joints_ctl, args.gripper)
    last_log = time.monotonic()
    while True:
        piper.ModeCtrl(0x01, 0x01, 100, 0x00)
        piper.JointCtrl(*joints_ctl)
        piper.GripperCtrl(args.gripper, 3000, 0x01, 0)
        now = time.monotonic()
        if now - last_log > 10:
            logging.info("still holding pose %s", joints_ctl)
            last_log = now
        time.sleep(0.005)


if __name__ == "__main__":
    main()
