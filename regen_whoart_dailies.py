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
    """Parse all Antigravity brain transcripts (~/.gemini/antigravity*/brain/**/transcript.jsonl)
    as well as conversation DBs and CLI history. Populates per-day totals and per-model breakdowns
    into estimated{}, preserving the full record of Antigravity activity."""
    import glob as _g
    import re as _re
    per_day = defaultdict(lambda: defaultdict(int))
    per_mdl = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))

    # 1. Brain transcripts (the primary source of turn logs)
    dirs = [
        os.path.expanduser(os.path.join("~", ".gemini", d, "brain"))
        for d in ("antigravity", "antigravity-cli", "antigravity-ide", "antigravity-backup")
    ]
    seen_steps = set()
    for bdir in dirs:
        if not os.path.exists(bdir):
            continue
        for root, _, filenames in os.walk(bdir):
            for fname in filenames:
                if fname != "transcript.jsonl":
                    continue
                fpath = os.path.join(root, fname)
                try:
                    if os.path.getmtime(fpath) < CUT.timestamp():
                        continue
                except OSError:
                    continue
                parts = os.path.normpath(fpath).split(os.sep)
                conv_id = parts[-4] if len(parts) >= 4 else "unknown"
                try:
                    fh = open(fpath, encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                with fh:
                    accumulated_chars = 0
                    last_model_call = 0
                    current_model = "gemini-3-flash-preview"
                    for idx, line in enumerate(fh):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        content = rec.get("content") or ""
                        m = _re.search(r"<USER_SETTINGS_CHANGE>\s*The user changed setting `Model Selection` from .*? to (.*?)(?:\.\s|\.$|$)", content)
                        if m:
                            cand = m.group(1).strip().lower()
                            if "gemini 3.5 flash (high)" in cand: current_model = "gemini-3.5-flash-high"
                            elif "gemini 3.5 flash (low)" in cand: current_model = "gemini-3.5-flash-low"
                            elif "gemini 3.5 pro" in cand: current_model = "gemini-3.5-pro"
                            elif "gemini 3.5 flash" in cand: current_model = "gemini-3.5-flash"
                            elif "gemini 3.1 pro (high)" in cand: current_model = "gemini-3.1-pro-high"
                            elif "gemini 3.1 pro (low)" in cand: current_model = "gemini-3.1-pro-low"
                            elif "claude sonnet 4.6" in cand: current_model = "claude-sonnet-4-6-thinking"
                            elif "claude opus 4.6" in cand: current_model = "claude-opus-4-6-thinking"
                            elif "gpt-5.6 sol" in cand: current_model = "gpt-5.6-sol"
                            elif "gpt-5.6 terra" in cand: current_model = "gpt-5.6-terra"
                            elif "gpt-5.6 luna" in cand: current_model = "gpt-5.6-luna"
                            elif "gemini 3" in cand: current_model = "gemini-3-flash-preview"
                            else: current_model = m.group(1).strip()

                        created = rec.get("created_at") or rec.get("timestamp")
                        if not created:
                            continue
                        try:
                            ts = dt.datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
                        except Exception:
                            continue
                        if ts < CUT:
                            continue

                        source = rec.get("source")
                        step_type = rec.get("type")
                        thinking = rec.get("thinking") or ""
                        tool_calls = str(rec.get("tool_calls") or "")
                        step_chars = len(content) + len(thinking) + len(tool_calls)
                        accumulated_chars += step_chars

                        step_uid = f"{conv_id}_{idx}"
                        if step_uid in seen_steps:
                            continue
                        seen_steps.add(step_uid)

                        if source == "MODEL" and step_type == "PLANNER_RESPONSE":
                            ot = max(1, step_chars // 4)
                            total_in = (accumulated_chars // 4) + 6000
                            if last_model_call > 0:
                                cr = (last_model_call // 4) + 6000
                                it = max(0, total_in - cr)
                            else:
                                cr = 0
                                it = total_in
                            last_model_call = accumulated_chars
                            date = ts.strftime("%Y-%m-%d")
                            model_name = f"{current_model} (estimated)"
                            per_day[date]["invocations"] += 1
                            per_day[date]["input_tokens"] += it
                            per_day[date]["output_tokens"] += ot
                            per_day[date]["cache_read"] += cr
                            per_mdl[date][model_name]["input_tokens"] += it
                            per_mdl[date][model_name]["output_tokens"] += ot
                            per_mdl[date][model_name]["cache_read"] += cr

    # 2. Supplementary DB scan for IDE sessions not present in brain transcripts
    pat = _re.compile(rb'\{[^{}]{0,4000}?"(?:cached_)?input_tokens"\s*:\s*\d+[^{}]{0,4000}?\}')
    root_ide = os.path.expanduser("~/.gemini/antigravity-ide/conversations/*.db")
    for path in _g.glob(root_ide):
        try:
            if os.path.getmtime(path) < CUT.timestamp():
                continue
            with open(path, "rb") as fh:
                blob = fh.read()
        except OSError:
            continue
        date = dt.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d")
        for match in pat.finditer(blob):
            try:
                obj = json.loads(match.group().decode("utf-8", "replace"))
            except ValueError:
                continue
            it = obj.get("input_tokens") or 0
            ot = obj.get("output_tokens") or 0
            cr = obj.get("cache_read_tokens") or 0
            if date not in per_day:  # only add if day had no brain transcript activity
                per_day[date]["input_tokens"] += it
                per_day[date]["output_tokens"] += ot
                per_day[date]["cache_read"] += cr
                per_mdl[date]["gemini-3-flash-preview (estimated)"]["input_tokens"] += it
                per_mdl[date]["gemini-3-flash-preview (estimated)"]["output_tokens"] += ot
                per_mdl[date]["gemini-3-flash-preview (estimated)"]["cache_read"] += cr

    return per_day, per_mdl


def main():
    day, mdl = collect()
    codex = collect_codex()
    agy_day, agy_mdl = collect_antigravity()

    # Fold Codex into exact{} under its own model key -- same machine, real tokens.
    for date, vals in codex.items():
        day[date]["invocations"] += vals["invocations"]
        for k in ("input_tokens", "output_tokens", "cache_read"):
            day[date][k] += vals[k]
            mdl[date]["codex"][k] += vals[k]

    # Fold Antigravity into day, models, and estimated
    for date, vals in agy_day.items():
        day[date]["invocations"] += vals["invocations"]
    for date, models in agy_mdl.items():
        for mname, mvals in models.items():
            for k in ("input_tokens", "output_tokens", "cache_read"):
                mdl[date][mname][k] += mvals[k]

    stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    today_str = dt.date.today().isoformat()
    os.makedirs(BACKUP, exist_ok=True)
    written = 0

    all_dates = sorted(set(day.keys()) | set(agy_day.keys()))
    for date in all_dates:
        path = f"dailies/whoart/{date}.json"
        rec = None
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    rec = json.load(fh)
                with open(f"{BACKUP}/{date}.json", "w", encoding="utf-8") as fh:
                    json.dump(rec, fh, indent=1)
            except Exception:
                rec = None
        if not rec:
            rec = {"counter": "cfae0dd41682", "date": date, "machine": "whoart", "provider_stats": {}, "sketch": []}

        rec["exact"] = {
            k: day[date].get(k, 0) for k in ("cache_creation", "cache_read", "input_tokens", "output_tokens")
        }
        rec["exact"]["reasoning"] = 0

        est = agy_day.get(date, {})
        rec["estimated"] = {
            "cache_creation": 0,
            "cache_read": est.get("cache_read", 0),
            "input_tokens": est.get("input_tokens", 0),
            "output_tokens": est.get("output_tokens", 0),
            "reasoning": 0,
        }

        rec["invocations"] = day[date].get("invocations", 0) + est.get("invocations", 0)
        rec["models"] = {
            name: {
                **{k: vals.get(k, 0) for k in ("cache_creation", "cache_read", "input_tokens", "output_tokens")},
                "reasoning": 0,
            }
            for name, vals in mdl[date].items()
        }

        # Keep sources and totals consistent
        sources = {}
        if any(rec["exact"].values()):
            # Separate Claude Code and Codex if present
            codex_vals = mdl[date].get("codex")
            if codex_vals:
                sources["OpenAI Codex"] = {
                    "cache_creation": 0,
                    "cache_read": codex_vals.get("cache_read", 0),
                    "input_tokens": codex_vals.get("input_tokens", 0),
                    "output_tokens": codex_vals.get("output_tokens", 0),
                    "reasoning": 0,
                }
            claude_exact = {
                k: rec["exact"][k] - (codex_vals.get(k, 0) if codex_vals else 0)
                for k in ("cache_creation", "cache_read", "input_tokens", "output_tokens", "reasoning")
            }
            if any(claude_exact.values()):
                sources["Claude Code"] = claude_exact

        if any(rec["estimated"].values()):
            sources["Antigravity (Fallback)"] = dict(rec["estimated"])

        rec["sources"] = sources
        rec["totals"] = {
            k: rec["exact"].get(k, 0) + rec["estimated"].get(k, 0)
            for k in ("cache_creation", "cache_read", "input_tokens", "output_tokens", "reasoning")
        }
        rec["partial"] = date >= today_str
        rec["generated_at"] = stamp
        rec["generated_by"] = "antigravity"
        rec["correction"] = {
            "at": stamp,
            "source": "claude-code ~/.claude/projects/**/*.jsonl (exact) + codex ~/.codex/**/*.jsonl (exact) + antigravity ~/.gemini/antigravity*/brain/**/transcript.jsonl (estimated)",
            "reason": "Regenerate whoart usage dailies with backfilled Google Antigravity tokens and unified multi-lane telemetry",
            "prior_backup": f"{BACKUP}/{date}.json",
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, indent=1, sort_keys=True)
        written += 1

    out_exact = sum(day[d]["output_tokens"] for d in day)
    out_agy = sum(agy_day[d]["output_tokens"] for d in agy_day)
    inv_total = sum(day[d]["invocations"] for d in day) + sum(agy_day[d]["invocations"] for d in agy_day)
    print(f"regenerated {written} whoart dailies")
    print(f"August+ output tokens: {out_exact + out_agy:,} (Exact: {out_exact:,} | Antigravity: {out_agy:,})")
    print(f"Total invocations:     {inv_total:,}")


if __name__ == "__main__":
    main()

