from __future__ import annotations

import io
import json
import shutil
from dataclasses import replace
from pathlib import Path
from urllib.error import HTTPError

import pandas as pd
import pytest

from market_data_csv_builder.cancellation import CancellationToken
from market_data_csv_builder.config import AppConfig, load_config, validate_config
from market_data_csv_builder.features import FEATURE_IDS
from market_data_csv_builder.intraday import provider
from market_data_csv_builder.intraday.export import calculate, read_csv, validate_snapshot
from market_data_csv_builder.intraday.hyperliquid import HyperliquidPublicClient, HyperliquidCandleStore, eligible_hyperliquid
from market_data_csv_builder.intraday.pipeline import run_intraday
from market_data_csv_builder.intraday.provider import TIMEFRAMES, RequestFailure

FIXED = 1800000000


class FixtureClient(HyperliquidPublicClient):
    def __init__(self):
        super().__init__(AppConfig(), CancellationToken())
        self.calls = []
        self.broken = False

    def post(self, body):
        self.calls.append(body)
        kind = body['type']
        if kind == 'perpDexs':
            return [None, {'name': 'xyz'}, {'name': 'other'}]
        if kind == 'spotMeta':
            return {'tokens': [{'index': 0, 'name': 'USDC'}, {'index': 1, 'name': 'USDH'}]}
        if kind == 'spotMetaAndAssetCtxs':
            return [{'tokens': [{'index': 0, 'name': 'USDC'}, {'index': 1, 'name': 'USDH'}],
                     'universe': [{'tokens': [1, 0]}]}, [{'midPx': '1'}]]
        if kind == 'metaAndAssetCtxs':
            dex = body['dex']
            symbol = dex + ':BTC' if dex else 'BTC'
            return [{'collateralToken': 1 if dex == 'other' else 0,
                     'universe': [{'name': symbol, 'szDecimals': 3}]},
                    [{'dayNtlVlm': '10000000', 'markPx': '100', 'oraclePx': '101',
                      'funding': '0.00001', 'impactPxs': ['1', '999']}]]
        if kind == 'l2Book':
            return {'coin': body['coin'], 'time': FIXED*1000,
                    'levels': [[{'px': '99', 'sz': '2'}], [{'px': '101', 'sz': '3'}]]}
        if kind == 'candleSnapshot':
            if self.broken:
                raise RequestFailure('fixture failure')
            req = body['req']
            ms = {'1d': 86400000, '4h': 14400000, '1h': 3600000, '15m': 900000, '5m': 300000}[req['interval']]
            boundary = FIXED * 1000 // ms * ms
            rows = []
            for stamp in range(req['startTime'], req['endTime'] + 1, ms):
                if stamp < boundary - 2300 * ms:
                    continue
                price = 100 + (stamp//ms % 100) / 10
                rows.append(dict(t=stamp, T=stamp+ms-1, s=req['coin'], i=req['interval'],
                    o=price, h=price+2, l=price-2, c=price+1, v='12'))
            return rows
        raise AssertionError(body)


@pytest.fixture
def fixed(monkeypatch):
    monkeypatch.setattr(provider.time, 'time', lambda: FIXED)


def test_native_multi_dex_identity_and_book():
    client = FixtureClient()
    items = client.instruments()
    assert [x['symbol'] for x in items] == ['BTC', 'other:BTC', 'xyz:BTC']
    assert [x['quoteCoin'] for x in items] == ['USDC', 'USDH', 'USDC']
    tickers = client.tickers()
    assert len(eligible_hyperliquid(items, tickers, 10_000_000)) == 3
    assert not eligible_hyperliquid(items, tickers, 10_000_001)
    assert 'lastPrice' not in tickers['BTC']
    client.selected = ['xyz:BTC']
    assert client.tickers()['xyz:BTC']['bid1Price'] == '99'
    assert items[0]['max_price_decimals'] == 3


@pytest.mark.parametrize('tf', TIMEFRAMES)
def test_cold_incremental_corrupt_cache_and_nullable_turnover(tmp_path, fixed, tf):
    client = FixtureClient()
    store = HyperliquidCandleStore(tmp_path, client, CancellationToken())
    cold, _ = store.fetch('xyz:BTC', tf, 1200)
    assert len(cold) == 2200 and cold.is_closed.sum() == 2199
    assert cold.turnover.isna().all()
    client.calls.clear()
    warm, _ = store.fetch('xyz:BTC', tf, 1200)
    pd.testing.assert_frame_equal(cold, warm)
    assert len(client.calls) == 1
    request = client.calls[0]['req']
    assert request['endTime'] - request['startTime'] < 3 * TIMEFRAMES[tf][1]
    path = next(store.directory.rglob('*.json'))
    saved = json.loads(path.read_text())
    assert len(saved['rows']) == 2199
    saved['rows'].append(saved['rows'][0])
    path.write_text(json.dumps(saved))
    client.calls.clear()
    repaired, _ = store.fetch('xyz:BTC', tf, 1200)
    pd.testing.assert_frame_equal(cold, repaired)
    assert len(client.calls) == 1
    saved = json.loads(path.read_text()); del saved['rows'][100]
    path.write_text(json.dumps(saved))
    repaired, _ = store.fetch('xyz:BTC', tf, 1200)
    pd.testing.assert_frame_equal(cold, repaired)
    client.broken = True
    before = path.read_bytes()
    with pytest.raises(RequestFailure):
        store.fetch('xyz:BTC', tf, 1200)
    assert path.read_bytes() == before


def test_shared_features_full_tail_parity(tmp_path, fixed):
    frame, _ = HyperliquidCandleStore(tmp_path, FixtureClient(), CancellationToken()).fetch('BTC', '5m', 100)
    hl = calculate(frame, CancellationToken())
    bybit = frame.copy(); bybit['turnover'] = bybit.close * bybit.volume
    reference = calculate(bybit, CancellationToken())
    columns = [*FEATURE_IDS, 'atr_wilder_14', 'return_1_bar', 'return_5_bar', 'return_20_bar']
    pd.testing.assert_frame_equal(hl[columns], reference[columns])
    changed = frame.copy(); changed.loc[len(changed)-1, 'close'] += .1
    tail = calculate(changed, CancellationToken(), previous=hl)
    full = calculate(changed, CancellationToken())
    pd.testing.assert_frame_equal(tail[columns], full[columns], rtol=1e-10, atol=1e-10)


def test_pipeline_snapshot_isolation_and_failure(tmp_path, fixed):
    (tmp_path/'docs').mkdir()
    shutil.copy(Path(__file__).parents[1]/'docs/APX_HYPERLIQUID_INTRADAY_CONSUMER.md', tmp_path/'docs')
    config = AppConfig()
    config = replace(config, intraday_hyperliquid=replace(config.intraday_hyperliquid,
        closed_bars=10, timeframes=['5m'], feature_workers=1))
    client = FixtureClient()
    report = run_intraday(config, tmp_path, client=client, profile='intraday_hyperliquid')
    directory = tmp_path/'output/intraday_hyperliquid/current'
    validate_snapshot(directory)
    assert report['counts']['successful_series'] == 3
    rows = read_csv(directory/'universe.csv')
    assert all(row['last_price'] == 'N/A' and row['tick_size'] == 'N/A' for row in rows)
    assert {row['dex'] for row in rows} == {'', 'xyz', 'other'}
    assert not (tmp_path/'output/intraday').exists()
    assert not (tmp_path/'data/cache/intraday').exists()
    old = (directory/'manifest.json').read_bytes()
    client.broken = True
    with pytest.raises(ValueError, match='No successful series'):
        run_intraday(config, tmp_path, client=client, profile='intraday_hyperliquid')
    assert (directory/'manifest.json').read_bytes() == old


def test_hl_retry_weights_and_public_only(monkeypatch):
    client = HyperliquidPublicClient(AppConfig(), CancellationToken())
    weights, cooldowns = [], []
    monkeypatch.setattr(client.limiter, 'acquire', weights.append)
    monkeypatch.setattr(client.limiter, 'cooldown', lambda _: None)
    monkeypatch.setattr(client.limiter, 'record_429', cooldowns.append)
    attempts = []
    def open_request(request):
        attempts.append(request)
        if len(attempts) == 1:
            raise HTTPError(request.full_url, 429, 'slow', {'Retry-After': '2'}, None)
        return io.BytesIO(b'[null]')
    monkeypatch.setattr(client, 'open', open_request)
    assert client.post({'type': 'perpDexs'}) == [None]
    assert weights == [20, 20] and cooldowns == [2]
    with pytest.raises(ValueError):
        client.post({'type': 'order'})


def test_independent_config_and_retention_limit():
    config = load_config(Path(__file__).parents[1]/'config_intraday_hyperliquid.toml')
    assert config.intraday.source == 'bybit'
    assert config.intraday_hyperliquid.source == 'hyperliquid'
    with pytest.raises(ValueError, match='5000'):
        validate_config(replace(config, intraday_hyperliquid=replace(config.intraday_hyperliquid, closed_bars=5000)))


def test_gap_pagination_does_not_stop_on_short_page(tmp_path, fixed):
    class SparseClient(FixtureClient):
        def post(self, body):
            result = super().post(body)
            if body['type'] == 'candleSnapshot':
                return [row for row in result if row['t']//300000 % 7 != 0]
            return result
    store = HyperliquidCandleStore(tmp_path, SparseClient(), CancellationToken(), enabled=False)
    store.page_limit = 100
    frame, _ = store.fetch('BTC', '5m', 1200)
    boundary = FIXED*1000//300000*300000
    assert frame.iloc[0].timestamp.value//1000000 <= boundary - 2198*300000
    assert len(frame) > 1800
    assert not frame.timestamp.duplicated().any()


def test_publication_report_failure_rolls_back(tmp_path, monkeypatch):
    from market_data_csv_builder.intraday import export
    current = tmp_path/'current'; current.mkdir(); (current/'old').write_text('keep')
    staging = tmp_path/'staging'; staging.mkdir(); (staging/'new').write_text('new')
    def fail(*args):
        raise OSError('report write failure')
    monkeypatch.setattr(export, 'write_json', fail)
    with pytest.raises(OSError):
        export.publish(staging, tmp_path, 'test', report={'elapsed_seconds': {'total': 1}})
    assert (current/'old').read_text() == 'keep'
    assert (staging/'new').exists()


def test_collateral_fx_threshold_uses_observed_rate():
    class FxClient(FixtureClient):
        def post(self, body):
            result = super().post(body)
            if body['type'] == 'spotMetaAndAssetCtxs':
                result[1][0]['midPx'] = '0.98'
            return result
    client = FxClient(); items = client.instruments(); tickers = client.tickers()
    assert tickers['other:BTC']['turnover24h_usd'] == 9800000
    assert {x['symbol'] for x in eligible_hyperliquid(items, tickers, 10000000)} == {'BTC', 'xyz:BTC'}
