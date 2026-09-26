from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from tracker_query import tracker_query


def test_current_day_is_live_and_never_complete_before_midnight(tmp_path):
    tz_str = "America/New_York"
    tz = ZoneInfo(tz_str)
    today = datetime.now(tz).date().isoformat()
    
    # Write a daily artifact for today
    m_dir = tmp_path / "dailies" / "blade1tb"
    m_dir.mkdir(parents=True)
    
    daily_file = m_dir / f"{today}.json"
    daily_file.write_text(f"""{{
        "schema": 1,
        "machine": "blade1tb",
        "date": "{today}",
        "partial": false,
        "exact": {{"input_tokens": 1000, "output_tokens": 500, "cache_read": 2000, "cache_creation": 0}},
        "estimated": {{"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_creation": 0}},
        "models": {{
            "claude-3-7-sonnet": {{"input_tokens": 1000, "output_tokens": 500, "cache_read": 2000, "cache_creation": 0}}
        }}
    }}""", encoding="utf-8")
    
    res = tracker_query(
        root=tmp_path,
        as_of=today,
        period="LATEST",
        timezone=tz_str,
        machines=["blade1tb"]
    )
    
    # Invariant: Current day is open -> period_closed is False, complete is False
    assert res["period_closed"] is False
    assert res["aggregation_complete"] is True
    assert res["source_complete"] is True
    assert res["status"] == "partial"
    assert res["state"]["flags"]["complete"] is False
    assert res["state"]["flags"]["period_closed"] is False
    assert res["state"]["flags"]["aggregation_complete"] is True
    assert res["state"]["freshness"] == "LIVE"
    assert "period.current_day_open" in res["state"]["reason_codes"]
    assert "TEMPORAL_WINDOW_OPEN" in res["state"]["reason_codes"]
    assert res["measurement_basis"] == "exact"
    assert res["observed_total"] == 3500


def test_past_day_finalizes_as_complete_when_all_sources_present(tmp_path):
    tz_str = "America/New_York"
    tz = ZoneInfo(tz_str)
    past_day = (datetime.now(tz).date() - timedelta(days=2)).isoformat()
    
    m_dir = tmp_path / "dailies" / "blade1tb"
    m_dir.mkdir(parents=True)
    
    daily_file = m_dir / f"{past_day}.json"
    daily_file.write_text(f"""{{
        "schema": 1,
        "machine": "blade1tb",
        "date": "{past_day}",
        "partial": false,
        "exact": {{"input_tokens": 2000, "output_tokens": 1000, "cache_read": 4000, "cache_creation": 0}},
        "estimated": {{"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_creation": 0}},
        "models": {{
            "claude-3-7-sonnet": {{"input_tokens": 2000, "output_tokens": 1000, "cache_read": 4000, "cache_creation": 0}}
        }}
    }}""", encoding="utf-8")
    
    res = tracker_query(
        root=tmp_path,
        as_of=past_day,
        period="LATEST",
        timezone=tz_str,
        machines=["blade1tb"]
    )
    
    # Past day: period_closed is True -> complete is True
    assert res["period_closed"] is True
    assert res["aggregation_complete"] is True
    assert res["source_complete"] is True
    assert res["status"] == "complete"
    assert res["state"]["flags"]["complete"] is True
    assert res["state"]["flags"]["period_closed"] is True
    assert res["state"]["freshness"] == "CURRENT"
    assert res["total"] == 7000
    assert res["measurement_basis"] == "exact"


def test_measurement_basis_mixed_and_estimated(tmp_path):
    tz_str = "America/New_York"
    tz = ZoneInfo(tz_str)
    past_day = (datetime.now(tz).date() - timedelta(days=1)).isoformat()
    
    m_dir = tmp_path / "dailies" / "blade1tb"
    m_dir.mkdir(parents=True)
    
    # Mixed: exact + estimated
    daily_file = m_dir / f"{past_day}.json"
    daily_file.write_text(f"""{{
        "schema": 1,
        "machine": "blade1tb",
        "date": "{past_day}",
        "partial": false,
        "exact": {{"input_tokens": 100, "output_tokens": 100, "cache_read": 0, "cache_creation": 0}},
        "estimated": {{"input_tokens": 500, "output_tokens": 500, "cache_read": 0, "cache_creation": 0}},
        "models": {{
            "claude-3-7-sonnet": {{"input_tokens": 100, "output_tokens": 100, "cache_read": 0, "cache_creation": 0}},
            "gemma4:31b (estimated)": {{"input_tokens": 500, "output_tokens": 500, "cache_read": 0, "cache_creation": 0}}
        }}
    }}""", encoding="utf-8")
    
    res = tracker_query(
        root=tmp_path,
        as_of=past_day,
        period="LATEST",
        timezone=tz_str,
        machines=["blade1tb"]
    )
    
    assert res["measurement_basis"] == "mixed"
    assert res["token_basis"] == "mixed"
    assert res["quality_activity"]["exact"] == 200
    assert res["quality_activity"]["estimated"] == 1000
    assert res["observed_total"] == 1200
