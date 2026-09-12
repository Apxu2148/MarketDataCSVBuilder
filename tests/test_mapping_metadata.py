from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from market_data_csv_builder.config import AppConfig, BybitConfig, MoexConfig
from market_data_csv_builder.export import (
    CATALOG_COLUMNS,
    LATEST_SNAPSHOT_COLUMNS,
    MAPPING_METADATA_COLUMNS,
    SnapshotWriter,
)
from market_data_csv_builder.features import FEATURE_IDS, STRUCTURAL_FEATURE_IDS
from market_data_csv_builder.models import Instrument, ReadyDataset
from market_data_csv_builder.sources.bybit import BybitSource, _delivery_date
from market_data_csv_builder.sources.moex import MoexSource, _iso_date_text, _normalized_underlying


LEGACY_CATALOG_COLUMNS = (
    "snapshot_id",
    "generated_at",
    "source",
    "market",
    "symbol",
    "instrument_name",
    "contract_type",
    "dex",
    "quote_currency",
    "avg_daily_turnover",
    "turnover_currency",
    "closed_bars_available",
    "first_timestamp",
    "last_timestamp",
    "current_candle_present",
    "full_path",
    "compact_path",
    "status",
)

LEGACY_LATEST_BASE_COLUMNS = (
    "snapshot_id",
    "generated_at",
    "source",
    "market",
    "symbol",
    "instrument_name",
    "contract_type",
    "dex",
    "quote_currency",
    "avg_daily_turnover",
    "turnover_currency",
    "closed_bars_available",
    "last_closed_timestamp",
    "last_closed_close",
    "current_timestamp",
    "current_close",
    "current_candle_present",
    "return_1d",
    "return_5d",
    "return_20d",
    "has_100_closed_bars",
    "has_1000_closed_bars",
    "full_path",
    "compact_path",
)
LEGACY_LATEST_COLUMNS = (*LEGACY_LATEST_BASE_COLUMNS, *FEATURE_IDS)


class FakeHttp:
    def __init__(self, payloads: dict[str, Any]) -> None:
        self.payloads = payloads
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_json(self, url: str, params: dict[str, Any]) -> Any:
        self.calls.append((url, dict(params)))
        for suffix, payload in self.payloads.items():
            if url.endswith(suffix):
                return payload(params) if callable(payload) else payload
        raise AssertionError(url)


def _frame(instrument: Instrument) -> pd.DataFrame:
    rows = []
    for index, timestamp in enumerate(pd.date_range("2026-09-09", periods=4, freq="1D", tz="UTC")):
        close = 100.0 + index
        row = {
            "source": instrument.source,
            "market": instrument.market,
            "symbol": instrument.symbol,
            "instrument_name": instrument.instrument_name,
            "contract_type": instrument.contract_type,
            "dex": instrument.dex,
            "quote_currency": instrument.quote_currency,
            "timestamp": timestamp,
            "open": close - 1,
            "high": close + 1,
            "low": close - 2,
            "close": close,
            "volume": 10.0,
            "turnover": 1000.0,
            "turnover_currency": instrument.quote_currency,
            "is_closed": index < 3,
            "provisional": index == 3,
        }
        for feature_id in FEATURE_IDS:
            row[feature_id] = ((-index, close),) if feature_id in STRUCTURAL_FEATURE_IDS else float(index)
        rows.append(row)
    return pd.DataFrame(rows)


def _ready(writer: SnapshotWriter, instrument: Instrument, turnover: float) -> ReadyDataset:
    frame = _frame(instrument)
    full_path, compact_path = writer.write_dataset(instrument, frame, 2)
    return ReadyDataset(
        instrument=instrument,
        average_turnover=turnover,
        turnover_currency=instrument.quote_currency,
        closed_bars_available=3,
        first_timestamp=frame.iloc[0]["timestamp"].isoformat(),
        last_timestamp=frame.iloc[-1]["timestamp"].isoformat(),
        current_candle_present=True,
        full_path=full_path,
        compact_path=compact_path,
    )


def test_schema_extension_is_append_only() -> None:
    assert CATALOG_COLUMNS[: len(LEGACY_CATALOG_COLUMNS)] == LEGACY_CATALOG_COLUMNS
    assert CATALOG_COLUMNS[len(LEGACY_CATALOG_COLUMNS) :] == MAPPING_METADATA_COLUMNS
    assert LATEST_SNAPSHOT_COLUMNS[: len(LEGACY_LATEST_COLUMNS)] == LEGACY_LATEST_COLUMNS
    assert LATEST_SNAPSHOT_COLUMNS[len(LEGACY_LATEST_COLUMNS) :] == MAPPING_METADATA_COLUMNS


def test_moex_known_underlying_normalization_and_expiry() -> None:
    assert _normalized_underlying("GAZR", "GZU6", "futures") == "GAZP"
    assert _normalized_underlying("SBRF", "SRU6", "futures") == "SBER"
    assert _normalized_underlying("MIX", "MXU6", "futures") == "IMOEX"
    assert _normalized_underlying("MIX", "MXZ6", "futures") == "IMOEX"
    assert _normalized_underlying("", "GAZP", "shares") == "GAZP"
    assert _iso_date_text("2026-09-17") == "2026-09-17"
    assert _iso_date_text(None) == ""


def test_moex_discovery_exports_source_and_normalized_underlying(tmp_path: Path) -> None:
    source = MoexSource(
        AppConfig(moex=MoexConfig(shares_enabled=False, bonds_enabled=False, funds_enabled=False)),
        tmp_path,
        no_cache=True,
    )
    columns = [
        "SECID", "SHORTNAME", "SECNAME", "LATNAME", "LOTSIZE", "BOARDID",
        "PRIMARY_BOARDID", "SECTYPE", "SECTYPE_NAME", "TYPE", "TYPE_NAME",
        "GROUP", "STATUS", "ASSETCODE", "LASTTRADEDATE",
    ]
    source.http = FakeHttp(
        {
            "/futures/markets/forts/securities.json": {
                "securities": {
                    "columns": columns,
                    "data": [
                        ["GZU6", "GAZR-9.26", "", "", 1, "RFUD", "RFUD", "", "", "", "Future", "", "A", "GAZR", "2026-09-17"],
                        ["SRU6", "SBRF-9.26", "", "", 1, "RFUD", "RFUD", "", "", "", "Future", "", "A", "SBRF", "2026-09-17"],
                        ["MXU6", "MIX-9.26", "", "", 1, "RFUD", "RFUD", "", "", "", "Future", "", "A", "MIX", "2026-09-17"],
                        ["MXZ6", "MIX-12.26", "", "", 1, "RFUD", "RFUD", "", "", "", "Future", "", "A", "MIX", "2026-12-17"],
                    ],
                }
            }
        }
    )
    found = {item.symbol: item for item in source.discover()}
    assert found["GZU6"].metadata["source_underlying_symbol"] == "GAZR"
    assert found["GZU6"].metadata["underlying_symbol"] == "GAZP"
    assert found["SRU6"].metadata["underlying_symbol"] == "SBER"
    assert found["MXU6"].metadata["underlying_symbol"] == "IMOEX"
    assert found["MXZ6"].metadata["underlying_symbol"] == "IMOEX"
    assert found["MXU6"].metadata["contract_expiry"] == "2026-09-17"
    assert found["MXZ6"].metadata["contract_expiry"] == "2026-12-17"
    requested_columns = source.http.calls[0][1]["securities.columns"]
    assert "ASSETCODE" in requested_columns
    assert "LASTTRADEDATE" in requested_columns


def test_bybit_delivery_time_is_iso_date_and_discovery_keeps_underlying(tmp_path: Path) -> None:
    delivery = int(datetime(2026, 12, 25, 8, tzinfo=UTC).timestamp() * 1000)
    assert _delivery_date(delivery) == "2026-12-25"
    assert _delivery_date(None) == ""
    assert _delivery_date(0) == ""

    source = BybitSource(
        AppConfig(bybit=BybitConfig(linear_perpetual_enabled=True, linear_futures_enabled=True)),
        tmp_path,
        no_cache=True,
    )
    source.http = FakeHttp(
        {
            "/v5/market/instruments-info": {
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "symbol": "BTCUSDT",
                            "status": "Trading",
                            "contractType": "LinearPerpetual",
                            "baseCoin": "BTC",
                            "quoteCoin": "USDT",
                            "settleCoin": "USDT",
                        },
                        {
                            "symbol": "BTC-25DEC26",
                            "status": "Trading",
                            "contractType": "LinearFutures",
                            "baseCoin": "BTC",
                            "quoteCoin": "USDT",
                            "settleCoin": "USDT",
                            "deliveryTime": str(delivery),
                        },
                    ],
                    "nextPageCursor": "",
                },
            }
        }
    )
    found = {item.symbol: item for item in source.discover()}
    assert found["BTCUSDT"].metadata["underlying_symbol"] == "BTC"
    assert found["BTCUSDT"].metadata["contract_expiry"] == ""
    assert found["BTC-25DEC26"].metadata["underlying_symbol"] == "BTC"
    assert found["BTC-25DEC26"].metadata["contract_expiry"] == "2026-12-25"


def test_mapping_metadata_and_roll_liquidity_rank_are_reproducible(tmp_path: Path) -> None:
    writer = SnapshotWriter(tmp_path, "output")
    writer.begin()

    instruments = [
        Instrument(
            "moex", "shares", "GAZP", "Gazprom", "Share", quote_currency="RUB",
            metadata={"source_underlying_symbol": "GAZP", "underlying_symbol": "GAZP", "contract_expiry": ""},
        ),
        Instrument(
            "moex", "futures", "GZU6", "GAZR-9.26", "TermFuture", quote_currency="RUB",
            metadata={"source_underlying_symbol": "GAZR", "underlying_symbol": "GAZP", "contract_expiry": "2026-09-17"},
        ),
        Instrument(
            "moex", "shares", "SBER", "Sberbank", "Share", quote_currency="RUB",
            metadata={"source_underlying_symbol": "SBER", "underlying_symbol": "SBER", "contract_expiry": ""},
        ),
        Instrument(
            "moex", "futures", "SRU6", "SBRF-9.26", "TermFuture", quote_currency="RUB",
            metadata={"source_underlying_symbol": "SBRF", "underlying_symbol": "SBER", "contract_expiry": "2026-09-17"},
        ),
        Instrument(
            "moex", "futures", "MXU6", "MIX-9.26", "TermFuture", quote_currency="RUB",
            metadata={"source_underlying_symbol": "MIX", "underlying_symbol": "IMOEX", "contract_expiry": "2026-09-17"},
        ),
        Instrument(
            "moex", "futures", "MXZ6", "MIX-12.26", "TermFuture", quote_currency="RUB",
            metadata={"source_underlying_symbol": "MIX", "underlying_symbol": "IMOEX", "contract_expiry": "2026-12-17"},
        ),
    ]
    turnover = {
        "GAZP": 5_800_000_000,
        "GZU6": 3_200_000_000,
        "SBER": 6_200_000_000,
        "SRU6": 3_700_000_000,
        "MXU6": 97_500_000_000,
        "MXZ6": 3_100_000_000,
    }
    ready = [_ready(writer, instrument, turnover[instrument.symbol]) for instrument in instruments]
    report = {"counts": {"READY": len(ready), "latest_snapshot_rows": len(ready)}}
    writer.finalize_metadata(
        snapshot_id="20260912T230000Z",
        generated_at="2026-09-12T23:00:00+00:00",
        ready=ready,
        universe=[],
        report=report,
    )
    writer.publish()

    current = tmp_path / "output/current"
    with (current / "catalog.csv").open(encoding="utf-8", newline="") as handle:
        catalog = {row["symbol"]: row for row in csv.DictReader(handle)}
    with (current / "latest_snapshot.csv").open(encoding="utf-8", newline="") as handle:
        latest = {row["symbol"]: row for row in csv.DictReader(handle)}

    assert catalog["GZU6"]["source_underlying_symbol"] == "GAZR"
    assert catalog["GZU6"]["underlying_symbol"] == "GAZP"
    assert catalog["SRU6"]["source_underlying_symbol"] == "SBRF"
    assert catalog["SRU6"]["underlying_symbol"] == "SBER"
    assert catalog["MXU6"]["source_underlying_symbol"] == "MIX"
    assert catalog["MXU6"]["underlying_symbol"] == "IMOEX"
    assert catalog["MXZ6"]["underlying_symbol"] == "IMOEX"
    assert catalog["MXU6"]["underlying_liquidity_rank"] == "1"
    assert catalog["MXZ6"]["underlying_liquidity_rank"] == "2"
    assert catalog["MXU6"]["contract_expiry"] == "2026-09-17"
    assert catalog["MXZ6"]["contract_expiry"] == "2026-12-17"

    for symbol in catalog:
        assert {key: catalog[symbol][key] for key in MAPPING_METADATA_COLUMNS} == {
            key: latest[symbol][key] for key in MAPPING_METADATA_COLUMNS
        }

    assert {row["status"] for row in catalog.values()} == {"READY"}
    on_disk_report = json.loads((current / "run_report.json").read_text(encoding="utf-8"))
    assert on_disk_report["counts"]["READY"] == len(ready)
