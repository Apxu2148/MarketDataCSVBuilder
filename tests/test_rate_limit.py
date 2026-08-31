from __future__ import annotations

import threading
import time
from pathlib import Path
from urllib.error import HTTPError

from market_data_csv_builder.cancellation import CancellationRequested, CancellationToken
from market_data_csv_builder.config import CacheConfig, HttpConfig
from market_data_csv_builder.http import JsonHttpClient
from market_data_csv_builder.rate_limit import WeightedRateLimiter
from market_data_csv_builder.sources.hyperliquid import estimate_info_request_weight


def _limiter(
    token: CancellationToken,
    *,
    budget: int = 100,
    window: float = 0.05,
    cooldown: float = 0.05,
) -> WeightedRateLimiter:
    return WeightedRateLimiter(
        weight_budget=budget,
        window_seconds=window,
        default_cooldown_seconds=cooldown,
        cancellation_token=token,
    )


def test_hyperliquid_info_and_candle_weights_are_estimated_conservatively() -> None:
    day_ms = 24 * 60 * 60 * 1000
    assert estimate_info_request_weight({"type": "meta"}) == 20
    assert estimate_info_request_weight({"type": "perpDexs"}) == 20
    assert estimate_info_request_weight({"type": "allMids"}) == 2
    assert estimate_info_request_weight({"type": "userRole"}) == 60
    assert estimate_info_request_weight(
        {
            "type": "candleSnapshot",
            "req": {
                "interval": "1d",
                "startTime": 0,
                "endTime": 120 * day_ms,
            },
        }
    ) == 23
    assert estimate_info_request_weight(
        {
            "type": "candleSnapshot",
            "req": {
                "interval": "1d",
                "startTime": 0,
                "endTime": 2520 * day_ms,
            },
        }
    ) == 63


def test_weighted_limiter_waits_for_rolling_budget_and_reports_metrics() -> None:
    limiter = _limiter(CancellationToken(), budget=5)
    limiter.acquire(4)
    started = time.monotonic()
    limiter.acquire(2)
    elapsed = time.monotonic() - started
    metrics = limiter.metrics()
    assert elapsed >= 0.04
    assert metrics.requests_sent == 2
    assert metrics.estimated_api_weight == 6
    assert metrics.rate_limit_wait_seconds >= 0.04
    assert metrics.waiting_threads == 0


def test_weighted_limiter_paces_requests_instead_of_releasing_a_burst() -> None:
    limiter = _limiter(CancellationToken(), budget=10, window=0.1)
    limiter.acquire(1)
    started = time.monotonic()
    limiter.acquire(1)
    elapsed = time.monotonic() - started
    assert elapsed >= 0.008


def test_global_429_cooldown_delays_all_new_requests() -> None:
    limiter = _limiter(CancellationToken())
    limiter.record_429(0.05)
    started = time.monotonic()
    limiter.acquire(1)
    elapsed = time.monotonic() - started
    metrics = limiter.metrics()
    assert elapsed >= 0.04
    assert metrics.http_429_count == 1
    assert metrics.rate_limit_wait_seconds >= 0.04


def test_cancellation_interrupts_rate_limit_wait() -> None:
    token = CancellationToken()
    limiter = _limiter(token, budget=1, window=60)
    limiter.acquire(1)
    outcomes: list[str] = []

    def blocked_acquire() -> None:
        try:
            limiter.acquire(1)
        except CancellationRequested:
            outcomes.append("cancelled")

    worker = threading.Thread(target=blocked_acquire)
    worker.start()
    deadline = time.monotonic() + 1
    while limiter.metrics().waiting_threads != 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    token.request()
    worker.join(timeout=1)
    assert outcomes == ["cancelled"]
    assert not worker.is_alive()


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    @staticmethod
    def read() -> bytes:
        return b'{"ok":true}'


def test_http_429_uses_global_limiter_and_retry_after(
    monkeypatch, tmp_path: Path
) -> None:
    token = CancellationToken()
    limiter = _limiter(token, budget=1000, window=1, cooldown=10)
    attempts = 0

    def fake_urlopen(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise HTTPError(
                "https://example.invalid/info",
                429,
                "Too Many Requests",
                {"Retry-After": "0.05"},
                None,
            )
        return _Response()

    monkeypatch.setattr("market_data_csv_builder.http.urlopen", fake_urlopen)
    client = JsonHttpClient(
        cache=CacheConfig(enabled=False),
        http=HttpConfig(
            timeout_seconds=1,
            max_retries=2,
            backoff_seconds=0,
            max_backoff_seconds=0,
        ),
        root=tmp_path,
        no_cache=True,
        cancellation_token=token,
        rate_limiter=limiter,
    )
    started = time.monotonic()
    assert client.post_json(
        "https://example.invalid/info",
        {"type": "candleSnapshot"},
        estimated_weight=23,
    ) == {"ok": True}
    assert time.monotonic() - started >= 0.04
    metrics = limiter.metrics()
    assert attempts == 2
    assert metrics.requests_sent == 2
    assert metrics.estimated_api_weight == 46
    assert metrics.http_429_count == 1
