"""A corrupt pricing cache must be moved aside once, not re-parsed forever.

Observed 2026-10-13: a truncated ~/.nougen/pricing_cache.json made every
token_tracker run log "Failed to load pricing cache" repeatedly and stall the
daily export for minutes. The loader now quarantines a corrupt file on first
sight so the next fetch regenerates a clean cache.
"""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pl = _load("pricing_live", "pricing_live.py")


def test_corrupt_cache_is_quarantined_and_not_reparsed(tmp_path):
    cache = tmp_path / "pricing_cache.json"
    cache.write_text('{"fetched_at": "2026-10-13T00:00:00+00:00", "models": {"a": [1,', encoding="utf-8")

    models, valid = pl.load_cache(cache)

    assert (models, valid) == ({}, False)
    assert not cache.exists(), "corrupt cache must be moved out of the way"
    quarantined = list(tmp_path.glob("pricing_cache.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8").startswith('{"fetched_at"')

    # Second load: nothing left to parse, so it is a clean miss with no re-quarantine.
    assert pl.load_cache(cache) == ({}, False)
    assert len(list(tmp_path.glob("pricing_cache.json.corrupt-*"))) == 1


def test_missing_cache_is_a_plain_miss(tmp_path):
    assert pl.load_cache(tmp_path / "absent.json") == ({}, False)
