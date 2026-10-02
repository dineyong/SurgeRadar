from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from typing import Any


class Malformed(ValueError):
    pass


def parse_frame(raw: str | bytes) -> list[Any]:
    try:
        items = json.loads(raw)
    except (ValueError, UnicodeError):
        raise Malformed("invalid_json") from None
    if not isinstance(items, list) or not items:
        raise Malformed("expected_nonempty_array")
    return items


def number(item: dict, key: str, *, optional: bool = False) -> Decimal | None:
    value = item.get(key)
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise Malformed("invalid_number")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise Malformed("invalid_number") from None
    if not result.is_finite() or result < 0:
        raise Malformed("invalid_number")
    return result


def timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise Malformed("invalid_timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Malformed("invalid_timestamp") from None
    if result.tzinfo is None:
        raise Malformed("timestamp_requires_timezone")
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class MinuteBar:
    ticker: str
    event_at: datetime  # provider minute START
    received_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    vwap: Decimal | None
    trade_count: int | None
    provider: str = "alpaca"
    feed: str = "sip"

    @property
    def coverage_class(self):
        return "single_exchange_iex" if self.feed == "iex" else "consolidated_sip"

    @property
    def lag_seconds(self) -> float:
        # A completed minute bar normally arrives after its end, not its start.
        return (self.received_at - self.event_at - timedelta(minutes=1)).total_seconds()


def normalize_bar(item: Any, received_at: datetime, *, feed: str = "sip") -> MinuteBar:
    if feed not in ("iex", "sip"):
        raise Malformed("invalid_feed")
    if not isinstance(item, dict) or item.get("T") != "b":
        raise Malformed("not_minute_bar")
    symbol = item.get("S")
    # Bound untrusted strings before logging. Do not emit arbitrary provider text.
    import re
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9./-]{0,19}", symbol):
        raise Malformed("invalid_symbol")
    start = timestamp(item.get("t"))
    if start.second or start.microsecond:
        raise Malformed("unaligned_minute")
    if received_at.tzinfo is None:
        raise Malformed("receive_timestamp_requires_timezone")
    o, h, low, c = (number(item, k) for k in ("o", "h", "l", "c"))
    if min(o, h, low, c) <= 0 or low > min(o, c) or h < max(o, c) or low > h:
        raise Malformed("invalid_ohlc")
    n = item.get("n")
    if n is not None and (isinstance(n, bool) or not isinstance(n, int) or n < 0):
        raise Malformed("invalid_trade_count")
    return MinuteBar(symbol, start, received_at.astimezone(timezone.utc),
                     o, h, low, c, number(item, "v"), number(item, "vw", optional=True), n,
                     feed=feed)
