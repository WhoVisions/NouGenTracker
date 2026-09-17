"""One-call, coverage-aware queries over published tracker daily artifacts."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Literal, TypedDict
from zoneinfo import ZoneInfo

TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation", "cache_read")


class TrackerFlags(TypedDict):
    observed: bool
    complete: bool
    canonical: bool | None
    current: bool | None
    exact: bool | None
    validated: bool
    traceable: bool
    reconciled: bool
    estimated: bool
    stale: bool | None
    partial: bool
    conflicted: bool | None
    superseded: bool | None
    missing_expected_entities: bool
    missing_expected_nodes: bool
    missing_expected_dates: bool
    provenance_incomplete: bool
    retryable: bool
    recoverable: bool
    failover_available: bool
    continuation_available: bool
    deeper_search_available: bool
    exact_source_available: bool


Availability = Literal[
    "AVAILABLE", "UNAVAILABLE", "UNKNOWN", "UNREACHABLE", "DEGRADED", "INTERMITTENT", "TIMEOUT",
    "RATE_LIMITED", "AUTH_FAILED", "PERMISSION_DENIED", "CIRCUIT_OPEN", "MAINTENANCE",
]
Observation = Literal[
    "OBSERVED", "UNOBSERVED", "UNREAD", "DEFERRED", "SKIPPED", "DISCOVERED", "REQUESTED", "FETCHED",
    "PARSED", "INDEXED",
]
Completeness = Literal[
    "COMPLETE", "PARTIAL", "EMPTY", "TRUNCATED", "PAGINATED", "EXHAUSTED", "INDETERMINATE",
    "COVERAGE_UNKNOWN", "UNKNOWN",
]
Freshness = Literal[
    "LIVE", "CURRENT", "FRESH", "AGING", "STALE", "EXPIRED", "SUPERSEDED", "FUTURE_DATED",
    "LATE_ARRIVING", "UNKNOWN",
]
TruthQuality = Literal[
    "EXACT", "OBSERVED_EXACT", "ESTIMATED", "DERIVED", "INFERRED", "RECONCILED", "PROJECTED",
    "APPROXIMATE", "UNKNOWN", "DISPUTED", "CONTRADICTED",
]
Validation = Literal[
    "VALID", "UNVALIDATED", "VALIDATING", "INVALID", "SCHEMA_MISMATCH", "CHECKSUM_MISMATCH",
    "RANGE_INVALID", "SEMANTICALLY_INVALID", "DUPLICATE", "POSSIBLE_DUPLICATE",
]
Canonicality = Literal[
    "CANONICAL", "NON_CANONICAL", "CANDIDATE", "HISTORICAL", "SUPERSEDED", "AMENDED", "RETRACTED",
    "ORPHANED", "CURRENT", "UNKNOWN",
]
Computation = Literal[
    "RAW", "NORMALIZED", "AGGREGATED", "MATERIALIZED", "CACHED", "RECOMPUTED", "RECONCILED",
    "ESTIMATED", "BACKFILLED",
]
Provenance = Literal[
    "PROVEN", "TRACEABLE", "PARTIALLY_TRACEABLE", "SOURCE_MISSING", "SOURCE_UNAVAILABLE", "UNVERIFIED",
    "ORPHANED",
]
Federation = Literal[
    "FEDERATION_COMPLETE", "FEDERATION_PARTIAL", "NODE_MISSING", "NODE_DEFERRED", "NODE_FAILED",
    "NODE_STALE", "NODE_DIVERGED", "FAILOVER_ACTIVE", "FAILOVER_EXHAUSTED",
]
Retrieval = Literal[
    "EXACT_HIT", "CANONICAL_HIT", "LEXICAL_HIT", "SEMANTIC_HIT", "GRAPH_HIT", "HYBRID_HIT", "RERANKED",
    "RECURSIVE_HIT", "FALLBACK_HIT", "CACHE_HIT", "NO_HIT", "HIT", "ERROR", "NOT_RUN",
]
Conflict = Literal[
    "CONSISTENT", "CONFLICTED", "DIVERGENT", "AMBIGUOUS", "MULTIPLE_CANDIDATES", "RESOLVED_BY_RECENCY",
    "RESOLVED_BY_PROVENANCE", "RESOLVED_BY_CANONICALITY", "UNRESOLVED", "CLEAR", "UNKNOWN",
]
Execution = Literal[
    "PENDING", "RUNNING", "RETRYING", "BACKING_OFF", "FAILING_OVER", "COMPLETE", "FAILED", "CANCELLED",
    "BUDGET_EXHAUSTED",
]
Pagination = Literal[
    "NOT_REQUIRED", "ACTIVE", "CONTINUATION_AVAILABLE", "EXHAUSTED", "STALLED", "LOOP_DETECTED",
    "CURSOR_INVALID", "COMPLETE", "CONTINUING", "NOT_APPLICABLE",
]
Anomaly = Literal[
    "NORMAL", "OUTLIER", "SPIKE", "DROP", "GAP", "COUNTER_RESET", "NEGATIVE_DELTA", "DUPLICATE_COHORT",
    "IDENTITY_DRIFT", "CLOCK_DRIFT", "IMPOSSIBLE_VALUE",
]
Confidence = Literal["CERTAIN", "HIGH", "MEDIUM", "LOW", "INSUFFICIENT"]


class TrackerState(TypedDict):
    status: Literal["SUCCESS", "DEGRADED"]
    observation: Observation
    completeness: Completeness
    conflict: Conflict
    freshness: Freshness
    retrieval: Retrieval
    pagination: Pagination
    availability: Availability
    truth_quality: TruthQuality
    validation: Validation
    canonicality: Canonicality
    computation: Computation
    provenance: Provenance
    federation: Federation
    execution: Execution
    anomaly: Anomaly
    confidence: Confidence
    flags: TrackerFlags
    reason_codes: list[str]
    evidence: list[dict[str, Any]]
    coverage: dict[str, Any]
    canonical_key: str
    recovery: str


def _recovery(state: TrackerState) -> str:
    """Keep machine-actionable recovery separate from the human status label."""
    flags = state["flags"]
    if (state["completeness"] in {"PARTIAL", "COVERAGE_UNKNOWN", "INDETERMINATE"}
            and (flags["missing_expected_nodes"] or flags["missing_expected_entities"]
                 or flags["missing_expected_dates"])
            and (flags["retryable"] or flags["recoverable"])):
        return "CONTINUE_FEDERATION"
    if state["conflict"] in {"CONFLICTED", "DIVERGENT", "AMBIGUOUS", "MULTIPLE_CANDIDATES", "UNRESOLVED"} and flags["traceable"]:
        return "TRACE_PROVENANCE"
    if state["freshness"] in {"AGING", "STALE", "EXPIRED", "SUPERSEDED", "LATE_ARRIVING"} and state["canonical_key"]:
        return "REFRESH_CANONICAL"
    if state["retrieval"] == "NO_HIT" and not state["coverage"]["complete"]:
        return "EXPAND_RETRIEVAL"
    if state["retrieval"] == "NO_HIT" and state["coverage"]["complete"] and flags["deeper_search_available"]:
        return "DRIFT_RECURSE"
    if state["pagination"] in {"STALLED", "LOOP_DETECTED", "CURSOR_INVALID"}:
        return "REPARTITION_QUERY"
    if state["availability"] in {
        "TIMEOUT", "UNAVAILABLE", "UNREACHABLE", "RATE_LIMITED", "AUTH_FAILED", "PERMISSION_DENIED",
        "CIRCUIT_OPEN",
    } and flags["failover_available"]:
        return "FAILOVER"
    if state["truth_quality"] in {"ESTIMATED", "DERIVED", "INFERRED", "PROJECTED", "APPROXIMATE", "DISPUTED"} and flags.get("exact_source_available", False):
        return "RECONCILE"
    if state["canonicality"] in {"SUPERSEDED", "AMENDED", "RETRACTED", "ORPHANED"}:
        return "FOLLOW_SUPERSESSION"
    return "STOP_WITH_EXPLICIT_STATE"


def _days(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError("end must be on or after start")
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _compress_days(values: list[str]) -> list[str]:
    if not values:
        return []
    parsed = sorted(date.fromisoformat(value) for value in values)
    ranges = []
    first = previous = parsed[0]
    for current in parsed[1:]:
        if current == previous + timedelta(days=1):
            previous = current
            continue
        ranges.append(first.isoformat() if first == previous else f"{first.isoformat()}..{previous.isoformat()}")
        first = previous = current
    ranges.append(first.isoformat() if first == previous else f"{first.isoformat()}..{previous.isoformat()}")
    return ranges


def _read_daily(path: Path, machine: str, day: str) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("daily artifact must not be a symlink")
    raw = path.read_bytes()
    record = json.loads(raw)
    if not isinstance(record, dict):
        raise ValueError("daily artifact must be a JSON object")
    if record.get("machine") != machine or record.get("date") != day:
        raise ValueError("daily identity does not match its path")
    if "partial" in record and not isinstance(record["partial"], bool):
        raise ValueError("partial must be a boolean")
    for section in ("exact", "estimated"):
        values = record.get(section, {})
        if not isinstance(values, dict):
            raise ValueError(f"{section} must be an object")
        for field in TOKEN_FIELDS:
            value = values.get(field, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{section}.{field} must be a non-negative integer")
    models = record.get("models", {})
    if not isinstance(models, dict):
        raise ValueError("models must be an object")
    for name, values in models.items():
        if not isinstance(name, str) or not isinstance(values, dict):
            raise ValueError("model records must map names to objects")
        for field in TOKEN_FIELDS:
            value = values.get(field, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"models.{name}.{field} must be a non-negative integer")
    return {"record": record, "sha256": hashlib.sha256(raw).hexdigest()}


def tracker_query(
    root: str | Path,
    *,
    machines: Iterable[str],
    scope: str = "fleet",
    period: str,
    as_of: str,
    timezone: str = "America/New_York",
    start: str | None = None,
    end: str | None = None,
    group_by: str = "machine",
    prove: bool = False,
) -> dict[str, Any]:
    """Aggregate published dailies; absent data remains deferred, never zero."""
    tz = ZoneInfo(timezone)
    try:
        as_of_day = date.fromisoformat(as_of)
    except ValueError:
        instant = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValueError("as_of datetime must include a timezone")
        as_of_day = instant.astimezone(tz).date()
    period = period.upper()
    if period == "YTD":
        lo = date(as_of_day.year, 1, 1)
        hi = as_of_day
    elif period == "MTD":
        lo = date(as_of_day.year, as_of_day.month, 1)
        hi = as_of_day
    elif period == "RANGE":
        if not start or not end:
            raise ValueError("RANGE requires start and end")
        lo, hi = date.fromisoformat(start), date.fromisoformat(end)
    elif period == "LATEST":
        lo = hi = as_of_day
    else:
        raise ValueError("period must be YTD, MTD, RANGE, or LATEST")
    requested_days = _days(lo, hi)
    expected = sorted(set(machines))
    if scope not in {"fleet", "machine"}:
        raise ValueError("scope must be fleet or machine")
    if not expected:
        raise ValueError("machines must not be empty")
    if group_by not in {"machine", "model"}:
        raise ValueError("group_by must be machine or model")

    machine_rows = []
    missing_by_machine: dict[str, dict[str, Any]] = {}
    malformed: list[dict[str, str]] = []
    partial_by_machine: dict[str, dict[str, Any]] = {}
    source_hashes: dict[str, str] = {}
    metric_total = 0
    exact_total = 0
    estimated_total = 0
    group_totals: dict[str, int] = {}

    for machine in expected:
        total = {field: 0 for field in TOKEN_FIELDS}
        exact_activity = estimated_activity = 0
        found_days: list[str] = []
        missing_days: list[str] = []
        partial_days: list[str] = []
        machine_provenance = []
        for day in requested_days:
            day_text = day.isoformat()
            path = Path(root) / "dailies" / machine / f"{day_text}.json"
            if not path.exists():
                missing_days.append(day_text)
                continue
            try:
                item = _read_daily(path, machine, day_text)
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
                malformed.append({"machine": machine, "day": day_text, "reason": str(exc)})
                missing_days.append(day_text)
                continue
            record = item["record"]
            found_days.append(day_text)
            if record.get("partial") is True:
                partial_days.append(day_text)
            source_id = f"daily:{machine}:{day_text}"
            source_hashes[source_id] = item["sha256"]
            machine_provenance.append(source_id)
            for quality, bucket in (("exact", "exact"), ("estimated", "estimated")):
                values = record.get(quality, {})
                activity = sum(values.get(field, 0) for field in TOKEN_FIELDS)
                if quality == "exact":
                    exact_activity += activity
                else:
                    estimated_activity += activity
                if group_by == "model":
                    for name, stats in sorted(record.get("models", {}).items()):
                        if quality == "estimated" and "estimated" not in name.casefold():
                            continue
                        if quality == "exact" and "estimated" in name.casefold():
                            continue
                        group_totals[name] = group_totals.get(name, 0) + sum(
                            stats.get(field, 0) for field in TOKEN_FIELDS
                        )
                for field in TOKEN_FIELDS:
                    total[field] += values.get(field, 0)
            metric_total += sum(record.get(bucket, {}).get(field, 0)
                                for bucket in ("exact", "estimated")
                                for field in TOKEN_FIELDS)
        malformed_days = any(item["machine"] == machine for item in malformed)
        state = "OBSERVED_ZERO" if not missing_days and not partial_days and all(v == 0 for v in total.values()) else (
            "COMPLETE" if not missing_days else ("PARTIAL" if found_days or malformed_days else "DEFERRED"))
        if partial_days:
            state = "PARTIAL"
        if missing_days:
            missing_by_machine[machine] = {
                "count": len(missing_days), "ranges": _compress_days(missing_days),
            }
        if partial_days:
            partial_by_machine[machine] = {
                "count": len(partial_days), "ranges": _compress_days(partial_days),
            }
        exact_total += exact_activity
        estimated_total += estimated_activity
        machine_rows.append({
            "machine": machine,
            "state": state,
            "observed_days": len(found_days),
            "expected_days": len(requested_days),
            "missing_day_count": len(missing_days),
            "missing_ranges": _compress_days(missing_days),
            "partial_days": partial_days,
            "observed_activity": sum(total.values()),
            "tokens": total if found_days else None,
            "quality_activity": {"exact": exact_activity, "estimated": estimated_activity},
            "provenance": machine_provenance if prove else None,
        })

    complete = not missing_by_machine and not partial_by_machine and not malformed
    total = metric_total if complete else None
    deferred_machines = [row["machine"] for row in machine_rows if row["state"] == "DEFERRED"]
    reason_codes = []
    if missing_by_machine:
        reason_codes.append("coverage.missing_machine_days")
        reason_codes.append("EXPECTED_DATE_MISSING")
    if partial_by_machine:
        reason_codes.append("coverage.partial_daily_artifact")
        reason_codes.append("SNAPSHOT_INCOMPLETE")
    if deferred_machines:
        reason_codes.append("EXPECTED_NODE_NOT_QUERIED")
    if malformed:
        reason_codes.append("artifact.malformed")
    if estimated_total:
        reason_codes.append("quality.estimated_tokens_present")
    if not source_hashes:
        reason_codes.append("RETRIEVAL_CONFIDENCE_LOW")
    has_observations = bool(source_hashes)
    has_files = has_observations or bool(malformed)
    retryable = bool(deferred_machines or missing_by_machine or partial_by_machine)
    state: TrackerState = {
        "status": "SUCCESS" if complete else "DEGRADED",
        "observation": "PARSED" if has_observations else ("FETCHED" if malformed else "DEFERRED"),
        "completeness": "COMPLETE" if complete else "PARTIAL",
        "conflict": "UNKNOWN",
        "freshness": "UNKNOWN",
        "retrieval": "HIT" if any(row["observed_days"] for row in machine_rows) else "NO_HIT",
        "pagination": "NOT_APPLICABLE",
        "availability": "AVAILABLE",
        "truth_quality": "ESTIMATED" if estimated_total else ("EXACT" if has_observations else "UNKNOWN"),
        "validation": "INVALID" if malformed else "VALID",
        "canonicality": "UNKNOWN",
        "computation": "AGGREGATED" if has_observations else "RAW",
        "provenance": (
            "PROVEN" if prove else (
                "TRACEABLE" if has_observations else ("SOURCE_UNAVAILABLE" if malformed else "SOURCE_MISSING")
            )
        ),
        "federation": (
            "FEDERATION_COMPLETE" if complete else (
                "NODE_FAILED" if malformed and not deferred_machines else (
                    "NODE_DEFERRED" if deferred_machines else "FEDERATION_PARTIAL"
                )
            )
        ),
        "execution": "COMPLETE",
        "anomaly": "GAP" if retryable else "NORMAL",
        "confidence": "HIGH" if complete else ("MEDIUM" if has_observations else "INSUFFICIENT"),
        "flags": {
            "observed": has_files,
            "complete": complete,
            "canonical": None,
            "current": None,
            "exact": has_observations and not bool(estimated_total) and not malformed,
            "validated": not malformed,
            "reconciled": False,
            "estimated": bool(estimated_total),
            "stale": None,
            "partial": not complete,
            "conflicted": None,
            "superseded": None,
            "missing_expected_entities": False,
            "missing_expected_nodes": bool(deferred_machines),
            "missing_expected_dates": bool(missing_by_machine or partial_by_machine),
            "provenance_incomplete": not prove or bool(malformed),
            "retryable": retryable,
            "recoverable": retryable,
            "traceable": bool(prove),
            "deeper_search_available": False,
            "failover_available": False,
            "continuation_available": retryable,
            "exact_source_available": False,
        },
        "reason_codes": reason_codes,
        "evidence": [
            {"source_id": source_id, "sha256": digest if prove else None}
            for source_id, digest in sorted(source_hashes.items())
        ],
        "coverage": {
            "complete": complete,
            "expected_machine_days": len(expected) * len(requested_days),
            "observed_machine_days": sum(row["observed_days"] for row in machine_rows),
            "expected_nodes": expected,
            "observed_nodes": [row["machine"] for row in machine_rows if row["observed_days"]],
            "missing_nodes": deferred_machines,
        },
        "canonical_key": (
            f"total_activity/v1:{scope}:{period}:{lo.isoformat()}:{hi.isoformat()}:{timezone}"
        ),
    }
    state["recovery"] = _recovery(state)
    result = {
        "status": "complete" if complete else "partial",
        "scope": scope,
        "period": period,
        "window": {"start": lo.isoformat(), "end": hi.isoformat(), "timezone": timezone},
        "metric": "total_activity/v1",
        "total": total,
        "observed_total": metric_total,
        "floor": not complete,
        "quality_activity": {"exact": exact_total, "estimated": estimated_total},
        "expected_machines": expected,
        "machines": machine_rows,
        "missing_by_machine": missing_by_machine,
        "partial_by_machine": partial_by_machine,
        "malformed": malformed,
        "coverage": state["coverage"],
        "state": state,
        "group_by": group_by,
        "groups": [{"key": key, "activity": value}
                   for key, value in sorted(group_totals.items())] if group_by == "model" else [],
        "provenance": {"source_hashes": dict(sorted(source_hashes.items()))} if prove else None,
    }
    return result
