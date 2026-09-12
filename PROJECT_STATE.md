# Project State

Last updated: 2026-09-12 (Europe/Moscow).

## Implemented

- Independent Python 3.11 project with local `venv`, `setup.bat`, `run.bat`, TOML config and CLI.
- Fresh per-run discovery for enabled MOEX shares/term futures/perpetual futures/funds/bonds, Bybit V5 linear perpetual/futures plus optional spot, and Hyperliquid default plus discovered perp DEXes.
- Metadata-based Bybit `LinearPerpetual`/`LinearFutures` classification without ticker-suffix or underlying-class filtering; USDT and USDC quote currencies preserved. Official `api.bytick.com` mainnet fallback is configurable.
- Source-specific 1D OHLCV/turnover normalization, time/API pagination, simple HTTP cache, bounded retries/backoff/timeouts, per-source bounded workers, 30-closed-bar liquidity scan, and current-candle exclusion from liquidity.
- MOEX FORTS preserves candle OHLCV while joining closed-day RUB turnover from historical trading-results `VALUE` by trading date. The current Moscow day's provisional candle uses `VALTODAY` when available; FORTS has no synthetic `close * volume * lot_size` turnover fallback. Securities discovery remains a single non-paginated request.
- Stateless left-looking calculation of all 29 MarketDataVault features, including canonical population-sigma Bollinger, strict pattern boundaries, and 12 structural list features with deterministic compact JSON.
- Full/compact per-series CSV, safe Windows filenames, catalog/status/report/snapshot README, partial instrument failure isolation, and `_building -> current/previous` rotation. An all-failed run keeps diagnostic metadata in `_building` and is not promoted.
- Each successful build also publishes source-neutral `latest_snapshot.csv`: one row per READY catalog series, identity/paths, closed-history depth, closed-bar 1/5/20-day returns, an explicitly separate provisional current price, and the canonical 29 features from the last fully closed candle.
- `catalog.csv` and `latest_snapshot.csv` now append advisory `source_underlying_symbol`, `underlying_symbol`, `contract_expiry`, and `underlying_liquidity_rank`. The extension preserves all legacy column positions and does not change READY eligibility. MOEX preserves source `ASSETCODE`, normalizes the APX regression families `GAZR`→`GAZP`, `SBRF`→`SBER`, `MIX`→`IMOEX`, and reads `LASTTRADEDATE`; Bybit preserves `baseCoin` and dated-future `deliveryTime`. Liquidity rank is deterministic within source/market/underlying READY families and is intended for roll assistance only, not canonical mapping authority.
- Flushed console progress: source/stage start and completion, processed/total, counters, elapsed time, 10-second heartbeat while futures are pending, and immediate per-symbol warnings.
- Graceful Ctrl+C cancellation: one SIGINT sets a shared token and prints immediately; bounded executors stop submission and cancel queued futures; HTTP retry/backoff and feature loops stop cooperatively; incomplete `_building` is not promoted; CLI/batch preserve cancellation code 130 without the Windows Y/N batch prompt.
- HTTP cache reads explicitly import the `time` module used for TTL checks; a regression test exercises a fresh cache hit so missing runtime dependencies in this path fail offline.
- Hyperliquid has one cancellation-aware weighted limiter shared by discovery, liquidity and history workers. It reserves 80% of the official 1200-weight/minute IP limit, applies documented `info` base weights plus conservative requested-candle weight, enforces a rolling 60-second budget, evenly paces requests, and applies a `Retry-After`-aware global cooldown after HTTP 429. Console progress and `run_report.json` include `rate_limit_wait_seconds`, `http_429_count`, `requests_sent`, `estimated_api_weight`, and live `waiting_threads`.

## Offline tests

- Latest branch acceptance: GitHub Actions / Python 3.11, `python -m pytest -q`.
- Result: **62 passed, 3 deselected**, exit 0, 5.96 s on 2026-09-12.
- New mapping regression coverage verifies append-only `catalog.csv` / `latest_snapshot.csv` schemas, unchanged `run_report.json` semantics, unchanged READY eligibility, MOEX source/native + normalized underlying and expiry metadata, Bybit underlying + delivery-date metadata, GAZP↔GZU6, SBER↔SRU6, IMOEX↔MXU6, and the MXU6↔MXZ6 roll family with deterministic liquidity ranks 1/2.
- Existing coverage remains green for MOEX/Bybit/Hyperliquid normalization; MOEX FORTS historical `VALUE` overriding zero candle value, 30-closed-day liquidity, current `VALTODAY`, no synthetic FORTS turnover fallback, and one-request securities discovery; Bybit USDT, USDC, LinearPerpetual, LinearFutures, Spot pagination contract and backward time pagination; MOEX categories/cursor; Hyperliquid multi-DEX/delisted behavior; liquidity/current exclusion; all 29 features; insufficient history; anti-look-ahead; structural lifecycle/serialization; safe filenames; current/provisional flags; full/compact slicing; rotation; all-failed non-publication; partial download/feature failures; `latest_snapshot.csv` READY/catalog parity and source-neutral identity/path mapping; last-closed versus provisional feature selection; closed-bar returns; 99/100/999/1000 history flags; three-bar READY history; absent-current empty fields; preservation of published aggregate data on cancellation; real Python SIGINT dispatch; bounded cancellation; queued-future cancellation; cancellation-aware HTTP retry suppression; a fresh HTTP cache hit through the TTL check; documented Hyperliquid request-weight estimation; rolling weighted-budget waits; even pacing; global 429 cooldown; nonzero `Retry-After`; cancellation during limiter wait; and console/on-disk rate-limit metrics.

## Live checks

### Hyperliquid

- Live pytest discovery + one daily series: PASS.
- Full-universe rate-limit acceptance, `--source hyperliquid --no-cache`, threshold 10M USD-equivalent:
  - snapshot `20260830T144307Z`, exit 0;
  - discovered 326 across 6 DEXes; passed 56, READY 56, below threshold 270, failed 0;
  - `http_429_count=0`, `requests_sent=399`, `estimated_api_weight=11381`, cumulative worker `rate_limit_wait_seconds=3830.230`;
  - discovery 14.616 s, liquidity 484.497 s, history 223.809 s, features 25.548 s, export 2.251 s, total 750.723 s;
  - all 56 READY CSVs end with `is_closed=false, provisional=true`;
  - `catalog.csv` has 56 rows and `universe_status.csv` has all 326 rows;
  - current `run_report.json` SHA-256 `41387A00A11AB5BB6736BA52D68F666CA5BE43BE8DF8AA8FCE144FD9CF86812C`.
- Post-regression end-to-end smoke, `--source hyperliquid --limit 5 --refresh-cache`:
  - snapshot `20260830T140846Z`;
  - discovered 5, passed liquidity 1, READY 1, below threshold 4, failed 0;
  - total 12.127 s;
  - published `AAVE.csv` ends with `is_closed=false, provisional=true`.
- End-to-end smoke, `--source hyperliquid --limit 5 --no-cache`:
  - snapshot `20260830T130144Z`;
  - discovered 5, passed liquidity 3, READY 3, below threshold 2, failed 0;
  - 6 dataset CSV plus catalog/status;
  - total 14.343 s.
- Multi-DEX live discovery: 326 active instruments across 6 DEXes:
  - default 176, hyna 18, io 3, mkts 4, para 22, xyz 103.
- Additional-DEX candle check: `hyna:1000PEPE`, 3 closed + current, current detected, turnover currency USDC: PASS.
- Output validation after performance run: every READY series has all 29 feature columns and a final `is_closed=false, provisional=true` row; short histories are retained; compact files contain 100 closed + current; `previous/run_report.json` exists.

### Graceful cancellation (Windows PTY/manual)

- Command: `run.bat --source hyperliquid --limit 100 --no-cache`.
- A single real Ctrl+C during the liquidity stage printed `Cancellation requested. Stopping active work...` immediately.
- Cooperative shutdown completed in under 1 second in the latest accepted run; no remaining universe was processed.
- `run.bat` captured cancellation `ERRORLEVEL=130`; ordinary help/error paths were also checked as 0/2.
- The `Terminate batch job (Y/N)?` prompt was eliminated using the same-command `ERRORLEVEL` capture and `cmd` Ctrl-state reset.
- Existing `current` remained snapshot `20260830T140846Z` with unchanged SHA-256 `EAE4A6F3EC7357978AA400305B72709CF3E652629E3B74E6DBBD9301556047FA`; `_building` remained incomplete with zero files and was not promoted.

### MOEX

- Live client attempted through configured local proxy and with proxy bypass.
- Result: BLOCKED by TLS handshake timeout to `iss.moex.com` in this execution environment after bounded retries.
- Offline normalization, category switching, candle/history pagination, historical FORTS turnover, current `VALTODAY`, one-request discovery, mapping metadata, expiry handling and current-candle semantics pass.

### Bybit

- Live client attempted against both official mainnet endpoints `api.bybit.com` and `api.bytick.com` with proxy bypass.
- Result: BLOCKED by HTTP 403 from both endpoints for this execution environment/network location.
- An independent web transport could read current BTCUSDT `LinearPerpetual` metadata, confirming the endpoint contract, but this is not counted as a program live PASS.
- Offline fixtures verify USDT + USDC, current candle, quote turnover, LinearPerpetual, LinearFutures, underlying/base-coin metadata, dated-future delivery date, no underlying-class filter, cursor-based instrument discovery and time-based kline pagination.

## Performance

- Command: `run.bat --source hyperliquid --limit 25 --no-cache`
- Snapshot: `20260830T130252Z`.
- Discovered 25, passed liquidity 9, READY 9, below threshold 16, failed 0.
- Created 18 dataset CSV plus `catalog.csv` and `universe_status.csv` (20 CSV total).
- Stage timings: discovery 8.351 s; liquidity 3.069 s; full history 2.998 s; features 9.057 s; export 0.663 s; total 24.140 s.
- One transient HTTP failure was retried successfully; no instrument failed.
- Full row counts: six 1200+current series and three valid shorter histories (986+current, 634+current, 345+current). All compact outputs contain 100+current.

## Known issues / explicit limitations

- MOEX and Bybit program live validation remains blocked by this machine's current network/proxy/IP behavior; no code workaround can establish a trustworthy live PASS from this environment.
- The Windows live environment routes HTTP(S) through local `xray.exe` at `127.0.0.1:10809`. During both 12.5-minute no-cache Hyperliquid runs it briefly returned `URLError(FileNotFoundError(2, 'No such file or directory'))` about 476-479 seconds into liquidity, then recovered on normal retries with zero dataset failures. A no-cache run rules out cache/temp-file handling, and the `<urlopen error ...>` wrapper locates the exception in transport/proxy handling. Even pacing reduced the transient retries from eight to five; a separate controlled 24-request parallel probe completed without error, so this is intermittent rather than a deterministic endpoint/body failure.
- `--as-of` limits historical candle requests and anti-look-ahead calculations, but public discovery endpoints expose the current universe rather than a fully reconstructable historical universe.
- MOEX candles dated today are conservatively provisional for the entire Moscow calendar day because ISS daily candles do not expose a universal per-row finality flag.
- Hyperliquid uses candle quote value when present and the reference-project `close * volume` documented fallback otherwise.
- A deterministic `--limit` is applied after source discovery per source; it is intended for bounded smoke/performance work, not representative sampling.
- `underlying_symbol` is intentionally an advisory normalization hint, not a canonical economic-asset identifier. Downstream consumers must retain their own fail-closed mapping registry and treat unknown/new venue families as unresolved rather than trusting the hint silently.

## Recommended next test

On the local Windows environment where the production snapshot is generated, run the ordinary builder once after deploying the accepted metadata change, then verify the new columns against a real current `catalog.csv` / `latest_snapshot.csv`. Specifically confirm GZU6→GAZP, SRU6→SBER, MXU6/MXZ6→IMOEX, non-empty dated expiries, MXU6 liquidity rank ahead of MXZ6, exact READY/catalog/latest-snapshot parity, and unchanged run-report counts. Network live checks remain optional diagnostics and do not redefine mapping authority.
