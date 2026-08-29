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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pricing_live import calculate_cost, round_to_cents
import token_tracker as tt

logger = logging.getLogger("fleet_summary")

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


def validate_and_clamp_accounting(
    cold: float,
    realistic: float,
    paid: float,
) -> Tuple[float, float, float, float]:
    """Enforce accounting invariants:
    a. cold >= cached_realistic >= 0 and paid >= 0.
    b. absorbed == cold - paid, never displayed negative (clamp to 0 with logged warning).
    c. Evaluated using Decimal arithmetic.

    Returns:
      (cold_clamped, realistic_clamped, paid_clamped, absorbed)
    """
    if paid < 0:
        logger.warning("Negative paid spend (%s); clamping to 0.0", paid)
        paid = 0.0

    if realistic < 0:
        logger.warning("Negative realistic cost (%s); clamping to 0.0", realistic)
        realistic = 0.0

    if cold < realistic:
        logger.warning(
            "Accounting inconsistency: cold cost (%s) < realistic cost (%s); clamping cold to realistic",
            cold, realistic
        )
        cold = realistic

    cold_dec = round_to_cents(Decimal(str(cold)))
    realistic_dec = round_to_cents(Decimal(str(realistic)))
    paid_dec = round_to_cents(Decimal(str(paid)))
    absorbed_dec = cold_dec - paid_dec

    if absorbed_dec < Decimal("0.00"):
        logger.warning(
            "Accounting inconsistency: cold (%s) < paid (%s); clamping absorbed to 0.0",
            cold, paid
        )
        absorbed_dec = Decimal("0.00")

    absorbed = float(round_to_cents(absorbed_dec))
    return (float(cold_dec), float(realistic_dec), float(paid_dec), absorbed)


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
    cold_cost: float = 0.0
    paid_cost: float = 0.0
    absorbed_cost: float = 0.0
    exact_tokens: int = 0
    estimated_tokens: int = 0

    @property
    def is_estimated(self) -> bool:
        return (
            self.estimated_tokens > 0
            or (self.confidence is not None and self.confidence < 0.9999)
            or bool(self.inferred)
        )

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


def _bill_cold(model: str, bucket: Dict[str, int], when: str) -> float:
    """Price a dailies bucket cold: all input-side tokens fresh, reasoning as output, no cache discount."""
    inp, out, _cr, _src = tt.price_for(model, when)
    i = int(bucket.get("input_tokens", 0))
    cc = int(bucket.get("cache_creation", 0))
    cr = int(bucket.get("cache_read", 0))
    o = int(bucket.get("output_tokens", 0))
    rt = int(bucket.get("reasoning", 0))
    cost = calculate_cost(
        input_tokens=(i + cc + cr),
        output_tokens=(o + rt),
        inp_rate=inp,
        out_rate=out,
    )
    return float(cost)


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
                  now: Optional[datetime] = None,
                  paid: Optional[float] = None) -> FleetSummary:
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
    total_cost = total_cold = total_tokens = cache_read = 0
    exact_tokens = estimated_tokens = 0
    estimated_by: set = set()
    # (day, model, exact-bucket tuple) -> machines reporting it, for the
    # double-counting flag: two boxes publishing identical nonzero buckets
    # for the same model-day almost certainly counted the same calls.
    fingerprints: Dict[Tuple[str, str, Tuple[int, ...]], List[str]] = defaultdict(list)

    for rec in records:
        machine, day = rec["machine"], rec["date"]
        entry = machines.setdefault(machine, {
            "cost": 0.0, "cold_cost": 0.0, "tokens": 0, "calls": 0, "cache_read": 0,
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
            cold = _bill_cold(model, bucket, day)
            tokens = _tokens(bucket)
            entry["cost"] += cost
            entry["cold_cost"] = entry.get("cold_cost", 0.0) + cold
            entry["tokens"] += tokens
            entry["cache_read"] += int(bucket.get("cache_read", 0))
            entry["days"][day] += cost
            entry["models"][model] += cost
            total_cost += cost
            total_cold += cold
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

    if paid is None:
        # The configured subscription figure is MONTHLY, but this summary covers
        # however many distinct machine-days the dailies happen to span. Feeding
        # the raw monthly number into validate_and_clamp_accounting() made
        # `absorbed = cold - paid` subtract a full month of spend from a window
        # that might be two days, and the clamp above only checks the sign, not
        # the units, so the result looked verified while being incomparable.
        # Pro-rate to the days actually summarised. days_seen is the honest
        # window here: it counts days that really carry data, not calendar span,
        # so gaps in publication do not inflate the bill.
        import subscriptions as _subs
        _monthly, _ = _subs.monthly_total()
        paid = _subs.prorate(_monthly, len(days_seen))

    cold_clamped, realistic_clamped, paid_clamped, absorbed = validate_and_clamp_accounting(
        total_cold, total_cost, paid
    )

    measured = exact_tokens + estimated_tokens
    return FleetSummary(
        machines=machines,
        days=sorted(days_seen),
        total_cost=realistic_clamped,
        total_tokens=total_tokens,
        cache_read=cache_read,
        confidence=(exact_tokens / measured) if measured else None,
        inferred=sorted(estimated_by),
        overlaps=overlaps,
        freshness=freshness,
        local=me,
        cold_cost=cold_clamped,
        paid_cost=paid_clamped,
        absorbed_cost=absorbed,
        exact_tokens=exact_tokens,
        estimated_tokens=estimated_tokens,
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
