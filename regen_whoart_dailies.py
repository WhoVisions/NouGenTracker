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


def main():
    day, mdl = collect()
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
        rec["estimated"] = {k: 0 for k in
                            ("cache_creation", "cache_read", "input_tokens",
                             "output_tokens", "reasoning")}
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
            "source": "~/.claude/projects/**/*.jsonl",
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
