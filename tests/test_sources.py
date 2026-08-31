from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

from market_data_csv_builder.config import AppConfig, BybitConfig, HyperliquidConfig, MoexConfig
from market_data_csv_builder.liquidity import calculate_average_turnover
from market_data_csv_builder.models import Instrument
from market_data_csv_builder.sources.bybit import BybitSource
from market_data_csv_builder.sources.hyperliquid import HyperliquidSource
from market_data_csv_builder.sources.moex import MOSCOW, MoexSource, _has_more


class FakeHttp:
    def __init__(self, get_payloads: dict[str, Any] | None = None, post_handler=None) -> None:
        self.get_payloads = get_payloads or {}
        self.post_handler = post_handler
        self.calls: list[tuple[str, Any]] = []

    def get_json(self, url: str, params: dict[str, Any]) -> Any:
        self.calls.append((url, dict(params)))
        for suffix, payload in self.get_payloads.items():
            if url.endswith(suffix):
                return payload(params) if callable(payload) else payload
        raise AssertionError(url)

    def post_json(
        self, url: str, body: dict[str, Any], *, estimated_weight: int = 1
    ) -> Any:
        self.calls.append((url, dict(body)))
        return self.post_handler(body)


def _table(rows: list[list[Any]], columns: list[str]) -> dict[str, Any]:
    return {"securities": {"columns": columns, "data": rows}}


def test_moex_discovery_categories_and_candle_normalization(tmp_path: Path) -> None:
    config = AppConfig(moex=MoexConfig(page_size=500))
    source = MoexSource(config, tmp_path, no_cache=True)
    columns = ["SECID", "SHORTNAME", "SECNAME", "LATNAME", "LOTSIZE", "BOARDID", "PRIMARY_BOARDID", "SECTYPE", "SECTYPE_NAME", "TYPE", "TYPE_NAME", "GROUP", "STATUS"]
    source.http = FakeHttp(
        {
            "/stock/markets/shares/securities.json": _table(
                [["SBER", "Sber", "", "", 10, "", "", "common_share", "", "", "Share", "", "A"], ["TMOS", "ETF Fund", "", "", 1, "", "", "etf", "", "", "Fund", "", "A"]], columns
            ),
            "/stock/markets/bonds/securities.json": _table([["BOND", "Bond", "", "", 1, "", "", "bond", "", "", "Bond", "", "A"]], columns),
            "/futures/markets/forts/securities.json": _table([["SiU6", "Si future", "", "", 1, "", "", "", "", "", "Future", "", "A"], ["CNYF", "CNY perpetual", "", "", 1, "", "", "", "", "", "Future", "", "A"]], columns),
            "/stock/markets/shares/securities/SBER/candles.json": {
                "candles": {"columns": ["begin", "open", "high", "low", "close", "volume", "value"], "data": [["2026-01-01", 100, 110, 90, 105, 10, 1000], ["2026-01-02", 105, 115, 95, 110, 12, 1320]]}
            },
        }
    )
    instruments = source.discover()
    assert {(item.symbol, item.market, item.contract_type) for item in instruments} == {
        ("SBER", "shares", "Share"), ("TMOS", "funds", "Fund"), ("BOND", "bonds", "Bond"), ("SiU6", "futures", "TermFuture"), ("CNYF", "futures", "PerpetualFuture")
    }
    sber = next(item for item in instruments if item.symbol == "SBER")
    candles = source.fetch_daily_candles(sber, 30, datetime(2026, 1, 2, 12, tzinfo=UTC))
    assert [item.is_closed for item in candles] == [True, False]
    assert candles[0].turnover == 1000
    assert candles[0].turnover_currency == "RUB"


def test_moex_cursor_controls_pagination_even_when_server_page_is_smaller() -> None:
    payload = {"securities.cursor": {"columns": ["INDEX", "TOTAL", "PAGESIZE"], "data": [[0, 250, 100]]}}
    assert _has_more(payload, "securities", 0, 100, 500)
    assert not _has_more(payload, "securities", 200, 50, 500)


def test_moex_securities_discovery_is_one_request_without_pagination(tmp_path: Path) -> None:
    source = MoexSource(AppConfig(), tmp_path, no_cache=True)
    source.http = FakeHttp({"/futures/markets/forts/securities.json": _table([], ["SECID"])})

    assert source._fetch_securities("futures", "forts") == []
    assert len(source.http.calls) == 1
    assert "start" not in source.http.calls[0][1]
    assert "limit" not in source.http.calls[0][1]
    assert source.http.calls[0][1]["iss.only"] == "securities"


def test_moex_forts_uses_historical_value_and_current_valtoday(tmp_path: Path) -> None:
    source = MoexSource(AppConfig(moex=MoexConfig(page_size=500)), tmp_path, no_cache=True)
    today = datetime.now(MOSCOW).date()
    first_closed = today - timedelta(days=30)
    closed_dates = [first_closed + timedelta(days=index) for index in range(30)]
    candle_rows = [
        [value.isoformat(), 100 + index, 102 + index, 99 + index, 101 + index, 10 + index, 0]
        for index, value in enumerate(closed_dates)
    ]
    candle_rows.append([today.isoformat(), 130, 132, 129, 131, 40, 0])
    history_rows = [
        [value.isoformat(), "SiU6", "RFUD", (index + 1) * 1_000_000]
        for index, value in enumerate(closed_dates)
    ]
    source.http = FakeHttp(
        {
            "/futures/markets/forts/securities/SiU6/candles.json": {
                "candles": {
                    "columns": ["begin", "open", "high", "low", "close", "volume", "value"],
                    "data": candle_rows,
                }
            },
            "/history/engines/futures/markets/forts/securities/SiU6.json": {
                "history": {
                    "columns": ["TRADEDATE", "SECID", "BOARDID", "VALUE"],
                    "data": history_rows,
                }
            },
            "/futures/markets/forts/securities/SiU6.json": {
                "marketdata": {
                    "columns": ["SECID", "BOARDID", "VALTODAY"],
                    "data": [["SiU6", "RFUD", 7_500_000]],
                }
            },
        }
    )
    instrument = Instrument(
        "moex", "futures", "SiU6", "Si future", "TermFuture",
        quote_currency="RUB", api_engine="futures", api_market="forts", lot_size=999,
    )

    as_of = datetime.combine(today, time(12), tzinfo=MOSCOW).astimezone(UTC)
    candles = source.fetch_daily_candles(instrument, 30, as_of)

    assert len(candles) == 31
    assert candles[0].turnover == 1_000_000
    assert candles[0].volume == 10
    assert candles[-2].turnover == 30_000_000
    assert candles[-1].turnover == 7_500_000
    assert candles[-1].is_closed is False
    assert candles[-1].provisional is True
    assert calculate_average_turnover(candles, 30).average_turnover == 15_500_000
    assert all(item.turnover_currency == "RUB" for item in candles)


def test_moex_forts_does_not_synthesize_turnover_when_history_value_is_missing(tmp_path: Path) -> None:
    source = MoexSource(AppConfig(), tmp_path, no_cache=True)
    source.http = FakeHttp(
        {
            "/futures/markets/forts/securities/RIU6/candles.json": {
                "candles": {
                    "columns": ["begin", "open", "high", "low", "close", "volume", "value"],
                    "data": [["2026-01-01", 100, 110, 90, 105, 10, 0]],
                }
            },
            "/history/engines/futures/markets/forts/securities/RIU6.json": {
                "history": {
                    "columns": ["TRADEDATE", "SECID", "BOARDID", "VALUE"],
                    "data": [],
                }
            },
        }
    )
    instrument = Instrument(
        "moex", "futures", "RIU6", "RI future", "TermFuture",
        quote_currency="RUB", api_engine="futures", api_market="forts", lot_size=10,
    )

    candles = source.fetch_daily_candles(instrument, 30, datetime(2026, 1, 2, 12, tzinfo=UTC))

    assert len(candles) == 1
    assert candles[0].turnover is None


def test_bybit_metadata_types_usdt_usdc_and_time_pagination(tmp_path: Path) -> None:
    source = BybitSource(AppConfig(), tmp_path, no_cache=True)

    def instruments(_params):
        return {"retCode": 0, "result": {"list": [
            {"symbol": "BTCUSDT", "status": "Trading", "contractType": "LinearPerpetual", "quoteCoin": "USDT"},
            {"symbol": "BTCUSDC", "status": "Trading", "contractType": "LinearPerpetual", "quoteCoin": "USDC"},
            {"symbol": "ESUSDTZ26", "status": "Trading", "contractType": "LinearFutures", "quoteCoin": "USDT"},
        ], "nextPageCursor": ""}}

    source.http = FakeHttp(
        {
            "/v5/market/instruments-info": instruments,
            "/v5/market/kline": {"retCode": 0, "result": {"list": [
                ["1767398400000", "105", "115", "95", "110", "10", "1100"],
                ["1767312000000", "100", "110", "90", "105", "12", "1260"],
            ]}},
        }
    )
    instruments_found = source.discover()
    assert {(item.symbol, item.contract_type, item.quote_currency) for item in instruments_found} == {
        ("BTCUSDT", "LinearPerpetual", "USDT"),
        ("BTCUSDC", "LinearPerpetual", "USDC"),
        ("ESUSDTZ26", "LinearFutures", "USDT"),
    }
    btc = next(item for item in instruments_found if item.symbol == "BTCUSDT")
    candles = source.fetch_daily_candles(btc, 30, datetime(2026, 1, 3, 12, tzinfo=UTC))
    assert len(candles) == 2
    assert candles[-1].is_closed is False
    assert candles[-1].turnover == 1100
    kline_params = [call[1] for call in source.http.calls if call[0].endswith("/v5/market/kline")][-1]
    assert "cursor" not in kline_params


def test_bybit_market_config_switches(tmp_path: Path) -> None:
    source = BybitSource(
        AppConfig(bybit=BybitConfig(linear_perpetual_enabled=False, linear_futures_enabled=True, spot_enabled=False)),
        tmp_path,
        no_cache=True,
    )
    source.http = FakeHttp({"/v5/market/instruments-info": {"retCode": 0, "result": {"list": [
        {"symbol": "BTCUSDT", "status": "Trading", "contractType": "LinearPerpetual", "quoteCoin": "USDT"},
        {"symbol": "BTC-30DEC26", "status": "Trading", "contractType": "LinearFutures", "quoteCoin": "USDT"},
    ]}}})
    assert [(item.symbol, item.contract_type) for item in source.discover()] == [("BTC-30DEC26", "LinearFutures")]


def test_bybit_spot_discovery_does_not_send_unsupported_pagination(tmp_path: Path) -> None:
    source = BybitSource(
        AppConfig(bybit=BybitConfig(linear_perpetual_enabled=False, linear_futures_enabled=False, spot_enabled=True)),
        tmp_path,
        no_cache=True,
    )
    source.http = FakeHttp({"/v5/market/instruments-info": {"retCode": 0, "result": {"list": [
        {"symbol": "ETHUSDC", "status": "Trading", "quoteCoin": "USDC"}
    ]}}})
    instruments = source.discover()
    assert [(item.symbol, item.contract_type) for item in instruments] == [("ETHUSDC", "Spot")]
    params = source.http.calls[0][1]
    assert params == {"category": "spot"}


def test_bybit_kline_paginates_backward_by_end_not_cursor(tmp_path: Path) -> None:
    source = BybitSource(AppConfig(bybit=BybitConfig(page_size=2)), tmp_path, no_cache=True)
    calls = 0

    def kline(params):
        nonlocal calls
        calls += 1
        if calls == 1:
            rows = [
                ["1767398400000", "3", "4", "2", "3", "1", "3"],
                ["1767312000000", "2", "3", "1", "2", "1", "2"],
            ]
        else:
            rows = [
                ["1767225600000", "1", "2", "0.5", "1", "1", "1"],
                ["1767139200000", "1", "2", "0.5", "1", "1", "1"],
            ]
        return {"retCode": 0, "result": {"list": rows}}

    source.http = FakeHttp({"/v5/market/kline": kline})
    instrument = Instrument("bybit", "linear_perpetual", "BTCUSDT", "BTCUSDT", "LinearPerpetual", quote_currency="USDT", api_engine="linear")
    candles = source.fetch_daily_candles(instrument, 3, datetime(2026, 1, 3, 12, tzinfo=UTC))
    assert len(candles) == 4
    kline_calls = [params for url, params in source.http.calls if url.endswith("/v5/market/kline")]
    assert len(kline_calls) == 2
    assert kline_calls[1]["end"] < kline_calls[0]["end"]
    assert all("cursor" not in params and "start" not in params for params in kline_calls)


def test_hyperliquid_multi_dex_discovery_and_candles(tmp_path: Path) -> None:
    def handler(body: dict[str, Any]) -> Any:
        if body["type"] == "perpDexs":
            return [None, {"name": "xyz", "fullName": "XYZ"}]
        if body["type"] == "meta":
            return {"universe": [{"name": "xyz:XYZ100"}]} if body.get("dex") == "xyz" else {"universe": [{"name": "BTC"}, {"name": "OLD", "isDelisted": True}]}
        if body["type"] == "candleSnapshot":
            return [
                {"t": 1767225600000, "o": "100", "h": "110", "l": "90", "c": "105", "v": "12", "q": "1260"},
                {"t": 1767312000000, "o": "105", "h": "115", "l": "95", "c": "110", "v": "10"},
            ]
        raise AssertionError(body)

    source = HyperliquidSource(AppConfig(), tmp_path, no_cache=True)
    source.http = FakeHttp(post_handler=handler)
    instruments = source.discover()
    assert {(item.symbol, item.dex) for item in instruments} == {("BTC", "default"), ("xyz:XYZ100", "xyz")}
    btc = next(item for item in instruments if item.symbol == "BTC")
    candles = source.fetch_daily_candles(btc, 30, datetime(2026, 1, 2, 12, tzinfo=UTC))
    assert [item.is_closed for item in candles] == [True, False]
    assert candles[1].turnover == 1100
    assert candles[1].turnover_currency == "USDC"
