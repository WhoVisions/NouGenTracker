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
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DAILIES = REPO / "dailies"

# Reviewed and deliberately public: aggregate counts, model names, and the
# labels needed to tell machines apart when summing.
ALLOWED_TOP_LEVEL = {
    "counter",       # counting-version fingerprint; refuses cross-version sums
    "date",
    "estimated",     # token counts (ints)
    "exact",         # token counts (ints)
    "generated_at",
    "generated_by",  # agent lane, e.g. "claude-cli"
    "invocations",   # int
    "machine",       # fleet label, e.g. "whoart"
    "models",        # model name -> token counts
    "partial",
    "provider_stats", # privacy-safe provider aggregates (no session ids)
    "schema",
    "sketch",
    "sources",
    "totals",
}

# Shapes that must never appear anywhere in a published record, at any depth.
FORBIDDEN = [
    (re.compile(r"[A-Za-z]:\\\\|[A-Za-z]:/"), "a filesystem path from someone's machine"),
    (re.compile(r"/Users/|/home/|/root/"), "a home directory path"),
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"), "a session or conversation id"),
    (re.compile(r"sk-[A-Za-z0-9]|Bearer\s+[A-Za-z0-9]|api[_-]?key\s*[:=]", re.I), "a credential"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "an email address"),
    (re.compile(r"\.jsonl|transcript", re.I), "a transcript filename"),
]


def published():
    return sorted(DAILIES.glob("*/*.json")) if DAILIES.is_dir() else []


def test_there_is_something_to_check():
    """A silent zero-file pass would make every test below vacuous."""
    assert published(), "no published dailies found — this suite would prove nothing"


@pytest.mark.parametrize("path", published(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_only_reviewed_fields_are_published(path):
    record = json.loads(path.read_text(encoding="utf-8"))
    unexpected = set(record) - ALLOWED_TOP_LEVEL
    assert not unexpected, (
        f"{path.relative_to(REPO)} publishes unreviewed field(s) {sorted(unexpected)} "
        f"to a PUBLIC repo. If they are safe, add them to ALLOWED_TOP_LEVEL in this "
        f"file in the same commit — deliberately, not by letting the test pass."
    )


@pytest.mark.parametrize("path", published(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_private_shapes_anywhere_in_the_record(path):
    blob = path.read_text(encoding="utf-8")
    for pattern, what in FORBIDDEN:
        assert not pattern.search(blob), (
            f"{path.relative_to(REPO)} appears to contain {what}. This repo is "
            f"public; that must not ship."
        )


@pytest.mark.parametrize("path", published(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_counts_are_numbers_not_prose(path):
    """Token fields carrying strings would be a sign the exporter changed shape
    under us — and prose is where free text hides."""
    record = json.loads(path.read_text(encoding="utf-8"))
    for section in ("exact", "estimated"):
        for key, value in (record.get(section) or {}).items():
            assert isinstance(value, (int, float)), (
                f"{path.name}: {section}.{key} is {type(value).__name__}, expected a number"
            )
