#!/usr/bin/env python3
"""Probe every MCP server in a config and report which ones actually answer.

A registered server that never connects is not free. Antigravity blocks its
turn until tool registration finishes, so one dead entry can cost the whole
session — that is exactly how `agy --print` came to die at zero steps with 18
of 29 servers still connecting.

This spawns each server the way the CLI would, sends `initialize`, and waits.
Three outcomes, and the distinction matters:

  READY  answered inside the budget — safe to keep
  SLOW   answered, but late enough to hurt a cold start
  DEAD   never answered, or the process failed to start

Read-only: it never edits a config. `--json` emits the verdicts so a caller can
decide what to restore. Deciding what a fleet needs is the operator's call, not
a probe's.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18",
               "capabilities": {},
               "clientInfo": {"name": "mcp-triage", "version": "1.0"}},
}


def read_servers(path: Path, keys: Tuple[str, ...]) -> Dict[str, Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    servers: Dict[str, Dict[str, Any]] = {}
    for key in keys:
        servers.update(data.get(key) or {})
    return servers


def probe(name: str, spec: Dict[str, Any], budget: float,
          slow_at: float) -> Dict[str, Any]:
    command = spec.get("command")
    if not command:
        # An SSE/remote entry has nothing to spawn. Calling that DEAD would
        # condemn working servers on the strength of a probe that never ran —
        # the verdict has to distinguish "failed" from "not tested".
        return {"server": name, "verdict": "REMOTE", "seconds": 0.0,
                "detail": "SSE/remote entry — no local process to probe"}
    argv = [command, *(spec.get("args") or [])]
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (spec.get("env") or {}).items()})
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, env=env,
            cwd=spec.get("cwd") or None)
    except (OSError, ValueError) as exc:
        return {"server": name, "verdict": "DEAD", "seconds": 0.0,
                "detail": f"could not start: {exc}"}

    reply: List[str] = []

    def reader() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                if line.strip():
                    reply.append(line)
                    return
        except Exception:
            pass

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        proc.stdin.write(json.dumps(INIT) + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]
    except (OSError, ValueError):
        pass
    thread.join(budget)
    elapsed = time.monotonic() - started

    detail = ""
    verdict = "DEAD"
    if reply:
        try:
            payload = json.loads(reply[0])
            info = (payload.get("result") or {}).get("serverInfo") or {}
            detail = f"{info.get('name', '?')} {info.get('version', '')}".strip()
            verdict = "SLOW" if elapsed > slow_at else "READY"
        except ValueError:
            detail = "answered with non-JSON on stdout"
    else:
        detail = ("no initialize response within budget"
                  if proc.poll() is None else
                  f"process exited {proc.returncode} before answering")
    try:
        proc.kill()
    except Exception:
        pass
    return {"server": name, "verdict": verdict, "seconds": round(elapsed, 1),
            "detail": detail}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config",
                        default=str(Path.home() / ".gemini" / "config"
                                    / "mcp_config.json"))
    parser.add_argument("--keys", default="mcpServers,mcpServersParked",
                        help="comma-separated top-level keys to probe")
    parser.add_argument("--budget", type=float,
                        default=float(os.environ.get("MCP_TRIAGE_BUDGET", "25")),
                        help="seconds to wait for initialize (default 25)")
    parser.add_argument("--slow-at", type=float,
                        default=float(os.environ.get("MCP_TRIAGE_SLOW_AT", "8")),
                        help="seconds above which a server counts as SLOW")
    parser.add_argument("--only", default="", help="comma-separated server names")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    path = Path(os.path.expanduser(args.config))
    if not path.exists():
        print(f"no such config: {path}", file=sys.stderr)
        return 2
    servers = read_servers(path, tuple(k.strip() for k in args.keys.split(",")))
    if args.only:
        wanted = {n.strip() for n in args.only.split(",")}
        servers = {k: v for k, v in servers.items() if k in wanted}
    if not servers:
        print("no servers to probe", file=sys.stderr)
        return 1

    results: List[Dict[str, Any]] = []
    if not args.json:
        print(f"probing {len(servers)} server(s) from {path.name} "
              f"(budget {args.budget:g}s, slow above {args.slow_at:g}s)\n")
    for name in sorted(servers):
        result = probe(name, servers[name], args.budget, args.slow_at)
        results.append(result)
        if not args.json:
            print(f"  {result['verdict']:<6}{result['seconds']:>6.1f}s  "
                  f"{name:<28}{result['detail'][:64]}")

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    counts: Dict[str, int] = {}
    for result in results:
        counts[result["verdict"]] = counts.get(result["verdict"], 0) + 1
    print("\n" + "  ".join(f"{v}={counts.get(v, 0)}"
                           for v in ("READY", "SLOW", "DEAD")))
    dead = [r["server"] for r in results if r["verdict"] == "DEAD"]
    if dead:
        print("\nDEAD entries cost a blocking startup phase on every launch, "
              "not nothing:\n  " + ", ".join(dead))
    return 0


if __name__ == "__main__":
    sys.exit(main())
