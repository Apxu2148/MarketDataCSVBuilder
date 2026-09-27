"""Explicit public live acceptance; all artifacts stay in the selected worktree."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from market_data_csv_builder.cancellation import CancellationToken
from market_data_csv_builder.config import load_config
from market_data_csv_builder.intraday.hyperliquid import HyperliquidPublicClient, eligible_hyperliquid
from market_data_csv_builder.intraday.pipeline import run_intraday


class SmokeClient(HyperliquidPublicClient):
    def instruments(self):
        items = super().instruments()
        tickers = self.tickers()
        eligible = eligible_hyperliquid(items, tickers, self.config.min_turnover24h_usd)
        selected = {}
        for item in eligible:
            selected.setdefault(item['dex'], item)
        if len(selected) < 3:
            raise ValueError('Smoke requires native and at least two HIP-3 DEXes')
        chosen = [selected[dex] for dex in sorted(selected)[:3]]
        print('SMOKE samples:', [item['symbol'] for item in chosen], flush=True)
        return chosen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--label', default='cold')
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    config = load_config(project/'config_intraday_hyperliquid.toml')
    root = project
    token = CancellationToken()
    client = None
    if args.smoke:
        root = project/'output/hyperliquid_smoke'
        (root/'docs').mkdir(parents=True, exist_ok=True)
        shutil.copy(project/'docs/APX_HYPERLIQUID_INTRADAY_CONSUMER.md', root/'docs')
        client = SmokeClient(config, token)
    report = run_intraday(config, root, profile='intraday_hyperliquid', client=client, cancellation_token=token)
    evidence = project/'output/hyperliquid_acceptance'
    evidence.mkdir(parents=True, exist_ok=True)
    label = 'smoke' if args.smoke else args.label
    (evidence/(label+'.json')).write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report['counts']), flush=True)


if __name__ == '__main__':
    main()
