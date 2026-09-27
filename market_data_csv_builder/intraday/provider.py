from __future__ import annotations

import http.client
import json
import logging
import math
import random
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen, build_opener, ProxyHandler

import pandas as pd

from ..cancellation import CancellationRequested, CancellationToken
from ..http import _retry_after_seconds
from ..rate_limit import WeightedRateLimiter
from ..utils import safe_path_component

TIMEFRAMES = {"1D": ("D", 86400000), "4H": ("240", 14400000),
              "1H": ("60", 3600000), "15m": ("15", 900000), "5m": ("5", 300000)}
# _HORIZONS.long=(1000,100): candidates expire inside the trailing 1000 rows.
# All other shared features use <=100 rows. ATR seed influence decays by (13/14)^999.
WARMUP_BARS = 999
RAW_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume", "turnover"]
logger = logging.getLogger(__name__)


def now_iso():
    return datetime.now(UTC).isoformat()


class RequestFailure(RuntimeError):
    def __init__(self, message, retries=0):
        super().__init__(message)
        self.retries = retries


class BybitPublicClient:
    """Uncached public V5 reads; one limiter and retry budget for all workers."""
    def __init__(self, config, token):
        self.config = config.intraday
        self.urls = list(dict.fromkeys([config.bybit.base_url, *config.bybit.fallback_base_urls]))
        self.token = token
        self.limiter = WeightedRateLimiter(weight_budget=1,
            window_seconds=1 / self.config.requests_per_second,
            default_cooldown_seconds=1, cancellation_token=token)
        self.lock = threading.Lock()
        self.counters = dict(retry_count=0, http_429_count=0, bybit_10006_count=0)

    def open(self, request):
        if self.config.use_system_proxy:
            return urlopen(request, timeout=self.config.request_timeout)
        # Per-request direct opener, never modify global/system proxy configuration.
        return build_opener(ProxyHandler({})).open(request, timeout=self.config.request_timeout)

    def metrics(self):
        result = self.limiter.metrics().as_dict()
        with self.lock:
            result.update(self.counters)
        result["rate_limit_events"] = result["http_429_count"] + result["bybit_10006_count"]
        return result

    def increment(self, name):
        with self.lock:
            self.counters[name] += 1

    def get(self, path, **params):
        if path not in {"instruments-info", "tickers", "kline"}:
            raise ValueError("Only public market endpoints are supported")
        error = None
        host_index = 0
        for attempt in range(self.config.max_retries + 1):
            self.token.raise_if_requested()
            self.limiter.acquire(1)
            url = self.urls[host_index].rstrip("/")
            url += "/v5/market/" + path + "?" + urlencode({"category": "linear", **params})
            retryable = True
            try:
                with self.open(Request(url, headers={"User-Agent": "MarketDataCSVBuilder/2", "Accept": "application/json"})) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    headers = response.headers
                self.token.raise_if_requested()
                if not isinstance(payload, dict) or "retCode" not in payload:
                    raise ValueError("Malformed Bybit envelope")
                code = int(payload["retCode"])
                if code == 10006 or headers.get("X-Bapi-Limit-Status") == "0":
                    try:
                        delay = max(0.1, float(headers.get("X-Bapi-Limit-Reset-Timestamp", 0)) / 1000 - time.time())
                    except (ValueError, TypeError):
                        delay = 1
                    self.limiter.cooldown(delay)
                if code:
                    if code == 10006:
                        self.increment("bybit_10006_count")
                    retryable = code in {10006, 10000, 10016}
                    raise RequestFailure(f"Bybit {code}: {payload.get('retMsg')}")
                result = payload.get("result")
                if not isinstance(result, dict) or not isinstance(result.get("list"), list):
                    raise ValueError("Malformed Bybit result.list")
                return result
            except HTTPError as exc:
                error = exc
                retryable = exc.code in {403, 429} or 500 <= exc.code < 600
                if exc.code == 429:
                    self.increment("http_429_count")
                    self.limiter.cooldown(_retry_after_seconds(exc) or 1)
                if exc.code == 403:
                    # Do not hammer a banned IP or cycle hosts indefinitely.
                    retryable = host_index + 1 < len(self.urls)
                    if retryable:
                        host_index += 1
            except (URLError, TimeoutError, http.client.HTTPException, json.JSONDecodeError, RequestFailure) as exc:
                error = exc
            if not retryable or attempt == self.config.max_retries:
                break
            self.increment("retry_count")
            delay = min(self.config.backoff_max, self.config.backoff_base * 2 ** attempt)
            delay += random.uniform(0, self.config.jitter)
            logger.warning("INTRADAY retry %s: %s; backoff %.2fs", attempt + 1, error, delay)
            self.limiter.cooldown(delay)
        raise RequestFailure(str(error), attempt) from error

    def instruments(self):
        rows, seen, cursor = {}, set(), ""
        while True:
            result = self.get("instruments-info", limit=1000, cursor=cursor)
            for row in result["list"]:
                if not isinstance(row, dict) or not row.get("symbol"):
                    raise ValueError("Malformed instrument metadata")
                rows[row["symbol"]] = row
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                return [rows[key] for key in sorted(rows)]
            if cursor in seen:
                raise ValueError("Instrument pagination cursor repeated")
            seen.add(cursor)

    def tickers(self):
        rows = self.get("tickers")["list"]
        if any(not isinstance(row, dict) or not row.get("symbol") for row in rows):
            raise ValueError("Malformed ticker")
        return {row["symbol"]: row for row in rows}


def eligible_instruments(instruments, tickers, threshold):
    selected = []
    for item in instruments:
        if (item.get("status"), item.get("contractType"), item.get("quoteCoin"), item.get("settleCoin")) != (
                "Trading", "LinearPerpetual", "USDT", "USDT"):
            continue
        ticker = tickers.get(item["symbol"])
        if ticker is None:
            raise ValueError(f"Missing discovery ticker: {item['symbol']}")
        turnover = float(ticker["turnover24h"])
        if not math.isfinite(turnover) or turnover < 0:
            raise ValueError(f"Invalid turnover24h: {item['symbol']}")
        if turnover >= threshold:
            selected.append(item)
    return sorted(selected, key=lambda row: row["symbol"])


class CandleStore:
    """Versioned, completed-only cache, independent of APX's HTTP cache."""
    cache_namespace = "intraday/v1/bybit/linear_perpetual"
    nullable_turnover = False
    page_limit = 1000

    def __init__(self, root: Path, client, token, enabled=True, refresh=False):
        self.directory = root / "data/cache" / self.cache_namespace
        self.client, self.token = client, token
        self.enabled, self.refresh = enabled, refresh

    def fetch(self, symbol, timeframe, closed_bars, previous=None):
        self.token.raise_if_requested()
        interval, duration = TIMEFRAMES[timeframe]
        cutoff = int(time.time() * 1000)
        boundary = cutoff // duration * duration
        count = closed_bars + WARMUP_BARS
        start = boundary - count * duration
        path = self.directory / safe_path_component(symbol) / (timeframe + ".json")
        cached = []
        if previous is not None:
            cached = previous.loc[previous.is_closed, RAW_COLUMNS].copy()
            cached["timestamp"] = cached.timestamp.map(lambda stamp: int(stamp.timestamp() * 1000))
            cached = cached.values.tolist()
        elif self.enabled and not self.refresh and path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved["symbol"] == symbol and saved["timeframe"] == timeframe:
                    cached = saved["rows"]
            except (ValueError, KeyError, TypeError, OSError):
                logger.warning("Ignoring invalid intraday cache %s", path)
        rows = {}
        try:
            for row in cached:
                ts = int(row[0])
                if ts in rows:
                    raise ValueError("Duplicate cached candle")
                if ts < boundary and ts >= start:
                    rows[ts] = row
            self._frame(rows, duration, cutoff)
        except (ValueError, TypeError, IndexError):
            rows = {}
        # Fetch every missing range, plus two closed candles and the current candle.
        missing = [stamp for stamp in range(start, boundary + duration, duration)
                   if stamp not in rows or stamp >= boundary - 2 * duration]
        ranges = []
        for stamp in missing:
            if ranges and stamp == ranges[-1][1] + duration:
                ranges[-1][1] = stamp
            else:
                ranges.append([stamp, stamp])
        for first, last in reversed(ranges):
            end = min(cutoff, last + duration - 1)
            while end >= first:
                self.token.raise_if_requested()
                response = self.client.get("kline", symbol=symbol, interval=interval,
                    start=first, end=end, limit=self.page_limit)
                batch = response["list"]
                if not batch and "next_end" not in response:
                    break
                stamps = []
                for row in batch:
                    if not isinstance(row, list) or len(row) < 7:
                        raise ValueError("Malformed kline row")
                    ts = int(row[0])
                    if ts < first or ts > end or ts % duration:
                        raise ValueError("Kline outside requested interval or grid")
                    if ts in stamps:
                        raise ValueError("Duplicate candle in API response")
                    rows[ts] = row[:7]
                    stamps.append(ts)
                next_end = response["next_end"] if "next_end" in response else min(stamps) - 1
                if next_end >= end:
                    raise ValueError("Kline pagination did not advance")
                end = next_end
                if len(batch) < self.page_limit and "next_end" not in response:
                    break
        frame = self._frame(rows, duration, cutoff)
        if frame.empty:
            raise ValueError("No candles returned")
        if self.enabled:
            self.token.raise_if_requested()
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"symbol": symbol, "timeframe": timeframe,
                       "rows": [[None if value is None or (isinstance(value, float) and math.isnan(value)) else value for value in rows[stamp]]
                                for stamp in sorted(rows) if stamp + duration <= cutoff]}
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")
            temporary.replace(path)
        return frame, now_iso()

    @classmethod
    def _frame(cls, rows, duration, cutoff):
        frame = pd.DataFrame([rows[key] for key in sorted(rows)], columns=RAW_COLUMNS)
        if frame.empty:
            return frame
        for name in RAW_COLUMNS:
            frame[name] = pd.to_numeric(frame[name], errors="raise")
        required = frame.drop(columns="turnover") if cls.nullable_turnover else frame
        if not required.map(math.isfinite).all().all() or not frame.turnover.dropna().map(math.isfinite).all():
            raise ValueError("Non-finite candle")
        invalid = (frame[["open", "high", "low", "close"]] <= 0).any(axis=1) | (frame[["volume", "turnover"]] < 0).any(axis=1)
        if invalid.any():
            raise ValueError(f"Invalid candle price/volume: {frame.loc[invalid].iloc[0].to_dict()}")
        if (frame.high < frame[["open", "close", "low"]].max(axis=1)).any() or (frame.low > frame[["open", "close", "high"]].min(axis=1)).any():
            raise ValueError("Inconsistent OHLC")
        if (frame.timestamp % duration != 0).any() or (frame.timestamp > cutoff).any():
            raise ValueError("Invalid candle timestamp")
        frame["is_closed"] = frame.timestamp + duration <= cutoff
        frame["provisional"] = ~frame.is_closed
        frame["timestamp"] = pd.to_datetime(frame.timestamp, unit="ms", utc=True)
        return frame
