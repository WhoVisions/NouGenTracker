"""Per-machine daily token rollups, carried by git.

token_tracker.py answers "what did *this* box spend?" by reading the logs each
agent already writes. That answer never leaves the machine it was computed on,
so the fleet-wide question — "what did I spend across every box?" — had no
answer at all.

This module is the transport. Each machine exports its own days to
`dailies/<machine>/<YYYY-MM-DD>.json`, commits them, and pushes. Every other
machine pulls and sums. There is no server and no daemon; git is the bus, the
same way NouGenRelay carries handoffs.

Design rules, inherited from the fleet usage ledger:
- One file per (machine, day). Re-exporting a day REPLACES that file, so a
  re-run can never double-count. Machines only ever write their own directory,
  which is what keeps concurrent pushes from conflicting.
- Integers and model names only. Never session text, paths, or secrets — these
  files land in a public repo.
- Today is exported as `partial`, because the day is not over. Aggregation
  reports partial days separately instead of quietly mixing them into totals.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import socket
import statistics
import subprocess
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCHEMA_VERSION = 2

# Bottom-k MinHash width. 64 gives a Jaccard standard error of ~1/sqrt(64) =
# 12.5%, which cleanly separates "independent machines" (~0) from "same logs
# read twice" (~1) while costing ~0.5 KB per machine-day.
MINHASH_K = 64

# Iglewicz & Hoaglin's cutoff for the modified z-score. Their recommendation is
# 3.5; token usage is heavy-tailed and bursty, so a robust score is the right
# tool and the standard threshold is the right place to start.
OUTLIER_Z = 3.5

REPO_ROOT = Path(__file__).resolve().parent
DAILIES_DIR = REPO_ROOT / "dailies"

# The token fields every record carries. Kept explicit rather than derived so a
# new field in token_tracker.py cannot silently change the on-disk schema.
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation",
    "cache_read",
    "reasoning",
)


# --- identity -------------------------------------------------------------
#
# Resolved exactly the way NouGenRelay resolves it, so one grep for a machine
# name finds its commits, its handoffs and its dailies. Divergence here would
# silently split one box's history in two.

def slugify_machine(name: str) -> str:
    """Lowercase, non-alphanumerics to hyphens, no trailing hyphen."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug or "unknown-machine"


def resolve_machine() -> str:
    """NOUGEN_MACHINE wins; otherwise the hostname."""
    explicit = os.environ.get("NOUGEN_MACHINE", "").strip()
    if explicit:
        return slugify_machine(explicit)
    try:
        return slugify_machine(socket.gethostname())
    except Exception:
        return "unknown-machine"


def resolve_agent() -> str:
    """Which lane wrote this. No OS equivalent, so it must be declared."""
    return slugify_machine(os.environ.get("NOUGEN_AGENT", "").strip() or "unknown-agent")


# --- export ---------------------------------------------------------------

def known_machines(dailies_dir: Optional[Path] = None) -> List[str]:
    """Machines that have already published into this clone."""
    root = dailies_dir or DAILIES_DIR
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def unintroduced_machine_warning(
    machine: Optional[str] = None, dailies_dir: Optional[Path] = None
) -> Optional[str]:
    """Flag a machine name the fleet has never seen. None if fine.

    This is the check that stops one box from being counted twice under two
    names. NouGenRelay learned it the hard way: `who-mac-mini`, `phoebus` and
    `kushboygroups-mac-mini-local` are all the same Mac, and a hostname change
    is enough to fork a machine's history in two. Totals summed across a split
    identity are wrong in the direction that looks plausible, which is the
    worst direction to be wrong in.

    It warns rather than refuses — a box's first export is legitimately a new
    name, and there is no human at the keyboard to ask.
    """
    machine = machine or resolve_machine()
    known = known_machines(dailies_dir)
    if not known or machine in known:
        return None
    return (
        f"'{machine}' has never published dailies here. Known: {', '.join(known)}. "
        "If this box already has a fleet name, export under it: "
        f"NOUGEN_MACHINE=<name>. Otherwise this becomes a new machine."
    )


def _day_of(invocation: Dict[str, Any]) -> Optional[str]:
    ts = invocation.get("timestamp")
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d")
    if isinstance(ts, str) and len(ts) >= 10:
        return ts[:10]
    return None


def fingerprint(invocation: Dict[str, Any]) -> int:
    """A stable 64-bit id for one model call.

    Two machines that read the same synced log directory would otherwise each
    report the same call, and a plain sum would silently double it. Hashing the
    fields that identify a call — when, which model, how many tokens — lets the
    aggregator recognise the overlap instead of assuming the machines are
    independent.
    """
    parts = [
        str(invocation.get("timestamp")),
        str(invocation.get("model")),
        str(invocation.get("session_id")),
    ] + [str(invocation.get(f) or 0) for f in TOKEN_FIELDS]
    digest = hashlib.blake2b("\x1f".join(parts).encode("utf-8"), digest_size=8)
    return int.from_bytes(digest.digest(), "big")


def minhash(fingerprints: Iterable[int], k: int = MINHASH_K) -> List[int]:
    """Bottom-k MinHash sketch of a set of fingerprints.

    Storing every fingerprint would multiply a daily file by its invocation
    count (500+ on a busy day, in a public repo). A bottom-k sketch is ~k
    integers regardless of set size, and the Jaccard similarity of two sketches
    estimates the true overlap of the underlying sets with standard error
    ~1/sqrt(k) — 12.5% at k=64, which is far below the threshold that matters
    here. Broder's original construction; the estimator is unbiased.
    """
    return sorted(set(fingerprints))[:k]


def jaccard(sketch_a: List[int], sketch_b: List[int], k: int = MINHASH_K) -> float:
    """Estimate |A ∩ B| / |A ∪ B| from two bottom-k sketches.

    The union sketch is the bottom-k of the merged sketches; the estimate is
    the fraction of those k that both sides carry. Valid because bottom-k of a
    union is computable from the two bottom-k sketches alone.
    """
    if not sketch_a or not sketch_b:
        return 0.0
    union = sorted(set(sketch_a) | set(sketch_b))[:k]
    if not union:
        return 0.0
    both = set(sketch_a) & set(sketch_b)
    return sum(1 for v in union if v in both) / len(union)


def rollup(invocations: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Collapse raw invocations into per-day, per-source token totals.

    Model detail is kept as a per-day breakdown because that is what makes a
    fleet total actionable — "we spent 4M tokens" is trivia, "we spent 4M on
    opus across three boxes" is a decision.

    Exact and estimated counts are tracked separately rather than added
    together. Some providers report usage; others are inferred from text
    length, and folding a guess into a measurement produces a total that cannot
    be reasoned about. Keeping the split lets a reader see how much of any
    number is actually known.
    """
    days: Dict[str, Dict[str, Any]] = {}
    for inv in invocations:
        day = _day_of(inv)
        if not day:
            continue
        bucket = days.setdefault(
            day,
            {"sources": defaultdict(lambda: defaultdict(int)),
             "models": defaultdict(lambda: defaultdict(int)),
             "exact": defaultdict(int),
             "estimated": defaultdict(int),
             "fingerprints": [],
             "invocations": 0},
        )
        source = str(inv.get("source") or "unknown")
        model = str(inv.get("model") or "unknown")
        # `exact` is set by the parsers: True where the provider reported usage,
        # False where the tracker inferred it.
        confidence = "exact" if inv.get("exact") else "estimated"
        for field in TOKEN_FIELDS:
            value = int(inv.get(field) or 0)
            bucket["sources"][source][field] += value
            bucket["models"][model][field] += value
            bucket[confidence][field] += value
        bucket["fingerprints"].append(fingerprint(inv))
        bucket["invocations"] += 1

    # defaultdicts serialise fine but compare badly in tests; freeze them.
    return {
        day: {
            "sources": {s: dict(f) for s, f in b["sources"].items()},
            "models": {m: dict(f) for m, f in b["models"].items()},
            "exact": {f: b["exact"].get(f, 0) for f in TOKEN_FIELDS},
            "estimated": {f: b["estimated"].get(f, 0) for f in TOKEN_FIELDS},
            "sketch": minhash(b["fingerprints"]),
            "invocations": b["invocations"],
        }
        for day, b in days.items()
    }


def totals_of(breakdown: Dict[str, Dict[str, int]]) -> Dict[str, int]:
    """Sum a {name: {field: n}} breakdown down to one {field: n}."""
    out = {field: 0 for field in TOKEN_FIELDS}
    for fields in breakdown.values():
        for field in TOKEN_FIELDS:
            out[field] += int(fields.get(field) or 0)
    return out


def export_days(
    invocations: Iterable[Dict[str, Any]],
    machine: Optional[str] = None,
    today: Optional[date] = None,
    dailies_dir: Optional[Path] = None,
) -> List[Path]:
    """Write one file per day this machine has usage for. Returns paths written."""
    machine = machine or resolve_machine()
    today = today or date.today()
    root = (dailies_dir or DAILIES_DIR) / machine
    root.mkdir(parents=True, exist_ok=True)

    written: List[Path] = []
    for day, bucket in sorted(rollup(invocations).items()):
        record = {
            "schema": SCHEMA_VERSION,
            "machine": machine,
            "date": day,
            # A day is only final once it is over. Anything else would let a
            # mid-afternoon export freeze an incomplete day as authoritative.
            "partial": day >= today.strftime("%Y-%m-%d"),
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "generated_by": resolve_agent(),
            "invocations": bucket["invocations"],
            "totals": totals_of(bucket["sources"]),
            "exact": bucket["exact"],
            "estimated": bucket["estimated"],
            # Hex, because JSON numbers above 2^53 are not safely round-trippable.
            "sketch": [f"{v:016x}" for v in bucket["sketch"]],
            "sources": bucket["sources"],
            "models": bucket["models"],
        }
        path = root / f"{day}.json"
        # Replace, never append: one file per (machine, day) is what makes a
        # re-export idempotent.
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written


# --- aggregate ------------------------------------------------------------

def load_fleet(dailies_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Every daily record every machine has published into this clone."""
    root = dailies_dir or DAILIES_DIR
    if not root.exists():
        return []

    records: List[Dict[str, Any]] = []
    for path in sorted(root.glob("*/*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A corrupt or half-pushed file must not take the whole report
            # down; the fleet is only as reliable as its worst clone.
            continue
        if not isinstance(record, dict) or record.get("schema") != SCHEMA_VERSION:
            continue
        # Trust the directory over the field: the path is what git merged, the
        # field is what some other box claimed about itself.
        record["machine"] = path.parent.name
        records.append(record)
    return records


def aggregate(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Fold published dailies into fleet totals, per machine and per day."""
    by_machine: Dict[str, Dict[str, int]] = {}
    by_day: Dict[str, Dict[str, int]] = {}
    by_source: Dict[str, Dict[str, int]] = {}
    exact_totals: Dict[str, int] = {f: 0 for f in TOKEN_FIELDS}
    estimated_totals: Dict[str, int] = {f: 0 for f in TOKEN_FIELDS}
    partial: List[Tuple[str, str]] = []

    # One machine, one day, one truth. Resolve every duplicate BEFORE summing —
    # deciding as we go would leave a superseded record already added to the
    # totals with no way to take it back out.
    winners: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for record in records:
        key = (record.get("machine", "unknown-machine"), record.get("date", ""))
        stamp = str(record.get("generated_at") or "")
        incumbent = winners.get(key)
        if incumbent is None or stamp > str(incumbent.get("generated_at") or ""):
            winners[key] = record

    for (machine, day), record in winners.items():
        totals = {f: int(record.get("totals", {}).get(f) or 0) for f in TOKEN_FIELDS}
        for target, name in ((by_machine, machine), (by_day, day)):
            slot = target.setdefault(name, {f: 0 for f in TOKEN_FIELDS})
            for field in TOKEN_FIELDS:
                slot[field] += totals[field]
        for source, fields in (record.get("sources") or {}).items():
            slot = by_source.setdefault(source, {f: 0 for f in TOKEN_FIELDS})
            for field in TOKEN_FIELDS:
                slot[field] += int(fields.get(field) or 0)
        for name, target in (("exact", exact_totals), ("estimated", estimated_totals)):
            fields = record.get(name) or {}
            for field in TOKEN_FIELDS:
                target[field] += int(fields.get(field) or 0)
        if record.get("partial"):
            partial.append((machine, day))

    winner_list = list(winners.items())
    return {
        "machines": by_machine,
        "days": by_day,
        "sources": by_source,
        "totals": totals_of(by_machine),
        "exact": exact_totals,
        "estimated": estimated_totals,
        "confidence": _confidence(exact_totals, estimated_totals),
        "overlaps": detect_overlaps(winner_list),
        "anomalies": detect_anomalies(by_day),
        "partial": sorted(partial),
        "machine_count": len(by_machine),
        "day_count": len(by_day),
    }


def _confidence(exact: Dict[str, int], estimated: Dict[str, int]) -> float:
    """Share of the fleet total that was measured rather than inferred."""
    measured = sum(exact.values())
    inferred = sum(estimated.values())
    return measured / (measured + inferred) if (measured + inferred) else 1.0


def detect_overlaps(
    winners: List[Tuple[Tuple[str, str], Dict[str, Any]]], threshold: float = 0.5
) -> List[Dict[str, Any]]:
    """Machine-days whose sketches say two boxes read the same calls.

    Independent machines share essentially no fingerprints, so any material
    Jaccard similarity means the same underlying log was counted twice — the
    one failure mode that inflates a fleet total while every individual file
    looks correct.
    """
    by_day: Dict[str, List[Tuple[str, List[int]]]] = defaultdict(list)
    for (machine, day), record in winners:
        raw = record.get("sketch") or []
        try:
            sketch = [int(v, 16) if isinstance(v, str) else int(v) for v in raw]
        except (TypeError, ValueError):
            continue
        if sketch:
            by_day[day].append((machine, sketch))

    found: List[Dict[str, Any]] = []
    for day, entries in by_day.items():
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                score = jaccard(entries[i][1], entries[j][1])
                if score >= threshold:
                    found.append({
                        "date": day,
                        "machines": sorted((entries[i][0], entries[j][0])),
                        "jaccard": round(score, 3),
                    })
    return sorted(found, key=lambda d: (-d["jaccard"], d["date"]))


def detect_anomalies(by_day: Dict[str, Dict[str, int]]) -> List[Dict[str, Any]]:
    """Days whose spend is a genuine outlier, by modified z-score in log space.

    Two choices matter here, and both were measured against this repo's own
    history rather than assumed.

    First, robust statistics. Mean and standard deviation are the wrong tools:
    usage is bursty, and a single spike drags both, so the spike ends up hiding
    itself. The modified z-score (Iglewicz & Hoaglin) uses the median and the
    median absolute deviation, and neither moves when one day explodes.

        z = 0.6745 * (x - median) / MAD

    The 0.6745 factor makes MAD a consistent estimator of sigma for normal
    data, so the familiar 3.5 cutoff keeps its usual meaning.

    Second, log space. Daily token counts here span 3.3e5 to 1.8e8 — three
    orders of magnitude — because usage is multiplicative: an active day is a
    *multiple* of an idle one, not a fixed amount above it. On raw values that
    spread inflates MAD until nothing can clear the threshold (max |z| = 1.81
    across this fleet's 17 days, flagging nothing). On log10 values the same
    series peaks at |z| = 5.23. Logs also make the test symmetric in the way
    the domain actually is: a day at a hundredth of typical is exactly as
    notable as one at a hundred times, which is false on a linear scale.
    """
    series = [(day, sum(t.values())) for day, t in sorted(by_day.items())]
    # Below ~7 points the median and MAD are too unstable to call anything an
    # outlier; saying nothing beats saying something wrong.
    if len(series) < 7:
        return []

    # +1 keeps a zero-token day finite; at these magnitudes it changes nothing.
    values = [math.log10(v + 1) for _, v in series]
    median = statistics.median(values)
    deviations = [abs(v - median) for v in values]
    mad = statistics.median(deviations)
    if mad == 0:
        # A flat majority: fall back to the mean absolute deviation, which the
        # same authors recommend when MAD degenerates.
        mad = statistics.mean(deviations) / 0.7979 if any(deviations) else 0.0
    if mad == 0:
        return []

    out = []
    for (day, raw), value in zip(series, values):
        z = 0.6745 * (value - median) / mad
        if abs(z) >= OUTLIER_Z:
            out.append({
                "date": day,
                "tokens": raw,          # report the real count, not its log
                "z": round(z, 1),
                # How many times typical this day was — the number a reader
                # actually wants, recovered from log space.
                "ratio": round(10 ** (value - median), 2),
                "direction": "spike" if z > 0 else "trough",
            })
    return sorted(out, key=lambda d: -abs(d["z"]))


# --- git transport --------------------------------------------------------

def _git(*args: str, cwd: Optional[Path] = None) -> Tuple[int, str]:
    try:
        proc = subprocess.run(
            ("git",) + args,
            cwd=str(cwd or REPO_ROOT),
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def install_hooks(repo_root: Optional[Path] = None) -> Tuple[bool, str]:
    """Point this clone at the tracked hooks directory.

    `core.hooksPath` lives in .git/config, which is per-clone and cannot be
    committed — so every fresh clone starts unhooked no matter what is baked
    into the repo. This is the one command that closes that gap, and nothing
    else is allowed to depend on someone having run it.
    """
    root = repo_root or REPO_ROOT
    if not (root / ".githooks" / "prepare-commit-msg").exists():
        return False, "no .githooks/prepare-commit-msg in this repo"
    code, out = _git("config", "core.hooksPath", ".githooks", cwd=root)
    if code != 0:
        return False, out or "git config failed"
    return True, "core.hooksPath -> .githooks"


def hooks_installed(repo_root: Optional[Path] = None) -> bool:
    root = repo_root or REPO_ROOT
    code, out = _git("config", "core.hooksPath", cwd=root)
    if code != 0 or not out:
        return False
    path = Path(out)
    if not path.is_absolute():
        path = root / path
    return (path / "prepare-commit-msg").exists()


def stamped_commit(subject: str, paths: List[Path], repo_root: Optional[Path] = None) -> Tuple[bool, str]:
    """Commit the given paths with identity already in the message.

    The trailers are written here rather than left to prepare-commit-msg for
    the same reason NouGenRelay writes its own: this function already resolved
    the machine and agent to name the files, so making the stamp depend on a
    per-clone config step would only lose information the caller already had.
    `git interpret-trailers` and this use the same keys, so a hooked clone
    produces no duplicates.
    """
    root = repo_root or REPO_ROOT
    if not paths:
        return False, "nothing to commit"
    code, out = _git("add", "--", *[str(p) for p in paths], cwd=root)
    if code != 0:
        return False, out
    code, out = _git("diff", "--cached", "--quiet", cwd=root)
    if code == 0:
        return False, "no changes to commit"
    message = f"{subject}\n\nMachine: {resolve_machine()}\nAgent: {resolve_agent()}\n"
    return _git("commit", "-m", message, cwd=root)[0] == 0, "committed"
