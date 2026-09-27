"""Public Hyperliquid INTRADAY transport and native/HIP-3 metadata."""
from __future__ import annotations

import http.client
import json
import logging
import math
import random
import threading
from urllib.error import HTTPError, URLError
from urllib.request import Request, ProxyHandler, build_opener, urlopen

from ..http import _retry_after_seconds
from ..rate_limit import WeightedRateLimiter
from ..sources.hyperliquid import estimate_info_request_weight, normalize_perp_dexes
from .provider import CandleStore, RequestFailure, TIMEFRAMES, now_iso

logger = logging.getLogger(__name__)
INTERVALS = {"D": "1d", "240": "4h", "60": "1h", "15": "15m", "5": "5m"}


class HyperliquidCandleStore(CandleStore):
    cache_namespace = "intraday_hyperliquid/v1/hyperliquid/perpetual"
    nullable_turnover = True
    page_limit = 5000


class HyperliquidPublicClient:
    def __init__(self, config, token):
        self.config = config.intraday_hyperliquid
        self.token = token
        self.limiter = WeightedRateLimiter(weight_budget=int(1200 * self.config.rate_limit_safety_fraction),
            window_seconds=60, default_cooldown_seconds=60, cancellation_token=token)
        self.lock = threading.Lock()
        self.retries = 0
        self.dexes = []
        self.metadata = {}
        self.selected = None
        self.book_errors = []
        self.spot = None

    def metrics(self):
        result = self.limiter.metrics().as_dict()
        with self.lock:
            result.update(retry_count=self.retries)
        result["rate_limit_events"] = result["http_429_count"]
        return result

    def open(self, request):
        if self.config.use_system_proxy:
            return urlopen(request, timeout=self.config.request_timeout)
        return build_opener(ProxyHandler({})).open(request, timeout=self.config.request_timeout)

    def post(self, body):
        if body.get("type") not in {"perpDexs", "spotMeta", "spotMetaAndAssetCtxs", "metaAndAssetCtxs", "candleSnapshot", "l2Book"}:
            raise ValueError("Only public market reads are supported")
        request = Request(self.config.base_url.rstrip("/") + "/info",
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "User-Agent": "MarketDataCSVBuilder/3"})
        for attempt in range(self.config.max_retries + 1):
            self.token.raise_if_requested()
            self.limiter.acquire(estimate_info_request_weight(body))
            retryable = True
            try:
                with self.open(request) as response:
                    result = json.loads(response.read().decode())
                self.token.raise_if_requested()
                return result
            except HTTPError as exc:
                error = exc
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if exc.code == 429:
                    self.limiter.record_429(_retry_after_seconds(exc))
            except (URLError, TimeoutError, http.client.HTTPException, json.JSONDecodeError) as exc:
                error = exc
            if not retryable or attempt == self.config.max_retries:
                raise RequestFailure(str(error), attempt) from error
            with self.lock:
                self.retries += 1
            delay = min(self.config.backoff_max, self.config.backoff_base * 2 ** attempt) + random.uniform(0, self.config.jitter)
            logger.warning("Hyperliquid retry %s: %s", attempt + 1, error)
            self.limiter.cooldown(delay)

    def contexts(self, dex):
        payload = self.post(dict(type="metaAndAssetCtxs", dex=dex))
        if (not isinstance(payload, list) or len(payload) != 2 or
            not isinstance(payload[0], dict) or not isinstance(payload[0].get("universe"), list) or
            not isinstance(payload[1], list) or len(payload[0]["universe"]) != len(payload[1])):
            raise ValueError(f"Malformed metadata/contexts for DEX {dex!r}")
        return payload

    def instruments(self):
        # Discovery failure must not silently remove an entire DEX from the snapshot.
        self.dexes = list(dict.fromkeys(d.name for d in normalize_perp_dexes(self.post({"type": "perpDexs"}))))
        spot = self.post({"type": "spotMeta"})
        self.spot = spot
        tokens = {row["index"]: row["name"] for row in spot["tokens"]}
        items = {}
        for dex in self.dexes:
            meta, _ = self.contexts(dex)
            collateral = meta.get("collateralToken")
            currency = tokens.get(collateral)
            if currency is None:
                raise ValueError(f"Unknown collateral token {collateral!r} for {dex!r}")
            for row in meta["universe"]:
                symbol = row["name"]
                if dex and not symbol.startswith(dex + ":"):
                    raise ValueError("HIP-3 native symbol lacks DEX prefix")
                if row.get("isDelisted"):
                    continue
                if symbol in items:
                    raise ValueError("Duplicate native instrument identity")
                decimals = row["szDecimals"]
                if type(decimals) is not int or not 0 <= decimals <= 6:
                    raise ValueError("Invalid szDecimals")
                items[symbol] = dict(symbol=symbol, status="Trading", contractType="Perpetual",
                    quoteCoin=currency, settleCoin=currency, baseCoin=symbol.split(":")[-1],
                    dex=dex, collateral_token=collateral, sz_decimals=decimals,
                    max_price_decimals=6-decimals, max_price_significant_figures=5,
                    lotSizeFilter={"qtyStep": format(10 ** -decimals, f".{decimals}f")},
                    fundingInterval=60, displayName=symbol.split(":")[-1])
        self.metadata = items
        return [items[key] for key in sorted(items)]

    def tickers(self):
        tickers = {}
        # USDC is the explicit USD-equivalent numeraire, not relabeled USDT.
        rates = {0: 1.0}
        if any(item["quoteCoin"] != "USDC" for item in self.metadata.values()):
            spot_meta, spot_ctxs = self.post({"type": "spotMetaAndAssetCtxs"})
            usdc = next(t["index"] for t in spot_meta["tokens"] if t["name"] == "USDC")
            rates = {usdc: 1.0}
            if len(spot_meta["universe"]) != len(spot_ctxs):
                raise ValueError("Malformed collateral FX contexts")
            for pair, ctx in zip(spot_meta["universe"], spot_ctxs):
                base, quote = pair["tokens"]
                raw = ctx.get("midPx")
                if raw is not None and quote == usdc:
                    rate = float(raw)
                    if math.isfinite(rate) and rate > 0:
                        rates[base] = rate
        for dex in self.dexes:
            meta, contexts = self.contexts(dex)
            for item, context in zip(meta["universe"], contexts):
                symbol = item["name"]
                if symbol not in self.metadata:
                    continue
                tickers[symbol] = dict(turnover24h=context.get("dayNtlVlm"),
                    volume24h=context.get("dayBaseVlm"), markPrice=context.get("markPx"),
                    indexPrice=context.get("oraclePx"), fundingRate=context.get("funding"),
                    openInterest=context.get("openInterest"), mid_price=context.get("midPx"),
                    context_fetched_at_utc=now_iso())
                rate = rates.get(self.metadata[symbol]["collateral_token"])
                if rate is None:
                    raise ValueError(f"No observable USDC conversion for collateral of {symbol}")
                tickers[symbol]["collateral_usd_rate"] = rate
                tickers[symbol]["turnover24h_usd"] = float(context["dayNtlVlm"]) * rate
        # Book top is an independent public snapshot; impactPxs is NOT best bid/ask.
        if self.selected is not None:
            for symbol in self.selected:
                if symbol not in tickers:
                    continue
                try:
                    book = self.post(dict(type="l2Book", coin=symbol))
                    if book.get("coin") != symbol or len(book["levels"]) != 2:
                        raise ValueError("Malformed book")
                    for side, prefix in zip(book["levels"], ("bid", "ask")):
                        if side:
                            tickers[symbol][prefix + "1Price"] = side[0]["px"]
                            tickers[symbol][prefix + "1Size"] = side[0]["sz"]
                    tickers[symbol]["book_fetched_at_utc"] = now_iso()
                    tickers[symbol]["book_time_ms"] = book.get("time")
                except (RequestFailure, ValueError, KeyError, TypeError) as exc:
                    self.book_errors.append(dict(symbol=symbol, reason=str(exc)))
                    logger.warning("Hyperliquid book %s unavailable: %s", symbol, exc)
        return tickers

    def get(self, path, **params):
        if path != "kline":
            raise ValueError("Unsupported candle operation")
        duration = next(ms for interval, ms in TIMEFRAMES.values() if interval == params["interval"])
        # Explicit window boundary keeps pagination correct even across empty ranges.
        first = max(params["start"], params["end"] // duration * duration - (params["limit"] - 1) * duration)
        interval = INTERVALS[params["interval"]]
        payload = self.post(dict(type="candleSnapshot", req=dict(coin=params["symbol"],
            interval=interval, startTime=first, endTime=params["end"])))
        if not isinstance(payload, list):
            raise ValueError("Malformed candle snapshot")
        rows = []
        for row in payload:
            if row.get("s") != params["symbol"] or row.get("i") != interval:
                raise ValueError("Candle identity mismatch")
            rows.append([row["t"], row["o"], row["h"], row["l"], row["c"], row["v"], row.get("q")])
        return {"list": rows, "next_end": first - 1}


def eligible_hyperliquid(instruments, tickers, threshold):
    selected = []
    for item in instruments:
        ticker = tickers.get(item["symbol"])
        if ticker is None:
            raise ValueError(f"Missing discovery context: {item['symbol']}")
        turnover = float(ticker["turnover24h_usd"])
        if not math.isfinite(turnover) or turnover < 0:
            raise ValueError("Invalid dayNtlVlm")
        if turnover >= threshold:
            selected.append(item)
    return sorted(selected, key=lambda row: row["symbol"])
