"""Operator-facing orchestration of one real-vehicle Pi05 streaming session.

This module glues together the pieces that already exist and are tested in
isolation: :mod:`p450_stream.real_official_node` (ROS node, guard services,
watchdog) and :class:`p450_stream.real_official_runner.OfficialPi05Runner`
(warmup + bounded action streaming).  It deliberately keeps the safety
boundary of that stack:

* the process itself never calls ``authorize``/``takeoff``/``land`` — the
  operator drives those through the ``/p450_real_guard/*`` services from a
  separate terminal, exactly as in the bench card's B4 flow;
* streaming only starts once the supervisor reaches ``ACTIVE`` on its own
  (i.e. the operator authorized *and* the official takeoff settled);
* any terminal official state, a shutdown request, or an authorization
  timeout aborts the runner so the session still leaves evidence.

It is still not a "one command flies the aircraft" tool: warmup completes
first, then the CLI blocks waiting for the external authorization.
"""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from p450_stream.real_official_node import OfficialRealNode, _create_live_facade
from p450_stream.real_official_runner import (
    MAX_EXECUTE_STEPS,
    OfficialPi05Runner,
    RunnerOutcome,
)


POLL_S = 0.1
MAX_PREFETCH_STEPS = 9  # Mirrors the runner's hard cap; argparse re-checks it.
_TERMINAL_PHASES = frozenset({"RC_TAKEOVER", "LOCALIZATION_FAULT", "LANDING"})

OPERATOR_INSTRUCTIONS = """operator actions required in another terminal:
  rosservice call /p450_real_guard/status     # readiness before authorizing
  rosservice call /p450_real_guard/authorize
  rosservice call /p450_real_guard/takeoff
streaming starts automatically once the supervisor reports ACTIVE."""


def run_session(
    *,
    runner: OfficialPi05Runner,
    supervisor: Any,
    steps: int,
    prefetch_steps: int,
    authorization_timeout_s: float,
    sleep: Callable[[float], None],
    now: Callable[[], float],
    reporter: Callable[[str], None],
    shutdown_requested: Callable[[], bool] = lambda: False,
    poll_s: float = POLL_S,
) -> tuple[int, str]:
    """Drive warmup → external authorization wait → bounded run.

    Returns the ``(exit_code, reason)`` to relay to the shell.  On any abort
    path the runner records a fault outcome, so evidence exists either way.
    """
    outcome = runner.warmup()
    if outcome.exit_code != 0:
        reason = outcome.reason or outcome.state
        reporter(f"warmup failed: {reason}")
        return outcome.exit_code, f"warmup:{reason}"

    reporter("warmup complete; policy validated, aircraft untouched")
    for line in OPERATOR_INSTRUCTIONS.splitlines():
        reporter(line)

    deadline = now() + float(authorization_timeout_s)
    while True:
        phase = supervisor.phase
        if phase == "ACTIVE":
            break
        if phase in _TERMINAL_PHASES:
            reporter(f"official state {phase} before activation; aborting")
            return _abort_outcome(runner, f"official_state:{phase}", reporter)
        if shutdown_requested():
            reporter("shutdown requested before activation; aborting")
            return _abort_outcome(runner, "operator_shutdown", reporter)
        if now() >= deadline:
            reporter("authorization timeout; aborting")
            return _abort_outcome(runner, "authorization_timeout", reporter)
        try:
            sleep(poll_s)
        except KeyboardInterrupt:
            # Ctrl+C while waiting: abort through the runner so the session
            # still records a fault outcome.  During the run phase the same
            # interrupt propagates instead and the runner's own
            # BaseException path finishes the evidence.
            reporter("shutdown requested before activation; aborting")
            return _abort_outcome(runner, "operator_shutdown", reporter)

    reporter(f"supervisor ACTIVE; streaming {steps} steps")
    outcome = runner.run(execute_steps=steps, prefetch_steps=prefetch_steps)
    reason = outcome.reason or outcome.state.lower()
    reporter(f"run finished: exit_code={outcome.exit_code} reason={reason}")
    return outcome.exit_code, reason


def _abort_outcome(
    runner: OfficialPi05Runner, reason: str, reporter: Callable[[str], None]
) -> tuple[int, str]:
    outcome: RunnerOutcome = runner.abort(reason)
    reported = outcome.reason or reason
    reporter(f"aborted: {reported}")
    return outcome.exit_code, reported


def _build_policy_client(endpoint: str) -> Any:
    # Lazy import: keeps --help and unit tests usable without openpi_client.
    from openpi_client.websocket_client_policy import WebsocketClientPolicy

    return WebsocketClientPolicy(endpoint)


def _close_policy_client(client: Any) -> None:
    """Close policy clients across versions without importing openpi here."""
    close = getattr(client, "close", None)
    if callable(close):
        close()
        return
    websocket = getattr(client, "_ws", None)
    if websocket is not None:
        websocket.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="p450-official-session",
        description=(
            "Run one official-stack Pi05 streaming session: node + warmup, "
            "then wait for the operator's external authorize/takeoff before "
            "streaming bounded actions."
        ),
    )
    parser.add_argument(
        "--policy-endpoint",
        required=True,
        help="policy server websocket, e.g. ws://<ground-pc>:8000",
    )
    parser.add_argument(
        "--steps",
        type=int,
        default=200,
        help=f"action steps to execute once ACTIVE (1-{MAX_EXECUTE_STEPS})",
    )
    parser.add_argument(
        "--prefetch-steps",
        type=int,
        default=3,
        help=f"steps of lookahead while streaming (1-{MAX_PREFETCH_STEPS})",
    )
    parser.add_argument(
        "--authorization-timeout-s",
        type=float,
        default=600.0,
        help="how long to wait for the operator's authorize+takeoff",
    )
    parser.add_argument(
        "--artifacts-root",
        default="artifacts/real_sessions",
        help="root directory for session evidence",
    )
    return parser


def _validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    # Range-check before touching ROS so misuse exits at once with code 2.
    if not 1 <= args.steps <= MAX_EXECUTE_STEPS:
        parser.error(f"--steps must be in 1-{MAX_EXECUTE_STEPS}")
    if not 1 <= args.prefetch_steps <= MAX_PREFETCH_STEPS:
        parser.error(f"--prefetch-steps must be in 1-{MAX_PREFETCH_STEPS}")
    if not args.authorization_timeout_s > 0.0:
        parser.error("--authorization-timeout-s must be positive")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate(parser, args)

    session_id = str(uuid4())
    # uuid-based name: the vehicle has no RTC, wall-clock stamps would lie.
    artifact_dir = Path(args.artifacts_root) / session_id

    ros = _create_live_facade()
    node = OfficialRealNode(ros=ros, command_output_enabled=True)
    # Same live wiring as real_official_node.main: the 10 Hz tick owns
    # watchdog/refresh, so TAKEOFF settles to ACTIVE while we block below.
    ros.schedule_periodic(0.1, node.tick)
    supervisor = node.supervisor
    policy = _build_policy_client(args.policy_endpoint)
    runner = OfficialPi05Runner(
        policy=policy,
        supervisor=supervisor,
        artifact_dir=artifact_dir,
        session_id=session_id,
        clock=lambda: ros.now_ns() / 1_000_000_000.0,
        now_ns=ros.now_ns,
        sleep=ros.sleep,
    )

    shutdown = {"requested": False, "tearing_down": False}

    def _request_shutdown(*_args: Any) -> None:
        shutdown["requested"] = True
        # Raise so a mid-run Ctrl+C actually interrupts the streaming loop;
        # during the authorization wait run_session catches and aborts.
        # While our own teardown signals ROS shutdown, stay quiet instead:
        # raising inside rospy's shutdown sequence would leave its threads
        # running and hang the process at interpreter exit.
        if shutdown["tearing_down"]:
            return
        raise KeyboardInterrupt

    ros.on_shutdown(_request_shutdown)
    previous_int = signal.signal(signal.SIGINT, _request_shutdown)
    try:
        code, reason = run_session(
            runner=runner,
            supervisor=supervisor,
            steps=args.steps,
            prefetch_steps=args.prefetch_steps,
            authorization_timeout_s=args.authorization_timeout_s,
            sleep=ros.sleep,
            now=lambda: ros.now_ns() / 1_000_000_000.0,
            reporter=print,
            shutdown_requested=lambda: shutdown["requested"],
        )
    except KeyboardInterrupt:
        # The runner's BaseException path already finished the evidence;
        # this is just the shell-facing exit for Ctrl+C during the run.
        print("session interrupted", file=sys.stderr)
        return 130
    finally:
        signal.signal(signal.SIGINT, previous_int)
        # Teardown order: mark teardown (so the ROS shutdown hook stays
        # quiet), release the node, close the policy websocket (its recv
        # thread otherwise outlives the session), then signal ROS shutdown
        # so rospy's non-daemon threads stop instead of hanging exit.
        shutdown["tearing_down"] = True
        node.shutdown()
        _close_policy_client(policy)
        ros.shutdown(f"session {session_id} finished")

    print(f"session {session_id}: exit_code={code} reason={reason}")
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
