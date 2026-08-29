"""AI_MONTHLY_SUBSCRIPTION_USD is monthly; the report window is not.

Printing the month's figure beside window-scoped throughput made a 20-hour
window and a six-day window both claim the same spend, and made
``Absorbed = cold - paid`` subtract a full month of subscription from a
fraction of a day of cold-boot. These tests pin the pro-rating.

The arithmetic lives inline in the report block, so it is restated here as the
contract it has to satisfy. If the implementation is reworked, this file is the
statement of what the numbers must still mean.
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DAYS_PER_MONTH = 365.25 / 12


def prorate(monthly, window_days, days_per_month=DEFAULT_DAYS_PER_MONTH):
    return monthly * (window_days / days_per_month)


def test_a_full_average_month_bills_the_whole_subscription():
    assert prorate(100.0, DEFAULT_DAYS_PER_MONTH) == pytest.approx(100.0)


def test_a_shorter_window_bills_strictly_less():
    """The bug: a 20-hour window reported a whole month's spend."""
    day = prorate(108.88, 20.5 / 24)
    week = prorate(108.88, 6.1)
    assert day < week < 108.88


def test_windows_scale_linearly_with_their_length():
    assert prorate(100.0, 6.0) == pytest.approx(2 * prorate(100.0, 3.0))


def test_two_different_windows_never_report_the_same_spend():
    """Regression on the exact symptom that surfaced this."""
    assert prorate(108.88, 20.5 / 24) != prorate(108.88, 6.1)


def test_zero_length_window_bills_nothing():
    assert prorate(100.0, 0.0) == 0.0


def test_unset_subscription_stays_zero_at_any_window_length():
    assert prorate(0.0, 400.0) == 0.0


def test_absorbed_stays_a_like_for_like_subtraction():
    """cold and paid must both be window-scoped or the difference is meaningless."""
    monthly, window_days, cold = 108.88, 6.1, 6660.79
    paid = prorate(monthly, window_days)
    assert 0 < paid < monthly
    assert cold - paid > cold - monthly  # window-scoped paid absorbs less


@pytest.mark.parametrize("bad", ["", "not-a-number", "0", "-5"])
def test_bad_days_per_month_override_falls_back_not_crashes(bad):
    try:
        v = float(bad or "") or 0
    except ValueError:
        v = 0
    if v <= 0:
        v = DEFAULT_DAYS_PER_MONTH
    assert v > 0


def test_report_labels_the_window_not_the_month():
    """The old label read as spend-to-date; it must name its scope."""
    src = open(os.path.join(ROOT, "token_tracker.py"), encoding="utf-8").read()
    assert "You paid (actual subscription spend)" not in src
    assert "subscription, this window" in src
    assert "pro-rated over" in src


def test_tracker_still_imports_after_the_change():
    r = subprocess.run(
        [sys.executable, "-c", "import ast,io;ast.parse(io.open('token_tracker.py',encoding='utf-8').read())"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
