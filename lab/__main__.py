"""python -m lab {demo,agent,serve}"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time

from lab import data
from lab.agent import DEFAULT_EFFORT, DEFAULT_MODEL, run_agent
from lab.demo import TRACE_DIR, print_summary, run_demo
from lab.runtime import DEFAULT_PORTS, run_servers


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m lab")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo", help="Experiment 1: deterministic unsafe/safe/root-cause run, writes traces/*.json")
    agent = sub.add_parser("agent", help="Experiment 2: real model, unsafe mode (needs ANTHROPIC_API_KEY)")
    agent.add_argument("--runs", type=int, default=10)
    agent.add_argument("--model", default=os.environ.get("LAB_MODEL", DEFAULT_MODEL))
    agent.add_argument("--effort", default=os.environ.get("LAB_EFFORT", DEFAULT_EFFORT))
    serve = sub.add_parser("serve", help="Run the three servers on 127.0.0.1:8101-8103 until Ctrl+C")
    serve.add_argument("--mode", choices=["unsafe", "safe"], default="safe")
    args = parser.parse_args()
    # Per-request client logs and the SDK's "tool failed" log would drown the summary; traces hold the detail.
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.CRITICAL)

    if args.cmd == "demo":
        traces = asyncio.run(run_demo(TRACE_DIR, DEFAULT_PORTS))
        print_summary(traces, TRACE_DIR)
        return 0
    if args.cmd == "agent":
        return asyncio.run(run_agent(args.runs, args.model, args.effort))
    with run_servers(args.mode, data.DeployState(), DEFAULT_PORTS) as urls:
        for name, url in urls.items():
            print(f"{name:<7} {url}  ({args.mode})")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
