# Integrations — letting an agent answer its own token questions

The tracker knew what every agent CLI on the machine had spent. The agents did
not. "How many tokens have I used?" went to a human with a terminal, and the
usual answer was a guess from conversation length.

This directory connects the two.

## `agy_usage_mcp.py` — MCP server

A zero-dependency JSON-RPC stdio server exposing five tools:

| Tool | Answers |
|---|---|
| `my_token_usage` | this CLI's own lane |
| `machine_token_usage` | every lane on this machine + the shadow bill |
| `fleet_token_usage` | every machine that has published dailies |
| `token_cost_by_model` | which model is costing the most |
| `token_usage_provenance` | where the numbers came from, and which are estimated |

**The tracker stays the single authority.** The server shells out to
`token_tracker.py` rather than reimplementing pricing, so a price-table fix in
this repo re-prices every answer with no change here. Results are cached for 15
minutes (`AGY_USAGE_CACHE_TTL`) because a full scan reads hundreds of
transcripts and nobody should pay for it twice in one conversation.

**Stdlib only, on purpose.** A CLI spawns this in whatever environment it
happens to have; a missing package would turn a usage question into a crash.
Every path resolves at runtime — `NOUGENTRACKER_DIR` first, then a probe of
known checkout locations, then an error naming the variable to set. No drive
letters, no usernames, nothing that only works on the box it was written on.

### Install (Antigravity / `agy`)

Merge into `~/.gemini/config/mcp_config.json`:

```json
{
  "mcpServers": {
    "nougen-usage": {
      "command": "python",
      "args": ["/path/to/integrations/agy_usage_mcp.py"],
      "env": { "NOUGENTRACKER_DIR": "/path/to/NouGenTracker" }
    }
  }
}
```

Any MCP-speaking client works the same way — nothing in the server is
Antigravity-specific.

### Verify without a client

```bash
printf '%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | python agy_usage_mcp.py
```

## `agy_token_usage_SKILL.md` — the skill

Tools without instructions get used wrongly. Copy to
`~/.gemini/config/skills/token-usage/SKILL.md`. It tells the agent which tool
answers which question and, more importantly, three things to say out loud:

- **Antigravity's own numbers are estimated, not measured** — that lane does not
  persist exact counts. Claude Code, the Gemini CLI and Codex do.
- **The cost is a shadow bill, not an invoice** — what the tokens would cost at
  list price. Work on a subscription bills nothing extra.
- **Cache-reads dominate the count and not the cost** — they run ~95% of volume
  at a fraction of the input rate, so a total that looks alarming is usually
  cache-read volume.

And one rule: **never answer a usage question by estimating.** A guessed token
count is indistinguishable from a measured one to whoever reads it, and this
project has already shipped a number that looked plausible and was nearly double
what it should have been.

## A missing machine is not a cheap machine

`fleet_token_usage` only sees machines that have published. A box that stopped
publishing looks exactly like a box that stopped spending, so the skill requires
the agent to name an absent machine rather than quote a smaller total as if it
were complete.
