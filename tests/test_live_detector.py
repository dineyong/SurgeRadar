import asyncio
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from surgeradar.cli import main
from surgeradar.config import Config
from surgeradar.detector import Detector
from surgeradar.logging import JsonLog
from surgeradar.model import MinuteBar
from surgeradar.providers import Alpaca
from surgeradar.replay import load_fixture, replay
from surgeradar.runner import run
from test_ingestion import FakeSocket, FakeProvider, controls, ENV

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic_iex.jsonl"


def payload(bar):
    return {"T": "b", "S": bar.ticker, "t": bar.event_at.isoformat(),
            "o": str(bar.open), "h": str(bar.high), "l": str(bar.low),
            "c": str(bar.close), "v": str(bar.volume)}


class LiveRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bars = load_fixture(FIXTURE)

    def symbols(self, symbol):
        return [bar for bar in self.bars if bar.ticker == symbol]

    async def execute(self, bars, *, mode="live-detect", extra=None):
        symbols = tuple(sorted({b.ticker for b in bars}))
        config = Config("fake-key", "fake-secret", feed="iex", symbols=symbols,
                        duration=.4, report_seconds=.05, max_reconnects=0)
        frames = controls(symbols) + [[payload(bar)] for bar in bars]
        if extra:
            frames.append(extra)
        output = io.StringIO()
        code = await run(config, FakeProvider(config, [FakeSocket(frames)]),
                         JsonLog(output, (config.key, config.secret)), mode)
        return code, [json.loads(line) for line in output.getvalue().splitlines()]

    def final(self, rows):
        return next(row for row in rows if row["event"] == "summary" and row["final"])

    async def test_normalized_bar_reaches_existing_detector(self):
        observed = []
        original = Detector.evaluate

        def evaluate(detector, bar):
            observed.append(bar)
            return original(detector, bar)

        with patch.object(Detector, "evaluate", evaluate):
            code, _ = await self.execute(self.symbols("SURGE"))
        self.assertEqual(code, 0)
        self.assertEqual(len(observed), 6)
        self.assertTrue(all(isinstance(bar, MinuteBar) for bar in observed))
        self.assertEqual(observed[-1].close, Decimal("10.16"))
        self.assertTrue(all(bar.feed == "iex" for bar in observed))

    async def test_exactly_one_detection_same_as_replay(self):
        code, rows = await self.execute(self.bars)
        self.assertEqual(code, 0)
        detections = [row for row in rows if row["event"] == "live_detection"]
        self.assertEqual(len(detections), 1)
        expected = next(row for row in replay(self.bars) if row["detected"])
        for key, value in expected.items():
            if key != "event":
                self.assertEqual(detections[0][key], value)
        self.assertEqual(self.final(rows)["detections"], 1)

    async def test_candidate_rejection_reasons_and_metadata(self):
        _, rows = await self.execute(self.symbols("STEADY"))
        row = [row for row in rows if row["event"] == "live_candidate_rejection"][-1]
        self.assertEqual(row["failed_predicates"], ["change_1m", "acceleration"])
        for key in ("features", "config", "predicates", "received_at", "processed_at", "receive_lag_seconds"):
            self.assertIn(key, row)
        self.assertEqual(row["detector_version"], "v0.1-no-rvol")
        self.assertEqual(row["provider"], "alpaca")
        self.assertEqual(row["feed"], "iex")
        self.assertEqual(row["coverage_class"], "single_exchange_iex")
        self.assertTrue(row["predicates"]["price"]["passed"])
        self.assertFalse(row["predicates"]["acceleration"]["passed"])

    async def test_non_candidate_suppressed_but_periodic_counters_present(self):
        _, rows = await self.execute(self.symbols("SURGE")[:5])
        self.assertFalse(any(row["event"] in ("evaluation", "minute_bar", "live_detection", "live_candidate_rejection") for row in rows))
        summaries = [row for row in rows if row["event"] == "summary"]
        self.assertTrue(any(not row["final"] for row in summaries))
        self.assertEqual(self.final(rows)["bars_evaluated"], 5)
        for summary in summaries:
            self.assertLessEqual(summary["bars_evaluated"], 5)
            self.assertEqual(summary["candidates"], 0)
            self.assertEqual(summary["detections"], 0)
            self.assertEqual(summary["candidate_rejections"], 0)
            self.assertLessEqual(summary["symbols_with_detector_state"], 1)
            self.assertEqual(summary["detector_errors"], 0)

    async def test_missing_minute_resets_symbol_history(self):
        _, rows = await self.execute(self.symbols("GAP"))
        final = self.final(rows)
        self.assertEqual(final["detections"], 0)
        self.assertEqual(final["history_gaps"], 1)
        self.assertEqual(final["detector_continuity"], "discontinuous")
        self.assertFalse(next(row for row in rows if row["event"] == "live_detect_result")["continuous"])
        row = next(row for row in rows if row["event"] == "live_candidate_rejection")
        self.assertIn("contiguous_history", row["failed_predicates"])
        self.assertEqual(row["predicates"]["contiguous_history"]["value"], "3")

    async def test_reconnect_resets_even_if_timestamps_are_contiguous(self):
        bars = self.symbols("SURGE")
        for error in (None, {"T": "error", "code": 407}):
            with self.subTest(error=error):
                config = Config("fake-key", "fake-secret", feed="iex", symbols=("SURGE",),
                                duration=1.3, report_seconds=.2, max_reconnects=1)
                first_frames = controls(config.symbols) + [[payload(bar)] for bar in bars[:5]]
                if error:
                    first_frames.append([error])
                first = FakeSocket(first_frames, fail_after=not error)
                second = FakeSocket(controls(config.symbols) + [[payload(bars[-1])]])
                output = io.StringIO()
                code = await run(config, FakeProvider(config, [first, second]), JsonLog(output), "live-detect")
                self.assertEqual(code, 0)
                rows = [json.loads(line) for line in output.getvalue().splitlines()]
                final = self.final(rows)
                self.assertEqual(final["bars_evaluated"], 6)
                self.assertEqual(final["detections"], 0)
                self.assertEqual(final["stream_discontinuities"], 1)
                self.assertEqual(final["reconnects"], 1)
                self.assertEqual(final["detector_continuity"], "discontinuous")
                self.assertTrue(any(row["event"] == "detector_state_reset" for row in rows))

    async def test_duplicate_bar_is_error_not_duplicate_detection(self):
        bars = self.symbols("SURGE")
        code, rows = await self.execute(bars+[bars[-1]])
        self.assertEqual(code, 3)
        final = self.final(rows)
        self.assertEqual(final["bars_evaluated"], 6)
        self.assertEqual(final["detections"], 1)
        self.assertEqual(final["detector_errors"], 1)
        self.assertEqual(next(row for row in rows if row["event"] == "detector_error")["reason"],
                         "duplicate_or_out_of_order_bar")

    async def test_malformed_and_other_channels_do_not_reach_detector(self):
        _, rows = await self.execute(self.symbols("SURGE")[:1],
                                     extra=[{"T": "q"}, {**payload(self.symbols("SURGE")[1]), "v": -1}, 42])
        final = self.final(rows)
        self.assertEqual(final["bars_evaluated"], 1)
        self.assertEqual(final["malformed_messages"], 2)
        self.assertEqual(final["ignored_messages"], 1)

    async def test_smoke_remains_without_detector(self):
        with patch.object(Detector, "evaluate") as evaluate:
            _, rows = await self.execute(self.symbols("SURGE"), mode="smoke")
            evaluate.assert_not_called()
        self.assertTrue(any(row["event"] == "minute_bar" for row in rows))
        self.assertNotIn("bars_evaluated", self.final(rows))

    async def test_detector_error_safe_and_history_cleared(self):
        with patch.object(Detector, "evaluate", side_effect=RuntimeError("fake-secret")):
            code, rows = await self.execute(self.symbols("SURGE")[:1])
        self.assertEqual(code, 3)
        self.assertEqual(self.final(rows)["detector_errors"], 1)
        self.assertEqual(self.final(rows)["symbols_with_detector_state"], 0)
        self.assertNotIn("fake-secret", json.dumps(rows))

    async def test_real_loopback_live_detect(self):
        from websockets.asyncio.client import connect
        from websockets.asyncio.server import serve
        bars = self.symbols("SURGE")
        config = Config("fake-key", "fake-secret", feed="iex", symbols=("SURGE",),
                        duration=.15, report_seconds=.03)

        async def server(socket):
            await socket.send(json.dumps(controls()[0]))
            self.assertEqual(json.loads(await socket.recv())["action"], "auth")
            await socket.send(json.dumps(controls()[1]))
            self.assertEqual(json.loads(await socket.recv())["bars"], ["SURGE"])
            await socket.send(json.dumps(controls(config.symbols)[2]))
            await socket.send(json.dumps([payload(bar) for bar in bars]))
            await socket.wait_closed()

        async with serve(server, "127.0.0.1", 0) as server_socket:
            port = server_socket.sockets[0].getsockname()[1]

            class LoopbackProvider(Alpaca):
                def open(self):
                    return connect(f"ws://127.0.0.1:{port}", proxy=None)

            output = io.StringIO()
            code = await run(config, LoopbackProvider(config), JsonLog(output), "live-detect")
        self.assertEqual(code, 0)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(self.final(rows)["detections"], 1)


class LiveConfigurationTests(unittest.TestCase):
    def test_live_iex_limit_still_rejected_before_network(self):
        env = {**ENV, "SURGERADAR_FEED": "iex", "SURGERADAR_SYMBOLS": ",".join(f"S{i}" for i in range(31))}
        with patch.dict(os.environ, env, clear=True), patch.object(Alpaca, "open") as opened, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(["live-detect"]), 2)
            opened.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())["feed"], "iex")

    def test_wildcard_rejected_for_live_iex(self):
        with patch.dict(os.environ, {**ENV, "SURGERADAR_FEED": "iex", "SURGERADAR_SYMBOLS": "*"}, clear=True), \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(["live-detect"]), 2)

    def test_cli_routes_to_live_runner_with_selected_iex(self):
        from unittest.mock import AsyncMock
        env = {**ENV, "SURGERADAR_FEED": "iex"}
        with patch.dict(os.environ, env, clear=True), patch("surgeradar.cli.run", new_callable=AsyncMock) as runner:
            runner.return_value = 0
            self.assertEqual(main(["live-detect"]), 0)
            self.assertEqual(runner.call_args.args[0].feed, "iex")
            self.assertEqual(runner.call_args.args[3], "live-detect")


if __name__ == "__main__":
    unittest.main()
