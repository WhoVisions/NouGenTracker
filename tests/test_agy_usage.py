"""agy reports thinking INSIDE output, and model_bill charges both.

That combination is the whole reason this module needs tests. `model_bill`
prices (output_tokens + reasoning_tokens) at the output rate, and agy's
`usage.output_tokens` already contains `thinking_tokens` — verified twice on
2026-08-01, the arithmetic closing exactly both times. Log the two fields as
reported and every reasoning token is billed twice, silently, on a lane whose
entire purpose is to be the EXACTLY counted one.

The second thing kept fixed here is the ledger path. fleet_usage_log came from
Watchtower with a parents[3] fallback that, in this repo, points at
C:\\Users\\super\\vault — while token_tracker reads <repo>/vault. A writer and a
reader disagreeing about the file is not an error anyone sees: rows are written,
nothing raises, and the tracker reports zero fleet usage forever.
"""
import importlib.util
import json
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


agy = _load("agy_usage", "fleet/agy_usage.py")
ledger = _load("fleet_usage_log", "fleet/fleet_usage_log.py")
tracker = _load("token_tracker", "token_tracker.py")


# Both real samples, captured from `agy -p --output-format json` on 2026-08-01.
SAMPLE_CHEAP = {
    "conversation_id": "e895cc2b-6f50-48d8-8ed5-fe8cacaaa30e",
    "status": "SUCCESS",
    "response": "ok\n",
    "duration_seconds": 35.6305624,
    "usage": {"input_tokens": 32861, "output_tokens": 28, "thinking_tokens": 24,
              "cache_read_tokens": 0, "total_tokens": 32889},
}
SAMPLE_THINKING = {
    "conversation_id": "0f8395ee-3693-4f3e-a478-364cb6c1d1d7",
    "status": "SUCCESS",
    "response": "391\n",
    "duration_seconds": 37.5986613,
    "usage": {"input_tokens": 31715, "output_tokens": 695, "thinking_tokens": 689,
              "cache_read_tokens": 0, "total_tokens": 32410},
}


@pytest.mark.parametrize("sample", [SAMPLE_CHEAP, SAMPLE_THINKING])
def test_thinking_is_inside_output_in_the_samples(sample):
    """The invariant the split depends on, asserted against the real captures."""
    u = sample["usage"]
    assert u["input_tokens"] + u["output_tokens"] == u["total_tokens"]
    assert u["thinking_tokens"] <= u["output_tokens"]


@pytest.mark.parametrize("sample", [SAMPLE_CHEAP, SAMPLE_THINKING])
def test_split_preserves_the_output_total(sample):
    u = sample["usage"]
    out, reasoning = agy.split_thinking(u["output_tokens"], u["thinking_tokens"])
    assert out + reasoning == u["output_tokens"]
    assert reasoning == u["thinking_tokens"]


# A continued conversation, and a tool-using run. Both real, both 2026-08-01.
SAMPLE_CACHED = {
    "conversation_id": "7cec483b-366e-4cd1-bd50-12996633ba68",
    "status": "SUCCESS",
    "response": "two\n",
    "usage": {"input_tokens": 52543, "output_tokens": 351, "thinking_tokens": 345,
              "cache_read_tokens": 12502, "total_tokens": 52894},
}
SAMPLE_TOOLS = {
    "conversation_id": "6451d244-5020-4aaa-9155-8fa24803e203",
    "status": "SUCCESS",
    "response": "There are **8** files...",
    "usage": {"input_tokens": 107053, "output_tokens": 6139, "thinking_tokens": 5102,
              "cache_read_tokens": 377419, "total_tokens": 113192},
}

ALL_SAMPLES = [SAMPLE_CHEAP, SAMPLE_THINKING, SAMPLE_CACHED, SAMPLE_TOOLS]


@pytest.mark.parametrize("sample", ALL_SAMPLES)
def test_total_is_input_plus_output_and_excludes_cache_reads(sample):
    u = sample["usage"]
    assert u["input_tokens"] + u["output_tokens"] == u["total_tokens"]


def test_cache_reads_are_not_a_subset_of_input():
    """The reason there is no split_cached, kept as evidence rather than a comment.

    Two real rows report MORE cache read than input, so a reader who assumes the
    thinking-inside-output pattern generalises and subtracts would understate
    fresh input — 12,502 tokens on the continued conversation.
    """
    for sample in (SAMPLE_CACHED, SAMPLE_TOOLS):
        u = sample["usage"]
        assert u["cache_read_tokens"] > 0
    assert SAMPLE_TOOLS["usage"]["cache_read_tokens"] > SAMPLE_TOOLS["usage"]["input_tokens"]


@pytest.mark.parametrize("sample", ALL_SAMPLES)
def test_ask_logs_input_and_cache_read_exactly_as_reported(sample, tmp_path, monkeypatch):
    path = tmp_path / "fleet_usage.jsonl"
    monkeypatch.setenv("FLEET_USAGE_LEDGER", str(path))
    monkeypatch.setattr(agy, "agy_binary", lambda: "agy")
    monkeypatch.setattr(agy.subprocess, "run", _fake_run(sample))

    u = sample["usage"]
    got = agy.ask("q", log=True)["usage"]
    assert got["input_tokens"] == u["input_tokens"]
    assert got["cached_tokens"] == u["cache_read_tokens"]
    # Output is the only field that is split, and the split is lossless.
    assert got["output_tokens"] + got["reasoning_tokens"] == u["output_tokens"]


def test_split_refuses_to_invent_reasoning_it_cannot_justify():
    # If thinking ever exceeds output the invariant is gone; under-report
    # reasoning rather than hand model_bill a negative output count.
    assert agy.split_thinking(10, 99) == (10, 0)
    assert agy.split_thinking(0, 0) == (0, 0)
    assert agy.split_thinking(None, None) == (0, 0)


def test_splitting_does_not_change_what_the_lane_costs():
    """Split vs unsplit must bill the same, or the fix traded one error for another."""
    u = SAMPLE_THINKING["usage"]
    out, reasoning = agy.split_thinking(u["output_tokens"], u["thinking_tokens"])
    split = {"input_tokens": u["input_tokens"], "output_tokens": out,
             "cache_read_input_tokens": 0, "reasoning_tokens": reasoning,
             "cache_creation_input_tokens": 0}
    whole = dict(split, output_tokens=u["output_tokens"], reasoning_tokens=0)
    assert tracker.model_bill(agy.DEFAULT_MODEL, split)[0] == pytest.approx(
        tracker.model_bill(agy.DEFAULT_MODEL, whole)[0])


def test_logging_both_fields_as_reported_would_have_double_billed():
    """The bug this module is written to avoid, priced so it cannot be waved off."""
    u = SAMPLE_THINKING["usage"]
    honest = {"input_tokens": u["input_tokens"], "output_tokens": u["output_tokens"],
              "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
              "reasoning_tokens": 0}
    naive = dict(honest, reasoning_tokens=u["thinking_tokens"])
    assert tracker.model_bill(agy.DEFAULT_MODEL, naive)[0] > \
        tracker.model_bill(agy.DEFAULT_MODEL, honest)[0]


def test_the_default_model_has_a_documented_price():
    """An exactly-counted row billed at DEFAULT_PRICING is still an estimate."""
    _, _, _, src = tracker.price_for(agy.DEFAULT_MODEL)
    assert src == tracker.DOC


def test_writer_and_tracker_agree_on_the_ledger_file(monkeypatch):
    monkeypatch.delenv("FLEET_USAGE_LEDGER", raising=False)
    monkeypatch.delenv("SOL_VAULT_DIR", raising=False)
    assert pathlib.Path(ledger._ledger_path()) == pathlib.Path(tracker.FLEET_USAGE_LEDGER)


def test_parse_agy_json_skips_noise_before_the_result():
    stdout = "\n".join([
        "checking for updates...",
        json.dumps({"note": "not the result, no usage key"}),
        json.dumps(SAMPLE_CHEAP),
    ])
    assert agy.parse_agy_json(stdout)["conversation_id"] == SAMPLE_CHEAP["conversation_id"]


def test_parse_agy_json_raises_rather_than_returning_empty_usage():
    with pytest.raises(agy.AgyError):
        agy.parse_agy_json("agy: something went wrong\n")


def _fake_run(sample, returncode=0, stderr=""):
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, returncode, stdout=json.dumps(sample), stderr=stderr)
    return run


def test_ask_logs_an_exact_row_the_tracker_reads_back(tmp_path, monkeypatch):
    """End to end on the accounting: ask -> ledger -> parse_fleet_usage."""
    path = tmp_path / "fleet_usage.jsonl"
    monkeypatch.setenv("FLEET_USAGE_LEDGER", str(path))
    monkeypatch.setattr(agy, "agy_binary", lambda: "agy")
    monkeypatch.setattr(agy.subprocess, "run", _fake_run(SAMPLE_THINKING))

    result = agy.ask("what is 17*23?", model="gemini-3.5-flash-high")
    assert result["logged"] is True

    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["provider"] == agy.PROVIDER and row["lane"] == agy.LANE
    assert row["output_tokens"] + row["reasoning_tokens"] == \
        SAMPLE_THINKING["usage"]["output_tokens"]
    assert row["input_tokens"] == SAMPLE_THINKING["usage"]["input_tokens"]


def test_ask_can_answer_without_writing_a_row(tmp_path, monkeypatch):
    path = tmp_path / "fleet_usage.jsonl"
    monkeypatch.setenv("FLEET_USAGE_LEDGER", str(path))
    monkeypatch.setattr(agy, "agy_binary", lambda: "agy")
    monkeypatch.setattr(agy.subprocess, "run", _fake_run(SAMPLE_CHEAP))

    result = agy.ask("ping", log=False)
    assert result["logged"] is False
    assert not path.exists()


def test_ask_raises_when_the_box_has_no_agy(monkeypatch):
    monkeypatch.setattr(agy, "agy_binary", lambda: None)
    with pytest.raises(agy.AgyError, match="not found"):
        agy.ask("anything")


def test_ask_raises_on_a_non_success_status(monkeypatch):
    monkeypatch.setattr(agy, "agy_binary", lambda: "agy")
    monkeypatch.setattr(agy.subprocess, "run",
                        _fake_run(dict(SAMPLE_CHEAP, status="ERROR")))
    with pytest.raises(agy.AgyError, match="ERROR"):
        agy.ask("anything", log=False)


def test_ask_surfaces_a_nonzero_exit(monkeypatch):
    monkeypatch.setattr(agy, "agy_binary", lambda: "agy")
    monkeypatch.setattr(agy.subprocess, "run",
                        _fake_run(SAMPLE_CHEAP, returncode=2, stderr="quota exhausted"))
    with pytest.raises(agy.AgyError, match="quota exhausted"):
        agy.ask("anything", log=False)


def test_ask_does_not_move_the_counting_version():
    """This lane must not restamp the counter the fleet is about to re-export onto."""
    fd = _load("fleet_dailies", "fleet_dailies.py")
    assert "agy_usage" not in fd.COUNTING_SURFACE
    source = (ROOT / "token_tracker.py").read_text(encoding="utf-8")
    assert fd._ast_digest(source) == "3e1ec4bcf451"
