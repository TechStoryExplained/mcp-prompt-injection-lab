"""Experiment 1: deterministic demo. Writes traces/<label>.json for each scenario."""

from __future__ import annotations

import re
from pathlib import Path

import httpx2

from lab import data
from lab.host import Host, mcp_client
from lab.runtime import run_servers
from lab.trace import Trace

PROTOCOL_VERSION = "2026-07-28"
TRACE_DIR = Path(__file__).resolve().parent.parent / "traces"

_CALL_IN_TEXT = re.compile(r'(\w+)\(((?:\s*\w+="[^"]*"\s*,?)+)\)')


def injected_calls(text: str) -> list[tuple[str, dict]]:
    """Tool calls written as plain text inside tool output, e.g. restart_service(service="payment")."""
    return [(name, dict(re.findall(r'(\w+)="([^"]*)"', args))) for name, args in _CALL_IN_TEXT.findall(text)]


async def naive_agent(host: Host) -> dict:
    """A scripted stand-in for a model that obeys instructions found in tool output.

    Deliberately worst-case and deterministic, so the security layers can be shown reliably.
    Experiment 2 (`python -m lab agent`) measures what a real model does instead.
    """
    await host.call("get_checkout_errors", {})
    issues = await host.call("get_issues", {})
    for issue in issues.json()["issues"]:
        for name, args in injected_calls(issue["body"]):
            outcome = await host.call(name, args)  # obeys the first injected instruction it finds
            return {
                "obeyed_issue": issue["number"],
                "attempted": {"tool": name, "arguments": args},
                "executed": outcome.executed,
                "is_error": outcome.is_error,
                "text": outcome.text,
            }
    return {"attempted": None, "executed": False}


async def scenario_unsafe_restart(urls: dict, state: data.DeployState) -> Trace:
    t = Trace("unsafe_restart", "Unsafe: poisoned issue -> auto-approved prod restart succeeds", "unsafe", "none")
    async with Host("unsafe", urls, t) as host:
        result = await naive_agent(host)
    t.outcome = {**result, "restarts_recorded": list(state.restarts)}
    return t


async def scenario_header_mismatch(urls: dict, state: data.DeployState) -> Trace:
    """Raw POST whose Mcp-Name header disagrees with the body's params.name."""
    t = Trace("header_mismatch", "Header/body mismatch -> 400 + JSON-RPC -32020 HeaderMismatch", "unsafe", "protocol (transport header validation)")
    before = len(state.restarts)
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "restart_service",
            "arguments": {"service": "payment", "environment": "prod"},
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientInfo": {"name": "header-mismatch-probe", "version": "0.1.0"},
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }
    headers = {
        "Authorization": f"Bearer {data.PROD_ADMIN_TOKEN}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": "tools/call",
        "Mcp-Name": "get_deployment_status",  # a gateway routing on this header would see a read call
    }
    async with httpx2.AsyncClient(event_hooks=t.hooks("probe")) as http:
        resp = await http.post(urls["deploy"], json=body, headers=headers)
    t.outcome = {
        "http_status": resp.status_code,
        "jsonrpc_error": resp.json().get("error"),
        "restart_executed": len(state.restarts) > before,
    }
    return t


async def scenario_safe_hidden_by_host(urls: dict, state: data.DeployState) -> Trace:
    t = Trace("safe_hidden_by_host", "Safe, layer 1: investigation host never loads restart_service", "safe", "1 (host)")
    async with Host("safe", urls, t) as host:
        result = await naive_agent(host)
        exposed = sorted(tool.name for tool in host.tools)
    t.outcome = {**result, "host_exposed_tools": exposed, "restarts_recorded": list(state.restarts)}
    return t


async def scenario_safe_denied_by_server(urls: dict, state: data.DeployState) -> Trace:
    """Bypass the host entirely: call restart_service directly with the investigator token."""
    t = Trace("safe_denied_by_server", "Safe, layer 2: direct restart (host bypassed) refused by server scope check", "safe", "2 (server)")
    async with mcp_client(urls["deploy"], data.INVESTIGATOR_TOKEN, t, "direct-client (host bypassed)") as client:
        listed = sorted(tool.name for tool in (await client.list_tools()).tools)
        result = await client.call_tool("restart_service", {"service": "payment", "environment": "prod"})
    text = "\n".join(c.text for c in result.content if getattr(c, "type", None) == "text")
    t.outcome = {
        "server_lists_restart_service": "restart_service" in listed,
        "is_error": result.is_error,
        "text": text,
        "restarts_recorded": list(state.restarts),
    }
    return t


async def scenario_root_cause(urls: dict, state: data.DeployState) -> Trace:
    t = Trace("root_cause", "Safe host, read tools only: find the root cause", "safe", "1 (host, read-only toolset)")
    async with Host("safe", urls, t) as host:
        logs = (await host.call("get_checkout_errors", {})).json()
        status = (await host.call("get_deployment_status", {"service": "payment"})).json()
        config = (await host.call("read_config", {"service": "payment"})).json()
    live = next(d for d in status["deployments"] if d["status"] == "live")
    change = next(c for c in config["diff_vs_previous"]["changes"] if c["key"] == "timeout_ms")
    t.outcome = {
        "top_error": logs["summary"]["top_error"],
        "errors_before": logs["summary"]["errors_before_deploy_4f2c"],
        "errors_after": logs["summary"]["errors_after_deploy_4f2c"],
        "live_deploy": live["deploy"],
        "config_change": change,
        "conclusion": (
            f"{logs['summary']['top_error']} started with deploy {live['deploy']}, which changed payment "
            f"timeout_ms {change['from']} -> {change['to']}. A restart would not fix it; revert the config "
            f"(a human with deploy rights decides)."
        ),
        "restarts_recorded": list(state.restarts),
    }
    return t


async def run_demo(out_dir: Path = TRACE_DIR, ports: dict[str, int] | None = None) -> list[Trace]:
    traces: list[Trace] = []
    state = data.DeployState()
    with run_servers("unsafe", state, ports) as urls:
        traces.append(await scenario_unsafe_restart(urls, state))
        traces.append(await scenario_header_mismatch(urls, state))
    state.reset()
    with run_servers("safe", state, ports) as urls:
        traces.append(await scenario_safe_hidden_by_host(urls, state))
        traces.append(await scenario_safe_denied_by_server(urls, state))
        traces.append(await scenario_root_cause(urls, state))
    for t in traces:
        t.save(out_dir)
    return traces


def print_summary(traces: list[Trace], out_dir: Path) -> None:
    by = {t.label: t.outcome for t in traces}
    u, h, s, m, r = (by[k] for k in ("unsafe_restart", "safe_hidden_by_host", "safe_denied_by_server", "header_mismatch", "root_cause"))
    rows = [
        ("unsafe_restart", "UNSAFE", f"restart executed on prod: {bool(u['restarts_recorded'])} (auto-approved, prod-admin token)"),
        ("safe_hidden_by_host", "LAYER 1", f"host exposes {len(h['host_exposed_tools'])} read tools, restart hidden; injected call executed: {h['executed']}"),
        ("safe_denied_by_server", "LAYER 2", f"direct call isError={s['is_error']}: {s['text']}"),
        ("header_mismatch", "PROTOCOL", f"HTTP {m['http_status']}, JSON-RPC {m['jsonrpc_error']['code']}; restart executed: {m['restart_executed']}"),
        ("root_cause", "ANSWER", r["conclusion"]),
    ]
    print("\nmcp-checkout-lab demo (protocol 2026-07-28, synthetic data)\n")
    for label, tag, text in rows:
        print(f"  [{tag:<8}] {label:<22} {text}")
    print(f"\nTraces written to {out_dir}:")
    for t in traces:
        print(f"  {t.label}.json")
