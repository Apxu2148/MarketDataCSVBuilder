"""Complete missing staged feature CSVs from local closed history; never publish."""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from market_data_csv_builder.cancellation import CancellationToken
from market_data_csv_builder.concurrency import BoundedExecutor
from market_data_csv_builder.config import load_config
from market_data_csv_builder.export import slice_closed_and_current
from market_data_csv_builder.intraday.export import SERIES_COLUMNS, load_reusable_series, serialize, write_csv, write_json
from market_data_csv_builder.intraday.pipeline import _calculate_worker
from market_data_csv_builder.intraday.provider import CandleStore, TIMEFRAMES, now_iso
from market_data_csv_builder.progress import StageProgress
from market_data_csv_builder.utils import safe_path_component


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging", type=Path, required=True)
    args = parser.parse_args()
    staging = args.staging.resolve()
    output = ROOT / "output/intraday"
    if staging.parent != (output / ".staging").resolve() or not staging.is_dir():
        parser.error("Expected an existing project INTRADAY staging directory")
    config = load_config(ROOT / "config.toml").intraday
    identities = {}
    present = set()
    identity_names = ("source", "market", "symbol", "instrument_name", "contract_type", "dex", "quote_currency", "turnover_currency")
    for path in sorted((staging / "series").rglob("*.csv")):
        frame = load_reusable_series(path, path.parent.name, path.stem)
        identities[path.parent.name] = {name: frame.iloc[0][name] for name in identity_names}
        present.add((path.parent.name, path.stem))
    tasks = [(symbol, tf) for symbol in sorted(identities) for tf in config.timeframes if (symbol, tf) not in present]
    token = CancellationToken()
    progress = StageProgress("intraday", "offline staging completion", len(tasks))
    report = dict(started_at_utc=now_iso(), source_snapshot=staging.name, preserved_series=len(present),
                  completed=[], failures=[], published=False, freshness="cached closed candles only; live final refresh required")
    lock = output / ".run.lock"
    with lock.open("x", encoding="utf-8") as handle:
        handle.write("offline staging completion " + now_iso())
    try:
        with ProcessPoolExecutor(max_workers=config.feature_workers) as pool:
            def calculate_cached(key):
                symbol, tf = key
                path = ROOT / "data/cache/intraday/v1/bybit/linear_perpetual" / safe_path_component(symbol) / (tf + ".json")
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved["symbol"] != symbol or saved["timeframe"] != tf:
                    raise ValueError("Cache identity mismatch")
                rows = {int(row[0]): row for row in saved["rows"]}
                if not rows:
                    raise ValueError("Empty closed cache")
                frame = CandleStore._frame(rows, TIMEFRAMES[tf][1], max(rows) + TIMEFRAMES[tf][1])
                return pool.submit(_calculate_worker, frame).result()

            progress.start()
            processed = 0
            with BoundedExecutor(tasks, calculate_cached, max_workers=config.feature_workers,
                                 thread_name_prefix="offline-resume", cancellation_token=token) as jobs:
                while jobs.has_pending:
                    for (symbol, tf), future in jobs.next_completed():
                        try:
                            frame = slice_closed_and_current(future.result(), config.closed_bars)
                            for name, value in identities[symbol].items():
                                frame[name] = "" if name == "dex" else value
                            frame["timeframe"] = tf
                            frame["snapshot_id"] = staging.name
                            path = staging / "series" / safe_path_component(symbol) / (tf + ".csv")
                            temporary = path.with_suffix(".csv.tmp")
                            write_csv(temporary, serialize(frame), SERIES_COLUMNS)
                            load_reusable_series(temporary, symbol, tf)
                            temporary.replace(path)
                            report["completed"].append(dict(symbol=symbol, timeframe=tf, closed_bars=len(frame)))
                        except Exception as exc:
                            report["failures"].append(dict(symbol=symbol, timeframe=tf, reason=str(exc)))
                            progress.warning(f"{symbol}/{tf}", str(exc))
                        processed += 1
                        write_json(staging / "offline_resume_progress.json", report)
                    progress.update(processed, completed=len(report["completed"]), failed=len(report["failures"]))
            progress.complete(completed=len(report["completed"]), failed=len(report["failures"]))
        report["completed_at_utc"] = now_iso()
        write_json(staging / "offline_resume_progress.json", report)
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
