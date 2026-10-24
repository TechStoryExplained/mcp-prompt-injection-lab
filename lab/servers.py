"""The three MCP servers: logs, github, deploy. Built on the official MCP Python SDK (v2)."""

from __future__ import annotations

from typing import Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from lab import data

Mode = Literal["unsafe", "safe"]

READ_ONLY = ToolAnnotations(readOnlyHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


def build_logs() -> MCPServer:
    server = MCPServer("logs", version="0.1.0")

    @server.tool(annotations=READ_ONLY)
    def get_checkout_errors() -> dict:
        """Recent checkout error log lines and a summary."""
        return {"summary": data.CHECKOUT_LOG_SUMMARY, "sample": data.CHECKOUT_ERRORS}

    return server


def build_github() -> MCPServer:
    server = MCPServer("github", version="0.1.0")

    @server.tool(annotations=READ_ONLY)
    def get_issues() -> dict:
        """Open issues in the checkout repository."""
        return {"issues": data.ISSUES}

    return server


def build_deploy(mode: Mode, state: data.DeployState) -> MCPServer:
    """Deploy server. In safe mode every tool checks the caller's bearer-token scope (layer 2)."""
    server = MCPServer("deploy", version="0.1.0")

    def authorize(ctx: Context, scope: str) -> str:
        if mode == "unsafe":
            # Unsafe: one shared prod-admin credential, no scope check at all.
            return "unchecked"
        auth = (ctx.headers or {}).get("authorization", "")
        token = data.TOKENS.get(auth.removeprefix("Bearer ").strip())
        if token is None:
            raise ToolError("unauthorized: missing or unknown bearer token")
        if scope not in token.scopes:
            raise ToolError(
                f"forbidden: token for principal '{token.principal}' lacks scope '{scope}' "
                f"(has: {', '.join(sorted(token.scopes))})"
            )
        return token.principal

    @server.tool(annotations=READ_ONLY)
    def get_deployment_status(service: str, ctx: Context) -> dict:
        """Deployment history for a service."""
        authorize(ctx, "deploy:read")
        return {"service": service, "deployments": data.DEPLOYMENTS.get(service, [])}

    @server.tool(annotations=READ_ONLY)
    def read_config(service: str, ctx: Context) -> dict:
        """Current config for a service, with the diff against the previous deploy."""
        authorize(ctx, "deploy:read")
        return data.config_diff(service)

    @server.tool(annotations=DESTRUCTIVE)
    def restart_service(service: str, environment: str, ctx: Context) -> dict:
        """Restart a service in an environment."""
        principal = authorize(ctx, "deploy:restart")
        entry = {"service": service, "environment": environment, "authorized_as": principal}
        state.restarts.append(entry)
        return {"restarted": True, **entry, "restart_count": len(state.restarts)}

    return server
