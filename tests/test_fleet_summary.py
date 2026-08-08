"""The dashboard's numbers come from the dailies — the fleet's one transport.

This pins the re-cut of the relay work (old PR #1): dashboard.py renders a
FleetSummary, and fleet_summary.py builds that summary from the committed
dailies rather than from a second rollup transport. The doubts the page
surfaces (double counting, stale peers, inferred tokens) must come out of the
same files the fleet already trusts.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import fleet_summary as fs


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def daily(machine, date, model="claude-opus-5", tokens=1000, estimated=0,
          invocations=10, generated_at=None):
    bucket = {"input_tokens": tokens, "output_tokens": tokens // 10,
              "cache_creation": 0, "cache_read": tokens * 5, "reasoning": 0}
    return {
        "counter": "3c881c47eb1d",
        "date": date,
        "machine": machine,
        "invocations": invocations,
        "exact": dict(bucket),
        "estimated": {"input_tokens": estimated, "output_tokens": 0,
                      "cache_creation": 0, "cache_read": 0, "reasoning": 0},
        "models": {model: bucket},
        "generated_at": generated_at or f"{date}T23:59:00+00:00",
    }


def write_dailies(root: Path, records):
    for rec in records:
        d = root / rec["machine"]
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{rec['date']}.json").write_text(json.dumps(rec), encoding="utf-8")


def test_summary_folds_all_machines(tmp_path, monkeypatch):
    monkeypatch.setenv("NOUGEN_MACHINE", "phoebus")
    write_dailies(tmp_path, [
        daily("phoebus", "2026-08-01"),
        daily("blade1tb", "2026-08-01"),
        daily("whoart", "2026-08-02"),
    ])
    s = fs.fleet_summary(root=tmp_path, now=NOW)
    assert set(s.machines) == {"phoebus", "blade1tb", "whoart"}
    assert s.days == ["2026-08-01", "2026-08-02"]
    assert s.total_tokens == sum(m["tokens"] for m in s.machines.values())
    assert s.total_cost > 0
    assert s.local == "phoebus"


def test_identical_buckets_on_two_machines_flag_double_counting(tmp_path, monkeypatch):
    """Two boxes publishing the same nonzero model-day bucket almost certainly
    counted the same calls — the page must say so rather than quietly sum."""
    monkeypatch.setenv("NOUGEN_MACHINE", "phoebus")
    write_dailies(tmp_path, [
        daily("phoebus", "2026-08-01", tokens=7777),
        daily("blade1tb", "2026-08-01", tokens=7777),
    ])
    s = fs.fleet_summary(root=tmp_path, now=NOW)
    assert len(s.overlaps) == 1
    pair = {s.overlaps[0].machine_a, s.overlaps[0].machine_b}
    assert pair == {"phoebus", "blade1tb"}


def test_a_quiet_peer_is_reported_stale_not_omitted(tmp_path, monkeypatch):
    monkeypatch.setenv("NOUGEN_MACHINE", "phoebus")
    write_dailies(tmp_path, [
        daily("phoebus", "2026-08-08", generated_at="2026-08-08T11:00:00+00:00"),
        daily("blade1tb", "2026-08-01", generated_at="2026-08-01T23:59:00+00:00"),
    ])
    s = fs.fleet_summary(root=tmp_path, now=NOW)
    stale = {m for m, _ts, _age, is_stale in s.freshness if is_stale}
    assert stale == {"blade1tb"}
    # blade's numbers still count — stale is a flag, not an exclusion.
    assert "blade1tb" in s.machines


def test_estimated_tokens_lower_confidence_and_name_the_machine(tmp_path, monkeypatch):
    monkeypatch.setenv("NOUGEN_MACHINE", "phoebus")
    write_dailies(tmp_path, [
        daily("phoebus", "2026-08-01"),
        daily("blade1tb", "2026-08-01", estimated=500_000),
    ])
    s = fs.fleet_summary(root=tmp_path, now=NOW)
    assert s.confidence is not None and s.confidence < 1.0
    assert s.inferred == ["blade1tb"]


def test_the_page_renders_from_real_summary(tmp_path, monkeypatch):
    """End to end: dailies -> summary -> self-contained HTML."""
    monkeypatch.setenv("NOUGEN_MACHINE", "phoebus")
    write_dailies(tmp_path, [
        daily("phoebus", "2026-08-01"),
        daily("blade1tb", "2026-08-01", estimated=500_000),
    ])
    import dashboard
    s = fs.fleet_summary(root=tmp_path, now=NOW)
    page = dashboard.render(s)
    assert "phoebus" in page and "blade1tb" in page
    assert "http" not in page.split("</footer>")[0].lower().replace(
        "http-equiv", "")  # no external assets


def test_days_window_filters_old_records(tmp_path, monkeypatch):
    monkeypatch.setenv("NOUGEN_MACHINE", "phoebus")
    write_dailies(tmp_path, [
        daily("phoebus", "2026-05-01"),
        daily("phoebus", "2026-08-07"),
    ])
    s = fs.fleet_summary(days=7, root=tmp_path, now=NOW)
    assert s.days == ["2026-08-07"]
