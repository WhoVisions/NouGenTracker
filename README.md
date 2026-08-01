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

## Multiple machines: the relay (`relay.py`)

One box only ever knew its own spend. The relay passes a **baton** between machines so a
single report can tell the truth about a day. A leg has three phases, and each is a command:

```bash
python relay.py start --mission "what this box is about to work on"
python relay.py mid      # checkpoint: re-export and stamp progress
python relay.py end      # hand off: final export, commit, push
python relay.py status   # peers, freshness, overlap and confidence
```

`--mission` is the half that stops duplicated work. Rollups say what a machine
*did*; the intent board says what a machine is *doing*, published at `start` and read
by every peer before it begins. Two boxes on this fleet took the same instruction and
built the same feature thirty minutes apart because nothing carried that signal.

Then any report blends them in automatically:

```bash
python token_tracker.py --days 1 --by-machine   # split by box
python token_tracker.py --days 1 --no-peers     # this box alone
```

**Bake it in** — wire the phases to your Claude Code session lifecycle
(SessionStart → `start`, Stop → `mid`, SessionEnd → `end`):

```bash
python relay.py hooks             # print the config
python relay.py hooks --install   # merge it in, with a backup
```

Design rules this follows:

- **Aggregate only.** What crosses the wire is `day, source, model` plus the five token
  buckets. No session ids, no transcript paths, no usernames — privacy by construction,
  not by a redaction pass.
- **One file per machine per day**, so concurrent machines never write the same path and
  therefore never conflict. No shared index to corrupt.
- **Never double count — twice over.** A machine refuses to read its own rollup back as a
  peer, checked on the `machine` field rather than the folder name. That catches the easy
  case. The hard one is two machines reading the *same synced log directory*, where the
  duplicate arrives wearing a different machine's name and every individual file still
  looks correct. Each machine-day therefore carries a 64-wide bottom-k MinHash sketch of
  its call fingerprints (~0.5 KB regardless of call volume); the reader estimates Jaccard
  similarity between machine-days and **says so** when it is high. Days within one of each
  other are compared, not just identical dates, because two boxes in different timezones
  bucket the same call into different calendar days. Totals are **not** silently corrected:
  guessing which copy to drop would be a worse error than naming the doubt.
- **Say how much is actually measured.** Some providers report usage; others are inferred
  from text length, and summing them into one number hides that. Reports print the share of
  billable tokens that were measured, and name the sources contributing inferred ones.
  `cache_read` is excluded from that ratio — it routinely runs 100× every other field, so
  including it turns the figure into "percent of cache-reads measured" wearing a more
  important-sounding name. An empty corpus reports **unknown**, never 100%.
- **A quiet peer shows as an age, never as a smaller total.** The freshness table prints
  every run; anything past `RELAY_STALE_HOURS` (default 48) is marked `STALE`.
- **Transport failure is never fatal.** Rollups are written locally first; the git push is
  best effort. Being the first machine on the relay is a normal state, not an error.
- `mid` is throttled (`RELAY_MID_MIN_MINUTES`, default 30) because the Stop hook fires
  every turn and a full export rescans every transcript.

Knobs: `RELAY_DIR`, `RELAY_REMOTE`, `RELAY_BRANCH`, `RELAY_MACHINE`, `RELAY_STALE_HOURS`,
`RELAY_WINDOW_DAYS`, `RELAY_MID_MIN_MINUTES` — env beats `tracker_config.json` beats
default, same engine as every other knob here. Identity resolves
`RELAY_MACHINE` → `NOUGEN_MACHINE` → config → hostname probe, and prints which route won.
**Point `RELAY_REMOTE` at a private repo.** Spend data does not belong in a public one.

## Design notes
- **Trust the tool's own counter.** Where a provider records exact tokens (Claude usage
  blocks, Codex `tokens_used`, the Gemini CLI `tokens` block), the tracker uses them and
  only estimates when there is genuinely nothing on disk.
- **Cache discipline is the whole economy.** Cache-reads dominate token *counts* but bill
  at ~10% of input; pricing them correctly is the difference between an honest gauge and a
  scary-but-meaningless number.
- Pricing tables are tunable knobs (`DOC` = first-party documented, `EST` = estimate).

---

*Part of the NouGenAi / Who Visions fleet. Built for the Stadium.*
