#!/usr/bin/env python3
"""NouGenTracker relay — the cross-machine baton for token usage.

The GM runs several boxes (blade1tb, phoebus, whoart). Each one records its own
token spend locally and knows nothing about the others, so no single report ever
told the truth about a day. The relay fixes that by passing a baton.

A leg of the relay has three phases, and every phase is a command:

    relay start   take the baton — pull peer rollups, open a leg, report what
                  the other machines have been doing while this box was idle
    relay mid     checkpoint mid-leg — re-export and stamp progress, so a power
                  cut costs at most the work since the last checkpoint
    relay end     hand the baton on — final export, commit, push

What crosses the wire is an AGGREGATE ONLY rollup: day, source, model, and the
five token buckets. No session ids, no transcript paths, no usernames. The
privacy property is structural rather than a redaction pass — a field that does
not exist cannot leak.

Layout under ``relay_dir`` (one file per machine per day, so two machines can
never write the same path and therefore never conflict):

    <relay_dir>/<machine>/usage_<YYYY-MM-DD>.json     rollup
    <relay_dir>/<machine>/baton_<YYYY-MM-DD>_<sid>.json   leg record

Every knob resolves dynamically — env var (name uppercased) beats the
tracker_config.json overlay beats the built-in default — reusing
``token_tracker._cfg`` so there is exactly one config engine in this project.

    RELAY_DIR             where rollups live      (default ~/.nougen/relay)
    RELAY_REMOTE          git transport URL       (default $NOUGEN_HANDOFF_REMOTE)
    RELAY_BRANCH          branch on that remote   (default "relay")
    RELAY_MACHINE         this box's identity     (default $NOUGEN_MACHINE, else hostname)
    RELAY_STALE_HOURS     peer staleness warning  (default 48)
    RELAY_WINDOW_DAYS     days each export covers (default 2)

The transport is a PRIVATE repo. This repo is public; usage data never lands in
it. Relay failure never fails a report — the rollup is written locally first and
the push is best-effort.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__version__ = "1.0.0"

#: Bumped when the rollup payload changes shape. Readers skip unknown versions
#: rather than crashing — three machines will not upgrade on the same day.
SCHEMA_VERSION = 1

PHASES: Tuple[str, ...] = ("start", "mid", "end")


# ---------------------------------------------------------------------------
# Host module — one config engine, one pricing table, one set of collectors
# ---------------------------------------------------------------------------

def _load_tracker():
    """Import token_tracker whether we are on sys.path or merely beside it."""
    try:
        import token_tracker as tt  # type: ignore
        return tt
    except ImportError:
        pass
    path = Path(__file__).resolve().parent / "token_tracker.py"
    spec = importlib.util.spec_from_file_location("token_tracker", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load token_tracker from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("token_tracker", module)
    spec.loader.exec_module(module)
    return module


tt = _load_tracker()
_cfg = tt._cfg
LOG = tt.LOG


def _int_cfg(name: str, default: int) -> int:
    return int(_cfg(name, default, int))


# ---------------------------------------------------------------------------
# Identity & paths — probed, never assumed
# ---------------------------------------------------------------------------

def machine_id() -> str:
    """This box's relay identity.

    Precedence: RELAY_MACHINE > NOUGEN_MACHINE > tracker config > hostname.
    The env vars are set at User scope on Windows and therefore do NOT reach
    already-running shells, so the hostname probe is a first-class route rather
    than a nicety — the same failure that produced the 2026-07-06 false
    hardware incident when a stale env var was trusted on its own.
    """
    for env in ("RELAY_MACHINE", "NOUGEN_MACHINE"):
        val = os.environ.get(env, "").strip()
        if val:
            return val.lower()
    val = str(_cfg("relay_machine", "")).strip()
    if val:
        return val.lower()
    return (socket.gethostname() or "").strip().lower()


def machine_provenance() -> str:
    """Where machine_id() got its answer — printed so a rename is never silent."""
    for env in ("RELAY_MACHINE", "NOUGEN_MACHINE"):
        if os.environ.get(env, "").strip():
            return f"env {env}"
    if str(_cfg("relay_machine", "")).strip():
        return "tracker config"
    return "hostname probe"


def relay_dir() -> Path:
    return Path(os.path.expanduser(
        _cfg("relay_dir", os.path.join("~", ".nougen", "relay")))).resolve()


def relay_remote() -> str:
    """Transport URL. Falls back to the handoff remote the fleet already uses."""
    return str(_cfg("relay_remote", os.environ.get("NOUGEN_HANDOFF_REMOTE", ""))).strip()


def relay_branch() -> str:
    return str(_cfg("relay_branch", "relay")).strip() or "relay"


def stale_hours() -> int:
    return _int_cfg("relay_stale_hours", 48)


def window_days() -> int:
    return _int_cfg("relay_window_days", 2)


def _machine_dir(machine: Optional[str] = None) -> Path:
    return relay_dir() / (machine or machine_id())


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Rollup export — aggregate only, by construction
# ---------------------------------------------------------------------------

ROLLUP_FIELDS: Tuple[str, ...] = (
    "input_tokens", "output_tokens", "cache_creation",
    "cache_read", "reasoning",
)

#: Buckets counted when asking "how much of this is measured?". cache_read is
#: excluded on purpose: it routinely runs 100x every other field, so including
#: it turns a confidence figure into "percent of cache-reads measured" wearing
#: a more important-sounding name.
CONFIDENCE_FIELDS: Tuple[str, ...] = (
    "input_tokens", "output_tokens", "cache_creation", "reasoning",
)


# ---------------------------------------------------------------------------
# Overlap sketches
#
# Two machines that read the same synced log directory both report the same
# calls, and summing them silently doubles a fleet total while every single
# file still looks correct. Excluding a machine's own rollup (which we do) does
# not catch that — the duplicate arrives wearing a different machine's name.
#
# Each machine-day therefore carries a bottom-k MinHash sketch of its call
# fingerprints: ~0.5 KB regardless of call volume, and enough to estimate the
# Jaccard similarity between any two machine-days.
# ---------------------------------------------------------------------------

SKETCH_VERSION = 1
SKETCH_HASH = "blake2b-64"


def sketch_k() -> int:
    return _int_cfg("relay_sketch_k", 64)


def call_fingerprint(inv: Any) -> int:
    """A stable 64-bit id for one model call.

    Deliberately does NOT include session_id. The same underlying call read
    from two machines can carry different session ids depending on the log
    format, and including it would suppress exactly the duplication this is
    built to detect.
    """
    parts = [inv.timestamp.isoformat(), str(inv.model)] + [
        str(getattr(inv, field, 0) or 0) for field in ROLLUP_FIELDS]
    digest = hashlib.blake2b("\x1f".join(parts).encode("utf-8"), digest_size=8)
    return int.from_bytes(digest.digest(), "big")


def minhash(fingerprints: Iterable[int], k: Optional[int] = None) -> List[int]:
    """Bottom-k sketch: the k smallest distinct fingerprints, ascending."""
    return sorted(set(fingerprints))[:(k or sketch_k())]


def jaccard(sketch_a: Sequence[int], sketch_b: Sequence[int],
            k: Optional[int] = None) -> float:
    """Estimate |A n B| / |A u B| from two bottom-k sketches.

    k is clamped to the smaller sketch. Two machines may have been written by
    builds configured with different k, and applying the reader's k to a
    shorter sketch would understate the overlap — the direction that hides the
    problem.
    """
    if not sketch_a or not sketch_b:
        return 0.0
    k = min(k or sketch_k(), len(sketch_a), len(sketch_b))
    union = sorted(set(sketch_a) | set(sketch_b))[:k]
    both = set(sketch_a) & set(sketch_b)
    if not union:
        return 0.0
    return sum(1 for value in union if value in both) / len(union)


def _encode_sketch(values: Sequence[int]) -> Dict[str, Any]:
    """Serialize a sketch WITH its parameters — a reader must never have to
    assume the k or the hash a writer used."""
    return {
        "version": SKETCH_VERSION,
        "hash": SKETCH_HASH,
        "k": sketch_k(),
        "values": [f"{value:016x}" for value in values],
    }


def _decode_sketch(payload: Any) -> Tuple[List[int], Optional[int]]:
    """(values, k). Unknown versions/hashes decode to empty rather than to a
    wrong similarity — a bogus overlap warning trains people to ignore them."""
    if not isinstance(payload, dict):
        return [], None
    if payload.get("version") != SKETCH_VERSION or payload.get("hash") != SKETCH_HASH:
        LOG.warning("relay: ignoring sketch (version %s / hash %s)",
                    payload.get("version"), payload.get("hash"))
        return [], None
    values = []
    for item in payload.get("values") or []:
        try:
            values.append(int(str(item), 16))
        except ValueError:
            continue
    k = payload.get("k")
    return values, int(k) if isinstance(k, int) else None


def collect_local(days: Optional[int] = None) -> List[Any]:
    """Every local invocation in the window, from all five source collectors."""
    window = tt.Window.last_days(days or window_days())
    invocations: List[Any] = []
    for parse in (tt.parse_claude, tt.parse_antigravity, tt.parse_codex,
                  tt.parse_gemini_cli, tt.parse_fleet_usage):
        try:
            invocations.extend(parse(window).usage.invocations)
        except Exception as exc:  # one dead source must not sink the export
            LOG.warning("relay: collector %s failed: %s", parse.__name__, exc)
    return invocations


def rollup_rows(invocations: Iterable[Any]) -> List[Dict[str, Any]]:
    """Fold invocations into (day, source, model, exact) rows.

    This is the whole privacy story: session_id and source_file are dropped
    here and never reach a file that crosses machines.
    """
    buckets: Dict[Tuple[str, str, str, bool], Dict[str, int]] = defaultdict(
        lambda: defaultdict(int))
    for inv in invocations:
        key = (inv.timestamp.strftime("%Y-%m-%d"), inv.source, inv.model,
               bool(inv.exact))
        row = buckets[key]
        row["input_tokens"] += inv.input_tokens
        row["output_tokens"] += inv.output_tokens
        row["cache_creation"] += inv.cache_creation
        row["cache_read"] += inv.cache_read
        row["reasoning"] += inv.reasoning
        row["invocations"] += 1
    rows: List[Dict[str, Any]] = []
    for (day, source, model, exact), totals in sorted(buckets.items()):
        row: Dict[str, Any] = {"day": day, "source": source, "model": model,
                               "exact": exact}
        row.update({field: int(totals[field]) for field in ROLLUP_FIELDS})
        row["invocations"] = int(totals["invocations"])
        rows.append(row)
    return rows


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    """Atomic write — a half-written rollup is a wrong number, not a warning."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def sketches_by_day(invocations: Iterable[Any]) -> Dict[str, List[int]]:
    """One bottom-k sketch per local day."""
    per_day: Dict[str, List[int]] = defaultdict(list)
    for inv in invocations:
        per_day[inv.timestamp.strftime("%Y-%m-%d")].append(call_fingerprint(inv))
    return {day: minhash(values) for day, values in per_day.items()}


def confidence(rows: Sequence[Dict[str, Any]]) -> Optional[float]:
    """Share of billable tokens that were MEASURED rather than inferred.

    Returns None — not 1.0 — when there is nothing to judge. An empty corpus
    reporting "100% measured" is wrong in the reassuring direction, which is
    the worst direction for a number whose whole job is flagging doubt.
    """
    measured = inferred = 0
    for row in rows:
        total = sum(int(row.get(field, 0)) for field in CONFIDENCE_FIELDS)
        if row.get("exact", True):
            measured += total
        else:
            inferred += total
    if measured + inferred == 0:
        return None
    return measured / (measured + inferred)


def format_confidence(score: Optional[float]) -> str:
    """Render a confidence ratio without rounding doubt away.

    A corpus that is 99.97% measured must not print as a flat "100.0%" while
    the line underneath names an inferred source — that reads as a
    contradiction and teaches the reader to distrust both numbers.
    """
    if score is None:
        return "unknown (nothing to judge)"
    if score >= 1.0:
        return "100% measured"
    if score > 0.999:
        return ">99.9% measured"
    return f"{score:.1%} measured"


def estimated_sources(rows: Sequence[Dict[str, Any]]) -> List[str]:
    """Which sources are contributing inferred numbers, so a single parser
    that stopped reporting exact counts is nameable instead of just dragging
    the fleet percentage down anonymously."""
    return sorted({str(row.get("source") or "?") for row in rows
                   if not row.get("exact", True)})


def export(days: Optional[int] = None,
           rows: Optional[List[Dict[str, Any]]] = None,
           invocations: Optional[Sequence[Any]] = None) -> List[Path]:
    """Write this machine's rollups, one file per day covered. Returns paths.

    ``rows`` lets a caller that already scanned pass the result in — scanning
    is the expensive part (hundreds of transcripts), so the baton phases do it
    exactly once per invocation.
    """
    machine = machine_id()
    if not machine or machine in {"localhost", "unknown"}:
        raise RuntimeError(
            "relay: cannot resolve a machine identity; set RELAY_MACHINE or "
            "NOUGEN_MACHINE. An anonymous rollup would be unattributable.")
    if rows is None or invocations is None:
        invocations = collect_local(days)
        rows = rollup_rows(invocations)
    by_day: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_day[row["day"]].append(row)
    sketches = sketches_by_day(invocations)

    written: List[Path] = []
    generated = _utc_now().isoformat()
    for day, day_rows in sorted(by_day.items()):
        path = _machine_dir(machine) / f"usage_{day}.json"
        _write_json(path, {
            "schema": SCHEMA_VERSION,
            "machine": machine,
            "day": day,
            "generated_utc": generated,
            "tracker_version": getattr(tt, "__version__", "unknown"),
            "relay_version": __version__,
            "sketch": _encode_sketch(sketches.get(day, [])),
            "rows": day_rows,
        })
        written.append(path)
    return written


# ---------------------------------------------------------------------------
# Baton — start / mid / end
# ---------------------------------------------------------------------------

def _session_pointer() -> Path:
    return _machine_dir() / ".current_session"


def session_id(create: bool = False) -> str:
    """The current leg's id, shared by start/mid/end.

    ``relay start`` writes the pointer; mid and end read it, which is what makes
    three separate process invocations append to one baton.
    """
    for env in ("RELAY_SESSION", "CLAUDE_SESSION_ID"):
        val = os.environ.get(env, "").strip()
        if val:
            return val.replace(os.sep, "_")[:32]
    pointer = _session_pointer()
    if pointer.exists():
        val = pointer.read_text(encoding="utf-8").strip()
        if val:
            return val
    if not create:
        return "adhoc"
    val = uuid.uuid4().hex[:8]
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(val, encoding="utf-8")
    return val


def _current_path(machine: Optional[str] = None) -> Path:
    """Where a machine publishes what it is working on RIGHT NOW."""
    return _machine_dir(machine) / "current.json"


def publish_intent(mission: str, phase: str, sid: str) -> Path:
    """Announce this box's active mission to the fleet.

    Rollups say what a machine DID. Nothing said what a machine is DOING, so
    two boxes took the same instruction on 2026-07-31 and built the same
    feature thirty minutes apart, each invisible to the other until both
    pull requests existed. This is the missing half of the relay.
    """
    path = _current_path()
    payload = {
        "schema": SCHEMA_VERSION,
        "machine": machine_id(),
        "session": sid,
        "phase": phase,
        "mission": mission[:300],
        "updated_utc": _utc_now().isoformat(),
        "active": phase != "end",
    }
    if not mission and path.exists():
        # mid/end without a fresh mission keep the one start published
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
            payload["mission"] = str(previous.get("mission") or "")[:300]
        except (OSError, ValueError):
            pass
    _write_json(path, payload)
    return path


def read_intents(include_local: bool = False) -> List[Dict[str, Any]]:
    """What every other machine says it is working on."""
    root = relay_dir()
    local = machine_id()
    out: List[Dict[str, Any]] = []
    if not root.exists():
        return out
    for path in sorted(root.glob("*/current.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        machine = str(payload.get("machine") or "").lower()
        if not machine or (machine == local and not include_local):
            continue
        out.append(payload)
    return out


def _baton_path(sid: str, day: Optional[str] = None) -> Path:
    day = day or _utc_now().strftime("%Y-%m-%d")
    return _machine_dir() / f"baton_{day}_{sid}.json"


def _totals_snapshot(rows: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    snapshot = {field: 0 for field in ROLLUP_FIELDS}
    snapshot["invocations"] = 0
    for row in rows:
        for field in ROLLUP_FIELDS:
            snapshot[field] += int(row.get(field, 0))
        snapshot["invocations"] += int(row.get("invocations", 0))
    return snapshot


def append_leg(phase: str, note: str = "", days: Optional[int] = None,
               mission: str = "") -> Path:
    """Record one phase of this machine's leg and refresh its rollups."""
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {PHASES}, got {phase!r}")
    sid = session_id(create=(phase == "start"))
    invocations = collect_local(days)
    rows = rollup_rows(invocations)
    export(days, rows=rows, invocations=invocations)
    publish_intent(mission, phase, sid)
    path = _baton_path(sid)
    if path.exists():
        try:
            baton = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            baton = {}
    else:
        baton = {}
    baton.setdefault("schema", SCHEMA_VERSION)
    baton.setdefault("machine", machine_id())
    baton.setdefault("session", sid)
    baton.setdefault("legs", [])
    if mission:
        baton["mission"] = mission[:300]
    baton["legs"].append({
        "phase": phase,
        "ts_utc": _utc_now().isoformat(),
        "note": note[:500],
        "totals": _totals_snapshot(rows),
        "confidence": confidence(rows),
    })
    _write_json(path, baton)
    return path


# ---------------------------------------------------------------------------
# Peers
# ---------------------------------------------------------------------------

@dataclass
class PeerRollup:
    machine: str
    day: str
    generated_utc: Optional[datetime]
    rows: List[Dict[str, Any]]
    path: Path
    sketch: List[int] = field(default_factory=list)
    sketch_k: Optional[int] = None


@dataclass
class Overlap:
    """Two machine-days that appear to have counted the same calls."""
    machine_a: str
    day_a: str
    machine_b: str
    day_b: str
    similarity: float


def detect_overlaps(rollups: Sequence[PeerRollup],
                    threshold: Optional[float] = None) -> List[Overlap]:
    """Machine-day pairs whose sketches say they saw the same calls.

    Days within ONE day of each other are compared, not just identical day
    keys. Two boxes in different timezones bucket the same call into different
    calendar days, and a same-day-only comparison would be blind to precisely
    the fleet configuration most likely to double count.
    """
    if threshold is None:
        threshold = float(_cfg("relay_overlap_threshold", 0.5, float))
    found: List[Overlap] = []
    usable = [r for r in rollups if r.sketch]
    for i, first in enumerate(usable):
        for second in usable[i + 1:]:
            if first.machine == second.machine:
                continue
            try:
                delta = abs((datetime.strptime(first.day, "%Y-%m-%d")
                             - datetime.strptime(second.day, "%Y-%m-%d")).days)
            except ValueError:
                continue
            if delta > 1:
                continue
            score = jaccard(first.sketch, second.sketch,
                            k=min(x for x in (first.sketch_k, second.sketch_k,
                                              sketch_k()) if x))
            if score >= threshold:
                found.append(Overlap(first.machine, first.day,
                                     second.machine, second.day, score))
    return sorted(found, key=lambda o: -o.similarity)


def read_peers(include_local: bool = False) -> List[PeerRollup]:
    """Load every peer rollup on disk.

    The local machine is excluded by its ``machine`` FIELD, not by its
    directory name — a rollup copied into the wrong folder must still not be
    double counted. Double counting is the failure mode that would silently
    inflate every number in this project forever, so it gets the strict check.
    """
    root = relay_dir()
    local = machine_id()
    peers: List[PeerRollup] = []
    if not root.exists():
        return peers
    for path in sorted(root.glob("*/usage_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOG.warning("relay: unreadable rollup %s: %s", path, exc)
            continue
        if payload.get("schema") != SCHEMA_VERSION:
            LOG.warning("relay: skipping %s (schema %s, this build reads %s)",
                        path, payload.get("schema"), SCHEMA_VERSION)
            continue
        machine = str(payload.get("machine") or "").lower()
        if not machine:
            LOG.warning("relay: skipping unattributed rollup %s", path)
            continue
        if machine == local and not include_local:
            continue
        generated = None
        raw = payload.get("generated_utc")
        if isinstance(raw, str):
            try:
                generated = datetime.fromisoformat(raw)
            except ValueError:
                generated = None
        values, k = _decode_sketch(payload.get("sketch"))
        peers.append(PeerRollup(
            machine=machine,
            day=str(payload.get("day") or ""),
            generated_utc=generated,
            rows=list(payload.get("rows") or []),
            path=path,
            sketch=values,
            sketch_k=k,
        ))
    return peers


def peer_freshness(peers: Sequence[PeerRollup]) -> List[Tuple[str, Optional[datetime], float, bool]]:
    """(machine, last export, age hours, stale?) — one row per peer machine."""
    latest: Dict[str, Optional[datetime]] = {}
    for peer in peers:
        current = latest.get(peer.machine)
        if peer.generated_utc and (current is None or peer.generated_utc > current):
            latest[peer.machine] = peer.generated_utc
        latest.setdefault(peer.machine, None)
    now = _utc_now()
    limit = stale_hours()
    out = []
    for machine in sorted(latest):
        ts = latest[machine]
        age = (now - ts).total_seconds() / 3600.0 if ts else float("inf")
        out.append((machine, ts, age, age > limit))
    return out


# ---------------------------------------------------------------------------
# Fleet summary — tokens, cost, and the doubts attached to both
# ---------------------------------------------------------------------------

#: The GM's standing spend signal. Not a budget to stay under: the doctrine here
#: is Jevons — efficiency buys capability, not savings — so this reports
#: cost-per-day crossing a line, it never advises using less.
def alert_usd() -> float:
    return float(_cfg("nougen_usage_alert_usd", 0.0, float) or 0.0)


def row_bill(row: Mapping[str, Any]) -> float:
    """API-equivalent USD for one rollup row, using the tracker's own pricing."""
    bucket = {
        "input_tokens": int(row.get("input_tokens", 0)),
        "output_tokens": int(row.get("output_tokens", 0)),
        "cache_creation_input_tokens": int(row.get("cache_creation", 0)),
        "cache_read_input_tokens": int(row.get("cache_read", 0)),
        "reasoning_tokens": int(row.get("reasoning", 0)),
    }
    return tt.model_bill(row.get("model"), bucket)[0]


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


def fleet_summary(days: Optional[int] = None) -> FleetSummary:
    """Everything the fleet has published, folded into one view.

    Includes THIS machine: a fleet report that omits the box running it is the
    same mistake as a machine that cannot see its peers.
    """
    rollups = read_peers(include_local=True)
    if days:
        cutoff = (_utc_now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rollups = [r for r in rollups if r.day >= cutoff]
    machines: Dict[str, Dict[str, Any]] = {}
    all_rows: List[Dict[str, Any]] = []
    days_seen: set = set()
    total_cost = total_tokens = cache_read = 0

    for rollup in rollups:
        entry = machines.setdefault(rollup.machine, {
            "cost": 0.0, "tokens": 0, "calls": 0, "cache_read": 0,
            "days": defaultdict(float), "models": defaultdict(float),
        })
        days_seen.add(rollup.day)
        for row in rollup.rows:
            all_rows.append(row)
            cost = row_bill(row)
            tokens = sum(int(row.get(field, 0)) for field in ROLLUP_FIELDS)
            entry["cost"] += cost
            entry["tokens"] += tokens
            entry["calls"] += int(row.get("invocations", 0))
            entry["cache_read"] += int(row.get("cache_read", 0))
            entry["days"][rollup.day] += cost
            entry["models"][str(row.get("model") or "unknown")] += cost
            total_cost += cost
            total_tokens += tokens
            cache_read += int(row.get("cache_read", 0))

    return FleetSummary(
        machines=machines,
        days=sorted(days_seen),
        total_cost=total_cost,
        total_tokens=total_tokens,
        cache_read=cache_read,
        confidence=confidence(all_rows),
        inferred=estimated_sources(all_rows),
        overlaps=detect_overlaps(rollups),
        freshness=peer_freshness([r for r in rollups if r.machine != machine_id()]),
        local=machine_id(),
    )


# ---------------------------------------------------------------------------
# Transport — best effort, never fatal
# ---------------------------------------------------------------------------

def _git(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=_int_cfg("relay_git_timeout", 120))


def _ensure_clone() -> Optional[Path]:
    """Make relay_dir a git clone of the transport, or return None if we can't."""
    remote = relay_remote()
    root = relay_dir()
    root.mkdir(parents=True, exist_ok=True)
    if not remote:
        return None
    if (root / ".git").exists():
        return root
    branch = relay_branch()
    probe = _git(["clone", "--branch", branch, "--single-branch", remote, "."], root)
    if probe.returncode == 0:
        return root
    # Branch does not exist yet (first machine on the relay) — create it local.
    init = _git(["init", "-b", branch], root)
    if init.returncode != 0:
        LOG.warning("relay: git init failed: %s", init.stderr.strip())
        return None
    _git(["remote", "add", "origin", remote], root)
    return root


def _has_commits(root: Path) -> bool:
    return _git(["rev-parse", "--verify", "HEAD"], root).returncode == 0


def pull() -> bool:
    """Fetch peer rollups. Returns False only on a real transport problem.

    Being the FIRST machine on the relay is a normal state, not a failure: the
    branch simply does not exist upstream yet. Warning about it would train the
    GM to ignore relay warnings, so that case returns success quietly.
    """
    root = _ensure_clone()
    if root is None:
        return False
    branch = relay_branch()
    fetch = _git(["fetch", "origin", branch], root)
    upstream_exists = (
        fetch.returncode == 0
        and _git(["rev-parse", "--verify", f"origin/{branch}"], root).returncode == 0
    )
    if not upstream_exists:
        LOG.debug("relay: no upstream %s yet — this box is first on the relay",
                  branch)
        return True
    if not _has_commits(root):
        result = _git(["checkout", "-B", branch, f"origin/{branch}"], root)
    else:
        result = _git(["pull", "--rebase", "--autostash", "origin", branch], root)
    if result.returncode != 0:
        LOG.warning("relay: pull failed: %s", result.stderr.strip())
        return False
    return True


def push(message: Optional[str] = None) -> bool:
    """Commit and publish this machine's rollups. Returns False on failure."""
    root = _ensure_clone()
    if root is None:
        return False
    branch = relay_branch()
    _git(["add", "-A"], root)
    status = _git(["status", "--porcelain"], root)
    commit = _git(["commit", "-m",
                   message or f"relay: {machine_id()} {_utc_now():%Y-%m-%dT%H:%MZ}"],
                  root)
    if commit.returncode != 0 and status.stdout.strip():
        LOG.warning("relay: commit failed: %s", commit.stderr.strip())
    result = _git(["push", "origin", branch], root)
    if result.returncode != 0:
        LOG.warning("relay: push failed: %s", result.stderr.strip())
        return False
    return True


# ---------------------------------------------------------------------------
# Hook installation — this is what "baked in" means
# ---------------------------------------------------------------------------

def hook_command(phase: str) -> str:
    script = Path(__file__).resolve()
    return f'"{sys.executable}" "{script}" {phase} --quiet'


HOOK_EVENTS: Dict[str, str] = {
    "SessionStart": "start",
    "Stop": "mid",
    "SessionEnd": "end",
}

#: Marker that makes installation idempotent — we recognise our own entries.
HOOK_MARKER = "relay.py"


def hook_config() -> Dict[str, Any]:
    return {
        event: [{"hooks": [{"type": "command", "command": hook_command(phase)}]}]
        for event, phase in HOOK_EVENTS.items()
    }


def install_hooks(settings_path: Path) -> Tuple[bool, str]:
    """Merge relay hooks into a Claude Code settings file, with a backup."""
    settings: Dict[str, Any] = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            return False, f"refusing to touch malformed {settings_path}: {exc}"
        backup = settings_path.with_suffix(
            settings_path.suffix + f".bak-relay-{_utc_now():%Y%m%d%H%M%S}")
        backup.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    hooks = settings.setdefault("hooks", {})
    added = []
    for event, entry in hook_config().items():
        existing = hooks.setdefault(event, [])
        already = any(
            HOOK_MARKER in hook.get("command", "")
            for group in existing if isinstance(group, dict)
            for hook in group.get("hooks", []) if isinstance(hook, dict)
        )
        if already:
            continue
        existing.extend(entry)
        added.append(event)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    if not added:
        return True, f"relay hooks already present in {settings_path}"
    return True, f"installed relay hooks ({', '.join(added)}) in {settings_path}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_identity() -> None:
    print(f"machine: {machine_id()}  (via {machine_provenance()})")
    print(f"relay dir: {relay_dir()}")
    remote = relay_remote()
    print(f"transport: {remote or '(none configured — local only)'}"
          f"{'  branch ' + relay_branch() if remote else ''}")


def print_peer_table(peers: Sequence[PeerRollup]) -> None:
    rows = peer_freshness(peers)
    print()
    print(f"{'PEER MACHINES':<20}{'last rollup (UTC)':<24}{'age':>10}  state")
    print("-" * 66)
    if not rows:
        print("(none yet — no peer has exported to this relay)")
        return
    for machine, ts, age, stale in rows:
        when = f"{ts:%Y-%m-%d %H:%M}" if ts else "never"
        age_s = "-" if age == float("inf") else f"{age:.1f}h"
        print(f"{machine:<20}{when:<24}{age_s:>10}  "
              f"{'STALE' if stale else 'ok'}")
    print("-" * 66)
    print(f"(stale threshold {stale_hours()}h — a quiet peer is shown as an age, "
          "never as a smaller total)")


def _throttle_path() -> Path:
    return _machine_dir() / ".last_mid"


def mid_is_due() -> bool:
    """True when enough time has passed to justify another mid checkpoint.

    ``mid`` is wired to the Stop hook, which fires on EVERY turn, and a full
    export rescans hundreds of transcripts. Unthrottled that would tax every
    turn of every session to re-derive numbers that barely moved. The interval
    is the knob RELAY_MID_MIN_MINUTES (default 30).
    """
    minutes = _int_cfg("relay_mid_min_minutes", 30)
    if minutes <= 0:
        return True
    path = _throttle_path()
    if not path.exists():
        return True
    try:
        last = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True
    return (_utc_now() - last) >= timedelta(minutes=minutes)


def _stamp_mid() -> None:
    path = _throttle_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_utc_now().isoformat(), encoding="utf-8")


def print_intent_board(intents: Sequence[Dict[str, Any]]) -> None:
    """Who else is mid-mission. Printed at start so a second box can see the
    first one working before it repeats the work."""
    active = [i for i in intents if i.get("active")]
    if not active:
        return
    print()
    print("OTHER MACHINES CURRENTLY WORKING")
    print("-" * 66)
    for intent in active:
        when = str(intent.get("updated_utc") or "")[:16].replace("T", " ")
        print(f"  {intent.get('machine'):<12} {when:<18} "
              f"{intent.get('phase', '?')}")
        print(f"    -> {intent.get('mission') or '(no mission stated)'}")
    print("-" * 66)
    print("  check these before starting — two boxes have already built the "
          "same feature\n  thirty minutes apart on this fleet")


def print_overlaps(overlaps: Sequence[Overlap]) -> None:
    if not overlaps:
        return
    print()
    print("!! OVERLAP — these machines appear to have counted the same calls:")
    for overlap in overlaps:
        span = (f"{overlap.day_a}" if overlap.day_a == overlap.day_b
                else f"{overlap.day_a}/{overlap.day_b}")
        print(f"     {span}  {overlap.machine_a} + {overlap.machine_b}  "
              f"(Jaccard {overlap.similarity:.3f})")
    print("     fleet totals are inflated by the shared portion — the numbers "
          "are NOT corrected,\n     because guessing which copy to drop would "
          "be a worse error than naming the doubt")


def cmd_phase(args: argparse.Namespace) -> int:
    phase = args.phase
    if phase == "mid" and not args.force and not mid_is_due():
        if not args.quiet:
            print(f"relay: mid skipped (checkpointed within the last "
                  f"{_int_cfg('relay_mid_min_minutes', 30)}m; --force overrides)")
        return 0
    if phase == "start" and not args.no_pull:
        pull()
    try:
        path = append_leg(phase, note=args.note or "", days=args.days,
                          mission=getattr(args, "mission", "") or "")
    except RuntimeError as exc:
        print(f"relay: {exc}", file=sys.stderr)
        return 1
    if phase == "mid":
        _stamp_mid()
    if phase in ("mid", "end") and not args.no_push:
        push()
    if args.quiet:
        return 0
    _print_identity()
    print(f"leg: {phase}  session {session_id()}  -> {path.name}")
    if phase in ("start", "end"):
        print_intent_board(read_intents())
        print_peer_table(read_peers())
        print_overlaps(detect_overlaps(read_peers(include_local=True)))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    _print_identity()
    everything = read_peers(include_local=True)
    peers = [p for p in everything if p.machine != machine_id()]
    local = [p for p in everything if p.machine == machine_id()]
    print_intent_board(read_intents(include_local=args.all))
    print_peer_table(peers)
    print_overlaps(detect_overlaps(everything))
    score = confidence([row for p in everything for row in p.rows])
    print(f"\nlocal rollups on disk: {len(local)}   peer rollups: {len(peers)}")
    print(f"fleet confidence: {format_confidence(score)} "
          "(billable tokens; cache-read excluded)")
    inferred = estimated_sources([row for p in everything for row in p.rows])
    if inferred:
        print(f"  inferred by: {', '.join(inferred)}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    written = export(args.days)
    for path in written:
        print(path)
    if not args.no_push:
        push()
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Render the fleet as a page. A table tells; a page shows."""
    import dashboard as dash

    summary = fleet_summary(args.days)
    target = Path(os.path.expanduser(args.out)) if args.out else (
        relay_dir() / "dashboard.html")
    dash.write(summary, target, threshold=alert_usd(), title=args.title)
    print(target)
    if args.open:
        try:
            import webbrowser
            webbrowser.open(target.as_uri())
        except Exception as exc:  # pragma: no cover - platform dependent
            LOG.warning("relay: could not open a browser: %s", exc)
    return 0


def cmd_hooks(args: argparse.Namespace) -> int:
    if args.install:
        target = Path(os.path.expanduser(args.settings))
        ok, message = install_hooks(target)
        print(message)
        return 0 if ok else 1
    print(json.dumps({"hooks": hook_config()}, indent=2))
    print("\n# add --install to merge this into your Claude Code settings",
          file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="relay.py",
        description="NouGenTracker cross-machine relay: start / mid / end.",
        epilog="the baton: start takes it, mid checkpoints it, end passes it on",
    )
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    subs = parser.add_subparsers(dest="command", required=True)

    for phase, blurb in (
            ("start", "take the baton: pull peers, open a leg"),
            ("mid", "checkpoint mid-leg: re-export and stamp progress"),
            ("end", "hand off: final export, commit, push")):
        sub = subs.add_parser(phase, help=blurb)
        sub.add_argument("--note", default="", help="free text stored on the leg")
        sub.add_argument("--days", type=int, default=None,
                         help=f"days of history to export (default {window_days()})")
        sub.add_argument("--no-pull", action="store_true", help="skip git pull")
        sub.add_argument("--no-push", action="store_true", help="skip git push")
        sub.add_argument("--quiet", action="store_true", help="hook mode: no output")
        sub.add_argument("--force", action="store_true",
                         help="ignore the mid-checkpoint throttle")
        sub.add_argument("--mission", default="",
                         help="what this box is working on — published to the "
                              "fleet so peers can see it before duplicating it")
        sub.set_defaults(func=cmd_phase, phase=phase)

    status = subs.add_parser(
        "status", help="identity, peers, freshness, overlap and confidence")
    status.add_argument("--all", action="store_true",
                        help="include this machine in the intent board")
    status.set_defaults(func=cmd_status)

    exp = subs.add_parser("export", help="write rollups without touching a baton")
    exp.add_argument("--days", type=int, default=None)
    exp.add_argument("--no-push", action="store_true")
    exp.set_defaults(func=cmd_export)

    board = subs.add_parser("dashboard",
                            help="render the fleet as a self-contained HTML page")
    board.add_argument("--out", default="", help="output path")
    board.add_argument("--days", type=int, default=None,
                       help="limit to the last N days of rollups")
    board.add_argument("--title", default="Fleet usage")
    board.add_argument("--open", action="store_true",
                       help="open the page in a browser when it is written")
    board.set_defaults(func=cmd_dashboard)

    hooks = subs.add_parser("hooks", help="print or install the Claude Code hooks")
    hooks.add_argument("--install", action="store_true")
    hooks.add_argument("--settings",
                       default=os.path.join("~", ".claude", "settings.json"),
                       help="settings file to merge into")
    hooks.set_defaults(func=cmd_hooks)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    import logging
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
