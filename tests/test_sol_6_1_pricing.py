"""Standard API-equivalent rates from OpenAI's model page, 2026-09-29.

https://developers.openai.com/api/docs/models/gpt-6.1-sol
All resolver tests are offline and use an isolated cache.
"""
import pytest

import pricing_live
import token_tracker as tracker


@pytest.fixture(autouse=True)
def offline_pricing(monkeypatch, tmp_path):
    pricing_live.reset_process_state()
    monkeypatch.setenv("NOUGEN_PRICING_CACHE_PATH", str(tmp_path / "prices.json"))
    monkeypatch.delenv("NOUGEN_PRICE_GPT_6_1_SOL", raising=False)

    def unavailable(*args, **kwargs):
        raise OSError("offline pricing test")

    monkeypatch.setattr(pricing_live.urllib.request, "urlopen", unavailable)
    yield
    pricing_live.reset_process_state()


def test_exact_model_uses_official_fallback():
    assert tracker.MODEL_PRICING["gpt-6.1-sol"] == (2.0, 10.0, 0.1, tracker.DOC)
    assert tracker.price_for("gpt-6.1-sol") == (2.0, 10.0, 0.1, pricing_live.FALLBACK_CONST)
    assert tracker.price_for("gpt-6.1-sol")[:3] != tracker.DEFAULT_PRICING[:3]


@pytest.mark.parametrize("suffix", ["low", "medium", "high", "xhigh", "max", "ultra"])
def test_effort_variants_keep_base_rates_and_inferred_provenance(suffix):
    assert tracker.price_for(f"gpt-6.1-sol-{suffix}") == (2.0, 10.0, 0.1, tracker.EST)
    # Direct resolver callers also support the new suffixes.
    assert pricing_live.resolve_price(
        f"gpt-6.1-sol-{suffix}", fallback_pricing=tracker.MODEL_PRICING
    ) == (2.0, 10.0, 0.1, tracker.EST)


def test_display_name_and_estimated_label_resolve():
    assert tracker.price_for("GPT-6.1 Sol (estimated)")[:3] == (2.0, 10.0, 0.1)


def test_mixed_token_bucket_prices_each_bucket_once():
    # Aggregate totals do not imply a single long-context request.
    cost, source = tracker.model_bill("gpt-6.1-sol", {
        "input_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
        "output_tokens": 600_000,
        "reasoning_tokens": 400_000,
    }, "2026-09-29")
    assert cost == pytest.approx(14.6)  # 2 + .1 + 2.5 + 10
    assert source == pricing_live.FALLBACK_CONST


def test_new_model_does_not_reprice_previous_sol():
    assert tracker.price_for("gpt-5.6-sol", "2026-09-29")[:3] == (5.0, 30.0, 0.5)


def test_live_table_parser_preserves_five_percent_cache_rate():
    # Minimal official table shape, including cache writes before output.
    html = ('<table><tr><td>gpt-6.1-sol</td><td>$2.00</td>'
            '<td>$0.10</td><td>$2.50</td><td>$10.00</td></tr></table>')
    assert pricing_live.parse_openai_pricing(html)["gpt-6.1-sol"] == (2.0, 10.0, 0.1)
