"""The fleet gets one throughput number, and every surface must render it.

The bug these tests pin: the MCP connector's `total_activity/v1` summed four
fields off the `exact` bucket only, while fleet_summary.BUCKET_FIELDS counts
five across exact AND estimated. Same dailies, same window, two headline
totals that disagreed by more than half the real volume.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import fleet_summary as fs


def _daily(tmp, machine, date, exact=None, estimated=None, **extra):
    d = tmp / machine
    d.mkdir(parents=True, exist_ok=True)
    rec = {
        "machine": machine,
        "date": date,
        "counter": extra.pop("counter", "cfae0dd41682"),
        "invocations": extra.pop("invocations", 1),
        "exact": {f: 0 for f in fs.BUCKET_FIELDS},
        "estimated": {f: 0 for f in fs.BUCKET_FIELDS},
    }
    rec["exact"].update(exact or {})
    rec["estimated"].update(estimated or {})
    rec.update(extra)
    (d / f"{date}.json").write_text(json.dumps(rec), encoding="utf-8")
    return rec


def test_v1_drops_estimated_and_reasoning(tmp_path):
    """The regression fixture. v1 must undercount; v2 must not.

    Mirrors the shape of the real 2026-08-22..2026-08-29 discrepancy:
    a lane whose volume is overwhelmingly *estimated* (an Antigravity-style
    lane) is nearly invisible to v1.
    """
    _daily(tmp_path, "blade1tb", "2026-08-22",
           exact={"input_tokens": 100, "output_tokens": 10, "reasoning": 7},
           estimated={"cache_read": 1_000_000})
    _daily(tmp_path, "phoebus", "2026-08-23",
           exact={"cache_read": 500, "cache_creation": 25})

    v1 = fs.legacy_total_activity_v1(root=tmp_path)
    v2 = fs.canonical_summary(root=tmp_path)

    # v1 sees only exact, and only four of the five fields.
    assert v1 == 100 + 10 + 500 + 25
    # v2 sees everything.
    assert v2["blended_total"] == 100 + 10 + 7 + 1_000_000 + 500 + 25
    assert v2["exact_tokens"] == 642
    assert v2["estimated_tokens"] == 1_000_000
    assert v2["reasoning"] == 7

    dropped = v2["blended_total"] - v1
    assert dropped == 1_000_007, "v1 must drop every estimated token and reasoning"
    assert dropped / v2["blended_total"] > 0.99


def test_exact_plus_estimated_equals_blended(tmp_path):
    """The invariant: the decomposition must always reconstitute the total."""
    _daily(tmp_path, "whoart", "2026-08-24",
           exact={"input_tokens": 3, "reasoning": 1},
           estimated={"output_tokens": 5})
    s = fs.canonical_summary(root=tmp_path)
    assert s["exact_tokens"] + s["estimated_tokens"] == s["blended_total"]
    lane = s["lanes"][0]
    assert lane["exact_tokens"] + lane["estimated_tokens"] == lane["blended_total"]
    assert sum(l["blended_total"] for l in s["lanes"]) == s["blended_total"]


def test_window_is_inclusive_on_both_ends(tmp_path):
    for day in ("2026-08-21", "2026-08-22", "2026-08-29", "2026-08-30"):
        _daily(tmp_path, "phoebus", day, exact={"input_tokens": 1})
    s = fs.canonical_summary(since="2026-08-22", until="2026-08-29", root=tmp_path)
    assert s["blended_total"] == 2
    assert s["window"]["first_day"] == "2026-08-22"
    assert s["window"]["last_day"] == "2026-08-29"


def test_stale_lane_is_named_not_silently_summed(tmp_path):
    """A stale lane must warn. A quiet lane must never look like a complete one."""
    now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    fresh = (now - timedelta(hours=1)).isoformat()
    old = (now - timedelta(hours=fs.STALE_HOURS + 24)).isoformat()
    _daily(tmp_path, "phoebus", "2026-08-29",
           exact={"input_tokens": 5}, generated_at=fresh)
    _daily(tmp_path, "whoart", "2026-08-25",
           exact={"input_tokens": 5}, generated_at=old)

    s = fs.canonical_summary(root=tmp_path, now=now)
    lanes = {l["lane"]: l for l in s["lanes"]}
    assert lanes["whoart"]["stale"] is True
    assert lanes["phoebus"]["stale"] is False
    assert any("whoart" in w and "exported" in w for w in s["warnings"])


def test_mixed_counting_cohorts_are_flagged(tmp_path):
    """Two lanes on different counting code are not comparable. Say so."""
    _daily(tmp_path, "phoebus", "2026-08-29",
           exact={"input_tokens": 1}, counter="cfae0dd41682")
    _daily(tmp_path, "whoart", "2026-08-29",
           exact={"input_tokens": 1}, counter="3c881c47eb1d")
    s = fs.canonical_summary(root=tmp_path)
    assert any("MIXED COUNTING" in w for w in s["warnings"])


def test_unknown_agent_provenance_is_flagged(tmp_path):
    _daily(tmp_path, "phoebus", "2026-08-29",
           exact={"input_tokens": 1}, generated_by="unknown-agent")
    s = fs.canonical_summary(root=tmp_path)
    assert any("unknown-agent" in w for w in s["warnings"])


def test_empty_window_says_missing_not_zero(tmp_path):
    """Zero must mean measured zero. No telemetry is a different statement."""
    s = fs.canonical_summary(since="2020-01-01", until="2020-01-02", root=tmp_path)
    assert s["blended_total"] == 0
    assert any("not measured zero" in w for w in s["warnings"])
    assert s["confidence"] is None


def test_archived_cohorts_are_excluded(tmp_path):
    """Archived stale-cohort days must not be double counted back in."""
    _daily(tmp_path, "phoebus", "2026-08-29", exact={"input_tokens": 10})
    arch = tmp_path / "phoebus" / "archive-3c881c47eb1d"
    arch.mkdir(parents=True)
    (arch / "2026-08-29.json").write_text(json.dumps({
        "machine": "phoebus", "date": "2026-08-29",
        "exact": {"input_tokens": 999}, "estimated": {},
    }), encoding="utf-8")
    s = fs.canonical_summary(root=tmp_path)
    assert s["blended_total"] == 10


def test_version_is_declared(tmp_path):
    s = fs.canonical_summary(root=tmp_path)
    assert s["version"] == "total_activity/v2"
    assert "reasoning" in s["definition"]
    assert "estimated" in s["definition"]
