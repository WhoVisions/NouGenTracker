#!/usr/bin/env python3
"""MCP server letting any agent CLI answer its own token questions.

Every agent on this machine writes a log of what it spent. None of them could
read it, so "how many tokens have I used?" went to a human with a terminal and
the usual answer was a guess from conversation length. This exposes the tracker
as tools, to every CLI that speaks MCP — Antigravity, Claude Code, the Gemini
CLI, Codex.

Design commitments, each of which is a decision someone will otherwise re-open:

**The tracker stays the single authority.** This shells out to
``token_tracker.py`` instead of reimplementing pricing, so a price-table fix in
that repo re-prices every answer here with no change. Duplicated pricing is how
two components come to disagree about the same day.

**Stdlib only.** A CLI spawns this in whatever environment it happens to have.
A missing package would turn a usage question into a crash.

**Answers are structured AND readable.** Every tool returns prose for the model
to quote and a validated object for it to compute with. Text-only forces an
agent to re-parse a table it will eventually parse wrong.

**Uncertainty travels with the number.** Each result carries which sources were
estimated rather than measured, and how stale the cache was. A token count
whose provenance was dropped is indistinguishable from one that was invented.

**stdout belongs to the protocol.** All logging goes to stderr. One stray print
corrupts the stream and the failure looks like a broken client.

Paths resolve at runtime — ``NOUGENTRACKER_DIR``, then a probe of known checkout
locations, then an error naming the variable to set. Nothing assumes a drive
letter or a username.

Run ``--selftest`` to exercise the whole surface without a client.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

SERVER_NAME = "nougen-usage"
VERSION = "2.0.0"

#: Protocol revisions this server implements. On initialize we echo the
#: client's version when we know it, and otherwise answer with our newest —
#: which is what the spec asks for and what lets one binary serve four CLIs
#: that upgrade on their own schedules.
SUPPORTED_PROTOCOLS: Tuple[str, ...] = ("2025-06-18", "2025-03-26", "2024-11-05")
PREFERRED_PROTOCOL = SUPPORTED_PROTOCOLS[0]

CACHE_TTL_S = int(os.environ.get("NOUGEN_USAGE_CACHE_TTL", "900"))
SCAN_TIMEOUT_S = int(os.environ.get("NOUGEN_USAGE_TIMEOUT", "300"))
DEFAULT_DAYS = int(os.environ.get("NOUGEN_USAGE_DEFAULT_DAYS", "7"))


def log(message: str) -> None:
    """stderr only — stdout is the protocol channel."""
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Resolution — env, then probe, never a constant
# ---------------------------------------------------------------------------

def tracker_dir() -> Optional[Path]:
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
    path = Path(os.environ.get(
        "NOUGEN_USAGE_CACHE_DIR", str(Path.home() / ".nougen" / "cache")))
    path.mkdir(parents=True, exist_ok=True)
    return path


class TrackerError(RuntimeError):
    """A failure the agent should report verbatim rather than work around."""


def run_tracker(args: List[str], key: str) -> Tuple[str, float]:
    """Run the tracker; return (stdout, cache_age_seconds).

    Age is returned rather than hidden so a caller can say how fresh the answer
    is. A cached number presented as live is a small lie that compounds.
    """
    path = cache_dir() / f"{key}.txt"
    if path.exists():
        age = time.time() - path.stat().st_mtime
        if age <= CACHE_TTL_S:
            try:
                return path.read_text(encoding="utf-8"), age
            except OSError:
                pass
    root = tracker_dir()
    if root is None:
        raise TrackerError(
            "cannot find token_tracker.py. Set NOUGENTRACKER_DIR to the "
            "NouGenTracker checkout on this machine.")
    try:
        result = subprocess.run(
            [sys.executable, "token_tracker.py", *args], cwd=str(root),
            capture_output=True, text=True, timeout=SCAN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise TrackerError(
            f"the tracker scan exceeded {SCAN_TIMEOUT_S}s. Raise "
            "NOUGEN_USAGE_TIMEOUT, or ask for a shorter window.")
    if result.returncode != 0:
        raise TrackerError(
            f"tracker exited {result.returncode}: "
            f"{(result.stderr or '').strip()[:400]}")
    try:
        path.write_text(result.stdout, encoding="utf-8")
    except OSError:
        pass
    return result.stdout, 0.0


# ---------------------------------------------------------------------------
# Report parsing — best effort, and honest when it fails
# ---------------------------------------------------------------------------

_NUM = r"[\d,]+"
_MACHINE_ROW = re.compile(
    rf"^\s+(?P<machine>[a-z0-9][\w.-]*)\s+(?P<input>{_NUM})\s+"
    rf"(?P<output>{_NUM})\s*(?P<cache>{_NUM})\s+\$(?P<spend>[\d,.]+)\s*$")
_MODEL_ROW = re.compile(rf"^\s+\$(?P<spend>[\d,.]+)\s+(?P<model>\S+)")
_DAY_ROW = re.compile(
    rf"^(?P<day>\d{{4}}-\d{{2}}-\d{{2}})\s+(?P<input>{_NUM})\s+"
    rf"(?P<output>{_NUM})\s+(?P<cache>{_NUM})\s+(?P<reasoning>{_NUM})\s*$")


def _int(text: str) -> int:
    return int(text.replace(",", ""))


def _float(text: str) -> float:
    return float(text.replace(",", ""))


def section(text: str, header_contains: str) -> str:
    out: List[str] = []
    grabbing = False
    for line in text.splitlines():
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


def parse_days(text: str) -> List[Dict[str, Any]]:
    days = []
    for line in text.splitlines():
        match = _DAY_ROW.match(line)
        if match:
            days.append({
                "day": match["day"],
                "input_tokens": _int(match["input"]),
                "output_tokens": _int(match["output"]),
                "cache_read_tokens": _int(match["cache"]),
                "reasoning_tokens": _int(match["reasoning"]),
            })
    return days


def parse_machines(text: str) -> List[Dict[str, Any]]:
    machines = []
    for line in text.splitlines():
        if "counter" in line.lower() or "FLEET" in line:
            continue
        match = _MACHINE_ROW.match(line)
        if match:
            machines.append({
                "machine": match["machine"],
                "input_tokens": _int(match["input"]),
                "output_tokens": _int(match["output"]),
                "cache_read_tokens": _int(match["cache"]),
                "spend_usd": _float(match["spend"]),
            })
    return machines


def parse_models(text: str) -> List[Dict[str, Any]]:
    models = []
    for line in text.splitlines():
        match = _MODEL_ROW.match(line)
        if match:
            models.append({"model": match["model"],
                           "spend_usd": _float(match["spend"])})
    return models


ESTIMATED_LANES = ("Antigravity",)


def estimated_note(report: str) -> List[str]:
    """Which lanes in this report are inferred rather than measured."""
    found = []
    for lane in ESTIMATED_LANES:
        if lane.lower() in report.lower():
            found.append(lane)
    if "estimated" in report.lower():
        for match in re.finditer(r"^\s+\$[\d,.]+\s+(\S+)\s+\(estimated\)",
                                 report, re.M):
            found.append(match.group(1))
    return sorted(set(found))


# ---------------------------------------------------------------------------
# Tool results
# ---------------------------------------------------------------------------

DISCLAIMERS = {
    # Say the word "estimated" outright. The agent quoting this will reach for
    # whichever term appears here, and "inferred from text length" reads as a
    # methodology note rather than a warning.
    "estimated": ("These figures are ESTIMATED, not measured: Antigravity does "
                  "not persist exact token counts, so they are inferred from "
                  "text length. Claude Code, Codex and the Gemini CLI report "
                  "exact counts."),
    "shadow": ("Cost is an API-equivalent shadow bill at first-party list "
               "prices, not an invoice. Work running on a subscription bills "
               "nothing extra — the figure measures leverage, not money owed."),
    "cache": ("Cache-reads routinely run ~95% of all tokens while billing at a "
              "fraction of input. A total that looks alarming is usually "
              "cache-read volume; quote the cost beside it."),
}


def _result(text: str, data: Dict[str, Any], age: float,
            notes: Optional[List[str]] = None) -> Tuple[str, Dict[str, Any]]:
    data = dict(data)
    data["cache_age_seconds"] = round(age, 1)
    data["as_of"] = "cached" if age > 1 else "live"
    data["disclaimers"] = notes or []
    return text, data


def tool_my_usage(days: int = DEFAULT_DAYS) -> Tuple[str, Dict[str, Any]]:
    report, age = run_tracker(["--days", str(days)], f"days{days}")
    block = section(report, "Antigravity")
    if not block:
        return _result(
            f"No Antigravity usage found in the last {days} day(s). The "
            "tracker reads ~/.gemini/antigravity*/brain/**/transcript.jsonl; "
            "if this CLI wrote elsewhere, that path is what to check.",
            {"lane": "antigravity", "days": days, "days_detail": []}, age)
    return _result(
        block + "\n\nNOTE: " + DISCLAIMERS["estimated"],
        {"lane": "antigravity", "days": days, "measured": False,
         "days_detail": parse_days(block)},
        age, [DISCLAIMERS["estimated"], DISCLAIMERS["shadow"]])


def tool_machine_usage(days: int = DEFAULT_DAYS) -> Tuple[str, Dict[str, Any]]:
    report, age = run_tracker(["--days", str(days)], f"days{days}")
    parts = [section(report, name) for name in
             ("Claude Code", "Antigravity", "Codex", "Gemini CLI", "Fleet Usage")]
    bill = tail_after(report, "By Model Breakdown", 30)
    text = "\n\n".join(p for p in parts if p)
    if bill:
        text += "\n\n" + bill
    return _result(
        text.strip() or report[:4000],
        {"scope": "machine", "days": days,
         "models": parse_models(bill), "estimated_sources": estimated_note(report)},
        age, [DISCLAIMERS["shadow"], DISCLAIMERS["cache"]])


def tool_fleet_usage(days: Optional[int] = None) -> Tuple[str, Dict[str, Any]]:
    args = ["--fleet"] + (["--days", str(days)] if days else [])
    report, age = run_tracker(args, f"fleet{days or 'all'}")
    block = tail_after(report, "Fleet totals", 45)
    machines = parse_machines(block)
    if not block:
        return _result(
            "No fleet totals available — no machine has published dailies to "
            "this clone yet.", {"scope": "fleet", "machines": []}, age)
    return _result(
        block, {"scope": "fleet", "days": days, "machines": machines,
                "machine_count": len(machines),
                "total_spend_usd": round(sum(m["spend_usd"] for m in machines), 2),
                "models": parse_models(block),
                "estimated_sources": estimated_note(block),
                "caveat": ("Only machines that have PUBLISHED appear here. A "
                           "box that stopped publishing looks exactly like a "
                           "box that stopped spending — name an absent machine "
                           "rather than quoting a smaller total as complete.")},
        age, [DISCLAIMERS["shadow"], DISCLAIMERS["cache"]])


def tool_cost_by_model(days: int = DEFAULT_DAYS) -> Tuple[str, Dict[str, Any]]:
    report, age = run_tracker(["--days", str(days)], f"days{days}")
    block = tail_after(report, "By Model Breakdown", 30)
    return _result(block or "No model breakdown in the window.",
                   {"days": days, "models": parse_models(block)},
                   age, [DISCLAIMERS["shadow"]])


def tool_provenance() -> Tuple[str, Dict[str, Any]]:
    root = tracker_dir()
    data = {
        "tracker_dir": str(root) if root else None,
        "resolved_by": ("NOUGENTRACKER_DIR" if os.environ.get("NOUGENTRACKER_DIR")
                        else "probe"),
        "cache_dir": str(cache_dir()),
        "cache_ttl_seconds": CACHE_TTL_S,
        "scan_timeout_seconds": SCAN_TIMEOUT_S,
        "server_version": VERSION,
        "protocol_versions": list(SUPPORTED_PROTOCOLS),
        "exact_lanes": ["Claude Code", "Codex", "Gemini CLI", "Fleet (Ollama)"],
        "estimated_lanes": ["Antigravity"],
        "antigravity_source":
            "~/.gemini/antigravity*/brain/**/transcript.jsonl",
    }
    return json.dumps(data, indent=2), data


ToolFn = Callable[..., Tuple[str, Dict[str, Any]]]

_DAYS_SCHEMA = {"type": "object", "properties": {
    "days": {"type": "integer", "minimum": 1, "maximum": 3650,
             "description": f"days back from now (default {DEFAULT_DAYS})"}},
    "additionalProperties": False}

_OUT = {"type": "object", "properties": {
    "cache_age_seconds": {"type": "number"},
    "as_of": {"type": "string", "enum": ["live", "cached"]},
    "disclaimers": {"type": "array", "items": {"type": "string"}}}}

TOOLS: Dict[str, Dict[str, Any]] = {
    "my_token_usage": {
        "fn": tool_my_usage,
        "title": "My token usage",
        "description": (
            "How many tokens THIS CLI has used, by day. Use when asked 'how "
            "many tokens have I used', 'what have I spent', or about your own "
            "consumption. Never answer such a question by estimating from "
            "conversation length — call this."),
        "schema": _DAYS_SCHEMA,
    },
    "machine_token_usage": {
        "fn": tool_machine_usage,
        "title": "This machine's usage",
        "description": (
            "Every AI lane on THIS machine — Claude Code, Antigravity, Codex, "
            "Gemini CLI and the local Ollama fleet — plus the shadow bill. "
            "Use for 'what has this computer spent'."),
        "schema": _DAYS_SCHEMA,
    },
    "fleet_token_usage": {
        "fn": tool_fleet_usage,
        "title": "Fleet usage",
        "description": (
            "Totals across EVERY machine that has published, with per-machine "
            "spend. Use for 'the whole fleet', 'all my computers', or when "
            "comparing machines."),
        "schema": {"type": "object", "properties": {
            "days": {"type": "integer", "minimum": 1, "maximum": 3650,
                     "description": "days back; omit for all published history"}},
            "additionalProperties": False},
    },
    "token_cost_by_model": {
        "fn": tool_cost_by_model,
        "title": "Cost by model",
        "description": ("Spend broken down by model at first-party list "
                        "prices. Use for 'which model is costing the most'."),
        "schema": _DAYS_SCHEMA,
    },
    "token_usage_provenance": {
        "fn": tool_provenance,
        "title": "Where these numbers come from",
        "description": (
            "Tracker location, how it was resolved, cache settings, and which "
            "lanes are estimated rather than measured. Use when a figure looks "
            "wrong or needs sourcing."),
        "schema": {"type": "object", "properties": {},
                   "additionalProperties": False},
    },
}

#: Every tool reads. None mutate, none reach the network beyond the local
#: filesystem. Declaring that lets a client skip a confirmation prompt it would
#: otherwise be right to show.
ANNOTATIONS = {"readOnlyHint": True, "destructiveHint": False,
               "idempotentHint": True, "openWorldHint": False}

RESOURCES = [{
    "uri": "nougen://usage/provenance",
    "name": "Token usage provenance",
    "description": "Which lanes are measured, which are estimated, and where "
                   "the tracker lives on this machine.",
    "mimeType": "application/json",
}]


def tool_descriptor(name: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": name,
        "title": spec["title"],
        "description": spec["description"],
        "inputSchema": spec["schema"],
        "outputSchema": _OUT,
        "annotations": dict(ANNOTATIONS, title=spec["title"]),
    }


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 over stdio
# ---------------------------------------------------------------------------

PARSE_ERROR, INVALID_REQUEST = -32700, -32600
METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL = -32601, -32602, -32603


def send(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def respond(msg_id: Any, result: Any = None,
            error: Optional[Dict[str, Any]] = None) -> None:
    payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    send(payload)


def negotiate(requested: Optional[str]) -> str:
    if requested in SUPPORTED_PROTOCOLS:
        return requested
    if requested:
        log(f"client asked for protocol {requested}; answering "
            f"{PREFERRED_PROTOCOL}")
    return PREFERRED_PROTOCOL


def call_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    spec = TOOLS.get(name)
    if spec is None:
        raise KeyError(name)
    allowed = set((spec["schema"].get("properties") or {}))
    unknown = set(args) - allowed
    if unknown:
        raise ValueError(f"unknown argument(s): {', '.join(sorted(unknown))}")
    text, data = spec["fn"](**args)
    return {"content": [{"type": "text", "text": text}],
            "structuredContent": data, "isError": False}


def handle(message: Dict[str, Any]) -> None:
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        respond(msg_id, {
            "protocolVersion": negotiate(params.get("protocolVersion")),
            "capabilities": {"tools": {"listChanged": False},
                             "resources": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": VERSION,
                           "title": "NouGen token usage"},
            "instructions": (
                "Answer token, spend and burn-rate questions with these tools "
                "rather than estimating. State that Antigravity figures are "
                "estimated, that cost is a shadow bill and not an invoice, and "
                "that cache-reads dominate the token count but not the cost."),
        })
    elif method in ("notifications/initialized", "initialized", "ping"):
        if method == "ping" and msg_id is not None:
            respond(msg_id, {})
    elif method == "tools/list":
        respond(msg_id, {"tools": [tool_descriptor(n, s)
                                   for n, s in TOOLS.items()]})
    elif method == "resources/list":
        respond(msg_id, {"resources": RESOURCES})
    elif method == "resources/read":
        uri = params.get("uri")
        if uri != RESOURCES[0]["uri"]:
            respond(msg_id, error={"code": INVALID_PARAMS,
                                   "message": f"unknown resource {uri}"})
            return
        text, _ = tool_provenance()
        respond(msg_id, {"contents": [{"uri": uri, "mimeType": "application/json",
                                       "text": text}]})
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            respond(msg_id, call_tool(name, args))
        except KeyError:
            respond(msg_id, error={"code": METHOD_NOT_FOUND,
                                   "message": f"unknown tool {name}"})
        except ValueError as exc:
            respond(msg_id, error={"code": INVALID_PARAMS, "message": str(exc)})
        except TrackerError as exc:
            # A tool-level failure is content with isError, not a protocol
            # error: the agent should be able to tell the user what broke.
            respond(msg_id, {"content": [{"type": "text",
                                          "text": f"Could not answer: {exc}"}],
                             "isError": True})
        except Exception as exc:  # never take the CLI down over a usage question
            log(f"unhandled in {name}: {exc!r}")
            respond(msg_id, {"content": [{"type": "text",
                                          "text": f"Could not answer: {exc}"}],
                             "isError": True})
    elif msg_id is not None:
        respond(msg_id, error={"code": METHOD_NOT_FOUND,
                               "message": f"unsupported method {method}"})


def serve() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None,
                  "error": {"code": PARSE_ERROR, "message": "invalid JSON"}})
            continue
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            send({"jsonrpc": "2.0", "id": None,
                  "error": {"code": INVALID_REQUEST,
                            "message": "expected a JSON-RPC 2.0 object"}})
            continue
        try:
            handle(message)
        except Exception as exc:
            log(f"fatal in handler: {exc!r}")
            if message.get("id") is not None:
                respond(message["id"],
                        error={"code": INTERNAL, "message": str(exc)})
    return 0


def selftest() -> int:
    """Exercise the surface without a client. Exit non-zero on any failure."""
    failures: List[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        print(f"  {'PASS' if condition else 'FAIL'}  {label}"
              + (f"  — {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(label)

    print(f"{SERVER_NAME} {VERSION} selftest")
    root = tracker_dir()
    check("tracker resolves", root is not None,
          "set NOUGENTRACKER_DIR")
    check("protocol negotiation echoes a known version",
          negotiate("2024-11-05") == "2024-11-05")
    check("protocol negotiation falls back for an unknown version",
          negotiate("1999-01-01") == PREFERRED_PROTOCOL)
    descriptors = [tool_descriptor(n, s) for n, s in TOOLS.items()]
    check("every tool declares a schema, output schema and annotations",
          all(d["inputSchema"] and d["outputSchema"]
              and d["annotations"]["readOnlyHint"] for d in descriptors))
    check("tool names are stable", sorted(TOOLS) == sorted([
        "fleet_token_usage", "machine_token_usage", "my_token_usage",
        "token_cost_by_model", "token_usage_provenance"]))
    try:
        call_tool("my_token_usage", {"nonsense": 1})
        check("unknown arguments are rejected", False)
    except ValueError:
        check("unknown arguments are rejected", True)
    try:
        call_tool("no_such_tool", {})
        check("unknown tools are rejected", False)
    except KeyError:
        check("unknown tools are rejected", True)
    out = call_tool("token_usage_provenance", {})
    check("provenance returns structured content",
          isinstance(out.get("structuredContent"), dict)
          and "estimated_lanes" in out["structuredContent"])
    check("parser reads a machine row",
          parse_machines("  blade1tb   351,617,109  16,693,84310,905,469,872   "
                         "$5,552.69")[0]["spend_usd"] == 5552.69)
    check("parser skips counter and FLEET rows",
          parse_machines("  counter abc (current) 1,2 3,4 5,6 $1.00\n"
                         "  FLEET 1,2 3,4 5,6 $9.99") == [])
    check("parser reads a day row",
          parse_days("2026-07-31           1,854       748,873     "
                     "259,307,728             0")[0]["output_tokens"] == 748873)
    if root is not None:
        try:
            text, data = tool_fleet_usage()
            check("fleet tool answers", bool(text))
            check("fleet answer carries provenance",
                  "cache_age_seconds" in data and "as_of" in data)
        except TrackerError as exc:
            check("fleet tool answers", False, str(exc))
    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# Installation — wire every MCP-speaking CLI on this machine
# ---------------------------------------------------------------------------

#: Where each CLI keeps its MCP registry. Probed for existence at install time;
#: a CLI that is not installed is skipped rather than assumed.
TARGETS: Tuple[Tuple[str, str, str], ...] = (
    ("antigravity", "~/.gemini/config/mcp_config.json", "json"),
    ("gemini-cli", "~/.gemini/settings.json", "json"),
    ("claude-code", "~/.claude.json", "json"),
    ("codex", "~/.codex/config.toml", "toml"),
)


def _server_entry() -> Dict[str, Any]:
    env: Dict[str, str] = {}
    root = tracker_dir()
    if root is not None:
        env["NOUGENTRACKER_DIR"] = str(root)
    entry: Dict[str, Any] = {
        "command": sys.executable,
        "args": [str(Path(__file__).resolve())],
    }
    if env:
        entry["env"] = env
    return entry


def _backup(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    backup = path.with_suffix(path.suffix + f".bak-nougen-{stamp}")
    backup.write_bytes(path.read_bytes())
    return backup


def _install_json(path: Path, dry_run: bool) -> str:
    data: Dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            return f"SKIP  {path} is not valid JSON ({exc}) — refusing to touch it"
        if not dry_run:
            _backup(path)
    servers = data.setdefault("mcpServers", {})
    existing = len(servers)
    action = "update" if SERVER_NAME in servers else "add"
    servers[SERVER_NAME] = _server_entry()
    if dry_run:
        return f"WOULD {action}  {path}  ({existing} server(s) already there)"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return f"{action.upper()}  {path}  ({existing} preserved)"


def _install_toml(path: Path, dry_run: bool) -> str:
    """Codex keeps MCP servers in TOML.

    Written by hand rather than with a TOML library: tomllib is read-only and
    3.11+, and adding a writer dependency would break the stdlib-only promise
    that lets this run wherever a CLI spawns it. The edit is append-only and
    idempotent, which is the whole reason that is safe.
    """
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    header = f"[mcp_servers.{SERVER_NAME}]"
    if header in text:
        return f"SKIP  {path} already has {header} — edit it by hand to change"
    root = tracker_dir()
    block = [
        "", f"# {SERVER_NAME}: lets this CLI answer its own token questions",
        header,
        f'command = {json.dumps(sys.executable)}',
        f'args = [{json.dumps(str(Path(__file__).resolve()))}]',
    ]
    if root is not None:
        block += [f"[mcp_servers.{SERVER_NAME}.env]",
                  f'NOUGENTRACKER_DIR = {json.dumps(str(root))}']
    if dry_run:
        return f"WOULD add  {path}  ({header})"
    if path.exists():
        _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip("\n") + "\n" + "\n".join(block) + "\n",
                    encoding="utf-8")
    return f"ADD  {path}  ({header})"


def install(dry_run: bool = False) -> int:
    print(f"{SERVER_NAME} {VERSION} — installing into every MCP-speaking CLI"
          + (" (dry run)" if dry_run else ""))
    root = tracker_dir()
    print(f"tracker: {root or 'UNRESOLVED — set NOUGENTRACKER_DIR'}\n")
    touched = 0
    for name, raw, fmt in TARGETS:
        path = Path(os.path.expanduser(raw))
        if not path.exists() and fmt == "json":
            print(f"skip   {name:<13} {path} not present")
            continue
        if not path.exists() and fmt == "toml":
            print(f"skip   {name:<13} {path} not present")
            continue
        result = (_install_json(path, dry_run) if fmt == "json"
                  else _install_toml(path, dry_run))
        print(f"{name:<13} {result}")
        touched += 1
    print(f"\n{touched} CLI config(s) processed. Restart each CLI to pick the "
          "server up.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--selftest", action="store_true",
                        help="exercise the tool surface and exit")
    parser.add_argument("--install", action="store_true",
                        help="register this server with every MCP-speaking CLI "
                             "found on this machine (backs up each config)")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --install, report what would change")
    parser.add_argument("--version", action="version",
                        version=f"{SERVER_NAME} {VERSION}")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.install:
        return install(dry_run=args.dry_run)
    log(f"ready — tracker {tracker_dir() or 'UNRESOLVED'}")
    return serve()


if __name__ == "__main__":
    sys.exit(main())
