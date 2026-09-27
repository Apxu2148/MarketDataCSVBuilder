"""Audit completed cache and retained snapshot coverage without network calls."""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from market_data_csv_builder.intraday.export import read_csv
from market_data_csv_builder.intraday.hyperliquid import HyperliquidCandleStore
from market_data_csv_builder.intraday.provider import TIMEFRAMES

def main():
    output = ROOT/'output/intraday_hyperliquid'
    current = output/'current'
    previous = sorted(output.glob('previous_*'))[-1]
    symbols = lambda directory: {row['symbol'] for row in read_csv(directory/'universe.csv')}
    old, new = symbols(previous), symbols(current)
    files = list((ROOT/'data/cache/intraday_hyperliquid').rglob('*.json'))
    cutoff = int(time.time()*1000)
    rows_checked = 0
    for path in files:
        saved = json.loads(path.read_text())
        stamps = [int(row[0]) for row in saved['rows']]
        assert stamps == sorted(set(stamps)), path
        duration = TIMEFRAMES[saved['timeframe']][1]
        frame = HyperliquidCandleStore._frame(dict(zip(stamps, saved['rows'])), duration, cutoff)
        assert frame.is_closed.all(), path
        rows_checked += len(frame)
    result = dict(cache_files=len(files), cache_rows=rows_checked, duplicates=0,
        invalid_ohlcv_or_grid=0, provisional_cached=0, retained_previous_symbols=len(old),
        common_symbols=len(old & new),
        added_symbols=sorted(new-old), removed_symbols=sorted(old-new))
    (ROOT/'output/hyperliquid_acceptance/cache_audit.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))

if __name__ == '__main__':
    main()
