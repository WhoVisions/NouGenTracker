"""The passive status lane stays bounded and side-effect free."""

import json
from datetime import date
from pathlib import Path

import pytest

import tracker_live


def _daily(root, machine, day, *, partial=False):
    path = root / "dailies" / machine / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "date": day,
        "machine": machine,
        "generated_at": f"{day}T23:59:00Z",
        "partial": partial,
    }), encoding="utf-8")
    return path


def test_reports_fresh_stale_and_missing_without_calling_usage_tracker(tmp_path):
    _daily(tmp_path, "freshbox", "2026-09-03")
    _daily(tmp_path, "stalebox", "2026-08-20")

    result = tracker_live.inspect_tracker(
        tmp_path, ["freshbox", "stalebox", "missingbox"],
        stale_after_days=2, today=date(2026, 9, 4))

    states = {item["machine"]: item["state"] for item in result["machines"]}
    assert states == {
        "freshbox": "fresh", "missingbox": "missing", "stalebox": "stale"}
    assert result["complete"] is False
    assert "missingbox" in result["incomplete_machines"]
    assert not any(result["side_effects"].values())
    assert result["scope"] == "publication_freshness_not_usage"


def test_partial_latest_daily_is_not_reported_as_healthy(tmp_path):
    _daily(tmp_path, "box", "2026-09-04", partial=True)
    result = tracker_live.inspect_tracker(
        tmp_path, ["box"], today=date(2026, 9, 4))
    assert result["machines"][0]["state"] == "partial"
    assert result["complete"] is False


def test_status_reads_only_the_latest_record_per_machine(tmp_path, monkeypatch):
    older = _daily(tmp_path, "box", "2026-09-02")
    latest = _daily(tmp_path, "box", "2026-09-03")
    opened = []
    original = Path.open

    def guarded_open(path, mode="r", *args, **kwargs):
        assert not any(flag in mode for flag in "wax+")
        opened.append(path)
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    result = tracker_live.inspect_tracker(
        tmp_path, ["box"], today=date(2026, 9, 4))

    assert result["machines"][0]["latest_day"] == "2026-09-03"
    assert opened == [latest]
    assert older not in opened


def test_status_does_not_create_or_modify_files(tmp_path, monkeypatch):
    _daily(tmp_path, "box", "2026-09-03")
    before = sorted((path.relative_to(tmp_path), path.read_bytes())
                    for path in tmp_path.rglob("*") if path.is_file())

    def forbidden(*args, **kwargs):
        raise AssertionError("passive status attempted a filesystem mutation")

    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(Path, "unlink", forbidden)
    result = tracker_live.inspect_tracker(
        tmp_path, ["box"], today=date(2026, 9, 4))

    after = sorted((path.relative_to(tmp_path), path.read_bytes())
                   for path in tmp_path.rglob("*") if path.is_file())
    assert before == after
    assert result["machines"][0]["state"] == "fresh"


def test_missing_is_unknown_not_zero(tmp_path):
    result = tracker_live.inspect_tracker(
        tmp_path, ["box"], today=date(2026, 9, 4))
    assert result["machines"][0]["state"] == "missing"
    assert "zero" in result["caveat"]
    assert "usage" not in result["machines"][0]


def test_machine_name_cannot_escape_dailies_root(tmp_path):
    with pytest.raises(ValueError, match="invalid machine"):
        tracker_live.inspect_tracker(tmp_path, ["../../private"])
