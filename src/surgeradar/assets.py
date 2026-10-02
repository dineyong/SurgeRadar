"""Conservative asset-master classification; absence of exclusions is not proof."""
import re
from typing import Protocol

ASSET_BASES = {"paper": "https://paper-api.alpaca.markets", "live": "https://api.alpaca.markets"}


class AssetProvider(Protocol):
    def fetch_active(self) -> tuple[int | None, object, dict]: ...


class AlpacaAssets:
    def __init__(self, key, secret, environment="paper"):
        if environment not in ASSET_BASES:
            raise ValueError("invalid_asset_environment")
        self.key, self.secret = key, secret
        self.endpoint = ASSET_BASES[environment] + "/v2/assets?status=active&asset_class=us_equity"
        self.cached = None

    def fetch_active(self):
        if self.cached is None:
            from .universe import AlpacaScreener
            self.cached = AlpacaScreener(self.key, self.secret).fetch_url(self.endpoint, 16_777_216)
        return self.cached


def asset_index(body):
    if not isinstance(body, list):
        raise ValueError("malformed_asset_response")
    result = {}
    for asset in body:
        if not isinstance(asset, dict):
            raise ValueError("malformed_asset_response")
        symbol = asset.get("symbol")
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9./-]{0,19}", symbol) or symbol in result:
            raise ValueError("malformed_asset_response")
        result[symbol] = asset
    return result


def probe_assets(provider):
    status, body, _ = provider.fetch_active()
    diagnostic = {"http_status": status, "access": {401: "credentials_rejected", 403: "forbidden", 429: "rate_limited"}.get(
                  status, "transport_error" if status is None else "http_error"), "asset_count": 0}
    assets = {}
    if status == 200:
        try:
            assets = asset_index(body)
        except ValueError:
            diagnostic["access"] = "malformed_response"
        else:
            diagnostic.update(access="accessible", asset_count=len(assets))
    return diagnostic, assets


def classify(symbol, metadata):
    summary = {}
    reasons = []
    if not isinstance(metadata, dict):
        # Only exclusion hints, never common-stock verification from a ticker.
        hint = ("warrant_suffix_hint" if re.search(r"(?:\.W|\.WS|W)$", symbol) else
                "right_suffix_hint" if re.search(r"(?:\.RT|\.R)$", symbol) else
                "unit_suffix_hint" if symbol.endswith(".U") else None)
        return {"classification": "metadata_unverified", "filter_reasons": ["metadata_missing"] + ([hint] if hint else []),
                "asset_summary": summary}
    for key in ("symbol", "class", "status", "tradable", "exchange", "name"):
        value = metadata.get(key)
        if isinstance(value, (str, bool)):
            summary[key] = value[:240] if isinstance(value, str) else value
    if "symbol" in metadata and metadata["symbol"] != symbol:
        return {"classification": "metadata_unverified", "filter_reasons": ["metadata_symbol_mismatch"], "asset_summary": summary}
    if metadata.get("class") is not None and metadata["class"] != "us_equity":
        reasons.append("non_us_equity")
    if metadata.get("status") is not None and metadata["status"] != "active":
        reasons.append("inactive")
    if metadata.get("tradable") is False:
        reasons.append("untradable")
    name = metadata.get("name", "")
    name = name if isinstance(name, str) else ""
    # Alpaca's documented asset schema has no universal security subtype field.
    # Require affirmative ordinary/common-share language in the asset-master name.
    patterns = {
        "warrant": r"\bwarrants?\b", "right": r"\brights?\b", "unit": r"\bunits?\b",
        "etf": r"\b(etfs?|etps?|exchange[- ]traded|funds?|ishares|proshares|direxion|spdr|vanguard)\b",
        "leveraged_or_inverse": r"\b(leveraged|inverse|ultra(short)?|bull|bear|[23]x)\b",
        "preferred": r"\b(preferred|preference|depositary shares)\b",
        "depositary_receipt": r"\b(adrs?|adss?|american depositary|american depository|depositary receipts?)\b",
        "other_instrument": r"\b(notes?|bonds?|debentures?|etns?|beneficial interest|closed[- ]end)\b",
    }
    reasons.extend(reason for reason, pattern in patterns.items() if re.search(pattern, name, re.I))
    if reasons:
        return {"classification": "excluded_with_reason", "filter_reasons": reasons, "asset_summary": summary}
    complete = (metadata.get("class") == "us_equity" and metadata.get("status") == "active"
                and metadata.get("tradable") is True and metadata.get("exchange") in
                ("NASDAQ", "NYSE", "AMEX", "ARCA", "NYSEARCA", "BATS") and bool(name))
    affirmative = bool(re.search(r"\b(common (stock|shares)|ordinary shares)\b", name, re.I))
    if not complete or not affirmative:
        return {"classification": "metadata_unverified",
                "filter_reasons": ["metadata_incomplete" if not complete else "ordinary_common_stock_not_identified"],
                "asset_summary": summary}
    return {"classification": "common_stock_verified", "filter_reasons": [],
            "asset_summary": {**summary, "verification_basis": "asset_master_name_common_or_ordinary_shares"}}
