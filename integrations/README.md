# Integrations — letting an agent answer its own token questions

Every agent on this machine writes a log of what it spent. None of them could
read it, so "how many tokens have I used?" went to a human with a terminal and
the usual answer was a guess from conversation length.

`nougen_usage_mcp.py` is an MCP server that closes that gap for **every**
MCP-speaking CLI — Antigravity, Claude Code, the Gemini CLI, Codex.

## Install

```bash
python integrations/nougen_usage_mcp.py --install --dry-run   # see the plan
python integrations/nougen_usage_mcp.py --install             # do it
```

It probes for each CLI's registry, backs up every file it touches, merges
rather than overwrites, and skips anything not installed:

| CLI | Registry | Format |
|---|---|---|
| Antigravity (`agy`) | `~/.gemini/config/mcp_config.json` | JSON |
| Gemini CLI | `~/.gemini/settings.json` | JSON |
| Claude Code | `~/.claude.json` | JSON |
| Codex | `~/.codex/config.toml` | TOML |

Restart each CLI afterwards. Then copy `token_usage_SKILL.md` to your agent's
skills directory — for Antigravity that is
`~/.gemini/config/skills/token-usage/SKILL.md`.

## Tools

| Tool | Answers |
|---|---|
| `my_token_usage` | this CLI's own lane |
| `machine_token_usage` | every lane on this machine + the shadow bill |
| `fleet_token_usage` | every machine that has published dailies |
| `token_cost_by_model` | which model is costing the most |
| `token_usage_provenance` | where the numbers came from, and which are estimated |

Each returns **prose to quote and a validated object to compute with** —
`structuredContent` alongside the text, with `outputSchema` declared. Text-only
forces an agent to re-parse a table it will eventually parse wrong.

## Design commitments

Each of these is a decision someone will otherwise re-open.

**The tracker stays the single authority.** The server shells out to
`token_tracker.py` rather than reimplementing pricing, so a price-table fix in
this repo re-prices every answer with no change here. Duplicated pricing is how
two components come to disagree about the same day.

**Stdlib only.** A CLI spawns this in whatever environment it happens to have; a
missing package would turn a usage question into a crash. That constraint is why
the Codex TOML edit is written by hand — `tomllib` is read-only and 3.11+, and a
writer dependency would cost more than it saves. The edit is append-only and
idempotent, which is what makes that safe.

**Uncertainty travels with the number.** Every result carries `cache_age_seconds`,
whether it is `live` or `cached`, which sources were estimated rather than
measured, and the disclaimers that belong beside it. A token count whose
provenance was dropped is indistinguishable from one that was invented.

**stdout belongs to the protocol.** All logging goes to stderr. One stray print
corrupts the stream and the failure looks like a broken client.

**Read-only, and it says so.** Every tool declares `readOnlyHint`,
`idempotentHint` and `openWorldHint: false`, so a client can skip a confirmation
prompt it would otherwise be right to show.

**Protocol versions are negotiated, not assumed.** `2025-06-18`, `2025-03-26` and
`2024-11-05` are all answered correctly — one binary serving four CLIs that
upgrade on their own schedules.

**Tool failures are content, not protocol errors.** A tracker that cannot be
found returns `isError: true` with a message the agent can relay, instead of a
JSON-RPC error the user never sees.

## Verify without a client

```bash
python integrations/nougen_usage_mcp.py --selftest
```

Thirteen checks covering protocol negotiation, schema/annotation completeness,
argument rejection, unknown-tool rejection, the report parsers, and one live
end-to-end tool call.

## What the skill enforces

Tools without instructions get used wrongly. `token_usage_SKILL.md` requires the
agent to state three things when it reports a number:

- **Antigravity's counts are estimated**, not measured — that lane persists no
  exact counts. Claude Code, Codex and the Gemini CLI do.
- **The cost is a shadow bill, not an invoice.** Work on a subscription bills
  nothing extra; the figure measures leverage, not money owed.
- **Cache-reads dominate the count and not the cost** — ~95% of volume at a
  fraction of the input rate, so an alarming total is usually cache-read volume.

And one rule: **never answer a usage question by estimating.** A guessed count is
indistinguishable from a measured one to whoever reads it, and this project has
already shipped a plausible-looking number that was nearly double.

## A missing machine is not a cheap machine

`fleet_token_usage` only sees machines that have published, and says so in its
own payload. A box that stopped publishing looks exactly like a box that stopped
spending, so the caveat travels with the data rather than living in this file.
