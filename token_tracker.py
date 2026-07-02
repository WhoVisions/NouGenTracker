#!/usr/bin/env python3
"""NouGenTracker — cross-provider token usage monitor.

Aggregates token usage (input / output / cache-read / reasoning) across every
AI lane that leaves telemetry on this machine, then renders day tables, an
honest API-equivalent shadow bill, cache-health scores, and period analytics.

Sources
-------
1. Claude Code        JSONL transcripts under ``~/.claude/projects/`` (exact).
2. Google Antigravity active sessions via the internal loopback RPC (exact)
                      plus archived ``brain`` transcripts (chars/4 estimate).
3. OpenAI Codex       ``~/.codex/state_5.sqlite`` threads + rollout JSONL
                      (exact, per token_count event).
4. Gemini CLI         ``~/.gemini/tmp/*/chats`` (exact when the log carries a
                      tokens dict, chars/4 estimate otherwise).
5. Fleet ledger       append-only JSONL written by ``fleet/fleet_usage_proxy.py``
                      for local Ollama/Gemma, OpenRouter, and HF lanes (exact).

Importing this module is side-effect free: nothing is scanned and nothing is
printed until :func:`main` runs (the one optional read is the small JSON
config overlay below). Public helpers (``price_for``, ``model_bill``,
``resolve_model``, ``parse_ts``, the C4AI token-counter port, …) are importable
by other tools (e.g. ``hi_token_tracker.py``) without touching ``sys.argv``.

Configuration
-------------
Nothing is hardcoded-only. Every knob resolves with this precedence:

1. environment variable (the knob name uppercased, e.g. ``CHARS_PER_TOKEN``)
2. the JSON overlay file named by ``TOKEN_TRACKER_CONFIG``
   (default: ``tracker_config.json`` beside this script — see
   ``tracker_config.example.json`` for every supported key)
3. the built-in default, so the tool always runs with zero setup

Overridable: all source paths (``claude_projects_dir``,
``antigravity_brain_dirs``, ``codex_state_db``, ``gemini_cli_tmp_dir``,
``fleet_usage_ledger``), the model catalog (``model_map``, ``model_pricing``,
``default_pricing``, ``free_models``, ``settings_model_rules``), provider
labels/order, every heuristic and threshold, RPC endpoints/timeouts, report
sizes, and the token-counter demo defaults.
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dtm
import glob
import json
import logging
import math
import os
import re
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
)

__version__ = "2.1.0"

LOG = logging.getLogger("nougentracker")

# Token-bucket keys, in canonical order. ``cols()`` returns values in this
# order minus the reordering quirk kept for backward compatibility below.
KEYS: Tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "reasoning_tokens",
)

# A per-bucket token tally: bucket key -> token count.
TokenCounts = Dict[str, int]

# ---------------------------------------------------------------------------
# Configuration engine — every knob below resolves dynamically:
#   1. environment variable (knob name uppercased)   — per-run override
#   2. TOKEN_TRACKER_CONFIG json overlay (optional)  — per-machine override
#   3. built-in default                              — zero-setup fallback
# ---------------------------------------------------------------------------

TRACKER_CONFIG_PATH = os.environ.get(
    "TOKEN_TRACKER_CONFIG",
    str(Path(__file__).resolve().parent / "tracker_config.json"),
)


def _load_tracker_config(path: str) -> Dict[str, Any]:
    """Read the optional JSON overlay; malformed files degrade to defaults."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("config root must be a JSON object")
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        LOG.warning("ignoring unreadable config %s: %s", path, exc)
        return {}


CONFIG: Dict[str, Any] = _load_tracker_config(TRACKER_CONFIG_PATH)


def _cfg(name: str, default: Any, cast: Callable[[Any], Any] = str) -> Any:
    """Resolve one knob: env var beats config file beats built-in default."""
    env_val = os.environ.get(name.upper())
    if env_val:
        try:
            return cast(env_val)
        except (TypeError, ValueError):
            LOG.warning("bad env %s=%r; falling back", name.upper(), env_val)
    if name in CONFIG:
        try:
            return cast(CONFIG[name])
        except (TypeError, ValueError):
            LOG.warning("bad config %s=%r; using default", name, CONFIG[name])
    return default


def _as_path_list(val: Any) -> List[str]:
    """Accept a JSON list or an os.pathsep-separated env string."""
    if isinstance(val, str):
        return [p for p in val.split(os.pathsep) if p]
    return [str(p) for p in val]


# ---------------------------------------------------------------------------
# Paths & environment (all env/config overridable — see module docstring)
# ---------------------------------------------------------------------------

PROJECTS = os.path.expanduser(
    _cfg("claude_projects_dir", os.path.join("~", ".claude", "projects")))

_DEFAULT_BRAIN_DIRS = [
    os.path.join("~", ".gemini", flavor, "brain")
    for flavor in ("antigravity", "antigravity-cli", "antigravity-ide",
                   "antigravity-backup")
]
ANTIGRAVITY_BRAIN_DIRS: List[str] = [
    os.path.expanduser(p)
    for p in _cfg("antigravity_brain_dirs", _DEFAULT_BRAIN_DIRS, cast=_as_path_list)
]

CODEX_STATE = os.path.expanduser(
    _cfg("codex_state_db", os.path.join("~", ".codex", "state_5.sqlite")))

# Fleet usage ledger: forward token accounting for local Ollama/Gemma,
# OpenRouter, HF and other lanes that otherwise write no token telemetry to
# disk. Written by fleet/fleet_usage_proxy.py + the instrumented fleet clients.
FLEET_USAGE_LEDGER = _cfg(
    "fleet_usage_ledger",
    str(Path(__file__).resolve().parent / "vault" / "fleet_usage.jsonl"),
)

# The Gemini CLI stores conversations under <tmp>/<project>/chats/ as either
# a single .json file with a messages[] array, or .jsonl one msg/line. These
# hold message TEXT only — no usageMetadata — so tokens are ESTIMATED via the
# same chars-per-token heuristic the Antigravity fallback uses.
GEMINI_CLI_TMP_DIR = os.path.expanduser(
    _cfg("gemini_cli_tmp_dir", os.path.join("~", ".gemini", "tmp")))
GEMINI_CLI_CHAT_GLOBS: List[str] = [
    os.path.join(GEMINI_CLI_TMP_DIR, "*", "chats", "*.json"),
    os.path.join(GEMINI_CLI_TMP_DIR, "*", "chats", "*.jsonl"),
    os.path.join(GEMINI_CLI_TMP_DIR, "*", "chats", "*", "*.jsonl"),
]

DEFAULT_DAYS = _cfg("token_tracker_default_days", 2, int)
# --lanes loads the full available history for the analytics dashboard.
LANES_HISTORY_DAYS = _cfg("lanes_history_days", 760, int)

# Estimation heuristic shared by the Antigravity fallback and Gemini CLI
# parsers: ~N characters per token, plus a flat per-call context overhead
# (system prompt, tool schemas) that never appears in the transcript text.
CHARS_PER_TOKEN = _cfg("chars_per_token", 4, int)
CONTEXT_OVERHEAD_TOKENS = _cfg("context_overhead_tokens", 6_000, int)

# Cache-health thresholds (cache-read share of all tokens).
CACHE_SHARE_EXCELLENT = _cfg("cache_share_excellent", 0.95, float)
CACHE_SHARE_GOOD = _cfg("cache_share_good", 0.85, float)
CACHE_SHARE_WARNING = _cfg("cache_share_warning", 0.60, float)
# Fresh-input volume above which a model/session is flagged, regardless of share.
INPUT_WARN_THRESHOLD = _cfg("input_warn_threshold", 250_000, int)

# Cache-creation bills at this multiple of the fresh-input rate
# (the 5-minute cache-write tier on Anthropic's price sheet).
CACHE_WRITE_MULTIPLIER = _cfg("cache_write_multiplier", 1.25, float)

# Report sizes.
TOP_HOGS_COUNT = _cfg("top_hogs_count", 20, int)
TOP_SESSIONS_COUNT = _cfg("top_sessions_count", 10, int)

# Model assumed for transcripts that never record a model switch.
ANTIGRAVITY_DEFAULT_MODEL = _cfg("antigravity_default_model", "gemini-3-flash-preview")
GEMINI_CLI_DEFAULT_MODEL = _cfg("gemini_cli_default_model", ANTIGRAVITY_DEFAULT_MODEL)

# Antigravity loopback RPC endpoint.
RPC_HOST = _cfg("antigravity_rpc_host", "127.0.0.1")
RPC_SERVICE = _cfg("antigravity_rpc_service",
                   "exa.language_server_pb.LanguageServerService")
RPC_HEARTBEAT_TIMEOUT = _cfg("antigravity_rpc_timeout", 1.5, float)
RPC_QUERY_TIMEOUT = _cfg("antigravity_rpc_query_timeout", 3.0, float)

# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------

MODEL_MAP: Dict[str, str] = {
    'MODEL_PLACEHOLDER_M132': 'gemini-3.5-flash-high',
    'MODEL_PLACEHOLDER_M131': 'gemini-3.5-flash-medium',
    'MODEL_PLACEHOLDER_M130': 'gemini-3.5-flash-low',
    'MODEL_PLACEHOLDER_M37': 'gemini-3.1-pro-high',
    'MODEL_PLACEHOLDER_M36': 'gemini-3.1-pro-low',
    'MODEL_PLACEHOLDER_M18': 'gemini-3-flash',
    'MODEL_PLACEHOLDER_M8': 'gemini-3-pro-high',
    'MODEL_PLACEHOLDER_M7': 'gemini-3-pro-low',
    'MODEL_PLACEHOLDER_M9': 'gemini-3-pro-image',
    'MODEL_PLACEHOLDER_M26': 'claude-opus-4-6-thinking',
    'MODEL_PLACEHOLDER_M35': 'claude-sonnet-4-6-thinking',
    'MODEL_PLACEHOLDER_M12': 'claude-opus-4-5-thinking',
    'MODEL_OPENAI_GPT_OSS_120B_MEDIUM': 'gpt-oss-120b-medium',
    'MODEL_CLAUDE_4_5_SONNET': 'claude-sonnet-4-5',
    'MODEL_CLAUDE_4_5_SONNET_THINKING': 'claude-sonnet-4-5-thinking',

    # New models from gemini_api_models.json
    'models/gemini-2.5-flash': 'gemini-2.5-flash',
    'models/gemini-2.5-pro': 'gemini-2.5-pro',
    'models/gemini-2.0-flash': 'gemini-2.0-flash',
    'models/gemini-2.0-flash-001': 'gemini-2.0-flash-001',
    'models/gemini-2.0-flash-lite-001': 'gemini-2.0-flash-lite-001',
    'models/gemini-2.0-flash-lite': 'gemini-2.0-flash-lite',
    'models/gemini-2.5-flash-preview-tts': 'gemini-2.5-flash-preview-tts',
    'models/gemini-2.5-pro-preview-tts': 'gemini-2.5-pro-preview-tts',
    'models/gemma-3-1b-it': 'gemma-3-1b-it',
    'models/gemma-3-4b-it': 'gemma-3-4b-it',
    'models/gemma-3-12b-it': 'gemma-3-12b-it',
    'models/gemma-3-27b-it': 'gemma-3-27b-it',
    'models/gemma-3n-e4b-it': 'gemma-3n-e4b-it',
    'models/gemma-3n-e2b-it': 'gemma-3n-e2b-it',
    'models/gemma-4-26b-a4b-it': 'gemma-4-26b-a4b-it',
    'models/gemma-4-31b-it': 'gemma-4-31b-it',
    'models/gemini-flash-latest': 'gemini-flash-latest',
    'models/gemini-flash-lite-latest': 'gemini-flash-lite-latest',
    'models/gemini-pro-latest': 'gemini-pro-latest',
    'models/gemini-2.5-flash-lite': 'gemini-2.5-flash-lite',
    'models/gemini-2.5-flash-image': 'nano-banana',
    'models/gemini-3-pro-preview': 'gemini-3-pro-preview',
    'models/gemini-3-flash-preview': 'gemini-3-flash-preview',
    'models/gemini-3.1-pro-preview': 'gemini-3.1-pro-preview',
    'models/gemini-3.1-pro-preview-customtools': 'gemini-3.1-pro-preview-customtools',
    'models/gemini-3.1-flash-lite-preview': 'gemini-3.1-flash-lite-preview',
    'models/gemini-3-pro-image-preview': 'nano-banana-pro',
    'models/nano-banana-pro-preview': 'nano-banana-pro',
    'models/gemini-3.1-flash-image-preview': 'nano-banana-2',
    'models/lyria-3-clip-preview': 'lyria-3-clip-preview',
    'models/lyria-3-pro-preview': 'lyria-3-pro-preview',
    'models/gemini-robotics-er-1.5-preview': 'gemini-robotics-er-1.5-preview',
    'models/gemini-2.5-computer-use-preview-10-2025': 'gemini-2.5-computer-use-preview-10-2025',
    'models/deep-research-pro-preview-12-2025': 'deep-research-pro-preview-12-2025',
    'models/gemini-embedding-001': 'gemini-embedding-001',
    'models/gemini-embedding-2-preview': 'gemini-embedding-2-preview',
    'models/aqa': 'aqa',
    'models/imagen-4.0-generate-001': 'imagen-4',
    'models/imagen-4.0-ultra-generate-001': 'imagen-4-ultra',
    'models/imagen-4.0-fast-generate-001': 'imagen-4-fast',
    'models/veo-2.0-generate-001': 'veo-2',
    'models/veo-3.0-generate-001': 'veo-3',
    'models/veo-3.0-fast-generate-001': 'veo-3-fast',
    'models/veo-3.1-generate-preview': 'veo-3.1',
    'models/veo-3.1-fast-generate-preview': 'veo-3.1-fast',
    'models/veo-3.1-lite-generate-preview': 'veo-3.1-lite',
    'models/gemini-2.5-flash-native-audio-latest': 'gemini-2.5-flash-native-audio-latest',
    'models/gemini-2.5-flash-native-audio-preview-09-2025': 'gemini-2.5-flash-native-audio-preview-09-2025',
    'models/gemini-2.5-flash-native-audio-preview-12-2025': 'gemini-2.5-flash-native-audio-preview-12-2025',
    'models/gemini-3.1-flash-live-preview': 'gemini-3.1-flash-live-preview',
}
# Overlay: {"model_map": {"raw-id": "display-name"}} in the config file.
MODEL_MAP.update({str(k): str(v)
                  for k, v in (CONFIG.get("model_map") or {}).items()})


def _config_settings_rules() -> Tuple[Tuple[Tuple[str, ...], str], ...]:
    """Extra model-switch detection rules from the config overlay, tried
    before the built-ins. Format: [[["needle", ...], "model-name"], ...]."""
    rules: List[Tuple[Tuple[str, ...], str]] = []
    for entry in CONFIG.get("settings_model_rules") or []:
        try:
            needles, model = entry
            if isinstance(needles, str):
                needles = [needles]
            rules.append((tuple(str(n).lower() for n in needles), str(model)))
        except (TypeError, ValueError):
            LOG.warning("skipping bad settings_model_rules entry: %r", entry)
    return tuple(rules)


# Antigravity fallback transcripts record model switches as free-text
# <USER_SETTINGS_CHANGE> blobs. Ordered longest-match-first: the first entry
# whose any needle appears in the lowercased blob wins. Config rules first.
_BUILTIN_SETTINGS_MODEL_RULES: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("gemini 3.5 flash (high)",), "gemini-3.5-flash-high"),
    (("gemini 3.5 flash (medium)",), "gemini-3.5-flash-medium"),
    (("gemini 3.5 flash (low)",), "gemini-3.5-flash-low"),
    (("gemini 3.5 pro",), "gemini-3.5-pro"),
    (("gemini 3.5 flash",), "gemini-3.5-flash"),
    (("gemini 3.1 pro (high)",), "gemini-3.1-pro-high"),
    (("gemini 3.1 pro (low)",), "gemini-3.1-pro-low"),
    (("claude sonnet 4.6",), "claude-sonnet-4-6-thinking"),
    (("claude opus 4.6",), "claude-opus-4-6-thinking"),
    (("gpt-5.6 sol", "gpt 5.6 sol"), "gpt-5.6-sol"),
    (("gpt-5.6 terra", "gpt 5.6 terra"), "gpt-5.6-terra"),
    (("gpt-5.6 luna", "gpt 5.6 luna"), "gpt-5.6-luna"),
    (("gpt-oss 120b", "gpt-oss 128b"), "gpt-oss-120b-medium"),
    (("gemini 3 flash",), "gemini-3-flash-preview"),
    (("gemini 3",), "gemini-3-flash-preview"),
)
_SETTINGS_MODEL_RULES = _config_settings_rules() + _BUILTIN_SETTINGS_MODEL_RULES

_SETTINGS_CHANGE_RE = re.compile(
    r"<USER_SETTINGS_CHANGE>\s*The user changed setting `Model Selection` "
    r"from .*? to (.*?)(?:\.\s|\.$|$)"
)


def resolve_model(model_id: Optional[str]) -> str:
    """Map a raw provider model id to its canonical display name."""
    if not model_id:
        return "unknown"
    if model_id.startswith("models/"):
        model_id = model_id[7:]
    if model_id in MODEL_MAP:
        return MODEL_MAP[model_id]
    if model_id.startswith("MODEL_PLACEHOLDER_"):
        num = model_id.split("_")[-1]
        return f"gemini-{num.lower()}"
    return model_id


def _model_from_settings_change(content: str) -> Optional[str]:
    """Extract the newly-selected model from a settings-change blob, if any."""
    match = _SETTINGS_CHANGE_RE.search(content)
    if not match:
        return None
    candidate = match.group(1).strip()
    lowered = candidate.lower()
    for needles, model in _SETTINGS_MODEL_RULES:
        if any(needle in lowered for needle in needles):
            return model
    return candidate


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

DOC = "doc"   # first-party documented list price (any vendor)
EST = "est"   # estimate with no first-party source wired in


class Pricing(NamedTuple):
    """USD per million tokens. Reasoning bills at the output rate;
    cache-creation bills at 1.25x input (the 5-minute cache-write tier)."""

    input: float
    output: float
    cache_read: float
    source: str


# Claude rates are first-party list prices from claude.com/pricing.
# Everything tagged EST is an ESTIMATE — treat those as tunable knobs, not
# ground truth, and correct them as real invoices arrive. The point of this
# table is an HONEST reference bill, not an impressive one: cache-reads are
# priced as cache-reads, not as fresh input.
MODEL_PRICING: Dict[str, Pricing] = {
    # ---- Claude: first-party list prices ----
    "claude-fable-5":             Pricing(10.00, 50.00, 1.000, DOC),
    "claude-opus-4-8":            Pricing(5.00, 25.00, 0.500, DOC),
    "claude-opus-4-7":            Pricing(5.00, 25.00, 0.500, DOC),
    "claude-opus-4-6":            Pricing(5.00, 25.00, 0.500, DOC),
    "claude-opus-4-6-thinking":   Pricing(5.00, 25.00, 0.500, DOC),
    "claude-opus-4-5":            Pricing(5.00, 25.00, 0.500, DOC),
    "claude-opus-4-5-thinking":   Pricing(5.00, 25.00, 0.500, DOC),
    "claude-sonnet-4-6":          Pricing(3.00, 15.00, 0.300, DOC),
    "claude-sonnet-4-6-thinking": Pricing(3.00, 15.00, 0.300, DOC),
    "claude-sonnet-4-5":          Pricing(3.00, 15.00, 0.300, DOC),
    "claude-sonnet-4-5-20250929": Pricing(3.00, 15.00, 0.300, DOC),
    "claude-sonnet-4-5-thinking": Pricing(3.00, 15.00, 0.300, DOC),
    "claude-haiku-4-5":           Pricing(1.00, 5.00, 0.100, DOC),
    # ---- Gemini: first-party list prices (ai.google.dev/gemini-api/docs/pricing) ----
    # Flash thinking tiers (high/medium/low) share one standard price.
    "gemini-3.5-flash-high":      Pricing(1.50, 9.00, 0.15, DOC),
    "gemini-3.5-flash-medium":    Pricing(1.50, 9.00, 0.15, DOC),
    "gemini-3.5-flash-low":       Pricing(1.50, 9.00, 0.15, DOC),
    "gemini-3.5-pro":             Pricing(2.00, 12.00, 0.20, DOC),
    "gemini-3.5-pro-preview":     Pricing(2.00, 12.00, 0.20, EST),
    "gemini-3.5-flash":           Pricing(1.50, 9.00, 0.15, DOC),
    "gemini-3.5-flash-preview":   Pricing(1.50, 9.00, 0.15, EST),
    # Gemini 3.1 Pro standard, <=200k-token prompt tier.
    "gemini-3.1-pro-high":        Pricing(2.00, 12.00, 0.20, DOC),
    "gemini-3.1-pro-low":         Pricing(2.00, 12.00, 0.20, DOC),
    # Gemini 3 Flash Preview standard (the actual heavy Antigravity model).
    "gemini-3-flash-preview":     Pricing(0.50, 3.00, 0.05, DOC),
    "gemini-3-flash":             Pricing(0.50, 3.00, 0.05, DOC),
    # ---- Gemini CLI models (seen in ~/.gemini/tmp/*/chats logs) ----
    # 3.1 Pro preview shares the documented 3.1-pro standard tier.
    "gemini-3.1-pro-preview":     Pricing(2.00, 12.00, 0.20, DOC),
    "gemini-3.1-pro-preview-customtools": Pricing(2.00, 12.00, 0.20, DOC),
    # 3 Pro preview: no first-party row wired in yet -> estimate at pro tier.
    "gemini-3-pro-preview":       Pricing(2.00, 12.00, 0.20, EST),
    # 2.5 family: first-party list prices (<=200k tier).
    "gemini-2.5-pro":             Pricing(1.25, 10.00, 0.31, DOC),
    "gemini-2.5-flash":           Pricing(0.30, 2.50, 0.075, DOC),
    # 2.0 family: first-party list prices
    "gemini-2.0-flash":           Pricing(0.075, 0.30, 0.01875, DOC),
    "gemini-2.0-flash-lite":      Pricing(0.0375, 0.15, 0.009375, DOC),
    "gemini-2.0-pro":             Pricing(0.80, 3.20, 0.20, DOC),
    # Flash-lite tiers: estimate, no first-party row confirmed here.
    "gemini-3.1-flash-lite":          Pricing(0.10, 0.40, 0.01, EST),
    "gemini-3.1-flash-lite-preview":  Pricing(0.10, 0.40, 0.01, EST),
    # ---- OpenAI: first-party list prices (cached input -> cache_read) ----
    "gpt-5.6-sol-ultra":          Pricing(5.00, 30.00, 0.50, DOC),
    "gpt-5.6-sol":                Pricing(5.00, 30.00, 0.50, DOC),
    "gpt-5.6-terra":              Pricing(2.50, 15.00, 0.25, DOC),
    "gpt-5.6-luna":               Pricing(1.00, 6.00, 0.10, DOC),
    "gpt-5.5":                    Pricing(5.00, 30.00, 0.50, DOC),
    "gpt-5.4":                    Pricing(2.50, 15.00, 0.25, DOC),
    "gpt-5.4-mini":               Pricing(0.75, 4.50, 0.075, DOC),
    "gpt-5-codex-mini":           Pricing(0.75, 4.50, 0.075, EST),
    "gpt-5.1-codex-mini":         Pricing(0.75, 4.50, 0.075, EST),
    # gpt-oss is open-weights; Dave runs it free via OpenRouter/local. Nominal host est.
    "gpt-oss-120b-medium":        Pricing(0.10, 0.40, 0.010, EST),
}

def _pricing_from_row(row: Any) -> Pricing:
    """Build a Pricing from a config row [input, output, cache_read, source?]."""
    return Pricing(float(row[0]), float(row[1]), float(row[2]),
                   str(row[3]) if len(row) > 3 else EST)


# Overlay: {"model_pricing": {"model": [in, out, cache_read, "doc"|"est"]}}.
for _name, _row in (CONFIG.get("model_pricing") or {}).items():
    try:
        MODEL_PRICING[str(_name)] = _pricing_from_row(_row)
    except (TypeError, ValueError, IndexError):
        LOG.warning("skipping bad model_pricing row for %r: %r", _name, _row)

# Unknown model: conservative estimate so the bill never silently reads $0.
_DEFAULT_PRICING_ROW = CONFIG.get("default_pricing")
try:
    DEFAULT_PRICING = (_pricing_from_row(_DEFAULT_PRICING_ROW)
                       if _DEFAULT_PRICING_ROW else Pricing(1.00, 4.00, 0.100, EST))
except (TypeError, ValueError, IndexError):
    LOG.warning("skipping bad default_pricing row: %r", _DEFAULT_PRICING_ROW)
    DEFAULT_PRICING = Pricing(1.00, 4.00, 0.100, EST)

# Local Ollama/Gemma models and OpenRouter ':free' routes cost $0 — they are
# tracked for VOLUME, not spend (the fleet enforces a hard-free policy).
FREE_LOCAL_MODELS = {
    "dav1d:e2b", "sol-ai:e4b", "kaedra:e4b", "iris-ai:e4b",
    "gemma4-aggressive:e4b", "gemma4-aggressive:e2b", "gemma2:2b", "gemma:2b",
}
# Overlay: {"free_models": ["name", ...]}.
FREE_LOCAL_MODELS |= {str(m) for m in CONFIG.get("free_models") or []}

FREE_PRICING = Pricing(0.0, 0.0, 0.0, DOC)

ESTIMATED_SUFFIX = " (estimated)"


def price_for(model_name: Optional[str]) -> Pricing:
    """Resolve pricing for a model, ignoring the ' (estimated)' suffix the
    Antigravity fallback parser appends."""
    key = (model_name or "").replace(ESTIMATED_SUFFIX, "").strip()
    # Free lanes: local Ollama/Gemma + OpenRouter ':free' routes.
    if key.endswith(":free") or key in FREE_LOCAL_MODELS:
        return FREE_PRICING
    return MODEL_PRICING.get(key, DEFAULT_PRICING)


def model_bill(model_name: Optional[str], d: Mapping[str, int]) -> Tuple[float, str]:
    """Honest API-equivalent cost (USD) for one model's token bucket.

    Cache-reads are billed at their discounted rate, cache-creation at
    CACHE_WRITE_MULTIPLIER x input, and reasoning at the output rate — the way
    a real invoice prices them. Returns (cost_usd, source_tag).
    """
    pricing = price_for(model_name)
    cost = (
        d.get("input_tokens", 0) * pricing.input
        + d.get("cache_creation_input_tokens", 0) * pricing.input * CACHE_WRITE_MULTIPLIER
        + d.get("cache_read_input_tokens", 0) * pricing.cache_read
        + (d.get("output_tokens", 0) + d.get("reasoning_tokens", 0)) * pricing.output
    ) / 1_000_000
    return cost, pricing.source


# ---------------------------------------------------------------------------
# Time window
# ---------------------------------------------------------------------------

def _far_future() -> datetime:
    """A timezone-aware 'no upper bound' sentinel that survives Windows'
    OSError on datetime.max.astimezone()."""
    try:
        return datetime.max.replace(tzinfo=timezone.utc).astimezone()
    except OSError:
        return datetime(3000, 1, 1, tzinfo=timezone.utc).astimezone()


@dataclass(frozen=True)
class Window:
    """The inclusive-lower / exclusive-upper report window, plus 'now'."""

    cutoff: datetime
    limit_upper: datetime
    now: datetime
    days: int = DEFAULT_DAYS

    def contains(self, ts: datetime) -> bool:
        return self.cutoff <= ts <= self.limit_upper

    @classmethod
    def last_days(cls, days: int, now: Optional[datetime] = None) -> "Window":
        now = now or datetime.now(timezone.utc).astimezone()
        cutoff_env = os.environ.get("TOKEN_TRACKER_CUTOFF")
        cutoff = (
            datetime.fromisoformat(cutoff_env)
            if cutoff_env else now - timedelta(days=days)
        )
        return cls(cutoff=cutoff, limit_upper=_far_future(), now=now, days=days)

    @classmethod
    def from_range(
        cls,
        start: Optional[str],
        end: Optional[str],
        days: int,
        now: Optional[datetime] = None,
    ) -> "Window":
        """Explicit --start/--end range; both bounds inclusive by calendar day."""
        now = now or datetime.now(timezone.utc).astimezone()
        local_tz = datetime.now().astimezone().tzinfo
        cutoff = (
            datetime.fromisoformat(start).replace(tzinfo=local_tz)
            if start else now - timedelta(days=days)
        )
        if end:
            # end is inclusive -> extend to the end of that calendar day
            limit_upper = (
                datetime.fromisoformat(end).replace(tzinfo=local_tz)
                + timedelta(days=1)
            )
        else:
            limit_upper = _far_future()
        return cls(cutoff=cutoff, limit_upper=limit_upper, now=now, days=days)

    @classmethod
    def from_month(cls, month: str, now: Optional[datetime] = None) -> "Window":
        """A calendar month given as YYYY-MM."""
        now = now or datetime.now(timezone.utc).astimezone()
        year_s, month_s = month.split("-")[:2]
        year, mon = int(year_s), int(month_s)
        start_dt = datetime(year, mon, 1)
        end_dt = datetime(year + 1, 1, 1) if mon == 12 else datetime(year, mon + 1, 1)
        local_tz = datetime.now().astimezone().tzinfo
        return cls(
            cutoff=start_dt.replace(tzinfo=local_tz),
            limit_upper=end_dt.replace(tzinfo=local_tz),
            now=now,
        )


def default_window() -> Window:
    """The window used when parsers are called without one (library use)."""
    return Window.last_days(DEFAULT_DAYS)


# ---------------------------------------------------------------------------
# Records & aggregation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Invocation:
    """One model call's token usage, normalized across every source."""

    timestamp: datetime
    source: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation: int = 0
    cache_read: int = 0
    reasoning: int = 0
    exact: bool = True
    session_id: str = "unknown"
    source_file: str = ""

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens + self.output_tokens + self.cache_creation
                + self.cache_read + self.reasoning)

    def billing_bucket(self) -> TokenCounts:
        """This invocation's tokens keyed the way ``model_bill`` expects."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation,
            "cache_read_input_tokens": self.cache_read,
            "reasoning_tokens": self.reasoning,
        }


class UsageAggregate:
    """Accumulates invocations into per-day / per-model / total buckets."""

    def __init__(self) -> None:
        self.by_day: Dict[str, TokenCounts] = defaultdict(lambda: defaultdict(int))
        self.by_model: Dict[str, TokenCounts] = defaultdict(lambda: defaultdict(int))
        self.totals: TokenCounts = defaultdict(int)
        self.invocations: List[Invocation] = []

    def add(self, inv: Invocation) -> None:
        day = inv.timestamp.strftime("%Y-%m-%d")
        for key, value in inv.billing_bucket().items():
            self.by_day[day][key] += value
            self.by_model[inv.model][key] += value
            self.totals[key] += value
        self.invocations.append(inv)

    def __len__(self) -> int:
        return len(self.invocations)


# ---------------------------------------------------------------------------
# Shared parsing helpers
# ---------------------------------------------------------------------------

def parse_ts(rec: Any) -> Optional[datetime]:
    """Best-effort timestamp from a record's common timestamp fields."""
    if not rec or not isinstance(rec, dict):
        return None
    ts = (rec.get("timestamp") or rec.get("created_at")
          or rec.get("startTime") or rec.get("start_time"))
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except (ValueError, AttributeError):
        return None


def usage_of(rec: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract the usage dict from a Claude Code record (message- or top-level)."""
    msg = rec.get("message")
    u = msg.get("usage") if isinstance(msg, dict) else None
    if u is None:
        u = rec.get("usage")
    return u if isinstance(u, dict) else None


def model_of(rec: Mapping[str, Any]) -> str:
    msg = rec.get("message")
    if isinstance(msg, dict) and msg.get("model"):
        return msg["model"]
    return rec.get("model") or "unknown"


def fmt(n: int) -> str:
    return f"{n:,}"


def cols(d: Mapping[str, int]) -> Tuple[int, int, int, int, int]:
    """(input, output, cache_creation, cache_read, reasoning) from a bucket."""
    return (d["input_tokens"], d["output_tokens"],
            d["cache_creation_input_tokens"], d["cache_read_input_tokens"],
            d["reasoning_tokens"])


def _iter_jsonl(path: str) -> Iterator[Dict[str, Any]]:
    """Yield parsed JSON objects from a JSONL file, skipping bad lines."""
    try:
        fh = open(path, encoding="utf-8", errors="ignore")
    except OSError as exc:
        LOG.debug("cannot open %s: %s", path, exc)
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


# ---------------------------------------------------------------------------
# Source: Claude Code
# ---------------------------------------------------------------------------

@dataclass
class ClaudeScan:
    usage: UsageAggregate
    files_scanned: int = 0
    records: int = 0


def parse_claude(window: Optional[Window] = None) -> ClaudeScan:
    """Scan ~/.claude/projects JSONL transcripts (exact usage records)."""
    window = window or default_window()
    scan = ClaudeScan(usage=UsageAggregate())
    files = glob.glob(os.path.join(PROJECTS, "**", "*.jsonl"), recursive=True)
    scan.files_scanned = len(files)
    seen: set = set()

    for f in files:
        for rec in _iter_jsonl(f):
            u = usage_of(rec)
            if not u:
                continue
            ts = parse_ts(rec)
            if ts is None or not window.contains(ts):
                continue
            uid = rec.get("uuid")
            if uid is not None:
                if uid in seen:
                    continue
                seen.add(uid)
            path_parts = f.split(os.sep)
            session = (rec.get("project_name")
                       or (path_parts[-2] if len(path_parts) >= 2 else "unknown_project"))
            scan.usage.add(Invocation(
                timestamp=ts,
                source="Claude Code",
                model=model_of(rec),
                input_tokens=int(u.get("input_tokens") or 0),
                output_tokens=int(u.get("output_tokens") or 0),
                cache_creation=int(u.get("cache_creation_input_tokens") or 0),
                cache_read=int(u.get("cache_read_input_tokens") or 0),
                reasoning=int(u.get("reasoning_tokens") or 0),
                exact=True,
                session_id=session,
                source_file=os.path.basename(f),
            ))
            scan.records += 1
    return scan


# ---------------------------------------------------------------------------
# Source: Google Antigravity (loopback RPC + archived transcripts)
# ---------------------------------------------------------------------------

def _insecure_loopback_ctx() -> ssl.SSLContext:
    """TLS context for the Antigravity language server's self-signed loopback
    cert. Verification is intentionally disabled: the endpoint is 127.0.0.1
    and authenticated by the process-local CSRF token instead."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _rpc_post(
    port: int,
    token: str,
    method: str,
    payload: Mapping[str, Any],
    ctx: ssl.SSLContext,
    timeout: float,
) -> Optional[Dict[str, Any]]:
    """POST one Connect-protocol RPC to the local language server."""
    url = f"https://{RPC_HOST}:{port}/{RPC_SERVICE}/{method}"
    req = urllib.request.Request(
        url,
        data=json.dumps(dict(payload)).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "X-Codeium-Csrf-Token": token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        LOG.debug("RPC %s on port %s failed: %s", method, port, exc)
        return None


def _find_language_server_candidates() -> List[Dict[str, Any]]:
    """Locate running Antigravity language_server processes + CSRF tokens."""
    candidates: List[Dict[str, Any]] = []
    # 1. WMIC process detection (fast path; deprecated but still present)
    try:
        output = subprocess.check_output(
            "wmic process get ProcessId,CommandLine /FORMAT:CSV",
            shell=True,
        ).decode("utf-8", errors="ignore")
        for line in output.splitlines():
            line = line.strip()
            if not line or "wmic" in line:
                continue
            if "language_server" in line.lower() and "--csrf_token" in line:
                parts = line.split(",")
                if len(parts) < 3:
                    continue
                try:
                    pid = int(parts[-1].strip())
                except ValueError:
                    continue
                cmd_line = ",".join(parts[1:-1])
                token_match = re.search(r"--csrf_token\s+([a-f0-9-]+)", cmd_line)
                if token_match:
                    candidates.append({"pid": pid, "token": token_match.group(1)})
    except (OSError, subprocess.SubprocessError) as exc:
        LOG.debug("wmic process scan failed: %s", exc)

    # 2. PowerShell fallback process detection
    if not candidates:
        try:
            output = subprocess.check_output(
                'powershell -Command "Get-CimInstance Win32_Process | '
                "Where-Object { $_.CommandLine -like '*language_server*' } | "
                'Select-Object ProcessId, CommandLine | ConvertTo-Json"',
                shell=True,
            ).decode("utf-8", errors="ignore")
            if output.strip():
                data = json.loads(output)
                if isinstance(data, dict):
                    data = [data]
                for p in data:
                    pid = p.get("ProcessId")
                    cmd_line = p.get("CommandLine") or ""
                    token_match = re.search(r"--csrf_token\s+([a-f0-9-]+)", cmd_line)
                    if pid and token_match:
                        candidates.append({"pid": pid, "token": token_match.group(1)})
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            LOG.debug("powershell process scan failed: %s", exc)
    return candidates


def _listening_ports_for_pid(pid: int) -> List[int]:
    ports: List[int] = []
    try:
        output = subprocess.check_output("netstat -ano", shell=True).decode(
            "utf-8", errors="ignore")
    except (OSError, subprocess.SubprocessError) as exc:
        LOG.debug("netstat failed: %s", exc)
        return ports
    for line in output.splitlines():
        parts = line.split()
        if "LISTENING" not in line or not parts:
            continue
        if parts[-1] != str(pid):
            continue
        port_match = re.search(r":(\d+)$", parts[1])
        if port_match:
            ports.append(int(port_match.group(1)))
    return ports


def locate_antigravity_rpc() -> List[Tuple[int, str]]:
    """Find live, heartbeat-verified (port, csrf_token) RPC connections."""
    verified: List[Tuple[int, str]] = []
    ctx = _insecure_loopback_ctx()
    for cand in _find_language_server_candidates():
        for port in set(_listening_ports_for_pid(cand["pid"])):
            resp = _rpc_post(
                port, cand["token"], "Heartbeat",
                {"uuid": "00000000-0000-0000-0000-000000000000"},
                ctx, timeout=RPC_HEARTBEAT_TIMEOUT,
            )
            if resp is not None:
                verified.append((port, cand["token"]))
    return verified


def _rpc_usage_int(usage: Mapping[str, Any], *keys: str) -> int:
    """First non-empty usage field among aliases, coerced to int."""
    for key in keys:
        val = usage.get(key)
        if val:
            return int(val)
    return 0


@dataclass
class AntigravityScan:
    usage: UsageAggregate
    active_sessions: int = 0
    rpc_records: int = 0
    fallback_transcripts: int = 0
    estimated_records: int = 0
    records: int = 0
    # Earliest timestamp seen on disk (window-independent) — drives the
    # computed retention note in the report header.
    earliest_record: Optional[datetime] = None

    @property
    def sessions_scanned(self) -> int:
        return self.active_sessions + self.fallback_transcripts

    def note_ts(self, ts: Optional[datetime]) -> None:
        if ts is not None and (self.earliest_record is None
                               or ts < self.earliest_record):
            self.earliest_record = ts


def _parse_antigravity_rpc(window: Window, scan: AntigravityScan) -> set:
    """Query active cascades over RPC; returns the cascade ids seen."""
    active_ids: set = set()
    ctx = _insecure_loopback_ctx()
    for port, token in locate_antigravity_rpc():
        traj = _rpc_post(port, token, "GetAllCascadeTrajectories", {}, ctx,
                         timeout=RPC_QUERY_TIMEOUT)
        if traj is None:
            continue
        for cascade_id in traj.get("trajectorySummaries", {}):
            active_ids.add(cascade_id)
            meta = _rpc_post(
                port, token, "GetCascadeTrajectoryGeneratorMetadata",
                {"cascadeId": cascade_id}, ctx, timeout=RPC_QUERY_TIMEOUT,
            )
            if meta is None:
                continue
            for item in meta.get("generatorMetadata", []):
                chat_model = item.get("chatModel", {})
                usage = chat_model.get("usage", {})
                if not usage:
                    continue
                ts_str = chat_model.get("chatStartMetadata", {}).get("createdAt")
                ts = None
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(
                            ts_str.replace("Z", "+00:00")).astimezone()
                    except ValueError:
                        ts = None
                scan.note_ts(ts)
                if ts is None or not window.contains(ts):
                    continue
                model_id = chat_model.get("model") or usage.get("model") or "unknown"
                scan.usage.add(Invocation(
                    timestamp=ts,
                    source="Antigravity (RPC)",
                    model=resolve_model(model_id),
                    input_tokens=_rpc_usage_int(
                        usage, "inputTokens", "input_token_count",
                        "prompt_token_count", "prompt_eval_count"),
                    output_tokens=_rpc_usage_int(
                        usage, "outputTokens", "output_token_count", "eval_count"),
                    cache_creation=_rpc_usage_int(
                        usage, "cacheCreationInputTokens", "cacheWriteTokens"),
                    cache_read=_rpc_usage_int(
                        usage, "cachedContentTokenCount",
                        "cached_content_token_count", "cacheReadTokens"),
                    reasoning=_rpc_usage_int(
                        usage, "reasoning_tokens", "thinking_tokens",
                        "reasoning_output_tokens"),
                    exact=True,
                    session_id=cascade_id,
                    source_file="RPC",
                ))
                scan.rpc_records += 1
                scan.records += 1
    return active_ids


def _find_brain_transcripts() -> List[str]:
    files: List[str] = []
    for brain_dir in ANTIGRAVITY_BRAIN_DIRS:
        if not os.path.exists(brain_dir):
            continue
        for root, _dirs, filenames in os.walk(brain_dir):
            if "transcript.jsonl" in filenames:
                files.append(os.path.join(root, "transcript.jsonl"))
    return files


def _parse_antigravity_fallback(
    window: Window, scan: AntigravityScan, skip_ids: set
) -> None:
    """chars/4 estimation over archived brain transcripts.

    For each PLANNER_RESPONSE step the input is estimated from all characters
    accumulated so far (plus flat context overhead); characters already sent
    at the previous model call are attributed to cache-read.
    """
    seen: set = set()
    for f in _find_brain_transcripts():
        parts = os.path.normpath(f).split(os.sep)
        conv_id = parts[-4] if len(parts) >= 4 else "unknown_conv"
        # Skip active sessions that were already queried via RPC
        if conv_id in skip_ids:
            continue

        scan.fallback_transcripts += 1
        accumulated_chars = 0
        last_model_call_accumulated_chars = 0
        # Default fallback (most Antigravity sessions)
        current_model = ANTIGRAVITY_DEFAULT_MODEL

        for idx, rec in enumerate(_iter_jsonl(f)):
            content = rec.get("content") or ""
            switched = _model_from_settings_change(content)
            if switched is not None:
                current_model = switched

            ts = parse_ts(rec)
            scan.note_ts(ts)
            if ts is None or not window.contains(ts):
                continue

            thinking = rec.get("thinking") or ""
            tool_calls = str(rec.get("tool_calls") or "")
            step_chars = len(content) + len(thinking) + len(tool_calls)

            step_uid = f"{conv_id}_{idx}"
            if step_uid in seen:
                continue
            seen.add(step_uid)

            if rec.get("source") == "MODEL" and rec.get("type") == "PLANNER_RESPONSE":
                ot = max(1, step_chars // CHARS_PER_TOKEN)
                total_in = (accumulated_chars // CHARS_PER_TOKEN) + CONTEXT_OVERHEAD_TOKENS
                if last_model_call_accumulated_chars > 0:
                    cr = (last_model_call_accumulated_chars // CHARS_PER_TOKEN
                          + CONTEXT_OVERHEAD_TOKENS)
                    it = max(0, total_in - cr)
                else:
                    cr = 0
                    it = total_in
                last_model_call_accumulated_chars = accumulated_chars + step_chars

                scan.usage.add(Invocation(
                    timestamp=ts,
                    source="Antigravity (Fallback)",
                    model=f"{current_model}{ESTIMATED_SUFFIX}",
                    input_tokens=it,
                    output_tokens=ot,
                    cache_read=cr,
                    exact=False,
                    session_id=conv_id,
                    source_file=os.path.basename(f),
                ))
                scan.estimated_records += 1
                scan.records += 1

            accumulated_chars += step_chars


def parse_antigravity(window: Optional[Window] = None) -> AntigravityScan:
    """Hybrid Antigravity scan: exact RPC for live sessions, estimated
    fallback for archived transcripts."""
    window = window or default_window()
    scan = AntigravityScan(usage=UsageAggregate())
    active_ids = _parse_antigravity_rpc(window, scan)
    scan.active_sessions = len(active_ids)
    _parse_antigravity_fallback(window, scan, active_ids)
    return scan


# ---------------------------------------------------------------------------
# Source: OpenAI Codex
# ---------------------------------------------------------------------------

@dataclass
class CodexScan:
    usage: UsageAggregate
    sessions_scanned: int = 0
    records: int = 0


def parse_codex(window: Optional[Window] = None) -> CodexScan:
    """Granular rollout parsing: per-event token_count records from the
    rollout JSONL files referenced by ~/.codex/state_5.sqlite threads."""
    window = window or default_window()
    scan = CodexScan(usage=UsageAggregate())
    if not os.path.exists(CODEX_STATE):
        return scan

    try:
        conn = sqlite3.connect(CODEX_STATE)
        try:
            rows = conn.execute(
                "SELECT model, rollout_path FROM threads "
                "WHERE updated_at > ? AND rollout_path IS NOT NULL;",
                (int(window.cutoff.timestamp()),),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        LOG.warning("codex state db unreadable: %s", exc)
        return scan

    for model_name, rollout_path in rows:
        if not os.path.exists(rollout_path):
            continue
        scan.sessions_scanned += 1
        model = model_name or "(unknown)"
        path_parts = rollout_path.split(os.sep)
        session = path_parts[-2] if len(path_parts) >= 2 else "rollout"

        for rec in _iter_jsonl(rollout_path):
            if rec.get("type") != "event_msg":
                continue
            payload = rec.get("payload") or {}
            if payload.get("type") != "token_count":
                continue
            usage = (payload.get("info") or {}).get("last_token_usage") or {}
            if not usage:
                continue
            ts = parse_ts(rec)
            if ts is None or not window.contains(ts):
                continue
            scan.usage.add(Invocation(
                timestamp=ts,
                source="OpenAI Codex",
                model=model,
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                cache_read=int(usage.get("cached_input_tokens") or 0),
                reasoning=int(usage.get("reasoning_output_tokens") or 0),
                exact=True,
                session_id=session,
                source_file=os.path.basename(rollout_path),
            ))
            scan.records += 1
    return scan


def codex_native_total(window: Optional[Window] = None) -> Tuple[int, int]:
    """Codex's OWN lifetime counter: SUM(threads.tokens_used), windowed to the
    report range by thread updated_at. This is the figure behind Codex's
    `/usage` display — a cross-check on our granular rollout parse. Thread-level
    granularity (coarser than per-event), so treat as an order-of-magnitude
    validator, not a row-exact match. Returns (thread_count, total_tokens)."""
    window = window or default_window()
    if not os.path.exists(CODEX_STATE):
        return 0, 0
    try:
        conn = sqlite3.connect(CODEX_STATE)
        try:
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(tokens_used), 0) FROM threads "
                "WHERE updated_at > ? AND updated_at <= ?",
                (int(window.cutoff.timestamp()), int(window.limit_upper.timestamp())),
            ).fetchone()
        finally:
            conn.close()
        return int(row[0] or 0), int(row[1] or 0)
    except (sqlite3.Error, OSError, OverflowError) as exc:
        LOG.warning("codex native total unavailable: %s", exc)
        return 0, 0


# ---------------------------------------------------------------------------
# Source: Gemini CLI
# ---------------------------------------------------------------------------

def _gemini_text(content: Any) -> str:
    """Flatten a CLI message 'content' (str | list[{text}] | dict) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, dict):
                out.append(str(p.get("text") or p.get("content") or ""))
            else:
                out.append(str(p))
        return "".join(out)
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    return str(content or "")


@dataclass
class GeminiCliScan:
    usage: UsageAggregate
    files_scanned: int = 0
    records: int = 0
    exact_records: int = 0
    estimated_records: int = 0


def _load_gemini_session(path: str) -> Tuple[Dict[str, Any], Optional[datetime]]:
    """Read one chat file into {msg_id: msg} plus the session start time."""
    messages_by_id: Dict[str, Any] = {}
    session_start: Optional[datetime] = None
    if path.endswith(".json"):
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            data = json.load(fh)
        session_start = parse_ts(data)
        for m in data.get("messages", []):
            if "id" in m:
                messages_by_id[m["id"]] = m
    else:  # .jsonl, one message per line
        for rec in _iter_jsonl(path):
            if rec.get("startTime") and not session_start:
                session_start = parse_ts(rec)
            if "id" in rec:
                messages_by_id[rec["id"]] = rec
    return messages_by_id, session_start


def parse_gemini_cli(window: Optional[Window] = None) -> GeminiCliScan:
    """Hybrid Gemini CLI scan: exact when the log carries a tokens dict,
    chars/4 estimation otherwise."""
    window = window or default_window()
    scan = GeminiCliScan(usage=UsageAggregate())

    files: List[str] = []
    for g in GEMINI_CLI_CHAT_GLOBS:
        files.extend(glob.glob(g))
    files = sorted(set(files))

    cutoff_date = window.cutoff.date() - timedelta(days=1)
    limit_date = window.limit_upper.date() + timedelta(days=1)
    local_tz = datetime.now().astimezone().tzinfo

    for f in files:
        basename = os.path.basename(f)
        match = re.search(r"session-(\d{4}-\d{2}-\d{2})", basename)
        if not match:
            continue
        try:
            file_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if not (cutoff_date <= file_date <= limit_date):
            continue

        try:
            messages_by_id, session_start = _load_gemini_session(f)
        except (OSError, ValueError) as exc:
            LOG.debug("skipping gemini chat %s: %s", f, exc)
            continue
        if not messages_by_id:
            continue
        if not session_start:
            session_start = datetime.combine(
                file_date, datetime.min.time()).replace(tzinfo=local_tz)

        def _msg_time(item: Tuple[str, Any]) -> float:
            ts = parse_ts(item[1])
            return ts.timestamp() if ts else 0

        sorted_messages = [m for _id, m in sorted(messages_by_id.items(), key=_msg_time)]

        session_chars = 0
        previous_session_chars = 0
        scan.files_scanned += 1

        # Propagate model selection within session
        current_model = GEMINI_CLI_DEFAULT_MODEL
        for msg in sorted_messages:
            m_id = msg.get("model")
            if m_id and m_id != "unknown":
                current_model = m_id
                break

        path_parts = os.path.normpath(f).split(os.sep)
        proj_name = path_parts[-3] if len(path_parts) >= 3 else "unknown_project"

        for msg in sorted_messages:
            role = (msg.get("type") or msg.get("role") or "").lower()
            if role in ("user", "human"):
                text = _gemini_text(
                    msg.get("content") if msg.get("content") is not None
                    else msg.get("text"))
                session_chars += len(text)
                continue
            if role not in ("gemini", "model", "assistant"):
                continue

            ts = parse_ts(msg) or session_start
            text = _gemini_text(
                msg.get("content") if msg.get("content") is not None
                else msg.get("text"))
            step_chars = len(text)

            if not window.contains(ts):
                session_chars += step_chars + len(str(msg.get("thoughts") or ""))
                continue

            model_id = msg.get("model")
            if not model_id or model_id == "unknown":
                model_name = resolve_model(current_model)
            else:
                model_name = resolve_model(model_id)
                current_model = model_id

            tokens = msg.get("tokens")
            it = ot = cr = rt = 0
            exact = False
            if tokens and isinstance(tokens, dict):
                it = int(tokens.get("input") or 0)
                ot = int(tokens.get("output") or 0)
                cr = int(tokens.get("cached") or 0)
                rt = int(tokens.get("thoughts") or 0)
                exact = it > 0 or ot > 0

            if exact:
                scan.exact_records += 1
            else:
                scan.estimated_records += 1
                total_in = (session_chars // CHARS_PER_TOKEN) + CONTEXT_OVERHEAD_TOKENS
                if previous_session_chars > 0:
                    cr = (previous_session_chars // CHARS_PER_TOKEN
                          + CONTEXT_OVERHEAD_TOKENS)
                    it = max(0, total_in - cr)
                else:
                    cr = 0
                    it = total_in
                ot = max(1, step_chars // CHARS_PER_TOKEN)
                rt = len(str(msg.get("thoughts") or "")) // CHARS_PER_TOKEN

            scan.usage.add(Invocation(
                timestamp=ts,
                source="Gemini CLI",
                model=model_name if exact else f"{model_name}{ESTIMATED_SUFFIX}",
                input_tokens=it,
                output_tokens=ot,
                cache_read=cr,
                reasoning=rt,
                exact=exact,
                session_id=proj_name,
                source_file=basename,
            ))
            scan.records += 1
            previous_session_chars = session_chars
            session_chars += step_chars + len(str(msg.get("thoughts") or ""))

    return scan


# ---------------------------------------------------------------------------
# Source: Fleet usage ledger
# ---------------------------------------------------------------------------

@dataclass
class FleetScan:
    usage: UsageAggregate
    records: int = 0


def parse_fleet_usage(window: Optional[Window] = None) -> FleetScan:
    """Forward, EXACT accounting for local Ollama/Gemma, OpenRouter and HF
    lanes. Append-only JSONL written by fleet/fleet_usage_proxy.py; each line:
    {timestamp, provider, lane, model, input_tokens, output_tokens,
    cached_tokens, reasoning_tokens, source} — exact counts from each lane's
    own API response (ollama prompt_eval_count/eval_count; OpenRouter usage)."""
    window = window or default_window()
    scan = FleetScan(usage=UsageAggregate())
    if not os.path.exists(FLEET_USAGE_LEDGER):
        return scan

    for rec in _iter_jsonl(FLEET_USAGE_LEDGER):
        ts = parse_ts(rec)
        if ts is None or not window.contains(ts):
            continue
        scan.usage.add(Invocation(
            timestamp=ts,
            source=f"Fleet:{rec.get('provider') or 'Fleet'}",
            model=rec.get("model") or "unknown",
            input_tokens=int(rec.get("input_tokens") or 0),
            output_tokens=int(rec.get("output_tokens") or 0),
            cache_read=int(rec.get("cached_tokens") or 0),
            reasoning=int(rec.get("reasoning_tokens") or 0),
            exact=True,
            session_id=rec.get("lane") or "fleet",
            source_file=os.path.basename(FLEET_USAGE_LEDGER),
        ))
        scan.records += 1
    return scan


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

DAY_TABLE_HEADER = (
    f"{'Day':<12}{'input':>14}{'output':>14}{'cache-read':>16}{'reasoning':>14}"
)
SEP_70 = "=" * 70


def print_day_table(by_day: Mapping[str, TokenCounts], totals: TokenCounts) -> None:
    print(DAY_TABLE_HEADER)
    print("-" * len(DAY_TABLE_HEADER))
    for day in sorted(by_day):
        i, o, _cc, cr, rt = cols(by_day[day])
        print(f"{day:<12}{fmt(i):>14}{fmt(o):>14}{fmt(cr):>16}{fmt(rt):>14}")
    print("-" * len(DAY_TABLE_HEADER))
    ti, to, _tcc, tcr, trt = cols(totals)
    print(f"{'TOTAL':<12}{fmt(ti):>14}{fmt(to):>14}{fmt(tcr):>16}{fmt(trt):>14}")


def compute_and_print_split(invocations: Sequence[Invocation], title: str) -> None:
    """Exact vs estimated token/cost split for a set of invocations."""
    exact_tok = est_tok = 0
    exact_cost = est_cost = 0.0
    for inv in invocations:
        cost, _src = model_bill(inv.model, inv.billing_bucket())
        if inv.exact:
            exact_tok += inv.total_tokens
            exact_cost += cost
        else:
            est_tok += inv.total_tokens
            est_cost += cost

    blended_tok = exact_tok + est_tok
    confidence = (exact_tok / blended_tok * 100) if blended_tok > 0 else 100.0

    print(f"--- Exact vs Estimated Split ({title}) ---")
    print(f"Exact tokens:                       {fmt(exact_tok)}")
    print(f"Estimated tokens:                   {fmt(est_tok)}")
    print(f"Blended total:                      {fmt(blended_tok)}")
    print(f"Confidence:                         {confidence:.1f}%")
    print()
    print(f"Exact shadow cost:                  ${exact_cost:,.2f}")
    print(f"Estimated shadow cost:              ${est_cost:,.2f}")
    print(f"Blended shadow cost:                ${exact_cost + est_cost:,.2f}")
    print("----------------------------------------------------------------------\n")


def get_cache_label(share: float) -> str:
    if share >= CACHE_SHARE_EXCELLENT:
        return "Excellent"
    if share >= CACHE_SHARE_GOOD:
        return "Good"
    if share >= CACHE_SHARE_WARNING:
        return "Warning"
    return "Cold context leak"


def _cache_warnings(fresh_input: int, share: float) -> List[str]:
    reasons = []
    if fresh_input > INPUT_WARN_THRESHOLD:
        reasons.append(f"input_tokens > {INPUT_WARN_THRESHOLD:,} ({fmt(fresh_input)})")
    if share < CACHE_SHARE_GOOD:
        reasons.append(f"cache_share < {CACHE_SHARE_GOOD:.0%} ({share * 100:.1f}%)")
    return reasons


def print_cache_health_report(invocations: Sequence[Invocation]) -> None:
    print("======================================================================")
    print("CACHE HEALTH SCORE REPORT")
    print("======================================================================")

    # 1. By Model
    print("--- By Model Cache Health ---")
    model_stats: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"input": 0, "total": 0, "cache_read": 0})
    for inv in invocations:
        stats = model_stats[inv.model]
        stats["input"] += inv.input_tokens
        stats["total"] += inv.total_tokens
        stats["cache_read"] += inv.cache_read

    for m, stats in sorted(model_stats.items()):
        total = stats["total"]
        if total == 0:
            continue
        share = stats["cache_read"] / total
        print(f"  Model: {m:<30} | Share: {share * 100:5.1f}% | "
              f"Label: {get_cache_label(share):<20}")
        reasons = _cache_warnings(stats["input"], share)
        if reasons:
            print(f"    [WARNING] {', '.join(reasons)}")

    # 2. By Session (top TOP_SESSIONS_COUNT)
    print(f"\n--- Top {TOP_SESSIONS_COUNT} Sessions Cache Health ---")
    session_stats: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"input": 0, "total": 0, "cache_read": 0, "source": ""})
    for inv in invocations:
        stats = session_stats[inv.session_id]
        stats["input"] += inv.input_tokens
        stats["total"] += inv.total_tokens
        stats["cache_read"] += inv.cache_read
        stats["source"] = inv.source

    for s, stats in sorted(session_stats.items(),
                           key=lambda x: -x[1]["total"])[:TOP_SESSIONS_COUNT]:
        total = stats["total"]
        if total == 0:
            continue
        share = stats["cache_read"] / total
        print(f"  Session: {s:<30} ({stats['source']:<15}) | "
              f"Share: {share * 100:5.1f}% | Label: {get_cache_label(share):<20}")
        reasons = _cache_warnings(stats["input"], share)
        if reasons:
            print(f"    [WARNING] {', '.join(reasons)}")
    print()


def get_model_bucket(model_name: str) -> str:
    """Classify a model into a spend class for the bucket report."""
    m = model_name.replace(ESTIMATED_SUFFIX, "").strip().lower()
    if "opus" in m:
        return "premium_reasoning" if "thinking" in m else "premium_cloud"
    if "thinking" in m:
        return "premium_reasoning"
    if "pro" in m or "flash-high" in m:
        return "standard_cloud"
    if "flash-low" in m:
        return "cheap_cloud"
    if "flash" in m or "haiku" in m or "lite" in m:
        return "cheap_cloud"
    if "gpt-5.5" in m or "gpt-5.4" in m:
        return "cheap_cloud" if "mini" in m else "premium_cloud"
    if "gpt-oss" in m:
        return "cheap_or_local"
    if "gemma" in m or "local" in m or "ollama" in m:
        return "local_free"

    pricing = price_for(model_name)
    if pricing.input >= 5.0:
        return "premium_cloud"
    if pricing.input >= 1.5:
        return "standard_cloud"
    if pricing.input > 0.0:
        return "cheap_cloud"
    return "local_free"


_BUCKET_DISPLAY = {
    "premium_cloud": "Premium cloud usage",
    "premium_reasoning": "Premium cloud usage",
    "standard_cloud": "Standard cloud usage",
    "cheap_cloud": "Cheap cloud usage",
    "cheap_or_local": "Cheap cloud usage",
    "local_free": "Local/free usage",
}


def print_model_class_buckets(invocations: Sequence[Invocation]) -> None:
    buckets: Dict[str, Dict[str, float]] = {
        "Premium cloud usage": {"tokens": 0, "cost": 0.0},
        "Standard cloud usage": {"tokens": 0, "cost": 0.0},
        "Cheap cloud usage": {"tokens": 0, "cost": 0.0},
        "Local/free usage": {"tokens": 0, "cost": 0.0},
    }
    for inv in invocations:
        cost, _src = model_bill(inv.model, inv.billing_bucket())
        name = _BUCKET_DISPLAY.get(get_model_bucket(inv.model), "Local/free usage")
        buckets[name]["tokens"] += inv.total_tokens
        buckets[name]["cost"] += cost

    print("======================================================================")
    print("MODEL CLASS BUCKETS REPORT")
    print("======================================================================")
    for name, data in buckets.items():
        print(f"{name:<25} total {fmt(int(data['tokens'])):>16} tokens   "
              f"${data['cost']:,.2f}")
    print()


def print_top_hogs(invocations: Sequence[Invocation],
                   top_n: Optional[int] = None) -> None:
    top_n = TOP_HOGS_COUNT if top_n is None else top_n
    print("======================================================================")
    print(f"Top {top_n} Token Hogs")
    print("======================================================================")
    for idx, inv in enumerate(
            sorted(invocations, key=lambda x: -x.input_tokens)[:top_n], 1):
        ts_str = inv.timestamp.strftime("%Y-%m-%d %H:%M:%S") if inv.timestamp else "unknown"
        exact_str = "exact" if inv.exact else "estimated"
        print(f"{idx:2d}. [{ts_str}] {inv.source} | {inv.model} | {exact_str}")
        print(f"    input: {fmt(inv.input_tokens):<12} | output: {fmt(inv.output_tokens):<12} "
              f"| cache-read: {fmt(inv.cache_read):<12} | total: {fmt(inv.total_tokens)}")
        print(f"    session/transcript path: {inv.session_id}")
    print()


# Substring -> display label, first match wins. Config overlay:
# {"company_rules": [["needle", "label"], ...]} is tried before the built-ins,
# and {"fleet_provider_rules": [...]} likewise for Fleet:<provider> sources.
_BUILTIN_COMPANY_RULES: Tuple[Tuple[str, str], ...] = (
    ("Claude Code", "Anthropic (Claude Code)"),
    ("Gemini CLI", "Google (Gemini CLI)"),
    ("Antigravity", "Google (Antigravity)"),
    ("Codex", "OpenAI (Codex)"),
    ("OpenAI", "OpenAI (Codex)"),
)
_BUILTIN_FLEET_RULES: Tuple[Tuple[str, str], ...] = (
    ("Ollama", "Local (Ollama/Gemma)"),
    ("OpenRouter", "OpenRouter (free)"),
    ("HF", "HuggingFace (free)"),
    ("HuggingFace", "HuggingFace (free)"),
)


def _config_label_rules(key: str) -> Tuple[Tuple[str, str], ...]:
    rules: List[Tuple[str, str]] = []
    for entry in CONFIG.get(key) or []:
        try:
            needle, label = entry
            rules.append((str(needle), str(label)))
        except (TypeError, ValueError):
            LOG.warning("skipping bad %s entry: %r", key, entry)
    return tuple(rules)


_COMPANY_RULES = _config_label_rules("company_rules") + _BUILTIN_COMPANY_RULES
_FLEET_RULES = _config_label_rules("fleet_provider_rules") + _BUILTIN_FLEET_RULES


def _company_of(source: str) -> str:
    """Provider/company display name for an invocation source tag."""
    if source.startswith("Fleet:"):
        prov = source.split(":", 1)[1]
        for needle, label in _FLEET_RULES:
            if needle in prov:
                return label
        return f"Fleet ({prov})"
    for needle, label in _COMPANY_RULES:
        if needle in source:
            return label
    return "Unknown"


def print_route_recommendations(invocations: Sequence[Invocation]) -> None:
    """Routing recommendations computed from this window's measured data:
    lane mix, premium-class cost share, per-company cache health, and the
    single (company, day) with the highest fresh-input volume."""
    print("======================================================================")
    print("Recommended routing changes:")
    print("======================================================================")
    recs: List[str] = []

    # Lane mix: is anything running on the local/free lane at all?
    bucket_tokens: Dict[str, int] = defaultdict(int)
    bucket_cost: Dict[str, float] = defaultdict(float)
    for inv in invocations:
        name = _BUCKET_DISPLAY.get(get_model_bucket(inv.model), "Local/free usage")
        cost, _src = model_bill(inv.model, inv.billing_bucket())
        bucket_tokens[name] += inv.total_tokens
        bucket_cost[name] += cost
    total_cost = sum(bucket_cost.values())

    if invocations and bucket_tokens.get("Local/free usage", 0) == 0:
        recs.append("- Route repeated scans/summarization to local/free lanes "
                    "(0 local/free tokens this window).")

    premium_cost = bucket_cost.get("Premium cloud usage", 0.0)
    if total_cost > 0 and premium_cost / total_cost >= 0.5:
        recs.append(f"- Reserve premium models for final synthesis and "
                    f"arbitration ({premium_cost / total_cost:.0%} of shadow "
                    f"cost is premium-class).")

    # Per-company cache health: flag companies below the 'Good' share.
    company_cache: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    for inv in invocations:
        stats = company_cache[_company_of(inv.source)]
        stats[0] += inv.cache_read
        stats[1] += inv.total_tokens
    for company in sorted(company_cache):
        cr, tot = company_cache[company]
        if tot and (cr / tot) < CACHE_SHARE_GOOD:
            recs.append(f"- Compress {company} context before replaying long "
                        f"sessions (cache share {cr / tot:.0%} < "
                        f"{CACHE_SHARE_GOOD:.0%}).")

    # The single largest fresh-input day per company.
    spikes: Dict[Tuple[str, str], int] = defaultdict(int)
    for inv in invocations:
        if inv.timestamp is None:
            continue
        key = (_company_of(inv.source), inv.timestamp.strftime("%Y-%m-%d"))
        spikes[key] += inv.input_tokens
    if spikes:
        (company, day), volume = max(spikes.items(), key=lambda kv: kv[1])
        if volume > INPUT_WARN_THRESHOLD:
            recs.append(f"- Investigate {day} {company} input spike "
                        f"({fmt(volume)} fresh input tokens in one day).")

    if not recs:
        recs.append("- No routing changes recommended: lane mix and cache "
                    "health look sound this window.")
    for rec in recs:
        print(rec)
    print("======================================================================")


def merge_by_model(scans: Iterable[UsageAggregate]) -> Dict[str, TokenCounts]:
    """Union of per-model buckets across sources, with model names sanitized."""
    all_models: Dict[str, TokenCounts] = defaultdict(lambda: defaultdict(int))
    for agg in scans:
        for model, bucket in agg.by_model.items():
            key = model or "(unknown)"
            for k, v in bucket.items():
                all_models[key][k] += v
    return all_models


def print_model_breakdown_and_bill(all_models: Mapping[str, TokenCounts]) -> None:
    """Per-model totals plus the honest API-equivalent shadow bill.

    The bill shows what these tokens WOULD have cost at first-party API list
    prices, with cache-reads priced as cache-reads (not as fresh input) and
    reasoning as output. This is a hypothetical reference point, NOT money
    saved: on flat-rate subscriptions this volume was never going to be bought
    at API rates. Naive (all-tokens x flat-rate) math overstates this
    several-fold because cache-reads dominate the token count but bill at ~10%
    of input. Set AI_MONTHLY_SUBSCRIPTION_USD to print real spend alongside."""
    print("--- By Model Breakdown ---")
    mw = max(len(str(m)) for m in all_models)
    grand_total_tokens = 0
    grand_total_cost = 0.0
    grand_total_cold = 0.0
    total_cache_reads = 0
    cache_read_billed = 0.0
    cache_read_at_input = 0.0
    used_estimate = False

    for model in sorted(all_models, key=lambda m: -sum(all_models[m].values())):
        i, o, cc, cr, rt = cols(all_models[model])
        total = i + o + cc + cr + rt
        grand_total_tokens += total
        total_cache_reads += cr
        cost, src = model_bill(model, all_models[model])
        grand_total_cost += cost
        # Cold-boot: every input-side token (input + cache-creation + cache-read)
        # charged at full fresh-input rate; reasoning billed as output.
        pricing = price_for(model)
        grand_total_cold += ((i + cc + cr) * pricing.input
                             + (o + rt) * pricing.output) / 1_000_000
        cache_read_billed += cr * pricing.cache_read
        cache_read_at_input += cr * pricing.input
        if src == EST:
            used_estimate = True
        tag = "~" if src == EST else " "
        print(f"  {model:<{mw}}  total {fmt(total):>16}   "
              f"(in {fmt(i + cc)}, out {fmt(o)}, cache-read {fmt(cr)}, "
              f"reasoning {fmt(rt)})  {tag}${cost:,.2f}")

    sub_cost = float(os.environ.get("AI_MONTHLY_SUBSCRIPTION_USD", "0") or 0)
    cache_share = (total_cache_reads / grand_total_tokens * 100) if grand_total_tokens else 0

    print("\n======================================================================")
    print("API-EQUIVALENT SHADOW BILL  (hypothetical reference, NOT realized savings)")
    print("======================================================================")
    print(f"Realistic cost (cache-reads billed as cache): ${grand_total_cost:,.2f}")
    print(f"COLD-BOOT cost (no cache, every token fresh): ${grand_total_cold:,.2f}")
    print(f"What caching saved vs cold-boot:              "
          f"${grand_total_cold - grand_total_cost:,.2f}")
    if used_estimate:
        print("  ~ = model priced from an estimate, not a first-party doc")
    # Measured blended discount: what this window's cache-reads billed at,
    # as a share of what the same tokens would cost at fresh-input rates.
    billed_ratio = (cache_read_billed / cache_read_at_input
                    if cache_read_at_input else 0.0)
    print(f"Cache-reads as share of all tokens:         {cache_share:.1f}%  "
          f"(billed ~{billed_ratio:.0%} of input - why naive math inflates)")
    if sub_cost > 0:
        print(f"Your actual subscription spend:             ${sub_cost:,.2f}")
    print("----------------------------------------------------------------------")
    print("This is the price you DIDN'T pay by using flat-rate plans, not a sum")
    print("you earned. Treat it as a usage gauge, not a savings account.")
    print("======================================================================\n")


# Display order for the by-provider summary; config key: "provider_order".
_PROVIDER_ORDER: List[str] = [str(p) for p in CONFIG.get("provider_order") or (
    "Anthropic (Claude Code)", "Google (Antigravity)", "Google (Gemini CLI)",
    "OpenAI (Codex)", "Local (Ollama/Gemma)", "OpenRouter (free)",
    "HuggingFace (free)",
)]


def print_by_provider_summary(invocations: Sequence[Invocation]) -> None:
    provider_stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "input_tokens": 0, "output_tokens": 0, "cache_creation": 0,
        "cache_read": 0, "reasoning": 0, "cost": 0.0, "count": 0,
    })
    for inv in invocations:
        cost, _src = model_bill(inv.model, inv.billing_bucket())
        stats = provider_stats[_company_of(inv.source)]
        stats["input_tokens"] += inv.input_tokens
        stats["output_tokens"] += inv.output_tokens
        stats["cache_creation"] += inv.cache_creation
        stats["cache_read"] += inv.cache_read
        stats["reasoning"] += inv.reasoning
        stats["cost"] += cost
        stats["count"] += 1

    print("======================================================================")
    print("BY-PROVIDER SUMMARY REPORT")
    print("======================================================================")
    hdr = (f"{'Provider/Company':<30}{'Invocations':>12}{'Input (w/CC)':>16}"
           f"{'Output':>14}{'Cache Read':>16}{'Reasoning':>14}{'Cost':>12}")
    print(hdr)
    print("-" * len(hdr))

    totals = {"inv": 0, "in": 0, "out": 0, "cr": 0, "rt": 0, "cost": 0.0}
    ordered = _PROVIDER_ORDER + [c for c in provider_stats if c not in _PROVIDER_ORDER]
    for company in ordered:
        if company not in provider_stats:
            continue
        stats = provider_stats[company]
        it = stats["input_tokens"] + stats["cache_creation"]
        totals["inv"] += stats["count"]
        totals["in"] += it
        totals["out"] += stats["output_tokens"]
        totals["cr"] += stats["cache_read"]
        totals["rt"] += stats["reasoning"]
        totals["cost"] += stats["cost"]
        print(f"{company:<30}{fmt(stats['count']):>12}{fmt(it):>16}"
              f"{fmt(stats['output_tokens']):>14}{fmt(stats['cache_read']):>16}"
              f"{fmt(stats['reasoning']):>14}  ${stats['cost']:10.2f}")

    print("-" * len(hdr))
    print(f"{'TOTAL':<30}{fmt(totals['inv']):>12}{fmt(totals['in']):>16}"
          f"{fmt(totals['out']):>14}{fmt(totals['cr']):>16}"
          f"{fmt(totals['rt']):>14}  ${totals['cost']:10.2f}")
    print("======================================================================\n")


def print_period_comparison(
    invocations: Sequence[Invocation], now: datetime, n: int
) -> None:
    """--compare N: last N days vs the prior N days, per company."""
    mid = now - timedelta(days=n)
    lo = now - timedelta(days=n * 2)

    def company(src: str) -> str:
        return (src or "unknown").split(" (")[0].split(":")[0].strip()

    cur: Dict[str, int] = defaultdict(int)
    prev: Dict[str, int] = defaultdict(int)
    for inv in invocations:
        if inv.timestamp is None:
            continue
        if inv.timestamp >= mid:
            cur[company(inv.source)] += inv.total_tokens
        elif inv.timestamp >= lo:
            prev[company(inv.source)] += inv.total_tokens

    companies = sorted(set(cur) | set(prev), key=lambda c: -(cur[c] + prev[c]))
    sep = "-" * 70
    print()
    print(SEP_70)
    print(f"Period comparison — last {n}d vs prior {n}d (total tokens, by company)")
    print(f"  last {n}d : {mid:%Y-%m-%d} -> {now:%Y-%m-%d}")
    print(f"  prior {n}d: {lo:%Y-%m-%d} -> {mid:%Y-%m-%d}")
    print(SEP_70)
    print(f"{'Company':<22}{'prior ' + str(n) + 'd':>16}{'last ' + str(n) + 'd':>16}{'delta':>12}")
    print(sep)
    total_cur = total_prev = 0
    for c in companies:
        p, cu = prev[c], cur[c]
        total_cur += cu
        total_prev += p
        d = f"{(cu - p) / p * 100:+.0f}%" if p else ("new" if cu else "-")
        print(f"{c:<22}{p:>16,}{cu:>16,}{d:>12}")
    print(sep)
    dt = f"{(total_cur - total_prev) / total_prev * 100:+.0f}%" if total_prev else "-"
    print(f"{'TOTAL':<22}{total_prev:>16,}{total_cur:>16,}{dt:>12}")
    print(SEP_70)


def print_analytics_dashboard(
    invocations: Sequence[Invocation], now: datetime
) -> None:
    """--lanes: period comparisons (24h / week / month / quarter / half / year,
    each "to-date" vs the same elapsed offset into the prior period) plus
    records (highest day/week/month + longest & current streak). One pass
    builds a per-day series; every lane is date math on it."""

    def humanize(n: float) -> str:
        n = int(n)
        if n == 0:
            return "0"
        for unit, val in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
            if abs(n) >= val:
                return f"{n / val:.2f}{unit}"
        return str(n)

    def delta(this: int, last: int) -> str:
        if last == 0:
            return "new" if this > 0 else "-"
        return f"{(this - last) / last:+.0%}"

    today = now.date()
    day_sums: Dict[dtm.date, int] = defaultdict(int)
    records: List[Tuple[datetime, int]] = []
    for inv in invocations:
        if inv.timestamp is None:
            continue
        day_sums[inv.timestamp.date()] += inv.total_tokens
        records.append((inv.timestamp, inv.total_tokens))

    if not records:
        print("no data")
        return

    def dsum(lo: dtm.date, hi: dtm.date) -> int:
        """Inclusive day-range sum over the per-day series."""
        total = 0
        cur = lo
        while cur <= hi:
            total += day_sums.get(cur, 0)
            cur += dtm.timedelta(days=1)
        return total

    lanes: List[Tuple[str, int, int]] = []
    # 24h rolling (timestamp precision)
    lanes.append((
        "this 24h / prior 24h",
        sum(t for ts, t in records if now - dtm.timedelta(hours=24) <= ts < now),
        sum(t for ts, t in records
            if now - dtm.timedelta(hours=48) <= ts < now - dtm.timedelta(hours=24)),
    ))
    # week-to-date (Mon start)
    ws = today - dtm.timedelta(days=today.weekday())
    off = (today - ws).days
    lanes.append(("this week / last week", dsum(ws, today),
                  dsum(ws - dtm.timedelta(days=7), ws - dtm.timedelta(days=7 - off))))
    # month-to-date
    ms = today.replace(day=1)
    off = (today - ms).days
    lme = ms - dtm.timedelta(days=1)
    lms = lme.replace(day=1)
    lanes.append(("this month / last month", dsum(ms, today),
                  dsum(lms, min(lms + dtm.timedelta(days=off), lme))))
    # quarter-to-date (3mo)
    qm = ((today.month - 1) // 3) * 3 + 1
    qs = dtm.date(today.year, qm, 1)
    off = (today - qs).days
    pqs = dtm.date(today.year if qm > 1 else today.year - 1,
                   qm - 3 if qm > 1 else 10, 1)
    lanes.append(("this quarter / last (3mo)", dsum(qs, today),
                  dsum(pqs, min(pqs + dtm.timedelta(days=off),
                                qs - dtm.timedelta(days=1)))))
    # half-to-date (6mo)
    hm = 1 if today.month <= 6 else 7
    hs = dtm.date(today.year, hm, 1)
    off = (today - hs).days
    phs = dtm.date(today.year if hm > 1 else today.year - 1,
                   hm - 6 if hm > 1 else 7, 1)
    lanes.append(("this half / last (6mo)", dsum(hs, today),
                  dsum(phs, min(phs + dtm.timedelta(days=off),
                                hs - dtm.timedelta(days=1)))))
    # year-to-date
    ys = dtm.date(today.year, 1, 1)
    off = (today - ys).days
    lys = dtm.date(today.year - 1, 1, 1)
    lanes.append(("this year / last year", dsum(ys, today),
                  dsum(lys, min(lys + dtm.timedelta(days=off),
                                dtm.date(today.year - 1, 12, 31)))))

    print(f"{'Lane':<28}{'this':>14}{'prior':>14}{'delta':>9}")
    print("-" * 65)
    for name, t, l in lanes:
        print(f"{name:<28}{humanize(t):>14}{humanize(l):>14}{delta(t, l):>9}")

    print()
    print("Records (full history)")
    print("-" * 65)
    md = max(day_sums, key=lambda d: day_sums[d])
    print(f"  Highest Day:    {md} ({humanize(day_sums[md])})")
    weeks: Dict[Tuple[int, int], int] = defaultdict(int)
    months: Dict[Tuple[int, int], int] = defaultdict(int)
    for d, v in day_sums.items():
        weeks[d.isocalendar()[:2]] += v
        months[(d.year, d.month)] += v
    mw = max(weeks, key=lambda k: weeks[k])
    print(f"  Highest Week:   {mw[0]}-W{mw[1]:02d} ({humanize(weeks[mw])})")
    mm = max(months, key=lambda k: months[k])
    print(f"  Highest Month:  {mm[0]}-{mm[1]:02d} ({humanize(months[mm])})")

    dates = sorted(day_sums)
    best = cur = 0
    bstart = bend = tstart = dates[0]
    cd = dates[0]
    while cd <= dates[-1]:
        if day_sums.get(cd, 0) > 0:
            if cur == 0:
                tstart = cd
            cur += 1
            if cur > best:
                best, bstart, bend = cur, tstart, cd
        else:
            cur = 0
        cd += dtm.timedelta(days=1)
    cstreak = 0
    cd = today if day_sums.get(today, 0) > 0 else today - dtm.timedelta(days=1)
    while day_sums.get(cd, 0) > 0:
        cstreak += 1
        cd -= dtm.timedelta(days=1)
    print(f"  Longest Streak: {best} days ({bstart}..{bend})")
    print(f"  Current Streak: {cstreak} days")


# =============================================================================
# C4AI Token Counter Integration (vendored minimal core)
# Source: https://github.com/C4AI/token-counter
# Attribution: C4AI token-counter (MIT). Folded here so token_tracker.py
# stays a single-file import with no external package required for basic use.
# Counts tokenizer tokens in local files or HuggingFace datasets, producing
# distribution stats (total, mean, median, IQR, P95, P99, stddev). Uses
# HuggingFace tokenizers (default: Qwen/Qwen3-1.7B-Base), Parquet/JSONL/text.
# =============================================================================

DEFAULT_TC_MODEL = _cfg("tc_tokenizer_model", "Qwen/Qwen3-1.7B-Base")
DEFAULT_TC_BATCH_SIZE = _cfg("tc_batch_size", 256, int)


@dataclass
class TokenCountStats:
    """Accumulates per-document token counts and derives distribution stats.

    Mirrors the public contract of C4AI token_counter.reporting.TokenCountStats
    so that callers can swap in the full library when available.
    """

    total_tokens: int = 0
    documents_processed: int = 0
    rows_seen: int = 0
    null_field_rows: int = 0
    empty_text_rows: int = 0
    non_string_rows_coerced: int = 0
    # Distribution tracking (sorted list for percentile computation)
    _lengths: List[int] = field(default_factory=list, repr=False)
    started_at_epoch: Optional[float] = None
    completed_at_epoch: Optional[float] = None
    wall_time: float = 0.0

    def observe_document(self, *, text: str, token_length: int) -> None:
        self.total_tokens += token_length
        self.documents_processed += 1
        bisect.insort(self._lengths, token_length)

    @property
    def mean_tokens(self) -> Optional[float]:
        if not self._lengths:
            return None
        return self.total_tokens / len(self._lengths)

    def _percentile(self, p: float) -> Optional[float]:
        n = len(self._lengths)
        if n == 0:
            return None
        idx = p * (n - 1)
        lo, hi = int(idx), min(int(idx) + 1, n - 1)
        frac = idx - lo
        return self._lengths[lo] + frac * (self._lengths[hi] - self._lengths[lo])

    @property
    def median_tokens(self) -> Optional[float]:
        return self._percentile(0.50)

    @property
    def p95_tokens(self) -> Optional[float]:
        return self._percentile(0.95)

    @property
    def p99_tokens(self) -> Optional[float]:
        return self._percentile(0.99)

    @property
    def stddev_tokens(self) -> Optional[float]:
        n = len(self._lengths)
        if n < 2:
            return None
        mu = self.total_tokens / n
        return math.sqrt(sum((x - mu) ** 2 for x in self._lengths) / (n - 1))

    @property
    def iqr_tokens(self) -> Optional[float]:
        p25 = self._percentile(0.25)
        p75 = self._percentile(0.75)
        if p25 is None or p75 is None:
            return None
        return p75 - p25

    def distribution_summary(self) -> Dict[str, Any]:
        return {
            "total_tokens": self.total_tokens,
            "documents_processed": self.documents_processed,
            "mean": self.mean_tokens,
            "median": self.median_tokens,
            "iqr": self.iqr_tokens,
            "p95": self.p95_tokens,
            "p99": self.p99_tokens,
            "stddev": self.stddev_tokens,
        }

    def to_checkpoint_state(self) -> Dict[str, Any]:
        return {
            "total_tokens": self.total_tokens,
            "documents_processed": self.documents_processed,
            "rows_seen": self.rows_seen,
            "null_field_rows": self.null_field_rows,
            "empty_text_rows": self.empty_text_rows,
            "non_string_rows_coerced": self.non_string_rows_coerced,
            "lengths": self._lengths,
            "started_at_epoch": self.started_at_epoch,
            "completed_at_epoch": self.completed_at_epoch,
            "wall_time": self.wall_time,
        }

    @classmethod
    def from_checkpoint_state(cls, state: Mapping[str, Any]) -> "TokenCountStats":
        obj = cls()
        obj.total_tokens = state.get("total_tokens", 0)
        obj.documents_processed = state.get("documents_processed", 0)
        obj.rows_seen = state.get("rows_seen", 0)
        obj.null_field_rows = state.get("null_field_rows", 0)
        obj.empty_text_rows = state.get("empty_text_rows", 0)
        obj.non_string_rows_coerced = state.get("non_string_rows_coerced", 0)
        obj._lengths = list(state.get("lengths", []))
        obj.started_at_epoch = state.get("started_at_epoch")
        obj.completed_at_epoch = state.get("completed_at_epoch")
        obj.wall_time = state.get("wall_time", 0.0)
        return obj


def _tc_token_lengths(tokenizer: Any, texts: List[str]) -> List[int]:
    """Return per-text token lengths using the given HuggingFace tokenizer."""
    if not texts:
        return []
    try:
        encoded = tokenizer(texts, add_special_tokens=False,
                            return_attention_mask=False)
        input_ids = (encoded["input_ids"] if isinstance(encoded, dict)
                     else encoded.input_ids)
        return [len(ids) for ids in input_ids]
    except Exception:  # tokenizer implementations vary; fall back per-text
        return [len(tokenizer.encode(t, add_special_tokens=False)) for t in texts]


def _tc_load_tokenizer(model: str, *, trust_remote_code: bool = False) -> Any:
    """Load a HuggingFace AutoTokenizer. Raises ImportError if transformers absent."""
    from transformers import AutoTokenizer  # type: ignore
    return AutoTokenizer.from_pretrained(model, trust_remote_code=trust_remote_code)


def count_tokens_in_texts(
    texts: Iterable[str],
    *,
    model: str = DEFAULT_TC_MODEL,
    batch_size: int = DEFAULT_TC_BATCH_SIZE,
    trust_remote_code: bool = False,
    tokenizer: Any = None,
) -> TokenCountStats:
    """Count tokenizer tokens across an iterable of strings.

    This is the primary programmatic entry-point folded in from C4AI
    token-counter. It accepts any iterable of strings — files, dataset rows,
    in-memory lists — and returns a TokenCountStats with full distribution stats.

    Parameters
    ----------
    texts:
        Iterable of raw text strings to tokenize.
    model:
        HuggingFace tokenizer model. Default: Qwen/Qwen3-1.7B-Base.
    batch_size:
        Documents per tokenizer batch for throughput.
    trust_remote_code:
        Pass to AutoTokenizer.from_pretrained.
    tokenizer:
        Pre-loaded tokenizer object (skips loading if provided).

    Returns
    -------
    TokenCountStats
        Distribution stats including total_tokens, mean, median, p95, p99.
    """
    if tokenizer is None:
        tokenizer = _tc_load_tokenizer(model, trust_remote_code=trust_remote_code)

    stats = TokenCountStats()
    stats.started_at_epoch = time.time()
    batch: List[str] = []

    def _flush() -> None:
        if not batch:
            return
        lengths = _tc_token_lengths(tokenizer, batch)
        for text, length in zip(batch, lengths):
            stats.observe_document(text=text, token_length=length)
        batch.clear()

    for raw in texts:
        stats.rows_seen += 1
        if raw is None:
            stats.null_field_rows += 1
            continue
        if not isinstance(raw, str):
            stats.non_string_rows_coerced += 1
            raw = str(raw)
        if raw == "":
            stats.empty_text_rows += 1
        batch.append(raw)
        if len(batch) >= batch_size:
            _flush()

    _flush()
    stats.completed_at_epoch = time.time()
    stats.wall_time = max(0.0, stats.completed_at_epoch - stats.started_at_epoch)
    return stats


def count_tokens_in_file(
    path: str,
    *,
    field: str = "text",
    file_fmt: str = "jsonl",
    model: str = DEFAULT_TC_MODEL,
    batch_size: int = DEFAULT_TC_BATCH_SIZE,
    max_docs: Optional[int] = None,
    trust_remote_code: bool = False,
    tokenizer: Any = None,
) -> TokenCountStats:
    """Count tokens in a local JSONL, plain-text, or Parquet file.

    For JSONL: each line is parsed as JSON and the ``field`` key is extracted.
    For plain-text (file_fmt='text'): each line is treated as one document.
    For Parquet (file_fmt='parquet'): requires pandas + pyarrow.

    Parameters
    ----------
    path:
        Absolute or relative path to the input file.
    field:
        Key to extract from each JSON object (JSONL/Parquet only).
    file_fmt:
        'jsonl', 'text', or 'parquet'.
    model:
        HuggingFace tokenizer model.
    batch_size:
        Documents per tokenizer batch.
    max_docs:
        Stop after processing this many documents.
    trust_remote_code:
        Passed to the tokenizer loader.
    tokenizer:
        Pre-loaded tokenizer to reuse across calls.

    Returns
    -------
    TokenCountStats
        Populated distribution stats object.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Input file not found: {path}")

    def _iter() -> Iterator[str]:
        count = 0
        if file_fmt == "jsonl":
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                        val = row.get(field, "")
                    except json.JSONDecodeError:
                        val = line
                    yield val if isinstance(val, str) else str(val)
                    count += 1
                    if max_docs is not None and count >= max_docs:
                        return
        elif file_fmt == "text":
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    yield line.rstrip("\n")
                    count += 1
                    if max_docs is not None and count >= max_docs:
                        return
        elif file_fmt == "parquet":
            try:
                import pandas as pd  # type: ignore
            except ImportError:
                raise ImportError(
                    "pandas required for parquet support: pip install pandas pyarrow")
            df = pd.read_parquet(path, columns=[field])
            for val in df[field]:
                yield val if isinstance(val, str) else str(val)
                count += 1
                if max_docs is not None and count >= max_docs:
                    return
        else:
            raise ValueError(
                f"Unsupported format: {file_fmt}. Use 'jsonl', 'text', or 'parquet'.")

    return count_tokens_in_texts(
        _iter(),
        model=model,
        batch_size=batch_size,
        trust_remote_code=trust_remote_code,
        tokenizer=tokenizer,
    )


def print_token_distribution_report(
    stats: TokenCountStats, *, title: str = "Token Distribution"
) -> None:
    """Print a compact distribution summary to stdout.

    Mirrors the summary block from C4AI token-counter Markdown reports.
    """
    d = stats.distribution_summary()
    sep = "=" * 60
    print(f"\n{sep}")
    print(f"  {title}")
    print(sep)
    print(f"  Total tokens      : {d['total_tokens']:,}")
    print(f"  Documents         : {d['documents_processed']:,}")
    if d["mean"] is not None:
        print(f"  Mean              : {d['mean']:,.1f}")
    if d["median"] is not None:
        print(f"  Median            : {d['median']:,.1f}")
    if d["iqr"] is not None:
        print(f"  IQR               : {d['iqr']:,.1f}")
    if d["p95"] is not None:
        print(f"  P95               : {d['p95']:,.1f}")
    if d["p99"] is not None:
        print(f"  P99               : {d['p99']:,.1f}")
    if d["stddev"] is not None:
        print(f"  Std Dev           : {d['stddev']:,.1f}")
    print(sep)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="token_tracker.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Cross-provider token usage monitor. Reports input / output / cache-read / "
            "reasoning tokens by company (Claude Code, Google Antigravity, OpenAI Codex, "
            "Gemini CLI, + Fleet ledger) and by day, over any range you ask for."
        ),
        epilog=(
            "ranges (most specific wins: --start/--end > --month > --compare > --weeks > --days > positional)\n"
            f"  token_tracker.py                                    last {DEFAULT_DAYS} days (default)\n"
            "  token_tracker.py 7                                  last 7 days (legacy positional)\n"
            "  token_tracker.py --days 7                           last 7 days\n"
            "  token_tracker.py --weeks 2                          last 14 days\n"
            "  token_tracker.py --month 2026-06                    a calendar month\n"
            "  token_tracker.py --start 2026-06-11 --end 2026-06-18   explicit inclusive range\n"
            "  token_tracker.py --compare 7                        last 7d vs prior 7d, per company\n"
            "  token_tracker.py 7 --by-provider                   group the Fleet ledger by provider\n"
            "\nenv: TOKEN_TRACKER_CUTOFF=<iso8601> overrides the lower bound;\n"
            "     TOKEN_TRACKER_CONFIG=<path.json> overlays every table & knob\n"
            "     (see tracker_config.example.json); any knob name uppercased\n"
            "     works as a direct env override (e.g. CHARS_PER_TOKEN=5)."
        ),
    )
    parser.add_argument("days_pos", nargs="?", type=int, default=None,
                        help=f"legacy positional: days back from now (default {DEFAULT_DAYS})")
    parser.add_argument("--days", type=int, default=None, help="days back from now")
    parser.add_argument("--weeks", type=int, default=None,
                        help="weeks back from now (7*N days)")
    parser.add_argument("--start", metavar="YYYY-MM-DD", default=None,
                        help="explicit window start, inclusive")
    parser.add_argument("--end", metavar="YYYY-MM-DD", default=None,
                        help="explicit window end, inclusive (default = now)")
    parser.add_argument("--month", metavar="YYYY-MM", default=None,
                        help="restrict the window to a calendar month")
    parser.add_argument("--compare", type=int, metavar="N", default=None,
                        help="compare the last N days vs the prior N days, per company")
    parser.add_argument("--lanes", action="store_true",
                        help="full analytics dashboard: 24h/WTD/MTD/QTD/HTD/YTD "
                             "comparisons + records (highest day/week/month/streak)")
    parser.add_argument("--by-provider", action="store_true",
                        help="group the Fleet usage ledger rows by provider")
    parser.add_argument("--top", type=int, default=TOP_HOGS_COUNT, metavar="N",
                        help=f"rows in the Token Hogs table (default {TOP_HOGS_COUNT})")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="log skipped files / RPC probes to stderr")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    # --demo-tc subcommand (vendored C4AI token-counter)
    demo = parser.add_argument_group("token-counter demo (vendored C4AI core)")
    demo.add_argument("--demo-tc", metavar="PATH", default=None,
                      help="count tokenizer tokens in a file and exit")
    demo.add_argument("--field", default="text",
                      help="JSON field to extract (JSONL/Parquet)")
    demo.add_argument("--format", default="jsonl",
                      choices=["jsonl", "text", "parquet"], dest="file_fmt",
                      help="demo input format")
    demo.add_argument("--model", default=DEFAULT_TC_MODEL,
                      help="HuggingFace tokenizer model for the demo")
    demo.add_argument("--max-docs", type=int, default=None,
                      help="max documents to process in the demo")
    return parser


def resolve_days(args: argparse.Namespace) -> int:
    """Days of history to collect, honoring flag precedence and wide modes."""
    days = DEFAULT_DAYS
    if args.days is not None:
        days = args.days
    elif args.weeks is not None:
        days = args.weeks * 7
    elif args.days_pos is not None:
        days = args.days_pos
    # --compare widens the collection window so both periods are gathered.
    if args.compare is not None and args.compare > 0:
        days = max(days, args.compare * 2)
    if args.lanes:
        days = max(days, LANES_HISTORY_DAYS)
    return days


def resolve_window(args: argparse.Namespace) -> Window:
    days = resolve_days(args)
    if args.start or args.end:
        return Window.from_range(args.start, args.end, days)
    if args.month:
        try:
            return Window.from_month(args.month)
        except (ValueError, IndexError) as exc:
            print(f"Error parsing --month {args.month}: {exc}")
            raise SystemExit(1)
    return Window.last_days(days)


def _print_window_header(args: argparse.Namespace, window: Window) -> None:
    print("\n======================================================================")
    if args.start or args.end:
        print(f"Token usage monitor — range {window.cutoff:%Y-%m-%d} -> "
              f"{window.limit_upper - timedelta(days=1):%Y-%m-%d}")
        print(f"window: {window.cutoff:%Y-%m-%d %H:%M} -> "
              f"{window.limit_upper:%Y-%m-%d %H:%M} {window.now:%Z}")
    elif args.month:
        print(f"Token usage monitor — Month: {args.month}")
        print(f"window: {window.cutoff:%Y-%m-%d %H:%M} -> "
              f"{window.limit_upper:%Y-%m-%d %H:%M} {window.now:%Z}")
    elif args.compare:
        print(f"Token usage monitor — compare last {args.compare}d vs prior "
              f"{args.compare}d (collecting {window.days} days)")
        print(f"window: {window.cutoff:%Y-%m-%d %H:%M} -> "
              f"{window.now:%Y-%m-%d %H:%M} {window.now:%Z}")
    else:
        print(f"Token usage monitor — last {window.days} day(s)")
        print(f"window: {window.cutoff:%Y-%m-%d %H:%M} -> "
              f"{window.now:%Y-%m-%d %H:%M} {window.now:%Z}")
    print("======================================================================\n")


def run_demo_tc(args: argparse.Namespace) -> int:
    """--demo-tc PATH: run the vendored C4AI token-counter and exit."""
    print(f"[token-counter demo] Loading tokenizer: {args.model}")
    print(f"[token-counter demo] Input: {args.demo_tc}  "
          f"format={args.file_fmt}  field={args.field}")
    stats = count_tokens_in_file(
        args.demo_tc,
        field=args.field,
        file_fmt=args.file_fmt,
        model=args.model,
        max_docs=args.max_docs,
    )
    print_token_distribution_report(
        stats, title=f"Token Distribution -- {args.demo_tc}")
    print(f"\n[token-counter demo] Wall time: {stats.wall_time:.2f}s")
    return 0


def run_report(args: argparse.Namespace) -> int:
    """Scan every source for the resolved window and render the full report."""
    window = resolve_window(args)
    _print_window_header(args, window)

    # 1. Claude Code
    claude = parse_claude(window)
    print("--- Claude Code ---")
    print(f"Transcripts scanned: {claude.files_scanned}   "
          f"Usage records: {claude.records}\n")
    print_day_table(claude.usage.by_day, claude.usage.totals)
    print()

    # 2. Google Antigravity
    anti = parse_antigravity(window)
    # Retention note computed from what is actually on disk, not assumed.
    if anti.earliest_record is not None:
        retention = (f"[! partial - earliest Antigravity record on disk: "
                     f"{anti.earliest_record:%Y-%m-%d}; anything older is "
                     f"encrypted or purged]")
    else:
        retention = "[! no Antigravity records found on disk]"
    print(f"--- Google Antigravity (Hybrid RPC & Estimation) {retention} ---")
    print(f"Sessions scanned: {anti.sessions_scanned} ({anti.active_sessions} active "
          f"via RPC + {anti.fallback_transcripts} fallback from disk)")
    print(f"Invocations tracked: {anti.records} ({anti.rpc_records} exact via RPC "
          f"+ {anti.estimated_records} estimated fallback)\n")
    print_day_table(anti.usage.by_day, anti.usage.totals)
    print()
    compute_and_print_split(anti.usage.invocations, "Antigravity")

    # 3. OpenAI Codex
    codex = parse_codex(window)
    print("--- OpenAI Codex (Granular Rollout Parsing) ---")
    print(f"Sessions scanned: {codex.sessions_scanned}   "
          f"Usage events: {codex.records}\n")
    print_day_table(codex.usage.by_day, codex.usage.totals)
    # Cross-check our rollout parse against Codex's OWN native counter. Compare
    # like with like: Codex's tokens_used excludes cache-reads, so validate
    # against our non-cache billable tokens (input w/CC + output + reasoning),
    # and note cache separately rather than inflating the delta.
    txi, txo, _txcc, txcr, txrt = cols(codex.usage.totals)
    nthreads, native = codex_native_total(window)
    billable = txi + txo + txrt
    if native:
        delta = (billable - native) / native * 100
        print(f"  cross-check: Codex native (threads.tokens_used) {fmt(native)} "
              f"across {nthreads} threads "
              f"| our billable (in+out+reasoning, no cache) {fmt(billable)} "
              f"| delta {delta:+.0f}% "
              f"(+{fmt(txcr)} cache-read tracked separately)")
    print()

    # 3b. Gemini CLI (pre-May history fills here)
    gemini = parse_gemini_cli(window)
    print("--- Gemini CLI (Hybrid Exact & Estimation) ---")
    print(f"Sessions scanned: {gemini.files_scanned}   Invocations tracked: "
          f"{gemini.records} ({gemini.exact_records} exact + "
          f"{gemini.estimated_records} estimated)\n")
    print_day_table(gemini.usage.by_day, gemini.usage.totals)
    print()
    compute_and_print_split(gemini.usage.invocations, "Gemini CLI")

    # 3d. Fleet usage ledger
    fleet = parse_fleet_usage(window)
    print("--- Fleet Usage Ledger (Local Ollama/Gemma + OpenRouter + HF) ---")
    print(f"Invocations tracked: {fleet.records} "
          "(exact, from each lane's API response)\n")
    print_day_table(fleet.usage.by_day, fleet.usage.totals)
    print()

    # 4. Cross-source aggregates
    scans = (claude.usage, anti.usage, codex.usage, gemini.usage, fleet.usage)
    all_models = merge_by_model(scans)
    all_invocations: List[Invocation] = []
    for agg in scans:
        all_invocations.extend(agg.invocations)

    if all_models:
        print_model_breakdown_and_bill(all_models)
        compute_and_print_split(all_invocations, "Blended Report")
        print_cache_health_report(all_invocations)
        print_model_class_buckets(all_invocations)
        print_top_hogs(all_invocations, top_n=args.top)
        print_route_recommendations(all_invocations)
        if args.by_provider:
            print_by_provider_summary(all_invocations)

    print()

    if args.compare:
        print_period_comparison(all_invocations, window.now, args.compare)

    if args.lanes:
        print()
        print(SEP_70)
        print(f"Token usage analytics  (history through {window.now:%Y-%m-%d})")
        print(SEP_70)
        print_analytics_dashboard(all_invocations, window.now)

    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args, _unknown = build_parser().parse_known_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if args.demo_tc:
        return run_demo_tc(args)
    return run_report(args)


if __name__ == "__main__":
    sys.exit(main())
