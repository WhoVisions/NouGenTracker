#!/usr/bin/env python3
"""Passive publication-freshness status for NouGenTracker.

This module is intentionally a different plane from ``token_tracker.py``.
It never scans agent logs, starts a tracker subprocess, creates a cache, writes
a file, or reaches the network.  It lists published daily filenames and reads
at most the newest aggregate record for each expected machine.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


DEFAULT_MACHINES = ("blade1tb", "phoebus", "whoart")
DEFAULT_STALE_AFTER_DAYS = 2
MAX_RECORD_BYTES = 1_048_576
_DAY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")
_MACHINE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def expected_machines() -> List[str]:
    """Configured fleet names, with a small canonical local-first default."""
    raw = os.environ.get("NOUGENTRACKER_EXPECTED_MACHINES", "")
    values = raw.split(",") if raw.strip() else DEFAULT_MACHINES
    return sorted({value.strip().lower() for value in values if value.strip()})


def _read_record(path: Path) -> Dict[str, Any]:
    """Read one bounded, non-symlink aggregate record."""
    if path.is_symlink():
        raise ValueError("latest daily is a symlink")
    with path.open("rb") as handle:
        raw = handle.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        raise ValueError(f"latest daily exceeds {MAX_RECORD_BYTES} bytes")
    record = json.loads(raw.decode("utf-8"))
    if not isinstance(record, dict):
        raise ValueError("latest daily is not a JSON object")
    return record


def _machine_status(root: Path, machine: str, today: date,
                    stale_after_days: int) -> Dict[str, Any]:
    machine_dir = root / "dailies" / machine
    status: Dict[str, Any] = {
        "machine": machine,
        "state": "missing",
        "latest_day": None,
        "age_days": None,
        "partial": None,
        "generated_at": None,
        "detail": "no canonical daily record published",
    }
    if not machine_dir.is_dir() or machine_dir.is_symlink():
        return status

    candidates = []
    for path in machine_dir.iterdir():
        match = _DAY_FILE.fullmatch(path.name)
        if match and path.is_file() and not path.is_symlink():
            candidates.append((match.group(1), path))
    if not candidates:
        return status

    day_text, latest = max(candidates, key=lambda item: item[0])
    status["latest_day"] = day_text
    try:
        day = date.fromisoformat(day_text)
        record = _read_record(latest)
        if record.get("date") != day_text:
            raise ValueError("record date does not match its filename")
        if str(record.get("machine", "")).lower() != machine:
            raise ValueError("record machine does not match its directory")
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        status.update(state="invalid", detail=str(exc))
        return status

    age = (today - day).days
    partial = bool(record.get("partial", False))
    status.update(
        age_days=age,
        partial=partial,
        generated_at=record.get("generated_at"),
    )
    if age < 0:
        status.update(state="future", detail="latest daily is dated in the future")
    elif partial:
        status.update(state="partial", detail="latest daily is explicitly partial")
    elif age > stale_after_days:
        status.update(
            state="stale",
            detail=f"latest daily is {age} days old (limit {stale_after_days})",
        )
    else:
        status.update(state="fresh", detail="publication is within freshness limit")
    return status


def inspect_tracker(root: Path, machines: Optional[Iterable[str]] = None,
                    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
                    today: Optional[date] = None) -> Dict[str, Any]:
    """Return publication health without starting the usage collection plane."""
    if stale_after_days < 0:
        raise ValueError("stale_after_days must be non-negative")
    root = Path(root).resolve()
    names = sorted({name.strip().lower() for name in
                    (machines if machines is not None else expected_machines())
                    if name.strip()})
    invalid = [name for name in names if not _MACHINE.fullmatch(name)]
    if invalid:
        raise ValueError(f"invalid machine name(s): {', '.join(invalid)}")
    check_day = today or datetime.now(timezone.utc).date()
    statuses = [_machine_status(root, name, check_day, stale_after_days)
                for name in names]
    incomplete = [item["machine"] for item in statuses
                  if item["state"] != "fresh"]
    return {
        "mode": "passive_metadata_only",
        "scope": "publication_freshness_not_usage",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checked_day": check_day.isoformat(),
        "tracker_dir": str(root),
        "stale_after_days": stale_after_days,
        "complete": not incomplete,
        "incomplete_machines": incomplete,
        "machines": statuses,
        "side_effects": {
            "raw_logs_read": False,
            "files_written": False,
            "network_used": False,
            "tracker_scan_started": False,
        },
        "caveat": (
            "This reports only whether expected machines recently published "
            "aggregate dailies. It does not measure current token usage, and a "
            "missing or stale machine must never be interpreted as zero usage."
        ),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path,
                        default=Path(__file__).resolve().parent,
                        help="NouGenTracker checkout to inspect")
    parser.add_argument("--machine", action="append", dest="machines",
                        help="expected machine (repeatable; defaults to fleet config)")
    parser.add_argument("--stale-after-days", type=int,
                        default=DEFAULT_STALE_AFTER_DAYS)
    parser.add_argument("--json", action="store_true",
                        help="emit the structured result")
    args = parser.parse_args(argv)
    result = inspect_tracker(args.repo, args.machines, args.stale_after_days)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("NouGenTracker passive publication status")
        for item in result["machines"]:
            print(f"  {item['machine']:<12} {item['state']:<8} "
                  f"{item['latest_day'] or '-'}  {item['detail']}")
        print("No raw logs read; no files written; no network; no tracker scan.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
