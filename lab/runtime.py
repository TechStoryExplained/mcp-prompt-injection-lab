"""Run the three servers over Streamable HTTP on 127.0.0.1 (uvicorn, one thread each)."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn

from lab import data
from lab.servers import Mode, build_deploy, build_github, build_logs

HOST = "127.0.0.1"  # loopback only, never 0.0.0.0
DEFAULT_PORTS = {"logs": 8101, "github": 8102, "deploy": 8103}


def free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


@contextmanager
def run_servers(mode: Mode, state: data.DeployState, ports: dict[str, int] | None = None) -> Iterator[dict[str, str]]:
    """Start logs/github/deploy for `mode`; yield {name: url}. Ports default to ephemeral."""
    ports = ports or {name: free_port() for name in DEFAULT_PORTS}
    apps = {"logs": build_logs(), "github": build_github(), "deploy": build_deploy(mode, state)}
    servers: list[uvicorn.Server] = []
    threads: list[threading.Thread] = []
    try:
        for name, mcp_server in apps.items():
            # json_response=True: each request gets one application/json reply (the spec allows JSON or SSE).
            # The SDK enables Host/Origin validation automatically for a 127.0.0.1 bind.
            app = mcp_server.streamable_http_app(json_response=True, host=HOST)
            server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=ports[name], log_level="warning"))
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            servers.append(server)
            threads.append(thread)
        deadline = time.monotonic() + 10
        while not all(s.started for s in servers):
            if time.monotonic() > deadline:
                raise RuntimeError("servers did not start")
            time.sleep(0.02)
        yield {name: f"http://{HOST}:{ports[name]}/mcp" for name in apps}
    finally:
        for s in servers:
            s.should_exit = True
        for t in threads:
            t.join(timeout=5)
