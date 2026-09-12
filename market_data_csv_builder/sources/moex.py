from __future__ import annotations

import logging
import math
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from ..cancellation import CancellationToken
from ..config import AppConfig
from ..http import HttpError, JsonHttpClient
from ..models import Candle, Instrument
from .base import select_latest_bars

logger = logging.getLogger(__name__)
MOSCOW = ZoneInfo("Europe/Moscow")

# Advisory normalization only. APX canonical economic mapping remains external to
# MarketDataCSVBuilder (AP-032). Source-native ASSETCODE is exported separately.
MOEX_UNDERLYING_ALIASES = {
    "GAZR": "GAZP",
    "SBRF": "SBER",
    "MIX": "IMOEX",
}


class MoexSource:
    name = "moex"

    def __init__(
        self,
        config: AppConfig,
        root: Path,
        *,
        refresh_cache: bool = False,
        no_cache: bool = False,
        cancellation_token: CancellationToken | None = None,
    ) -> None:
        self.config = config.moex
        self.max_workers = config.moex.max_workers
        self.threshold = config.moex.threshold_rub
        self.cancellation_token = cancellation_token or CancellationToken()
        self.http = JsonHttpClient(
            cache=config.cache,
            http=config.http,
            root=root,
            refresh_cache=refresh_cache,
            no_cache=no_cache,
            cancellation_token=self.cancellation_token,
        )

    def discover(self) -> list[Instrument]:
        self.cancellation_token.raise_if_requested()
        rows: list[tuple[str, str, dict[str, Any]]] = []
        if self.config.shares_enabled or self.config.funds_enabled:
            rows.extend(("stock", "shares", row) for row in self._fetch_securities("stock", "shares"))
        if self.config.bonds_enabled:
            rows.extend(("stock", "bonds", row) for row in self._fetch_securities("stock", "bonds"))
        if self.config.term_futures_enabled or self.config.perpetual_futures_enabled:
            rows.extend(("futures", "forts", row) for row in self._fetch_securities("futures", "forts"))

        selected: dict[tuple[str, str], Instrument] = {}
        for engine, api_market, row in rows:
            self.cancellation_token.raise_if_requested()
            symbol = _text(row, "SECID")
            if not symbol or (_text(row, "STATUS") and _text(row, "STATUS").upper() not in {"A", "ACTIVE"}):
                continue
            name = _first_text(row, "SHORTNAME", "SECNAME", "LATNAME") or symbol
            type_text = " ".join(
                _text(row, key) for key in ("TYPE_NAME", "SECTYPE_NAME", "SECTYPE", "TYPE", "GROUP")
            ).strip()
            if api_market == "shares":
                is_fund = _is_fund(symbol, name, type_text)
                if is_fund and not self.config.funds_enabled:
                    continue
                if not is_fund and not self.config.shares_enabled:
                    continue
                market = "funds" if is_fund else "shares"
                contract_type = "Fund" if is_fund else "Share"
            elif api_market == "bonds":
                market, contract_type = "bonds", "Bond"
            else:
                perpetual = self._is_perpetual(symbol, name, type_text)
                if perpetual and not self.config.perpetual_futures_enabled:
                    continue
                if not perpetual and not self.config.term_futures_enabled:
                    continue
                market = "futures"
                contract_type = "PerpetualFuture" if perpetual else "TermFuture"
            lot_size = max(_to_float(row.get("LOTSIZE"), 1.0), 1.0)
            source_underlying = symbol if market != "futures" else _text(row, "ASSETCODE")
            underlying = _normalized_underlying(source_underlying, symbol, market)
            contract_expiry = (
                _iso_date_text(row.get("LASTTRADEDATE"))
                if contract_type == "TermFuture"
                else ""
            )
            instrument = Instrument(
                source=self.name,
                market=market,
                symbol=symbol,
                instrument_name=name,
                contract_type=contract_type,
                quote_currency="RUB",
                api_engine=engine,
                api_market=api_market,
                lot_size=lot_size,
                metadata={
                    "type_name": type_text,
                    "board_id": _text(row, "PRIMARY_BOARDID") or _text(row, "BOARDID"),
                    "source_underlying_symbol": source_underlying,
                    "underlying_symbol": underlying,
                    "contract_expiry": contract_expiry,
                },
            )
            selected.setdefault((market, symbol), instrument)
        return sorted(selected.values(), key=lambda item: (item.market, item.symbol))

    def fetch_daily_candles(
        self, instrument: Instrument, closed_bars: int, as_of: datetime
    ) -> list[Candle]:
        self.cancellation_token.raise_if_requested()
        local_as_of = as_of.astimezone(MOSCOW)
        calendar_days = max(120, math.ceil(closed_bars * 2.05) + 60)
        from_date = local_as_of.date() - timedelta(days=calendar_days)
        till_date = local_as_of.date()
        rows: list[dict[str, Any]] = []
        start = 0
        while True:
            self.cancellation_token.raise_if_requested()
            url = (
                f"{self.config.base_url.rstrip('/')}/engines/{instrument.api_engine}/markets/"
                f"{instrument.api_market}/securities/{quote(instrument.api_symbol, safe='')}/candles.json"
            )
            payload = self.http.get_json(
                url,
                {
                    "from": from_date.isoformat(),
                    "till": till_date.isoformat(),
                    "interval": 24,
                    "start": start,
                    "limit": self.config.page_size,
                    "lang": self.config.language,
                    "iss.meta": "off",
                    "iss.only": "candles,candles.cursor",
                    "candles.columns": "begin,open,high,low,close,volume,value",
                    "candles.cursor.columns": "INDEX,TOTAL,PAGESIZE",
                },
            )
            batch = _table(payload, "candles")
            rows.extend(batch)
            if not _has_more(payload, "candles", start, len(batch), self.config.page_size):
                break
            start += len(batch)

        candle_dates = {
            parsed
            for row in rows
            if (parsed := _parse_date(row.get("begin"))) is not None
        }
        historical_turnover: dict[date, float] = {}
        current_turnover: float | None = None
        is_forts = instrument.api_engine == "futures" and instrument.api_market == "forts"
        if is_forts:
            closed_dates = [value for value in candle_dates if value < local_as_of.date()]
            if closed_dates:
                historical_turnover = self._fetch_forts_historical_turnover(
                    instrument, min(closed_dates), max(closed_dates)
                )
            if (
                local_as_of.date() == datetime.now(MOSCOW).date()
                and local_as_of.date() in candle_dates
            ):
                current_turnover = self._fetch_forts_current_turnover(instrument)

        by_timestamp: dict[datetime, Candle] = {}
        for row in rows:
            self.cancellation_token.raise_if_requested()
            candle_date = _parse_date(row.get("begin"))
            if candle_date is None:
                continue
            open_price = _to_float(row.get("open"))
            high = _to_float(row.get("high"))
            low = _to_float(row.get("low"))
            close = _to_float(row.get("close"))
            if min(open_price, high, low, close) <= 0:
                continue
            volume = max(_to_float(row.get("volume")), 0.0)
            is_closed = candle_date < local_as_of.date()
            if is_forts:
                turnover = historical_turnover.get(candle_date) if is_closed else current_turnover
            else:
                value = _optional_float(row.get("value"))
                turnover = value if value is not None and value >= 0 else close * volume * instrument.lot_size
            timestamp = datetime.combine(candle_date, time.min, tzinfo=MOSCOW)
            by_timestamp[timestamp] = Candle(
                timestamp=timestamp,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
                turnover=turnover,
                turnover_currency="RUB",
                is_closed=is_closed,
            )
        return select_latest_bars(list(by_timestamp.values()), closed_bars)

    def _fetch_forts_historical_turnover(
        self, instrument: Instrument, from_date: date, till_date: date
    ) -> dict[date, float]:
        rows: list[dict[str, Any]] = []
        start = 0
        while True:
            self.cancellation_token.raise_if_requested()
            payload = self.http.get_json(
                f"{self.config.base_url.rstrip('/')}/history/engines/futures/markets/forts/"
                f"securities/{quote(instrument.api_symbol, safe='')}.json",
                {
                    "from": from_date.isoformat(),
                    "till": till_date.isoformat(),
                    "start": start,
                    "limit": self.config.page_size,
                    "lang": self.config.language,
                    "iss.meta": "off",
                    "iss.only": "history,history.cursor",
                    "history.columns": "TRADEDATE,SECID,BOARDID,VALUE",
                    "history.cursor.columns": "INDEX,TOTAL,PAGESIZE",
                },
            )
            batch = _table(payload, "history")
            rows.extend(batch)
            if not _has_more(payload, "history", start, len(batch), self.config.page_size):
                break
            start += len(batch)
        return _turnover_by_date(rows, instrument.api_symbol)

    def _fetch_forts_current_turnover(self, instrument: Instrument) -> float | None:
        try:
            payload = self.http.get_json(
                f"{self.config.base_url.rstrip('/')}/engines/futures/markets/forts/"
                f"securities/{quote(instrument.api_symbol, safe='')}.json",
                {
                    "lang": self.config.language,
                    "iss.meta": "off",
                    "iss.only": "marketdata",
                    "marketdata.columns": "SECID,BOARDID,VALTODAY",
                },
            )
            rows = _table(payload, "marketdata")
        except (HttpError, ValueError) as exc:
            logger.warning("MOEX FORTS VALTODAY is unavailable for %s: %s", instrument.symbol, exc)
            return None
        values = [
            value
            for row in rows
            if _matches_symbol(row, instrument.api_symbol)
            and (value := _nonnegative_float(row.get("VALTODAY"))) is not None
        ]
        return sum(values) if values else None

    def _fetch_securities(self, engine: str, market: str) -> list[dict[str, Any]]:
        self.cancellation_token.raise_if_requested()
        payload = self.http.get_json(
            f"{self.config.base_url.rstrip('/')}/engines/{engine}/markets/{market}/securities.json",
            {
                "lang": self.config.language,
                "iss.meta": "off",
                "iss.only": "securities",
                "securities.columns": (
                    "SECID,SHORTNAME,SECNAME,LATNAME,LOTSIZE,BOARDID,PRIMARY_BOARDID,"
                    "SECTYPE,SECTYPE_NAME,TYPE,TYPE_NAME,GROUP,STATUS,ASSETCODE,LASTTRADEDATE"
                ),
            },
        )
        return _table(payload, "securities")

    def _is_perpetual(self, symbol: str, name: str, type_text: str) -> bool:
        text = f"{name} {type_text}".lower()
        if any(marker.lower() in text for marker in self.config.perpetual_name_markers):
            return True
        if "-" in symbol:
            return False
        return any(symbol.upper().endswith(suffix.upper()) for suffix in self.config.perpetual_symbol_suffixes)


def _table(payload: Any, name: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get(name), dict):
        raise ValueError(f"MOEX response is missing table {name!r}")
    table = payload[name]
    columns, data = table.get("columns", []), table.get("data", [])
    if not isinstance(columns, list) or not isinstance(data, list):
        raise ValueError(f"MOEX table {name!r} has invalid shape")
    return [dict(zip(columns, row)) for row in data if isinstance(row, list)]


def _has_more(payload: Any, name: str, start: int, batch_size: int, configured_page_size: int) -> bool:
    if batch_size <= 0:
        return False
    cursor_name = f"{name}.cursor"
    if isinstance(payload, dict) and isinstance(payload.get(cursor_name), dict):
        cursor = _table(payload, cursor_name)
        if cursor:
            total = int(_to_float(cursor[0].get("TOTAL"), 0.0))
            return start + batch_size < total
    return batch_size >= configured_page_size


def _text(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    return "" if value is None else str(value).strip()


def _first_text(row: dict[str, Any], *keys: str) -> str:
    return next((_text(row, key) for key in keys if _text(row, key)), "")


def _normalized_underlying(source_underlying: str, symbol: str, market: str) -> str:
    if market != "futures":
        return symbol
    key = source_underlying.upper()
    return MOEX_UNDERLYING_ALIASES.get(key, source_underlying)


def _iso_date_text(value: Any) -> str:
    parsed = _parse_date(value)
    return parsed.isoformat() if parsed is not None else ""


def _is_fund(symbol: str, name: str, type_text: str) -> bool:
    text = f"{symbol} {name} {type_text}".lower()
    return any(marker in text for marker in ("fund", "etf", "reit", "mutual", "пиф", "бпиф", "фонд", "пай"))


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return default if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return default


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _nonnegative_float(value: Any) -> float | None:
    parsed = _optional_float(value)
    if parsed is None or not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


def _matches_symbol(row: dict[str, Any], symbol: str) -> bool:
    row_symbol = _text(row, "SECID")
    return not row_symbol or row_symbol.casefold() == symbol.casefold()


def _turnover_by_date(rows: list[dict[str, Any]], symbol: str) -> dict[date, float]:
    result: dict[date, float] = {}
    for row in rows:
        trade_date = _parse_date(row.get("TRADEDATE"))
        value = _nonnegative_float(row.get("VALUE"))
        if trade_date is None or value is None or not _matches_symbol(row, symbol):
            continue
        result[trade_date] = result.get(trade_date, 0.0) + value
    return result
