# NouGenTracker

A single-file, cross-provider **AI token usage tracker**. It reads the local logs each
coding agent already writes and reconciles them into one honest, cache-aware report —
input / output / cache-read / reasoning tokens by **provider**, by **day**, over any range.

No telemetry, no network calls for the core report — it parses what's already on disk.

## Why

Every AI coding tool counts tokens differently and hides them in a different place.
Claude Code, OpenAI Codex, the Gemini CLI, and Google Antigravity each keep their own
store with their own schema and their own definition of "a token." NouGenTracker treats
those disparate histories as **one body of work** and produces a unified ledger, so you
can see what the whole fleet actually spent — and what it *would* have cost at first-party
API list prices (a usage gauge, not a bill).

## Providers covered

| Lane | Source | Tokens |
|---|---|---|
| **Anthropic (Claude Code)** | `~/.claude/projects/**/*.jsonl` | exact |
| **OpenAI (Codex)** | `~/.codex/state_5.sqlite` + rollout logs | exact (+ native `tokens_used` cross-check) |
| **Google (Gemini CLI)** | `~/.gemini/tmp/*/chats/session-*.json[l]` | exact via the `tokens` block |
| **Google (Antigravity)** | `~/.gemini/antigravity*/brain/**/transcript.jsonl` | estimated (chars÷4) |
| **Fleet ledger** (local Ollama/Gemma, OpenRouter, HF) | `vault/fleet_usage.jsonl` | exact, see `fleet/` |

## Usage

```bash
python token_tracker.py                       # last 2 days (default)
python token_tracker.py --days 7              # last 7 days
python token_tracker.py --weeks 2            # last 14 days
python token_tracker.py --month 2026-06      # a calendar month
python token_tracker.py --start 2026-06-01 --end 2026-06-15
python token_tracker.py --compare 7          # last 7d vs prior 7d, per provider
python token_tracker.py --by-provider        # add the by-provider summary

# Exact lower bound (must be tz-aware ISO):
TOKEN_TRACKER_CUTOFF="2026-06-29T06:00:00-04:00" python token_tracker.py --by-provider
```

### What you get
- Per-provider and per-day token tables.
- An **API-equivalent shadow bill** with cache-reads priced as cache-reads (not as fresh
  input), so the number is honest rather than inflated.
- Cache-health, model-class, and "cold context leak" route hints.

## Forward tracking for local/free lanes (`fleet/`)

Local Ollama/Gemma calls and OpenRouter/HF requests don't persist token counts anywhere,
so they're invisible to a log parser. The `fleet/` components fix that **going forward**:

- **`fleet/fleet_usage_proxy.py`** — a transparent logging proxy that sits on Ollama's
  port, forwards to a relocated upstream, and records each response's
  `prompt_eval_count` / `eval_count` to an append-only ledger. Inference correctness
  first: bytes are forwarded faithfully; logging is a best-effort side effect.
- **`fleet/fleet_usage_log.py`** — the append-only JSONL ledger writer
  (`vault/fleet_usage.jsonl`). `token_tracker.py` reads it as the Fleet provider lane.

Point your OpenAI-compatible / Ollama clients at the proxy and the local lanes start
showing up in the report. Config via `FLEET_PROXY_PORT`, `FLEET_OLLAMA_UPSTREAM`,
`FLEET_USAGE_LEDGER`.

## Design notes
- **Trust the tool's own counter.** Where a provider records exact tokens (Claude usage
  blocks, Codex `tokens_used`, the Gemini CLI `tokens` block), the tracker uses them and
  only estimates when there is genuinely nothing on disk.
- **A request is the unit, not a log row.** Claude Code writes one row per content block
  — the assistant message, then one per `tool_use` — and every row repeats the *same*
  usage object under a fresh `uuid`. Deduping by uuid dedupes nothing. Measured on one
  box 2026-08-01: 652 of 1,136 requests spanned multiple rows, usage identical in every
  one, inflating cache-reads from 287M to 554M. Dedup is by `requestId` (the unit the
  provider bills), with `uuid` as the fallback. The bug scaled with how many tools a
  session called, so the busier the work, the more wrong the report.
- **Findings are computed or absent.** The report's closing section reads the window's
  own invocations — spend concentration, cache hit rate per lane, caches written but
  never reused, estimated-vs-exact confidence, and days above 3x their lane's own median.
  Each line carries the number it came from. When nothing crosses a threshold it says so,
  because advice that fires unconditionally teaches people to skip the section.
- **Cache discipline is the whole economy.** Cache-reads dominate token *counts* but bill
  at ~10% of input; pricing them correctly is the difference between an honest gauge and a
  scary-but-meaningless number.
- Pricing tables are tunable knobs (`DOC` = first-party documented, `EST` = estimate).

---

*Part of the NouGenAi / Who Visions fleet. Built for the Stadium.*

## Fleet totals — every machine's usage and spend, summed

`token_tracker.py` reads the logs on the box it runs on. That answer never
leaves the machine, so the fleet-wide question had no answer. Dailies are the
transport: each machine exports its own days, git carries them, every machine
sums them.

```bash
python3 token_tracker.py --install-hooks          # once per clone (see below)
NOUGEN_MACHINE=phoebus python3 token_tracker.py --publish
git push -u origin dailies/phoebus                # then open a PR

python3 token_tracker.py --fleet                  # totals across every machine
```

Each machine writes only `dailies/<machine>/<YYYY-MM-DD>.json`, so two boxes
pushing at once cannot conflict. Re-exporting a day replaces that day's file, so
a re-run can never double-count.

To publish on a schedule, run `--publish` from cron or launchd on each box. It
is deliberately not a GitHub Action: a runner has no access to the agent logs on
your machines, so the export has to happen where the logs are.

### Dollars are a view, not a record

Daily files store **tokens only**. Spend is recomputed from those tokens at read
time, using the price table in the clone doing the reading.

This matters because prices and measurements age differently. `claude-opus-5`
billed at a fifth of its real rate until the table was fixed; introductory rates
expire; a machine on an older checkout has an older table. Freezing dollars into
each daily file would bake every machine's pricing bugs into the fleet total
permanently, and correcting a price would mean rewriting every published file.
Recomputing means one table fix re-prices every machine retroactively, including
days exported months ago.

### Machine identity

Resolved the way NouGenRelay resolves it — `NOUGEN_MACHINE`, else the hostname,
lowercased and slugified — so one grep finds a box's commits, its handoffs, and
its dailies. **Set `NOUGEN_MACHINE` if the box already has a fleet name.** One
machine under two names is counted twice, and the total is wrong in the
direction that looks plausible; `--export` warns on a name the fleet has never
published under.

`--install-hooks` is per-clone because `core.hooksPath` lives in `.git/config`,
which cannot be committed. Nothing depends on someone having run it: the tool
stamps its own commits with `Machine:`/`Agent:` trailers, and the hook only
covers commits written by hand.

### What the numbers say about themselves

- **confidence** — the share of the total that was *measured* rather than
  inferred from text length. On this fleet only ~26% of reported tokens are
  measured, which a plain total hides completely.
- **overlap** — if two machines read the same synced log directory, both report
  the same calls and a plain sum doubles them. Every machine-day carries a
  64-wide bottom-k MinHash sketch of its call fingerprints; the aggregator
  estimates Jaccard similarity and says so when it is high. ~0.5 KB per
  machine-day regardless of call volume.
- **outlier days** — modified z-score (median + MAD) rather than mean and
  standard deviation, which one spike would drag until it hid itself. Computed
  on log10 of the daily total: usage is multiplicative and spans three orders of
  magnitude here, and on raw values MAD inflates until nothing clears the
  threshold (max |z| = 1.81 across this fleet's 17 days — silence). In log space
  the same series peaks at 5.23.
