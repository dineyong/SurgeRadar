# SurgeRadar V0.2

Issues [#2](https://github.com/dineyong/SurgeRadar/issues/2) and
[#3](https://github.com/dineyong/SurgeRadar/issues/3): validate explicitly selected
Alpaca IEX or SIP entitlement and minute-bar ingestion in one Python process.
Issue [#4](https://github.com/dineyong/SurgeRadar/issues/4) adds an independent
detector core and offline replay. The live ingestion runner does not invoke it.
No relative-volume baseline, database, alerts, post-signal tracking or trading code.

## Offline detector replay

After installation, from the repository root (no API credentials required):

```powershell
.\.venv\Scripts\python.exe -m surgeradar replay fixtures/synthetic_iex.jsonl
```

The synthetic IEX fixture includes six symbols: SURGE (accelerating surge),
STEADY (steady rise), WEAKVOL (weak volume), NOMOM (insufficient momentum),
LOW (under $1), and GAP (missing minute). Only SURGE should detect.
Output is JSON Lines of `detection`, `candidate_rejection`, then `replay_summary`.
The core returns every evaluation, including non-candidates; the CLI omits these
non-candidate evaluations to keep output readable. Candidates during warm-up
are rejections with explicit history failures and null unavailable features.

Each fixture line is a normalized bar with required `provider`, `feed`, `symbol`,
`event_at`, `open`, `high`, `low`, `close`, and `volume`. Prices can be decimal
strings; the fixture uses `provider=alpaca, feed=iex` as **synthetic source labels**,
not as evidence of live data. `received_at`, `vwap`, and `trade_count` are optional.
Omitted receive time is deterministically event time + 60 seconds; no current
clock is used. The existing IEX/SIP model metadata is retained without changing
live ingestion. Replay rejects missing source metadata, invalid bars, empty
fixtures and duplicate provider/feed/symbol/timestamp keys before emitting results.

Replay sorts input by timestamp, with provider/feed/symbol as deterministic tie
breakers. It evaluates each bar using only the current and earlier bars, keeping
at most six contiguous minutes per provider/feed/symbol. A missing minute or
overnight gap resets that history. Direct core calls reject duplicates and
out-of-order input. Results contain symbol, minute-start `feature_at`, close as
`detected_price`, all features, predicate values/operators/thresholds/pass flags,
failed predicate names, candidate/detected flags, provider/feed/coverage_class,
`detector_version=v0.1-no-rvol`, and the full threshold configuration.

For current close C[k], `change_nm = C[k]/C[k-n] - 1` (fractional return).
`volume_5m` sums k-4 through k. `dollar_volume_5m` sums close * volume over those
five bars: a bar-level approximation, not exact trade notional. Acceleration is
`change_1m - ((C[k-1]/C[k-3] - 1)/2)`. Decimal arithmetic uses fixed precision
28 and rounding independently of the caller's numeric context.

| Config field | Default | Detection comparison |
|---|---|---|
| min_price | 1.00 | close >= |
| min_change_1m | 0.008 | change_1m >= |
| min_change_3m | 0.012 | change_3m >= |
| min_change_5m | 0 | change_5m strictly > |
| min_acceleration | 0.004 | acceleration >= |
| min_volume_1m | 5000 | current bar volume >= |
| min_dollar_volume_5m | 250000 | five-bar dollar volume >= |
| candidate_change_1m | 0.004 | candidate if change_1m >= OR next row |
| candidate_change_3m | 0.008 | candidate if change_3m >= |

All detection predicates and six-minute contiguous history must pass. Candidate
status alone never causes a detection. Candidate thresholds do not prevent an
otherwise matching detection when custom configuration makes them stricter.
IEX volume remains IEX-only; future results must stay separated by
provider/feed/detector version/config.

Override any threshold with a local JSON object, e.g.
`{"min_price":"2.00","min_volume_1m":"10000"}`, then run:

```powershell
.\.venv\Scripts\python.exe -m surgeradar replay fixtures/synthetic_iex.jsonl --config thresholds.json
```

Unknown fields, negative/nonfinite values, and malformed input return exit 2.
Thresholds are algorithm plumbing for later validation; synthetic results do not
establish profitability or predictive quality.

## Install

Python 3.11+ (validated on Windows / Python 3.12). From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

On Linux/macOS use `.venv/bin/python` and `.venv/bin/surgeradar`.
Runtime dependencies: websockets and psutil. Tests use standard-library unittest.
Installation needs package-index access; execution needs outbound TLS to Alpaca.

## Configure explicitly

The runner reads **process environment variables only**; it does not automatically
load .env. [.env.example](.env.example) documents the names and safe defaults.
Do not paste real credentials into Git, logs, command-line arguments or committed files.
No subscription purchase or account modification is performed.

PowerShell, with prompts (the secret prompt is hidden):

```powershell
$env:SURGERADAR_FEED = 'iex'
$env:APCA_API_KEY_ID = Read-Host 'Alpaca key ID'
$credentialInput = Read-Host 'Alpaca secret key' -AsSecureString
$env:APCA_API_SECRET_KEY = [System.Net.NetworkCredential]::new('', $credentialInput).Password
$env:SURGERADAR_SYMBOLS = 'SPY,AAPL,MSFT'
$env:SURGERADAR_DURATION_SECONDS = '180'
$env:SURGERADAR_REPORT_SECONDS = '10'
```

After use, remove credentials from this shell:

```powershell
Remove-Item Env:APCA_API_KEY_ID, Env:APCA_API_SECRET_KEY -ErrorAction SilentlyContinue
Remove-Variable credentialInput -ErrorAction SilentlyContinue
```

`SURGERADAR_FEED=iex` or `SURGERADAR_FEED=sip` is required; no default feed or
silent fallback is used. Other feeds are rejected before connection. The selected
endpoint is `wss://stream.data.alpaca.markets/v2/iex` or `/v2/sip`. Config/credentials and raw upstream
messages or exception text are never logged. websocket library logging is disabled
to prevent authentication-frame logging.

| Variable | Default / meaning |
|---|---|
| APCA_API_KEY_ID / APCA_API_SECRET_KEY | required, not printed |
| SURGERADAR_FEED | required: iex or sip; example uses free Basic/IEX |
| SURGERADAR_SYMBOLS | SPY,AAPL,MSFT; comma-separated, wildcard rejected here |
| SURGERADAR_DURATION_SECONDS | 180; positive, connection/retries included |
| SURGERADAR_REPORT_SECONDS | 10; periodic metrics |
| SURGERADAR_QUEUE_SIZE | 256 inbound frames, bounded; no infinite capacity |
| SURGERADAR_MAX_RECONNECTS | 3 retries after initial connection; 0 disables |

Basic is free and provides real-time IEX data with a 30-symbol WebSocket limit.
IEX mode requires 1–30 distinct explicit symbols; wildcard and larger lists fail
before connection. The default small liquid set is SPY,AAPL,MSFT.
IEX is **not the full US market**, and IEX volume is **not consolidated market
volume**. These runs are suitable for pipeline/algorithm prototyping, not for
validating production-quality full-market surge detection. Any future detector
results must remain separated by provider/feed/version from SIP results. The
runner labels both normalized bars and all JSON diagnostics with the selected feed.

## Run the three diagnostics

```powershell
# Authentication on the explicitly selected feed, then verify bars ACK.
.\.venv\Scripts\surgeradar.exe check

# Small liquid symbol set; log each valid normalized bar.
.\.venv\Scripts\surgeradar.exe smoke

# Whole US stock feed via bars=["*"]; aggregate logs to reduce stdout overhead.
# SIP only; explicitly select SIP and provide entitled credentials to use broad.
$env:SURGERADAR_FEED = 'sip'
$env:SURGERADAR_DURATION_SECONDS = '300'
.\.venv\Scripts\surgeradar.exe broad
```

Equivalent: `python -m surgeradar check|smoke|broad` using the installed environment.
Inspect `$LASTEXITCODE` immediately after the command. Ctrl+C stops the runner.
PowerShell can save stdout via `| Tee-Object -FilePath logs/broad.jsonl` after creating
the ignored `logs` directory; logging throughput can affect this benchmark.

**check** confirms authentication on the exact selected endpoint and bars ACK;
it does not assert receipt of actual market data. Code 409 reports
`sip_not_entitled` with `sip_entitled=false` and stops. Code 402 is
`credentials_rejected`, not evidence of missing SIP rights. Code 406 reports
connection-limit failure. Many subscriptions allow only one stream; stop other
clients before running this runner. HTTP/network failures report entitlement as
unverified, not as entitled or a reason to substitute another feed.

**smoke/broad** require at least one valid minute bar to exit successfully.
Outside trading hours, a healthy authenticated session may receive no bars and
return 5. Inspect its timestamps; receipt alone does not prove that data is fresh.
No test fixture is emitted by the diagnostic CLI. Fixtures and loopback WebSockets
exist only in offline tests and **do not demonstrate real feed entitlement**.

**broad** is SIP-only. With `feed=iex`, it returns exit 2 with
`broad not supported on Basic/IEX` before opening a connection or sending wildcard.
In SIP mode it implements the documented all-symbol subscription. Unlimited symbols is
a provider entitlement, not a throughput guarantee. Wildcard ACK denial is explicit
and stops. Whole-feed observations include stocks/ETFs beyond a NASDAQ/NYSE common
stock universe; no universe selection/filtering is implemented in this issue.

## Diagnostics and benchmark interpretation

Every JSON line, including failures, metrics and results, includes provider=alpaca,
selected feed, coverage_class (`single_exchange_iex` or `consolidated_sip`),
entitlement (`unverified`, `authenticated`, or `denied`), limitations,
requested_symbol_count, api_version=v2 and runner_version.
For SIP wildcard the symbol count is null because the universe size is unknown;
symbol_scope is `all_symbols`, rather than misrepresenting wildcard as one symbol.
Events include starting, connected, sip_entitlement_verified, subscription_ack,
disconnected, reconnect_scheduled, reconnected, provider_error, queue_overflow,
minute_bar (small mode), summary, event_minute and smoke_result. IEX authentication
uses `iex_entitlement_verified`; it never claims SIP entitlement.

- `frames_received`: WebSocket recv frames, including malformed/overflow frames.
  `messages_received`: decoded array entries, including controls, ignored and malformed
  entries; malformed frame JSON has no known message count.
- `bars_received`, `unique_symbols`, malformed/ignored counts are cumulative.
  Updated bars, quotes, trades and other channels are ignored, never treated as new minute bars.
  No correction, deduplication or historical backfill is implemented here.
- `messages_per_second` and `bars_per_second` cover the interval since the previous
  summary. The final summary can be a very short interval; use periodic summaries
  for throughput judgment.
- `event_minute` groups by **provider event minute in UTC**, with
  bars_per_minute and unique_symbols_per_minute. These are repeated cumulative
  snapshots per minute, **not increments to sum**. A latest/final minute may be
  provisional; disconnects, partial runs and no-trade minutes reduce coverage.
  At most 120 event-minute buckets are kept in memory.
- Each small-mode bar exposes event_at (minute **start**), received_at and receive lag.
  Summary also exposes last_event_at and last_received_at.
  Lag = received_at − (event_at + 60 seconds), measured at recv, before processing.
  p50/p95/p99 interpolate a bounded tail of the last 100,000 valid bar observations.
  They are not all-run or per-minute percentiles. Negative values expose possible
  clock/provider timestamp anomalies; synchronise the machine clock before analysis.
- Bounded application queue depth, oldest queued frame age, high-water mark and
  processing queue age p99 (last 100,000 dequeues) indicate local pressure.
  The websocket library also has a separate bounded 16-frame buffer; its depth is
  not included. Queue overflow is logged and causes failure; frames are not silently
  dropped and advertised as a successful run. Raw queued frames are not persisted.
- CPU percent is psutil process utilisation between snapshots; it may exceed 100%
  on multi-core hosts. RSS bytes is resident process memory. No hardware capacity
  claim is made from these numbers.

After a transport failure the runner reconnects with bounded exponential delay,
reauthenticates and re-subscribes to the same selected feed. Reconnects mark continuity
unverified; **no replay/backfill** is provided in V0.1. If retries exhaust, failure
is returned even if earlier bars arrived. Backoff and shutdown are bounded;
queue draining adds up to 2 seconds and connection close up to 2 seconds.

A successful ingestion exit means only that bars were observed during the run.
Inspect lag, discontinuities, overflows and workload bursts; it never sets
`broad_capacity_verified=true`. Start small, then run broad through opening/closing
bursts before deciding local capacity is adequate. This issue does not implement
later detector quality gates.

| Exit code | Meaning |
|---|---|
| 0 | check authenticated + ACK, or smoke/broad observed bars; see diagnostics |
| 2 | invalid configuration/credentials, IEX >30 symbols, or IEX broad unsupported |
| 3 | transport, credentials/protocol/queue failure or retry exhaustion |
| 4 | selected-feed insufficient subscription (provider 409); no fallback |
| 5 | no valid bars during smoke/broad; not verified |
| 130 | interrupted by user |

## Tests and current live blocker

`python -m unittest discover -s tests -v` covers normalization, JSON batches,
invalid fields, configuration/no-fallback, secret-safe logs, metrics, handshake
ordering, entitlement denial, ACK mismatch, no-bar result, queue overload,
reconnect/re-authentication and a real **local loopback** WebSocket protocol test.

At implementation time the local environment had neither APCA_API_KEY_ID nor
APCA_API_SECRET_KEY. All three actual CLI modes therefore stopped at configuration
validation with exit 2. **Real SIP entitlement, small-symbol live receipt and
whole-market throughput remain unverified.** Supplying valid credentials with SIP
access and running during a trading session is the next external validation step.
No purchase was made. Issue #3 adds offline IEX validation; actual IEX receipt
still requires the owner's credentials and a session with trading on IEX.

Exact IEX smoke command after installation and the credential prompts above:

```powershell
$env:SURGERADAR_FEED = 'iex'
$env:SURGERADAR_SYMBOLS = 'SPY,AAPL,MSFT'
$env:SURGERADAR_DURATION_SECONDS = '180'
.\.venv\Scripts\python.exe -m surgeradar smoke
```

## Official protocol references

Checked 2026-10-02:

- [Alpaca WebSocket protocol](https://docs.alpaca.markets/us/docs/streaming-market-data):
  auth, ACK, wildcard, error codes and connection/slow-client limits.
- [Alpaca real-time stock data](https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data):
  SIP endpoint, minute-bar fields/timestamps and updated-bar distinction.
- [Alpaca data plans](https://docs.alpaca.markets/us/docs/about-market-data-api):
  entitlement/symbol limits; this project does not purchase a plan.
- [websockets asyncio client](https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html):
  bounded buffers, receive and connection settings.
