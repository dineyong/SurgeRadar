"""Entitlement-gated screener boundary and deterministic IEX symbol selection."""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

UNIVERSE_VERSION = "v0.2-verified-common-stock"
ENDPOINTS = {
    "actives": "https://data.alpaca.markets/v1beta1/screener/stocks/most-actives?top=100&by=volume",
    "movers": "https://data.alpaca.markets/v1beta1/screener/stocks/movers?top=50",
}
LIMITATIONS = ["screener_access_not_assumed", "screener_uses_sip_based_data",
               "monitoring_is_single_exchange_iex", "max_30_explicit_symbols",
               "missing_asset_metadata_not_verified", "may_include_previous_session_movers",
               "ranking_not_predictive", "no_paid_fallback", "verified_common_stocks_only",
               "asset_name_classification_conservative", "us_equity_does_not_prove_us_domicile"]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never forward auth headers to a redirected endpoint


class ScreenerProvider(Protocol):
    def fetch(self, source: str) -> tuple[int | None, object, dict]: ...


class AlpacaScreener:
    def __init__(self, key, secret):
        self.key, self.secret = key, secret

    def fetch(self, source):
        return self.fetch_url(ENDPOINTS[source])

    def fetch_url(self, url, maximum_bytes=1_048_576):
        request = Request(url, headers={"APCA-API-KEY-ID": self.key,
                                                   "APCA-API-SECRET-KEY": self.secret,
                                                   "Accept": "application/json"})
        try:
            with build_opener(NoRedirect()).open(request, timeout=10) as response:
                # Both endpoints are bounded lists; bound response memory too.
                raw = response.read(maximum_bytes + 1)
                if len(raw) > maximum_bytes:
                    return response.status, None, {}
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeError):
                    body = None
                return response.status, body, {}
        except HTTPError as exc:
            headers = dict(exc.headers) if exc.headers else {}
            exc.close()
            return exc.code, None, headers
        except (URLError, OSError, ValueError, HTTPException):
            return None, None, {}  # no upstream exception/body can leak credentials


def parse_source(source, body):
    if not isinstance(body, dict):
        raise ValueError("malformed_screener_response")
    if source == "movers" and body.get("market_type", "stocks") != "stocks":
        raise ValueError("non_stock_screener_response")
    rows = body.get("gainers" if source == "movers" else "most_actives")
    maximum = 50 if source == "movers" else 100
    if not isinstance(rows, list) or len(rows) > maximum:
        raise ValueError("malformed_screener_response")
    metric_name = "percent_change" if source == "movers" else "volume"
    metrics = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("malformed_screener_response")
        symbol = row.get("symbol")
        value = row.get(metric_name)
        if (not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9./-]{0,19}", symbol)
                or isinstance(value, bool) or not isinstance(value, (str, int, float))):
            raise ValueError("malformed_screener_response")
        try:
            value = Decimal(str(value))
        except InvalidOperation:
            raise ValueError("malformed_screener_response") from None
        if not value.is_finite() or value < 0:
            raise ValueError("malformed_screener_response")
        # Top gainers only; zero change provides no gainer-discovery signal.
        if source == "movers" and value == 0:
            continue
        metrics[symbol] = max(value, metrics.get(symbol, value))
    ordered = sorted(metrics, key=lambda symbol: (-Fraction(metrics[symbol]), symbol))
    return [{"symbol": symbol, "rank": rank, "metric": str(metrics[symbol])}
            for rank, symbol in enumerate(ordered, 1)]


def probe(provider):
    statuses, sources = [], {}
    stopped = False
    for source in ENDPOINTS:
        if stopped:
            statuses.append({"source": source, "endpoint": ENDPOINTS[source], "http_status": None,
                             "access": "not_attempted", "reason": "stopped_after_rate_limit"})
            continue
        status, body, headers = provider.fetch(source)
        access = {401: "credentials_rejected", 403: "forbidden", 429: "rate_limited"}.get(status,
                  "transport_error" if status is None else "http_error")
        row = {"source": source, "endpoint": ENDPOINTS[source], "http_status": status,
               "access": access, "usable": False, "symbol_count": 0}
        if status == 200:
            try:
                parsed = parse_source(source, body)
            except ValueError:
                row["access"] = "malformed_response"
            else:
                sources[source] = parsed
                row.update(access="accessible", usable=bool(parsed), symbol_count=len(parsed))
        if status == 429:
            retry_after = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
            row["retry_after_seconds"] = int(retry_after) if isinstance(retry_after, str) and re.fullmatch(r"[0-9]{1,9}", retry_after) else None
            stopped = True
        statuses.append(row)
    return statuses, sources


@dataclass(frozen=True)
class SelectionConfig:
    mover_weight: int = 1
    active_weight: int = 1
    limit: int = 30

    def __post_init__(self):
        if (type(self.mover_weight) is not int or type(self.active_weight) is not int or
                min(self.mover_weight, self.active_weight) < 1 or
                type(self.limit) is not int or not 1 <= self.limit <= 30):
            raise ValueError("invalid_selection_config")


def rank_candidates(sources, config=None, assets=None, *, apply_limit=True):
    config = config or SelectionConfig()
    assets = assets or {}
    merged = {}
    for source, rows in sources.items():
        weight = config.mover_weight if source == "movers" else config.active_weight
        for row in rows:
            symbol = row["symbol"]
            metadata = assets.get(symbol)
            if metadata is not None:
                if not isinstance(metadata, dict):
                    raise ValueError("invalid_asset_metadata")
                if (metadata.get("class", "us_equity") != "us_equity" or
                        metadata.get("status", "active") != "active" or metadata.get("tradable", True) is not True):
                    continue
            item = merged.setdefault(symbol, {"symbol": symbol, "score": Fraction(0),
                                              "mover_rank": None, "active_rank": None,
                                              "reasons": [], "asset_metadata_verified": False})
            item["score"] += Fraction(weight, row["rank"])
            item["mover_rank" if source == "movers" else "active_rank"] = row["rank"]
            item["reasons"].append("top_gainer" if source == "movers" else "most_active_by_volume")
            item["asset_metadata_verified"] = metadata is not None and all(k in metadata for k in ("class", "status", "tradable"))
    ranked = sorted(merged.values(), key=lambda item: (-item["score"], item["symbol"]))
    if apply_limit:
        ranked = ranked[:config.limit]
    return [{**item, "score": str(item["score"]), "reasons": sorted(item["reasons"])} for item in ranked]


def filtered_universe(sources, config=None, assets=None):
    from collections import Counter
    from .assets import classify
    config = config or SelectionConfig()
    ranked = rank_candidates(sources, config, apply_limit=False)
    assessments = []
    for item in ranked:
        classification = classify(item["symbol"], (assets or {}).get(item["symbol"]))
        assessments.append({**item, **classification,
                            "asset_metadata_verified": classification["classification"] == "common_stock_verified"})
    eligible = [item for item in assessments if item["classification"] == "common_stock_verified"]
    removed = [item for item in assessments if item["classification"] != "common_stock_verified"]
    return {"candidates": [{**item, "asset_metadata_verified": True} for item in eligible[:config.limit]],
            "assessments": assessments, "excluded_count": len(removed),
            "excluded_breakdown": dict(sorted(Counter(reason for item in removed for reason in item["filter_reasons"]).items())),
            "metadata_coverage": {"total_candidates": len(assessments),
                "common_stock_verified": len(eligible),
                "excluded_with_reason": sum(item["classification"] == "excluded_with_reason" for item in assessments),
                "metadata_unverified": sum(item["classification"] == "metadata_unverified" for item in assessments)}}


def select(sources, config=None, assets=None):
    return filtered_universe(sources, config, assets)["candidates"]


def main(mode, fixture=None):
    from .logging import JsonLog
    from .assets import AlpacaAssets, classify, probe_assets
    key, secret = os.environ.get("APCA_API_KEY_ID", "").strip(), os.environ.get("APCA_API_SECRET_KEY", "").strip()
    log = JsonLog(secrets=(key, secret))
    log.context.update(feed="sip_based_screener", coverage_class="discovery_not_stream_coverage",
                       api_version="v1beta1", entitlement="unverified", limitations=LIMITATIONS)
    if mode == "asset-check":
        log.context.update(feed="asset_master", api_version="v2")
    if mode == "universe" and not fixture and os.environ.get("SURGERADAR_DISCOVERY_ENABLED") != "true":
        log.emit("universe_disabled", discovery_enabled=False, reason="run_universe_check_first_then_explicitly_enable")
        return 2
    try:
        config = (SelectionConfig() if mode != "universe" else
                  SelectionConfig(mover_weight=int(os.environ.get("SURGERADAR_UNIVERSE_MOVER_WEIGHT", "1")),
                                  active_weight=int(os.environ.get("SURGERADAR_UNIVERSE_ACTIVE_WEIGHT", "1"))))
        assets = {}
        asset_status = {"access": "fixture_only", "http_status": None}
        if fixture:
            body = json.loads(Path(fixture).read_text(encoding="utf-8"))
            sources = {s: parse_source(s, body[s]) for s in ENDPOINTS if s in body}
            assets = body.get("assets", {})
            if not isinstance(assets, dict):
                raise ValueError("invalid_asset_metadata")
            statuses = []
        else:
            if not key or not secret or any(c in key + secret for c in "\r\n"):
                log.emit("asset_check" if mode == "asset-check" else "universe_check",
                         discovery_enabled=False, reason="credentials_missing_or_invalid", sources=[])
                return 2
            statuses, sources = ([], {}) if mode == "asset-check" else probe(AlpacaScreener(key, secret))
            asset_provider = AlpacaAssets(key, secret, os.environ.get("SURGERADAR_ASSET_ENVIRONMENT", "paper"))
            if any(row["access"] == "rate_limited" for row in statuses):
                asset_status = {"access": "not_attempted", "http_status": None, "reason": "stopped_after_rate_limit"}
            else:
                asset_status, assets = probe_assets(asset_provider)
            asset_status["endpoint"] = asset_provider.endpoint
            common_count = sum(classify(symbol, metadata)["classification"] == "common_stock_verified"
                               for symbol, metadata in assets.items())
            log.emit("asset_check", feed="asset_master", api_version="v2", asset_metadata=asset_status,
                     common_stock_verified_count=common_count,
                     common_stock_filtering_usable=asset_status["access"] == "accessible" and common_count > 0)
            if mode == "asset-check":
                return 0 if asset_status["access"] == "accessible" else 4
            log.context["entitlement"] = ("screener_access_verified" if any(row["access"] == "accessible" for row in statuses)
                                          else "forbidden" if all(row["access"] == "forbidden" for row in statuses)
                                          else "unverified")
        blocked_by_rate_limit = any(row["access"] == "rate_limited" for row in statuses)
        blocked_by_rate_limit = blocked_by_rate_limit or asset_status["access"] == "rate_limited"
        available = any(sources.values()) and not blocked_by_rate_limit
        filtering_usable = available and bool(select(sources, config, assets))
        log.emit("universe_check", discovery_available=filtering_usable and not bool(fixture),
                 discovery_enabled=filtering_usable and not bool(fixture) and os.environ.get("SURGERADAR_DISCOVERY_ENABLED") == "true",
                 access_verified=not bool(fixture) and any(row["access"] == "accessible" for row in statuses),
                 reason="fixture_only" if fixture else "sources_available" if available else "no_usable_source",
                 sources=statuses, asset_metadata=asset_status,
                 common_stock_filtering_usable=filtering_usable)
        if mode == "universe-check":
            return 0 if any(row["access"] == "accessible" for row in statuses) and not blocked_by_rate_limit else 4
        if not available:
            log.emit("universe_disabled", discovery_enabled=False, reason="no_usable_source")
            return 4
        filtered = filtered_universe(sources, config, assets)
        selected = filtered.pop("candidates")
        log.emit("universe", generated_at=datetime.now(timezone.utc).isoformat(), universe_version=UNIVERSE_VERSION,
                 symbols=[item["symbol"] for item in selected], count=len(selected), candidates=selected,
                 config={"mover_weight": config.mover_weight, "active_weight": config.active_weight,
                         "limit": config.limit, "score_formula": "mover_weight/mover_rank + active_weight/active_rank"},
                 fixture_only=bool(fixture), discovery_enabled=not bool(fixture) and bool(selected), monitoring_feed="iex",
                 asset_metadata=asset_status, **filtered)
        return 0 if selected else 4 if asset_status["access"] not in ("accessible", "fixture_only") else 5
    except (OSError, ValueError, TypeError, AttributeError):
        log.emit("universe_error", reason="invalid_fixture_or_selection_config", discovery_enabled=False)
        return 2
