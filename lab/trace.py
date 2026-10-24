"""Trace recorder: HTTP headers + JSON bodies of every MCP exchange, plus host-layer decisions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2

from lab import data

# Headers that only add run-to-run noise to the committed traces.
_DROP_HEADERS = {"date", "accept-encoding", "connection", "user-agent", "content-length", "server"}


def redact_auth(value: str) -> str:
    token = data.TOKENS.get(value.removeprefix("Bearer ").strip())
    if token is None:
        return "Bearer [REDACTED]"
    return f"Bearer [REDACTED fake token: principal={token.principal} scopes={','.join(sorted(token.scopes))}]"


def _headers(headers: httpx2.Headers) -> dict[str, str]:
    out = {}
    for k, v in headers.items():
        k = k.lower()
        if k in _DROP_HEADERS:
            continue
        out[k] = redact_auth(v) if k == "authorization" else v
    return out


def _body(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")


@dataclass
class Trace:
    label: str
    title: str
    mode: str
    layer: str  # which defence (if any) this trace demonstrates
    events: list[dict] = field(default_factory=list)
    outcome: dict = field(default_factory=dict)

    def note(self, layer: str, **fields: Any) -> None:
        """Record a decision that produced no HTTP traffic (e.g. the host hiding or refusing a tool)."""
        self.events.append({"kind": "decision", "layer": layer, **fields})

    def hooks(self, actor: str) -> dict:
        """httpx2 event hooks that append each request/response pair to this trace."""

        async def on_response(response: httpx2.Response) -> None:
            await response.aread()
            req = response.request
            self.events.append(
                {
                    "kind": "http",
                    "actor": actor,
                    "request": {
                        "method": req.method,
                        "url": str(req.url),
                        "headers": _headers(req.headers),
                        "body": _body(req.content),
                    },
                    "response": {
                        "status": response.status_code,
                        "headers": _headers(response.headers),
                        "body": _body(response.content),
                    },
                }
            )

        return {"response": [on_response]}

    def save(self, out_dir: Path) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{self.label}.json"
        doc = {"label": self.label, "title": self.title, "mode": self.mode, "layer": self.layer, "outcome": self.outcome, "events": self.events}
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        return path
