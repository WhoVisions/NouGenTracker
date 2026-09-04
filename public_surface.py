#!/usr/bin/env python3
"""Validate every JSON record committed beneath the public dailies tree."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import List, Sequence


ALLOWED_TOP_LEVEL = {
    "counter", "date", "estimated", "exact", "generated_at", "generated_by",
    "invocations", "machine", "models", "partial", "provider_stats", "schema",
    "sketch", "sources", "totals",
}

FORBIDDEN = (
    (re.compile(r"[A-Za-z]:\\\\|[A-Za-z]:/"), "machine filesystem path"),
    (re.compile(r"/Users/|/home/|/root/"), "home-directory path"),
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"), "session identifier"),
    (re.compile(r"sk-[A-Za-z0-9]|Bearer\s+[A-Za-z0-9]|api[_-]?key\s*[:=]", re.I),
     "credential shape"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "email address"),
    (re.compile(r"\.jsonl|transcript", re.I), "transcript reference"),
)


def published(root: Path) -> List[Path]:
    dailies = root / "dailies"
    return sorted(dailies.glob("**/*.json")) if dailies.is_dir() else []


def validate(root: Path) -> List[str]:
    """Return privacy/schema errors without reproducing private content."""
    root = root.resolve()
    paths = published(root)
    if not paths:
        return ["dailies: no JSON records found"]
    errors: List[str] = []
    for path in paths:
        rel = path.relative_to(root)
        try:
            blob = path.read_text(encoding="utf-8")
            record = json.loads(blob)
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append(f"{rel}: invalid JSON ({exc})")
            continue
        if not isinstance(record, dict):
            errors.append(f"{rel}: top level is not an object")
            continue
        unexpected = sorted(set(record) - ALLOWED_TOP_LEVEL)
        if unexpected:
            errors.append(f"{rel}: unreviewed top-level fields {unexpected}")
        for pattern, label in FORBIDDEN:
            if pattern.search(blob):
                errors.append(f"{rel}: contains forbidden {label}")
    return errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path,
                        default=Path(__file__).resolve().parent)
    args = parser.parse_args(argv)
    errors = validate(args.repo)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"Public surface OK: {len(published(args.repo.resolve()))} JSON records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
