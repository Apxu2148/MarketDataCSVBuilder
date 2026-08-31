from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from market_data_csv_builder.features import FEATURE_IDS, calculate_features


def _frame(count: int = 120) -> pd.DataFrame:
    closes = [float(index + 1) for index in range(count)]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=count, freq="1D", tz="UTC"),
            "is_closed": [True] * count,
            "open": [value - 0.25 for value in closes],
            "high": [value + 1.0 for value in closes],
            "low": [max(0.01, value - 1.0) for value in closes],
            "close": closes,
        }
    )


def test_all_29_feature_ids_are_emitted(candle_frame: pd.DataFrame) -> None:
    result = calculate_features(candle_frame)
    assert len(FEATURE_IDS) == 29
    assert len(set(FEATURE_IDS)) == 29
    assert all(feature_id in result for feature_id in FEATURE_IDS)


@pytest.mark.parametrize("period", [10, 15, 20, 30, 50, 100])
def test_sma_uses_left_inclusive_window(period: int) -> None:
    result = calculate_features(_frame())
    assert pd.isna(result.loc[period - 2, f"sma_{period}"])
    assert result.loc[period - 1, f"sma_{period}"] == pytest.approx((1 + period) / 2)


def test_bollinger_and_donchian_match_canonical_formulas() -> None:
    frame = _frame(25)
    frame.loc[7, "high"] = 500.0
    frame.loc[8, "low"] = 0.001
    result = calculate_features(frame)
    window = frame.iloc[4:24]
    assert result.loc[23, "bb_upper_20_2"] == pytest.approx(window["close"].mean() + 2 * window["close"].std(ddof=0))
    assert result.loc[23, "bb_lower_20_2"] == pytest.approx(window["close"].mean() - 2 * window["close"].std(ddof=0))
    assert result.loc[23, "donchian_upper_20"] == window["high"].max()
    assert result.loc[23, "donchian_lower_20"] == window["low"].min()


@pytest.mark.parametrize(
    ("feature", "rows", "expected"),
    [
        ("possible_doji", [(8, 10, 0.1, 9)], 1),
        ("possible_doji", [(2, 10, 0.1, 1)], -1),
        ("outside_bar", [(4, 10, 1, 6), (4, 11, 0.5, 7)], 1),
        ("outside_bar", [(4, 10, 1, 6), (7, 11, 0.5, 4)], -1),
        ("close_extreme_10", [(8, 10, 0.1, 9.1)], 1),
        ("triangle_exit", [(4, 10, 1, 6), (4, 9, 2, 6), (5, 10, 3, 7)], 1),
        ("three_candle_trend", [(1, 5, 0.1, 4), (2, 6, 1, 5), (3, 7, 2, 6)], 1),
        ("three_candle_trend_correction", [(1, 5, 0.1, 4), (2, 6, 1, 5), (3, 7, 2, 6), (5, 6, 1, 3)], 1),
    ],
)
def test_pattern_truth_tables(feature: str, rows: list[tuple[float, float, float, float]], expected: int) -> None:
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=len(rows), freq="1D", tz="UTC"),
            "is_closed": [True] * len(rows),
            "open": [row[0] for row in rows],
            "high": [row[1] for row in rows],
            "low": [row[2] for row in rows],
            "close": [row[3] for row in rows],
        }
    )
    assert calculate_features(frame).iloc[-1][feature] == expected


def test_insufficient_history_is_empty_not_artificial() -> None:
    result = calculate_features(_frame(5))
    assert pd.isna(result.iloc[-1]["sma_10"])
    assert pd.isna(result.iloc[0]["outside_bar"])
    assert result.iloc[-1]["levels_resistance_short"] is None
    assert result.iloc[-1]["extrema_support_long"] is None


def test_structural_lists_match_strict_extremum_and_level_break_rules() -> None:
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=13, freq="1D", tz="UTC"),
            "is_closed": [True] * 13,
            "open": [10.0] * 13,
            "high": [11.0] * 13,
            "low": [9.0] * 13,
            "close": [10.0] * 13,
        }
    )
    frame.loc[3, "high"] = 20.0
    frame.loc[6, "high"] = 20.0
    frame.loc[8, "high"] = 30.0
    result = calculate_features(frame)
    assert result.loc[9, "extrema_resistance_short"] == ((-1, 30.0), (-3, 20.0), (-6, 20.0))
    frame.loc[10, ["open", "close", "high"]] = [20.0, 10.0, 25.0]
    broken = calculate_features(frame)
    assert broken.loc[10, "levels_resistance_short"] == ((-2, 30.0),)


def test_anti_lookahead_prefixes_match_full_calculation(candle_frame: pd.DataFrame) -> None:
    full = calculate_features(candle_frame)
    for length in (25, 125, 1001, 1201):
        prefix = calculate_features(candle_frame.iloc[:length].copy())
        for feature_id in FEATURE_IDS:
            expected = full.iloc[length - 1][feature_id]
            actual = prefix.iloc[-1][feature_id]
            if expected is pd.NA or (isinstance(expected, float) and pd.isna(expected)):
                assert actual is pd.NA or pd.isna(actual)
            else:
                assert actual == pytest.approx(expected) if isinstance(expected, float) else actual == expected

