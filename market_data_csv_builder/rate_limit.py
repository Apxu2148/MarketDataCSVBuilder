from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

from .cancellation import CancellationToken


@dataclass(frozen=True)
class RateLimitMetrics:
    rate_limit_wait_seconds: float
    http_429_count: int
    requests_sent: int
    estimated_api_weight: int
    waiting_threads: int

    def as_dict(self) -> dict[str, float | int]:
        return {
            "rate_limit_wait_seconds": round(self.rate_limit_wait_seconds, 3),
            "http_429_count": self.http_429_count,
            "requests_sent": self.requests_sent,
            "estimated_api_weight": self.estimated_api_weight,
            "waiting_threads": self.waiting_threads,
        }


class WeightedRateLimiter:
    """Thread-safe rolling-window weighted limiter with a shared 429 cooldown."""

    def __init__(
        self,
        *,
        weight_budget: int,
        window_seconds: float,
        default_cooldown_seconds: float,
        cancellation_token: CancellationToken,
    ) -> None:
        if weight_budget <= 0:
            raise ValueError("weight_budget must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if default_cooldown_seconds < 0:
            raise ValueError("default_cooldown_seconds cannot be negative")
        self.weight_budget = weight_budget
        self.window_seconds = window_seconds
        self.default_cooldown_seconds = default_cooldown_seconds
        self.cancellation_token = cancellation_token
        self._condition = threading.Condition()
        self._events: deque[tuple[float, int]] = deque()
        self._window_weight = 0
        self._cooldown_until = 0.0
        self._next_request_at = 0.0
        self._wait_seconds = 0.0
        self._http_429_count = 0
        self._requests_sent = 0
        self._estimated_api_weight = 0
        self._active_wait_started: dict[int, float] = {}

    def acquire(self, estimated_weight: int) -> None:
        if estimated_weight <= 0:
            raise ValueError("estimated_weight must be positive")
        if estimated_weight > self.weight_budget:
            raise ValueError(
                f"estimated_weight {estimated_weight} exceeds budget {self.weight_budget}"
            )
        wait_started = 0.0
        waiting = False
        try:
            with self._condition:
                while True:
                    self.cancellation_token.raise_if_requested()
                    now = time.monotonic()
                    self._discard_expired(now)
                    cooldown_wait = max(0.0, self._cooldown_until - now)
                    pacing_wait = max(0.0, self._next_request_at - now)
                    budget_available = self._window_weight + estimated_weight <= self.weight_budget
                    if cooldown_wait <= 0 and pacing_wait <= 0 and budget_available:
                        self._events.append((now, estimated_weight))
                        self._window_weight += estimated_weight
                        self._requests_sent += 1
                        self._estimated_api_weight += estimated_weight
                        self._next_request_at = now + (
                            self.window_seconds * estimated_weight / self.weight_budget
                        )
                        return
                    if not waiting:
                        waiting = True
                        wait_started = now
                        self._active_wait_started[threading.get_ident()] = now
                    budget_wait = 0.0
                    if not budget_available and self._events:
                        budget_wait = max(
                            0.0, self._events[0][0] + self.window_seconds - now
                        )
                    wait_for = max(cooldown_wait, pacing_wait, budget_wait)
                    self._condition.wait(timeout=min(max(wait_for, 0.01), 0.25))
        finally:
            if waiting:
                with self._condition:
                    self._wait_seconds += max(0.0, time.monotonic() - wait_started)
                    self._active_wait_started.pop(threading.get_ident(), None)

    def record_429(self, retry_after_seconds: float | None) -> None:
        cooldown = (
            self.default_cooldown_seconds
            if retry_after_seconds is None
            else max(0.0, retry_after_seconds)
        )
        with self._condition:
            self._http_429_count += 1
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + cooldown)
            self._condition.notify_all()

    def metrics(self) -> RateLimitMetrics:
        with self._condition:
            now = time.monotonic()
            current_wait = sum(
                max(0.0, now - started)
                for started in self._active_wait_started.values()
            )
            return RateLimitMetrics(
                rate_limit_wait_seconds=self._wait_seconds + current_wait,
                http_429_count=self._http_429_count,
                requests_sent=self._requests_sent,
                estimated_api_weight=self._estimated_api_weight,
                waiting_threads=len(self._active_wait_started),
            )

    def _discard_expired(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._events and self._events[0][0] <= cutoff:
            _timestamp, weight = self._events.popleft()
            self._window_weight -= weight
