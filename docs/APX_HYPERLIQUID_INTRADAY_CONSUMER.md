# Apx Markets — Hyperliquid INTRADAY

Copy the entire successful output/intraday_hyperliquid/current directory to
`Apx Markets/01_INPUTS/marketdata/MarketDataCSVBuilder/intraday_hyperliquid/current/`.
Replace the previous Drive current as a whole. No incremental patch or local cache
is needed on Drive. Builder never connects to Drive.

Read README and manifest.json first, then universe.csv and latest_features.csv.
Use catalog.csv relative_path for selected series; avoid loading all series at once.
All files belong to one snapshot_id; reject mixed IDs. CSV is UTF-8, comma-separated,
with ISO UTC candle-open timestamps and decimal numbers. Structural feature cells
contain JSON [relative_bar_offset, price] pairs. Empty feature cells mean insufficient
history; [] means computed with no surviving structures. N/A means unavailable venue data.

Identity is (source=hyperliquid, dex, native symbol). The native DEX is empty for
the first perpetual DEX. HIP-3 symbols retain the full dex:coin prefix. Never merge
same-name instruments on different DEXes. Windows filenames are sanitized with a hash;
follow catalog paths rather than deriving paths from native names.

quote_coin/settle_coin and collateral_token come from venue metadata and spot-token
identity. Do not relabel USDC as USDT. Precision uses sz_decimals for quantity,
at most 5 significant price figures and 6-sz_decimals decimal places; integer
prices are exempt from the significant-figure limit. There is no constant tick_size.
Unknown order minima/maxima, last trade and 24h high/low remain N/A. index_price
contains oraclePx and oracle_price makes that meaning explicit. mid_price is not
last_price. Best bid/ask come only from l2Book, never from impactPxs; sizes are base
quantity. Book and context timestamps are independent; missing book is N/A.

Eligibility is fixed at initial rolling dayNtlVlm converted to USD-equivalent >= configured threshold, without
TOP-N. Final context turnover can differ. Candle volume is base quantity; candle
turnover is N/A unless actually supplied by the API, never close*volume.
USDC is the explicit USD-equivalent numeraire (rate 1); other collateral uses the
observed spot/USDC mid. Both initial and final conversion rates and converted
turnover are exported. This is a USDC-based USD-equivalent convention, not a claim
of an independently observed fiat USD/USDC peg. Unknown conversion aborts selection.

Timeframes: 1D, 4H, 1H, 15m, 5m. Requested export is 1200 completed bars plus
one current PROVISIONAL; short histories remain usable and explicitly counted.
The API exposes only the latest 5000 candles. Calculations request 999 additional
warm-up bars. All 29 canonical indicators use the unchanged shared engine.
ATR(14) uses Wilder smoothing, seeded by mean of first 14 true ranges; first TR
is high-low. The seed belongs to the extended window, not listing inception.
Returns 1/5/20 are decimal close/lagged-close-1. No look-ahead or synthetic candles.

latest_features contains separate CLOSED and PROVISIONAL rows. Use CLOSED for
completed-bar decisions. Provisional OHLC and every provisional feature can change.
Check manifest completion, per-series final_refresh_at_utc, last_closed_timestamp,
missing_current_bar, gap_count, status and actual candle age. A fresh fetch is not
proof of a recent trade. Traditional-market contracts may have stale/absent candles
outside underlying trading hours; do not infer a market calendar or forward-fill.
PARTIAL means gaps or refresh failure; FAILED has no usable series. Final refresh
visits all timeframes with 5m last. Timestamps are an interval, not a simultaneous quote.

Compare with Bybit only through a reviewed mapping registry that retains both native
IDs, DEX, quote/settlement currency and contract economics. Matching ticker text is
only a hint, especially for multipliers, indices, equity and commodity contracts.
Do not treat missing mappings as tradable equivalence.

run_report.json records request counts, retries, weights, cumulative worker limiter
wait and stage timings. Local completed-candle cache is separate from current.
Publication validates the complete staging tree and retains previous_<snapshot_id>
for rollback. Copy after the successful publication message; directory replacement
on Windows is not a single atomic exchange for concurrent readers.
