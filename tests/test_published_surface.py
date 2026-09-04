"""What this PUBLIC repo is allowed to publish about a private machine.

`dailies/` is usage telemetry from someone's personal computers, committed to a
repository anyone on the internet can read. The privacy property was true when
the exporter was written, and a docstring said so — but nothing stopped the next
field being added, and nothing would have failed if it had been.

So this is an allowlist, not a scan. A new key in an exported record fails here
until a human decides it is safe to make public. That is the correct direction
of default for a public surface: additions are reviewed, not assumed.

If a legitimate new field is added, add it here in the same commit. The failure
message is the review prompt.
"""

import json
from pathlib import Path

import pytest

from public_surface import ALLOWED_TOP_LEVEL, FORBIDDEN, published, validate

REPO = Path(__file__).resolve().parents[1]
PUBLISHED = published(REPO)


def test_there_is_something_to_check():
    """A silent zero-file pass would make every test below vacuous."""
    assert PUBLISHED, "no published dailies found — this suite would prove nothing"
    assert validate(REPO) == []


@pytest.mark.parametrize("path", PUBLISHED, ids=lambda p: str(p.relative_to(REPO)))
def test_only_reviewed_fields_are_published(path):
    record = json.loads(path.read_text(encoding="utf-8"))
    unexpected = set(record) - ALLOWED_TOP_LEVEL
    assert not unexpected, (
        f"{path.relative_to(REPO)} publishes unreviewed field(s) {sorted(unexpected)} "
        f"to a PUBLIC repo. If they are safe, add them to ALLOWED_TOP_LEVEL in this "
        f"file in the same commit — deliberately, not by letting the test pass."
    )


@pytest.mark.parametrize("path", PUBLISHED, ids=lambda p: str(p.relative_to(REPO)))
def test_no_private_shapes_anywhere_in_the_record(path):
    blob = path.read_text(encoding="utf-8")
    for pattern, what in FORBIDDEN:
        assert not pattern.search(blob), (
            f"{path.relative_to(REPO)} appears to contain {what}. This repo is "
            f"public; that must not ship."
        )


@pytest.mark.parametrize("path", PUBLISHED, ids=lambda p: str(p.relative_to(REPO)))
def test_counts_are_numbers_not_prose(path):
    """Token fields carrying strings would be a sign the exporter changed shape
    under us — and prose is where free text hides."""
    record = json.loads(path.read_text(encoding="utf-8"))
    for section in ("exact", "estimated"):
        for key, value in (record.get(section) or {}).items():
            assert isinstance(value, (int, float)), (
                f"{path.name}: {section}.{key} is {type(value).__name__}, expected a number"
            )
