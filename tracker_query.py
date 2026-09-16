"""One-call, coverage-aware queries over published tracker daily artifacts."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation", "cache_read")


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
        state = "OBSERVED_ZERO" if not missing_days and not partial_days and all(v == 0 for v in total.values()) else (
            "COMPLETE" if not missing_days else ("PARTIAL" if found_days else "DEFERRED"))
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
        "coverage": {"complete": complete,
                     "expected_machine_days": len(expected) * len(requested_days),
                     "observed_machine_days": sum(row["observed_days"] for row in machine_rows)},
        "group_by": group_by,
        "groups": [{"key": key, "activity": value}
                   for key, value in sorted(group_totals.items())] if group_by == "model" else [],
        "provenance": {"source_hashes": dict(sorted(source_hashes.items()))} if prove else None,
    }
    return result
