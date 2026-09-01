from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from market_data_csv_builder.config import AppConfig, GeneralConfig
from market_data_csv_builder.export import (
    LATEST_SNAPSHOT_COLUMNS,
    SnapshotWriter,
    dataset_relative_path,
    latest_screening_values,
    slice_closed_and_current,
)
from market_data_csv_builder.features import FEATURE_IDS, STRUCTURAL_FEATURE_IDS
from market_data_csv_builder.liquidity import calculate_average_turnover
from market_data_csv_builder.models import Candle, Instrument, ReadyDataset
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


def _calculated_export_frame(
    instrument: Instrument, closed_count: int, *, current: bool = True
) -> pd.DataFrame:
    count = closed_count + int(current)
    timestamps = pd.date_range("2026-01-01", periods=count, freq="1D", tz="UTC")
    rows = []
    for index, timestamp in enumerate(timestamps):
        close = float(index + 1)
        row = {
            "source": instrument.source,
            "market": instrument.market,
            "symbol": instrument.symbol,
            "instrument_name": instrument.instrument_name,
            "contract_type": instrument.contract_type,
            "dex": instrument.dex,
            "quote_currency": instrument.quote_currency,
            "timestamp": timestamp,
            "open": close - 0.25,
            "high": close + 1.0,
            "low": max(0.01, close - 1.0),
            "close": close,
            "volume": 10.0,
            "turnover": 1000.0,
            "turnover_currency": instrument.quote_currency,
            "is_closed": index < closed_count,
            "provisional": index >= closed_count,
        }
        for feature_id in FEATURE_IDS:
            row[feature_id] = (
                ((-index, close),) if feature_id in STRUCTURAL_FEATURE_IDS else float(index)
            )
        rows.append(row)
    return pd.DataFrame(rows)


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
    with (current / "latest_snapshot.csv").open(encoding="utf-8", newline="") as handle:
        latest_reader = csv.DictReader(handle)
        assert tuple(latest_reader.fieldnames or ()) == LATEST_SNAPSHOT_COLUMNS
        latest = list(latest_reader)
    assert len(latest) == len(catalog) == report["counts"]["latest_snapshot_rows"] == 1
    assert latest[0]["symbol"] == catalog[0]["symbol"]
    assert latest[0]["full_path"] == catalog[0]["full_path"]
    assert latest[0]["compact_path"] == catalog[0]["compact_path"]
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
    with full_path.open(encoding="utf-8", newline="") as handle:
        full_rows = list(csv.DictReader(handle))
    last_closed = [row for row in full_rows if row["is_closed"] == "true"][-1]
    current_row = [row for row in full_rows if row["is_closed"] == "false"][-1]
    assert latest[0]["last_closed_close"] == last_closed["close"] == "119"
    assert latest[0]["current_close"] == current_row["close"] == "120"
    assert latest[0]["current_candle_present"] == "true"
    assert float(latest[0]["return_1d"]) == pytest.approx(119 / 118 - 1)
    assert float(latest[0]["return_5d"]) == pytest.approx(119 / 114 - 1)
    assert latest[0]["return_20d"] == ""
    assert latest[0]["has_100_closed_bars"] == "false"
    assert latest[0]["has_1000_closed_bars"] == "false"
    assert {feature_id: latest[0][feature_id] for feature_id in FEATURE_IDS} == {
        feature_id: last_closed[feature_id] for feature_id in FEATURE_IDS
    }
    report_on_disk = json.loads((current / "run_report.json").read_text(encoding="utf-8"))
    assert report_on_disk["counts"]["READY"] == 1
    assert report_on_disk["counts"]["latest_snapshot_rows"] == 1
    assert report_on_disk["counts"]["total_csv_files_created"] == 5


def test_latest_snapshot_is_source_neutral_and_matches_ready_catalog(tmp_path: Path) -> None:
    instruments = [
        Instrument("moex", "shares", "SBER", "Sberbank", "Share", quote_currency="RUB"),
        Instrument("moex", "futures", "SiZ6", "USD/RUB", "Future", quote_currency="RUB"),
        Instrument("bybit", "linear", "BTCUSDT", "BTCUSDT", "LinearPerpetual", quote_currency="USDT"),
        Instrument("hyperliquid", "perpetual", "BTC", "Bitcoin", "Perpetual", "default", "USDC"),
    ]
    writer = SnapshotWriter(tmp_path, "output")
    writer.begin()
    ready = []
    for instrument in instruments:
        current_present = instrument.symbol != "SBER"
        frame = _calculated_export_frame(instrument, 3, current=current_present)
        full_path, compact_path = writer.write_dataset(instrument, frame, 2)
        ready.append(
            ReadyDataset(
                instrument=instrument,
                average_turnover=1234.5,
                turnover_currency=instrument.quote_currency,
                closed_bars_available=3,
                first_timestamp=frame.iloc[0]["timestamp"].isoformat(),
                last_timestamp=frame.iloc[-1]["timestamp"].isoformat(),
                current_candle_present=current_present,
                full_path=full_path,
                compact_path=compact_path,
            )
        )
    writer.finalize_metadata(
        snapshot_id="20260901T000000Z",
        generated_at="2026-09-01T00:00:00+00:00",
        ready=ready,
        universe=[],
        report={"counts": {"latest_snapshot_rows": 4}},
    )
    writer.publish()
    current = tmp_path / "output/current"
    with (current / "catalog.csv").open(encoding="utf-8", newline="") as handle:
        catalog = list(csv.DictReader(handle))
    with (current / "latest_snapshot.csv").open(encoding="utf-8", newline="") as handle:
        latest = list(csv.DictReader(handle))
    assert len(latest) == len(catalog) == 4
    identity = ("source", "market", "symbol", "contract_type", "dex", "full_path", "compact_path")
    assert [{key: row[key] for key in identity} for row in latest] == [
        {key: row[key] for key in identity} for row in catalog
    ]
    assert {(row["source"], row["market"], row["symbol"]) for row in latest} == {
        ("moex", "shares", "SBER"),
        ("moex", "futures", "SiZ6"),
        ("bybit", "linear", "BTCUSDT"),
        ("hyperliquid", "perpetual", "BTC"),
    }
    assert all(row["last_closed_close"] == "3" for row in latest)
    sber = next(row for row in latest if row["symbol"] == "SBER")
    assert sber["current_timestamp"] == ""
    assert sber["current_close"] == ""
    assert sber["current_candle_present"] == "false"
    assert all(row["current_close"] == "4" for row in latest if row["symbol"] != "SBER")
    assert all(row[FEATURE_IDS[0]] == "2" for row in latest)
    assert all(row[STRUCTURAL_FEATURE_IDS[0]] == "[[-2,3.0]]" for row in latest)
    assert all(row["return_5d"] == "" for row in latest)
    for artifact in (
        "full",
        "compact",
        "catalog.csv",
        "latest_snapshot.csv",
        "universe_status.csv",
        "README_APX_MARKETS.md",
        "run_report.json",
    ):
        assert (current / artifact).exists()


def test_latest_snapshot_returns_use_closed_candles_and_current_can_be_absent() -> None:
    instrument = Instrument("test", "spot", "KNOWN", "Known", "Spot", quote_currency="USD")
    frame = _calculated_export_frame(instrument, 22)
    values = latest_screening_values(frame)
    assert values["last_closed_close"] == 22
    assert values["current_close"] == 23
    assert values["return_1d"] == pytest.approx(22 / 21 - 1)
    assert values["return_5d"] == pytest.approx(22 / 17 - 1)
    assert values["return_20d"] == pytest.approx(22 / 2 - 1)
    assert values[FEATURE_IDS[0]] == 21

    without_current = latest_screening_values(_calculated_export_frame(instrument, 3, current=False))
    assert without_current["current_timestamp"] is None
    assert without_current["current_close"] is None
    assert without_current["current_candle_present"] == "false"
    assert without_current["return_5d"] is None
    assert without_current["return_20d"] is None


@pytest.mark.parametrize(
    ("closed_count", "has_100", "has_1000"),
    [(99, "false", "false"), (100, "true", "false"), (999, "true", "false"), (1000, "true", "true")],
)
def test_latest_snapshot_history_flags_use_actual_closed_count(
    closed_count: int, has_100: str, has_1000: str
) -> None:
    instrument = Instrument("test", "spot", "DEPTH", "Depth", "Spot", quote_currency="USD")
    values = latest_screening_values(
        _calculated_export_frame(instrument, closed_count, current=False)
    )
    assert values["closed_bars_available"] == closed_count
    assert values["has_100_closed_bars"] == has_100
    assert values["has_1000_closed_bars"] == has_1000


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


class ShortHistorySource:
    name = "short"
    max_workers = 1
    threshold = 100.0

    def discover(self) -> list[Instrument]:
        return [Instrument("short", "spot", "NEW", "New listing", "Spot", quote_currency="USD")]

    def fetch_daily_candles(
        self, _instrument: Instrument, closed_bars: int, _as_of: datetime
    ) -> list[Candle]:
        return _candles(min(closed_bars, 3) + 1, turnover=1000.0)


def test_three_closed_bar_dataset_stays_ready_with_unavailable_values_empty(
    tmp_path: Path,
) -> None:
    config = AppConfig(
        general=GeneralConfig(
            history_closed_bars=3,
            compact_closed_bars=3,
            liquidity_lookback_closed_bars=3,
            output_dir="output",
        )
    )
    report = run_pipeline(
        config,
        tmp_path,
        selected_source="all",
        limit=None,
        refresh_cache=False,
        no_cache=True,
        as_of=datetime(2026, 2, 1, 12, tzinfo=UTC),
        sources=[ShortHistorySource()],
    )
    with (tmp_path / "output/current/latest_snapshot.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        row = next(csv.DictReader(handle))
    assert report["counts"]["READY"] == report["counts"]["latest_snapshot_rows"] == 1
    assert row["symbol"] == "NEW"
    assert row["closed_bars_available"] == "3"
    assert row["sma_10"] == ""
    assert row["levels_resistance_short"] == ""
    assert row["return_1d"] != ""
    assert row["return_5d"] == ""
    assert row["return_20d"] == ""


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
    with (building / "latest_snapshot.csv").open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        assert tuple(next(reader)) == LATEST_SNAPSHOT_COLUMNS
        assert list(reader) == []
    assert not (tmp_path / "output/current").exists()
