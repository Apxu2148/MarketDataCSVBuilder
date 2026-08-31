from __future__ import annotations

from .models import Candle, LiquidityResult


def calculate_average_turnover(candles: list[Candle], lookback_closed_bars: int) -> LiquidityResult:
    closed = [item for item in sorted(candles, key=lambda value: value.timestamp) if item.is_closed]
    selected = closed[-lookback_closed_bars:]
    turnovers = [float(item.turnover) for item in selected if item.turnover is not None and item.turnover >= 0]
    if not turnovers:
        raise ValueError("No closed candles with usable turnover")
    currencies = {item.turnover_currency for item in selected if item.turnover is not None}
    if len(currencies) != 1:
        raise ValueError("Turnover currency changes inside one time series")
    return LiquidityResult(
        average_turnover=sum(turnovers) / len(turnovers),
        turnover_currency=next(iter(currencies)),
        closed_bars_used=len(turnovers),
    )

