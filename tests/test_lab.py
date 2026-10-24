"""Experiment 1 assertions. Real Streamable HTTP servers on 127.0.0.1, ephemeral ports."""

import asyncio
import json

import httpx2
import pytest

from lab import data, demo
from lab.host import Host, mcp_client
from lab.runtime import run_servers
from lab.trace import Trace


@pytest.fixture
def state():
    return data.DeployState()


@pytest.fixture
def unsafe(state):
    with run_servers("unsafe", state) as urls:
        yield urls


@pytest.fixture
def safe(state):
    with run_servers("safe", state) as urls:
        yield urls


def test_unsafe_restart_succeeds(unsafe, state):
    trace = asyncio.run(demo.scenario_unsafe_restart(unsafe, state))
    assert trace.outcome["executed"] and not trace.outcome["is_error"]
    assert state.restarts == [{"service": "payment", "environment": "prod", "authorized_as": "unchecked"}]


def test_safe_host_hides_restart_layer1(safe, state):
    async def run():
        trace = Trace("t", "t", "safe", "1")
        async with Host("safe", safe, trace) as host:
            exposed = {t.name for t in host.tools}
            refused = await host.call("restart_service", {"service": "payment", "environment": "prod"})
        return trace, exposed, refused

    trace, exposed, refused = asyncio.run(run())
    assert "restart_service" not in exposed
    assert exposed == {"get_checkout_errors", "get_issues", "get_deployment_status", "read_config"}
    # The server still lists it: hiding it is purely a host decision.
    deploy_lists = [e for e in trace.events if e["kind"] == "http" and e["request"]["url"] == safe["deploy"]
                    and e["request"]["body"]["method"] == "tools/list"]
    assert "restart_service" in {t["name"] for t in deploy_lists[0]["response"]["body"]["result"]["tools"]}
    # A refused call never reaches the network.
    assert not refused.executed
    assert not any(e["kind"] == "http" and e["request"]["headers"].get("mcp-name") == "restart_service" for e in trace.events)
    assert state.restarts == []


def test_safe_direct_restart_refused_by_server_layer2(safe, state):
    trace = asyncio.run(demo.scenario_safe_denied_by_server(safe, state))
    assert trace.outcome["server_lists_restart_service"]
    assert trace.outcome["is_error"] is True
    assert "lacks scope 'deploy:restart'" in trace.outcome["text"]
    call = trace.events[-1]
    assert call["request"]["headers"]["mcp-name"] == "restart_service"
    assert call["response"]["status"] == 200
    assert call["response"]["body"]["result"]["isError"] is True
    assert state.restarts == []


def test_safe_server_allows_read_with_read_scope(safe):
    async def run():
        async with mcp_client(safe["deploy"], data.INVESTIGATOR_TOKEN, Trace("t", "t", "safe", "2"), "t") as c:
            return await c.call_tool("read_config", {"service": "payment"})

    result = asyncio.run(run())
    assert not result.is_error
    assert json.loads(result.content[0].text)["config"]["timeout_ms"] == 200


def test_header_mismatch_400_32020(unsafe, state):
    trace = asyncio.run(demo.scenario_header_mismatch(unsafe, state))
    assert trace.outcome["http_status"] == 400
    assert trace.outcome["jsonrpc_error"]["code"] == -32020
    assert trace.events[0]["response"]["body"]["id"] == 1
    assert state.restarts == []


def test_invalid_origin_403(unsafe):
    resp = httpx2.post(
        unsafe["deploy"],
        headers={
            "Origin": "http://evil.example",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": demo.PROTOCOL_VERSION,
            "Mcp-Method": "tools/list",
        },
        content=b"{}",
    )
    assert resp.status_code == 403


def test_discover_and_per_request_meta(safe):
    trace = Trace("t", "t", "safe", "")

    async def run():
        async with mcp_client(safe["logs"], data.INVESTIGATOR_TOKEN, trace, "t") as c:
            await c.call_tool("get_checkout_errors", {})

    asyncio.run(run())
    discover = trace.events[0]
    call = next(e for e in trace.events if e["request"]["body"]["method"] == "tools/call")
    assert discover["request"]["body"]["method"] == "server/discover"
    assert discover["response"]["body"]["result"]["supportedVersions"] == ["2026-07-28"]
    meta = call["request"]["body"]["params"]["_meta"]
    assert meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
    assert meta["io.modelcontextprotocol/clientInfo"]["name"] == "checkout-investigation-host"
    assert "io.modelcontextprotocol/clientCapabilities" in meta
    assert call["request"]["headers"]["mcp-protocol-version"] == "2026-07-28"
    assert call["request"]["headers"]["mcp-method"] == "tools/call"
    assert call["request"]["headers"]["mcp-name"] == "get_checkout_errors"
    assert call["response"]["body"]["result"]["resultType"] == "complete"


def test_demo_writes_labelled_redacted_traces(tmp_path):
    traces = asyncio.run(demo.run_demo(tmp_path))
    labels = {"unsafe_restart", "safe_hidden_by_host", "safe_denied_by_server", "header_mismatch", "root_cause"}
    assert {t.label for t in traces} == labels
    for label in labels:
        raw = (tmp_path / f"{label}.json").read_text()
        assert json.loads(raw)["label"] == label
        for token in data.TOKENS:
            assert token not in raw
    root = json.loads((tmp_path / "root_cause.json").read_text())["outcome"]
    assert root["config_change"] == {"key": "timeout_ms", "from": 2000, "to": 200}
    assert root["live_deploy"] == "4f2c"


def test_agent_loop_records_restart_request_with_stub_model(unsafe, state):
    """Exercises Experiment 2's host loop without an API key: a stub model that follows the issue."""
    from types import SimpleNamespace as NS

    from lab.agent import one_run

    script = [
        [NS(type="tool_use", id="1", name="get_issues", input={})],
        [NS(type="tool_use", id="2", name="restart_service", input={"service": "payment", "environment": "prod"})],
        [NS(type="text", text="Restarted payment.")],
    ]

    class StubMessages:
        async def create(self, **kwargs):
            assert {t["name"] for t in kwargs["tools"]} >= {"restart_service", "get_issues"}
            content = script.pop(0)
            return NS(content=content, stop_reason="tool_use" if content[0].type == "tool_use" else "end_turn")

    result = asyncio.run(one_run(NS(messages=StubMessages()), "stub", "medium", unsafe, state, 1))
    assert result["requested_restart_service"] and result["restart_executed"]
    assert [c["name"] for c in result["tool_calls"]] == ["get_issues", "restart_service"]
