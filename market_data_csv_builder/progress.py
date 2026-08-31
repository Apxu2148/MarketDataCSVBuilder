from __future__ import annotations

import time
from datetime import datetime
from typing import Any


class StageProgress:
    """Rate-limited, immediately flushed console progress for long stages."""

    def __init__(self, source: str, stage: str, total: int | None = None, interval: float = 10.0) -> None:
        self.source = source.upper()
        self.stage = stage
        self.total = total
        self.interval = interval
        self.started = time.perf_counter()
        self.last_output = self.started
        self.processed = 0
        self.metrics: dict[str, Any] = {}

    def start(self) -> None:
        suffix = "" if self.total is None else f": 0/{self.total}"
        self._print(f"{self.source} {self.stage} started{suffix}")

    def update(self, processed: int, *, force: bool = False, **metrics: Any) -> None:
        self.processed = processed
        self.metrics.update(metrics)
        now = time.perf_counter()
        completed = self.total is not None and processed >= self.total
        if not (force or completed or now - self.last_output >= self.interval):
            return
        total = "?" if self.total is None else str(self.total)
        details = ", ".join(f"{key}={value}" for key, value in self.metrics.items())
        message = f"{self.source} {self.stage}: {processed}/{total}"
        if details:
            message += f", {details}"
        message += f", elapsed={now - self.started:.1f} s"
        self._print(message)
        self.last_output = now

    def warning(self, symbol: str, reason: str) -> None:
        compact = " ".join(str(reason).split())
        self._print(f"WARNING {self.source} {self.stage} {symbol or '<source>'}: {compact}")

    def complete(self, **metrics: Any) -> None:
        self.metrics.update(metrics)
        elapsed = time.perf_counter() - self.started
        details = ", ".join(f"{key}={value}" for key, value in self.metrics.items())
        message = f"{self.source} {self.stage} completed"
        if details:
            message += f": {details}"
        message += f", {elapsed:.1f} s"
        self._print(message)

    @staticmethod
    def _print(message: str) -> None:
        stamp = datetime.now().astimezone().strftime("%H:%M:%S")
        print(f"[{stamp}] {message}", flush=True)
