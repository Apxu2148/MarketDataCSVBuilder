# MarketDataCSVBuilder

MarketDataCSVBuilder discovers the currently available MOEX, Bybit and Hyperliquid universe, applies a configurable 30-closed-daily-bar liquidity filter, downloads daily history, calculates the 29 canonical MarketDataVault features, and publishes machine-readable CSV snapshots. It intentionally has no database, UI, server, REST API, scheduler, portfolio construction, or timeframe other than `1D`.

## Setup (Python 3.11)

Run `setup.bat`. It creates `C:\Python\MarketDataCSVBuilder\venv`, upgrades pip inside that environment, and installs `requirements.txt` only there. No global package installation is needed or supported.

## Run

```bat
run.bat
run.bat --source moex --limit 3 --no-cache
run.bat --source bybit --limit 5 --refresh-cache
run.bat --source hyperliquid --limit 5 --as-of 2026-08-30T12:00:00Z
```

CLI options:

- `--config PATH`: TOML configuration (default `config.toml`).
- `--source all|moex|bybit|hyperliquid`: select live sources.
- `--limit N`: deterministic per-source discovery limit for smoke/performance runs.
- `--refresh-cache`: bypass reads and refresh simple HTTP cache entries.
- `--no-cache`: disable cache reads and writes.
- `--as-of ISO`: reproducible cutoff where supported by public historical endpoints. A datetime must include a timezone; a date means the end of that UTC date.

Long stages print an immediately flushed heartbeat at least every ten seconds, along with source, processed/total, successes/failures and elapsed time. Individual instrument failures are warnings and do not stop other instruments.

One press of `Ctrl+C` requests graceful cancellation and prints `Cancellation requested. Stopping active work...` immediately. No new executor work is submitted, queued futures are cancelled, retries/backoff stop, and already-running HTTP calls are allowed only their configured timeout to unwind. An incomplete `_building` directory is never promoted, the existing `current` snapshot remains intact, and `run.bat` returns exit code 130. Its compound handoff also suppresses the Windows `Terminate batch job (Y/N)?` prompt while preserving ordinary exit codes.

## Configuration

`config.toml` controls enabled MOEX categories, Bybit metadata contract types, Hyperliquid DEX discovery/delisted policy, thresholds, cache, retry/timeout behavior, workers, and full/compact sizes. Defaults are 1200 closed daily bars plus a current unfinished candle for `full`, 100 plus current for `compact`, and 30 closed bars for liquidity. Bybit spot is disabled by default. Bybit linear markets are selected by `contractType` metadata, never by ticker suffix or underlying asset class.

Hyperliquid uses one weighted rolling-window limiter shared by every worker and pipeline stage. It reserves 80% of the official 1200-weight/minute IP budget, includes documented `info` and requested `candleSnapshot` response-size weight, evenly paces permits to avoid connection bursts, applies a global `Retry-After`-aware cooldown after HTTP 429, and exposes cumulative request, estimated-weight, worker-wait-time, and 429 metrics in console progress and `run_report.json`.

Bybit public reads try the configured `base_url` and then the official `fallback_base_urls` in order. An HTTP 403 can still occur when Bybit restricts the caller's network location; that is reported as an isolated source failure rather than silently changing the universe.

## Output

Only two published snapshots are retained:

```text
output/
  current/
    full/<source tree>/<symbol>.csv
    compact/<source tree>/<symbol>.csv
    catalog.csv
    universe_status.csv
    README_APX_MARKETS.md
    run_report.json
  previous/
  _building/        # exists only during/instead of an incomplete build
```

A complete build is first written to `_building`. Publication rotates the old `current` to `previous` and promotes the complete build. A failed/empty build is never promoted. Filenames contain no run timestamp and unsafe Windows symbols are mapped deterministically; the original API symbol remains in CSV and catalog rows.

Every instrument produces two CSVs. Identity, timestamp, OHLC, raw `volume`, source-specific quote/notional `turnover`, actual `turnover_currency`, `is_closed`, `provisional`, and all 29 feature columns are present. Structural values are compact JSON inside a CSV cell. Insufficient lookback is an empty cell. `catalog.csv` contains READY datasets only; `universe_status.csv` contains the entire considered universe, including below-threshold and failed rows. The snapshot's own `README_APX_MARKETS.md` is the consumer contract.

MOEX FORTS uses the candle endpoint only for daily OHLC and raw volume. Closed-day RUB turnover and the 30-closed-bar liquidity filter use historical trading-results `VALUE`, matched by trading date. The current unfinished day's turnover uses marketdata `VALTODAY` when available; it remains provisional, and FORTS never substitutes `close * volume * lot_size`. Existing MOEX shares/bonds, Bybit, and Hyperliquid turnover rules are unchanged.

## Tests

```bat
venv\Scripts\python.exe -m pytest
venv\Scripts\python.exe -m pytest -m live
```

The ordinary suite is fully offline. Live tests are separately marked and skipped unless `MARKET_DATA_CSV_BUILDER_LIVE=1` is set. See `PROJECT_STATE.md` for the latest recorded offline, live and performance results.

## Read-only references

- `C:\Python\MOEXPortfolioBuilder`
- `C:\Python\BybitPortfolioBuilder`
- `C:\Python\HyperliquidPortfolioBuilder`
- `C:\Python\MarketDataVault`

They supplied verified source/discovery/retry/turnover patterns and the canonical feature specification. This project does not import from or modify them at runtime.
