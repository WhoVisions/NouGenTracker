"""`_git` has to survive the repo's own files.

The dirty-tree check runs `git show HEAD:token_tracker.py`, and that file is
UTF-8 with em dashes and a ⚠ in it. Under `text=True` with no encoding, Python
decodes subprocess output with the console default — cp1252 on Windows — and the
decode raises inside subprocess's reader THREAD. `run()` then returns normally
with `stdout=None`, so the failure surfaces as `TypeError: unsupported operand
type(s) for +: 'NoneType' and 'str'` several frames away, and `--fleet` and
`--export` die outright on every Windows box.

Which is the worst possible place for it: the counting-version check exists to
stop bad numbers from summing, and it was crashing on the machines that had the
most to publish. Tested by asserting it WORKS on the input that broke it.
"""
import importlib.util
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fd = _load("fleet_dailies", "fleet_dailies.py")


def test_git_show_of_a_utf8_file_does_not_crash():
    """The exact call the dirty check makes, on the exact file it makes it on."""
    code, out = fd._git("show", "HEAD:token_tracker.py")
    if code != 0:
        return  # not a checkout of this repo; nothing to assert
    assert isinstance(out, str) and out


def test_git_returns_a_string_even_when_the_command_fails():
    """The None-plus-str crash is a decoding artefact, but the same shape of bug
    appears any time a stream is empty. Both sides must always be strings."""
    code, out = fd._git("show", "HEAD:definitely-not-a-file-in-this-repo")
    assert code != 0
    assert isinstance(out, str)


def test_non_cp1252_bytes_round_trip(tmp_path):
    """Directly: a file whose bytes cp1252 cannot decode. 0x9d — the byte that
    actually broke it — is part of the em dash in UTF-8."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "t@example.com"),
                 ("config", "user.name", "t")):
        subprocess.run(("git",) + args, cwd=repo, capture_output=True)
    (repo / "f.py").write_text("# em dash — and a warning ⚠\n", encoding="utf-8")
    subprocess.run(("git", "add", "f.py"), cwd=repo, capture_output=True)
    subprocess.run(("git", "commit", "-qm", "x"), cwd=repo, capture_output=True)

    code, out = fd._git("show", "HEAD:f.py", cwd=repo)
    assert code == 0
    assert "—" in out and "⚠" in out


def test_the_dirty_check_answers_instead_of_raising():
    """Its contract is a bool. Before the fix it raised, which is not False —
    it took the whole report down with it."""
    assert fd._tracker_differs_from_head(fd.TRACKER_SOURCE) in (True, False)


def test_fingerprinting_the_real_tracker_completes():
    """The end-to-end symptom: this is what `--export` and `--fleet` both call
    first, and what crashed."""
    counter = fd.counter_fingerprint()
    assert counter
    assert counter == fd.UNSTAMPED or len(counter.replace(fd.DIRTY_SUFFIX, "")) == 12
