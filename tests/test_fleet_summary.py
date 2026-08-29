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


# --- Accounting Invariants & Hero Metrics Reorder Tests -----------------------

def test_accounting_invariants_on_summary(tmp_path, monkeypatch):
    """Invariant a: cold >= cached_realistic >= 0 and paid >= 0.
    Invariant b: absorbed == cold - paid, never displayed negative.
    Invariant c: derived from single summary source of truth."""
    monkeypatch.setenv("NOUGEN_MACHINE", "phoebus")
    monkeypatch.setenv("AI_MONTHLY_SUBSCRIPTION_USD", "20.00")
    write_dailies(tmp_path, [
        daily("phoebus", "2026-08-01", model="claude-opus-5", tokens=100_000),
    ])
    s = fs.fleet_summary(root=tmp_path, now=NOW)

    # Invariant a
    assert s.cold_cost >= s.total_cost >= 0.0
    assert s.paid_cost >= 0.0
    assert s.paid_cost == 20.00

    # Invariant b
    expected_absorbed = max(0.0, s.cold_cost - s.paid_cost)
    assert abs(s.absorbed_cost - expected_absorbed) < 1e-4
    assert s.absorbed_cost >= 0.0


def test_validate_and_clamp_accounting_invariants():
    """Direct invariant unit tests on validate_and_clamp_accounting."""
    # 1. Normal case
    cold, real, paid, abs_val = fs.validate_and_clamp_accounting(100.0, 40.0, 20.0)
    assert cold == 100.0
    assert real == 40.0
    assert paid == 20.0
    assert abs_val == 80.0

    # 2. Paid exceeds cold -> absorbed clamped to 0, never negative
    cold, real, paid, abs_val = fs.validate_and_clamp_accounting(15.0, 10.0, 50.0)
    assert cold == 15.0
    assert real == 10.0
    assert paid == 50.0
    assert abs_val == 0.0

    # 3. Negative paid clamped to 0
    cold, real, paid, abs_val = fs.validate_and_clamp_accounting(50.0, 20.0, -10.0)
    assert paid == 0.0
    assert abs_val == 50.0

    # 4. Inconsistent cold < realistic -> cold clamped to realistic
    cold, real, paid, abs_val = fs.validate_and_clamp_accounting(10.0, 30.0, 5.0)
    assert cold == 30.0
    assert real == 30.0
    assert abs_val == 25.0


def test_dashboard_rendered_narrative_order(tmp_path, monkeypatch):
    """Verify narrative order in rendered HTML:
    1. Throughput first (hero)
    2. API equivalent second (cold-boot)
    3. Absorbed third (cold - paid)
    4. You paid last (subscription spend)."""
    monkeypatch.setenv("NOUGEN_MACHINE", "phoebus")
    monkeypatch.setenv("AI_MONTHLY_SUBSCRIPTION_USD", "40.00")
    write_dailies(tmp_path, [
        daily("phoebus", "2026-08-01", model="claude-opus-5", tokens=200_000),
    ])
    import dashboard
    s = fs.fleet_summary(root=tmp_path, now=NOW)
    html = dashboard.render(s)

    # Narrative order proof: locate each section in HTML string
    hero_idx = html.index('class="hero"')
    hero_note_idx = html.index('class="hero-note"')
    api_equiv_idx = html.index('<div class="k">api equivalent</div>')
    absorbed_idx = html.index('<div class="k">absorbed</div>')
    you_paid_idx = html.index('<div class="k">you paid</div>')

    # Assert narrative order: Hero (Throughput) -> API Equivalent -> Absorbed -> You Paid
    assert hero_idx < hero_note_idx < api_equiv_idx < absorbed_idx < you_paid_idx

    # Hero must lead with tokens (not dollars)
    hero_snippet = html[hero_idx:hero_note_idx]
    assert "$" not in hero_snippet
    assert dashboard._fmt_tokens(s.total_tokens) in hero_snippet


def test_estimated_marker_propagation_in_dashboard(tmp_path, monkeypatch):
    """When estimated tokens are present, estimated marker (~) propagates to hero & tiles."""
    monkeypatch.setenv("NOUGEN_MACHINE", "phoebus")
    write_dailies(tmp_path, [
        daily("phoebus", "2026-08-01", tokens=100_000),
        daily("blade1tb", "2026-08-01", tokens=50_000, estimated=50_000),
    ])
    import dashboard
    s = fs.fleet_summary(root=tmp_path, now=NOW)
    assert s.is_estimated is True
    assert s.confidence < 1.0

    html = dashboard.render(s)
    # Hero carries estimated marker
    hero_idx = html.index('class="hero"')
    hero_note_idx = html.index('class="hero-note"')
    tiles_idx = html.index('class="tiles"')

    hero_str = html[hero_idx:hero_note_idx]
    hero_note_str = html[hero_note_idx:tiles_idx]
    assert "~" in hero_str
    assert "estimated (~)" in hero_note_str
    assert f"{s.confidence:.1%} exact" in hero_note_str

    # When 100% exact (no estimated tokens)
    s_exact = fs.FleetSummary(
        machines={}, days=["2026-08-01"], total_cost=10.0, total_tokens=100_000,
        cache_read=50_000, confidence=1.0, inferred=[], overlaps=[], freshness=[], local="phoebus",
        cold_cost=20.0, paid_cost=0.0, absorbed_cost=20.0, exact_tokens=100_000, estimated_tokens=0,
    )
    assert s_exact.is_estimated is False
    html_exact = dashboard.render(s_exact)
    hero_exact_str = html_exact[html_exact.index('class="hero"'):html_exact.index('class="hero-note"')]
    hero_note_exact_str = html_exact[html_exact.index('class="hero-note"'):html_exact.index('class="tiles"')]
    assert "~" not in hero_exact_str
    assert "100% exact" in hero_note_exact_str

