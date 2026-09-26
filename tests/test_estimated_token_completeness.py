from tracker_query import tracker_query

def test_estimated_tokens_preserve_activity_without_false_zero(tmp_path):
    # Setup machine directory with a daily containing estimated tokens
    m_dir = tmp_path / "dailies" / "blade1tb"
    m_dir.mkdir(parents=True)
    
    daily_file = m_dir / "2026-09-26.json"
    daily_file.write_text("""{
        "schema": 1,
        "machine": "blade1tb",
        "date": "2026-09-26",
        "partial": true,
        "exact": {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_creation": 0},
        "estimated": {"input_tokens": 337160, "output_tokens": 128526, "cache_read": 19969221, "cache_creation": 0},
        "models": {
            "gemma4:31b-cloud (estimated)": {"input_tokens": 337160, "output_tokens": 128526, "cache_read": 19969221, "cache_creation": 0}
        }
    }""", encoding="utf-8")
    
    result = tracker_query(
        root=tmp_path,
        as_of="2026-09-26",
        period="LATEST",
        machines=["blade1tb"]
    )
    
    assert result["status"] == "partial"
    assert result["floor"] is True
    assert result["observed_total"] == 337160 + 128526 + 19969221
    assert result["quality_activity"]["estimated"] == 337160 + 128526 + 19969221
    assert result["quality_activity"]["exact"] == 0
    assert result["token_basis"] == "estimated"
    assert result["source_partial"] is True
    assert result["state"]["flags"]["estimated"] is True
    assert result["state"]["flags"]["complete"] is False


def test_mixed_exact_and_estimated_multiday_inputs(tmp_path):
    m_dir = tmp_path / "dailies" / "blade1tb"
    m_dir.mkdir(parents=True)

    # Day 1: exact tokens only, closed day
    (m_dir / "2026-09-24.json").write_text("""{
        "schema": 1,
        "machine": "blade1tb",
        "date": "2026-09-24",
        "partial": false,
        "exact": {"input_tokens": 1000, "output_tokens": 500, "cache_read": 2000, "cache_creation": 100},
        "estimated": {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_creation": 0}
    }""", encoding="utf-8")

    # Day 2: estimated tokens only, partial day
    (m_dir / "2026-09-25.json").write_text("""{
        "schema": 1,
        "machine": "blade1tb",
        "date": "2026-09-25",
        "partial": true,
        "exact": {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_creation": 0},
        "estimated": {"input_tokens": 3000, "output_tokens": 1500, "cache_read": 5000, "cache_creation": 200}
    }""", encoding="utf-8")

    result = tracker_query(
        root=tmp_path,
        as_of="2026-09-25",
        period="RANGE",
        start="2026-09-24",
        end="2026-09-25",
        machines=["blade1tb"]
    )

    day1_exact = 1000 + 500 + 2000 + 100
    day2_est = 3000 + 1500 + 5000 + 200

    assert result["status"] == "partial"
    assert result["token_basis"] == "mixed"
    assert result["source_partial"] is True
    assert result["quality_activity"]["exact"] == day1_exact
    assert result["quality_activity"]["estimated"] == day2_est
    assert result["observed_total"] == day1_exact + day2_est
    assert result["state"]["flags"]["complete"] is False
    assert result["state"]["flags"]["estimated"] is True
    assert result["state"]["flags"]["exact"] is False

