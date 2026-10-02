from dataclasses import dataclass, field
import math
import os
import re
from typing import Mapping


class ConfigError(ValueError):
    """A safe, static configuration error."""


def positive_number(env: Mapping[str, str], key: str, default: float) -> float:
    try:
        value = float(env.get(key, str(default)))
    except ValueError:
        raise ConfigError(f"{key} must be a positive finite number") from None
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{key} must be a positive finite number")
    return value


def integer(env: Mapping[str, str], key: str, default: int, minimum: int) -> int:
    try:
        value = int(env.get(key, str(default)))
    except ValueError:
        raise ConfigError(f"{key} must be an integer >= {minimum}") from None
    if value < minimum:
        raise ConfigError(f"{key} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True)
class Config:
    key: str = field(repr=False)
    secret: str = field(repr=False)
    feed: str = "sip"
    symbols: tuple[str, ...] = ("SPY", "AAPL", "MSFT")
    duration: float = 180
    report_seconds: float = 10
    queue_size: int = 256
    max_reconnects: int = 3

    def __post_init__(self):
        if self.feed not in ("iex", "sip"):
            raise ConfigError("SURGERADAR_FEED must explicitly be iex or sip; no fallback")
        if self.feed == "iex" and (not self.symbols or "*" in self.symbols or len(self.symbols) > 30):
            raise ConfigError("Basic/IEX requires 1-30 explicit symbols; wildcard is not supported")

    @property
    def coverage_class(self):
        return "single_exchange_iex" if self.feed == "iex" else "consolidated_sip"

    @property
    def limitations(self):
        return (["iex_only", "not_consolidated_volume", "max_30_symbols", "no_wildcard"]
                if self.feed == "iex" else ["requires_sip_entitlement"])

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        feed = env.get("SURGERADAR_FEED", "").strip()
        if feed not in ("iex", "sip"):
            raise ConfigError("SURGERADAR_FEED must explicitly be iex or sip; no fallback")
        key = env.get("APCA_API_KEY_ID", "").strip()
        secret = env.get("APCA_API_SECRET_KEY", "").strip()
        if not key or not secret:
            raise ConfigError("APCA_API_KEY_ID and APCA_API_SECRET_KEY are required")
        if any(c in key + secret for c in "\r\n"):
            raise ConfigError("Credentials cannot contain line breaks")
        symbols = tuple(dict.fromkeys(
            s.strip().upper() for s in env.get("SURGERADAR_SYMBOLS", "SPY,AAPL,MSFT").split(",")
        ))
        if not symbols or any(not re.fullmatch(r"[A-Z][A-Z0-9./-]{0,19}", s) for s in symbols):
            raise ConfigError("SURGERADAR_SYMBOLS must contain explicit stock symbols; use broad for wildcard")
        return cls(key, secret, feed=feed, symbols=symbols,
                   duration=positive_number(env, "SURGERADAR_DURATION_SECONDS", 180),
                   report_seconds=positive_number(env, "SURGERADAR_REPORT_SECONDS", 10),
                   queue_size=integer(env, "SURGERADAR_QUEUE_SIZE", 256, 1),
                   max_reconnects=integer(env, "SURGERADAR_MAX_RECONNECTS", 3, 0))
