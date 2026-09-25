"""Validate a published snapshot and report required live acceptance samples."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from market_data_csv_builder.features import FEATURE_IDS
from market_data_csv_builder.intraday.export import validate_snapshot


def main():
    directory = ROOT / "output/intraday/current"
    validate_snapshot(directory)
    universe = pd.read_csv(directory / "universe.csv")
    catalog = pd.read_csv(directory / "catalog.csv")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    report = json.loads((directory / "run_report.json").read_text(encoding="utf-8"))
    samples = {}
    for symbol in ("BTCUSDT", "ETHUSDT", "SUIUSDT"):
        if symbol not in set(universe.symbol):
            samples[symbol] = {"eligible": False}
            continue
        inventory = catalog.loc[catalog.symbol == symbol]
        assert set(inventory.timeframe) == set(manifest["timeframes"])
        rows = []
        for entry in inventory.to_dict("records"):
            if entry["status"] == "FAILED":
                rows.append(entry)
                continue
            frame = pd.read_csv(directory / entry["relative_path"])
            assert set(FEATURE_IDS) <= set(frame.columns)
            closed = frame.loc[frame.is_closed]
            current = frame.loc[frame.provisional]
            rows.append(dict(timeframe=entry["timeframe"], status=entry["status"],
                closed_bars=len(closed), provisional_bars=len(current),
                last_closed_timestamp=closed.iloc[-1].timestamp if not closed.empty else None,
                provisional_timestamp=current.iloc[-1].timestamp if not current.empty else None,
                last_closed_atr=float(closed.iloc[-1].atr_wilder_14) if len(closed) >= 14 else None,
                final_refresh_at_utc=entry["final_refresh_at_utc"]))
        samples[symbol] = dict(eligible=True, series=rows)
    result = dict(snapshot_id=manifest["snapshot_id"], counts=report["counts"],
        elapsed_seconds=report["elapsed_seconds"], requests_sent=report["requests_sent"],
        retry_count=report["retry_count"], rate_limit_events=report["rate_limit_events"],
        snapshot_bytes=sum(p.stat().st_size for p in directory.rglob("*") if p.is_file()),
        samples=samples)
    destination = ROOT / "output/intraday_acceptance.json"
    destination.write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
