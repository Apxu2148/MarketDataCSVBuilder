from __future__ import annotations

import csv
import json
import math
import time
from pathlib import Path

import pandas as pd

from ..export import BASE_COLUMNS, _structural_json
from ..features import FEATURE_IDS, STRUCTURAL_FEATURE_IDS, PATTERN_FEATURE_IDS, calculate_features
from .provider import TIMEFRAMES, WARMUP_BARS

RETURNS = tuple(f"return_{n}_bar" for n in (1, 5, 20))
SERIES_COLUMNS = ("snapshot_id", *BASE_COLUMNS, "timeframe", *FEATURE_IDS, "atr_wilder_14", *RETURNS)
CATALOG_COLUMNS = ("snapshot_id", "generated_at_utc", "symbol", "timeframe", "status", "relative_path",
    "requested_closed_bars", "closed_bars", "has_1200_closed_bars", "has_provisional_bar", "first_timestamp",
    "last_closed_timestamp", "provisional_timestamp", "history_fetched_at_utc", "final_refresh_at_utc",
    "feature_status", "error", "gap_count", "missing_current_bar")
LATEST_COLUMNS = (*SERIES_COLUMNS, "candle_state", "bars_available")
TICKER_FIELDS = {"last_price": "lastPrice", "mark_price": "markPrice", "index_price": "indexPrice",
    "turnover24h": "turnover24h", "volume24h": "volume24h", "price_change_24h": "price24hPcnt",
    "high_price_24h": "highPrice24h", "low_price_24h": "lowPrice24h", "bid1_price": "bid1Price",
    "bid1_size": "bid1Size", "ask1_price": "ask1Price", "ask1_size": "ask1Size",
    "funding_rate": "fundingRate", "open_interest": "openInterest"}
UNIVERSE_COLUMNS = ("snapshot_id", "symbol", "status", "contract_type", "quote_coin", "settle_coin",
    "base_coin", "eligible", "liquidity_threshold_usdt", "eligibility_turnover24h", "eligibility_at_utc",
    *TICKER_FIELDS, "next_funding_time", "tick_size", "qty_step", "min_order_qty", "max_order_qty",
    "min_notional", "funding_interval_minutes", "ticker_fetched_at_utc", "funding_fetched_at_utc",
    "instrument_metadata_fetched_at_utc")


def calculate(frame, token, previous=None, reuse_export=0):
    if previous is None:
        result = calculate_features(frame, cancellation_token=token)
    else:
        # Recompute refreshed/new rows with the full finite structural lookback.
        result = frame.copy()
        old = previous.set_index("timestamp")
        common = result.timestamp.isin(old.index)
        changed = ~common
        for name in ("open", "high", "low", "close", "is_closed"):
            changed |= result[name].ne(result.timestamp.map(old[name]))
        if reuse_export:
            # Persisted exports omit warm-up rows. Their absent feature values are
            # not changes to the underlying candles. Known changed rows still
            # invalidate the suffix, including changes before the export window.
            changed &= ~(~common & (result.timestamp < previous.timestamp.min()) & (result.index < reuse_export))
        positions = [i for i, value in enumerate(changed) if value]
        first = min(positions) if positions else len(result)
        for name in FEATURE_IDS:
            result[name] = result.timestamp.map(old[name])
        if first < len(result):
            offset = max(0, first - WARMUP_BARS)
            tail = calculate_features(frame.iloc[offset:].reset_index(drop=True), cancellation_token=token)
            for name in FEATURE_IDS:
                values = result[name].tolist()
                values[first:] = tail[name].iloc[first - offset:].tolist()
                result[name] = pd.Series(values, index=result.index)
        for name in PATTERN_FEATURE_IDS:
            result[name] = pd.array(result[name], dtype="Int8")
    previous = result.close.shift(1)
    tr = pd.concat([result.high - result.low, (result.high - previous).abs(),
                    (result.low - previous).abs()], axis=1).max(axis=1)
    # First TR is high-low when there is no previous close.
    atr = [float("nan")] * len(result)
    if len(result) >= 14:
        atr[13] = tr.iloc[:14].mean()
        ranges = tr.tolist()
        for index in range(14, len(result)):
            atr[index] = (atr[index - 1] * 13 + ranges[index]) / 14
    result["atr_wilder_14"] = atr
    for periods, name in zip((1, 5, 20), RETURNS):
        result[name] = result.close / result.close.shift(periods) - 1
    return result


def load_reusable_series(path, symbol, timeframe, source="bybit"):
    """Validate a staged series before reusing its already computed feature prefix."""
    frame = pd.read_csv(path, float_precision="round_trip")
    if frame.empty or set(SERIES_COLUMNS) - set(frame.columns):
        raise ValueError("Incomplete staged CSV/schema")
    for column, expected in (("symbol", symbol), ("timeframe", timeframe),
                             ("source", source), ("market", "perpetual" if source == "hyperliquid" else "linear_perpetual")):
        if not frame[column].eq(expected).all():
            raise ValueError("Staged series identity mismatch")
    frame["timestamp"] = pd.to_datetime(frame.timestamp, utc=True, errors="raise")
    if frame.timestamp.duplicated().any() or not frame.timestamp.is_monotonic_increasing:
        raise ValueError("Invalid staged timestamps")
    for column in ("is_closed", "provisional"):
        if frame[column].dtype != bool:
            raise ValueError("Invalid staged candle flags")
    if not frame.provisional.eq(~frame.is_closed).all() or frame.provisional.sum() > 1:
        raise ValueError("Contradictory staged candle flags")
    if frame.provisional.any() and not frame.iloc[-1].provisional:
        raise ValueError("Provisional staged candle must be last")
    numbers = frame[["open", "high", "low", "close", "volume", "turnover"]]
    if source == "hyperliquid":
        if not numbers.turnover.dropna().map(math.isfinite).all() or (numbers.turnover.dropna() < 0).any():
            raise ValueError("Invalid staged turnover")
        numbers = numbers.drop(columns="turnover")
    if not numbers.map(math.isfinite).all().all() or (numbers.iloc[:, :4] <= 0).any().any() or (numbers.iloc[:, 4:] < 0).any().any():
        raise ValueError("Invalid staged OHLCV")
    if (frame.high < frame[["open", "low", "close"]].max(axis=1)).any() or (frame.low > frame[["open", "high", "close"]].min(axis=1)).any():
        raise ValueError("Invalid staged OHLC range")
    for name in STRUCTURAL_FEATURE_IDS:
        def decode(value):
            if pd.isna(value):
                return None
            data = json.loads(value)
            if not isinstance(data, list) or any(not isinstance(pair, list) or len(pair) != 2 or
                type(pair[0]) is not int or pair[0] > 0 or not math.isfinite(pair[1]) or pair[1] <= 0 for pair in data):
                raise ValueError("Invalid staged structural feature")
            return tuple(tuple(pair) for pair in data)
        frame[name] = frame[name].map(decode)
    for name in set(FEATURE_IDS) - set(STRUCTURAL_FEATURE_IDS):
        values = pd.to_numeric(frame[name], errors="raise")
        if not values.dropna().map(math.isfinite).all():
            raise ValueError("Non-finite staged feature")
        frame[name] = pd.array(values, dtype="Int8") if name in PATTERN_FEATURE_IDS else values
    if len(frame) >= 1000:
        for name in FEATURE_IDS:
            value = frame.iloc[-1][name]
            if value is None or value is pd.NA or (isinstance(value, float) and math.isnan(value)):
                raise ValueError("Incomplete staged feature tail")
    return frame


def serialize(frame):
    output = frame.copy()
    output["timestamp"] = output.timestamp.map(lambda stamp: stamp.isoformat())
    for name in ("is_closed", "provisional"):
        output[name] = output[name].map(lambda value: "true" if value else "false")
    for name in STRUCTURAL_FEATURE_IDS:
        output[name] = output[name].map(_structural_json)
    return output


def write_csv(path, rows, columns):
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False, encoding="utf-8",
        lineterminator="\n", na_rep="", float_format="%.15g")


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False), encoding="utf-8")


def universe_row(item, ticker, initial, snapshot, threshold, metadata_at, ticker_at, eligibility_at):
    lot = item.get("lotSizeFilter", {})
    funding_time = ticker.get("nextFundingTime")
    return dict(snapshot_id=snapshot, symbol=item["symbol"], status=item["status"],
        contract_type=item["contractType"], quote_coin=item["quoteCoin"], settle_coin=item["settleCoin"],
        base_coin=item.get("baseCoin"), eligible="true", liquidity_threshold_usdt=threshold,
        eligibility_turnover24h=initial["turnover24h"], eligibility_at_utc=eligibility_at,
        **{key: ticker.get(value) for key, value in TICKER_FIELDS.items()},
        next_funding_time=pd.to_datetime(int(funding_time), unit="ms", utc=True).isoformat() if funding_time else None,
        tick_size=item.get("priceFilter", {}).get("tickSize"), qty_step=lot.get("qtyStep"),
        min_order_qty=lot.get("minOrderQty"), max_order_qty=lot.get("maxOrderQty"),
        min_notional=lot.get("minNotionalValue"), funding_interval_minutes=item.get("fundingInterval"),
        ticker_fetched_at_utc=ticker_at, funding_fetched_at_utc=ticker_at,
        instrument_metadata_fetched_at_utc=metadata_at)


def read_csv(path):
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"Missing CSV headers: {path}")
        return list(reader)


def validate_snapshot(directory: Path):
    for name in ("manifest.json", "README_INTRADAY_MARKET_DATA.md", "universe.csv", "catalog.csv", "latest_features.csv", "run_report.json"):
        if not (directory / name).is_file():
            raise ValueError(f"Missing snapshot file: {name}")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    report = json.loads((directory / "run_report.json").read_text(encoding="utf-8"))
    snapshot = manifest["snapshot_id"]
    if manifest["profile"] not in {"intraday", "intraday_hyperliquid"} or report["snapshot_id"] != snapshot:
        raise ValueError("Snapshot identity mismatch")
    catalog = read_csv(directory / "catalog.csv")
    universe = read_csv(directory / "universe.csv")
    latest = read_csv(directory / "latest_features.csv")
    for row in [*catalog, *universe, *latest]:
        if row["snapshot_id"] != snapshot:
            raise ValueError("Mixed snapshot IDs")
    symbols = {row["symbol"] for row in universe}
    keys = {(row["symbol"], row["timeframe"]) for row in catalog}
    if len(symbols) != len(universe) or len(keys) != len(catalog) or keys != {(s, tf) for s in symbols for tf in manifest["timeframes"]}:
        raise ValueError("Invalid universe/catalog coverage")
    latest_map = {(row["symbol"], row["timeframe"], row["candle_state"]): row for row in latest}
    if len(latest_map) != len(latest):
        raise ValueError("Duplicate latest_features rows")
    successful = 0
    expected_latest = set()
    paths = set()
    for entry in catalog:
        if entry["status"] == "FAILED":
            if entry["relative_path"] or not entry["error"]:
                raise ValueError("Failed series must have an error and no path")
            continue
        if entry["status"] not in {"READY", "PARTIAL"}:
            raise ValueError("Unknown catalog status")
        relative = Path(entry["relative_path"])
        path = (directory / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(directory.resolve()) or path in paths:
            raise ValueError("Unsafe or duplicate catalog path")
        paths.add(path)
        rows = read_csv(path)
        if not rows or set(SERIES_COLUMNS) - set(rows[0]):
            raise ValueError("Invalid series schema")
        timestamps = [pd.Timestamp(row["timestamp"]) for row in rows]
        if any(stamp.tzinfo is None or stamp.utcoffset().total_seconds() for stamp in timestamps):
            raise ValueError("Non-UTC timestamp")
        if timestamps != sorted(set(timestamps)):
            raise ValueError("Duplicate or unordered candles")
        closed, provisional = [], []
        for row in rows:
            if row["snapshot_id"] != snapshot or row["symbol"] != entry["symbol"] or row["timeframe"] != entry["timeframe"]:
                raise ValueError("Series identity mismatch")
            values = dict(row)
            if manifest["profile"] == "intraday_hyperliquid" and values["turnover"] == "N/A":
                values["turnover"] = "0"  # validation only; serialized unknown stays N/A
            o, h, l, c, volume, turnover = [float(values[name]) for name in ("open", "high", "low", "close", "volume", "turnover")]
            if not all(math.isfinite(value) for value in (o, h, l, c, volume, turnover)) or min(o,h,l,c) <= 0 or min(volume,turnover) < 0 or h < max(o,l,c) or l > min(o,h,c):
                raise ValueError("Invalid serialized OHLCV")
            state = (row["is_closed"], row["provisional"])
            if state == ("true", "false"):
                closed.append(row)
            elif state == ("false", "true"):
                provisional.append(row)
            else:
                raise ValueError("Contradictory candle state")
        if len(closed) > manifest["requested_closed_bars"] or len(provisional) > 1 or (provisional and provisional[-1] != rows[-1]):
            raise ValueError("Invalid closed/provisional window")
        if int(entry["closed_bars"]) != len(closed):
            raise ValueError("Closed count mismatch")
        if (entry["first_timestamp"] != rows[0]["timestamp"] or
            entry["last_closed_timestamp"] != (closed[-1]["timestamp"] if closed else "") or
            entry["provisional_timestamp"] != (provisional[-1]["timestamp"] if provisional else "")):
            raise ValueError("Catalog timestamp mismatch")
        if entry["has_provisional_bar"] != str(bool(provisional)).lower() or entry["has_1200_closed_bars"] != str(len(closed) >= 1200).lower():
            raise ValueError("Catalog flags mismatch")
        for state, subset in (("CLOSED", closed), ("PROVISIONAL", provisional)):
            if not subset:
                continue
            key = (entry["symbol"], entry["timeframe"], state)
            expected_latest.add(key)
            view = latest_map.get(key)
            if view is None or any(view[column] != subset[-1][column] for column in SERIES_COLUMNS):
                raise ValueError("latest_features differs from raw series")
        successful += 1
    if expected_latest != set(latest_map):
        raise ValueError("Unexpected latest_features rows")
    if not successful:
        raise ValueError("No successful series; current remains untouched")
    if manifest["expected_series_count"] != len(catalog) or manifest["successful_series_count"] != successful or manifest["failed_series_count"] != len(catalog) - successful:
        raise ValueError("Manifest counts mismatch")
    if manifest["eligible_contracts"] != len(universe) or manifest["partial_series_count"] != sum(row["status"] == "PARTIAL" for row in catalog):
        raise ValueError("Manifest universe/partial count mismatch")
    if report["counts"]["successful_series"] != successful or report["counts"]["failed_series"] != len(catalog) - successful:
        raise ValueError("Report counts mismatch")


def publish(staging, output_root, snapshot, report=None):
    """Validated promotion with rollback; retain old snapshot for recovery."""
    current = output_root / "current"
    started = time.perf_counter()
    previous = output_root / ("previous_" + snapshot)
    moved = current.exists()
    if moved:
        current.replace(previous)
    try:
        staging.replace(current)
        if report is not None:
            elapsed = time.perf_counter() - started
            report["elapsed_seconds"]["publication"] = elapsed
            report["elapsed_seconds"]["total"] += elapsed
            temporary = current / "run_report.json.tmp"
            other_bytes = sum(path.stat().st_size for path in current.rglob("*")
                              if path.is_file() and path.name != "run_report.json")
            for _ in range(4):
                report["snapshot_bytes"] = other_bytes + len(json.dumps(report,
                    indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            write_json(temporary, report)
            temporary.replace(current / "run_report.json")
    except BaseException:
        if current.exists() and not staging.exists():
            current.replace(staging)
        if moved:
            previous.replace(current)
        raise
    return current


def snapshot_readme(manifest, consumer):
    if manifest["profile"] == "intraday_hyperliquid":
        return (f"# Hyperliquid INTRADAY snapshot\n\nSnapshot: {manifest['snapshot_id']}\n"
                f"Completed UTC: {manifest['completed_at_utc']}\n\n" + consumer)
    return f"""# INTRADAY market data snapshot

Snapshot: {manifest['snapshot_id']}
Started UTC: {manifest['started_at_utc']}
Completed UTC: {manifest['completed_at_utc']}
Final bulk refresh UTC: {manifest['final_refresh_at_utc']}
Venue: Bybit; market: Linear USDT Perpetual; profile: intraday.
Rolling turnover24h threshold: {manifest['liquidity_filter']['threshold_usdt']} USDT.
Timeframes: {', '.join(manifest['timeframes'])}.
Export: up to {manifest['requested_closed_bars']} completed candles plus one current provisional.

## Files and schema

manifest.json describes identity, coverage and freshness. universe.csv contains one row per
eligible instrument, ticker/funding/book-top/specifications and their fetch timestamps.
catalog.csv inventories every symbol/timeframe, including failures; relative_path locates
series/<symbol>/<timeframe>.csv. latest_features.csv contains the last CLOSED and, when
available, PROVISIONAL row per series. run_report.json contains errors, timings and API metrics.
All CSVs use UTF-8, comma delimiters, dot decimals and ISO 8601 UTC candle-open timestamps.
Volume is base-coin quantity; turnover is actual USDT quote turnover.
Identity columns: snapshot_id, source, market, symbol, instrument_name, contract_type, dex,
quote_currency, timeframe. OHLCV, turnover_currency, is_closed and provisional follow.
is_closed=true/provisional=false means completed; false/true means unfinished, including features.

## Indicators

Shared feature columns: {', '.join(FEATURE_IDS)}.
Structural JSON lists contain [relative_bar_offset, price] pairs, most recent first;
empty cell means insufficient history, [] means computed with no surviving structures.
No future candles are used. Long structures use a 1000-bar trailing horizon; {WARMUP_BARS}
extra candles are fetched before the export window. Short-history instruments remain included.
ATR: TR=max(high-low, abs(high-previous_close), abs(low-previous_close)); first TR=high-low.
atr_wilder_14 starts with the mean of 14 TRs, then (previous_ATR*13+TR)/14.
ATR is seeded at the start of the fixed extended calculation window, not listing inception.
The seed influence after 999 warm-up bars is below 2e-32. Cached and fresh runs use the same
window policy. PROVISIONAL ATR is unfinished; use the CLOSED 5m row when completed ATR is required.
return_1_bar, return_5_bar, return_20_bar are decimal close/lagged-close-1 values.
Insufficient history is empty/null, never zero-filled. No trading signals or candidate selection.

## Universe and freshness

Eligibility is fixed from the initial bulk snapshot using rolling 24-hour turnover (not a
historical average), without TOP-N. eligibility_turnover24h and eligibility_at_utc preserve
that decision; turnover24h and other prices reflect the final ticker fetch. They may have
crossed the threshold during the run. Metadata describes instrument state at its fetch time.
Final refresh updates tails for all enabled timeframes and recalculates only the affected tail
with a 999-bar overlap; 5m is last. Check each catalog history_fetched_at_utc and
final_refresh_at_utc, plus candle timestamps. Data are fetched over an interval, not simultaneously.
PARTIAL means a known gap or failed refresh; FAILED means no usable exported series.
Missing current candle is explicitly flagged and never manufactured. Short history alone is not failure.

{consumer}
"""
