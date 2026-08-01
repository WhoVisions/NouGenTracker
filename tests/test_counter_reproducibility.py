"""A counting version's only job is to be looked up.

`+dirty` established the principle: a stamp naming code that was never committed
cannot answer "go read what produced this number". The ranking had not caught up
with it. Cohorts were ordered by headcount, so on 2026-08-01 seventeen files
stamped `22555db5d239` — a digest no commit in this repository reproduces —
outvoted fifteen stamped `71aef8ff08fa`, which five commits do. `current` went
to the cohort nobody could verify, and the verifiable one was reported stale.

That is the mechanism pointed backwards: it exists to stop unverifiable numbers
from being trusted, and it was electing them.
"""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fd = _load("fleet_dailies", "fleet_dailies.py")


def rec(machine, day, counter=None, stamp="2026-08-01T00:00:00-04:00"):
    body = {
        "schema": fd.SCHEMA_VERSION,
        "machine": machine,
        "date": day,
        "generated_at": stamp,
        "totals": {f: 100 for f in fd.TOKEN_FIELDS},
    }
    if counter:
        body["counter"] = counter
    return ((machine, day), body)


def test_a_counter_no_commit_reproduces_cannot_be_current():
    """The real corpus, with the real digests."""
    winners = ([rec("phoebus", f"2026-05-{d:02d}", "22555db5d239") for d in range(1, 18)]
               + [rec("whoart", f"2026-07-{d:02d}", "71aef8ff08fa") for d in range(1, 16)])
    info = fd.counter_cohorts(winners, local="71aef8ff08fa",
                              reproducible={"71aef8ff08fa"})
    assert info["current"] == "71aef8ff08fa"
    assert info["unverifiable"] == ["22555db5d239"]
    assert len(info["stale"]) == 17          # phoebus re-exports, not whoart


def test_a_dirty_counter_is_never_current():
    """It says outright that the code it names was never committed."""
    winners = [rec("a", "2026-08-01", "abc123" + fd.DIRTY_SUFFIX),
               rec("a", "2026-08-02", "abc123" + fd.DIRTY_SUFFIX),
               rec("b", "2026-08-01", "def456")]
    info = fd.counter_cohorts(winners, local="def456", reproducible={"def456"})
    assert info["current"] == "def456"
    assert len(info["stale"]) == 2


def test_headcount_still_decides_between_two_verifiable_cohorts():
    """Reproducibility outranks size. It does not replace it."""
    winners = [rec("a", "2026-08-01", "aaa"), rec("a", "2026-08-02", "aaa"),
               rec("b", "2026-08-01", "bbb")]
    info = fd.counter_cohorts(winners, local="aaa", reproducible={"aaa", "bbb"})
    assert info["current"] == "aaa"


def test_when_nothing_is_verifiable_the_corpus_is_still_ordered():
    """A clone without git, or one whose stamps all predate the commits it has,
    must still produce an answer instead of throwing."""
    winners = [rec("a", "2026-08-01", "aaa"), rec("a", "2026-08-02", "aaa"),
               rec("b", "2026-08-01", "bbb")]
    info = fd.counter_cohorts(winners, local="zzz", reproducible=set())
    assert info["current"] == "aaa"                      # falls back to headcount
    assert info["unverifiable"] == ["aaa", "bbb"]


def test_unverifiable_and_stale_are_different_diagnoses():
    """"Nobody has re-exported yet" and "this names code that was never
    committed" need different instructions, so the report must tell them apart."""
    winners = [rec("a", "2026-08-01", "current-one"),
               rec("b", "2026-08-01", "older-but-real"),
               rec("c", "2026-08-01", "never-committed")]
    info = fd.counter_cohorts(winners, local="current-one",
                              reproducible={"current-one", "older-but-real"})
    assert info["reproducible"] == ["current-one", "older-but-real"]
    assert info["unverifiable"] == ["never-committed"]


# --- the commit-side half --------------------------------------------------

def test_committed_counter_matches_a_clean_working_tree():
    """What makes 'reproducible' checkable at all: in a clean checkout the
    committed fingerprint and the working-tree one are the same value."""
    committed = fd.committed_counter()
    if committed == fd.UNSTAMPED:
        return  # not a git checkout
    assert committed == fd.counter_fingerprint().replace(fd.DIRTY_SUFFIX, "")


def test_committed_counter_is_never_marked_dirty():
    """It reads from git by definition, so it can never be describing an
    uncommitted tree — and a `+dirty` value here would be nonsense that then
    disqualified the one cohort that IS verifiable."""
    assert not fd.committed_counter().endswith(fd.DIRTY_SUFFIX)


def test_committed_counter_on_a_bad_ref_is_unstamped_not_an_exception():
    assert fd.committed_counter("no-such-ref-anywhere") == fd.UNSTAMPED


def test_the_probe_file_does_not_survive():
    """committed_counter writes a temp file inside the repo to fingerprint it.
    One left behind would show up as an untracked file in everyone's status —
    and, being a copy of the tracker, could be committed by an absent-minded
    `git add -A`."""
    before = set(fd.REPO_ROOT.glob(".counter-probe-*"))
    fd.committed_counter()
    assert set(fd.REPO_ROOT.glob(".counter-probe-*")) == before
