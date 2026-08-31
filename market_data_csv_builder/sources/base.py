from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ..models import Candle, Instrument


class MarketDataSource(Protocol):
    name: str
    max_workers: int
    threshold: float

    def discover(self) -> list[Instrument]: ...

    def fetch_daily_candles(
        self, instrument: Instrument, closed_bars: int, as_of: datetime
    ) -> list[Candle]: ...


def select_latest_bars(candles: list[Candle], closed_bars: int) -> list[Candle]:
    """Keep exactly the requested closed tail plus at most one current candle."""
    ordered = sorted(candles, key=lambda item: item.timestamp)
    closed = [item for item in ordered if item.is_closed][-closed_bars:]
    unfinished = [item for item in ordered if not item.is_closed]
    return [*closed, *unfinished[-1:]]

