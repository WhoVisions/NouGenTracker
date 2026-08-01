"""The unit of billing is the request, not the transcript row.

Claude Code writes one row per content block — the assistant message, then one
per tool_use — and every row repeats the SAME usage object under a fresh uuid.
A tracker that dedupes by uuid dedupes nothing and bills a request once per
block it produced. Measured on a real box 2026-08-01: 652 of 1,136 requests
spanned multiple rows, usage identical in all of them, inflating cache-reads
from 287M to 554M.

That is the failure this file exists to keep fixed: it is silent, it looks
like heavy usage rather than a bug, and it scales with how many tools a session
calls — so the busier the work, the more wrong the report.
"""
import datetime as dt
import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tt = _load("token_tracker", "token_tracker.py")

USAGE = {
    "input_tokens": 10,
    "output_tokens": 100,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 50_000,
}


def row(uuid, request_id, usage=None, ts="2026-08-01T12:00:00.000Z"):
    rec = {
        "uuid": uuid,
        "timestamp": ts,
        "message": {"model": "claude-opus-4-8", "usage": dict(usage or USAGE)},
    }
    if request_id is not None:
        rec["requestId"] = request_id
    return json.dumps(rec)


def write_transcript(tmp_path, rows):
    project = tmp_path / "projects" / "some-project"
    project.mkdir(parents=True)
    (project / "session.jsonl").write_text("\n".join(rows), encoding="utf-8")
    return tmp_path / "projects"


def run_parse(monkeypatch, projects):
    monkeypatch.setattr(tt, "PROJECTS", str(projects))
    monkeypatch.setattr(tt, "CUTOFF",
                        dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
    monkeypatch.setattr(tt, "LIMIT_UPPER",
                        dt.datetime(2027, 1, 1, tzinfo=dt.timezone.utc))
    monkeypatch.setattr(tt, "ALL_INVOCATIONS", [])
    return tt.parse_claude()


def test_one_request_split_across_rows_is_counted_once(monkeypatch, tmp_path):
    """Three rows, three uuids, one requestId — one API call, one charge."""
    projects = write_transcript(tmp_path, [
        row("uuid-a", "req-1"),
        row("uuid-b", "req-1"),
        row("uuid-c", "req-1"),
    ])
    _, _, totals, _, records = run_parse(monkeypatch, projects)
    assert records == 1
    assert totals["cache_read_input_tokens"] == 50_000


def test_distinct_requests_are_all_counted(monkeypatch, tmp_path):
    """The dedup must not swing the other way and swallow real traffic."""
    projects = write_transcript(tmp_path, [
        row("uuid-a", "req-1"),
        row("uuid-b", "req-2"),
        row("uuid-c", "req-3"),
    ])
    _, _, totals, _, records = run_parse(monkeypatch, projects)
    assert records == 3
    assert totals["cache_read_input_tokens"] == 150_000


def test_a_row_without_a_request_id_still_counts(monkeypatch, tmp_path):
    """Rare, but real — one such row existed in the measured sample. Dropping
    rows with no requestId would trade over-counting for under-counting."""
    projects = write_transcript(tmp_path, [
        row("uuid-a", "req-1"),
        row("uuid-lonely", None),
    ])
    _, _, totals, _, records = run_parse(monkeypatch, projects)
    assert records == 2
    assert totals["cache_read_input_tokens"] == 100_000


def test_the_same_row_replayed_is_not_double_counted(monkeypatch, tmp_path):
    """uuid remains the fallback key, so an exactly duplicated row without a
    requestId is still caught."""
    projects = write_transcript(tmp_path, [
        row("uuid-same", None),
        row("uuid-same", None),
    ])
    _, _, totals, _, records = run_parse(monkeypatch, projects)
    assert records == 1


def test_top_consumers_do_not_repeat_one_request(monkeypatch, tmp_path):
    """The visible symptom: the top-consumers list showed the same timestamp
    and token counts three times in a row, which is what sent us looking."""
    projects = write_transcript(tmp_path, [
        row("uuid-a", "req-1"),
        row("uuid-b", "req-1"),
        row("uuid-c", "req-1"),
    ])
    run_parse(monkeypatch, projects)
    assert len(tt.ALL_INVOCATIONS) == 1
