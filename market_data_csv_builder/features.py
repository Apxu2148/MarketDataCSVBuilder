"""Stateless, left-looking implementations of the 29 MarketDataVault features."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import pandas as pd

from .cancellation import CancellationToken

NUMERIC_FEATURE_IDS = (
    "sma_10",
    "sma_15",
    "sma_20",
    "sma_30",
    "sma_50",
    "sma_100",
    "bb_upper_20_2",
    "bb_lower_20_2",
    "donchian_upper_20",
    "donchian_lower_20",
    "donchian_middle_20",
)
PATTERN_FEATURE_IDS = (
    "possible_doji",
    "outside_bar",
    "close_extreme_10",
    "triangle_exit",
    "three_candle_trend",
    "three_candle_trend_correction",
)
STRUCTURAL_FEATURE_IDS = tuple(
    f"{group}_{side}_{horizon}"
    for group in ("levels", "extrema")
    for side in ("resistance", "support")
    for horizon in ("short", "medium", "long")
)
FEATURE_IDS = (*NUMERIC_FEATURE_IDS, *PATTERN_FEATURE_IDS, *STRUCTURAL_FEATURE_IDS)

_PATTERN_LOOKBACKS = {
    "possible_doji": 1,
    "outside_bar": 2,
    "close_extreme_10": 1,
    "triangle_exit": 3,
    "three_candle_trend": 3,
    "three_candle_trend_correction": 4,
}
_HORIZONS = {"short": (10, 1), "medium": (100, 10), "long": (1000, 100)}


class FeatureCalculationError(RuntimeError):
    pass


def calculate_features(
    candles: pd.DataFrame, *, cancellation_token: CancellationToken | None = None
) -> pd.DataFrame:
    """Return a copy with all 29 features; every output uses only rows through itself."""
    token = cancellation_token or CancellationToken()
    token.raise_if_requested()
    required = {"timestamp", "is_closed", "open", "high", "low", "close"}
    missing = sorted(required - set(candles.columns))
    if missing:
        raise FeatureCalculationError(f"Missing candle columns: {', '.join(missing)}")
    if candles.empty:
        result = candles.copy()
        for feature_id in FEATURE_IDS:
            result[feature_id] = pd.Series(dtype="object")
        return result
    if not candles["timestamp"].is_monotonic_increasing or candles["timestamp"].duplicated().any():
        raise FeatureCalculationError("Candle timestamps must be unique and increasing")
    numeric = candles[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
    if numeric.isna().any().any() or not numeric.map(math.isfinite).all().all():
        raise FeatureCalculationError("OHLC values must be finite numbers")
    if (numeric[["open", "high", "low", "close"]] <= 0).any().any():
        raise FeatureCalculationError("OHLC values must be positive")
    if (numeric["high"] < numeric[["open", "close", "low"]].max(axis=1)).any() or (
        numeric["low"] > numeric[["open", "close", "high"]].min(axis=1)
    ).any():
        raise FeatureCalculationError("OHLC range is inconsistent")

    result = candles.copy().reset_index(drop=True)
    closes = numeric["close"].reset_index(drop=True)
    highs = numeric["high"].reset_index(drop=True)
    lows = numeric["low"].reset_index(drop=True)
    for period in (10, 15, 20, 30, 50, 100):
        token.raise_if_requested()
        result[f"sma_{period}"] = closes.rolling(period, min_periods=period).mean()
    deviation = closes.rolling(20, min_periods=20).std(ddof=0)
    result["bb_upper_20_2"] = result["sma_20"] + 2.0 * deviation
    result["bb_lower_20_2"] = result["sma_20"] - 2.0 * deviation
    upper = highs.rolling(20, min_periods=20).max()
    lower = lows.rolling(20, min_periods=20).min()
    result["donchian_upper_20"] = upper
    result["donchian_lower_20"] = lower
    result["donchian_middle_20"] = (upper + lower) / 2.0

    for feature_id, lookback in _PATTERN_LOOKBACKS.items():
        token.raise_if_requested()
        values: list[int | pd._libs.missing.NAType] = []
        for index in range(len(result)):
            if index % 64 == 0:
                token.raise_if_requested()
            values.append(pd.NA if index + 1 < lookback else _pattern_value(result, index, feature_id))
        result[feature_id] = pd.array(values, dtype="Int8")

    for group in ("levels", "extrema"):
        for side in ("resistance", "support"):
            for horizon, (n, m) in _HORIZONS.items():
                token.raise_if_requested()
                feature_id = f"{group}_{side}_{horizon}"
                result[feature_id] = _structural_values(
                    result,
                    kind="level" if group == "levels" else "extremum",
                    side=side,
                    n=n,
                    m=m,
                    cancellation_token=token,
                )
    return result


def _pattern_value(candles: pd.DataFrame, index: int, feature_id: str) -> int:
    current = candles.iloc[index]
    if feature_id == "possible_doji":
        midpoint = (float(current["high"]) + float(current["low"])) / 2.0
        if float(current["open"]) > midpoint and float(current["close"]) > midpoint:
            return 1
        if float(current["open"]) < midpoint and float(current["close"]) < midpoint:
            return -1
        return 0
    if feature_id == "close_extreme_10":
        high, low, close = float(current["high"]), float(current["low"]), float(current["close"])
        if high <= low:
            return 0
        candle_range = high - low
        if candle_range > 10.0 * (high - close):
            return 1
        if candle_range > 10.0 * (close - low):
            return -1
        return 0
    previous = candles.iloc[index - 1]
    if feature_id == "outside_bar":
        outside = float(current["high"]) > float(previous["high"]) and float(current["low"]) < float(previous["low"])
        if outside and float(current["close"]) > float(current["open"]):
            return 1
        if outside and float(current["close"]) < float(current["open"]):
            return -1
        return 0
    two_back = candles.iloc[index - 2]
    if feature_id == "triangle_exit":
        inside = float(previous["high"]) < float(two_back["high"]) and float(previous["low"]) > float(two_back["low"])
        if inside and float(current["high"]) > float(previous["high"]) and float(current["low"]) > float(previous["low"]):
            return 1
        if inside and float(current["high"]) < float(previous["high"]) and float(current["low"]) < float(previous["low"]):
            return -1
        return 0
    if feature_id == "three_candle_trend":
        return _three_candle_trend(two_back, previous, current)
    if feature_id == "three_candle_trend_correction":
        return _trend_correction(candles.iloc[index - 3], two_back, previous, current)
    raise FeatureCalculationError(f"Unknown pattern feature: {feature_id}")


def _three_candle_trend(two_back: pd.Series, previous: pd.Series, current: pd.Series) -> int:
    rows = (two_back, previous, current)
    bullish = all(float(row["close"]) > float(row["open"]) for row in rows)
    rising = (
        float(current["high"]) > float(previous["high"]) > float(two_back["high"])
        and float(current["low"]) > float(previous["low"]) > float(two_back["low"])
    )
    if bullish and rising:
        return 1
    bearish = all(float(row["close"]) < float(row["open"]) for row in rows)
    falling = (
        float(current["high"]) < float(previous["high"]) < float(two_back["high"])
        and float(current["low"]) < float(previous["low"]) < float(two_back["low"])
    )
    return -1 if bearish and falling else 0


def _trend_correction(
    three_back: pd.Series, two_back: pd.Series, previous: pd.Series, current: pd.Series
) -> int:
    bullish = _three_candle_trend(three_back, two_back, previous) == 1
    bullish_correction = (
        float(current["close"]) < float(current["open"])
        and float(current["high"]) < float(previous["high"])
        and float(current["low"]) < float(previous["low"])
        and float(current["low"]) > float(three_back["low"])
    )
    if bullish and bullish_correction:
        return 1
    bearish = _three_candle_trend(three_back, two_back, previous) == -1
    bearish_correction = (
        float(current["close"]) > float(current["open"])
        and float(current["high"]) > float(previous["high"])
        and float(current["low"]) > float(previous["low"])
        and float(current["high"]) < float(three_back["high"])
    )
    return -1 if bearish and bearish_correction else 0


StructuralSide = Literal["resistance", "support"]
StructuralKind = Literal["level", "extremum"]


@dataclass(frozen=True)
class _Event:
    candidate: int
    price: float
    valid_from: int
    valid_stop: int


def _structural_values(
    candles: pd.DataFrame,
    *,
    kind: StructuralKind,
    side: StructuralSide,
    n: int,
    m: int,
    cancellation_token: CancellationToken,
) -> pd.Series:
    length = len(candles)
    values: list[object] = [None if index < n - 1 else [] for index in range(length)]
    column = "high" if side == "resistance" else "low"
    prices = [float(value) for value in candles[column]]
    opens = [float(value) for value in candles["open"]]
    closes = [float(value) for value in candles["close"]]
    events: list[_Event] = []
    for candidate in range(m, length - m):
        if candidate % 32 == 0:
            cancellation_token.raise_if_requested()
        price = prices[candidate]
        neighbors = prices[candidate - m : candidate] + prices[candidate + 1 : candidate + m + 1]
        is_extremum = all(price > value for value in neighbors) if side == "resistance" else all(price < value for value in neighbors)
        if not is_extremum:
            continue
        valid_from = max(n - 1, candidate + m)
        valid_stop = min(length, candidate + n - m)
        if valid_from >= valid_stop:
            continue
        if kind == "level":
            for index in range(candidate + 1, valid_stop):
                broken = (
                    opens[index] >= price or closes[index] >= price
                    if side == "resistance"
                    else opens[index] <= price or closes[index] <= price
                )
                if broken:
                    valid_stop = index
                    break
            if valid_stop <= valid_from:
                continue
        events.append(_Event(candidate, price, valid_from, valid_stop))
    for event in events:
        cancellation_token.raise_if_requested()
        for output in range(event.valid_from, event.valid_stop):
            value = values[output]
            if isinstance(value, list):
                value.append((event.candidate - output, event.price))
    for index, value in enumerate(values):
        if index % 128 == 0:
            cancellation_token.raise_if_requested()
        if isinstance(value, list):
            values[index] = tuple(sorted(value, key=lambda item: item[0], reverse=True))
    return pd.Series(values, dtype="object")
