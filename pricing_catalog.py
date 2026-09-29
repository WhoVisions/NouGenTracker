"""Offline, sourced model pricing catalog; USD per million tokens unless metered.

Text shadow bills use only the text/embedding projection. Other modalities and
per-minute/per-image charges remain distinct in the catalog, never fabricated
as text rates. Reading this module performs no network calls or cache writes.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import json
from pathlib import Path

CATALOG_PATH = Path(__file__).with_name("model_pricing_catalog.json")


def load_catalog():
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if catalog.get("schema_version") != 1:
        raise ValueError("unsupported model pricing catalog version")
    return catalog


def lookup_model(model, when=None, *, _catalog=None):
    """Return the exact model/alias record, with scheduled rates for a date."""
    catalog = _catalog if _catalog is not None else load_catalog()
    key = model.strip().lower()
    alias = catalog["aliases"].get(key)
    record = catalog["models"].get(alias["model"] if alias else key)
    if record is None:
        return None
    record = copy.deepcopy(record)
    record["model"] = alias["model"] if alias else key
    record["requested_model"] = key
    record["price_source"] = alias["source"] if alias else record.get("price_source", "doc")
    if when is None:
        day = datetime.date.today().isoformat()
    elif isinstance(when, datetime.datetime):
        day = when.date().isoformat()
    elif isinstance(when, datetime.date):
        day = when.isoformat()
    elif isinstance(when, str):
        if len(when) != 10:
            raise ValueError("pricing date must be YYYY-MM-DD")
        day = datetime.date.fromisoformat(when).isoformat()
    else:
        raise ValueError("pricing date must be a date or YYYY-MM-DD")
    for step in sorted(record.get("scheduled_rates", []), key=lambda s: s["starts_on"]):
        if day >= step["starts_on"]:
            record["rates"] = copy.deepcopy(step["rates"])
            if "metered" in step:
                record["metered"] = copy.deepcopy(step["metered"])
    if record.get("billing_starts_on") and day < record["billing_starts_on"]:
        record["rates"] = {"text": {"input": 0.0, "output": 0.0, "cache_read": 0.0}}
        record["billing_status"] = "not_yet_billed"
    return record


def _text_projection(record):
    rates = record["rates"].get("text") or record["rates"].get("embedding")
    if not rates or "input" not in rates:
        return None
    # An image/TTS model with text input and non-text output cannot be priced
    # by inventing a free text output. Embeddings have no output-token bill.
    if "output" not in rates and "embedding" not in record["rates"]:
        return None
    source = record.get("price_source", "doc")
    if len(record["rates"]) > 1 and source == "doc":
        source = "doc-text-only"
    return (rates["input"], rates.get("output", 0.0), rates.get("cache_read", 0.0), source)


def text_prices():
    """Current fallback rates for the tracker's disjoint text token buckets."""
    catalog = load_catalog()
    prices = {}
    for key in (*catalog["models"], *catalog["aliases"]):
        record = lookup_model(key, _catalog=catalog)
        price = _text_projection(record)
        if price is not None:
            prices[key] = price
    return prices


def text_price_schedules():
    """Explicit published future transitions; do not invent effective dates."""
    catalog = load_catalog()
    schedules = {}
    for key, record in catalog["models"].items():
        steps = record.get("scheduled_rates", [])
        if not steps and not record.get("billing_starts_on"):
            continue
        starts = [record["verified_on"]] + [step["starts_on"] for step in steps]
        if record.get("billing_starts_on"):
            starts.append(record["billing_starts_on"])
        bands = []
        starts = sorted(set(starts))
        for index, start in enumerate(starts):
            end = (datetime.date.fromisoformat(starts[index + 1]) - datetime.timedelta(days=1)).isoformat() if index + 1 < len(starts) else None
            price = _text_projection(lookup_model(key, start, _catalog=catalog))
            if price is not None:
                bands.append((start, end, price))
        if bands:
            schedules[key] = bands
    return schedules


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="exact model ID or published snapshot alias")
    parser.add_argument("--date", help="ISO date for a published pricing transition")
    args = parser.parse_args()
    if args.date is not None and not args.model:
        parser.error("--date requires --model")
    try:
        result = lookup_model(args.model, args.date) if args.model is not None else load_catalog()
    except ValueError as exc:
        parser.error(str(exc))
    if result is None:
        parser.error("model is not in the verified catalog")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
