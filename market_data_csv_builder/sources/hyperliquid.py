from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..cancellation import CancellationRequested, CancellationToken
from ..config import AppConfig
from ..http import JsonHttpClient
from ..models import Candle, Instrument
from ..rate_limit import WeightedRateLimiter
from .base import select_latest_bars

logger = logging.getLogger(__name__)

HYPERLIQUID_REST_WEIGHT_PER_MINUTE = 1200
HYPERLIQUID_RATE_LIMIT_WINDOW_SECONDS = 60.0
INFO_WEIGHT_TWO = {
    "l2Book",
    "allMids",
    "clearinghouseState",
    "orderStatus",
    "spotClearinghouseState",
    "exchangeStatus",
}
INTERVAL_MILLISECONDS = {
    "1m": 60_000,
    "3m": 3 * 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "2h": 2 * 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "8h": 8 * 60 * 60_000,
    "12h": 12 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
    "3d": 3 * 24 * 60 * 60_000,
    "1w": 7 * 24 * 60 * 60_000,
    "1M": 30 * 24 * 60 * 60_000,
}


@dataclass(frozen=True)
class PerpDex:
    name: str
    full_name: str

    @property
    def label(self) -> str:
        return self.name or "default"


class HyperliquidSource:
    name = "hyperliquid"

    def __init__(
        self,
        config: AppConfig,
        root: Path,
        *,
        refresh_cache: bool = False,
        no_cache: bool = False,
        cancellation_token: CancellationToken | None = None,
    ) -> None:
        self.config = config.hyperliquid
        self.max_workers = config.hyperliquid.max_workers
        self.threshold = config.hyperliquid.threshold_usd
        self.cancellation_token = cancellation_token or CancellationToken()
        self.url = f"{self.config.base_url.rstrip('/')}{self.config.info_path}"
        weight_budget = int(
            HYPERLIQUID_REST_WEIGHT_PER_MINUTE
            * self.config.rate_limit_safety_fraction
        )
        self.rate_limiter = WeightedRateLimiter(
            weight_budget=weight_budget,
            window_seconds=HYPERLIQUID_RATE_LIMIT_WINDOW_SECONDS,
            default_cooldown_seconds=self.config.rate_limit_default_cooldown_seconds,
            cancellation_token=self.cancellation_token,
        )
        self.http = JsonHttpClient(
            cache=config.cache,
            http=config.http,
            root=root,
            refresh_cache=refresh_cache,
            no_cache=no_cache,
            cancellation_token=self.cancellation_token,
            rate_limiter=self.rate_limiter,
        )

    def discover(self) -> list[Instrument]:
        self.cancellation_token.raise_if_requested()
        instruments: list[Instrument] = []
        errors: list[str] = []
        for dex in self._target_dexes():
            self.cancellation_token.raise_if_requested()
            try:
                body: dict[str, Any] = {"type": "meta"}
                if dex.name:
                    body["dex"] = dex.name
                payload = self._post_info(body)
                instruments.extend(self._normalize_instruments(payload, dex))
            except CancellationRequested:
                raise
            except Exception as exc:
                errors.append(f"{dex.label}: {exc}")
                logger.warning("Hyperliquid DEX %s discovery failed: %s", dex.label, exc)
        if not instruments:
            detail = "; ".join(errors) or "no active perp instruments"
            raise ValueError(f"Hyperliquid discovery returned no instruments: {detail}")
        unique = {item.key: item for item in instruments}
        return sorted(unique.values(), key=lambda item: (item.dex, item.symbol))

    def fetch_daily_candles(
        self, instrument: Instrument, closed_bars: int, as_of: datetime
    ) -> list[Candle]:
        self.cancellation_token.raise_if_requested()
        as_of_utc = as_of.astimezone(UTC)
        start = as_of_utc - timedelta(days=max(120, math.ceil(closed_bars * 2.05) + 60))
        payload = self._post_info(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": instrument.api_symbol,
                    "interval": "1d",
                    "startTime": int(start.timestamp() * 1000),
                    "endTime": int(as_of_utc.timestamp() * 1000),
                },
            },
        )
        if not isinstance(payload, list):
            raise ValueError("Hyperliquid candleSnapshot response must be a list")
        by_timestamp: dict[datetime, Candle] = {}
        for row in payload:
            self.cancellation_token.raise_if_requested()
            if not isinstance(row, dict):
                continue
            timestamp = _timestamp(row.get("t"))
            if timestamp is None:
                continue
            open_price = _to_float(row.get("o"))
            high = _to_float(row.get("h"))
            low = _to_float(row.get("l"))
            close = _to_float(row.get("c"))
            if min(open_price, high, low, close) <= 0:
                continue
            volume = max(_to_float(row.get("v")), 0.0)
            turnover = _optional_float(row.get("q"))
            if turnover is None:
                turnover = close * volume
            by_timestamp[timestamp] = Candle(
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
        return select_latest_bars(list(by_timestamp.values()), closed_bars)

    def _target_dexes(self) -> list[PerpDex]:
        self.cancellation_token.raise_if_requested()
        if self.config.perp_dexes:
            dexes = [PerpDex("" if name.lower() == "default" else name, name) for name in self.config.perp_dexes]
        elif self.config.discover_all_perp_dexes:
            try:
                payload = self._post_info({"type": "perpDexs"})
                dexes = normalize_perp_dexes(payload)
            except CancellationRequested:
                raise
            except Exception as exc:
                logger.warning("Hyperliquid perp DEX list failed; using default DEX: %s", exc)
                dexes = [PerpDex("", "Hyperliquid")]
        else:
            dexes = [PerpDex("", "Hyperliquid")]
        excluded = {"" if value.lower() == "default" else value for value in self.config.exclude_perp_dexes}
        return [dex for dex in dexes if dex.name not in excluded]

    def rate_limit_metrics(self) -> dict[str, float | int]:
        return self.rate_limiter.metrics().as_dict()

    def _post_info(self, body: dict[str, Any]) -> Any:
        return self.http.post_json(
            self.url, body, estimated_weight=estimate_info_request_weight(body)
        )

    def _normalize_instruments(self, payload: Any, dex: PerpDex) -> list[Instrument]:
        if not isinstance(payload, dict) or not isinstance(payload.get("universe"), list):
            raise ValueError("Hyperliquid meta response is missing universe")
        result: list[Instrument] = []
        for row in payload["universe"]:
            self.cancellation_token.raise_if_requested()
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("name") or "").strip()
            if not symbol or "/" in symbol or symbol.startswith("@"):
                continue
            if row.get("isDelisted") and not self.config.include_delisted:
                continue
            display_name = symbol.split(":", 1)[-1]
            result.append(
                Instrument(
                    source=self.name,
                    market="perpetual",
                    symbol=symbol,
                    instrument_name=display_name,
                    contract_type="Perpetual",
                    dex=dex.label,
                    quote_currency="USDC",
                    source_symbol=symbol,
                    api_engine="perp",
                    api_market="perpetual",
                    metadata={"dex_full_name": dex.full_name, "max_leverage": row.get("maxLeverage")},
                )
            )
        return result


def normalize_perp_dexes(payload: Any) -> list[PerpDex]:
    if not isinstance(payload, list):
        raise ValueError("Hyperliquid perpDexs response must be a list")
    result: list[PerpDex] = []
    has_default = False
    for row in payload:
        if row is None:
            result.append(PerpDex("", "Hyperliquid"))
            has_default = True
        elif isinstance(row, dict):
            name = str(row.get("name") or "").strip()
            if not name:
                if not has_default:
                    result.append(PerpDex("", str(row.get("fullName") or "Hyperliquid")))
                    has_default = True
                continue
            result.append(PerpDex(name, str(row.get("fullName") or name)))
    if not has_default:
        result.insert(0, PerpDex("", "Hyperliquid"))
    return result


def estimate_info_request_weight(body: dict[str, Any]) -> int:
    request_type = str(body.get("type") or "")
    if request_type in INFO_WEIGHT_TWO:
        base_weight = 2
    elif request_type == "userRole":
        base_weight = 60
    else:
        base_weight = 20
    if request_type != "candleSnapshot":
        return base_weight
    requested_candles = _estimate_requested_candles(body.get("req"))
    return base_weight + math.ceil(requested_candles / 60)


def _estimate_requested_candles(raw_request: Any) -> int:
    if not isinstance(raw_request, dict):
        return 1
    interval_ms = INTERVAL_MILLISECONDS.get(str(raw_request.get("interval") or ""))
    if interval_ms is None:
        return 5000
    try:
        start = int(raw_request["startTime"])
        end = int(raw_request["endTime"])
    except (KeyError, TypeError, ValueError):
        return 5000
    if end < start:
        return 1
    return min(5000, (end - start) // interval_ms + 1)


def _timestamp(value: Any) -> datetime | None:
    try:
        raw = int(value)
        if raw < 10_000_000_000:
            raw *= 1000
        return datetime.fromtimestamp(raw / 1000, tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
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
