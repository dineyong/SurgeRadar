"""Provider boundary and Alpaca control protocol; no business logic."""
from contextlib import AbstractAsyncContextManager
import json
import logging
from typing import Protocol
from .config import Config


class Socket(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...


class Provider(Protocol):
    def open(self) -> AbstractAsyncContextManager[Socket]: ...
    def authentication(self) -> str: ...
    def subscription(self, symbols: tuple[str, ...]) -> str: ...


class Alpaca:
    def __init__(self, config: Config):
        self.config = config

    def open(self):
        from websockets.asyncio.client import connect
        # Never let library debug logging emit the authentication frame.
        logger = logging.getLogger("surgeradar.websocket")
        logger.disabled = True
        return connect(f"wss://stream.data.alpaca.markets/v2/{self.config.feed}",
                       open_timeout=10, close_timeout=2,
                       ping_interval=20, ping_timeout=20,
                       max_queue=16, max_size=4 * 1024 * 1024, logger=logger)

    def authentication(self) -> str:
        return json.dumps({"action": "auth", "key": self.config.key, "secret": self.config.secret})

    def subscription(self, symbols: tuple[str, ...]) -> str:
        if self.config.feed == "iex" and (not symbols or "*" in symbols or len(symbols) > 30):
            from .config import ConfigError
            raise ConfigError("Basic/IEX requires 1-30 explicit symbols; wildcard is not supported")
        return json.dumps({"action": "subscribe", "bars": list(symbols)})


ERRORS = {
    400: "invalid_request", 401: "not_authenticated", 402: "credentials_rejected",
    403: "already_authenticated", 404: "authentication_timeout",
    405: "symbol_limit_exceeded", 406: "connection_limit_exceeded",
    407: "slow_client", 409: "sip_not_entitled", 410: "unsupported_channel",
    500: "provider_internal_error",
}


class ProviderError(Exception):
    def __init__(self, code: int | None):
        self.code = code
        self.reason = ERRORS.get(code, "provider_error")
        super().__init__(self.reason)

    @property
    def retryable(self) -> bool:
        return self.code in (407, 500)


def check_error(item: dict) -> None:
    if item.get("T") == "error":
        code = item.get("code")
        raise ProviderError(code if type(code) is int else None)
