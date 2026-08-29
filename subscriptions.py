"""Monthly subscription spend: resolve it, break it down, scope it to a window.

Two separate defects lived on this value before this module existed.

1. It was a single opaque env var. ``AI_MONTHLY_SUBSCRIPTION_USD=108.88`` says
   nothing about which lanes are in it, so nobody could check it, and a lane
   price change had no obvious place to land.

2. Both ``token_tracker.py`` and ``fleet_summary.py`` printed that MONTHLY
   figure straight into reports whose every other number is scoped to a
   requested window. A 20-hour window and a six-day window reported the same
   spend, and ``Absorbed = cold - paid`` subtracted a whole month of
   subscription from a fraction of a day of cold-boot.

No dollar amounts live in this file. This repository is public; the numbers
stay in the environment. Set either:

    NOUGEN_SUBSCRIPTIONS="anthropic=92.88,google=6.00,openai=10.00"
    AI_MONTHLY_SUBSCRIPTION_USD="108.88"        # legacy, unlabelled total

The first is preferred because it is auditable. The second is kept working so
an existing setup does not silently start reporting zero.
"""
import os

#: Average days in a calendar month. Overridable rather than inlined, per the
#: dynamic-over-hardcode rule; a caller reporting on calendar months may want
#: exactly 30, or 28 for a February-only window.
DEFAULT_DAYS_PER_MONTH = 365.25 / 12


def days_per_month():
    """Resolve the pro-rating divisor from the environment, with a fallback."""
    raw = os.environ.get("NOUGEN_SUB_DAYS_PER_MONTH", "")
    try:
        value = float(raw) if raw else DEFAULT_DAYS_PER_MONTH
    except ValueError:
        value = DEFAULT_DAYS_PER_MONTH
    # A zero or negative divisor would make pro-rating explode or invert. Treat
    # a nonsense override as absent rather than propagating it into a bill.
    return value if value > 0 else DEFAULT_DAYS_PER_MONTH


def monthly_breakdown():
    """Per-lane monthly subscription cost as {name: usd}.

    Reads NOUGEN_SUBSCRIPTIONS ("name=amount" pairs, comma or semicolon
    separated). Unparseable entries are skipped rather than zeroing the whole
    bill, because a typo in one lane should not silently erase the others.
    """
    raw = os.environ.get("NOUGEN_SUBSCRIPTIONS", "").strip()
    if not raw:
        return {}
    out = {}
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, _, amount = chunk.partition("=")
        name = name.strip().lower()
        try:
            value = float(amount.strip())
        except ValueError:
            continue
        if name and value >= 0:
            out[name] = out.get(name, 0.0) + value
    return out


def monthly_total():
    """Total monthly subscription spend, and where the figure came from.

    Returns (usd, source) where source is "breakdown", "legacy" or "unset", so
    a report can say how much to trust the number instead of just printing it.
    """
    breakdown = monthly_breakdown()
    if breakdown:
        return sum(breakdown.values()), "breakdown"
    raw = os.environ.get("AI_MONTHLY_SUBSCRIPTION_USD", "")
    try:
        legacy = float(raw) if raw else 0.0
    except ValueError:
        legacy = 0.0
    if legacy > 0:
        return legacy, "legacy"
    return 0.0, "unset"


def prorate(monthly_usd, window_days, divisor=None):
    """Scale a monthly figure to the window actually being reported."""
    if monthly_usd <= 0 or window_days <= 0:
        return 0.0
    return monthly_usd * (window_days / (divisor or days_per_month()))


def window_cost(window_days):
    """Convenience: resolved monthly total, pro-rated. Returns (usd, monthly, source)."""
    monthly, source = monthly_total()
    return prorate(monthly, window_days), monthly, source
