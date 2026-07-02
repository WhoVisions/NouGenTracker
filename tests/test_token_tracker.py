"""Dependency-free tests for the pricing core + the audit rubric.

These import token_tracker.py as a module (its argparse is guarded behind
__name__ == '__main__', so importing does not consume argv) and exercise the
deterministic pricing logic — no reliance on any user's local logs.
"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec (the documented importlib recipe) — dataclasses with
    # PEP 563 string annotations resolve their module through sys.modules.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tt = _load("token_tracker", "token_tracker.py")


def test_free_lanes_price_zero():
    assert tt.price_for("google/gemma-4-31b-it:free")[:3] == (0.0, 0.0, 0.0)
    assert tt.price_for("sol-ai:e4b")[:3] == (0.0, 0.0, 0.0)


def test_known_model_documented_price():
    inp, out, cache, src = tt.price_for("claude-opus-4-8")
    assert inp == 5.0 and out == 25.0 and cache == 0.5 and src == tt.DOC


def test_unknown_model_falls_back_to_estimate():
    assert tt.price_for("totally-made-up-model-9000")[3] == tt.EST


def test_estimated_suffix_is_stripped_before_lookup():
    assert tt.price_for("gemini-3-flash-preview (estimated)")[3] == tt.DOC


def test_model_bill_prices_cache_read_cheaper_than_input():
    d = {
        "input_tokens": 1_000_000,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 1_000_000,
        "output_tokens": 0,
        "reasoning_tokens": 0,
    }
    cost, _ = tt.model_bill("claude-opus-4-8", d)
    # 1M input @ $5/M + 1M cache-read @ $0.50/M = $5.50
    assert abs(cost - 5.50) < 1e-6


def test_reasoning_billed_at_output_rate():
    d = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 1_000_000,
    }
    cost, _ = tt.model_bill("claude-opus-4-8", d)
    assert abs(cost - 25.0) < 1e-6  # output rate


def test_config_file_overlay(tmp_path, monkeypatch):
    """TOKEN_TRACKER_CONFIG json overrides paths, knobs, pricing, free lanes."""
    import json
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "chars_per_token": 5,
        "input_warn_threshold": 111,
        "model_pricing": {"my-model": [1.0, 2.0, 0.1, "doc"]},
        "free_models": ["my-free:latest"],
        "model_map": {"RAW_ID_X": "mapped-x"},
    }))
    monkeypatch.setenv("TOKEN_TRACKER_CONFIG", str(cfg))
    mod = _load("token_tracker_cfg_overlay", "token_tracker.py")
    assert mod.CHARS_PER_TOKEN == 5
    assert mod.INPUT_WARN_THRESHOLD == 111
    assert mod.price_for("my-model")[:3] == (1.0, 2.0, 0.1)
    assert mod.price_for("my-free:latest")[:3] == (0.0, 0.0, 0.0)
    assert mod.resolve_model("RAW_ID_X") == "mapped-x"


def test_env_beats_config_file(tmp_path, monkeypatch):
    """A knob's uppercased env var wins over the config file value."""
    import json
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"context_overhead_tokens": 9999}))
    monkeypatch.setenv("TOKEN_TRACKER_CONFIG", str(cfg))
    monkeypatch.setenv("CONTEXT_OVERHEAD_TOKENS", "1234")
    mod = _load("token_tracker_env_beats_cfg", "token_tracker.py")
    assert mod.CONTEXT_OVERHEAD_TOKENS == 1234


def test_missing_config_file_uses_defaults(monkeypatch):
    monkeypatch.setenv("TOKEN_TRACKER_CONFIG", "definitely-not-a-real-file.json")
    mod = _load("token_tracker_no_cfg", "token_tracker.py")
    assert mod.CHARS_PER_TOKEN == 4
    assert mod.CACHE_WRITE_MULTIPLIER == 1.25


def test_audit_rubric_is_ten_points_and_runs():
    ad = _load("audit_daemon", "audit_daemon.py")
    rep = ad.audit()
    assert rep["max"] == 10
    assert 0 <= rep["score"] <= 10
    assert all("check" in r and "pass" in r for r in rep["results"])
