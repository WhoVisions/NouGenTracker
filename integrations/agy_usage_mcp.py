#!/usr/bin/env python3
"""MCP server letting an agent CLI answer its own token questions.

Antigravity (`agy`) could not say what it had spent. The numbers existed — the
tracker parses `~/.gemini/antigravity*/brain/**/transcript.jsonl` along with
every other lane — but nothing connected the agent to them, so "how many tokens
have I used?" went to a human with a terminal.

This exposes the tracker as tools. The tracker stays the single authority: this
server shells out to it rather than reimplementing pricing, so a price-table fix
in the repo re-prices these answers with no change here.

Zero dependencies — raw JSON-RPC 2.0 over stdio, stdlib only. A CLI spawns this
in whatever environment it happens to have, and a missing package would turn a
usage question into a crash.

Every path resolves at runtime: NOUGENTRACKER_DIR, else a probe of the known
checkout locations, else a clear error naming the variable to set. Nothing here
assumes a drive letter or a user name.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

VERSION = "1.0.0"
PROTOCOL = "2024-11-05"

#: How long a computed answer stays fresh. A full scan reads hundreds of
#: transcripts; asking twice in one conversation should not pay for it twice.
CACHE_TTL_S = int(os.environ.get("AGY_USAGE_CACHE_TTL", "900"))
SCAN_TIMEOUT_S = int(os.environ.get("AGY_USAGE_TIMEOUT", "300"))


def tracker_dir() -> Optional[Path]:
    """Locate the tracker checkout: env first, then probe. Never hardcoded."""
    explicit = os.environ.get("NOUGENTRACKER_DIR", "").strip()
    if explicit and (Path(explicit) / "token_tracker.py").exists():
        return Path(explicit)
    home = Path.home()
    for candidate in (
        home / "Watchtower" / "NouGen" / "NouGenTracker",
        home / "NouGenTracker",
        Path.cwd() / "NouGenTracker",
        Path.cwd(),
    ):
        if (candidate / "token_tracker.py").exists():
            return candidate
    return None


def cache_dir() -> Path:
    path = Path(os.environ.get("AGY_USAGE_CACHE_DIR",
                               str(Path.home() / ".nougen" / "cache")))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cached(key: str) -> Optional[str]:
    path = cache_dir() / f"{key}.txt"
    if not path.exists():
        return None
    if time.time() - path.stat().st_mtime > CACHE_TTL_S:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _store(key: str, value: str) -> None:
    try:
        (cache_dir() / f"{key}.txt").write_text(value, encoding="utf-8")
    except OSError:
        pass


def run_tracker(args: List[str], key: str) -> str:
    """Run the tracker and return its stdout, cached."""
    hit = _cached(key)
    if hit is not None:
        return hit
    root = tracker_dir()
    if root is None:
        raise RuntimeError(
            "cannot find token_tracker.py. Set NOUGENTRACKER_DIR to the "
            "NouGenTracker checkout on this machine.")
    result = subprocess.run(
        [sys.executable, "token_tracker.py", *args],
        cwd=str(root), capture_output=True, text=True, timeout=SCAN_TIMEOUT_S)
    if result.returncode != 0:
        raise RuntimeError(
            f"tracker exited {result.returncode}: "
            f"{(result.stderr or '').strip()[:400]}")
    _store(key, result.stdout)
    return result.stdout


def section(text: str, header_contains: str) -> str:
    """Pull one '--- Name ---' block out of a report."""
    lines = text.splitlines()
    out: List[str] = []
    grabbing = False
    for line in lines:
        if line.startswith("--- "):
            if grabbing:
                break
            grabbing = header_contains.lower() in line.lower()
        if grabbing:
            out.append(line)
    return "\n".join(out).strip()


def tail_after(text: str, marker: str, limit: int = 40) -> str:
    idx = text.lower().find(marker.lower())
    if idx < 0:
        return ""
    return "\n".join(text[idx:].splitlines()[:limit]).strip()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def tool_my_usage(days: int = 7) -> str:
    """This CLI's OWN usage — the Antigravity lane only."""
    report = run_tracker(["--days", str(days)], f"days{days}")
    block = section(report, "Antigravity")
    if not block:
        return ("No Antigravity usage found in the last "
                f"{days} day(s). The tracker reads "
                "~/.gemini/antigravity*/brain/**/transcript.jsonl; if this CLI "
                "wrote elsewhere, that path is the thing to check.")
    return (block + "\n\nNOTE: Antigravity tokens are ESTIMATED from text "
            "length — this lane does not persist exact counts, unlike Claude "
            "Code and the Gemini CLI. Treat the figure as an order of "
            "magnitude, not an invoice.")


def tool_machine_usage(days: int = 7) -> str:
    """Every lane on THIS machine, with the shadow bill."""
    report = run_tracker(["--days", str(days)], f"days{days}")
    parts = [section(report, "Claude Code"), section(report, "Antigravity"),
             section(report, "Codex"), section(report, "Gemini CLI"),
             section(report, "Fleet Usage")]
    body = "\n\n".join(p for p in parts if p)
    bill = tail_after(report, "By Model Breakdown", 30)
    return (body + ("\n\n" + bill if bill else "")).strip() or report[:4000]


def tool_fleet_usage(days: Optional[int] = None) -> str:
    """Totals across every machine that has published dailies."""
    args = ["--fleet"] + (["--days", str(days)] if days else [])
    report = run_tracker(args, f"fleet{days or 'all'}")
    block = tail_after(report, "Fleet totals", 45)
    return block or ("No fleet totals in the tracker output — no machine has "
                     "published dailies to this clone yet.")


def tool_cost_by_model(days: int = 7) -> str:
    """What each model cost, at first-party list prices."""
    report = run_tracker(["--days", str(days)], f"days{days}")
    block = tail_after(report, "By Model Breakdown", 30)
    return block or "No model breakdown in the window."


def tool_where(_: Any = None) -> str:
    """Say where the answers come from, so a wrong number is traceable."""
    root = tracker_dir()
    return json.dumps({
        "tracker": str(root) if root else None,
        "resolved_by": ("NOUGENTRACKER_DIR"
                        if os.environ.get("NOUGENTRACKER_DIR") else "probe"),
        "cache_dir": str(cache_dir()),
        "cache_ttl_seconds": CACHE_TTL_S,
        "antigravity_source":
            "~/.gemini/antigravity*/brain/**/transcript.jsonl (estimated)",
        "server_version": VERSION,
    }, indent=2)


TOOLS: Dict[str, Dict[str, Any]] = {
    "my_token_usage": {
        "fn": tool_my_usage,
        "description": (
            "How many tokens THIS CLI (Antigravity) has used, by day. Use when "
            "asked 'how many tokens have I used', 'what have I spent', or "
            "about your own consumption. Antigravity counts are estimated, "
            "not exact — say so when reporting them."),
        "schema": {"type": "object", "properties": {
            "days": {"type": "integer",
                     "description": "days back from now (default 7)"}}},
    },
    "machine_token_usage": {
        "fn": tool_machine_usage,
        "description": (
            "Every AI lane on THIS machine — Claude Code, Antigravity, Codex, "
            "Gemini CLI and the local Ollama fleet — plus the API-equivalent "
            "shadow bill. Use for 'what has this computer spent'."),
        "schema": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "days back (default 7)"}}},
    },
    "fleet_token_usage": {
        "fn": tool_fleet_usage,
        "description": (
            "Totals across EVERY machine that has published dailies, with "
            "per-machine spend. Use for 'the whole fleet', 'all my "
            "computers', or when comparing machines."),
        "schema": {"type": "object", "properties": {
            "days": {"type": "integer",
                     "description": "days back; omit for all published history"}}},
    },
    "token_cost_by_model": {
        "fn": tool_cost_by_model,
        "description": (
            "Spend broken down by model at first-party list prices. Use for "
            "'which model is costing the most'."),
        "schema": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "days back (default 7)"}}},
    },
    "token_usage_provenance": {
        "fn": tool_where,
        "description": (
            "Where these numbers come from: tracker location, how it was "
            "resolved, cache age, and which sources are estimated rather than "
            "measured. Use when a figure looks wrong or needs sourcing."),
        "schema": {"type": "object", "properties": {}},
    },
}


# ---------------------------------------------------------------------------
# JSON-RPC over stdio
# ---------------------------------------------------------------------------

def respond(msg_id: Any, result: Any = None, error: Any = None) -> None:
    payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def handle(message: Dict[str, Any]) -> None:
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        respond(msg_id, {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "nougen-usage", "version": VERSION},
        })
    elif method in ("notifications/initialized", "initialized"):
        pass  # notification: no id, no reply
    elif method == "tools/list":
        respond(msg_id, {"tools": [
            {"name": name, "description": spec["description"],
             "inputSchema": spec["schema"]}
            for name, spec in TOOLS.items()]})
    elif method == "tools/call":
        name = params.get("name")
        spec = TOOLS.get(name)
        if spec is None:
            respond(msg_id, error={"code": -32601,
                                   "message": f"unknown tool {name}"})
            return
        args = params.get("arguments") or {}
        try:
            text = spec["fn"](**args) if args else spec["fn"]()
        except TypeError:
            text = spec["fn"]()
        except subprocess.TimeoutExpired:
            text = ("The tracker scan exceeded its timeout. Raise "
                    "AGY_USAGE_TIMEOUT or ask for a shorter window.")
        except Exception as exc:  # a usage question must not crash the CLI
            text = f"Could not answer: {exc}"
        respond(msg_id, {"content": [{"type": "text", "text": str(text)}],
                         "isError": False})
    elif msg_id is not None:
        respond(msg_id, error={"code": -32601,
                               "message": f"unsupported method {method}"})


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        try:
            handle(message)
        except Exception as exc:  # never die on one bad message
            if isinstance(message, dict) and message.get("id") is not None:
                respond(message.get("id"),
                        error={"code": -32603, "message": str(exc)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
