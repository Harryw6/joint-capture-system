from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import http
import logging
import socket
import threading
import time
import traceback
from typing import Iterator

from openpi_client import msgpack_numpy
import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response
from websockets.sync import server as websocket_server

from p450_stream.mock_policy import SCENARIOS, MockP450Policy
from p450_stream.protocol import DEFAULT_PROFILE


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServerEndpoint:
    host: str
    port: int


class MockPolicyServer:
    """Small wire-compatible counterpart of OpenPI's policy server."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8000,
        scenario_name: str = "box_return_v1",
        terminal_feedback: bool = False,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.policy = MockP450Policy(
            action_horizon=10,
            scenario_name=scenario_name,
            terminal_feedback_start_step=120 if terminal_feedback else None,
            terminal_hold_start_step=150,
        )
        self.metadata = DEFAULT_PROFILE.as_metadata("mock-pi05-p450")

    def _handler(self, connection: websocket_server.ServerConnection) -> None:
        packer = msgpack_numpy.Packer()
        connection.send(packer.pack(self.metadata))
        while True:
            try:
                observation = msgpack_numpy.unpackb(connection.recv())
                started = time.monotonic()
                result = self.policy.infer(observation)
                result["server_timing"] = {
                    "infer_ms": (time.monotonic() - started) * 1000.0
                }
                connection.send(packer.pack(result))
            except websockets.ConnectionClosed:
                return
            except Exception:
                connection.send(traceback.format_exc())
                connection.close(code=1011, reason="mock policy failure")
                return

    @staticmethod
    def _health_check(connection, request):
        del connection
        if request.path == "/healthz":
            body = b"OK\n"
            return Response(
                status_code=http.HTTPStatus.OK,
                reason_phrase="OK",
                headers=Headers(
                    {
                        "Content-Type": "text/plain; charset=utf-8",
                        "Content-Length": str(len(body)),
                        "Connection": "close",
                    }
                ),
                body=body,
            )
        return None

    def _build_server(self, *, sock: socket.socket | None = None):
        return websocket_server.serve(
            self._handler,
            self.host if sock is None else None,
            self.port if sock is None else None,
            sock=sock,
            compression=None,
            max_size=None,
            process_request=self._health_check,
        )

    @contextmanager
    def running(self) -> Iterator[ServerEndpoint]:
        listener = socket.create_server((self.host, self.port), reuse_port=False)
        endpoint = ServerEndpoint(
            host=self.host,
            port=int(listener.getsockname()[1]),
        )
        server = self._build_server(sock=listener)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield endpoint
        finally:
            server.shutdown()
            thread.join(timeout=5.0)

    def serve_forever(self) -> None:
        LOGGER.info("serving mock P450 policy on %s:%d", self.host, self.port)
        with self._build_server() as server:
            server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve deterministic P450 action chunks")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--scenario",
        choices=tuple(SCENARIOS),
        default="box_return_v1",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    MockPolicyServer(
        host=args.host,
        port=args.port,
        scenario_name=args.scenario,
        terminal_feedback=True,
    ).serve_forever()


if __name__ == "__main__":
    main()
