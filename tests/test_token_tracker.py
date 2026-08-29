"""Dependency-free tests for the pricing core + the audit rubric.

These import token_tracker.py as a module (its argparse is guarded behind
__name__ == '__main__', so importing does not consume argv) and exercise the
deterministic pricing logic — no reliance on any user's local logs.
"""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
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


def test_cold_boot_pricing_vs_realistic_and_absorbed():
    """Cold-boot prices all input-side tokens at base input rate (no cache discount)."""
    inp_rate, out_rate, _cr, _src = tt.price_for("claude-opus-4-8")
    d = {
        "input_tokens": 1_000_000,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 1_000_000,
        "output_tokens": 100_000,
        "reasoning_tokens": 0,
    }
    realistic_cost, _ = tt.model_bill("claude-opus-4-8", d)
    # Realistic: 1M in ($5) + 1M cache ($0.5) + 0.1M out ($2.5) = $8.00
    assert abs(realistic_cost - 8.00) < 1e-6

    # Cold boot: (1M in + 1M cache) @ $5 in + 0.1M out ($2.5) = $12.50
    cold_cost = float(tt.calculate_cost(
        input_tokens=(1_000_000 + 1_000_000),
        output_tokens=100_000,
        inp_rate=inp_rate,
        out_rate=out_rate,
    ))
    assert abs(cold_cost - 12.50) < 1e-6
    assert cold_cost >= realistic_cost

    # Absorbed with paid = 2.50
    from decimal import Decimal
    paid_dec = Decimal("2.50")
    cold_dec = tt.round_to_cents(Decimal(str(cold_cost)))
    absorbed = float(cold_dec - paid_dec)
    assert abs(absorbed - 10.00) < 1e-6


def test_audit_rubric_is_ten_points_and_runs():
    ad = _load("audit_daemon", "audit_daemon.py")
    rep = ad.audit()
    assert rep["max"] == 10
    assert 0 <= rep["score"] <= 10
    assert all("check" in r and "pass" in r for r in rep["results"])

