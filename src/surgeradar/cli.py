import argparse
import asyncio
import os
from .config import Config, ConfigError
from .logging import JsonLog
from .providers import Alpaca
from .runner import run


def main(argv=None):
    parser = argparse.ArgumentParser(description="Alpaca ingestion diagnostics and offline detector replay")
    parser.add_argument("mode", choices=("check", "smoke", "broad", "replay", "live-detect", "universe-check", "universe", "asset-check"),
                        help="feed diagnostics, offline replay, or live minute-bar detector")
    parser.add_argument("fixture", nargs="?", help="local JSONL fixture for replay")
    parser.add_argument("--config", help="JSON detector threshold overrides for replay")
    parser.add_argument("--universe-fixture", help="local JSON screener fixture; universe mode only")
    args = parser.parse_args(argv)
    if args.mode in ("universe-check", "universe", "asset-check"):
        if args.fixture or args.config or (args.mode != "universe" and args.universe_fixture):
            parser.error("universe modes accept only --universe-fixture with universe")
        from .universe import main as universe_main
        return universe_main(args.mode, args.universe_fixture)
    if args.universe_fixture:
        parser.error("--universe-fixture is universe-only")
    if args.mode == "replay":
        if not args.fixture:
            parser.error("replay requires a local JSONL fixture")
        from .replay import main as replay_main
        return replay_main(args.fixture, args.config)
    if args.fixture or args.config:
        parser.error("fixture and --config are replay-only")
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
