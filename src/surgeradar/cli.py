import argparse
import asyncio
import os
from .config import Config, ConfigError
from .logging import JsonLog
from .providers import Alpaca
from .runner import run


def main(argv=None):
    parser = argparse.ArgumentParser(description="Alpaca IEX/SIP ingestion diagnostics (no detector)")
    parser.add_argument("mode", choices=("check", "smoke", "broad"),
                        help="selected-feed check, small symbols, or SIP-only all-symbol benchmark")
    args = parser.parse_args(argv)
    log = JsonLog(secrets=(os.environ.get("APCA_API_KEY_ID", ""),
                          os.environ.get("APCA_API_SECRET_KEY", "")))
    selected = os.environ.get("SURGERADAR_FEED", "").strip()
    log.context.update(feed=selected if selected in ("iex", "sip") else "invalid",
                       requested_symbol_count=None if args.mode == "broad" and selected == "sip" else
                       len({s.strip().upper() for s in os.environ.get("SURGERADAR_SYMBOLS", "SPY,AAPL,MSFT").split(",")}),
                       coverage_class="single_exchange_iex" if selected == "iex" else
                       "consolidated_sip" if selected == "sip" else "unknown",
                       limitations=["iex_only", "not_consolidated_volume", "max_30_symbols", "no_wildcard"]
                       if selected == "iex" else ["requires_sip_entitlement"] if selected == "sip" else [])
    if args.mode == "broad" and selected == "iex":
        log.emit("configuration_blocked", reason="broad not supported on Basic/IEX")
        return 2
    try:
        config = Config.from_env()
    except ConfigError as exc:
        log.emit("configuration_blocked", reason=str(exc))
        return 2
    log.configure(config, args.mode)
    try:
        return asyncio.run(run(config, Alpaca(config), log, args.mode))
    except KeyboardInterrupt:
        log.emit("interrupted")
        return 130
    except Exception as exc:
        # Never print exception strings/tracebacks potentially containing auth payloads.
        log.emit("unexpected_failure", error_type=type(exc).__name__)
        return 3
