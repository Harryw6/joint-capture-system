from __future__ import annotations

from dataclasses import dataclass
import queue
import threading
import time
from typing import Any, Callable


@dataclass(frozen=True)
class InferenceResult:
    response: Any | None
    error: BaseException | None
    received_at: float


class InferenceWorker:
    """Single-flight inference worker that never blocks the control executor."""

    _STOP = object()

    def __init__(
        self,
        infer: Callable[[Any], Any],
        *,
        clock: Callable[[], float] = time.monotonic,
        join_timeout_s: float = 1.0,
    ) -> None:
        self._infer = infer
        self._clock = clock
        self._join_timeout_s = float(join_timeout_s)
        self._input: queue.Queue[Any] = queue.Queue(maxsize=1)
        self._output: queue.Queue[InferenceResult] = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._in_flight = False
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            request = self._input.get()
            if request is self._STOP:
                return
            try:
                response = self._infer(request)
                result = InferenceResult(response, None, self._clock())
            except BaseException as error:
                result = InferenceResult(None, error, self._clock())
            self._output.put(result)
            with self._lock:
                if self._closed:
                    return

    def submit(self, request: Any) -> bool:
        with self._lock:
            if self._closed or self._in_flight:
                return False
            self._in_flight = True
            self._input.put_nowait(request)
            return True

    def _release(self, result: InferenceResult) -> InferenceResult:
        with self._lock:
            self._in_flight = False
        return result

    def poll(self) -> InferenceResult | None:
        try:
            result = self._output.get_nowait()
        except queue.Empty:
            return None
        return self._release(result)

    def wait_for_result(self, *, timeout_s: float) -> InferenceResult:
        result = self._output.get(timeout=timeout_s)
        return self._release(result)

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def close(self) -> bool:
        with self._lock:
            first_close = not self._closed
            self._closed = True
        if first_close:
            try:
                self._input.put_nowait(self._STOP)
            except queue.Full:
                pass
        self._thread.join(timeout=self._join_timeout_s)
        return not self._thread.is_alive()

    def __enter__(self) -> "InferenceWorker":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
