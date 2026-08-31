from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from market_data_csv_builder.config import AppConfig, GeneralConfig
from market_data_csv_builder.export import SnapshotWriter, dataset_relative_path, slice_closed_and_current
from market_data_csv_builder.features import FEATURE_IDS
from market_data_csv_builder.liquidity import calculate_average_turnover
from market_data_csv_builder.models import Candle, Instrument
from market_data_csv_builder.pipeline import run_pipeline
from market_data_csv_builder.utils import safe_path_component
from market_data_csv_builder.utils import parse_as_of


def _candles(count: int, *, current: bool = True, turnover: float = 1000.0) -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    result = []
    for index in range(count):
        close = 100 + index
        is_closed = not current or index < count - 1
        result.append(Candle(start + timedelta(days=index), close - 0.5, close + 1, close - 1, close, 10, turnover, "USD", is_closed))
    return result


def test_liquidity_uses_latest_30_closed_bars_and_excludes_current() -> None:
    candles = _candles(31)
    candles[-1] = Candle(candles[-1].timestamp, 1, 2, 0.5, 1, 1, 999_999_999, "USD", False)
    result = calculate_average_turnover(candles, 30)
    assert result.average_turnover == 1000
    assert result.closed_bars_used == 30


def test_short_history_and_full_compact_slicing() -> None:
    frame = pd.DataFrame({"timestamp": [item.timestamp for item in _candles(6)], "is_closed": [item.is_closed for item in _candles(6)]})
    full = slice_closed_and_current(frame, 100)
    compact = slice_closed_and_current(frame, 3)
    assert len(full) == 6
    assert len(compact) == 4
    assert compact.iloc[-1]["is_closed"] == False


def test_safe_windows_filenames_are_deterministic_and_original_is_not_used_as_path() -> None:
    first = safe_path_component("xyz:ABC/DEF?")
    assert first == safe_path_component("xyz:ABC/DEF?")
    assert all(character not in first for character in '<>:"/\\|?*')
    instrument = Instrument("hyperliquid", "perpetual", "xyz:ABC/DEF?", "ABC", "Perpetual", "xyz", "USDC")
    path = dataset_relative_path(instrument).as_posix()
    assert path.startswith("hyperliquid/xyz/")
    assert "ABC/DEF" not in path


def test_snapshot_rotation_keeps_only_current_and_previous(tmp_path: Path) -> None:
    writer = SnapshotWriter(tmp_path, "output")
    writer.begin()
    (writer.building / "marker.txt").write_text("one", encoding="utf-8")
    writer.publish()
    writer.begin()
    (writer.building / "marker.txt").write_text("two", encoding="utf-8")
    writer.publish()
    writer.begin()
    (writer.building / "marker.txt").write_text("three", encoding="utf-8")
    writer.publish()
    assert (tmp_path / "output/current/marker.txt").read_text(encoding="utf-8") == "three"
    assert (tmp_path / "output/previous/marker.txt").read_text(encoding="utf-8") == "two"


def test_as_of_requires_timezone_and_date_means_end_of_utc_day() -> None:
    parsed = parse_as_of("2026-08-30")
    assert parsed.tzinfo == UTC
    assert (parsed.hour, parsed.minute, parsed.second) == (23, 59, 59)
    try:
        parse_as_of("2026-08-30T12:00:00")
    except ValueError as exc:
        assert "timezone" in str(exc)
    else:
        raise AssertionError("naive datetime was accepted")


class FakeSource:
    name = "fake"
    max_workers = 3
    threshold = 100.0

    def discover(self) -> list[Instrument]:
        return [Instrument("fake", "perpetual", symbol, symbol, "Perpetual", "default", "USD") for symbol in ("GOOD", "DOWNLOAD", "FEATURE", "LOW")]

    def fetch_daily_candles(self, instrument: Instrument, closed_bars: int, as_of: datetime) -> list[Candle]:
        if instrument.symbol == "DOWNLOAD" and closed_bars > 3:
            raise TimeoutError("synthetic history timeout")
        turnover = 10.0 if instrument.symbol == "LOW" else 1000.0
        candles = _candles(min(closed_bars, 20) + 1, turnover=turnover)
        if instrument.symbol == "FEATURE" and closed_bars > 3:
            bad = candles[-2]
            candles[-2] = Candle(bad.timestamp, bad.open, bad.open - 1, bad.low, bad.close, bad.volume, bad.turnover, bad.turnover_currency, bad.is_closed)
        return candles


def test_partial_failures_still_publish_ready_datasets_and_statuses(tmp_path: Path) -> None:
    config = AppConfig(general=GeneralConfig(history_closed_bars=20, compact_closed_bars=5, liquidity_lookback_closed_bars=3, output_dir="output"))
    report = run_pipeline(
        config,
        tmp_path,
        selected_source="all",
        limit=None,
        refresh_cache=False,
        no_cache=True,
        as_of=datetime(2026, 2, 1, 12, tzinfo=UTC),
        sources=[FakeSource()],
    )
    assert report["counts"]["READY"] == 1
    assert report["counts"]["below_threshold"] == 1
    assert report["counts"]["download_failed"] == 1
    assert report["counts"]["feature_failed"] == 1
    current = tmp_path / "output/current"
    with (current / "catalog.csv").open(encoding="utf-8", newline="") as handle:
        catalog = list(csv.DictReader(handle))
    assert [row["symbol"] for row in catalog] == ["GOOD"]
    with (current / "universe_status.csv").open(encoding="utf-8", newline="") as handle:
        statuses = {row["symbol"]: row for row in csv.DictReader(handle)}
    assert statuses["DOWNLOAD"]["status"] == "DOWNLOAD_FAILED"
    assert statuses["FEATURE"]["status"] == "FEATURE_CALCULATION_FAILED"
    assert statuses["LOW"]["status"] == "BELOW_LIQUIDITY_THRESHOLD"
    full_path = current / catalog[0]["full_path"]
    compact_path = current / catalog[0]["compact_path"]
    full = pd.read_csv(full_path)
    compact = pd.read_csv(compact_path)
    assert len(full) == 21
    assert len(compact) == 6
    assert full.iloc[-1]["is_closed"] == False
    assert full.iloc[-1]["provisional"] == True
    assert set(FEATURE_IDS).issubset(full.columns)
    report_on_disk = json.loads((current / "run_report.json").read_text(encoding="utf-8"))
    assert report_on_disk["counts"]["READY"] == 1


class AllBelowSource(FakeSource):
    def discover(self) -> list[Instrument]:
        return [Instrument("fake", "perpetual", "LOW", "LOW", "Perpetual", "default", "USD")]

    def fetch_daily_candles(self, instrument: Instrument, closed_bars: int, as_of: datetime) -> list[Candle]:
        return _candles(min(closed_bars, 5) + 1, turnover=1.0)


class RateLimitedFakeSource:
    name = "hyperliquid"
    max_workers = 1
    threshold = 100.0

    def discover(self) -> list[Instrument]:
        return [
            Instrument(
                self.name,
                "perpetual",
                "GOOD",
                "GOOD",
                "Perpetual",
                "default",
                "USDC",
            )
        ]

    def fetch_daily_candles(
        self, _instrument: Instrument, closed_bars: int, _as_of: datetime
    ) -> list[Candle]:
        return _candles(min(closed_bars, 5) + 1, turnover=1000.0)

    @staticmethod
    def rate_limit_metrics() -> dict[str, float | int]:
        return {
            "rate_limit_wait_seconds": 12.5,
            "http_429_count": 1,
            "requests_sent": 7,
            "estimated_api_weight": 143,
            "waiting_threads": 0,
        }


def test_rate_limit_metrics_are_printed_and_saved_in_run_report(
    tmp_path: Path, capsys
) -> None:
    config = AppConfig(
        general=GeneralConfig(
            history_closed_bars=5,
            compact_closed_bars=2,
            liquidity_lookback_closed_bars=3,
            output_dir="output",
        )
    )
    report = run_pipeline(
        config,
        tmp_path,
        selected_source="hyperliquid",
        limit=None,
        refresh_cache=False,
        no_cache=True,
        as_of=datetime(2026, 2, 1, 12, tzinfo=UTC),
        sources=[RateLimitedFakeSource()],
    )
    metrics = report["sources"]["hyperliquid"]
    assert metrics["rate_limit_wait_seconds"] == 12.5
    assert metrics["http_429_count"] == 1
    assert metrics["requests_sent"] == 7
    assert metrics["estimated_api_weight"] == 143
    assert "estimated_api_weight=143" in capsys.readouterr().out
    on_disk = json.loads(
        (tmp_path / "output/current/run_report.json").read_text(encoding="utf-8")
    )
    assert on_disk["sources"]["hyperliquid"]["http_429_count"] == 1


def test_empty_success_set_keeps_diagnostics_in_building_and_does_not_publish(tmp_path: Path) -> None:
    config = AppConfig(general=GeneralConfig(history_closed_bars=5, compact_closed_bars=2, liquidity_lookback_closed_bars=3, output_dir="output"))
    with pytest.raises(RuntimeError, match="diagnostic snapshot"):
        run_pipeline(
            config,
            tmp_path,
            selected_source="all",
            limit=None,
            refresh_cache=False,
            no_cache=True,
            as_of=datetime(2026, 2, 1, 12, tzinfo=UTC),
            sources=[AllBelowSource()],
        )
    building = tmp_path / "output/_building"
    assert (building / "run_report.json").exists()
    assert (building / "universe_status.csv").exists()
    assert not (tmp_path / "output/current").exists()
