from __future__ import annotations

import csv
import json
import shutil
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
)


class SnapshotWriter:
    def __init__(self, root: Path, output_dir: str) -> None:
        self.output_root = (root / output_dir).resolve()
        self.building = self.output_root / "_building"

    def begin(self) -> Path:
        if self.building.exists():
            shutil.rmtree(self.building)
        (self.building / "full").mkdir(parents=True)
        (self.building / "compact").mkdir(parents=True)
        return self.building

    def write_dataset(
        self,
        instrument: Instrument,
        frame: pd.DataFrame,
        compact_closed_bars: int,
    ) -> tuple[str, str]:
        relative = dataset_relative_path(instrument)
        full_relative = Path("full") / relative
        compact_relative = Path("compact") / relative
        self._write_csv(self.building / full_relative, frame)
        compact = slice_closed_and_current(frame, compact_closed_bars)
        self._write_csv(self.building / compact_relative, compact)
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
        for item in ready:
            instrument = item.instrument
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
                }
            )
        _write_dict_csv(self.building / "catalog.csv", catalog_rows, CATALOG_COLUMNS)
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


def snapshot_readme(snapshot_id: str, generated_at: str) -> str:
    features = "\n".join(f"- `{feature_id}`" for feature_id in FEATURE_IDS)
    return f"""# APX Markets daily dataset snapshot

Snapshot `{snapshot_id}` was generated at `{generated_at}`. `full/` contains up to the configured long history plus the current unfinished candle. `compact/` contains the configured shorter tail plus the same current candle. Paths are `source/market/symbol.csv`; Hyperliquid uses `hyperliquid/dex/symbol.csv`.

Each CSV is one 1D time series and each row is one candle. Identity columns are followed by timestamp, OHLC, raw source volume, quote/notional turnover with its actual currency, candle state, and 29 feature columns. The delimiter is comma, decimal separator is a dot, and encoding is UTF-8.

`is_closed=true` and `provisional=false` identify final candles. The current unfinished candle has `is_closed=false` and `provisional=true`; its OHLCV, turnover, and features reflect information available when the snapshot was built and may change. All feature algorithms are left-looking. Empty feature cells mean insufficient history. Structural feature cells are deterministic compact JSON lists such as `[[-12,101.5],[-48,98.2]]`.

Turnover is not interchangeable with volume. MOEX uses ISS `value` in RUB when available and the Portfolio Builder fallback otherwise. Bybit uses V5 quote turnover and keeps the actual quote coin (including USDT or USDC). Hyperliquid uses candle quote/notional value when supplied and documented `close*volume` fallback, reported as USDC/USD-equivalent.

`catalog.csv` lists only successfully generated current series (`status=READY`) and their relative paths. `universe_status.csv` lists every instrument considered, including liquidity rejections and isolated technical failures. Absence from `catalog.csv` therefore does not imply delisting: inspect `universe_status.csv` and `run_report.json`.

The universe is rediscovered from enabled MOEX categories, Bybit V5 metadata contract types, and the default plus discovered Hyperliquid perp DEXes on every run. Snapshot freshness is given by `generated_at` in `catalog.csv` and `started_at`/`completed_at` in `run_report.json`.

## Feature columns

{features}
"""
