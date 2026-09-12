from __future__ import annotations

import csv
import json
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

from .features import FEATURE_IDS, STRUCTURAL_FEATURE_IDS
from .models import Instrument, ReadyDataset, UniverseRecord
from .utils import safe_path_component


BASE_COLUMNS = (
    "source",
    "market",
    "symbol",
    "instrument_name",
    "contract_type",
    "dex",
    "quote_currency",
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "turnover",
    "turnover_currency",
    "is_closed",
    "provisional",
)
CSV_COLUMNS = (*BASE_COLUMNS, *FEATURE_IDS)

# Append-only schema extension: all legacy catalog columns keep their original
# names and positions. These fields are advisory mapping aids only; catalog
# status=READY remains the sole builder eligibility flag and APX canonical
# economic mapping remains external to this project.
MAPPING_METADATA_COLUMNS = (
    "source_underlying_symbol",
    "underlying_symbol",
    "contract_expiry",
    "underlying_liquidity_rank",
)

CATALOG_COLUMNS = (
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
    *MAPPING_METADATA_COLUMNS,
)

LATEST_SNAPSHOT_BASE_COLUMNS = (
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
# Preserve all 53 legacy latest_snapshot positions; append metadata after the
# existing 29 feature columns for positional backward compatibility.
LATEST_SNAPSHOT_COLUMNS = (*LATEST_SNAPSHOT_BASE_COLUMNS, *FEATURE_IDS, *MAPPING_METADATA_COLUMNS)


class SnapshotWriter:
    def __init__(self, root: Path, output_dir: str) -> None:
        self.output_root = (root / output_dir).resolve()
        self.building = self.output_root / "_building"
        self._screening_values: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    def begin(self) -> Path:
        if self.building.exists():
            shutil.rmtree(self.building)
        self._screening_values.clear()
        (self.building / "full").mkdir(parents=True)
        (self.building / "compact").mkdir(parents=True)
        return self.building

    def write_dataset(
        self,
        instrument: Instrument,
        frame: pd.DataFrame,
        compact_closed_bars: int,
    ) -> tuple[str, str]:
        screening_values = latest_screening_values(frame)
        relative = dataset_relative_path(instrument)
        full_relative = Path("full") / relative
        compact_relative = Path("compact") / relative
        self._write_csv(self.building / full_relative, frame)
        compact = slice_closed_and_current(frame, compact_closed_bars)
        self._write_csv(self.building / compact_relative, compact)
        self._screening_values[instrument.key] = screening_values
        return full_relative.as_posix(), compact_relative.as_posix()

    def finalize_metadata(
        self,
        *,
        snapshot_id: str,
        generated_at: str,
        ready: list[ReadyDataset],
        universe: list[UniverseRecord],
        report: dict[str, Any],
    ) -> None:
        catalog_rows: list[dict[str, Any]] = []
        latest_snapshot_rows: list[dict[str, Any]] = []
        liquidity_ranks = _underlying_liquidity_ranks(ready)
        for item in ready:
            instrument = item.instrument
            mapping_metadata = _instrument_mapping_metadata(
                instrument,
                liquidity_ranks.get(instrument.key),
            )
            catalog_rows.append(
                {
                    "snapshot_id": snapshot_id,
                    "generated_at": generated_at,
                    "source": instrument.source,
                    "market": instrument.market,
                    "symbol": instrument.symbol,
                    "instrument_name": instrument.instrument_name,
                    "contract_type": instrument.contract_type,
                    "dex": instrument.dex,
                    "quote_currency": instrument.quote_currency,
                    "avg_daily_turnover": item.average_turnover,
                    "turnover_currency": item.turnover_currency,
                    "closed_bars_available": item.closed_bars_available,
                    "first_timestamp": item.first_timestamp,
                    "last_timestamp": item.last_timestamp,
                    "current_candle_present": _bool_text(item.current_candle_present),
                    "full_path": item.full_path,
                    "compact_path": item.compact_path,
                    "status": "READY",
                    **mapping_metadata,
                }
            )
            screening_values = self._screening_values.get(instrument.key)
            if screening_values is None:
                raise ValueError(f"Missing successfully exported frame for {instrument.key}")
            if screening_values["closed_bars_available"] != item.closed_bars_available:
                raise ValueError(f"Closed-bar count changed after export for {instrument.key}")
            latest_snapshot_rows.append(
                {
                    "snapshot_id": snapshot_id,
                    "generated_at": generated_at,
                    "source": instrument.source,
                    "market": instrument.market,
                    "symbol": instrument.symbol,
                    "instrument_name": instrument.instrument_name,
                    "contract_type": instrument.contract_type,
                    "dex": instrument.dex,
                    "quote_currency": instrument.quote_currency,
                    "avg_daily_turnover": item.average_turnover,
                    "turnover_currency": item.turnover_currency,
                    **screening_values,
                    "full_path": item.full_path,
                    "compact_path": item.compact_path,
                    **mapping_metadata,
                }
            )
        _write_dict_csv(self.building / "catalog.csv", catalog_rows, CATALOG_COLUMNS)
        _write_latest_snapshot_csv(self.building / "latest_snapshot.csv", latest_snapshot_rows)
        _write_dict_csv(
            self.building / "universe_status.csv",
            [item.as_dict() for item in universe],
            tuple(UniverseRecord("", "", "", "", "", None, 0, "").as_dict()),
        )
        (self.building / "run_report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        (self.building / "README_APX_MARKETS.md").write_text(
            snapshot_readme(snapshot_id, generated_at), encoding="utf-8"
        )

    def publish(self) -> Path:
        current = self.output_root / "current"
        previous = self.output_root / "previous"
        if previous.exists():
            shutil.rmtree(previous)
        if current.exists():
            current.replace(previous)
        self.building.replace(current)
        return current

    @staticmethod
    def _write_csv(path: Path, frame: pd.DataFrame) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        output = frame.loc[:, CSV_COLUMNS].copy()
        output["timestamp"] = output["timestamp"].map(lambda value: value.isoformat())
        output["is_closed"] = output["is_closed"].map(_bool_text)
        output["provisional"] = output["provisional"].map(_bool_text)
        for feature_id in STRUCTURAL_FEATURE_IDS:
            output[feature_id] = output[feature_id].map(_structural_json)
        output.to_csv(
            path,
            index=False,
            encoding="utf-8",
            lineterminator="\n",
            na_rep="",
            float_format="%.15g",
            quoting=csv.QUOTE_MINIMAL,
        )


def dataset_relative_path(instrument: Instrument) -> Path:
    if instrument.source == "hyperliquid":
        directory = Path("hyperliquid") / safe_path_component(instrument.dex or "default")
    else:
        directory = Path(instrument.source) / safe_path_component(instrument.market)
    return directory / f"{safe_path_component(instrument.symbol)}.csv"


def slice_closed_and_current(frame: pd.DataFrame, closed_bars: int) -> pd.DataFrame:
    closed = frame.loc[frame["is_closed"]].tail(closed_bars)
    unfinished = frame.loc[~frame["is_closed"]].tail(1)
    return pd.concat([closed, unfinished]).sort_values("timestamp").reset_index(drop=True)


def latest_screening_values(frame: pd.DataFrame) -> dict[str, Any]:
    """Extract the source-neutral screening state from a calculated candle frame."""
    closed = frame.loc[frame["is_closed"]].sort_values("timestamp").reset_index(drop=True)
    if closed.empty:
        raise ValueError("A READY dataset must contain at least one closed candle")
    last_closed = closed.iloc[-1]
    current = frame.loc[~frame["is_closed"]].sort_values("timestamp").tail(1)
    current_present = not current.empty
    values: dict[str, Any] = {
        "closed_bars_available": len(closed),
        "last_closed_timestamp": last_closed["timestamp"],
        "last_closed_close": last_closed["close"],
        "current_timestamp": current.iloc[0]["timestamp"] if current_present else None,
        "current_close": current.iloc[0]["close"] if current_present else None,
        "current_candle_present": _bool_text(current_present),
        "return_1d": _closed_bar_return(closed, 1),
        "return_5d": _closed_bar_return(closed, 5),
        "return_20d": _closed_bar_return(closed, 20),
        "has_100_closed_bars": _bool_text(len(closed) >= 100),
        "has_1000_closed_bars": _bool_text(len(closed) >= 1000),
    }
    values.update({feature_id: last_closed[feature_id] for feature_id in FEATURE_IDS})
    return values


def _instrument_mapping_metadata(instrument: Instrument, liquidity_rank: int | None) -> dict[str, Any]:
    metadata = instrument.metadata or {}
    source_underlying = str(
        metadata.get("source_underlying_symbol")
        or metadata.get("base_coin")
        or (
            instrument.instrument_name
            if instrument.source == "hyperliquid" and instrument.contract_type == "Perpetual"
            else ""
        )
    ).strip()
    underlying = str(metadata.get("underlying_symbol") or source_underlying).strip()
    return {
        "source_underlying_symbol": source_underlying,
        "underlying_symbol": underlying,
        "contract_expiry": str(metadata.get("contract_expiry") or "").strip(),
        "underlying_liquidity_rank": liquidity_rank,
    }


def _underlying_liquidity_ranks(ready: list[ReadyDataset]) -> dict[tuple[str, str, str, str], int]:
    groups: dict[tuple[str, str, str], list[ReadyDataset]] = defaultdict(list)
    for item in ready:
        metadata = _instrument_mapping_metadata(item.instrument, None)
        underlying = metadata["underlying_symbol"]
        if underlying:
            groups[(item.instrument.source, item.instrument.market, underlying)].append(item)

    result: dict[tuple[str, str, str, str], int] = {}
    for members in groups.values():
        ordered = sorted(
            members,
            key=lambda item: (-float(item.average_turnover), item.instrument.symbol),
        )
        for rank, item in enumerate(ordered, start=1):
            result[item.instrument.key] = rank
    return result


def _closed_bar_return(closed: pd.DataFrame, days: int) -> float | None:
    if len(closed) <= days:
        return None
    return float(closed.iloc[-1]["close"]) / float(closed.iloc[-days - 1]["close"]) - 1.0


def _structural_json(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _bool_text(value: bool) -> str:
    return "true" if bool(value) else "false"


def _write_dict_csv(path: Path, rows: list[dict[str, Any]], columns: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _write_latest_snapshot_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    output = pd.DataFrame(rows, columns=LATEST_SNAPSHOT_COLUMNS)
    for column in ("last_closed_timestamp", "current_timestamp"):
        output[column] = output[column].map(_timestamp_text)
    for feature_id in STRUCTURAL_FEATURE_IDS:
        output[feature_id] = output[feature_id].map(_structural_json)
    output.to_csv(
        path,
        index=False,
        encoding="utf-8",
        lineterminator="\n",
        na_rep="",
        float_format="%.15g",
        quoting=csv.QUOTE_MINIMAL,
    )


def _timestamp_text(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    return value.isoformat()


def snapshot_readme(snapshot_id: str, generated_at: str) -> str:
    features = "\n".join(f"- `{feature_id}`" for feature_id in FEATURE_IDS)
    return f"""# APX Markets daily dataset snapshot

Snapshot `{snapshot_id}` was generated at `{generated_at}`. `full/` contains up to the configured long history plus the current unfinished candle. `compact/` contains the configured shorter tail plus the same current candle. Paths are `source/market/symbol.csv`; Hyperliquid uses `hyperliquid/dex/symbol.csv`.

Each CSV is one 1D time series and each row is one candle. Identity columns are followed by timestamp, OHLC, raw source volume, quote/notional turnover with its actual currency, candle state, and 29 feature columns. The delimiter is comma, decimal separator is a dot, and encoding is UTF-8.

`is_closed=true` and `provisional=false` identify final candles. The current unfinished candle has `is_closed=false` and `provisional=true`; its OHLCV, turnover, and features reflect information available when the snapshot was built and may change. All feature algorithms are left-looking. Empty feature cells mean insufficient history. Structural feature cells are deterministic compact JSON lists such as `[[-12,101.5],[-48,98.2]]`.

Turnover is not interchangeable with volume. MOEX shares and bonds use candle `value` in RUB when available and the Portfolio Builder fallback otherwise. MOEX FORTS keeps candle OHLCV but joins closed-day RUB turnover from historical trading-results `VALUE`; today's provisional candle uses marketdata `VALTODAY` when available and never synthesizes turnover from `close*volume*lot_size`. Bybit uses V5 quote turnover and keeps the actual quote coin (including USDT or USDC). Hyperliquid uses candle quote/notional value when supplied and documented `close*volume` fallback, reported as USDC/USD-equivalent.

`catalog.csv` is the canonical inventory of successfully generated current series (`status=READY`) and their relative paths. `latest_snapshot.csv` is a compact derived screening view with exactly one row per READY series. Its 29 features and 1/5/20-day returns use only the last fully closed candle; `current_timestamp` and `current_close`, when present, describe the separate provisional candle. Empty feature or return cells mean insufficient history. `has_100_closed_bars` and `has_1000_closed_bars` expose the available history depth.

Both `catalog.csv` and `latest_snapshot.csv` append four mapping-assistance fields: `source_underlying_symbol`, `underlying_symbol`, `contract_expiry`, and `underlying_liquidity_rank`. They are advisory metadata only. `source_underlying_symbol` preserves the venue-native underlying identifier where available; `underlying_symbol` is a normalized hint (for example MOEX `GAZR` -> `GAZP`, `SBRF` -> `SBER`, `MIX` -> `IMOEX`); `contract_expiry` is an ISO date for dated contracts when the venue supplies it; `underlying_liquidity_rank` ranks READY series by `avg_daily_turnover` within the same source/market/underlying group, with 1 being most liquid. These fields never change READY eligibility and must not silently override a downstream canonical mapping registry.

The schema extension is append-only. All legacy `catalog.csv` columns retain their original names and positions. All legacy 53 `latest_snapshot.csv` columns retain their original names and positions; the four new fields are appended after them. `run_report.json` semantics and counts are unchanged.

## Recommended downstream workflow

1. Read `README_APX_MARKETS.md`.
2. Read `catalog.csv` and `latest_snapshot.csv`.
3. Screen the current universe in `latest_snapshot.csv`.
4. Load `compact/` CSV only for selected candidates.
5. Load `full/` CSV only when long history is necessary.
6. Use `universe_status.csv` only for diagnostics.

Do not load every instrument CSV at once for initial screening.

`universe_status.csv` lists every instrument considered, including liquidity rejections and isolated technical failures. Absence from `catalog.csv` therefore does not imply delisting: inspect `universe_status.csv` and `run_report.json`.

The universe is rediscovered from enabled MOEX categories, Bybit V5 metadata contract types, and the default plus discovered Hyperliquid perp DEXes on every run. Snapshot freshness is given by `generated_at` in `catalog.csv` and `started_at`/`completed_at` in `run_report.json`.

## Feature columns

{features}
"""
