"""Advice has to come from the data or it is decoration.

The previous version of this section printed four fixed strings, one of which
told every reader forever to "investigate the 2026-06-16 Antigravity spike".
Hardcoded advice survives the problem being fixed and fires when nothing is
wrong, which teaches people to skip the section — and then it is worse than
having none.

So the property under test is not "produces helpful text". It is: says nothing
when there is nothing to say, and names the number and the real date when there
is.
"""
import datetime as dt
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tt = _load("token_tracker", "token_tracker.py")


def inv(day="2026-08-01", source="Claude Code", model="claude-opus-4-8",
        input_tokens=0, output_tokens=0, cache_read=0, cache_creation=0,
        exact=True):
    return {
        "timestamp": dt.datetime.fromisoformat(f"{day}T12:00:00+00:00"),
        "source": source,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read": cache_read,
        "cache_creation": cache_creation,
        "reasoning": 0,
        "exact": exact,
    }


def joined(invocations):
    return " | ".join(tt.route_recommendations(invocations))


# --- silence is a valid answer ----------------------------------------------

def test_no_invocations_produces_no_findings():
    assert tt.route_recommendations([]) == []


def test_a_healthy_window_produces_no_findings():
    """One model, but well-cached, one lane, exact counts, flat days. Nothing
    here is worth a line, and inventing one is the failure mode."""
    healthy = [inv(day=f"2026-08-0{d}", input_tokens=1_000, cache_read=200_000)
               for d in range(1, 6)]
    assert tt.route_recommendations(healthy) == []


def test_a_low_traffic_lane_is_not_judged():
    """Ratios computed over a few thousand tokens are noise, and a report that
    flags noise gets ignored."""
    assert tt.route_recommendations([inv(input_tokens=500, cache_read=0)]) == []


# --- and when there is something to say, it carries the number --------------

def test_cost_concentration_names_the_model_and_its_share():
    heavy = [inv(model="claude-opus-4-8", output_tokens=1_000_000)]
    light = [inv(model="claude-haiku-4-5", output_tokens=1_000)]
    text = joined(heavy + light)
    assert "claude-opus-4-8" in text and "%" in text and "$" in text


def test_a_cold_context_lane_is_flagged_with_its_hit_rate():
    cold = [inv(input_tokens=500_000, cache_read=0)]
    text = joined(cold)
    assert "cache" in text.lower() and "0%" in text


def test_cache_written_but_never_reused_is_flagged():
    churn = [inv(cache_creation=400_000, cache_read=10_000)]
    assert "wrote more cache than it read" in joined(churn)


def test_an_estimated_lane_is_called_estimated():
    guessed = [inv(source="Antigravity (Fallback)", exact=False,
                   input_tokens=100_000) for _ in range(4)]
    assert "ESTIMATED" in joined(guessed)


def test_a_spike_names_the_day_it_actually_found():
    """The direct replacement for the hardcoded date. The flagged day must be
    the outlier in the data, and the quiet days must not be flagged."""
    days = [inv(day=f"2026-08-0{d}", input_tokens=100_000) for d in range(1, 6)]
    days.append(inv(day="2026-08-09", input_tokens=5_000_000))
    text = joined(days)
    assert "2026-08-09" in text
    assert "2026-08-03" not in text
    assert "2026-06-16" not in text  # the string that used to be unconditional


def test_two_days_are_too_few_to_call_a_spike():
    """With two points the median is one of them, so anything larger is 'an
    outlier'. That is arithmetic, not a finding."""
    pair = [inv(day="2026-08-01", input_tokens=100_000),
            inv(day="2026-08-02", input_tokens=9_000_000)]
    assert all("median day" not in line for line in tt.route_recommendations(pair))


def test_each_lane_is_measured_against_its_own_median():
    """A busy lane must not make a quiet lane's normal day look like a spike."""
    rows = []
    for d in range(1, 6):
        rows.append(inv(day=f"2026-08-0{d}", source="Claude Code",
                        input_tokens=10_000_000))
        rows.append(inv(day=f"2026-08-0{d}", source="Gemini CLI",
                        input_tokens=100_000))
    assert all("median day" not in line for line in tt.route_recommendations(rows))
