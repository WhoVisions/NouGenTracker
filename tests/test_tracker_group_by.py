from __future__ import annotations

import json
from pathlib import Path
from tracker_query import tracker_query, _provider_for_model


def test_provider_for_model_mappings():
    assert _provider_for_model("claude-3-5-sonnet-20241022") == "anthropic"
    assert _provider_for_model("anthropic/claude-3-opus") == "anthropic"
    assert _provider_for_model("gpt-4o") == "openai"
    assert _provider_for_model("o1-preview") == "openai"
    assert _provider_for_model("gemini-2.0-flash") == "google"
    assert _provider_for_model("deepseek-chat") == "deepseek"
    assert _provider_for_model("solai:latest") == "ollama"
    assert _provider_for_model("yukiai:latest") == "ollama"
    assert _provider_for_model("gemma-2-9b") == "ollama"
    assert _provider_for_model("unknown-custom-model") == "other"


def test_tracker_query_group_by_provider(tmp_path: Path):
    daily_dir = tmp_path / "dailies" / "blade1tb"
    daily_dir.mkdir(parents=True)
    
    daily_data = {
        "machine": "blade1tb",
        "date": "2026-09-20",
        "partial": False,
        "exact": {
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_creation": 200,
            "cache_read": 100
        },
        "estimated": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation": 0,
            "cache_read": 0
        },
        "models": {
            "claude-3-5-sonnet": {
                "input_tokens": 600,
                "output_tokens": 300,
                "cache_creation": 100,
                "cache_read": 50
            },
            "gpt-4o": {
                "input_tokens": 400,
                "output_tokens": 200,
                "cache_creation": 100,
                "cache_read": 50
            }
        }
    }
    (daily_dir / "2026-09-20.json").write_text(json.dumps(daily_data), encoding="utf-8")

    result = tracker_query(
        tmp_path,
        machines=["blade1tb"],
        period="RANGE",
        start="2026-09-20",
        end="2026-09-20",
        as_of="2026-09-25",
        group_by="provider"
    )

    assert result["group_by"] == "provider"
    groups = {g["key"]: g["activity"] for g in result["groups"]}
    assert "anthropic" in groups
    assert "openai" in groups
    assert groups["anthropic"] == (600 + 300 + 100 + 50)
    assert groups["openai"] == (400 + 200 + 100 + 50)
