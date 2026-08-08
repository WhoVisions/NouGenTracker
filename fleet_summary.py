#!/usr/bin/env python3
"""Build the fleet dashboard's summary from the committed dailies.

This is the re-cut of blade1tb's relay work (PR #1). The relay transport
itself — rollup files pushed to a side branch — is superseded: the dailies
under ``dailies/<machine>/`` travel with the repo and are already the fleet's
shared record. What was worth rescuing is the page (``dashboard.py``, kept
verbatim) and the summary it renders. This module feeds it from the dailies,
so there is exactly one usage transport in this project.

Field note: dailies store token buckets as ``cache_creation`` / ``cache_read``
/ ``reasoning``; ``token_tracker.model_bill`` prices the API's field names
(``cache_creation_input_tokens`` …). ``_bill`` maps between them — the dailies
are the storage format, the invoice names are the pricing format.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import token_tracker as tt

DAILIES = Path(__file__).resolve().parent / "dailies"
BUCKET_FIELDS = ("input_tokens", "output_tokens", "cache_creation",
                 "cache_read", "reasoning")
STALE_HOURS = float(os.environ.get("NOUGEN_FLEET_STALE_HOURS", "48"))


@dataclass
class Overlap:
    machine_a: str
    machine_b: str
    day_a: str
    day_b: str
    similarity: float


@dataclass
class FleetSummary:
    machines: Dict[str, Dict[str, Any]]
    days: List[str]
    total_cost: float
    total_tokens: int
    cache_read: int
    confidence: Optional[float]
    inferred: List[str]
    overlaps: List[Overlap]
    freshness: List[Tuple[str, Optional[datetime], float, bool]]
    local: str

    @property
    def cache_share(self) -> float:
        return self.cache_read / self.total_tokens if self.total_tokens else 0.0

    @property
    def busiest_day(self) -> Optional[Tuple[str, float]]:
        per_day: Dict[str, float] = defaultdict(float)
        for data in self.machines.values():
            for day, cost in data["days"].items():
                per_day[day] += cost
        if not per_day:
            return None
        return max(per_day.items(), key=lambda kv: kv[1])


def local_machine() -> str:
    """The exporter's naming: NOUGEN_MACHINE, else the slugged hostname."""
    explicit = os.environ.get("NOUGEN_MACHINE", "").strip()
    if explicit:
        return explicit
    try:
        host = socket.gethostname()
    except OSError:
        return "unknown-machine"
    return host.lower().replace(".", "-")


def _bill(model: str, bucket: Dict[str, int], when: str) -> float:
    """Price a dailies bucket with the tracker's invoice-shaped field names."""
    cost, _src = tt.model_bill(model, {
        "input_tokens": bucket.get("input_tokens", 0),
        "output_tokens": bucket.get("output_tokens", 0),
        "cache_creation_input_tokens": bucket.get("cache_creation", 0),
        "cache_read_input_tokens": bucket.get("cache_read", 0),
        "reasoning_tokens": bucket.get("reasoning", 0),
    }, when)
    return cost


def _tokens(bucket: Dict[str, int]) -> int:
    return sum(int(bucket.get(f, 0)) for f in BUCKET_FIELDS)


def _read_dailies(root: Path) -> List[Dict[str, Any]]:
    records = []
    for path in sorted(root.glob("*/[0-9]*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("date") and data.get("machine"):
            records.append(data)
    return records


def fleet_summary(days: Optional[int] = None,
                  root: Path = DAILIES,
                  now: Optional[datetime] = None) -> FleetSummary:
    """Everything the fleet has published, folded into one view.

    Includes THIS machine: a fleet report that omits the box running it is
    the same mistake as a machine that cannot see its peers.
    """
    now = now or datetime.now(timezone.utc)
    records = _read_dailies(root)
    if days:
        cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        records = [r for r in records if r["date"] >= cutoff]

    machines: Dict[str, Dict[str, Any]] = {}
    days_seen: set = set()
    latest: Dict[str, datetime] = {}
    total_cost = total_tokens = cache_read = 0
    exact_tokens = estimated_tokens = 0
    estimated_by: set = set()
    # (day, model, exact-bucket tuple) -> machines reporting it, for the
    # double-counting flag: two boxes publishing identical nonzero buckets
    # for the same model-day almost certainly counted the same calls.
    fingerprints: Dict[Tuple[str, str, Tuple[int, ...]], List[str]] = defaultdict(list)

    for rec in records:
        machine, day = rec["machine"], rec["date"]
        entry = machines.setdefault(machine, {
            "cost": 0.0, "tokens": 0, "calls": 0, "cache_read": 0,
            "days": defaultdict(float), "models": defaultdict(float),
        })
        days_seen.add(day)
        entry["calls"] += int(rec.get("invocations", 0))

        exact_tokens += _tokens(rec.get("exact", {}))
        est = _tokens(rec.get("estimated", {}))
        estimated_tokens += est
        if est:
            estimated_by.add(machine)

        for model, bucket in (rec.get("models") or {}).items():
            cost = _bill(model, bucket, day)
            tokens = _tokens(bucket)
            entry["cost"] += cost
            entry["tokens"] += tokens
            entry["cache_read"] += int(bucket.get("cache_read", 0))
            entry["days"][day] += cost
            entry["models"][model] += cost
            total_cost += cost
            total_tokens += tokens
            cache_read += int(bucket.get("cache_read", 0))
            print_ = tuple(int(bucket.get(f, 0)) for f in BUCKET_FIELDS)
            if any(print_):
                fingerprints[(day, model, print_)].append(machine)

        stamp = rec.get("generated_at")
        if stamp:
            try:
                dt = datetime.fromisoformat(stamp)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if machine not in latest or dt > latest[machine]:
                    latest[machine] = dt
            except ValueError:
                pass

    overlaps = [
        Overlap(machine_a=who[0], machine_b=who[1], day_a=day, day_b=day,
                similarity=1.0)
        for (day, _model, _fp), who in sorted(fingerprints.items())
        if len(set(who)) > 1
    ]

    me = local_machine()
    freshness = []
    for machine in sorted(machines):
        if machine == me:
            continue
        ts = latest.get(machine)
        age = (now - ts).total_seconds() / 3600 if ts else float("inf")
        freshness.append((machine, ts, age, age > STALE_HOURS))

    measured = exact_tokens + estimated_tokens
    return FleetSummary(
        machines=machines,
        days=sorted(days_seen),
        total_cost=total_cost,
        total_tokens=total_tokens,
        cache_read=cache_read,
        confidence=(exact_tokens / measured) if measured else None,
        inferred=sorted(estimated_by),
        overlaps=overlaps,
        freshness=freshness,
        local=me,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render the fleet dailies as one self-contained HTML page.")
    parser.add_argument("--days", type=int, default=None,
                        help="only include the last N days")
    parser.add_argument("--out", default="dashboard.html",
                        help="where to write the page")
    parser.add_argument("--title", default="NouGen fleet usage")
    parser.add_argument("--threshold", type=float,
                        default=float(os.environ.get("NOUGEN_ALERT_USD", "0")),
                        help="spend signal line in USD (0 = off)")
    args = parser.parse_args(argv)

    import dashboard as dash
    summary = fleet_summary(args.days)
    target = Path(os.path.expanduser(args.out))
    dash.write(summary, target, threshold=args.threshold, title=args.title)
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
