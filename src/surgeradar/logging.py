from datetime import datetime, timezone
import json
import sys
from typing import TextIO
from . import __version__


class JsonLog:
    def __init__(self, stream: TextIO | None = None, secrets: tuple[str, ...] = ()):
        self.stream = sys.stdout if stream is None else stream
        self.secrets = tuple(s for s in secrets if s)

    def emit(self, event: str, **fields) -> None:
        record = {"logged_at": datetime.now(timezone.utc).isoformat(),
                  "provider": "alpaca", "feed": "sip", "api_version": "v2",
                  "runner_version": __version__, "event": event, **fields}
        text = json.dumps(record, default=str, allow_nan=False)
        # Defense in depth. The caller never logs raw frames/auth/config/exceptions.
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
            escaped = json.dumps(secret)[1:-1]
            text = text.replace(escaped, "[REDACTED]")
        print(text, file=self.stream, flush=True)
