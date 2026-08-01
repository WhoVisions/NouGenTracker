# dailies/ — what this publishes, and what it never will

**This repository is public.** These files are usage telemetry from private
machines, so what lands here is a deliberate disclosure rather than a dump.

## What is in a record

Aggregate counts and the labels needed to tell machines apart when summing:

| field | example | why it is safe |
|---|---|---|
| `machine`, `generated_by` | `whoart`, `claude-cli` | fleet labels somebody chose, not hostnames or usernames |
| `exact`, `estimated` | `{"input_tokens": 1690, …}` | integers |
| `models` | `claude-fable-5` | public model names |
| `invocations` | `909` | an integer |
| `counter` | `71aef8ff08fa` | fingerprint of the counting logic (see below) |
| `date`, `generated_at`, `schema` | | timestamps and a version |

## What is never in a record

No prompts. No responses. No file paths. No session or conversation ids. No
transcript filenames. No credentials. No email addresses.

That is not a promise in a document — `tests/test_published_surface.py`
**enforces** it on every file in this directory, and it is an *allowlist*: a new
field fails the suite until a human adds it deliberately. Additions to a public
surface get reviewed rather than assumed.

## What you can still infer

Being straight about it, since these are real numbers: you can tell roughly how
heavily each machine is used, which models it favours, and its timezone offset
from `generated_at`. That is the cost of publishing the rollups at all. It is a
reasonable trade for a fleet that wants one honest total — but it is a trade,
and anyone adding a machine here should know they are making it.

## Why totals sometimes refuse to add up

`counter` fingerprints the counting logic that produced a record. When the
logic changes — a deduplication fix, a new source — the fingerprint moves, and
records from different fingerprints are **not summed**. A total that silently
mixes two counting methods is worse than no total, because it looks like an
answer.

## Adding a machine

```bash
NOUGEN_MACHINE=<name> python token_tracker.py --start <YYYY-MM-DD> --end <YYYY-MM-DD> --export
```

Bound it with `--start` / `--end`. Bare `--export` widens to the machine's whole
available history, which is a fine default for a private dashboard and the wrong
one for a public commit.

## Your history is shorter than you think

Claude Code prunes transcripts at `cleanupPeriodDays`, **default 30**. Anything
older is already gone and no backfill can recover it. If you want a long
history, raise that setting *before* you need it:

```jsonc
// ~/.claude/settings.json
{ "cleanupPeriodDays": 365 }
```

Measured on one machine 2026-08-01: a 45-day export returned 15 days, because
only 14 days of transcripts still existed on disk. The rest had already been
pruned.
