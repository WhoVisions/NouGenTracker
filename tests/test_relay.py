"""Tests for the cross-machine relay.

Same shape as test_token_tracker.py: load the modules by path so each test can
re-import a fresh copy after monkeypatching the environment, and assert on
values rather than on printed text. Nothing here touches a real log, a real
git remote, or the user's relay directory.
"""
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _isolate(monkeypatch, tmp_path):
    """Cut the tests off from whatever tracker_config.json this box happens to
    have — otherwise a developer's local relay_machine decides the result.

    token_tracker reads its overlay once at import and caches it in CONFIG, and
    relay imports whatever token_tracker is already in sys.modules, so the host
    module has to be dropped and re-read for the new env to take effect.
    """
    monkeypatch.setenv("TOKEN_TRACKER_CONFIG", str(tmp_path / "no-such.json"))
    sys.modules.pop("token_tracker", None)
    _load("token_tracker", "token_tracker.py")


def _fresh(monkeypatch, tmp_path, machine="blade1tb"):
    """A relay module bound to an empty temp relay dir and a known identity."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("RELAY_DIR", str(tmp_path))
    monkeypatch.setenv("RELAY_MACHINE", machine)
    monkeypatch.delenv("RELAY_SESSION", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    return _load("relay", "relay.py")


def _write_rollup(root, machine, day, rows, schema=1,
                  generated="2026-07-31T23:00:00+00:00"):
    path = root / machine
    path.mkdir(parents=True, exist_ok=True)
    target = path / f"usage_{day}.json"
    target.write_text(json.dumps({
        "schema": schema, "machine": machine, "day": day,
        "generated_utc": generated, "rows": rows,
    }), encoding="utf-8")
    return target


ROW = {
    "day": "2026-07-31", "source": "Claude Code", "model": "claude-opus-5",
    "exact": True, "input_tokens": 1000, "output_tokens": 2000,
    "cache_creation": 0, "cache_read": 500000, "reasoning": 0,
    "invocations": 7,
}


# --- identity ---------------------------------------------------------------

def test_machine_id_prefers_env_over_hostname(monkeypatch, tmp_path):
    relay = _fresh(monkeypatch, tmp_path, machine="Phoebus")
    assert relay.machine_id() == "phoebus"
    assert relay.machine_provenance() == "env RELAY_MACHINE"


def test_machine_id_falls_back_to_hostname(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("RELAY_DIR", str(tmp_path))
    monkeypatch.delenv("RELAY_MACHINE", raising=False)
    monkeypatch.delenv("NOUGEN_MACHINE", raising=False)
    relay = _load("relay", "relay.py")
    # The env vars are User-scope on Windows and absent from running shells,
    # so the probe route must produce a usable id on its own.
    assert relay.machine_id()
    assert relay.machine_provenance() == "hostname probe"


# --- peer reading -----------------------------------------------------------

def test_local_rollup_is_never_read_back_as_a_peer(monkeypatch, tmp_path):
    """The double-count guard: filter on the machine FIELD, not the folder."""
    relay = _fresh(monkeypatch, tmp_path)
    # A rollup that claims to be this box, filed under a peer's directory.
    _write_rollup(tmp_path, "phoebus", "2026-07-30",
                  [dict(ROW, day="2026-07-30")])
    decoy = tmp_path / "phoebus" / "usage_2026-07-30.json"
    payload = json.loads(decoy.read_text(encoding="utf-8"))
    payload["machine"] = "blade1tb"
    decoy.write_text(json.dumps(payload), encoding="utf-8")

    assert relay.read_peers() == []
    assert len(relay.read_peers(include_local=True)) == 1


def test_unknown_schema_is_skipped_not_fatal(monkeypatch, tmp_path):
    relay = _fresh(monkeypatch, tmp_path)
    _write_rollup(tmp_path, "phoebus", "2026-07-31", [ROW],
                  schema=relay.SCHEMA_VERSION + 99)
    assert relay.read_peers() == []


def test_unattributed_rollup_is_skipped(monkeypatch, tmp_path):
    relay = _fresh(monkeypatch, tmp_path)
    path = _write_rollup(tmp_path, "phoebus", "2026-07-31", [ROW])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["machine"] = ""
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert relay.read_peers() == []


def test_peer_freshness_marks_stale(monkeypatch, tmp_path):
    monkeypatch.setenv("RELAY_STALE_HOURS", "1")
    relay = _fresh(monkeypatch, tmp_path)
    _write_rollup(tmp_path, "phoebus", "2026-07-31", [ROW],
                  generated="2020-01-01T00:00:00+00:00")
    rows = relay.peer_freshness(relay.read_peers())
    assert len(rows) == 1
    machine, _ts, _age, stale = rows[0]
    assert (machine, stale) == ("phoebus", True)


# --- rollup shape -----------------------------------------------------------

def test_rollup_rows_drop_identifying_fields(monkeypatch, tmp_path):
    """Privacy is structural: the fields simply are not in the payload."""
    relay = _fresh(monkeypatch, tmp_path)
    tt = _load("token_tracker", "token_tracker.py")
    from datetime import datetime, timezone
    inv = tt.Invocation(
        timestamp=datetime(2026, 7, 31, 10, tzinfo=timezone.utc),
        source="Claude Code", model="claude-opus-5", input_tokens=5,
        output_tokens=6, cache_read=7,
        session_id="C--Users-super-Watchtower", source_file="/home/super/x.jsonl")
    rows = relay.rollup_rows([inv])
    assert len(rows) == 1
    assert set(rows[0]) == {
        "day", "source", "model", "exact", "input_tokens", "output_tokens",
        "cache_creation", "cache_read", "reasoning", "invocations"}
    blob = json.dumps(rows)
    assert "super" not in blob and "session" not in blob


def test_rollup_rows_sum_duplicate_keys(monkeypatch, tmp_path):
    relay = _fresh(monkeypatch, tmp_path)
    tt = _load("token_tracker", "token_tracker.py")
    from datetime import datetime, timezone
    same = dict(source="Claude Code", model="claude-opus-5", output_tokens=10)
    invs = [tt.Invocation(timestamp=datetime(2026, 7, 31, h, tzinfo=timezone.utc),
                          **same) for h in (1, 2, 3)]
    rows = relay.rollup_rows(invs)
    assert len(rows) == 1
    assert rows[0]["output_tokens"] == 30
    assert rows[0]["invocations"] == 3


# --- blending into the report ----------------------------------------------

def test_parse_relay_adds_peer_totals_exactly(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    tt = _load("token_tracker", "token_tracker.py")
    _write_rollup(tmp_path, "phoebus", "2026-07-31", [ROW])
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 8, 1, tzinfo=timezone.utc).astimezone()
    window = tt.Window(cutoff=now - timedelta(days=3),
                       limit_upper=now + timedelta(days=1), now=now, days=3)
    scan = tt.parse_relay(window)
    assert scan.machines == ("phoebus",)
    assert scan.usage.totals["output_tokens"] == ROW["output_tokens"]
    assert scan.usage.totals["cache_read_input_tokens"] == ROW["cache_read"]
    assert all(inv.machine == "phoebus" for inv in scan.usage.invocations)


def test_parse_relay_is_empty_without_peers(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    tt = _load("token_tracker", "token_tracker.py")
    scan = tt.parse_relay(tt.Window.last_days(2))
    assert scan.records == 0 and scan.usage.totals == {}


# --- baton ------------------------------------------------------------------

def test_baton_phases_append_to_one_session(monkeypatch, tmp_path):
    relay = _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(relay, "collect_local", lambda days=None: [])
    for phase in ("start", "mid", "end"):
        path = relay.append_leg(phase)
    baton = json.loads(path.read_text(encoding="utf-8"))
    assert [leg["phase"] for leg in baton["legs"]] == ["start", "mid", "end"]
    assert baton["machine"] == "blade1tb"
    assert len({leg["phase"] for leg in baton["legs"]}) == 3


def test_bad_phase_rejected(monkeypatch, tmp_path):
    relay = _fresh(monkeypatch, tmp_path)
    try:
        relay.append_leg("finish")
    except ValueError:
        return
    raise AssertionError("append_leg accepted an unknown phase")


def test_export_refuses_anonymous_machine(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("RELAY_DIR", str(tmp_path))
    monkeypatch.setenv("RELAY_MACHINE", "localhost")
    relay = _load("relay", "relay.py")
    monkeypatch.setattr(relay, "collect_local", lambda days=None: [])
    try:
        relay.export()
    except RuntimeError:
        return
    raise AssertionError("export wrote an unattributable rollup")


# --- hooks ------------------------------------------------------------------

def test_hook_install_is_idempotent(monkeypatch, tmp_path):
    relay = _fresh(monkeypatch, tmp_path)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": "echo existing"}]}]}}),
        encoding="utf-8")
    ok, _ = relay.install_hooks(settings)
    assert ok
    first = json.loads(settings.read_text(encoding="utf-8"))
    ok, message = relay.install_hooks(settings)
    assert ok and "already present" in message
    assert json.loads(settings.read_text(encoding="utf-8")) == first
    # the pre-existing hook survived
    commands = [h["command"] for g in first["hooks"]["SessionStart"]
                for h in g["hooks"]]
    assert "echo existing" in commands
    assert any("relay.py" in c for c in commands)


def test_hook_config_covers_all_three_phases(monkeypatch, tmp_path):
    relay = _fresh(monkeypatch, tmp_path)
    config = relay.hook_config()
    phases = {relay.HOOK_EVENTS[event] for event in config}
    assert phases == set(relay.PHASES)
