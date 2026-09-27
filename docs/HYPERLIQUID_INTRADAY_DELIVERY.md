# Hyperliquid INTRADAY delivery

Development is isolated in `C:\Python\MarketDataCSVBuilder_hyperliquid_work`,
branch `feature/hyperliquid-intraday`, based on original main `10c1cd0`.
No intermediate integration is authorized. Final integration requires accepted
new-profile tests, clean original main and no running original export.

## Changes

- Four launchers, shared code: legacy run.bat, APX, Bybit INTRADAY and Hyperliquid
  INTRADAY. Existing three launchers and config.toml are byte-for-byte unchanged.
- New independent TOML, output/cache/lock/staging namespace, native and all HIP-3
  discovery with rolling 10M USD-equivalent eligibility. No TOP-N in production.
- Actual collateral/quote identity, native DEX-prefixed symbols, observable oracle,
  funding and l2Book top, explicit unknowns and dynamic precision rules.
- Same 29 feature algorithms, ATR(14), returns, warm-up, final all-TF tail refresh
  with 5m last. Incremental local completed history; standalone validated current.
- Existing weighted limiter reused; bounded retries, shared cooldown and worker
  counts. Five timeframes; full candle-window queries use the API's 5000-bar capacity.
- Sparse/empty history ranges, malformed cache, duplicates and missing rows are
  handled explicitly. Staging can resume without changing existing snapshots.
- Snapshot publication rolls back on rename/report-update failure. Exact byte
  count accounts for Windows text newlines. Network, calculations, validation and
  publication timings are reported separately; worker wait is cumulative, not wall time.

## Validation

- Baseline: 96 passed, 3 live deselected.
- Final expanded offline suite: 109 passed, 3 live deselected, 42.47 seconds.
- All four Windows launchers `--help`: exit 0.
- Live Hyperliquid smoke: ARB/native, io:SNDK, xyz:BRENTOIL, all 15 series READY,
  178.6 seconds, 105 requests, 2639 weight, no retries or HTTP 429.
- Existing Bybit INTRADAY live public reads: 885 metadata records, 891 tickers,
  BTCUSDT 5m 1002 closed warm-up/history rows + current; four requests, no retries.
- Existing APX Bybit live smoke: PASS. MOEX: existing TLS handshake timeout after
  three bounded attempts. MOEX source code is unchanged; this is the same recorded
  connectivity limitation as the baseline, not a successful MOEX live check.

Continuation acceptance: 109 passed, 3 live deselected, 55.46 seconds. The initial
sandbox attempt had one Windows named-pipe permission failure; unrestricted tests pass.

## Full and cached acceptance (2026-09-27)

| Metric | First run | Cached repeat |
|---|---:|---:|
| READY series | 180 / 180 | 185 / 185 |
| Eligible instruments | 36 | 37 |
| Total seconds | 1287.016 | 977.947 |
| History download seconds | 676.883 | 399.986 |
| Feature calculation seconds | 138.728 | 141.986 |
| Final refresh seconds | 406.837 | 402.706 |
| Export worker seconds | 21.616 | 22.449 |
| Validation seconds | 3.316 | 3.718 |
| Publication seconds | 0.000453 | 0.002384 |
| API requests | 478 | 531 |
| Estimated API weight | 16728 | 12739 |
| Cumulative worker limiter wait seconds | 3462.376 | 2492.540 |
| Retries / HTTP 429 | 3 / 0 | 1 / 0 |

Overall repeat is 1.316x faster (24.0% less time); history is 1.692x faster
(40.9% less time). These are real sequential runs five hours apart, not a controlled
fixed-universe benchmark: 34 instruments are common; AAVE, XMR, xyz:INTC entered
and DASH, INJ exited the rolling liquidity filter. Requests increased due to tail,
missing-range and new-instrument requests; total estimated API weight fell 23.8%.
Export worker time overlaps final refresh and must not be added to total again.

Cold snapshot: `20260927T140210.586174Z_cb6a2b60`, completed 14:23:37 UTC.
Warm snapshot: `20260927T190753.421920Z_1f3cbadc`, completed 19:24:11 UTC.
Both publications have zero partial/failed series. Cold files remain in the worktree
under `previous_20260927T190753.421920Z_1f3cbadc`; staging and logs remain intact.
The cold report undercounted Windows bytes by 77; it is retained unchanged as evidence.
The fixed warm report exactly matches 144561767 bytes.

All warm CSVs pass identity/UTC/order/OHLCV/CLOSED-PROVISIONAL/catalog/latest checks.
All five TFs have zero gaps and zero missing current candles. Maximum 5m fetch age
at publication is 83.87 seconds (1D: 404.58 seconds); this is a timed snapshot,
not a live feed. Full feature recomputation matches exported 29 indicators,
ATR and returns for one native and two HIP-3 instruments across all five TFs,
with rtol/atol 1e-10; structural values match exactly. The same checks passed on
cold data before detecting its known byte-count metadata issue.
Cache audit: 195 retained files, 370351 rows, zero duplicates, invalid OHLC/grid
or provisional cached candles. Exited instruments' cached history is retained.

Machine-readable evidence is in `docs/hyperliquid_acceptance/`. Local logs:
`output/hyperliquid_cold.log`, `output/hyperliquid_smoke.log`, `output/legacy_live.log`,
`output/bybit_intraday_live.log`, `output/offline_final.log`.

## Consumer handoff

See `docs/APX_HYPERLIQUID_INTRADAY_CONSUMER.md` (also embedded in each current).
Copy the entire current to
`Apx Markets/01_INPUTS/marketdata/MarketDataCSVBuilder/intraday_hyperliquid/current/`.
No Drive integration or credentials are implemented.

Unknown candle turnover is N/A, not estimated close*volume. USDC is explicitly the
USD-equivalent numeraire; other collateral uses an observed spot/USDC mid or selection
fails closed. Spot conversion does not establish a separately measured fiat USD peg.
Short listing history, missing current bars and off-hours gaps are explicit and
never filled synthetically. Match Bybit only through a reviewed instrument registry.
