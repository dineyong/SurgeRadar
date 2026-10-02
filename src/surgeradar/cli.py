import argparse
import asyncio
import os
from .config import Config, ConfigError
from .logging import JsonLog
from .providers import Alpaca
from .runner import run


def main(argv=None):
    parser = argparse.ArgumentParser(description="Alpaca SIP ingestion diagnostics (no detector)")
    parser.add_argument("mode", choices=("check", "smoke", "broad"),
                        help="SIP auth/subscription check, small symbols, or all-symbol benchmark")
    args = parser.parse_args(argv)
    log = JsonLog(secrets=(os.environ.get("APCA_API_KEY_ID", ""),
                          os.environ.get("APCA_API_SECRET_KEY", "")))
    try:
        config = Config.from_env()
    except ConfigError as exc:
        log.emit("configuration_blocked", reason=str(exc))
        return 2
    try:
        return asyncio.run(run(config, Alpaca(config), log, args.mode))
    except KeyboardInterrupt:
        log.emit("interrupted")
        return 130
    except Exception as exc:
        # Never print exception strings/tracebacks potentially containing auth payloads.
        log.emit("unexpected_failure", error_type=type(exc).__name__)
        return 3
