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
from market_data_csv_builder.cli import build_parser, main
from market_data_csv_builder.config import AppConfig, IntradayConfig, validate_config
from market_data_csv_builder.features import FEATURE_IDS, calculate_features
from market_data_csv_builder.intraday import provider
from market_data_csv_builder.intraday.export import calculate, publish, read_csv, validate_snapshot
from market_data_csv_builder.intraday.pipeline import run_intraday
from market_data_csv_builder.intraday.provider import (BybitPublicClient, CandleStore, TIMEFRAMES,
    WARMUP_BARS, RequestFailure, eligible_instruments)

TOKEN = CancellationToken()
FIXED = 1800000000


def instrument(symbol="BTCUSDT", **changes):
    return dict(symbol=symbol, status="Trading", contractType="LinearPerpetual",
                quoteCoin="USDT", settleCoin="USDT", baseCoin="BTC", **changes)


class FakeClient:
    def __init__(self, length=2250, failed=None):
        self.length, self.failed = length, failed
        self.calls = []
        self.tick = 0

    def metrics(self):
        return dict(requests_sent=len(self.calls), retry_count=0, rate_limit_events=0,
                    http_429_count=0, bybit_10006_count=0)

    def instruments(self):
        return [instrument()]

    def tickers(self):
        self.tick += 1
        return {"BTCUSDT": dict(turnover24h="10000000", fundingRate="0.001", lastPrice=str(self.tick),
                                nextFundingTime="1800000000000", bid1Price="99", ask1Price="101")}

    def get(self, path, **params):
        self.calls.append((path, params))
        if self.failed == params.get("interval"):
            raise RequestFailure("synthetic failure", 4)
        duration = next(value[1] for value in TIMEFRAMES.values() if value[0] == params["interval"])
        boundary = FIXED * 1000 // duration * duration
        rows = []
        for index in range(self.length):
            stamp = boundary - (self.length - 1 - index) * duration
            if params["start"] <= stamp <= params["end"]:
                close = 100 + index / 10
                rows.append([str(stamp), str(close - .5), str(close + 1), str(close - 1), str(close), "10", "1000"])
        return {"list": list(reversed(rows))[:params["limit"]]}


@pytest.fixture
def fixed(monkeypatch):
    monkeypatch.setattr(provider.time, "time", lambda: FIXED)


def test_universe_threshold_metadata_no_top_n():
    rows = [instrument(f"S{i}") for i in range(150)]
    tickers = {row["symbol"]: {"turnover24h": str(10_000_000 + i)} for i, row in enumerate(rows)}
    assert len(eligible_instruments(rows, tickers, 10_000_000)) == 150
    assert len(eligible_instruments(rows, tickers, 10_000_001)) == 149
    assert not eligible_instruments(rows, tickers, 30_000_000)
    for field, value in [("status", "PreLaunch"), ("contractType", "LinearFutures"), ("settleCoin", "USDC"), ("quoteCoin", "USDC")]:
        bad = {**rows[0], field: value}
        assert not eligible_instruments([bad], tickers, 0)


@pytest.mark.parametrize("timeframe", TIMEFRAMES)
def test_pagination_cache_and_warmup(tmp_path, fixed, timeframe):
    client = FakeClient()
    store = CandleStore(tmp_path, client, TOKEN)
    frame, _ = store.fetch("BTCUSDT", timeframe, 1200)
    assert frame.is_closed.sum() == 1200 + WARMUP_BARS
    assert frame.provisional.sum() == 1
    assert frame.timestamp.is_monotonic_increasing and not frame.timestamp.duplicated().any()
    assert len(client.calls) == 3
    client.calls.clear()
    again, _ = store.fetch("BTCUSDT", timeframe, 1200)
    pd.testing.assert_frame_equal(frame, again)
    assert len(client.calls) == 1
    assert client.calls[0][1]["start"] == int(frame.iloc[-3].timestamp.timestamp() * 1000)
    cache = list(tmp_path.rglob("*.json"))[0]
    assert len(json.loads(cache.read_text())["rows"]) == 2199
    refreshed = CandleStore(tmp_path, client, TOKEN, refresh=True)
    client.calls.clear()
    refreshed.fetch("BTCUSDT", timeframe, 1200)
    assert len(client.calls) == 3


def test_cache_timeframe_separation_and_new_closed(tmp_path, fixed, monkeypatch):
    client = FakeClient()
    store = CandleStore(tmp_path, client, TOKEN)
    first, _ = store.fetch("BTCUSDT", "5m", 1200)
    store.fetch("BTCUSDT", "1D", 1200)
    assert len(list(tmp_path.rglob("*.json"))) == 2
    # API returns the old current as closed; absence of a new provisional is allowed.
    monkeypatch.setattr(provider.time, "time", lambda: FIXED + 301)
    after, _ = store.fetch("BTCUSDT", "5m", 1200)
    assert after.iloc[-1].is_closed
    assert not after.timestamp.duplicated().any()
    assert after.iloc[-1].timestamp == first.iloc[-1].timestamp


def test_malformed_response_is_not_cached(tmp_path, fixed):
    client = FakeClient()
    client.get = lambda *args, **kwargs: {"list": [["bad"]]}
    with pytest.raises(ValueError, match="Malformed"):
        CandleStore(tmp_path, client, TOKEN).fetch("BTCUSDT", "5m", 1200)
    assert not list(tmp_path.rglob("*.json"))


def test_atr_seed_smoothing_and_provisional():
    frame = pd.DataFrame(dict(timestamp=pd.date_range("2026-01-01", periods=17, tz="UTC"),
        open=[10.] * 17, high=[12.] * 14 + [14., 15., 16.], low=[8.] * 17,
        close=[10.] * 17, is_closed=[True]*16+[False]))
    result = calculate(frame, TOKEN)
    assert result.atr_wilder_14.iloc[:13].isna().all()
    assert result.atr_wilder_14.iloc[13] == 4
    assert result.atr_wilder_14.iloc[14] == pytest.approx((4*13+6)/14)
    assert result.atr_wilder_14.iloc[16] == pytest.approx((result.atr_wilder_14.iloc[15]*13+8)/14)
    assert calculate(frame.iloc[:10], TOKEN).atr_wilder_14.isna().all()
    pd.testing.assert_frame_equal(result.loc[:, FEATURE_IDS], calculate_features(frame).loc[:, FEATURE_IDS])


def test_final_tail_matches_full_engine(candle_frame):
    initial = candle_frame.copy()
    old = calculate(initial, TOKEN)
    updated = initial.copy()
    updated.loc[1200, "high"] += 20
    updated.loc[1200, "close"] += .5
    fast = calculate(updated, TOKEN, old)
    full = calculate(updated, TOKEN)
    pd.testing.assert_frame_equal(fast, full, check_dtype=False)


def test_structural_warmup_matches_longer_history(candle_frame):
    # The export's first row has the same structures with exactly 999 predecessors.
    long = pd.concat([candle_frame]*3, ignore_index=True)
    long["timestamp"] = pd.date_range("2020-01-01", periods=len(long), tz="UTC")
    full = calculate_features(long)
    offset = 1300
    shortened = calculate_features(long.iloc[offset-WARMUP_BARS:].reset_index(drop=True))
    pd.testing.assert_frame_equal(full.loc[offset:, FEATURE_IDS].reset_index(drop=True),
        shortened.loc[WARMUP_BARS:, FEATURE_IDS].reset_index(drop=True))


def prepare_root(tmp_path):
    (tmp_path / "docs").mkdir()
    source = Path(__file__).resolve().parents[1] / "docs/APX_INTRADAY_MARKETDATA_CONSUMER.md"
    shutil.copyfile(source, tmp_path / "docs" / source.name)


def test_pipeline_partial_export_consistency_and_isolation(tmp_path, fixed):
    prepare_root(tmp_path)
    apx = tmp_path / "output/current"
    apx.mkdir(parents=True)
    (apx / "marker").write_text("APX")
    client = FakeClient(length=25, failed="240")
    report = run_intraday(AppConfig(), tmp_path, client=client)
    current = tmp_path / "output/intraday/current"
    validate_snapshot(current)
    assert report["counts"]["successful_series"] == 4
    assert report["counts"]["failed_series"] == 1
    assert report["counts"]["partial_symbols"] == 1
    assert report["failures"][0]["retries"] == 4
    assert (apx / "marker").read_text() == "APX"
    catalog = read_csv(current / "catalog.csv")
    assert all(row["closed_bars"] == "24" for row in catalog if row["status"] == "READY")
    universe = read_csv(current / "universe.csv")
    assert universe[0]["last_price"] == "2"
    assert universe[0]["funding_rate"] == "0.001"
    assert all(row["final_refresh_at_utc"] for row in catalog if row["status"] == "READY")
    # Corruption must be rejected before publication.
    latest = current / "latest_features.csv"
    text = latest.read_text()
    latest.write_text(text.replace(",CLOSED,", ",BROKEN,", 1))
    with pytest.raises(ValueError):
        validate_snapshot(current)


def test_failed_run_preserves_current(tmp_path, fixed):
    prepare_root(tmp_path)
    config = replace(AppConfig(), intraday=IntradayConfig(timeframes=["5m"]))
    run_intraday(config, tmp_path, client=FakeClient(length=25))
    current = tmp_path / "output/intraday/current"
    before = {p.relative_to(current): p.read_bytes() for p in current.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="No successful"):
        run_intraday(config, tmp_path, client=FakeClient(failed="5"))
    assert before == {p.relative_to(current): p.read_bytes() for p in current.rglob("*") if p.is_file()}
    assert list((tmp_path / "output/intraday/.staging").rglob("failure.json"))


def test_publish_rollback(tmp_path, monkeypatch):
    current, staging = tmp_path / "current", tmp_path / "staging"
    current.mkdir()
    staging.mkdir()
    (current / "marker").write_text("old")
    original = Path.replace
    def fail(self, target):
        if self == staging:
            raise OSError("locked")
        return original(self, target)
    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError):
        publish(staging, tmp_path, "test")
    assert (current / "marker").read_text() == "old"


class Response(io.BytesIO):
    def __init__(self, code=0, headers=None):
        super().__init__(json.dumps(dict(retCode=code, retMsg="test", result={"list": []})).encode())
        self.headers = headers or {}


@pytest.mark.parametrize("error", [429, 10006])
@pytest.mark.parametrize("recover", [True, False])
def test_rate_limit_retry_recovery_exhaustion(monkeypatch, error, recover):
    config = replace(AppConfig(), intraday=IntradayConfig(max_retries=2, backoff_base=0, backoff_max=0, jitter=0))
    client = BybitPublicClient(config, TOKEN)
    waits = []
    monkeypatch.setattr(client.limiter, "acquire", lambda weight: None)
    monkeypatch.setattr(client.limiter, "cooldown", waits.append)
    calls = []
    def request(*args, **kwargs):
        calls.append(1)
        if recover and len(calls) > 1:
            return Response()
        if error == 429:
            raise HTTPError("url", 429, "limited", {"Retry-After": "2"}, None)
        return Response(10006, {"X-Bapi-Limit-Reset-Timestamp": str(int((provider.time.time()+2)*1000))})
    monkeypatch.setattr(client, "open", request)
    if recover:
        assert client.get("tickers") == {"list": []}
        assert len(calls) == 2
    else:
        with pytest.raises(RequestFailure) as caught:
            client.get("tickers")
        assert caught.value.retries == 2
        assert len(calls) == 3
    assert client.metrics()["rate_limit_events"] == (1 if recover else 3)
    assert any(wait > 1 for wait in waits)


def test_cli_apx_default_and_intraday_guards():
    parser = build_parser(Path("."))
    assert parser.parse_args([]).profile == "apx"
    assert parser.parse_args(["--profile", "apx"]).source == "all"
    with pytest.raises(SystemExit):
        main(["--profile", "intraday", "--limit", "1"])


@pytest.mark.parametrize("changes", [dict(timeframes=["bad"]), dict(timeframes=["5m", "5m"]),
    dict(closed_bars=0), dict(atr_period=10), dict(min_turnover24h_usdt=float("nan")), dict(concurrency=1.5)])
def test_invalid_config(changes):
    with pytest.raises(ValueError):
        validate_config(replace(AppConfig(), intraday=IntradayConfig(**changes)))


def test_full_export_window_and_cached_replay(tmp_path, fixed):
    prepare_root(tmp_path)
    config = replace(AppConfig(), intraday=IntradayConfig(timeframes=["5m"]))
    run_intraday(config, tmp_path, client=FakeClient())
    current = tmp_path / "output/intraday/current"
    first = pd.read_csv(current / "series/BTCUSDT/5m.csv")
    assert len(first) == 1201 and first.is_closed.sum() == 1200
    assert first.sma_100.notna().all()
    assert first.levels_resistance_long.notna().all()
    run_intraday(config, tmp_path, client=FakeClient())
    second = pd.read_csv(current / "series/BTCUSDT/5m.csv")
    pd.testing.assert_frame_equal(first.drop(columns="snapshot_id"), second.drop(columns="snapshot_id"))
    assert len(list((tmp_path / "output/intraday").glob("previous_*"))) == 1


def test_final_refresh_failure_is_partial(tmp_path, fixed):
    prepare_root(tmp_path)
    client = FakeClient(length=25)
    original = client.get
    def fail_tail(path, **params):
        if len(client.calls) >= 1:
            raise RequestFailure("tail unavailable", 4)
        return original(path, **params)
    client.get = fail_tail
    config = replace(AppConfig(), intraday=IntradayConfig(timeframes=["5m"]))
    report = run_intraday(config, tmp_path, client=client)
    assert report["counts"]["partial_series"] == 1
    catalog = read_csv(tmp_path / "output/intraday/current/catalog.csv")
    assert catalog[0]["status"] == "PARTIAL"
    assert not catalog[0]["final_refresh_at_utc"]
    assert catalog[0]["error"] == "tail unavailable"


def test_no_cache_and_cancellation(tmp_path, fixed):
    token = CancellationToken()
    client = FakeClient(length=25)
    store = CandleStore(tmp_path, client, token, enabled=False)
    store.fetch("BTCUSDT", "5m", 1200)
    assert not list(tmp_path.rglob("*.json"))
    token.request()
    from market_data_csv_builder.cancellation import CancellationRequested
    with pytest.raises(CancellationRequested):
        store.fetch("BTCUSDT", "5m", 1200)


def test_instruments_cursor_pagination(monkeypatch):
    client = BybitPublicClient(AppConfig(), TOKEN)
    calls = []
    def get(path, **params):
        calls.append(params)
        return {"list": [instrument("A" if not params["cursor"] else "B")],
                "nextPageCursor": "next" if not params["cursor"] else ""}
    monkeypatch.setattr(client, "get", get)
    assert [i["symbol"] for i in client.instruments()] == ["A", "B"]
    assert calls[1]["cursor"] == "next"
    monkeypatch.setattr(client, "get", lambda *a, **kw: {"list": [], "nextPageCursor": "same"})
    with pytest.raises(ValueError, match="cursor repeated"):
        client.instruments()


def test_apx_explicit_profile_preserves_dispatch(monkeypatch):
    from market_data_csv_builder import cli
    calls = []
    monkeypatch.setattr(cli, "run_pipeline", lambda *args, **kwargs: calls.append((args, kwargs)) or {})
    monkeypatch.setattr(cli, "_print_summary", lambda report: None)
    for args in ([], ["--profile", "apx"]):
        assert cli.main([*args, "--as-of", "2026-01-01T00:00:00Z"]) == 0
    assert calls[0][0] == calls[1][0]
    for args, kwargs in calls:
        assert kwargs["selected_source"] == "all" and kwargs["limit"] is None
        kwargs.pop("cancellation_token")
    assert calls[0][1] == calls[1][1]


def test_transient_error_retries_same_host(monkeypatch):
    config = replace(AppConfig(), intraday=IntradayConfig(max_retries=1, backoff_base=0, jitter=0))
    client = BybitPublicClient(config, TOKEN)
    monkeypatch.setattr(client.limiter, "acquire", lambda _: None)
    urls = []
    def request(req, **kwargs):
        urls.append(req.full_url)
        if len(urls) == 1:
            raise TimeoutError("TLS handshake timed out")
        return Response()
    monkeypatch.setattr(client, "open", request)
    client.get("tickers")
    assert len(urls) == 2 and urls[0] == urls[1]
