# Apx Markets / Intraday Prompt market data consumer contract

The fresh, validated MarketDataCSVBuilder INTRADAY snapshot is the primary
machine-readable OHLCV and indicator source for the Intraday workflow.
It contains public Bybit Linear USDT Perpetual data, not trading decisions.
All contracts passing the configured rolling 24-hour turnover threshold are
included; no TOP liquidity/strongest/weakest/basket selection occurs in Python.

Expected Google Drive location:
`Apx Markets/01_INPUTS/marketdata/MarketDataCSVBuilder/intraday/current/`.
Replace the entire previous snapshot with one complete new snapshot. Never mix runs.

## Mandatory read order

1. Read `README_INTRADAY_MARKET_DATA.md`.
2. Read `manifest.json`.
3. Validate profile=intraday, snapshot ID, completeness and freshness.
4. Read `universe.csv`.
5. Read `latest_features.csv`.
6. Perform cheap whole-universe screening under the current Intraday Prompt.
7. Use `catalog.csv` for exact paths and series status.
8. Load raw series CSV only for shortlisted candidates.
9. Do NOT load every raw series during initial screening.
10. Use CLOSED indicators when the trading methodology requires completed data.
11. Treat PROVISIONAL rows explicitly as unfinished data.
12. Do NOT recompute technical indicators in the LLM when Builder supplies valid values.
13. Do NOT call Bybit candle tools merely to obtain data already in a fresh valid snapshot.

## Validation, traceability and freshness

Check manifest expected/successful/partial/failed series and symbol counts, enabled
timeframes, eligible contracts and configured history depth. Match snapshot_id in
universe, catalog, latest_features and raw series. A partial run may be valid while
some symbols or timeframes are unavailable: inspect catalog status and run_report
failures before analysis. Do not treat absence as a market signal or delisting.

Every analysis reference must retain at least: snapshot_id, snapshot completed_at_utc,
symbol, timeframe, candle timestamp and candle_state (CLOSED or PROVISIONAL).
Use exact relative_path from catalog, resolved inside this snapshot; never guess paths.

Always check the age of the snapshot, ticker/funding timestamp, per-series
history_fetched_at_utc/final_refresh_at_utc and last candle timestamps. Fetches are
not simultaneous. Apply the freshness requirement of the active Intraday Prompt
or canonical trading methodology. This contract does not impose a permanent TTL.
If stale, do not describe the dataset as the current market; request a fresh Builder run.

If the snapshot is missing, invalid, mixed, or a required symbol/timeframe is absent,
explicitly report the unavailable input and defer conclusions that require it.
Do not silently invent, reconstruct, forward-fill or substitute missing market data.
Use external/current sources only when explicitly required by the methodology for
information unavailable in the snapshot, and disclose that source and timestamp.

## Candle and indicator semantics

Timestamps are candle-open times in UTC. is_closed=true and provisional=false means
completed. The PROVISIONAL candle and all its features are unfinished and can change.
The latest_features CLOSED row is the last fully closed raw row; PROVISIONAL is the
separate current row. For completed 5m ATR use atr_wilder_14 on the CLOSED 5m row.
Never substitute provisional ATR without explicit methodological authorization.

Use Builder indicator values, including the shared 29 features and Wilder ATR(14).
Returns are decimal close/lagged-close-1 for 1, 5 and 20 bars of that timeframe.
Empty values mean unavailable/insufficient history; short-history instruments are
retained. Structural JSON is a list of [relative bar offset, price] pairs; an empty
list means computed but no surviving structure, distinct from an empty CSV cell.
Indicators use additional calculation history before the exported window. Gaps are
flagged PARTIAL and are never filled; features then refer to observed bars only.

## Funding and best bid/ask

Funding rate and next funding time are current snapshot fields, not historical
funding series. funding_fetched_at_utc identifies their observation time. bid1/ask1
and sizes are a snapshot at ticker_fetched_at_utc, not a guaranteed live spread when
an LLM reads the file. No full order book is downloaded: it becomes stale too quickly
for this batch workflow. Do not infer depth, slippage or executable liquidity from it.

## Integration into Apx Markets governance

The canonical routing/index should point intraday market data to
`01_INPUTS/marketdata/MarketDataCSVBuilder/intraday/current`.
The Intraday workflow should reference
`00_CONTROL/ROUTE_SPECS/APX_INTRADAY_MARKETDATA_CONSUMER.md`.
The Intraday Prompt should first read this consumer specification and the snapshot README.
Its OHLCV source hierarchy should designate the fresh validated Builder snapshot
as primary for candle/indicator data, and avoid duplicate Bybit connector candle fetches.
Add these pointers to the existing routing/index and Intraday workflow documents;
their exact filenames have not been established here.
