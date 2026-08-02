"""The counting fingerprint must mean the same thing on every interpreter.

Its entire job is cross-machine comparability. A digest that moves with the
Python version fails at exactly that, and fails invisibly: each box reproduces
its own value and no other, so every box concludes the others are fabricating.

This fleet spent a day there. whoart and blade1tb run 3.11 and stamped
71aef8ff08fa; phoebus runs 3.13 and stamped 22555db5d239 — same committed
token_tracker.py, both trees clean. Two machines running byte-identical
counting code refused to sum.
"""
import ast
import importlib.util
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("fleet_dailies", ROOT / "fleet_dailies.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fd = _load()

OTHER_PYTHONS = [p for p in ("python3.10", "python3.11", "python3.12", "python3.13")
                 if shutil.which(p) and not sys.executable.endswith(p)]


def test_the_digest_uses_neither_dump_nor_unparse():
    """Both stdlib serialisers are interpreter-sensitive, in different ways.

    ast.dump writes whatever fields the running interpreter defines, so a field
    added in a point release moves the digest for unchanged source. ast.unparse
    dodges that but its formatting is not frozen either — 3.10 renders
    differently from 3.11, which CI caught after 3.11 and 3.13 had agreed
    locally and looked like proof. The traversal has to be ours.
    """
    source = (ROOT / "fleet_dailies.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_ast_digest":
            attrs = {n.func.attr for n in ast.walk(node)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
            assert "dump" not in attrs, "ast.dump moves when CPython adds a field"
            assert "unparse" not in attrs, "ast.unparse formatting differs across versions"
            names = {n.func.id for n in ast.walk(node)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            assert "_canonical" in names, "the digest must use the explicit traversal"
            return
    raise AssertionError("_ast_digest not found")


def test_volatile_fields_are_excluded_by_name():
    """type_params is the one that actually bit; keep the list honest."""
    assert "type_params" in fd._VOLATILE_AST_FIELDS


@pytest.mark.skipif(not OTHER_PYTHONS, reason="only one interpreter available")
@pytest.mark.parametrize("interpreter", OTHER_PYTHONS)
def test_another_interpreter_agrees_on_the_same_source(interpreter, tmp_path):
    """The real check: run the actual module under a different Python and
    demand the same digest for the same file."""
    target = ROOT / "token_tracker.py"
    script = (
        "import importlib.util,pathlib,sys\n"
        f"spec=importlib.util.spec_from_file_location('fd',r'{ROOT / 'fleet_dailies.py'}')\n"
        "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        f"print(m.counter_fingerprint(pathlib.Path(r'{target}')))\n"
    )
    try:
        proc = subprocess.run([interpreter, "-c", script], capture_output=True, text=True)
    except OSError as exc:
        # Windows resolves python3.11 to a Store app-execution alias: a stub
        # shutil.which finds and CreateProcess then refuses. Found on whoart,
        # where this test failed rather than skipping.
        pytest.skip(f"{interpreter} is on PATH but cannot be executed: {exc}")
    if proc.returncode != 0:
        pytest.skip(f"{interpreter} could not import the module: {proc.stderr.strip()[:120]}")

    theirs = proc.stdout.strip()
    mine = fd.counter_fingerprint(target)
    assert theirs == mine, (
        f"{interpreter} produced {theirs}, this interpreter produced {mine} — "
        "the same source must fingerprint identically everywhere"
    )


def test_docstring_edits_still_do_not_move_the_digest(tmp_path):
    """The property that made the AST approach right must survive the change."""
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text('def usage_of(rec):\n    """One."""\n    return rec.get("usage")\n')
    b.write_text('def usage_of(rec):\n    """Completely different prose."""\n    return rec.get("usage")\n')
    assert fd.counter_fingerprint(a) == fd.counter_fingerprint(b)


def test_a_behaviour_change_still_moves_the_digest(tmp_path):
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_text('def usage_of(rec):\n    return rec.get("usage")\n')
    b.write_text('def usage_of(rec):\n    return rec.get("usage") or {}\n')
    assert fd.counter_fingerprint(a) != fd.counter_fingerprint(b)
