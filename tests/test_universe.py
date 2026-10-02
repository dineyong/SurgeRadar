import io
from decimal import localcontext
import json
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from surgeradar.cli import main
from surgeradar.universe import AlpacaScreener, ENDPOINTS, NoRedirect, SelectionConfig, parse_source, probe, rank_candidates as select

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "screener_universe.json"
ENV = {"APCA_API_KEY_ID": "fake-key", "APCA_API_SECRET_KEY": 'fake-secret"quoted'}


class Provider:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def fetch(self, source):
        self.calls.append(source)
        return self.responses[source]


class UniverseTests(unittest.TestCase):
    def setUp(self):
        self.body = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.sources = {s: parse_source(s, self.body[s]) for s in ENDPOINTS}

    def test_parse_official_response_shape(self):
        self.assertEqual([r["symbol"] for r in self.sources["actives"]], ["BBB", "AAA", "CCC"])
        self.assertEqual([r["symbol"] for r in self.sources["movers"]], ["AAA", "CCC"])
        self.assertEqual(self.sources["movers"][0]["rank"], 1)

    def test_deduplicated_union_and_source_metadata(self):
        rows = select(self.sources)
        self.assertEqual([r["symbol"] for r in rows], ["AAA", "BBB", "CCC"])
        self.assertEqual(rows[0]["score"], "3/2")
        self.assertEqual(rows[0]["mover_rank"], 1)
        self.assertEqual(rows[0]["active_rank"], 2)
        self.assertEqual(rows[0]["reasons"], ["most_active_by_volume", "top_gainer"])

    def test_cap_at_30_and_no_wildcard(self):
        body = {"most_actives": [{"symbol": f"S{i:03}", "volume": 1000-i} for i in range(100)]}
        rows = select({"actives": parse_source("actives", body)})
        self.assertEqual(len(rows), 30)
        self.assertEqual(len({r["symbol"] for r in rows}), 30)
        self.assertNotIn("*", [r["symbol"] for r in rows])

    def test_deterministic_for_shuffled_payload_and_source_order(self):
        reversed_sources = {s: parse_source(s, {**self.body[s],
                            "gainers" if s == "movers" else "most_actives":
                            list(reversed(self.body[s]["gainers" if s == "movers" else "most_actives"]))})
                            for s in reversed(ENDPOINTS)}
        self.assertEqual(select(self.sources), select(reversed_sources))

    def test_tie_breaks_alphabetically(self):
        active = parse_source("actives", {"most_actives": [{"symbol": "BBB", "volume": 1}]})
        movers = parse_source("movers", {"gainers": [{"symbol": "AAA", "percent_change": 1}]})
        self.assertEqual([r["symbol"] for r in select({"actives": active, "movers": movers})], ["AAA", "BBB"])

    def test_duplicate_within_source_not_double_scored(self):
        rows = parse_source("actives", {"most_actives": [{"symbol": "AAA", "volume": 1}, {"symbol": "AAA", "volume": 2}]})
        self.assertEqual(len(rows), 1)
        self.assertEqual(select({"actives": rows})[0]["score"], "1")

    def test_ranks_independent_of_decimal_precision(self):
        body = {"most_actives": [{"symbol": "AAA", "volume": "1.000000001"},
                                 {"symbol": "BBB", "volume": "1.000000002"}]}
        expected = parse_source("actives", body)
        with localcontext() as context:
            context.prec = 3
            self.assertEqual(parse_source("actives", body), expected)
        self.assertEqual(expected[0]["symbol"], "BBB")

    def test_asset_filter_excludes_inactive_untradable_and_non_equity(self):
        for metadata in ({"class": "crypto"}, {"status": "inactive"}, {"tradable": False}):
            self.assertNotIn("AAA", [r["symbol"] for r in select(self.sources, assets={"AAA": metadata})])
        rows = select(self.sources, assets=self.body["assets"])
        self.assertTrue(all(row["asset_metadata_verified"] for row in rows))
        self.assertFalse(select(self.sources)[0]["asset_metadata_verified"])

    def test_weights_transparent_and_configurable(self):
        rows = select(self.sources, SelectionConfig(mover_weight=1, active_weight=10, limit=2))
        self.assertEqual([r["symbol"] for r in rows], ["BBB", "AAA"])
        self.assertEqual(rows[1]["score"], "6")

    def test_invalid_selection_config(self):
        for values in ({"limit": 31}, {"limit": 0}, {"mover_weight": 0}, {"active_weight": True}):
            with self.assertRaises(ValueError):
                SelectionConfig(**values)

    def test_malformed_responses(self):
        for body in (None, [], {}, {"most_actives": {}}, {"most_actives": [None]},
                     {"most_actives": [{"symbol": "*", "volume": 1}]},
                     {"most_actives": [{"symbol": "AAA", "volume": "NaN"}]},
                     {"most_actives": [{"symbol": "AAA", "volume": True}]},
                     {"most_actives": [{"symbol": "AAA", "volume": -1}]}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_source("actives", body)

    def test_empty_responses_are_accessible_but_not_usable(self):
        provider = Provider({"actives": (200, {"most_actives": []}, {}), "movers": (200, {"gainers": []}, {})})
        statuses, sources = probe(provider)
        self.assertTrue(all(r["access"] == "accessible" and not r["usable"] for r in statuses))
        self.assertEqual(select(sources), [])

    def test_only_stock_gainers_and_no_zero_movers(self):
        self.assertEqual(parse_source("movers", {"gainers": [{"symbol": "AAA", "percent_change": 0}], "losers": []}), [])
        with self.assertRaises(ValueError):
            parse_source("movers", {"gainers": [], "market_type": "crypto"})

    def test_403_both_sources_denied_no_fallback(self):
        provider = Provider({s: (403, {"error": ENV["APCA_API_SECRET_KEY"]}, {}) for s in ENDPOINTS})
        statuses, sources = probe(provider)
        self.assertEqual(provider.calls, ["actives", "movers"])
        self.assertEqual(sources, {})
        self.assertTrue(all(r["access"] == "forbidden" for r in statuses))
        self.assertNotIn(ENV["APCA_API_SECRET_KEY"], json.dumps(statuses))

    def test_429_stops_without_retry_or_second_probe(self):
        provider = Provider({"actives": (429, None, {"Retry-After": "60"})})
        statuses, sources = probe(provider)
        self.assertEqual(provider.calls, ["actives"])
        self.assertEqual(statuses[0]["retry_after_seconds"], 60)
        self.assertEqual(statuses[1]["access"], "not_attempted")
        self.assertFalse(sources)

    def test_partial_access_keeps_available_source(self):
        provider = Provider({"actives": (403, None, {}), "movers": (200, self.body["movers"], {})})
        statuses, sources = probe(provider)
        self.assertEqual(list(sources), ["movers"])
        self.assertEqual(len(select(sources)), 2)

    def test_malformed_http_200_not_usable(self):
        statuses, sources = probe(Provider({s: (200, {}, {}) for s in ENDPOINTS}))
        self.assertFalse(sources)
        self.assertTrue(all(r["access"] == "malformed_response" for r in statuses))

    def test_other_http_and_transport_errors_are_distinct(self):
        statuses, _ = probe(Provider({"actives": (500, None, {}), "movers": (None, None, {})}))
        self.assertEqual([r["access"] for r in statuses], ["http_error", "transport_error"])


class TransportTests(unittest.TestCase):
    def test_exact_requests_no_retry(self):
        response = MagicMock()
        response.status = 200
        response.read.return_value = b'{"most_actives":[]}'
        response.__enter__.return_value = response
        opener = MagicMock()
        opener.open.return_value = response
        with patch("surgeradar.universe.build_opener", return_value=opener):
            status, body, _ = AlpacaScreener("key", "secret").fetch("actives")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, ENDPOINTS["actives"])
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 10)
        self.assertEqual(status, 200)

    def test_http_error_and_exception_strings_not_returned(self):
        for error in (HTTPError(ENDPOINTS["actives"], 403, "secret", {}, None), URLError("secret")):
            opener = MagicMock()
            opener.open.side_effect = error
            with patch("surgeradar.universe.build_opener", return_value=opener):
                result = AlpacaScreener("key", "secret").fetch("actives")
            self.assertNotIn("secret", str(result))

    def test_redirects_disabled(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, None, None, "https://evil.test"))


class UniverseCliTests(unittest.TestCase):
    def invoke(self, argv, responses=None, extra=None):
        env = {**ENV, **(extra or {})}
        with patch.dict(os.environ, env, clear=True), patch("sys.stdout", new_callable=io.StringIO) as output, \
                patch("surgeradar.universe.AlpacaScreener.fetch") as fetch, \
                patch("surgeradar.assets.AlpacaAssets.fetch_active", return_value=(200,
                    list(json.loads(FIXTURE.read_text(encoding="utf-8"))["assets"].values()), {})):
            if responses is not None:
                fetch.side_effect = responses
            code = main(argv)
        text = output.getvalue()
        self.assertNotIn("fake-secret", text)
        self.assertNotIn("fake-key", text)
        return code, [json.loads(line) for line in text.splitlines()], fetch.call_count

    def test_universe_disabled_by_default_no_network(self):
        code, rows, calls = self.invoke(["universe"])
        self.assertEqual(code, 2)
        self.assertEqual(calls, 0)
        self.assertFalse(rows[0]["discovery_enabled"])

    def test_check_missing_credentials_no_network(self):
        code, rows, calls = self.invoke(["universe-check"], extra={"APCA_API_KEY_ID": ""})
        self.assertEqual(code, 2)
        self.assertEqual(calls, 0)

    def test_check_403_exact_status_and_disabled(self):
        code, rows, calls = self.invoke(["universe-check"], [(403, None, {}), (403, None, {})])
        self.assertEqual(code, 4)
        self.assertEqual(calls, 2)
        row = next(r for r in rows if r["event"] == "universe_check")
        self.assertFalse(row["discovery_enabled"])
        self.assertEqual([s["http_status"] for s in row["sources"]], [403, 403])
        self.assertEqual(row["api_version"], "v1beta1")
        self.assertEqual(row["entitlement"], "forbidden")

    def test_check_429_no_retry(self):
        code, rows, calls = self.invoke(["universe-check"], [(429, None, {"Retry-After": "120"})])
        self.assertEqual(code, 4)
        self.assertEqual(calls, 1)

    def test_check_succeeds_without_enabling_discovery(self):
        body = json.loads(FIXTURE.read_text(encoding="utf-8"))
        code, rows, calls = self.invoke(["universe-check"], [(200, body["actives"], {}), (403, None, {})])
        self.assertEqual(code, 0)
        self.assertEqual(calls, 2)
        row = next(r for r in rows if r["event"] == "universe_check")
        self.assertTrue(row["access_verified"])
        self.assertTrue(row["discovery_available"])
        self.assertFalse(row["discovery_enabled"])

    def test_second_source_rate_limit_disables_even_with_first_success(self):
        body = json.loads(FIXTURE.read_text(encoding="utf-8"))
        code, rows, calls = self.invoke(["universe"], [(200, body["actives"], {}), (429, None, {})],
                                        {"SURGERADAR_DISCOVERY_ENABLED": "true"})
        self.assertEqual(code, 4)
        self.assertEqual(calls, 2)
        self.assertFalse(any(r["event"] == "universe" for r in rows))

    def test_fixture_no_network_and_not_entitlement_evidence(self):
        code, rows, calls = self.invoke(["universe", "--universe-fixture", str(FIXTURE)])
        self.assertEqual(code, 0)
        self.assertEqual(calls, 0)
        self.assertTrue(rows[-1]["fixture_only"])
        self.assertFalse(rows[-1]["discovery_enabled"])
        self.assertEqual(rows[-1]["symbols"], ["AAA", "BBB", "CCC"])
        self.assertEqual(rows[-1]["count"], 3)

    def test_enabled_universe_still_probes_entitlement_before_selection(self):
        body = json.loads(FIXTURE.read_text(encoding="utf-8"))
        code, rows, calls = self.invoke(["universe"], [(200, body["actives"], {}), (403, None, {})],
                                        {"SURGERADAR_DISCOVERY_ENABLED": "true"})
        self.assertEqual(code, 0)
        self.assertEqual(calls, 2)
        self.assertEqual(rows[-1]["count"], 3)
        self.assertEqual(rows[-1]["monitoring_feed"], "iex")

    def test_enabled_but_denied_universe_stays_disabled(self):
        code, rows, _ = self.invoke(["universe"], [(403, None, {}), (403, None, {})],
                                    {"SURGERADAR_DISCOVERY_ENABLED": "true"})
        self.assertEqual(code, 4)
        self.assertEqual(rows[-1]["event"], "universe_disabled")


if __name__ == "__main__":
    unittest.main()
