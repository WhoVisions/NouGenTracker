"""Regenerate whoart usage dailies from the authoritative local Claude Code transcripts.

The existing dailies captured 36.1% of actual August output tokens (5,254,204 recorded
against 14,553,649 in the transcripts), with invocation counts roughly half. This rebuilds
exact{}, models{} and invocations from ~/.claude/projects/**/*.jsonl and zeroes estimated{}
so the two cannot double-count. Originals are preserved under _pre_correction_20260829/.

Corrects the record, not the cause: whatever generator produced the 36% figures still exists.
"""
import json
import os
import glob
import datetime as dt
from collections import defaultdict

ROOT = os.path.expanduser("~/.claude/projects")
CUT = dt.datetime(2026, 8, 1)
BACKUP = "dailies/whoart/_pre_correction_20260829"
FIELDS = (
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("cache_creation_input_tokens", "cache_creation"),
    ("cache_read_input_tokens", "cache_read"),
)


def collect():
    day = defaultdict(lambda: defaultdict(int))
    mdl = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for path in glob.glob(os.path.join(ROOT, "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(path) < CUT.timestamp():
                continue
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                ts = obj.get("timestamp") or ""
                if not ts:
                    continue
                try:
                    when = dt.datetime.fromisoformat(
                        ts.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
                except ValueError:
                    continue
                if when < CUT:
                    continue
                msg = obj.get("message") or {}
                usage = msg.get("usage") or {}
                if not usage:
                    continue
                date = when.strftime("%Y-%m-%d")
                name = msg.get("model") or "<unknown>"
                day[date]["invocations"] += 1
                for src, dst in FIELDS:
                    val = usage.get(src) or 0
                    day[date][dst] += val
                    mdl[date][name][dst] += val
    return day, mdl



def collect_codex():
    """Codex sessions. Its token_usage is CUMULATIVE per session, so take the max
    per file, never the sum -- summing every record overcounts by ~300x."""
    import glob as _g
    keys = {"input_tokens", "cached_input_tokens", "output_tokens",
            "reasoning_output_tokens"}
    per = defaultdict(lambda: defaultdict(int))

    def dig(obj, out):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in keys and isinstance(v, int):
                    out[k] = max(out.get(k, 0), v)
                elif isinstance(v, (dict, list)):
                    dig(v, out)
        elif isinstance(obj, list):
            for item in obj:
                dig(item, out)

    for path in _g.glob(os.path.expanduser("~/.codex/**/*.jsonl"), recursive=True):
        if os.path.getmtime(path) < CUT.timestamp():
            continue
        best = {}
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"output_tokens"' in line:
                    try:
                        dig(json.loads(line), best)
                    except ValueError:
                        pass
        if not best:
            continue
        date = dt.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d")
        per[date]["invocations"] += 1
        per[date]["input_tokens"] += best.get("input_tokens", 0)
        per[date]["output_tokens"] += best.get("output_tokens", 0)
        per[date]["cache_read"] += best.get("cached_input_tokens", 0)
    return per


def collect_antigravity():
    """Antigravity conversation DBs. Tokens are protobuf-encoded; only the JSON
    fragments embedded in the blobs are parseable, so this is a FLOOR, not a total.
    Goes into estimated{}, never exact{}."""
    import glob as _g
    import re as _re
    pat = _re.compile(
        rb'\{[^{}]{0,4000}?"(?:cached_)?input_tokens"\s*:\s*\d+[^{}]{0,4000}?\}')
    per = defaultdict(lambda: defaultdict(int))
    root = os.path.expanduser("~/.gemini/antigravity-ide/conversations/*.db")
    for path in _g.glob(root):
        if os.path.getmtime(path) < CUT.timestamp():
            continue
        with open(path, "rb") as fh:
            blob = fh.read()
        date = dt.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d")
        for match in pat.finditer(blob):
            try:
                obj = json.loads(match.group().decode("utf-8", "replace"))
            except ValueError:
                continue
            per[date]["input_tokens"] += obj.get("input_tokens") or 0
            per[date]["output_tokens"] += obj.get("output_tokens") or 0
            per[date]["cache_read"] += obj.get("cache_read_tokens") or 0
    return per


def main():
    day, mdl = collect()
    codex = collect_codex()
    agy = collect_antigravity()
    # Fold Codex into exact{} under its own model key -- same machine, real tokens.
    for date, vals in codex.items():
        day[date]["invocations"] += vals["invocations"]
        for k in ("input_tokens", "output_tokens", "cache_read"):
            day[date][k] += vals[k]
            mdl[date]["codex"][k] += vals[k]
    stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    os.makedirs(BACKUP, exist_ok=True)
    written = 0
    for date in sorted(day):
        path = f"dailies/whoart/{date}.json"
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                rec = json.load(fh)
            with open(f"{BACKUP}/{date}.json", "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=1)
        else:
            rec = {"counter": "cfae0dd41682", "date": date, "machine": "whoart"}
        rec["exact"] = {k: day[date][k] for k in
                        ("cache_creation", "cache_read", "input_tokens", "output_tokens")}
        rec["exact"]["reasoning"] = 0
        est = agy.get(date, {})
        rec["estimated"] = {
            "cache_creation": 0,
            "cache_read": est.get("cache_read", 0),
            "input_tokens": est.get("input_tokens", 0),
            "output_tokens": est.get("output_tokens", 0),
            "reasoning": 0,
        }
        rec["invocations"] = day[date]["invocations"]
        rec["models"] = {
            name: {**{k: vals[k] for k in
                      ("cache_creation", "cache_read", "input_tokens", "output_tokens")},
                   "reasoning": 0}
            for name, vals in mdl[date].items()
        }
        rec["generated_at"] = stamp
        rec["generated_by"] = "claude-cli"
        rec["correction"] = {
            "at": stamp,
            "source": "claude-code ~/.claude/projects/**/*.jsonl (exact) + codex ~/.codex/**/*.jsonl (exact, cumulative-max per session) + antigravity conversation blobs (estimated, JSON-fragment floor only)",
            "reason": "dailies captured 36.1% of actual August output tokens; "
                      "regenerated from local transcripts",
            "prior_backup": f"{BACKUP}/{date}.json",
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, indent=1, sort_keys=True)
        written += 1
    out = sum(day[d]["output_tokens"] for d in day)
    inv = sum(day[d]["invocations"] for d in day)
    print(f"regenerated {written} whoart dailies")
    print(f"August output tokens: {out:,} (dailies previously recorded 5,254,204)")
    print(f"August invocations:   {inv:,}")


if __name__ == "__main__":
    main()
