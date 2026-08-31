from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Instrument:
    source: str
    market: str
    symbol: str
    instrument_name: str
    contract_type: str
    dex: str = ""
    quote_currency: str = ""
    source_symbol: str | None = None
    api_engine: str = ""
    api_market: str = ""
    lot_size: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def api_symbol(self) -> str:
        return self.source_symbol or self.symbol

    @property
    def key(self) -> tuple[str, str, str, str]:
        return self.source, self.market, self.dex, self.symbol


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float | None
    turnover_currency: str
    is_closed: bool

    @property
    def provisional(self) -> bool:
        return not self.is_closed


@dataclass(frozen=True)
class LiquidityResult:
    average_turnover: float
    turnover_currency: str
    closed_bars_used: int


@dataclass
class UniverseRecord:
    source: str
    market: str
    symbol: str
    contract_type: str
    dex: str
    avg_daily_turnover: float | None
    threshold: float
    status: str
    reason: str = ""
    error_type: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "market": self.market,
            "symbol": self.symbol,
            "contract_type": self.contract_type,
            "dex": self.dex,
            "avg_daily_turnover": self.avg_daily_turnover,
            "threshold": self.threshold,
            "status": self.status,
            "reason": self.reason,
            "error_type": self.error_type,
        }


@dataclass
class ReadyDataset:
    instrument: Instrument
    average_turnover: float
    turnover_currency: str
    closed_bars_available: int
    first_timestamp: str
    last_timestamp: str
    current_candle_present: bool
    full_path: str
    compact_path: str

