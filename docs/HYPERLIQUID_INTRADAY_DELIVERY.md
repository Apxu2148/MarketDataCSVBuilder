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

Full/repeated live performance and integration evidence are pending. Local logs:
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
