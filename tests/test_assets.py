import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from surgeradar.assets import AlpacaAssets, asset_index, classify, probe_assets
from surgeradar.cli import main
from surgeradar.universe import ENDPOINTS, SelectionConfig, filtered_universe, parse_source, rank_candidates, select

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "common_stock_universe.json"


class AssetFilterTests(unittest.TestCase):
    def setUp(self):
        self.body = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.sources = {s: parse_source(s, self.body[s]) for s in ENDPOINTS}
        self.assets = self.body["assets"]

    def test_common_stock_included_with_evidence(self):
        row = classify("AAPL", self.assets["AAPL"])
        self.assertEqual(row["classification"], "common_stock_verified")
        self.assertIn("verification_basis", row["asset_summary"])

    def test_warrants_excluded_by_metadata(self):
        for symbol in ("VEEAW", "ARBEW", "ASTLW"):
            row = classify(symbol, self.assets[symbol])
            self.assertEqual(row["classification"], "excluded_with_reason")
            self.assertIn("warrant", row["filter_reasons"])

    def test_right_excluded(self):
        self.assertIn("right", classify("PNAQ.RT", self.assets["PNAQ.RT"])["filter_reasons"])

    def test_units_excluded(self):
        self.assertIn("unit", classify("PACK.U", self.assets["PACK.U"])["filter_reasons"])

    def test_etfs_excluded(self):
        for symbol in ("HYG", "TLT"):
            self.assertIn("etf", classify(symbol, self.assets[symbol])["filter_reasons"])
        for name in ("SPDR Synthetic Common Shares", "Vanguard Synthetic Common Shares"):
            self.assertIn("etf", classify("AAPL", {**self.assets["AAPL"], "name": name})["filter_reasons"])

    def test_leveraged_and_inverse_excluded(self):
        self.assertIn("leveraged_or_inverse", classify("SOXS", self.assets["SOXS"])["filter_reasons"])
        asset = {**self.assets["AAPL"], "name": "Synthetic Inverse ETF Common Shares"}
        self.assertIn("leveraged_or_inverse", classify("AAPL", asset)["filter_reasons"])
        self.assertNotEqual(classify("AAPL", asset)["classification"], "common_stock_verified")

    def test_inactive_excluded(self):
        self.assertEqual(classify("INACTIVE", self.assets["INACTIVE"])["filter_reasons"], ["inactive"])

    def test_untradable_excluded(self):
        self.assertEqual(classify("NOTRADE", self.assets["NOTRADE"])["filter_reasons"], ["untradable"])

    def test_non_equity_excluded(self):
        self.assertIn("non_us_equity", classify("CRYPTO", self.assets["CRYPTO"])["filter_reasons"])

    def test_unknown_name_not_accepted(self):
        self.assertEqual(classify("UNKNOWN", self.assets["UNKNOWN"])["classification"], "metadata_unverified")
        self.assertEqual(select(self.sources), [])

    def test_incomplete_metadata_not_accepted(self):
        for key in ("class", "status", "tradable", "exchange", "name"):
            asset = {k: v for k, v in self.assets["AAPL"].items() if k != key}
            self.assertEqual(classify("AAPL", asset)["classification"], "metadata_unverified")

    def test_symbol_mismatch_not_accepted(self):
        self.assertEqual(classify("AAPL", self.assets["MSFT"])["filter_reasons"], ["metadata_symbol_mismatch"])

    def test_ticker_suffix_hints_only_when_metadata_missing(self):
        for symbol, hint in (("ABCW", "warrant_suffix_hint"), ("ABC.RT", "right_suffix_hint"), ("ABC.U", "unit_suffix_hint")):
            row = classify(symbol, None)
            self.assertEqual(row["classification"], "metadata_unverified")
            self.assertIn(hint, row["filter_reasons"])
        asset = {**self.assets["AAPL"], "symbol": "WOW", "name": "Synthetic WOW Common Stock"}
        self.assertEqual(classify("WOW", asset)["classification"], "common_stock_verified")

    def test_other_instruments_excluded_even_with_common_words(self):
        for name, reason in (("Synthetic Preferred Common Stock", "preferred"),
                             ("Synthetic American Depositary Shares Common Stock", "depositary_receipt"),
                             ("Synthetic Exchange-Traded Notes Common Shares", "other_instrument")):
            row = classify("AAPL", {**self.assets["AAPL"], "name": name})
            self.assertIn(reason, row["filter_reasons"])
            self.assertEqual(row["classification"], "excluded_with_reason")

    def test_fewer_than_30_and_all_assessments_visible(self):
        result = filtered_universe(self.sources, assets=self.assets)
        self.assertEqual([row["symbol"] for row in result["candidates"]], ["AAPL", "MSFT"])
        self.assertEqual(result["excluded_count"], 13)
        self.assertEqual(len(result["assessments"]), 15)
        self.assertEqual(result["metadata_coverage"], {"total_candidates": 15, "common_stock_verified": 2,
                                                       "excluded_with_reason": 11, "metadata_unverified": 2})

    def test_ranking_preserved_after_filtering(self):
        expected = [row for row in rank_candidates(self.sources, apply_limit=False) if row["symbol"] in ("AAPL", "MSFT")]
        actual = select(self.sources, assets=self.assets)
        for a, e in zip(actual, expected):
            for key in ("symbol", "mover_rank", "active_rank", "score", "reasons"):
                self.assertEqual(a[key], e[key])

    def test_filter_before_cap_can_accept_rank_31(self):
        sources = {"actives": [{"symbol": f"S{i}", "rank": i+1, "metric": str(100-i)} for i in range(40)]}
        assets = {f"S{i}": {**self.assets["AAPL"], "symbol": f"S{i}",
                           "name": "Synthetic ETF" if i < 30 else "Synthetic Common Stock"} for i in range(40)}
        self.assertEqual(len(select(sources, assets=assets)), 10)
        self.assertEqual(select(sources, assets=assets)[0]["symbol"], "S30")
        for asset in assets.values():
            asset["name"] = "Synthetic Common Stock"
        self.assertEqual(len(select(sources, assets=assets)), 30)

    def test_determinism_and_weights_unchanged(self):
        expected = filtered_universe(self.sources, SelectionConfig(active_weight=3), self.assets)
        self.assertEqual(expected, filtered_universe(dict(reversed(list(self.sources.items()))),
                                                     SelectionConfig(active_weight=3), dict(reversed(list(self.assets.items())))))

    def test_safe_summary_not_full_payload(self):
        row = classify("AAPL", {**self.assets["AAPL"], "sensitive": "not-for-output"})
        self.assertNotIn("not-for-output", json.dumps(row))


class AssetProviderTests(unittest.TestCase):
    def test_bulk_cache_one_request_for_entire_command(self):
        provider = AlpacaAssets("key", "secret")
        with patch("surgeradar.universe.AlpacaScreener.fetch_url", return_value=(200, [], {})) as fetch:
            self.assertEqual(provider.fetch_active(), provider.fetch_active())
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(fetch.call_args.args[0], "https://paper-api.alpaca.markets/v2/assets?status=active&asset_class=us_equity")

    def test_environment_explicit_no_fallback(self):
        self.assertTrue(AlpacaAssets("key", "secret", "live").endpoint.startswith("https://api.alpaca.markets/"))
        with self.assertRaises(ValueError):
            AlpacaAssets("key", "secret", "https://evil.test")

    def test_malformed_and_duplicate_asset_master(self):
        for body in ({}, [None], [{"symbol": "AAA"}, {"symbol": "AAA"}], [{"symbol": "*"}]):
            with self.assertRaises(ValueError):
                asset_index(body)

    def test_denied_and_malformed_access(self):
        for status, body, access in ((403, None, "forbidden"), (429, None, "rate_limited"),
                                    (200, {}, "malformed_response"), (None, None, "transport_error")):
            with patch.object(AlpacaAssets, "fetch_active", return_value=(status, body, {})):
                diagnostic, assets = probe_assets(AlpacaAssets("key", "secret"))
            self.assertEqual(diagnostic["access"], access)
            self.assertEqual(assets, {})


class AssetCliTests(unittest.TestCase):
    def setUp(self):
        self.body = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def invoke(self, args, asset_status=200, asset_body=None, screener_rate_limit=False):
        env = {"APCA_API_KEY_ID": "fake-key", "APCA_API_SECRET_KEY": "fake-secret", "SURGERADAR_DISCOVERY_ENABLED": "true"}
        response = (asset_status, list(self.body["assets"].values()) if asset_body is None else asset_body, {})
        with patch.dict(os.environ, env, clear=True), patch("sys.stdout", new_callable=io.StringIO) as output, \
                patch("surgeradar.assets.AlpacaAssets.fetch_active", return_value=response) as asset_fetch, \
                patch("surgeradar.universe.AlpacaScreener.fetch", side_effect=lambda s: (429, None, {}) if screener_rate_limit else (200, self.body[s], {})) as screener:
            code = main(args)
        self.assertNotIn("fake-secret", output.getvalue())
        self.assertNotIn("fake-key", output.getvalue())
        return code, [json.loads(line) for line in output.getvalue().splitlines()], asset_fetch.call_count, screener.call_count

    def test_asset_check_independent_of_screener(self):
        code, rows, calls, screener_calls = self.invoke(["asset-check"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, 1)
        self.assertEqual(screener_calls, 0)
        self.assertTrue(rows[0]["common_stock_filtering_usable"])

    def test_extended_universe_check_shows_both_access_results(self):
        code, rows, calls, screener_calls = self.invoke(["universe-check"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, 1)
        self.assertEqual(screener_calls, 2)
        row = next(row for row in rows if row["event"] == "universe_check")
        self.assertTrue(row["access_verified"])
        self.assertEqual(row["asset_metadata"]["access"], "accessible")
        self.assertTrue(row["common_stock_filtering_usable"])

    def test_live_universe_filters_no_fill(self):
        code, rows, calls, _ = self.invoke(["universe"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, 1)
        row = rows[-1]
        self.assertEqual(row["symbols"], ["AAPL", "MSFT"])
        self.assertEqual(row["count"], 2)
        self.assertEqual(row["excluded_count"], 13)
        self.assertTrue(all(candidate["classification"] == "common_stock_verified" for candidate in row["candidates"]))

    def test_asset_denial_no_unverified_fill(self):
        code, rows, _, _ = self.invoke(["universe"], asset_status=403)
        self.assertEqual(code, 4)
        self.assertEqual(rows[-1]["symbols"], [])
        self.assertEqual(rows[-1]["metadata_coverage"]["metadata_unverified"], 15)
        self.assertFalse(rows[-1]["discovery_enabled"])

    def test_accessible_metadata_without_eligible_stock_is_not_usable(self):
        code, rows, _, _ = self.invoke(["universe"], asset_body=[self.body["assets"]["HYG"]])
        self.assertEqual(code, 5)
        self.assertEqual(rows[-1]["symbols"], [])
        row = next(row for row in rows if row["event"] == "universe_check")
        self.assertFalse(row["common_stock_filtering_usable"])
        self.assertFalse(row["discovery_enabled"])

    def test_asset_rate_limit_stops_selection(self):
        code, rows, calls, _ = self.invoke(["universe"], asset_status=429)
        self.assertEqual(code, 4)
        self.assertEqual(calls, 1)
        self.assertFalse(any(row["event"] == "universe" for row in rows))

    def test_rate_limit_prevents_asset_request(self):
        code, rows, calls, _ = self.invoke(["universe-check"], screener_rate_limit=True)
        self.assertEqual(code, 4)
        self.assertEqual(calls, 0)

    def test_fixture_filters_without_any_network(self):
        code, rows, calls, screener_calls = self.invoke(["universe", "--universe-fixture", str(FIXTURE)])
        self.assertEqual(code, 0)
        self.assertEqual((calls, screener_calls), (0, 0))
        self.assertEqual(rows[-1]["symbols"], ["AAPL", "MSFT"])
        self.assertTrue(rows[-1]["fixture_only"])


if __name__ == "__main__":
    unittest.main()
