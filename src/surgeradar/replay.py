"""Deterministic local JSONL replay; never connects to a provider."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path

from .detector import DETECTOR_VERSION, Detector, DetectorConfig
from .model import normalize_bar, timestamp


def load_fixture(path):
    bars = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or row.get("provider") != "alpaca" or row.get("feed") not in ("iex", "sip"):
                raise ValueError("explicit alpaca provider and iex/sip feed required")
            event_at = timestamp(row["event_at"])
            # Synthetic receive time, independent of wall clock; not live evidence.
            received_at = timestamp(row["received_at"]) if "received_at" in row else event_at + timedelta(minutes=1)
            item = {"T": "b", "S": row["symbol"], "t": row["event_at"],
                    **{target: row[source] for source, target in
                       (("open", "o"), ("high", "h"), ("low", "l"), ("close", "c"), ("volume", "v"))},
                    "vw": row.get("vwap"), "n": row.get("trade_count")}
            bars.append(replace(normalize_bar(item, received_at, feed=row["feed"]), provider=row["provider"]))
        except (ValueError, KeyError, TypeError):
            raise ValueError(f"invalid_fixture_row:{line_number}") from None
    if not bars:
        raise ValueError("empty_fixture")
    return bars


def replay(bars, config=None):
    ordered = sorted(bars, key=lambda b: (b.event_at, b.provider, b.feed, b.ticker))
    seen = set()
    for bar in ordered:
        key = (bar.provider, bar.feed, bar.ticker, bar.event_at)
        if key in seen:
            raise ValueError("duplicate_bar_in_fixture")
        seen.add(key)
    detector = Detector(config)
    return [detector.evaluate(bar) for bar in ordered]


def main(path, config_path=None):
    try:
        config_values = (json.loads(Path(config_path).read_text(encoding="utf-8"), parse_float=Decimal)
                         if config_path else {})
        if not isinstance(config_values, dict):
            raise ValueError("config_must_be_object")
        config = DetectorConfig(**config_values)
        results = replay(load_fixture(path), config)
    except (OSError, ValueError, TypeError):
        print(json.dumps({"event": "replay_error", "reason": "invalid_fixture_or_config",
                          "detector_version": DETECTOR_VERSION}))
        return 2
    for result in results:
        if result["event"] != "evaluation":
            print(json.dumps(result, sort_keys=True, allow_nan=False))
    sources = sorted({(r["provider"], r["feed"], r["coverage_class"]) for r in results})
    print(json.dumps({"event": "replay_summary", "detector_version": DETECTOR_VERSION,
                      "config": config.snapshot(), "bars_evaluated": len(results),
                      "detections": sum(r["detected"] for r in results),
                      "candidate_rejections": sum(r["event"] == "candidate_rejection" for r in results),
                      "sources": [{"provider": p, "feed": f, "coverage_class": c} for p, f, c in sources]},
                     sort_keys=True))
    return 0
