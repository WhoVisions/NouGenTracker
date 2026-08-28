"""pricing_live.py - Dynamic vendor pricing resolver for NouGenTracker.

Authority: GM directive (Rule 0.2 / Rule 0.0, dynamic over hardcode).
Fetches official pricing pages at runtime with fallback tiers:
  (a) env override NOUGEN_PRICE_<MODELKEY>="in,out,cacheread"
  (b) valid cache within TTL (%USERPROFILE%\\.nougen\\pricing_cache.json)
  (c) live fetch from official vendor docs (then cache)
  (d) fallback constant (MODEL_PRICING, source "fallback-const")
  (e) unknown-model default (DEFAULT_PRICING)

Vendors (authority order):
  1. Anthropic / Claude: https://platform.claude.com/docs/en/about-claude/pricing (.md / Accept: text/markdown)
  2. Gemini: https://ai.google.dev/gemini-api/docs/pricing
  3. OpenAI: https://platform.openai.com/docs/pricing
"""

import datetime
import html
import json
import logging
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

logger = logging.getLogger("pricing_live")

# --- Source Tags -------------------------------------------------------------
class SourceTag(str):
    """String subclass that preserves string representation while supporting
    backward-compatible equality with legacy tags (e.g. 'doc')."""

    def __new__(cls, val, aliases=()):
        obj = str.__new__(cls, val)
        obj._aliases = set(aliases)
        return obj

    def __eq__(self, other):
        return str(self) == other or other in self._aliases

    def __hash__(self):
        return str.__hash__(self)


DOC = "doc"
EST = "est"
LIVE = SourceTag("doc-live", aliases=["doc"])
CACHED = SourceTag("doc-cached", aliases=["doc"])
FALLBACK_CONST = SourceTag("fallback-const", aliases=["doc"])
ENV = SourceTag("env", aliases=["doc"])

# --- Vendor Endpoints --------------------------------------------------------
VENDOR_SOURCES = [
    (
        "anthropic",
        [
            ("https://platform.claude.com/docs/en/about-claude/pricing.md", {"Accept": "text/markdown"}),
            ("https://platform.claude.com/docs/en/about-claude/pricing", {"Accept": "text/markdown, text/html"}),
        ],
    ),
    (
        "gemini",
        [
            ("https://ai.google.dev/gemini-api/docs/pricing", {"Accept": "text/html"}),
        ],
    ),
    (
        "openai",
        [
            # GM-corrected canonical URL (2026-08-28); platform path kept as legacy fallback
            ("https://developers.openai.com/api/docs/pricing", {"Accept": "text/html"}),
            ("https://platform.openai.com/docs/pricing", {"Accept": "text/html"}),
        ],
    ),
]

# Process-level state: one fetch attempt per vendor per process
_ATTEMPTED_VENDORS = set()
_MEMORY_CACHE = {}
_MEMORY_FETCHED_AT = None


def get_cache_path() -> Path:
    """Resolve cache file path: NOUGEN_PRICING_CACHE_PATH or %USERPROFILE%\\.nougen\\pricing_cache.json."""
    custom = os.environ.get("NOUGEN_PRICING_CACHE_PATH")
    if custom:
        return Path(custom)
    userprofile = os.environ.get("USERPROFILE") or os.environ.get("HOME") or str(Path.home())
    return Path(userprofile) / ".nougen" / "pricing_cache.json"


def get_ttl_hours() -> float:
    """Read TTL from NOUGEN_PRICING_TTL_HOURS (default 24). Logs when default is used."""
    val = os.environ.get("NOUGEN_PRICING_TTL_HOURS")
    if val is not None:
        try:
            return float(val)
        except ValueError:
            logger.warning("Invalid NOUGEN_PRICING_TTL_HOURS=%r; falling back to default 24 hours", val)
            return 24.0
    logger.info("NOUGEN_PRICING_TTL_HOURS not set; using fallback default 24 hours")
    return 24.0


def get_fetch_timeout_s() -> float:
    """Read fetch timeout in seconds from NOUGEN_PRICING_FETCH_TIMEOUT_S (default 8)."""
    val = os.environ.get("NOUGEN_PRICING_FETCH_TIMEOUT_S")
    if val is not None:
        try:
            return float(val)
        except ValueError:
            pass
    return 8.0


def normalize_model_name(name: str) -> str:
    """Normalize model names to tracker key style: lowercase, hyphens, no surrounding punctuation."""
    if not name:
        return ""
    # Strip markdown links e.g. [name](url)
    name = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', name)
    # Strip notes, brackets, parentheses e.g. "(<272K context length)", "(retired...)", "[1]"
    name = re.sub(r'\[.*?\]', '', name)
    name = re.sub(r'\(.*?\)', '', name)
    s = name.lower().strip()
    # Replace spaces, slashes, underscores with hyphens
    s = re.sub(r'[\s/_]+', '-', s)
    # Remove any characters except alphanumeric, hyphen, dot
    s = re.sub(r'[^a-z0-9\.\-]+', '', s)
    s = re.sub(r'-+', '-', s).strip('-')
    return s


# --- Parsers -----------------------------------------------------------------

def parse_anthropic_pricing(text: str) -> dict:
    """Tolerant parser for Anthropic pricing pages (Markdown or HTML)."""
    results = {}
    if not text:
        return results

    try:
        # 1. Try Markdown table
        if "|" in text and "---" in text:
            lines = text.splitlines()
            in_table = False
            headers = []
            for line in lines:
                line_str = line.strip()
                if not line_str.startswith("|"):
                    in_table = False
                    continue
                parts = [c.strip() for c in line_str.strip("|").split("|")]
                if any("base input" in p.lower() or "input tokens" in p.lower() for p in parts):
                    headers = [p.lower() for p in parts]
                    in_table = True
                    continue
                if in_table and any("---" in p for p in parts):
                    continue
                if in_table and headers and len(parts) >= len(headers):
                    row = dict(zip(headers, parts))
                    model_raw = parts[0]
                    inp_col = next((v for k, v in row.items() if "base input" in k or ("input" in k and "batch" not in k and "fast" not in k)), None)
                    out_col = next((v for k, v in row.items() if "output" in k and "batch" not in k and "fast" not in k), None)
                    cache_col = next((v for k, v in row.items() if "cache hit" in k or "cache read" in k), None)
                    if inp_col and out_col:
                        m_in = re.findall(r'\$([0-9]+(?:\.[0-9]+)?)', inp_col)
                        m_out = re.findall(r'\$([0-9]+(?:\.[0-9]+)?)', out_col)
                        m_cache = re.findall(r'\$([0-9]+(?:\.[0-9]+)?)', cache_col) if cache_col else []
                        if m_in and m_out:
                            inp_val = float(m_in[0])
                            out_val = float(m_out[0])
                            cache_val = float(m_cache[0]) if m_cache else round(inp_val * 0.1, 4)
                            key = normalize_model_name(model_raw)
                            if key:
                                val = (inp_val, out_val, cache_val)
                                results[key] = val
                                if "." in key:
                                    results[key.replace(".", "-")] = val

        # 2. Try HTML tables if markdown did not produce results
        if not results:
            tables = re.findall(r'<table[\s\S]*?</table>', text, re.IGNORECASE)
            for t in tables:
                rows = re.findall(r'<tr[\s\S]*?</tr>', t, re.IGNORECASE)
                headers = []
                for r in rows:
                    cells = [re.sub(r'<[^>]+>', ' ', c).strip() for c in re.findall(r'<t[dh][\s\S]*?</t[dh]>', r, re.IGNORECASE)]
                    if not cells:
                        continue
                    if any('base input' in c.lower() or 'output token' in c.lower() for c in cells):
                        headers = [c.lower() for c in cells]
                        continue
                    if headers and len(cells) >= len(headers):
                        row = dict(zip(headers, cells))
                        model_raw = cells[0]
                        inp_col = next((v for k, v in row.items() if "base input" in k), None)
                        out_col = next((v for k, v in row.items() if "output token" in k), None)
                        cache_col = next((v for k, v in row.items() if "cache hit" in k or "cache read" in k), None)
                        if inp_col and out_col:
                            m_in = re.findall(r'\$([0-9]+(?:\.[0-9]+)?)', inp_col)
                            m_out = re.findall(r'\$([0-9]+(?:\.[0-9]+)?)', out_col)
                            m_cache = re.findall(r'\$([0-9]+(?:\.[0-9]+)?)', cache_col) if cache_col else []
                            if m_in and m_out:
                                inp_val = float(m_in[0])
                                out_val = float(m_out[0])
                                cache_val = float(m_cache[0]) if m_cache else round(inp_val * 0.1, 4)
                                key = normalize_model_name(model_raw)
                                if key:
                                    val = (inp_val, out_val, cache_val)
                                    results[key] = val
                                    if "." in key:
                                        results[key.replace(".", "-")] = val
    except Exception as e:
        logger.warning("Anthropic pricing parse error: %s; degraded to no-data", e)

    return results


def parse_gemini_pricing(text: str) -> dict:
    """Tolerant parser for Google Gemini API pricing pages (HTML)."""
    results = {}
    if not text:
        return results

    try:
        sections = re.findall(r'<h2[^>]*>([\s\S]*?)</h2>([\s\S]*?)(?=<h2|$)', text, re.IGNORECASE)
        for h2_raw, content in sections:
            model_name = re.sub(r'<[^>]+>', '', h2_raw).strip()
            model_name = model_name.encode('ascii', 'ignore').decode('ascii').strip()
            if not model_name or 'pricing' in model_name.lower():
                continue

            # Standard tier table preferred; fallback to first table
            std_match = re.search(r'<h3[^>]*>\s*Standard\s*</h3>([\s\S]*?)<(?:h[23]|/section)', content, re.IGNORECASE)
            sec_to_search = std_match.group(1) if std_match else content
            table_match = re.search(r'<table[\s\S]*?</table>', sec_to_search, re.IGNORECASE)
            if not table_match:
                continue

            rows = re.findall(r'<tr[\s\S]*?</tr>', table_match.group(0), re.IGNORECASE)
            inp, out, cache = None, None, None
            for r in rows:
                cells = [re.sub(r'<[^>]+>', ' ', c).strip() for c in re.findall(r'<t[dh][\s\S]*?</t[dh]>', r, re.IGNORECASE)]
                if len(cells) < 2:
                    continue
                label = cells[0].lower()
                val_cell = cells[-1]
                m_dollars = re.findall(r'\$([0-9]+(?:\.[0-9]+)?)', val_cell)
                if not m_dollars:
                    continue
                val = float(m_dollars[0])
                if 'input price' in label and inp is None:
                    inp = val
                elif 'output price' in label and out is None:
                    out = val
                elif 'context caching' in label and cache is None:
                    cache = val

            if inp is not None and out is not None:
                cache_read = cache if cache is not None else round(inp * 0.1, 4)
                key = normalize_model_name(model_name)
                if key:
                    val = (inp, out, cache_read)
                    results[key] = val
                    if "." in key:
                        results[key.replace(".", "-")] = val
    except Exception as e:
        logger.warning("Gemini pricing parse error: %s; degraded to no-data", e)

    return results


def parse_openai_pricing(text: str) -> dict:
    """Tolerant parser for OpenAI pricing pages (HTML / Next.js hydration props)."""
    results = {}
    if not text:
        return results

    try:
        unescaped = html.unescape(text)

        # 1. Parse React / Next.js hydration props
        # Format: [0, "model name"], [0, inp], [0, cached], [0, write], [0, out]
        pattern = re.compile(
            r'\[0,\s*"([^"]+?)"\]\s*,\s*'
            r'\[0,\s*([0-9]+(?:\.[0-9]+)?|-|"-")\]\s*,\s*'
            r'\[0,\s*([0-9]+(?:\.[0-9]+)?|-|"-")\]\s*,\s*'
            r'\[0,\s*([0-9]+(?:\.[0-9]+)?|-|"-")\]\s*,\s*'
            r'\[0,\s*([0-9]+(?:\.[0-9]+)?)\]'
        )
        for m in pattern.finditer(unescaped):
            m_name, m_inp, m_cached, m_write, m_out = m.groups()
            if m_inp in ('-', '"-"') or m_out in ('-', '"-"'):
                continue
            try:
                inp = float(m_inp)
                out = float(m_out)
                cache = float(m_cached) if m_cached not in ('-', '"-"') else 0.0
                key = normalize_model_name(m_name)
                if key and key not in results:
                    val = (inp, out, cache)
                    results[key] = val
                    if "." in key:
                        results[key.replace(".", "-")] = val
            except ValueError:
                continue

        # 2. Parse HTML tables
        tables = re.findall(r'<table[\s\S]*?</table>', unescaped, re.IGNORECASE)
        for t in tables:
            rows = re.findall(r'<tr[\s\S]*?</tr>', t, re.IGNORECASE)
            for r in rows:
                cells = [re.sub(r'<[^>]+>', ' ', c).strip() for c in re.findall(r'<t[dh][\s\S]*?</t[dh]>', r, re.IGNORECASE)]
                if len(cells) < 3:
                    continue
                for i, c in enumerate(cells):
                    clean_c = c.strip()
                    if re.match(r'^(?:gpt-|chat-|o[0-9]|sora-|dall-)', clean_c, re.I):
                        tail = cells[i + 1:]
                        dollars = []
                        for tc in tail:
                            m_d = re.findall(r'\$([0-9]+(?:\.[0-9]+)?)', tc)
                            if m_d:
                                dollars.append(float(m_d[0]))
                            elif tc in ('-', ''):
                                dollars.append(0.0)
                        if len(dollars) >= 2:
                            inp = dollars[0]
                            cache = dollars[1] if len(dollars) >= 3 else 0.0
                            out = dollars[3] if len(dollars) >= 4 else (dollars[2] if len(dollars) == 3 else dollars[1])
                            key = normalize_model_name(clean_c)
                            if key and key not in results:
                                val = (inp, out, cache)
                                results[key] = val
                                if "." in key:
                                    results[key.replace(".", "-")] = val
                        break
    except Exception as e:
        logger.warning("OpenAI pricing parse error: %s; degraded to no-data", e)

    return results


# --- Cache Management --------------------------------------------------------

def load_cache(cache_path: Path = None) -> tuple[dict, bool]:
    """Load cached pricing data. Returns (models_dict, is_valid_bool)."""
    global _MEMORY_CACHE, _MEMORY_FETCHED_AT

    path = cache_path or get_cache_path()
    if not path.is_file():
        return ({}, False)

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        fetched_at_str = data.get("fetched_at")
        if not fetched_at_str:
            return ({}, False)

        try:
            # Parse ISO timestamp
            fetched_dt = datetime.datetime.fromisoformat(fetched_at_str)
            if fetched_dt.tzinfo is None:
                fetched_dt = fetched_dt.replace(tzinfo=datetime.timezone.utc)
            now = datetime.datetime.now(datetime.timezone.utc)
            age_hours = (now - fetched_dt).total_seconds() / 3600.0
        except Exception:
            age_hours = 999999.0

        ttl_hours = get_ttl_hours()
        is_valid = age_hours <= ttl_hours

        models_raw = data.get("models")
        if models_raw is None:
            # Legacy or flat format
            models_raw = {k: v for k, v in data.items() if k not in ("fetched_at", "source_url", "sources")}

        models = {}
        for k, v in models_raw.items():
            if isinstance(v, (list, tuple)) and len(v) >= 3:
                models[k] = (float(v[0]), float(v[1]), float(v[2]))
            elif isinstance(v, dict):
                inp = float(v.get("input", 0.0))
                out = float(v.get("output", 0.0))
                cache = float(v.get("cache_read", round(inp * 0.1, 4)))
                models[k] = (inp, out, cache)

        _MEMORY_CACHE = models
        _MEMORY_FETCHED_AT = fetched_at_str
        return (models, is_valid)
    except Exception as e:
        logger.warning("Failed to load pricing cache at %s: %s", path, e)
        return ({}, False)


def save_cache(models: dict, source_urls: dict = None, cache_path: Path = None) -> None:
    """Save pricing models to cache JSON atomically."""
    path = cache_path or get_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        payload = {
            "fetched_at": now_iso,
            "source_url": source_urls or {},
            "models": {k: list(v) for k, v in models.items()},
        }
        tmp_path = path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        tmp_path.replace(path)
        logger.info("Saved pricing cache to %s (%d models)", path, len(models))
    except Exception as e:
        logger.warning("Failed to save pricing cache to %s: %s", path, e)


# --- Fetch Execution ---------------------------------------------------------

def fetch_vendor_pricing(vendor: str, timeout_s: float = None) -> tuple[dict, str]:
    """Fetch and parse pricing from a vendor's official documentation URL.
    Respects single fetch attempt per vendor per process."""
    if vendor in _ATTEMPTED_VENDORS:
        return ({}, "")
    _ATTEMPTED_VENDORS.add(vendor)

    timeout = timeout_s if timeout_s is not None else get_fetch_timeout_s()
    headers_base = {
        "User-Agent": "NouGenTracker/1.0 (pricing resolver; +https://github.com/WhoVisions/NouGenTracker)",
    }

    vendor_entry = next((v for v in VENDOR_SOURCES if v[0] == vendor), None)
    if not vendor_entry:
        return ({}, "")

    for url, extra_headers in vendor_entry[1]:
        hdrs = dict(headers_base)
        hdrs.update(extra_headers)
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_bytes = resp.read()
                text = raw_bytes.decode("utf-8", errors="replace")

            parsed = {}
            if vendor == "anthropic":
                parsed = parse_anthropic_pricing(text)
            elif vendor == "gemini":
                parsed = parse_gemini_pricing(text)
            elif vendor == "openai":
                parsed = parse_openai_pricing(text)

            if parsed:
                logger.info("Successfully fetched and parsed %s pricing from %s (%d models)", vendor, url, len(parsed))
                return (parsed, url)
        except Exception as e:
            # Silent fallback with one log line
            logger.info("Pricing fetch offline/failed for %s (%s): %s; silent fallback", vendor, url, e)

    return ({}, "")


def fetch_all_vendors(timeout_s: float = None, cache_path: Path = None) -> dict:
    """Fetch all vendors in authority order, merge with existing cache, and persist."""
    # Load existing cache data to avoid discarding models from other vendors
    existing_models, _ = load_cache(cache_path)
    combined = dict(existing_models)
    source_urls = {}

    for vendor, _ in VENDOR_SOURCES:
        parsed, url = fetch_vendor_pricing(vendor, timeout_s=timeout_s)
        if parsed:
            combined.update(parsed)
            source_urls[vendor] = url

    if combined:
        save_cache(combined, source_urls=source_urls, cache_path=cache_path)

    global _MEMORY_CACHE
    _MEMORY_CACHE = combined
    return combined


# --- Resolution Logic --------------------------------------------------------

def resolve_exact_price(key: str, fallback_pricing: dict = None, cache_path: Path = None) -> tuple:
    """Resolve exact model price through tiers (a) -> (b) -> (c) -> (d).
    Returns (inp, out, cache_read, source_tag) or None."""
    if not key:
        return None

    # Tier (a): Env override NOUGEN_PRICE_<MODELKEY>="in,out,cacheread"
    norm_upper = key.upper().replace("-", "_").replace(".", "_")
    norm_clean = re.sub(r'[^A-Z0-9_]+', '_', norm_upper).strip('_')
    env_keys = [
        f"NOUGEN_PRICE_{norm_clean}",
        f"NOUGEN_PRICE_{key.replace('.', '_')}",
        f"NOUGEN_PRICE_{key}",
    ]
    for ek in env_keys:
        env_val = os.environ.get(ek)
        if env_val:
            try:
                parts = [float(p.strip()) for p in env_val.split(",") if p.strip()]
                if len(parts) >= 2:
                    inp = parts[0]
                    out = parts[1]
                    cache_read = parts[2] if len(parts) >= 3 else round(inp * 0.1, 4)
                    res = (inp, out, cache_read, ENV)
                    logger.info("[tier a] Env override for %s (%s=%r) -> %s", key, ek, env_val, res)
                    return res
            except ValueError:
                logger.warning("Invalid pricing format in %s=%r", ek, env_val)

    # Tier (b): Valid cache within TTL
    cached_models, is_valid = load_cache(cache_path)
    if is_valid:
        if key in cached_models:
            inp, out, cache_read = cached_models[key]
            res = (inp, out, cache_read, CACHED)
            logger.info("[tier b] Valid cache hit within TTL for %s -> %s", key, res)
            return res
        # Check alias (e.g. dot vs hyphen)
        alt_key = key.replace("-", ".") if "-" in key else key.replace(".", "-")
        if alt_key in cached_models:
            inp, out, cache_read = cached_models[alt_key]
            res = (inp, out, cache_read, CACHED)
            logger.info("[tier b] Valid cache hit within TTL for %s (via %s) -> %s", key, alt_key, res)
            return res

    # Tier (c): Live fetch (then cache)
    # Only fetch if vendors have not been attempted yet in this process
    unattempted = [v for v, _ in VENDOR_SOURCES if v not in _ATTEMPTED_VENDORS]
    if unattempted:
        fetched = fetch_all_vendors(cache_path=cache_path)
        if key in fetched:
            inp, out, cache_read = fetched[key]
            res = (inp, out, cache_read, LIVE)
            logger.info("[tier c] Live fetch resolved %s -> %s", key, res)
            return res
        alt_key = key.replace("-", ".") if "-" in key else key.replace(".", "-")
        if alt_key in fetched:
            inp, out, cache_read = fetched[alt_key]
            res = (inp, out, cache_read, LIVE)
            logger.info("[tier c] Live fetch resolved %s (via %s) -> %s", key, alt_key, res)
            return res

    # Tier (d): Fallback constant MODEL_PRICING tagged "fallback-const"
    if fallback_pricing and key in fallback_pricing:
        entry = fallback_pricing[key]
        inp, out, cache_read = entry[0], entry[1], entry[2]
        res = (inp, out, cache_read, FALLBACK_CONST)
        logger.info("[tier d] Fallback constant for %s -> %s", key, res)
        return res

    return None


def resolve_price(
    model_name: str,
    fallback_pricing: dict = None,
    default_pricing: tuple = None,
    variant_suffixes: tuple = None,
    cache_path: Path = None,
) -> tuple:
    """Full pricing resolution function for token_tracker:
      1. Tiers (a)-(d) exact lookup
      2. Family variant inference (tagging EST)
      3. Tier (e) default pricing
    """
    key = normalize_model_name(model_name)
    exact = resolve_exact_price(key, fallback_pricing=fallback_pricing, cache_path=cache_path)
    if exact is not None:
        return exact

    # Family inference for variant suffixes (e.g. gemini-3.6-flash-high -> gemini-3.6-flash)
    suffixes = variant_suffixes or (
        "high", "medium", "low", "minimal", "thinking", "latest", "preview", "customtools"
    )
    parts = key.split("-")
    while len(parts) > 1 and parts[-1].lower() in suffixes:
        parts = parts[:-1]
        base_key = "-".join(parts)
        found = resolve_exact_price(base_key, fallback_pricing=fallback_pricing, cache_path=cache_path)
        if found is not None:
            inp, out, cache_read, _ = found
            res = (inp, out, cache_read, EST)
            logger.info("[family inference] Variant %s inferred from %s -> %s", key, base_key, res)
            return res

    # Tier (e): Default unknown-model pricing
    default = default_pricing if default_pricing is not None else (1.00, 4.00, 0.100, EST)
    logger.info("[tier e] Unknown model %s; using default pricing -> %s", key, default)
    return default


def reset_process_state():
    """Reset process-level state (for unit testing)."""
    global _ATTEMPTED_VENDORS, _MEMORY_CACHE, _MEMORY_FETCHED_AT
    _ATTEMPTED_VENDORS.clear()
    _MEMORY_CACHE.clear()
    _MEMORY_FETCHED_AT = None


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    print("--- NouGen Pricing Resolver Diagnostics ---")
    cache_f = get_cache_path()
    ttl = get_ttl_hours()
    print(f"Cache path: {cache_f}")
    print(f"TTL hours: {ttl}")
    models_cached, valid = load_cache()
    print(f"Cache status: {'VALID' if valid else 'EXPIRED/MISSING'} ({len(models_cached)} models cached)")
    if len(sys.argv) > 1:
        target = sys.argv[1]
        resolved = resolve_price(target)
        print(f"Resolved {target}: {resolved}")
