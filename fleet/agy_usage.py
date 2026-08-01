"""Antigravity's `agy` CLI as a MEASURED fleet lane.

The Antigravity lane is the worst-counted thing in this report. `parse_antigravity`
recovers what it can over RPC and otherwise falls back to chars/4, which is why
the fleet reads "37.6% of tokens measured" against "76.1% of dollars measured" —
8.1B cheap ESTIMATED tokens dragging the token share down.

`agy -p --output-format json` reports usage exactly:

    {"conversation_id": "...", "status": "SUCCESS", "response": "391\\n",
     "usage": {"input_tokens": 31715, "output_tokens": 695,
               "thinking_tokens": 689, "cache_read_tokens": 0,
               "total_tokens": 32410}}

So every Gemini question asked THROUGH this module lands in the fleet ledger as
an exact row (`parse_fleet_usage` stamps ledger rows `exact: True`) instead of
being estimated after the fact. It does not retro-fix the 8.1B already logged by
the IDE — nothing can — it stops the estimated bucket from growing.

Deliberately NOT in COUNTING_SURFACE: this writes ledger rows, it does not
change how any row is counted, so adding the lane leaves the counting version
where it was and does not stale anyone's published machine-days.

CLI:
    python fleet/agy_usage.py "how many tokens did that cost?"
    python fleet/agy_usage.py --model gemini-3.1-pro-high --effort high "..."
    python fleet/agy_usage.py --no-log --json "..."
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from .fleet_usage_log import log_fleet_usage
except ImportError:  # run as a script, not a package
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from fleet_usage_log import log_fleet_usage

# `agy models` also offers gemini-3.6-*, but token_tracker has no documented
# list price for the 3.6 family yet, so those rows would bill at DEFAULT_PRICING
# and re-introduce an estimate on the money side of a row whose tokens are
# exact. Default to a model priced from first-party docs; callers may override.
DEFAULT_MODEL = "gemini-3.5-flash-high"

PROVIDER = "Antigravity"
LANE = "agy-cli"

# Antigravity installs the CLI outside PATH on Windows often enough to be worth
# a fallback rather than a confusing "not found".
_FALLBACK_BINARIES = (
    os.path.expanduser(r"~\AppData\Local\agy\bin\agy.exe"),
    os.path.expanduser("~/.local/bin/agy"),
)


class AgyError(RuntimeError):
    """agy could not be run, or did not return a usable result."""


def agy_binary():
    """Path to the agy executable, or None if this box has no Antigravity CLI."""
    found = shutil.which("agy")
    if found:
        return found
    for candidate in _FALLBACK_BINARIES:
        if os.path.isfile(candidate):
            return candidate
    return None


def split_thinking(output_tokens, thinking_tokens):
    """Split agy's `output_tokens` into (visible output, reasoning).

    agy folds thinking INTO output — verified twice on 2026-08-01, both times
    exactly: input 32861 + output 28 == total 32889 with thinking 24, and
    input 31715 + output 695 == total 32410 with thinking 689 (the reply "391"
    being the 6-token remainder). thinking is never a separate addend.

    This matters for money, not tidiness: model_bill() charges
    (output_tokens + reasoning_tokens) at the output rate, so logging both as
    reported would bill the thinking twice. Splitting bills once and still
    shows reasoning in its own column.

    Falls back to (output, 0) if the invariant ever stops holding, which
    under-reports reasoning rather than over-charging for it.
    """
    out = int(output_tokens or 0)
    think = int(thinking_tokens or 0)
    if think <= 0 or think > out:
        return out, 0
    return out - think, think


# CACHE READS ARE DISJOINT FROM INPUT — do not "fix" this by subtracting.
#
# It looks like the same trap as thinking-inside-output, and it is not. Four
# captures on 2026-08-01, every one of them total == input + output:
#
#     in 32,861 + out     28 == 32,889   cache-read       0
#     in 31,715 + out    695 == 32,410   cache-read       0
#     in 52,543 + out    351 == 52,894   cache-read  12,502   (continued convo)
#     in 107,053 + out 6,139 == 113,192  cache-read 377,419   (tool-using run)
#
# In the last two the cache read is LARGER than the whole input, so it cannot be
# a subset of it — the two counters are independent and agy's total_tokens
# simply omits cache reads. Subtracting would have understated fresh input by
# 12,502 on the third row. Log both as reported.


def parse_agy_json(stdout):
    """The result object out of agy's stdout, or raise AgyError.

    Scans backwards for the last parseable JSON object carrying a `usage` key:
    plugins and update notices can print ahead of the result, and the result is
    always last.
    """
    for line in reversed([ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and "usage" in rec:
            return rec
    raise AgyError("no JSON result with a usage object in agy output")


def ask(prompt, model=DEFAULT_MODEL, effort=None, timeout=300, log=True, cwd=None):
    """Ask agy `prompt`, log the exact usage to the fleet ledger, return the result.

    Returns a dict with `response`, `model`, `conversation_id`, `usage` (the
    ledger-shaped counts, thinking already split out of output) and `logged`.
    Raises AgyError if agy is missing, fails, or reports a non-SUCCESS status.
    """
    binary = agy_binary()
    if not binary:
        raise AgyError("agy CLI not found on this machine (not on PATH, no known install)")

    cmd = [binary, "-p", prompt, "--output-format", "json",
           "--print-timeout", f"{int(timeout)}s"]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout + 30, cwd=cwd,
        )
    except subprocess.TimeoutExpired as exc:
        raise AgyError(f"agy did not answer within {timeout}s") from exc
    except OSError as exc:
        raise AgyError(f"could not run agy: {exc}") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise AgyError(f"agy exited {proc.returncode}: {detail[-1] if detail else 'no output'}")

    rec = parse_agy_json(proc.stdout)
    status = rec.get("status")
    if status and status != "SUCCESS":
        raise AgyError(f"agy status {status}")

    raw = rec.get("usage") or {}
    out, reasoning = split_thinking(raw.get("output_tokens"), raw.get("thinking_tokens"))
    usage = {
        "input_tokens": int(raw.get("input_tokens") or 0),
        "output_tokens": out,
        "cached_tokens": int(raw.get("cache_read_tokens") or 0),
        "reasoning_tokens": reasoning,
    }

    logged = False
    if log:
        # log_fleet_usage swallows its own errors by design, so ask it whether
        # the row landed rather than assuming it did.
        before = _ledger_size()
        log_fleet_usage(
            provider=PROVIDER,
            model=model or "antigravity-default",
            lane=LANE,
            source="agy-cli",
            **usage,
        )
        logged = _ledger_size() > before

    return {
        "response": rec.get("response", ""),
        "model": model or "antigravity-default",
        "conversation_id": rec.get("conversation_id"),
        "duration_seconds": rec.get("duration_seconds"),
        "usage": usage,
        "reported_total": int(raw.get("total_tokens") or 0),
        "logged": logged,
    }


def _ledger_size():
    """Bytes in the ledger right now, 0 if it does not exist yet."""
    try:
        from .fleet_usage_log import _ledger_path
    except ImportError:
        from fleet_usage_log import _ledger_path
    try:
        return Path(_ledger_path()).stat().st_size
    except OSError:
        return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Ask Antigravity's agy CLI a question and count the tokens exactly.",
    )
    ap.add_argument("prompt", help="the question to ask")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"agy model (default {DEFAULT_MODEL})")
    ap.add_argument("--effort", choices=("low", "medium", "high"), help="reasoning effort")
    ap.add_argument("--timeout", type=int, default=300, help="seconds to wait (default 300)")
    ap.add_argument("--no-log", action="store_true", help="do not write a ledger row")
    ap.add_argument("--json", action="store_true", help="print the whole result as JSON")
    args = ap.parse_args(argv)

    try:
        result = ask(args.prompt, model=args.model, effort=args.effort,
                     timeout=args.timeout, log=not args.no_log)
    except AgyError as exc:
        print(f"agy: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(result["response"].rstrip())
    u = result["usage"]
    ledger = "ledger: written" if result["logged"] else (
        "ledger: skipped" if args.no_log else "ledger: NOT written")
    print(
        f"\n[{result['model']}] in {u['input_tokens']:,}  out {u['output_tokens']:,}"
        f"  reasoning {u['reasoning_tokens']:,}  cache-read {u['cached_tokens']:,}"
        f"  | exact, {ledger}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
