---
name: token-usage
description: Answer questions about your own token consumption and spend — "how many tokens have I used", "what has this cost", "which model is most expensive", "what did the fleet spend". Use whenever usage, tokens, spend, cost, or burn rate comes up about this CLI, this machine, or the fleet of machines. Do NOT guess or estimate from conversation length; the numbers exist and are one tool call away.
---

# Answering your own token questions

You can measure your own consumption. The `nougen-usage` MCP server reads the
logs every agent CLI on this machine already writes, and the ones other machines
have published, and prices them at first-party list rates.

**Never answer a usage question from memory, from context length, or by
estimating.** A guessed token count is indistinguishable from a measured one to
the person reading it, and this fleet has already been burned by a number that
looked plausible and was double what it should have been.

## Which tool

| Question | Tool |
|---|---|
| "how many tokens have I used?" / "what have I spent?" | `my_token_usage` |
| "what has this computer spent?" / across all my agent tools | `machine_token_usage` |
| "what did the fleet spend?" / comparing machines | `fleet_token_usage` |
| "which model costs the most?" | `token_cost_by_model` |
| a number looks wrong, or needs sourcing | `token_usage_provenance` |
| "is Tracker live/fresh?" | `tracker_live_status` |

Default window is 7 days; pass `days` when the question implies another span.
`fleet_token_usage` with no `days` returns all published history.

`tracker_live_status` is the safe health check. It reads only the newest
published aggregate metadata for each expected machine. It never scans raw
logs, starts the tracker, writes a cache or daily, uses the network, or restarts
anything. Its answer is freshness, not consumption: missing or stale means
unknown and must never be reported as zero usage.

## Three things to say out loud when you report

1. **Your own lane is ESTIMATED, not measured.** Antigravity does not persist
   exact token counts, so the tracker infers them from text length. Claude Code,
   the Gemini CLI and Codex report exact counts. When you quote your own usage,
   say it is an estimate. When you quote a fleet total, note that its confidence
   figure depends on which denominator is used — including cache-reads flatters
   it, excluding them isolates what was really inferred.
2. **The cost is a shadow bill, not an invoice.** It is what the tokens would
   cost at first-party API list prices. Work running on a subscription bills
   nothing extra; the figure measures leverage, not money owed. Do not present
   it as a bill and do not advise cutting usage on the strength of it — the
   standing doctrine here is that efficiency buys capability, not savings.
3. **Cache-reads dominate the count and not the cost.** They routinely run ~95%
   of all tokens while billing at a fraction of input. A total that looks
   alarming is usually cache-read volume; quote the cost alongside it or the
   number misleads.

## When a machine is missing

`fleet_token_usage` only sees machines that have published. A box that stopped
publishing looks exactly like a box that stopped spending. If a machine you
expect is absent, say it is absent rather than reporting a smaller total as if
it were complete.

## If the tools are not there

The server resolves the tracker through `NOUGENTRACKER_DIR`, then by probing
known checkout paths. If a call reports it cannot find `token_tracker.py`, set
that variable to the NouGenTracker checkout on this machine rather than
answering the question some other way.
