"""Compose the tested Pi05 stream with the official-command supervisor.

The runner owns policy metadata, observations, asynchronous inference, the
step-indexed mailbox, monotonic deadlines, and evidence.  It deliberately has
no aircraft lifecycle operation: policy warmup completes before an external
operator changes the supervisor to ``ACTIVE``.  A warmup failure therefore
stays local and makes zero supervisor calls; after ``run`` enters the active
boundary, ordinary failures request one best-effort official HOLD.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import queue
import time
from typing import Any, Callable, Mapping
from uuid import uuid4

from p450_stream.evidence import EvidenceWriter
from p450_stream.inference_worker import InferenceResult, InferenceWorker
from p450_stream.kinematics import Pose
from p450_stream.loopback import make_observation
from p450_stream.protocol import validate_server_metadata
from p450_stream.runtime import MonotonicStepClock
from p450_stream.safety import ActionLimits, ActionRejected
from p450_stream.scheduler import ActionBuffer


MAX_EXECUTE_STEPS = 600  # Adapter safety cap: 60 s at the fixed 10 Hz rate.
MAX_SLEEP_S = 0.1
SAFE_TERMINAL_PHASES = {
    "HOLD",
    "RC_TAKEOVER",
    "LOCALIZATION_FAULT",
    "LANDING",
}


@dataclass(frozen=True)
class RunnerOutcome:
    exit_code: int
    state: str
    reason: str | None
    executed_steps: int


@dataclass(frozen=True)
class _PendingRequest:
    request_seq: int
    base_step: int
    requested_at: float
    client_send_monotonic_ns: int


class OfficialPi05Runner:
    """A pure-Python, single-flight adapter to ``OfficialRealSupervisor``."""

    def __init__(
        self,
        *,
        policy: Any,
        supervisor: Any,
        artifact_dir: str | Path,
        evidence: Any | None = None,
        session_id: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        now_ns: Callable[[], int] = time.monotonic_ns,
        sleep: Callable[[float], None] = time.sleep,
        worker_factory: Callable[..., Any] = InferenceWorker,
        buffer_factory: Callable[..., Any] = ActionBuffer,
        step_clock: MonotonicStepClock | None = None,
        inference_timeout_s: float = 2.0,
    ) -> None:
        if isinstance(inference_timeout_s, bool):
            raise ValueError("inference_timeout_s must be positive")
        try:
            checked_timeout = float(inference_timeout_s)
        except (TypeError, ValueError) as error:
            raise ValueError("inference_timeout_s must be positive") from error
        if not math.isfinite(checked_timeout) or checked_timeout <= 0.0:
            raise ValueError("inference_timeout_s must be positive")
        if session_id is not None and (
            not isinstance(session_id, str) or not session_id.strip()
        ):
            raise ValueError("session_id must be non-empty")
        try:
            action_limits = supervisor.action_limits
        except AttributeError as error:
            raise TypeError("supervisor_action_limits") from error
        if not isinstance(action_limits, ActionLimits):
            raise TypeError("supervisor_action_limits")
        self.policy = policy
        self.supervisor = supervisor
        self.evidence = (
            evidence if evidence is not None else EvidenceWriter(Path(artifact_dir))
        )
        self.session_id = session_id or str(uuid4())
        self._raw_clock = clock
        self._raw_now_ns = now_ns
        self._sleep = sleep
        self._last_clock_s: float | None = None
        self._last_now_ns: int | None = None
        self._clock = self._sample_clock
        self._now_ns = self._sample_now_ns
        self._worker_factory = worker_factory
        self._inference_timeout_s = checked_timeout
        self._worker: Any | None = None
        self._limits = action_limits
        self._buffer = buffer_factory(
            max_age_s=0.5,
            clock=clock,
            mode="real",
            limits=self._limits,
        )
        self._step_clock = step_clock or MonotonicStepClock(
            period_ns=100_000_000,
            max_lateness_ns=50_000_000,
        )
        self._pending: _PendingRequest | None = None
        self._next_seq = 0
        self._step = 0
        self._delivery_seq = -1
        self._next_prefetch_step = 0
        self._hold_attempted = False
        self._finished = False
        self.cleanup_failures: list[str] = []
        self.state = "NEW"
        self.last_error: str | None = None

    def _sample_clock(self) -> float:
        value = self._raw_clock()
        if type(value) is not float or not math.isfinite(value):
            raise RuntimeError("clock_invalid")
        if self._last_clock_s is not None and value < self._last_clock_s:
            raise RuntimeError("clock_rollback")
        self._last_clock_s = value
        return value

    def _sample_now_ns(self) -> int:
        value = self._raw_now_ns()
        if type(value) is not int or value < 0:
            raise RuntimeError("now_ns_invalid")
        if self._last_now_ns is not None and value < self._last_now_ns:
            raise RuntimeError("now_ns_rollback")
        self._last_now_ns = value
        return value

    def _bounded_sleep(self, seconds: float) -> None:
        try:
            requested = float(seconds)
        except (TypeError, ValueError) as error:
            raise RuntimeError("sleep_invalid") from error
        if not math.isfinite(requested):
            raise RuntimeError("sleep_invalid")
        self._sleep(min(MAX_SLEEP_S, max(0.0, requested)))

    def _outcome(self, exit_code: int, reason: str | None = None) -> RunnerOutcome:
        return RunnerOutcome(exit_code, self.state, reason, self._step)

    @staticmethod
    def _snapshot_observation(
        snapshot: Any,
        *,
        session_id: str,
        request_seq: int,
        base_step: int,
        sent_ns: int,
    ) -> dict[str, Any]:
        return make_observation(
            session_id=session_id,
            request_seq=request_seq,
            base_step=base_step,
            pose=Pose(
                x=snapshot.position[0],
                y=snapshot.position[1],
                z=snapshot.position[2],
                yaw=snapshot.attitude[2],
            ),
            velocity=snapshot.velocity,
            yaw_rate=snapshot.attitude_rate[2],
            state_stamp_ns=snapshot.state_received_monotonic_ns,
            client_send_monotonic_ns=sent_ns,
        )

    def _record(self, channel: str, record: dict[str, Any]) -> None:
        try:
            getattr(self.evidence, f"record_{channel}")(record)
        except Exception as error:
            raise RuntimeError("evidence_error") from error

    @staticmethod
    def _same_protocol_value(value: Any, expected: Any) -> bool:
        return type(value) is type(expected) and value == expected

    @staticmethod
    def _buffer_rejection_reason(reason: str) -> str:
        if reason == "session":
            return "session_mismatch"
        if reason == "sequence":
            return "request_sequence_mismatch"
        if reason in {"base_step", "future_base_step"}:
            return "response_base_step_mismatch"
        if reason == "request_expired":
            return "policy_response_stale"
        return f"action_response_rejected:{reason}"

    def _finish(self, *, passed: bool, reason: str | None) -> None:
        if self._finished:
            return
        self._finished = True
        self.evidence.finish(
            {
                "passed": bool(passed),
                "state": self.state,
                "failure_reason": reason,
                "session_id": self.session_id,
                "executed_steps": self._step,
                "cleanup_failures": list(self.cleanup_failures),
            }
        )

    def _close_worker(self) -> bool:
        if self._worker is None:
            return True
        worker = self._worker
        try:
            closed = bool(worker.close())
        except BaseException:
            return False
        if closed:
            self._worker = None
        return closed

    def _invalidate_actions(self) -> None:
        self._buffer.begin_session(f"invalidated-{self.session_id}-{self._step}")
        self._pending = None

    def _note_cleanup_failure(self, reason: str) -> None:
        if reason not in self.cleanup_failures:
            self.cleanup_failures.append(reason)
        try:
            self._record(
                "buffer",
                {"type": "cleanup_failure", "reason": reason},
            )
        except BaseException:
            pass

    def _finish_fault(
        self,
        reason: str,
        *,
        event_type: str,
        request_hold: bool,
        close_worker: bool = True,
        primary_interrupt: BaseException | None = None,
    ) -> RunnerOutcome:
        self.last_error = reason
        self.state = "FAULT"
        hold_interrupt = None
        try:
            self._invalidate_actions()
        except BaseException:
            self._note_cleanup_failure("buffer_invalidation_failed")
        if request_hold:
            try:
                held = self._best_effort_hold(reason)
            except BaseException as error:
                hold_interrupt = error
                self._note_cleanup_failure(
                    f"hold_interrupted:{type(error).__name__}"
                )
            else:
                if not held:
                    self._note_cleanup_failure("hold_failed")
        try:
            timestamp = self._now_ns()
        except BaseException:
            timestamp = None
            self._note_cleanup_failure("evidence_clock_failed")
        try:
            self._record(
                "buffer",
                {
                    "type": event_type,
                    "reason": reason,
                    "step": self._step,
                    "monotonic_ns": timestamp,
                },
            )
        except BaseException:
            self._note_cleanup_failure("evidence_record_failed")
        if close_worker and not self._close_worker():
            self._note_cleanup_failure("worker_close_failed")
        try:
            self._finish(passed=False, reason=reason)
        except BaseException:
            self._note_cleanup_failure("evidence_finish_failed")
        if hold_interrupt is not None and primary_interrupt is None:
            raise hold_interrupt
        return self._outcome(1, reason)

    def _local_fault(self, reason: str, *, event_type: str) -> RunnerOutcome:
        return self._finish_fault(
            reason, event_type=event_type, request_hold=False
        )

    def _retry_fault_cleanup(self) -> RunnerOutcome:
        if self._worker is not None and not self._close_worker():
            if "worker_close_failed" not in self.cleanup_failures:
                self.cleanup_failures.append("worker_close_failed")
        return self._outcome(1, self.last_error)

    def _stable_existing_outcome(self) -> RunnerOutcome | None:
        if self.state == "COMPLETE":
            return self._outcome(0)
        if self.state == "FAULT":
            return self._retry_fault_cleanup()
        return None

    def _warmup_fault(self, reason: str) -> RunnerOutcome:
        return self._local_fault(reason, event_type="warmup_fault")

    def _best_effort_hold(self, reason: str) -> bool:
        if self._hold_attempted:
            try:
                return self.supervisor.phase in SAFE_TERMINAL_PHASES
            except Exception:
                return False
        self._hold_attempted = True
        try:
            self.supervisor.hold(reason)
        except Exception:
            return False
        try:
            return self.supervisor.phase in SAFE_TERMINAL_PHASES
        except Exception:
            return False

    def _runtime_fault(
        self,
        reason: str,
        *,
        request_hold: bool = True,
        primary_interrupt: BaseException | None = None,
    ) -> RunnerOutcome:
        return self._finish_fault(
            reason,
            event_type="hold" if request_hold else "official_stop",
            request_hold=request_hold,
            primary_interrupt=primary_interrupt,
        )

    def abort(self, reason: str = "operator_abort") -> RunnerOutcome:
        """Stop an idle or active runner without invoking aircraft lifecycle APIs."""
        existing = self._stable_existing_outcome()
        if existing is not None:
            return existing
        active = getattr(self.supervisor, "phase", None) == "ACTIVE"
        if active:
            return self._runtime_fault(reason)
        return self._local_fault(reason, event_type="operator_abort")

    def warmup(self) -> RunnerOutcome:
        """Validate and exercise the policy without calling the supervisor."""
        existing = self._stable_existing_outcome()
        if existing is not None:
            return existing
        if self.state == "READY_FOR_EXTERNAL_AUTHORIZATION":
            return self._outcome(0)
        if self.state != "NEW":
            return self._warmup_fault("warmup_state")
        try:
            self.evidence.write_environment(
                {
                    "physical_vehicle": True,
                    "command_owner": "OfficialRealSupervisor",
                    "control_dt_s": 0.1,
                    "action_repr": "FLU_body_delta_pose",
                    "policy_endpoint": "injected",
                }
            )
        except Exception:
            return self._warmup_fault("evidence_error")
        try:
            validate_server_metadata(self.policy.get_server_metadata())
        except Exception:
            return self._warmup_fault("metadata_mismatch")

        try:
            self._worker = self._worker_factory(self.policy.infer, clock=self._clock)
            warmup_session = str(uuid4())
            snapshot = self.supervisor.backend.snapshot()
            sent_ns = self._now_ns()
            observation = self._snapshot_observation(
                snapshot,
                session_id=warmup_session,
                request_seq=0,
                base_step=0,
                sent_ns=sent_ns,
            )
        except Exception:
            return self._warmup_fault("observation_error")

        try:
            self._record(
                "policy",
                {
                    "type": "warmup_request",
                    "session_id": warmup_session,
                    "request_seq": 0,
                    "base_step": 0,
                    "client_send_monotonic_ns": sent_ns,
                },
            )
            if not self._worker.submit(observation):
                return self._warmup_fault("policy_warmup_rejected")
            try:
                result = self._worker.wait_for_result(
                    timeout_s=self._inference_timeout_s
                )
            except queue.Empty:
                return self._warmup_fault("policy_warmup_timeout")
            if result.error is not None:
                return self._warmup_fault("policy_warmup_failed")
            response = result.response
            if not isinstance(response, Mapping):
                return self._warmup_fault("policy_warmup_invalid_response")
            # Reuse ActionBuffer for the warmup response's complete protocol
            # and safety validation, but skip the altitude envelope: warmup
            # always happens on the ground before takeoff, and the envelope is
            # re-checked with live altitude when real actions are accepted.
            self._buffer.begin_session(warmup_session)
            self._buffer.accept(
                session_id=response.get("session_id"),
                seq=response.get("request_seq"),
                base_step=response.get("base_step"),
                current_step=0,
                requested_at=self._clock(),
                actions=response.get("actions"),
                check_envelope=False,
            )
            if not (
                self._same_protocol_value(response.get("session_id"), warmup_session)
                and self._same_protocol_value(response.get("request_seq"), 0)
                and self._same_protocol_value(response.get("base_step"), 0)
            ):
                return self._warmup_fault("policy_warmup_response_mismatch")
            self._record(
                "policy",
                {
                    "type": "warmup_response",
                    "session_id": warmup_session,
                    "request_seq": 0,
                    "received_monotonic_ns": int(result.received_at * 1_000_000_000),
                },
            )
        except Exception as error:
            reason = (
                "evidence_error"
                if str(error) == "evidence_error"
                else "policy_warmup_invalid_response"
            )
            return self._warmup_fault(reason)

        self._buffer.begin_session(self.session_id)
        self.state = "READY_FOR_EXTERNAL_AUTHORIZATION"
        return self._outcome(0)

    def _submit(self, base_step: int) -> None:
        if self._pending is not None:
            raise RuntimeError("inference_single_flight_rejected")
        snapshot = self.supervisor.backend.snapshot()
        request_seq = self._next_seq
        requested_at = self._clock()
        sent_ns = self._now_ns()
        observation = self._snapshot_observation(
            snapshot,
            session_id=self.session_id,
            request_seq=request_seq,
            base_step=base_step,
            sent_ns=sent_ns,
        )
        if not self._worker.submit(observation):
            raise RuntimeError("inference_single_flight_rejected")
        self._pending = _PendingRequest(
            request_seq=request_seq,
            base_step=base_step,
            requested_at=requested_at,
            client_send_monotonic_ns=sent_ns,
        )
        self._record(
            "policy",
            {
                "type": "request",
                "session_id": self.session_id,
                "request_seq": request_seq,
                "base_step": base_step,
                "client_send_monotonic_ns": sent_ns,
            },
        )
        self._next_seq += 1

    def _accept(self, result: InferenceResult) -> None:
        if result.error is not None:
            raise RuntimeError("policy_inference_failed")
        if self._pending is None or not isinstance(result.response, Mapping):
            raise RuntimeError("unexpected_policy_response")
        pending = self._pending
        response = result.response
        snapshot = self.supervisor.backend.snapshot()
        try:
            delivery = self._buffer.accept(
                session_id=response.get("session_id"),
                seq=response.get("request_seq"),
                base_step=response.get("base_step"),
                current_step=self._step,
                requested_at=pending.requested_at,
                actions=response.get("actions"),
                current_altitude_m=snapshot.position[2],
            )
        except ActionRejected as error:
            raise RuntimeError(self._buffer_rejection_reason(error.reason)) from error
        if not self._same_protocol_value(
            response.get("session_id"), self.session_id
        ):
            raise RuntimeError("session_mismatch")
        if not self._same_protocol_value(
            response.get("request_seq"), pending.request_seq
        ):
            raise RuntimeError("request_sequence_mismatch")
        if not self._same_protocol_value(response.get("base_step"), pending.base_step):
            raise RuntimeError("response_base_step_mismatch")
        self._delivery_seq = pending.request_seq
        self._pending = None
        self._record(
            "policy",
            {
                "type": "response",
                "session_id": self.session_id,
                "request_seq": delivery.seq,
                "base_step": delivery.base_step,
                "accepted_at_step": self._step,
                "received_monotonic_ns": int(result.received_at * 1_000_000_000),
                "response_age_ns": int(
                    (result.received_at - pending.requested_at) * 1_000_000_000
                ),
            },
        )
        self._record(
            "buffer",
            {
                "type": "admit",
                "request_seq": delivery.seq,
                "base_step": delivery.base_step,
                "first_step": delivery.first_step,
                "trimmed_steps": delivery.trimmed_steps,
                "action_count": delivery.action_count,
            },
        )

    @staticmethod
    def _exception_reason(error: BaseException) -> str:
        reason = str(error)
        stable = {
            "evidence_error",
            "inference_single_flight_rejected",
            "policy_inference_failed",
            "policy_inference_timeout",
            "unexpected_policy_response",
            "session_mismatch",
            "request_sequence_mismatch",
            "response_base_step_mismatch",
            "policy_response_stale",
            "mailbox_hold",
            "executor_late",
            "supervisor_not_active",
            "clock_invalid",
            "clock_rollback",
            "now_ns_invalid",
            "now_ns_rollback",
            "sleep_invalid",
        }
        if isinstance(error, ActionRejected):
            controlled = {
                "bad_mode",
                "bad_shape",
                "non_numeric",
                "non_finite",
                "step_xy_limit",
                "step_z_limit",
                "step_yaw_limit",
                "chunk_xy_limit",
                "chunk_z_limit",
                "chunk_yaw_limit",
                "current_altitude_required",
                "current_altitude_non_finite",
                "altitude_envelope",
            }
            suffix = error.reason if error.reason in controlled else "rejected"
            return f"action_rejected:{suffix}"
        if reason in stable or reason.startswith("action_response_rejected:"):
            return reason
        return "runtime_exception"

    def run(self, *, execute_steps: int, prefetch_steps: int = 3) -> RunnerOutcome:
        """Execute bounded action steps only after external activation."""
        existing = self._stable_existing_outcome()
        if existing is not None:
            return existing
        if self.state != "READY_FOR_EXTERNAL_AUTHORIZATION":
            return self._local_fault("runner_not_warmed", event_type="local_fault")
        valid_configuration = (
            type(execute_steps) is int
            and 1 <= execute_steps <= MAX_EXECUTE_STEPS
            and type(prefetch_steps) is int
            and 1 <= prefetch_steps <= 9
        )
        if getattr(self.supervisor, "phase", None) != "ACTIVE":
            return self._local_fault(
                "supervisor_not_active", event_type="precondition_rejected"
            )
        if not valid_configuration:
            return self._runtime_fault("invalid_run_configuration")

        try:
            status = self.supervisor.refresh(self._now_ns())
            if status != "active" or self.supervisor.phase != "ACTIVE":
                return self._runtime_fault(str(status), request_hold=False)
            self.state = "RUNNING"
            self._next_prefetch_step = prefetch_steps
            self._submit(self._step)
            while self._pending is not None:
                request_age_s = self._clock() - self._pending.requested_at
                if request_age_s > 0.5:
                    return self._runtime_fault("policy_response_stale")
                if request_age_s > self._inference_timeout_s:
                    return self._runtime_fault("policy_inference_timeout")

                status = self.supervisor.watchdog(self._now_ns())
                if status != "active" or self.supervisor.phase != "ACTIVE":
                    return self._runtime_fault(str(status), request_hold=False)
                initial = self._worker.poll()
                status = self.supervisor.watchdog(self._now_ns())
                if status != "active" or self.supervisor.phase != "ACTIVE":
                    return self._runtime_fault(str(status), request_hold=False)
                if initial is not None:
                    self._accept(initial)
                    break

                next_limit_s = min(0.5, self._inference_timeout_s)
                remaining_s = next_limit_s - request_age_s
                self._bounded_sleep(
                    min(MAX_SLEEP_S, max(0.000001, remaining_s + 0.000001))
                )
            self._step_clock.reset(start_ns=self._now_ns(), start_step=self._step)

            while self._step < execute_steps:
                status = self.supervisor.watchdog(self._now_ns())
                if status != "active" or self.supervisor.phase != "ACTIVE":
                    return self._runtime_fault(str(status), request_hold=False)

                completed = self._worker.poll()
                if completed is not None:
                    self._accept(completed)

                decision = self._step_clock.check(now_ns=self._now_ns())
                if not decision.due:
                    self._bounded_sleep(
                        max(0.0, -decision.lateness_ns / 1_000_000_000)
                    )
                    continue
                if not decision.execute:
                    raise RuntimeError("executor_late")

                action = self._buffer.next_action(step=self._step)
                if self._buffer.state == "HOLD":
                    raise RuntimeError("mailbox_hold")
                result = self.supervisor.execute_action(
                    action,
                    self._delivery_seq,
                    self._step,
                    self._now_ns(),
                )
                if result != "executed" or self.supervisor.phase != "ACTIVE":
                    return self._runtime_fault(str(result), request_hold=False)
                self._record(
                    "command",
                    {
                        "type": "executed_step",
                        "session_id": self.session_id,
                        "request_seq": self._delivery_seq,
                        "action_step": self._step,
                        "scheduled_monotonic_ns": decision.deadline_ns,
                        "executed_monotonic_ns": self._now_ns(),
                        "lateness_ns": decision.lateness_ns,
                        "action": [float(value) for value in action],
                    },
                )
                self._step += 1
                if (
                    self._step < execute_steps
                    and self._step >= self._next_prefetch_step
                    and self._pending is None
                ):
                    self._submit(self._step)
                    self._next_prefetch_step = self._step + prefetch_steps
        except ActionRejected as error:
            return self._runtime_fault(self._exception_reason(error))
        except Exception as error:
            return self._runtime_fault(self._exception_reason(error))
        except BaseException as error:
            self._runtime_fault(
                f"interrupted:{type(error).__name__}",
                primary_interrupt=error,
            )
            raise

        self._invalidate_actions()
        try:
            held = self._best_effort_hold("policy_complete")
        except BaseException as error:
            self._finish_fault(
                f"interrupted:{type(error).__name__}",
                event_type="hold_interrupted",
                request_hold=False,
                primary_interrupt=error,
            )
            raise
        if not held:
            return self._runtime_fault("completion_hold_failed", request_hold=False)
        if self.supervisor.phase not in SAFE_TERMINAL_PHASES:
            return self._runtime_fault("completion_hold_failed", request_hold=False)
        try:
            self._record(
                "buffer",
                {
                    "type": "hold",
                    "reason": "policy_complete",
                    "step": self._step,
                    "monotonic_ns": self._now_ns(),
                },
            )
            if not self._close_worker():
                self._note_cleanup_failure("worker_close_failed")
                return self._finish_fault(
                    "inference_worker_close_timeout",
                    event_type="cleanup_failure",
                    request_hold=False,
                    close_worker=False,
                )
            self.state = "COMPLETE"
            self._finish(passed=True, reason=None)
        except Exception:
            return self._runtime_fault("evidence_error")
        return self._outcome(0)
