from __future__ import annotations

import hashlib
import http.client
import json
import logging
import random
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .cancellation import CancellationRequested, CancellationToken
from .config import CacheConfig, HttpConfig
from .rate_limit import WeightedRateLimiter

logger = logging.getLogger(__name__)


class HttpError(RuntimeError):
    pass


class JsonHttpClient:
    def __init__(
        self,
        *,
        cache: CacheConfig,
        http: HttpConfig,
        root: Path,
        refresh_cache: bool = False,
        no_cache: bool = False,
        user_agent: str = "MarketDataCSVBuilder/1.0",
        cancellation_token: CancellationToken | None = None,
        rate_limiter: WeightedRateLimiter | None = None,
    ) -> None:
        self.http = http
        self.refresh_cache = refresh_cache
        self.cache_enabled = cache.enabled and not no_cache
        self.cache_ttl_seconds = cache.ttl_hours * 3600
        self.cache_dir = (root / cache.directory).resolve()
        self.user_agent = user_agent
        self.cancellation_token = cancellation_token or CancellationToken()
        self.rate_limiter = rate_limiter
        if self.cache_enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get_json(self, url: str, params: dict[str, Any]) -> Any:
        query = urlencode([(key, value) for key, value in params.items() if value is not None])
        target = f"{url}?{query}" if query else url
        return self._request(target, None, f"GET\n{target}")

    def post_json(
        self, url: str, body: dict[str, Any], *, estimated_weight: int = 1
    ) -> Any:
        raw_body = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        key = f"POST\n{url}\n{raw_body.decode('utf-8')}"
        return self._request(url, raw_body, key, estimated_weight=estimated_weight)

    def _request(
        self,
        url: str,
        body: bytes | None,
        cache_key: str,
        *,
        estimated_weight: int = 1,
    ) -> Any:
        self.cancellation_token.raise_if_requested()
        cache_path = self._cache_path(cache_key)
        if self.cache_enabled and not self.refresh_cache:
            cached = self._read_cache(cache_path)
            if cached is not None:
                self.cancellation_token.raise_if_requested()
                return cached
        last_error: Exception | None = None
        for attempt in range(1, self.http.max_retries + 1):
            self.cancellation_token.raise_if_requested()
            try:
                if self.rate_limiter is not None:
                    self.rate_limiter.acquire(estimated_weight)
                headers = {
                    "User-Agent": self.user_agent,
                    "Accept": "application/json",
                    "Connection": "close",
                }
                if body is not None:
                    headers["Content-Type"] = "application/json"
                request = Request(url, data=body, headers=headers)
                with urlopen(request, timeout=self.http.timeout_seconds) as response:
                    raw = response.read().decode("utf-8")
                self.cancellation_token.raise_if_requested()
                payload = json.loads(raw)
                if self.cache_enabled:
                    self._write_cache(cache_path, payload)
                return payload
            except CancellationRequested:
                raise
            except HTTPError as exc:
                last_error = exc
                if exc.code == 429 and self.rate_limiter is not None:
                    self.rate_limiter.record_429(_retry_after_seconds(exc))
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt == self.http.max_retries:
                    break
            except (URLError, TimeoutError, http.client.HTTPException, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt == self.http.max_retries:
                    break
            self.cancellation_token.raise_if_requested()
            delay = min(
                self.http.max_backoff_seconds,
                self.http.backoff_seconds * (2 ** (attempt - 1)),
            )
            delay += random.random() * min(0.25, delay / 4 if delay else 0)
            logger.warning("HTTP request failed (%s/%s): %s", attempt, self.http.max_retries, last_error)
            if delay and self.cancellation_token.wait(delay):
                raise CancellationRequested("Cancellation requested during HTTP backoff")
        raise HttpError(f"Request failed after {self.http.max_retries} attempts: {url}: {last_error}") from last_error

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.json"

    def _read_cache(self, path: Path) -> Any | None:
        if not path.exists():
            return None
        if self.cache_ttl_seconds <= 0 or time.time() - path.stat().st_mtime > self.cache_ttl_seconds:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Ignoring invalid cache entry: %s", path)
            return None

    @staticmethod
    def _write_cache(path: Path, payload: Any) -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
        temporary.replace(path)


def _retry_after_seconds(exc: HTTPError) -> float | None:
    if exc.headers is None:
        return None
    raw = exc.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(raw))
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
