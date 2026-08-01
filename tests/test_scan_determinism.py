"""Scan order must not decide which day a request is billed to.

parse_claude and parse_antigravity both dedupe across every file they read,
so the FIRST copy of a duplicated id is the one counted — and its timestamp
picks the day. If scan order is the filesystem's, two machines reading
identical logs can attribute the same request to different days and disagree
on a fleet total with nothing to point at.
"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "token_tracker.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)

DEDUPING_PARSERS = ("parse_claude", "parse_antigravity")


def _fn(name):
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in token_tracker.py")


def _calls(node, func_name):
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == func_name]


def test_every_deduping_parser_dedupes_and_so_needs_stable_order():
    """Guards the premise: if a parser stops deduping this test should be
    revisited rather than silently protecting nothing."""
    for name in DEDUPING_PARSERS:
        body = ast.dump(_fn(name))
        assert "seen" in body, f"{name} no longer dedupes — revisit these tests"


def test_parse_claude_sorts_its_file_list():
    fn = _fn("parse_claude")
    globs = _calls(fn, "sorted")
    assert globs, "parse_claude must sort its glob result before scanning"


def test_parse_claude_glob_is_wrapped_not_merely_accompanied():
    """A bare sorted() somewhere in the function is not the same as sorting the
    list that is actually iterated."""
    fn = _fn("parse_claude")
    for node in ast.walk(fn):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "files" for t in node.targets)):
            assert isinstance(node.value, ast.Call), "files = <call> expected"
            assert getattr(node.value.func, "id", None) == "sorted", (
                "the glob assigned to `files` must be wrapped in sorted()"
            )
            return
    raise AssertionError("no `files = ...` assignment found in parse_claude")


def test_parse_antigravity_walks_in_a_stable_order():
    """os.walk yields directory order; both dirs and filenames need sorting."""
    fn = _fn("parse_antigravity")
    dumped = ast.dump(fn)
    assert "sort" in dumped, "parse_antigravity must stabilise its os.walk order"
    assert _calls(fn, "sorted"), "filenames must be sorted, not just dirs"


def test_the_convention_is_shared_not_special_cased():
    """parse_gemini_cli already sorted; this documents that the rule is general."""
    assert _calls(_fn("parse_gemini_cli"), "sorted"), (
        "parse_gemini_cli was the precedent for this convention"
    )
