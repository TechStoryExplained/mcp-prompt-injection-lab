"""The demo host: connects to the three servers with the SDK client and applies host policy (layer 1)."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation, Tool

from lab import data
from lab.servers import Mode
from lab.trace import Trace

# Layer 1 (safe mode): the investigation host loads only these tools. Explicit allowlist,
# not the server's readOnlyHint annotation, because annotations are untrusted server claims.
INVESTIGATION_TOOLS = {"get_checkout_errors", "get_issues", "get_deployment_status", "read_config"}

CLIENT_INFO = Implementation(name="checkout-investigation-host", version="0.1.0")


def mcp_client(url: str, token: str, trace: Trace, actor: str) -> Client:
    """An SDK client speaking 2026-07-28 Streamable HTTP, with a bearer token and trace hooks."""
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, event_hooks=trace.hooks(actor))
    return Client(streamable_http_client(url, http_client=http), client_info=CLIENT_INFO)


@dataclass
class ToolOutcome:
    executed: bool  # did a request reach the server?
    is_error: bool
    text: str

    def json(self) -> Any:
        return json.loads(self.text)


class Host:
    def __init__(self, mode: Mode, urls: dict[str, str], trace: Trace) -> None:
        self.mode = mode
        self.urls = urls
        self.trace = trace
        self.token = data.PROD_ADMIN_TOKEN if mode == "unsafe" else data.INVESTIGATOR_TOKEN
        self._stack = AsyncExitStack()
        self._clients: dict[str, Client] = {}
        self._route: dict[str, str] = {}  # tool name -> server name
        self.tools: list[Tool] = []  # what the host exposes to the agent

    async def __aenter__(self) -> Host:
        for name, url in self.urls.items():
            self._clients[name] = await self._stack.enter_async_context(mcp_client(url, self.token, self.trace, "host"))
        await self._load_tools()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()

    async def _load_tools(self) -> None:
        hidden = []
        for server_name, client in self._clients.items():
            for tool in (await client.list_tools()).tools:
                if self.mode == "safe" and tool.name not in INVESTIGATION_TOOLS:
                    hidden.append(f"{server_name}.{tool.name}")
                    continue
                self._route[tool.name] = server_name
                self.tools.append(tool)
        if hidden:
            self.trace.note(
                "host",
                action="hide_tools",
                hidden=hidden,
                reason="investigation host loads only read tools (allowlist)",
            )

    async def call(self, name: str, arguments: dict) -> ToolOutcome:
        if name not in self._route:
            msg = f"tool '{name}' is not available in this host"
            self.trace.note("host", action="refuse_call", tool=name, arguments=arguments, reason=msg)
            return ToolOutcome(executed=False, is_error=True, text=msg)
        if self.mode == "unsafe":
            self.trace.note("host", action="auto_approve", tool=name, arguments=arguments)
        result = await self._clients[self._route[name]].call_tool(name, arguments)
        text = "\n".join(c.text for c in result.content if getattr(c, "type", None) == "text")
        return ToolOutcome(executed=True, is_error=bool(result.is_error), text=text)
