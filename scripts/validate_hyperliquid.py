"""Validate a full snapshot and recompute native/HIP-3 samples from local history."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from market_data_csv_builder.cancellation import CancellationToken
from market_data_csv_builder.features import FEATURE_IDS
from market_data_csv_builder.intraday.export import RETURNS, calculate, load_reusable_series, read_csv, validate_snapshot
from market_data_csv_builder.intraday.hyperliquid import HyperliquidCandleStore
from market_data_csv_builder.intraday.provider import TIMEFRAMES, RAW_COLUMNS
from market_data_csv_builder.utils import safe_path_component


def main():
    current = ROOT/'output/intraday_hyperliquid/current'
    validate_snapshot(current)
    manifest = json.loads((current/'manifest.json').read_text())
    report = json.loads((current/'run_report.json').read_text())
    catalog = read_csv(current/'catalog.csv')
    completed = pd.Timestamp(manifest['completed_at_utc'])
    freshness = {}
    for tf in manifest['timeframes']:
        entries = [row for row in catalog if row['timeframe'] == tf]
        ages = [(completed-pd.Timestamp(row['final_refresh_at_utc'])).total_seconds() for row in entries]
        assert min(ages) >= 0
        freshness[tf] = dict(series=len(entries), oldest_fetch_age_at_publication_seconds=max(ages),
            missing_current=sum(row['missing_current_bar'] == 'true' for row in entries),
            provider_gap_count=sum(int(row['gap_count']) for row in entries))
    samples = {}
    for row in read_csv(current/'universe.csv'):
        samples.setdefault(row['dex'], row['symbol'])
    evidence = []
    for entry in catalog:
        if entry['symbol'] not in samples.values() or entry['status'] == 'FAILED':
            continue
        symbol, tf = entry['symbol'], entry['timeframe']
        exported = load_reusable_series(current/entry['relative_path'], symbol, tf, source='hyperliquid')
        cache = ROOT/'data/cache'/HyperliquidCandleStore.cache_namespace/safe_path_component(symbol)/(tf+'.json')
        saved = json.loads(cache.read_text())
        rows = {int(row[0]): row for row in saved['rows']}
        for row in exported.loc[exported.provisional, RAW_COLUMNS].itertuples(index=False, name=None):
            rows[int(row[0].timestamp()*1000)] = [int(row[0].timestamp()*1000), *row[1:]]
        cutoff = int(pd.Timestamp(entry['final_refresh_at_utc']).timestamp()*1000)
        frame = HyperliquidCandleStore._frame(rows, TIMEFRAMES[tf][1], cutoff)
        recalculated = calculate(frame, CancellationToken()).set_index('timestamp')
        actual = exported.set_index('timestamp')
        columns = [*FEATURE_IDS, 'atr_wilder_14', *RETURNS]
        pd.testing.assert_frame_equal(actual[columns], recalculated.loc[actual.index, columns],
            check_dtype=False, rtol=1e-10, atol=1e-10)
        evidence.append(dict(symbol=symbol, timeframe=tf, rows=len(exported), feature_parity=True))
        print(f'Parity PASS: {symbol} {tf}', flush=True)
    result = dict(snapshot_id=manifest['snapshot_id'], counts=report['counts'], samples=evidence,
        checked_at_utc=datetime.now(timezone.utc).isoformat(), freshness=freshness,
        byte_count_matches=report['snapshot_bytes'] == sum(p.stat().st_size for p in current.rglob('*') if p.is_file()))
    assert result['byte_count_matches']
    destination = ROOT/'output/hyperliquid_acceptance/validation.json'
    destination.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
