"""Pricing-table tests.

A missing model does not fail loudly — it silently bills at DEFAULT_PRICING,
which is how 246M claude-opus-5 tokens came to be costed at a fifth of their
real price. These assert the table covers what the fleet actually runs.
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

CLAUDE_5_FAMILY = ("claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-haiku-4-5")


def test_five_family_models_are_priced():
    for model in CLAUDE_5_FAMILY:
        assert model in tt.MODEL_PRICING, f"{model} missing — would bill at the default"


def test_opus_5_is_not_billed_at_the_fallback_rate():
    assert tt.price_for("claude-opus-5")[:3] == (5.00, 25.00, 0.500)
    assert tt.price_for("claude-opus-5")[:3] != tt.DEFAULT_PRICING[:3]


def test_sonnet_5_is_priced_at_list():
    assert tt.price_for("claude-sonnet-5")[:3] == (3.00, 15.00, 0.300)


def test_cache_read_is_a_tenth_of_input_across_claude_models():
    """The documented ratio. A typo here is a silent mis-bill, not a failure."""
    for model in CLAUDE_5_FAMILY:
        inp, _out, cache, _src = tt.MODEL_PRICING[model]
        assert abs(cache - inp / 10) < 1e-9, f"{model}: cache {cache} != {inp}/10"


def test_the_estimated_suffix_still_resolves():
    """Antigravity appends ' (estimated)'; it must not defeat the lookup."""
    assert tt.price_for("claude-opus-5 (estimated)") == tt.price_for("claude-opus-5")


def test_unknown_models_still_fall_back_rather_than_reading_zero():
    assert tt.price_for("some-model-nobody-has-heard-of") == tt.DEFAULT_PRICING
    assert tt.DEFAULT_PRICING[0] > 0, "a $0 default would hide spend entirely"
