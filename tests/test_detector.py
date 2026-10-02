from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN, localcontext
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from surgeradar.cli import main
from surgeradar.detector import DETECTOR_VERSION, Detector, DetectorConfig
from surgeradar.replay import load_fixture, replay

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic_iex.jsonl"


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.bars = load_fixture(FIXTURE)
        self.results = replay(self.bars)

    def last(self, symbol):
        return [r for r in self.results if r["symbol"] == symbol][-1]

    def test_accelerating_surge_detects_with_exact_features(self):
        row = self.last("SURGE")
        self.assertTrue(row["detected"])
        self.assertEqual(row["failed_predicates"], [])
        self.assertEqual(row["detected_price"], "10.16")
        self.assertEqual(row["features"]["change_3m"], "0.016")
        self.assertEqual(row["features"]["change_5m"], "0.016")
        self.assertEqual(row["features"]["volume_1m"], "6000")
        self.assertEqual(row["features"]["volume_5m"], "30000")
        self.assertEqual(row["features"]["dollar_volume_5m"], "301140.00")
        self.assertEqual(row["features"]["change_1m"], str(Decimal("10.16") / Decimal("10.03") - 1))
        self.assertEqual(row["features"]["acceleration"],
                         str(Decimal("10.16") / Decimal("10.03") - 1 - Decimal("0.0015")))

    def test_steady_rise_fails_exact_momentum_and_acceleration(self):
        self.assertEqual(self.last("STEADY")["failed_predicates"], ["change_1m", "acceleration"])

    def test_weak_volume_fails_exact_volume_predicates(self):
        self.assertEqual(self.last("WEAKVOL")["failed_predicates"], ["volume_1m", "dollar_volume_5m"])

    def test_high_volume_without_momentum(self):
        self.assertEqual(self.last("NOMOM")["failed_predicates"], ["change_1m", "change_3m", "acceleration"])

    def test_under_dollar_fails_price(self):
        self.assertEqual(self.last("LOW")["failed_predicates"], ["price", "dollar_volume_5m"])

    def test_missing_history_cannot_trigger(self):
        self.assertEqual(self.last("GAP")["failed_predicates"],
                         ["contiguous_history", "change_3m", "change_5m", "acceleration", "dollar_volume_5m"])
        self.assertFalse(any(r["detected"] for r in self.results if r["symbol"] == "GAP"))

    def test_candidate_is_not_detection(self):
        row = self.last("STEADY")
        self.assertTrue(row["candidate"])
        self.assertFalse(row["detected"])
        self.assertEqual(row["event"], "candidate_rejection")

    def test_candidate_or_uses_either_horizon(self):
        row = self.last("NOMOM")
        self.assertLess(Decimal(row["features"]["change_1m"]), Decimal("0.004"))
        self.assertTrue(row["candidate"])  # three-minute path alone
        row = [r for r in self.results if r["symbol"] == "STEADY"][1]
        self.assertIsNone(row["features"]["change_3m"])
        self.assertTrue(row["candidate"])  # one-minute path alone

    def test_warmup_is_explicit_and_no_zero_imputation(self):
        first = self.results[0]
        self.assertFalse(first["predicates"]["contiguous_history"]["passed"])
        self.assertIsNone(first["features"]["change_1m"])
        self.assertIsNone(first["features"]["volume_5m"])

    def test_every_evaluation_has_metadata_config_and_predicates(self):
        for row in self.results:
            self.assertEqual(row["detector_version"], DETECTOR_VERSION)
            self.assertEqual(row["config"], DetectorConfig().snapshot())
            self.assertEqual(row["feed"], "iex")
            self.assertEqual(row["provider"], "alpaca")
            self.assertEqual(row["coverage_class"], "single_exchange_iex")
            self.assertEqual(len(row["predicates"]), 8)

    def test_configurable_thresholds(self):
        self.assertFalse(any(r["detected"] for r in replay(self.bars, DetectorConfig(min_price="100"))))

    def test_invalid_thresholds(self):
        for value in (True, "NaN", "Infinity", "-1", "garbage"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                DetectorConfig(min_price=value)

    def test_inclusive_thresholds_and_strict_change_5m(self):
        surge = [b for b in self.bars if b.ticker == "SURGE"]
        row = replay(surge)[-1]
        features = row["features"]
        config = DetectorConfig(min_price=row["detected_price"],
                                min_change_1m=features["change_1m"], min_change_3m=features["change_3m"],
                                min_acceleration=features["acceleration"], min_volume_1m=features["volume_1m"],
                                min_dollar_volume_5m=features["dollar_volume_5m"])
        self.assertTrue(replay(surge, config)[-1]["detected"])
        config = replace(config, min_change_5m=features["change_5m"])
        self.assertEqual(replay(surge, config)[-1]["failed_predicates"], ["change_5m"])

    def test_candidate_thresholds_do_not_gate_detection(self):
        rows = replay(self.bars, DetectorConfig(candidate_change_1m="10", candidate_change_3m="10"))
        surge = [r for r in rows if r["symbol"] == "SURGE"][-1]
        self.assertTrue(surge["detected"])
        self.assertFalse(surge["candidate"])

    def test_duplicate_and_out_of_order_rejected_by_core(self):
        bar = self.bars[0]
        for following in (bar, replace(bar, event_at=bar.event_at-timedelta(minutes=1))):
            detector = Detector()
            detector.evaluate(bar)
            with self.assertRaisesRegex(ValueError, "duplicate_or_out_of_order"):
                detector.evaluate(following)

    def test_feed_and_provider_state_isolation(self):
        bars = [b for b in self.bars if b.ticker == "SURGE"]
        detector = Detector()
        for i, bar in enumerate(bars):
            row = detector.evaluate(replace(bar, feed="iex" if i % 2 else "sip"))
            self.assertFalse(row["detected"])
        detector = Detector()
        for i, bar in enumerate(bars):
            row = detector.evaluate(replace(bar, provider="alpaca" if i % 2 else "synthetic"))
            self.assertFalse(row["detected"])

    def test_state_bounded_to_six_bars(self):
        bar = self.bars[0]
        detector = Detector()
        for i in range(100):
            detector.evaluate(replace(bar, event_at=bar.event_at+timedelta(minutes=i)))
        self.assertEqual(len(next(iter(detector.history.values()))), 6)


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.bars = load_fixture(FIXTURE)

    def test_sorted_interleaved_multi_symbol_determinism(self):
        expected = replay(self.bars)
        self.assertEqual(expected, replay(list(reversed(self.bars))))
        self.assertEqual(len({r["symbol"] for r in expected}), 6)
        self.assertEqual([r["feature_at"] for r in expected], sorted(r["feature_at"] for r in expected))

    def test_no_lookahead_prefix_stays_identical(self):
        cutoff = self.bars[0].event_at + timedelta(minutes=4)
        prefix = [b for b in self.bars if b.event_at <= cutoff]
        self.assertEqual(replay(prefix), [r for r in replay(self.bars)
                                         if r["feature_at"] <= cutoff.isoformat()])
        future = replace(self.bars[-1], event_at=cutoff+timedelta(days=1),
                         open=Decimal("9999"), high=Decimal("9999"), low=Decimal("9999"), close=Decimal("9999"))
        self.assertEqual(replay(prefix), replay(prefix+[future])[:-1])

    def test_replay_matches_per_symbol_core(self):
        rows = replay(self.bars)
        for symbol in {b.ticker for b in self.bars}:
            self.assertEqual([r for r in rows if r["symbol"] == symbol],
                             replay([b for b in self.bars if b.ticker == symbol]))

    def test_duplicate_fixture_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate_bar"):
            replay(self.bars+[self.bars[0]])

    def test_decimal_context_independent(self):
        expected = replay(self.bars)
        with localcontext() as context:
            context.prec = 5
            context.rounding = ROUND_DOWN
            self.assertEqual(replay(self.bars), expected)

    def test_cli_offline_no_credentials_no_network_and_repeatable_output(self):
        outputs = []
        with patch.dict(os.environ, {}, clear=True), patch("surgeradar.cli.Alpaca.open") as opened:
            for _ in range(2):
                with patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.assertEqual(main(["replay", str(FIXTURE)]), 0)
                    outputs.append(output.getvalue())
            opened.assert_not_called()
        self.assertEqual(outputs[0], outputs[1])
        rows = [json.loads(line) for line in outputs[0].splitlines()]
        self.assertEqual(rows[-1]["bars_evaluated"], 36)
        self.assertEqual(rows[-1]["detections"], 1)
        self.assertTrue(all(r["event"] in ("detection", "candidate_rejection", "replay_summary") for r in rows))

    def test_cli_config_override(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"min_price":"100"}', encoding="utf-8")
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(main(["replay", str(FIXTURE), "--config", str(path)]), 0)
            summary = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(summary["detections"], 0)
            self.assertEqual(summary["config"]["min_price"], "100")

    def test_invalid_rows_and_metadata_rejected(self):
        valid = json.loads(FIXTURE.read_text(encoding="utf-8").splitlines()[0])
        invalid = ["{}", "[]", "bad json", json.dumps({**valid, "feed": "unknown"}),
                   json.dumps({**valid, "provider": "unknown"}), json.dumps({**valid, "volume": -1}),
                   json.dumps({**valid, "event_at": "2026-10-01T14:30:00"}), ""]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.jsonl"
            for text in invalid:
                path.write_text(text, encoding="utf-8")
                with self.subTest(text=text), self.assertRaises(ValueError):
                    load_fixture(path)

    def test_cli_bad_config_no_partial_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            for text in ('{"unknown":1}', '[]', '{"min_price":"NaN"}', 'oops'):
                path.write_text(text, encoding="utf-8")
                with patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.assertEqual(main(["replay", str(FIXTURE), "--config", str(path)]), 2)
                self.assertEqual(json.loads(output.getvalue())["event"], "replay_error")


if __name__ == "__main__":
    unittest.main()
