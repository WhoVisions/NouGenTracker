"""The unintroduced-machine guard must be able to fire.

Measured on phoebus 2026-09-04: `token_tracker.py --export` ran without
NOUGEN_MACHINE set, fell back to the hostname, and silently created a fourth
machine series (`dailies/kushboygroups-mac-mini-local/`) beside the three real
ones. The warning written for exactly this — "'X' has never published dailies
here … Otherwise this becomes a new machine" — did **not** appear.

The function was correct. The call site sampled `known_machines()` *after*
`export_days()` had already created `dailies/<machine>/`, so the new machine
was always already "known" and the guard could never fire. A safeguard that is
structurally incapable of triggering is worse than none: it reads as coverage.

This matters beyond one stray directory. `fleet --fleet` sums across machine
directories, so a forked identity double-counts one box — wrong in the
direction that looks plausible, which is the worst direction to be wrong in.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fleet_dailies as fd  # noqa: E402


def test_a_brand_new_machine_is_flagged(tmp_path):
    (tmp_path / "phoebus").mkdir()
    (tmp_path / "blade1tb").mkdir()
    warn = fd.unintroduced_machine_warning("newbox", dailies_dir=tmp_path)
    assert warn and "newbox" in warn and "NOUGEN_MACHINE" in warn


def test_a_known_machine_is_not_flagged(tmp_path):
    (tmp_path / "phoebus").mkdir()
    assert fd.unintroduced_machine_warning("phoebus", dailies_dir=tmp_path) is None


def test_the_guard_still_fires_after_its_own_directory_exists(tmp_path):
    """The regression. The export creates dailies/<machine>/ before the guard
    is consulted, so the guard must judge against the set sampled BEFORE that
    — otherwise it silently approves the fork it exists to announce."""
    (tmp_path / "phoebus").mkdir()
    known_before = fd.known_machines(tmp_path)

    (tmp_path / "newbox").mkdir()          # what export_days() does

    # Without the pre-sampled set, the guard sees its own artifact and passes:
    assert fd.unintroduced_machine_warning("newbox", dailies_dir=tmp_path) is None
    # With it, the fork is still announced:
    warn = fd.unintroduced_machine_warning("newbox", dailies_dir=tmp_path,
                                           known=known_before)
    assert warn and "newbox" in warn


def test_first_ever_export_is_not_flagged(tmp_path):
    """An empty fleet has no name to be inconsistent with."""
    assert fd.unintroduced_machine_warning("firstbox", dailies_dir=tmp_path) is None
