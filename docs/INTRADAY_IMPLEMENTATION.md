# Implementation decisions and verification

One codebase, one unchanged `features.calculate_features`, separate profile pipelines.
APX CLI defaults, source adapters, daily liquidity filter and output schemas remain
unchanged. Only an additive generic cooldown method is added to the existing limiter.
INTRADAY uses that limiter, BoundedExecutor, CancellationToken, StageProgress,
the shared structural serializer and closed/current slicing. Its completed-bar cache
uses the same temporary-write/replace pattern as the existing HTTP cache, with a
separate versioned provider/market/symbol/timeframe namespace.

## Feature history audit

| Features | Requirement |
| --- | --- |
| SMA 10/15/20/30/50/100 | respective rolling length, at most 100 bars |
| Bollinger 20/2 and Donchian 20 | 20 bars, population deviation for Bollinger |
| possible_doji / close_extreme_10 | 1 bar (the latter is a within-candle rule) |
| outside_bar | 2 bars |
| triangle_exit / three_candle_trend | 3 bars |
| three_candle_trend_correction | 4 bars |
| levels/extrema short | n=10, m=1 |
| levels/extrema medium | n=100, m=10 |
| levels/extrema long | n=1000, m=100 |
| returns | 2/6/21 bars |

Structural candidates use m neighbors on either side and expire at candidate+n-m.
For an output at t, the earliest contributing candidate is t-n+m+1; its earliest
neighbor is t-n+1. Thus n bars suffice, and 999 predecessors reproduce all structural
features for the first exported bar. The left-looking engine confirms an event only
after its right neighbors exist; this is not look-ahead into unavailable future data.
Deterministic tests compare the 999-predecessor calculation against a longer series.

ATR requires recursive state. We use a reproducible fixed extended window, seed the
first 14 valid TRs by arithmetic mean and then Wilder smoothing. First TR=high-low.
With 999 predecessors the initial seed influence is attenuated by at least
(13/14)^986 at the first exported bar. This is a documented finite-seed approximation
to an inception-based ATR, not a different shared-feature algorithm. Short series
retain empty ATR until the 14th valid TR. Cache and cold runs use the same window.

Final refresh recalculates changed and appended rows with 999 predecessors. Unchanged
rows keep the shared engine's values. ATR/returns are recalculated over the complete
extended window; no indicator is calculated from the truncated export window alone.
All enabled TFs are refreshed in descending duration, 5m last; ticker/funding is last.

## Time, gaps and failures

Rows are sorted/deduplicated on provider UTC candle-open timestamps. Completion uses
timestamp+interval <= request cutoff; no provisional row is cached. No synthetic bars.
Internal gaps in the export window are PARTIAL and reported; missing current is a flag.
Features on a gapped series count observed bars, so consumers must inspect status.
Short history is retained and does not itself mean a failed series.

Universe membership is fixed from the initial fresh metadata/ticker join. Both quote
and settlement must be USDT, status Trading, contractType LinearPerpetual. The final
ticker refresh does not redefine membership mid-run. Both eligibility and current
turnover timestamps/values are exported so threshold crossings remain explicit.

Network retries share one paced limiter, including feedback from X-Bapi reset headers,
HTTP 429 and API 10006. Five attempts means initial request plus configured max_retries=4.
Public request paths are explicitly restricted to instruments-info, tickers and kline.
No orders, credentials, account endpoints, funding history or order book are used.

Publication validates all inventory rows, paths, candle states and screening/raw parity.
An exclusive profile lock prevents overlapping runs. The old current is renamed only
after validation; failed staging promotion restores it. Windows has no single directory
exchange primitive: consumers copy after the success message. Prior snapshots are kept
for recovery. A machine/process crash between renames can leave previous_<snapshot_id>
available for manual recovery; it is never deleted by the new run.

Timings: initial discovery includes metadata+ticker; history by timeframe is wall time;
feature_calculation is the initial full calculation stage; final_refresh includes tail
network/feature/export work; export is a nested component. These timings overlap and
must not be summed. total is elapsed through validation and final report preparation.

CPU calculations use a bounded ProcessPoolExecutor; network requests retain their shared
threaded limiter. Workers only evaluate DataFrames and perform no I/O. A spawned-worker
regression test verifies numerical and structural equivalence to the shared engine.
The parent stops submissions on cancellation and waits for at most the active calculations.
