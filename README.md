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
- **Cache discipline is the whole economy.** Cache-reads dominate token *counts* but bill
  at ~10% of input; pricing them correctly is the difference between an honest gauge and a
  scary-but-meaningless number.
- Pricing tables are tunable knobs (`DOC` = first-party documented, `EST` = estimate).

---

*Part of the NouGenAi / Who Visions fleet. Built for the Stadium.*
