import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
import io
import json
import os
import unittest
from unittest.mock import patch

from surgeradar.cli import main
from surgeradar.config import Config, ConfigError
from surgeradar.logging import JsonLog
from surgeradar.metrics import Metrics, percentile
from surgeradar.model import Malformed, normalize_bar, parse_frame
from surgeradar.providers import Alpaca, ProviderError
from surgeradar.runner import run

BAR = {"T": "b", "S": "AAPL", "t": "2026-10-01T14:30:00Z",
       "o": 100, "h": 102, "l": 99, "c": 101, "v": 1234, "vw": 100.5, "n": 30}
ENV = {"SURGERADAR_FEED": "sip", "APCA_API_KEY_ID": "fake-key",
       "APCA_API_SECRET_KEY": "fake-secret"}


class ParsingTests(unittest.TestCase):
    def test_normalization_and_end_lag(self):
        bar = normalize_bar(BAR, datetime(2026, 10, 1, 14, 31, 2, tzinfo=timezone.utc))
        self.assertEqual(bar.close, Decimal("101"))
        self.assertEqual(bar.volume, Decimal("1234"))
        self.assertEqual(bar.trade_count, 30)
        self.assertEqual(bar.lag_seconds, 2)
        self.assertEqual(bar.feed, "sip")

    def test_optional_fields(self):
        item = {k: v for k, v in BAR.items() if k not in ("vw", "n")}
        bar = normalize_bar(item, datetime.now(timezone.utc))
        self.assertIsNone(bar.vwap)
        self.assertIsNone(bar.trade_count)

    def test_bad_frames(self):
        for raw in ("junk", "{}", "[]", "null", b"\xff"):
            with self.subTest(raw=raw), self.assertRaises(Malformed):
                parse_frame(raw)

    def test_batched_frame(self):
        self.assertEqual(len(parse_frame(json.dumps([BAR, {"T": "q"}]))), 2)

    def test_bad_fields(self):
        for key, value in (("c", float("nan")), ("v", -1), ("n", True),
                           ("n", 3.5), ("h", 98), ("S", "secret\ntext"),
                           ("t", "2026-10-01T14:30:00"),
                           ("t", "2026-10-01T14:30:01Z"), ("o", False)):
            with self.subTest(key=key, value=value), self.assertRaises(Malformed):
                normalize_bar({**BAR, key: value}, datetime.now(timezone.utc))

    def test_fractional_volume_and_offset(self):
        item = {**BAR, "v": "12.5", "t": "2026-10-01T10:30:00-04:00"}
        bar = normalize_bar(item, datetime.now(timezone.utc))
        self.assertEqual(bar.volume, Decimal("12.5"))
        self.assertEqual(bar.event_at.hour, 14)

    def test_negative_lag_preserved_for_clock_diagnostics(self):
        bar = normalize_bar(BAR, datetime(2026, 10, 1, 14, 30, tzinfo=timezone.utc))
        self.assertEqual(bar.lag_seconds, -60)


class ConfigTests(unittest.TestCase):
    def test_explicit_sip_and_secret_repr(self):
        config = Config.from_env(ENV)
        self.assertNotIn("fake-key", repr(config))
        self.assertNotIn("fake-secret", repr(config))

    def test_reject_feed_without_fallback(self):
        for feed in ("", "iex", "delayed_sip", "test", "unknown"):
            with self.subTest(feed=feed), self.assertRaises(ConfigError):
                Config.from_env({**ENV, "SURGERADAR_FEED": feed})

    def test_missing_credentials(self):
        for key in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
            with self.subTest(key=key), self.assertRaises(ConfigError):
                Config.from_env({**ENV, key: ""})

    def test_numeric_validation(self):
        for key, value in (("SURGERADAR_DURATION_SECONDS", "NaN"),
                           ("SURGERADAR_REPORT_SECONDS", "0"),
                           ("SURGERADAR_QUEUE_SIZE", "-1"),
                           ("SURGERADAR_MAX_RECONNECTS", "1.5")):
            with self.subTest(key=key), self.assertRaises(ConfigError):
                Config.from_env({**ENV, key: value})

    def test_symbols(self):
        config = Config.from_env({**ENV, "SURGERADAR_SYMBOLS": "aapl,MSFT,AAPL"})
        self.assertEqual(config.symbols, ("AAPL", "MSFT"))
        for symbols in ("*", "AAPL,", "AAPL, secret:blah"):
            with self.subTest(symbols=symbols), self.assertRaises(ConfigError):
                Config.from_env({**ENV, "SURGERADAR_SYMBOLS": symbols})

    def test_cli_missing_env_no_network(self):
        with patch.dict(os.environ, {}, clear=True), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(["check"]), 2)
            self.assertEqual(json.loads(output.getvalue())["event"], "configuration_blocked")


class LoggingMetricsTests(unittest.TestCase):
    def test_secrets_redacted(self):
        output = io.StringIO()
        log = JsonLog(output, ("fake-secret", 'key"quoted'))
        log.emit("test", detail='fake-secret key"quoted')
        value = output.getvalue()
        self.assertNotIn("fake-secret", value)
        self.assertNotIn('key\\"quoted', value)
        self.assertEqual(json.loads(value)["provider"], "alpaca")

    def test_percentiles_and_empty(self):
        self.assertIsNone(percentile([], .5))
        self.assertEqual(percentile([4, 1, 3, 2], .5), 2.5)
        self.assertEqual(percentile([5], .99), 5)

    def test_minute_counts(self):
        metrics = Metrics()
        for _ in range(2):
            metrics.observe(normalize_bar(BAR, datetime.now(timezone.utc)))
        metrics.observe(normalize_bar({**BAR, "S": "MSFT"}, datetime.now(timezone.utc)))
        output = io.StringIO()
        metrics.report(JsonLog(output), 0, 0, final=True)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(rows[0]["bars_received"], 3)
        minute = next(row for row in rows if row["event"] == "event_minute")
        self.assertEqual(minute["bars_per_minute"], 3)
        self.assertEqual(minute["unique_symbols_per_minute"], 2)


class FakeSocket:
    def __init__(self, frames, *, fail_after=False):
        self.frames = list(frames)
        self.sent = []
        self.fail_after = fail_after

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        if self.frames:
            await asyncio.sleep(0)
            return json.dumps(self.frames.pop(0))
        if self.fail_after:
            raise OSError("do not log fake-secret upstream exception")
        await asyncio.Future()


class FakeProvider:
    def __init__(self, config, sockets):
        self.alpaca = Alpaca(config)
        self.sockets = iter(sockets)
        self.opens = 0

    @asynccontextmanager
    async def open(self):
        self.opens += 1
        yield next(self.sockets)

    def authentication(self):
        return self.alpaca.authentication()

    def subscription(self, symbols):
        return self.alpaca.subscription(symbols)


def controls(symbols=("SPY", "AAPL", "MSFT")):
    return [[{"T": "success", "msg": "connected"}],
            [{"T": "success", "msg": "authenticated"}],
            [{"T": "subscription", "bars": list(symbols)}]]


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def execute(self, frames, mode="smoke", **config_values):
        config = Config(**{"key": "fake-key", "secret": "fake-secret", "duration": .08,
                           "report_seconds": .02, "max_reconnects": 0, **config_values})
        socket = FakeSocket(frames)
        output = io.StringIO()
        provider = FakeProvider(config, [socket])
        result = await run(config, provider, JsonLog(output, (config.key, config.secret)), mode)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertNotIn(config.secret, output.getvalue())
        return result, rows, socket, provider

    async def test_check_auth_and_subscription_order(self):
        result, rows, socket, _ = await self.execute(controls(), mode="check")
        self.assertEqual(result, 0)
        self.assertEqual([m["action"] for m in socket.sent], ["auth", "subscribe"])
        self.assertEqual(socket.sent[-1]["bars"], ["SPY", "AAPL", "MSFT"])
        check = next(r for r in rows if r["event"] == "check_complete")
        self.assertFalse(check["ingestion_verified"])

    async def test_entitlement_denied_does_not_fallback(self):
        frames = [controls()[0], [{"T": "error", "code": 409, "msg": "fake-secret"}]]
        result, rows, socket, provider = await self.execute(frames, max_reconnects=3)
        self.assertEqual(result, 4)
        self.assertEqual(provider.opens, 1)
        self.assertEqual(len(socket.sent), 1)
        self.assertFalse(next(r for r in rows if r["event"] == "provider_error")["sip_entitled"])

    async def test_bad_credentials_distinct_from_entitlement(self):
        result, rows, _, _ = await self.execute(
            [controls()[0], [{"T": "error", "code": 402}]], mode="check")
        self.assertEqual(result, 3)
        self.assertEqual(next(r for r in rows if r["event"] == "provider_error")["reason"],
                         "credentials_rejected")

    async def test_ack_mismatch_fails(self):
        frames = controls()
        frames[-1] = [{"T": "subscription", "bars": ["AAPL"]}]
        result, rows, _, _ = await self.execute(frames)
        self.assertEqual(result, 3)

    async def test_smoke_receives_and_ignores(self):
        result, rows, _, _ = await self.execute(
            controls() + [[BAR, {"T": "q"}, 42, {**BAR, "v": -1}]])
        self.assertEqual(result, 0)
        summary = [r for r in rows if r["event"] == "summary"][-1]
        self.assertEqual(summary["bars_received"], 1)
        self.assertEqual(summary["malformed_messages"], 2)
        self.assertEqual(summary["ignored_messages"], 1)
        self.assertTrue(any(r["event"] == "minute_bar" for r in rows))

    async def test_no_bars_is_not_success(self):
        result, rows, _, _ = await self.execute(controls())
        self.assertEqual(result, 5)

    async def test_broad_subscription_and_metrics(self):
        result, rows, socket, _ = await self.execute(controls(("*",)) + [[BAR]], mode="broad")
        self.assertEqual(result, 0)
        self.assertEqual(socket.sent[-1]["bars"], ["*"])
        self.assertFalse(any(r["event"] == "minute_bar" for r in rows))
        self.assertTrue(any(r["event"] == "event_minute" for r in rows))

    async def test_handshake_timeout(self):
        result, rows, _, _ = await self.execute([], mode="check", duration=.02)
        self.assertEqual(result, 3)
        self.assertTrue(any(r["event"] == "reconnect_exhausted" for r in rows))

    async def test_transport_exhaustion_redacts_exception(self):
        config = Config("fake-key", "fake-secret", duration=.08, max_reconnects=0)
        socket = FakeSocket(controls(), fail_after=True)
        output = io.StringIO()
        result = await run(config, FakeProvider(config, [socket]),
                           JsonLog(output), "smoke")
        self.assertEqual(result, 3)
        self.assertNotIn("fake-secret", output.getvalue())

    async def test_reconnect_reauthenticates(self):
        config = Config("fake-key", "fake-secret", duration=1.3, report_seconds=10,
                        max_reconnects=1)
        first = FakeSocket(controls(), fail_after=True)
        second = FakeSocket(controls() + [[BAR]])
        output = io.StringIO()
        result = await run(config, FakeProvider(config, [first, second]), JsonLog(output), "smoke")
        self.assertEqual(result, 0)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(any(r["event"] == "reconnected" for r in rows))
        self.assertFalse(next(r for r in rows if r["event"] == "smoke_result")["continuous"])
        self.assertEqual(second.sent[0]["action"], "auth")

    async def test_overflow_fails_explicitly(self):
        from surgeradar.runner import stream, DiagnosticError

        class BurstSocket:
            async def recv(self):
                return json.dumps([BAR])  # deliberate offline overload

        config = Config("fake-key", "fake-secret", duration=.03, queue_size=1)
        metrics = Metrics()
        output = io.StringIO()
        with self.assertRaises(DiagnosticError):
            await stream(BurstSocket(), metrics, JsonLog(output), config,
                         asyncio.get_running_loop().time()+.03, log_bars=False)
        self.assertGreater(metrics.overflow, 0)
        self.assertIn("queue_overflow", output.getvalue())

    async def test_loopback_websocket_protocol(self):
        from websockets.asyncio.server import serve
        from websockets.asyncio.client import connect

        async def server(socket):
            await socket.send(json.dumps(controls()[0]))
            auth = json.loads(await socket.recv())
            self.assertEqual(auth["action"], "auth")
            await socket.send(json.dumps(controls()[1]))
            subscribe = json.loads(await socket.recv())
            self.assertEqual(subscribe["bars"], ["SPY", "AAPL", "MSFT"])
            await socket.send(json.dumps(controls()[2]))
            await socket.send(json.dumps([BAR]))
            await socket.wait_closed()

        config = Config("fake-key", "fake-secret", duration=.15, report_seconds=.05)
        async with serve(server, "127.0.0.1", 0) as ws_server:
            port = ws_server.sockets[0].getsockname()[1]

            class LoopbackProvider(Alpaca):
                def open(self):
                    return connect(f"ws://127.0.0.1:{port}", proxy=None)

            output = io.StringIO()
            result = await run(config, LoopbackProvider(config), JsonLog(output), "smoke")
        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
