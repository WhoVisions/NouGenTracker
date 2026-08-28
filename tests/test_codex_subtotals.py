"""OpenAI nests its usage subtotals; Anthropic does not. Lock the fold.

The tracker's totals and :func:`model_bill` are both written to Anthropic's
rules, where ``input_tokens`` and ``cache_read`` are disjoint and reasoning is
counted separately from output. OpenAI reports the opposite: cached input sits
*inside* ``input_tokens`` and reasoning sits *inside* ``output_tokens``.

Counting an OpenAI payload under Anthropic's rules therefore charges the cached
input twice (full input rate + cache rate) and the reasoning twice. These tests
pin the fold that reconciles the two, because the failure is silent -- it does
not raise, it just quietly inflates every Codex day.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from token_tracker import fold_openai_usage, model_bill


# A real delta lifted from a 2026-08-27 rollout. Note input + output == total,
# with cached and reasoning excluded -- that identity is the whole proof.
REAL = {
    "input_tokens": 1128655,
    "cached_input_tokens": 1042944,
    "cache_write_input_tokens": 0,
    "output_tokens": 7736,
    "reasoning_output_tokens": 604,
    "total_tokens": 1136391,
}


def test_the_payload_identity_this_fold_relies_on():
    """input + output == total. If OpenAI ever breaks this, the fold is wrong."""
    assert REAL["input_tokens"] + REAL["output_tokens"] == REAL["total_tokens"]


def test_folded_parts_sum_back_to_the_raw_buckets():
    f = fold_openai_usage(REAL)
    assert f["input_tokens"] + f["cache_read"] == REAL["input_tokens"]
    assert f["output_tokens"] + f["reasoning"] == REAL["output_tokens"]


def test_tracker_total_formula_reproduces_openais_own_total():
    """The i + o + cc + cr + rt used throughout the report must equal total_tokens."""
    f = fold_openai_usage(REAL)
    counted = (
        f["input_tokens"] + f["output_tokens"] + 0 + f["cache_read"] + f["reasoning"]
    )
    assert counted == REAL["total_tokens"]


def test_unfolded_payload_would_double_count():
    """Guards the regression itself: name the bug so a revert fails loudly."""
    naive = (
        REAL["input_tokens"]
        + REAL["output_tokens"]
        + REAL["cached_input_tokens"]
        + REAL["reasoning_output_tokens"]
    )
    assert naive - REAL["total_tokens"] == 1042944 + 604


def test_cache_share_reflects_a_real_hit_rate():
    """A 97% hit rate must not read as a sub-60% 'cold context leak'."""
    f = fold_openai_usage(REAL)
    total = f["input_tokens"] + f["output_tokens"] + f["cache_read"] + f["reasoning"]
    assert f["cache_read"] / total > 0.90


def test_cached_input_is_not_billed_at_both_rates():
    f = fold_openai_usage(REAL)
    folded_cost, _ = model_bill(
        "gpt-5.6-luna",
        {
            "input_tokens": f["input_tokens"],
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": f["cache_read"],
            "output_tokens": f["output_tokens"],
            "reasoning_tokens": f["reasoning"],
        },
    )
    naive_cost, _ = model_bill(
        "gpt-5.6-luna",
        {
            "input_tokens": REAL["input_tokens"],
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": REAL["cached_input_tokens"],
            "output_tokens": REAL["output_tokens"],
            "reasoning_tokens": REAL["reasoning_output_tokens"],
        },
    )
    assert folded_cost < naive_cost


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        {"input_tokens": 100, "output_tokens": 10},  # no total_tokens reported
    ],
)
def test_degenerate_payloads_do_not_explode(usage):
    f = fold_openai_usage(usage)
    assert all(f[k] >= 0 for k in ("input_tokens", "output_tokens", "cache_read", "reasoning"))


def test_missing_total_falls_back_to_input_plus_output():
    assert fold_openai_usage({"input_tokens": 100, "output_tokens": 10})["total_tokens"] == 110


def test_subtotal_larger_than_its_bucket_is_clamped_not_negative():
    """A schema change must degrade, not emit negative counts into every sum."""
    f = fold_openai_usage(
        {"input_tokens": 10, "cached_input_tokens": 999,
         "output_tokens": 5, "reasoning_output_tokens": 999}
    )
    assert f["input_tokens"] == 0 and f["cache_read"] == 10
    assert f["output_tokens"] == 0 and f["reasoning"] == 5
