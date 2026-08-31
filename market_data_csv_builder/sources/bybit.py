from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..cancellation import CancellationRequested, CancellationToken
from ..config import AppConfig
from ..http import JsonHttpClient
from ..models import Candle, Instrument
from .base import select_latest_bars


class BybitSource:
    name = "bybit"

    def __init__(
        self,
        config: AppConfig,
        root: Path,
        *,
        refresh_cache: bool = False,
        no_cache: bool = False,
        cancellation_token: CancellationToken | None = None,
    ) -> None:
        self.config = config.bybit
        self.base_urls = [
            config.bybit.base_url.rstrip("/"),
            *(value.rstrip("/") for value in config.bybit.fallback_base_urls),
        ]
        self.max_workers = config.bybit.max_workers
        self.threshold = config.bybit.threshold_usd
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
        result: list[Instrument] = []
        if self.config.linear_perpetual_enabled or self.config.linear_futures_enabled:
            for item in self._instrument_rows("linear"):
                self.cancellation_token.raise_if_requested()
                if str(item.get("status")) != "Trading":
                    continue
                contract_type = str(item.get("contractType") or "")
                if contract_type == "LinearPerpetual" and not self.config.linear_perpetual_enabled:
                    continue
                if contract_type == "LinearFutures" and not self.config.linear_futures_enabled:
                    continue
                if contract_type not in {"LinearPerpetual", "LinearFutures"}:
                    continue
                symbol = str(item.get("symbol") or "").strip()
                if not symbol:
                    continue
                result.append(
                    Instrument(
                        source=self.name,
                        market="linear_perpetual" if contract_type == "LinearPerpetual" else "linear_futures",
                        symbol=symbol,
                        instrument_name=str(item.get("displayName") or symbol),
                        contract_type=contract_type,
                        quote_currency=str(item.get("quoteCoin") or ""),
                        source_symbol=symbol,
                        api_engine="linear",
                        api_market="linear",
                        metadata={"base_coin": item.get("baseCoin"), "settle_coin": item.get("settleCoin")},
                    )
                )
        if self.config.spot_enabled:
            for item in self._instrument_rows("spot"):
                self.cancellation_token.raise_if_requested()
                if str(item.get("status")) != "Trading":
                    continue
                symbol = str(item.get("symbol") or "").strip()
                if symbol:
                    result.append(
                        Instrument(
                            source=self.name,
                            market="spot",
                            symbol=symbol,
                            instrument_name=str(item.get("displayName") or symbol),
                            contract_type="Spot",
                            quote_currency=str(item.get("quoteCoin") or ""),
                            source_symbol=symbol,
                            api_engine="spot",
                            api_market="spot",
                        )
                    )
        unique = {item.key: item for item in result}
        return sorted(unique.values(), key=lambda item: (item.market, item.symbol))

    def fetch_daily_candles(
        self, instrument: Instrument, closed_bars: int, as_of: datetime
    ) -> list[Candle]:
        self.cancellation_token.raise_if_requested()
        as_of_utc = as_of.astimezone(UTC)
        end_ms = int(as_of_utc.timestamp() * 1000)
        rows: dict[int, list[Any]] = {}
        page_end = end_ms
        while len(rows) < closed_bars + 1:
            self.cancellation_token.raise_if_requested()
            payload = self._get_json(
                "/v5/market/kline",
                {
                    "category": instrument.api_engine,
                    "symbol": instrument.api_symbol,
                    "interval": "D",
                    "end": page_end,
                    "limit": self.config.page_size,
                },
            )
            result = _result(payload)
            batch = result.get("list", [])
            if not isinstance(batch, list) or not batch:
                break
            valid_timestamps: list[int] = []
            for row in batch:
                self.cancellation_token.raise_if_requested()
                if not isinstance(row, list) or len(row) < 7:
                    continue
                try:
                    timestamp = int(row[0])
                except (TypeError, ValueError):
                    continue
                rows[timestamp] = row
                valid_timestamps.append(timestamp)
            if not valid_timestamps:
                break
            earliest = min(valid_timestamps)
            if len(batch) < self.config.page_size:
                break
            next_end = earliest - 1
            if next_end >= page_end:
                break
            page_end = next_end

        candles: list[Candle] = []
        for timestamp_ms, row in rows.items():
            self.cancellation_token.raise_if_requested()
            timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
            open_price, high, low, close = (_to_float(row[index]) for index in range(1, 5))
            if min(open_price, high, low, close) <= 0:
                continue
            volume = max(_to_float(row[5]), 0.0)
            turnover = _optional_float(row[6])
            candles.append(
                Candle(
                    timestamp=timestamp,
                    open=open_price,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    turnover=turnover,
                    turnover_currency=instrument.quote_currency,
                    is_closed=timestamp + timedelta(days=1) <= as_of_utc,
                )
            )
        return select_latest_bars(candles, closed_bars)

    def _instrument_rows(self, category: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor = ""
        while True:
            self.cancellation_token.raise_if_requested()
            params: dict[str, Any] = {"category": category}
            if category != "spot":
                params.update({"limit": 1000, "cursor": cursor or None})
            payload = self._get_json(
                "/v5/market/instruments-info",
                params,
            )
            result = _result(payload)
            batch = result.get("list", [])
            if not isinstance(batch, list):
                raise ValueError("Bybit instruments result.list must be a list")
            rows.extend(item for item in batch if isinstance(item, dict))
            next_cursor = str(result.get("nextPageCursor") or "")
            if category == "spot" or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return rows

    def _get_json(self, path: str, params: dict[str, Any]) -> Any:
        errors: list[str] = []
        for base_url in dict.fromkeys(self.base_urls):
            self.cancellation_token.raise_if_requested()
            try:
                return self.http.get_json(f"{base_url}{path}", params)
            except CancellationRequested:
                raise
            except Exception as exc:
                errors.append(f"{base_url}: {exc}")
        raise ValueError(f"All Bybit mainnet endpoints failed: {'; '.join(errors)}")


def _result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Bybit response must be an object")
    if int(payload.get("retCode", 0)) != 0:
        raise ValueError(f"Bybit API error {payload.get('retCode')}: {payload.get('retMsg')}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError("Bybit response is missing result object")
    return result


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
