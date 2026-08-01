"""A daily file's `schema` says it can be READ. Nothing said it could be COMPARED.

On 2026-08-01 parse_claude() stopped billing one API request once per content
block. The file format did not change at all, so every daily already published
stayed schema-valid, kept summing into the fleet total, and nothing anywhere
could tell that those numbers no longer meant the same thing as new ones.

The counter fingerprint closes that. These tests hold the two properties that
make it worth having — it must not move when nothing meaningful changed, and it
must move when something did — plus the cohort rules, whose whole job is to stop
a plausible-looking total from being printed over incomparable numbers.
"""
import importlib.util
import json
import pathlib
import textwrap

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fd = _load("fleet_dailies", "fleet_dailies.py")


def tracker_stub(tmp_path, body, name="parse_claude"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    src = tmp_path / "stub.py"
    src.write_text(textwrap.dedent(f"def {name}():\n{textwrap.indent(body, '    ')}"),
                   encoding="utf-8")
    return src


# --- the fingerprint must be boring about cosmetics ------------------------

def test_the_real_tracker_fingerprints(tmp_path):
    """Not a tautology: it fails if COUNTING_SURFACE drifts out of sync with the
    function names in token_tracker.py, which would silently hash `<missing>`
    for everything and produce one constant forever."""
    assert fd.counter_fingerprint() != fd.UNSTAMPED
    assert "<missing>" not in fd.counter_fingerprint()


def test_comments_and_formatting_do_not_move_it(tmp_path):
    a = tracker_stub(tmp_path / "a", "x = 1\nreturn x\n")
    b = tracker_stub(tmp_path / "b", "# a thorough explanation\nx  =  1\n\nreturn x\n")
    assert fd.counter_fingerprint(a) == fd.counter_fingerprint(b)


def test_docstrings_do_not_move_it(tmp_path):
    a = tracker_stub(tmp_path / "a", "return 1\n")
    b = tracker_stub(tmp_path / "b", '"""Now with reasoning written down."""\nreturn 1\n')
    assert fd.counter_fingerprint(a) == fd.counter_fingerprint(b)


def test_a_changed_dedup_key_moves_it(tmp_path):
    """The actual fix that started this: uuid -> requestId."""
    a = tracker_stub(tmp_path / "a", 'key = rec.get("uuid")\nreturn key\n')
    b = tracker_stub(tmp_path / "b", 'key = rec.get("requestId")\nreturn key\n')
    assert fd.counter_fingerprint(a) != fd.counter_fingerprint(b)


def test_a_deleted_parser_moves_it(tmp_path):
    a = tracker_stub(tmp_path / "a", "return 1\n", name="parse_claude")
    b = tracker_stub(tmp_path / "b", "return 1\n", name="something_else")
    assert fd.counter_fingerprint(a) != fd.counter_fingerprint(b)


def test_unreadable_source_reports_unstamped_not_a_fake_version(tmp_path):
    """Claiming a version you cannot verify is a lie in the safe-looking
    direction — the file would then sum with genuinely-current ones."""
    assert fd.counter_fingerprint(tmp_path / "does-not-exist.py") == fd.UNSTAMPED
    broken = tmp_path / "broken.py"
    broken.write_text("def (:\n", encoding="utf-8")
    assert fd.counter_fingerprint(broken) == fd.UNSTAMPED


# --- cohorts ---------------------------------------------------------------

def rec(machine, day, counter=None, stamp="2026-08-01T00:00:00-04:00", tokens=100):
    body = {
        "schema": fd.SCHEMA_VERSION,
        "machine": machine,
        "date": day,
        "generated_at": stamp,
        "totals": {f: tokens for f in fd.TOKEN_FIELDS},
        "exact": {f: tokens for f in fd.TOKEN_FIELDS},
        "estimated": {f: 0 for f in fd.TOKEN_FIELDS},
        "sources": {"Claude Code": {f: tokens for f in fd.TOKEN_FIELDS}},
    }
    if counter:
        body["counter"] = counter
    return ((machine, day), body)


def test_one_cohort_is_summable_whatever_version_it_is():
    """A corpus that agrees with itself is internally consistent even if this
    box runs something older. The aggregator is a reader, not an authority."""
    info = fd.counter_cohorts([rec("a", "2026-08-01", "aaa"),
                               rec("b", "2026-08-01", "aaa")], local="zzz")
    assert info["mixed"] is False
    assert info["stale"] == []
    assert info["local_is_current"] is False   # said, not enforced


def test_two_cohorts_are_not_summable():
    info = fd.counter_cohorts([rec("a", "2026-08-01", "aaa"),
                               rec("a", "2026-08-02", "aaa"),
                               rec("b", "2026-08-01", "bbb")], local="aaa")
    assert info["mixed"] is True
    assert info["current"] == "aaa"           # majority
    assert info["stale"] == [("b", "2026-08-01", "bbb")]


def test_an_entirely_unstamped_corpus_is_still_flagged():
    """The case this repo is actually in, and the one a plain majority rule
    misses: every file agrees, and every file is wrong. Unstamped is the absence
    of a version, not a version."""
    info = fd.counter_cohorts([rec("phoebus", "2026-07-30"),
                               rec("phoebus", "2026-07-31")], local="aaa")
    assert info["mixed"] is True
    assert info["current"] == "aaa"
    assert len(info["stale"]) == 2


def test_unstamped_never_wins_even_as_the_majority():
    info = fd.counter_cohorts([rec("a", "2026-08-01"), rec("a", "2026-08-02"),
                               rec("a", "2026-08-03"), rec("b", "2026-08-01", "new")],
                              local="new")
    assert info["current"] == "new"
    assert len(info["stale"]) == 3


def test_stale_range_names_what_one_machine_must_re_export():
    stale = [("phoebus", "2026-05-11", "x"), ("phoebus", "2026-07-31", "x"),
             ("blade1tb", "2026-06-01", "x")]
    assert fd.stale_range(stale, "phoebus") == ("2026-05-11", "2026-07-31")
    assert fd.stale_range(stale, "whoart") is None


def test_no_records_is_not_a_problem():
    info = fd.counter_cohorts([], local="aaa")
    assert info["mixed"] is False and info["stale"] == []


# --- end to end through aggregate -----------------------------------------

def test_aggregate_splits_totals_by_cohort(tmp_path):
    dailies = tmp_path / "dailies"
    for machine, day, counter in (("a", "2026-08-01", "aaa"),
                                  ("b", "2026-08-01", None)):
        d = dailies / machine
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{day}.json").write_text(json.dumps(rec(machine, day, counter)[1]),
                                       encoding="utf-8")
    agg = fd.aggregate(fd.load_fleet(dailies))
    assert agg["counters"]["mixed"] is True
    assert set(agg["by_counter"]) == {"aaa", fd.UNSTAMPED}
    # The blended total is still computed — it is what the split is computed
    # FROM — but the caller now has what it needs to refuse to print it.
    assert sum(agg["by_counter"]["aaa"].values()) > 0


def test_a_stamped_export_round_trips_its_counter(tmp_path):
    inv = [{"timestamp": "2026-08-01T12:00:00", "source": "Claude Code",
            "model": "claude-opus-4-8", "input_tokens": 5, "output_tokens": 5,
            "cache_read": 0, "cache_creation": 0, "reasoning": 0, "exact": True}]
    paths = fd.export_days(inv, machine="whoart", dailies_dir=tmp_path)
    written = json.loads(paths[0].read_text(encoding="utf-8"))
    assert written["counter"] == fd.counter_fingerprint()
    assert fd.counter_of(written) != fd.UNSTAMPED
