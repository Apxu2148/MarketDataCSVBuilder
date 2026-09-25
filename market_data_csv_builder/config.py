from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar


@dataclass(frozen=True)
class GeneralConfig:
    history_closed_bars: int = 1200
    compact_closed_bars: int = 100
    liquidity_lookback_closed_bars: int = 30
    output_dir: str = "output"
    log_level: str = "INFO"


@dataclass(frozen=True)
class CacheConfig:
    enabled: bool = True
    directory: str = "data/cache"
    ttl_hours: float = 12.0


@dataclass(frozen=True)
class HttpConfig:
    timeout_seconds: float = 30.0
    max_retries: int = 3
    backoff_seconds: float = 0.8
    max_backoff_seconds: float = 5.0


@dataclass(frozen=True)
class MoexConfig:
    enabled: bool = True
    base_url: str = "https://iss.moex.com/iss"
    language: str = "en"
    max_workers: int = 8
    page_size: int = 500
    threshold_rub: float = 100_000_000.0
    shares_enabled: bool = True
    term_futures_enabled: bool = True
    perpetual_futures_enabled: bool = True
    funds_enabled: bool = True
    bonds_enabled: bool = True
    perpetual_name_markers: list[str] = field(
        default_factory=lambda: ["perpetual", "вечн", "бессроч"]
    )
    perpetual_symbol_suffixes: list[str] = field(default_factory=lambda: ["F"])


@dataclass(frozen=True)
class BybitConfig:
    enabled: bool = True
    base_url: str = "https://api.bybit.com"
    fallback_base_urls: list[str] = field(default_factory=lambda: ["https://api.bytick.com"])
    max_workers: int = 8
    page_size: int = 1000
    threshold_usd: float = 1_000_000.0
    linear_perpetual_enabled: bool = True
    linear_futures_enabled: bool = True
    spot_enabled: bool = False


@dataclass(frozen=True)
class HyperliquidConfig:
    enabled: bool = True
    base_url: str = "https://api.hyperliquid.xyz"
    info_path: str = "/info"
    max_workers: int = 6
    threshold_usd: float = 1_000_000.0
    discover_all_perp_dexes: bool = True
    include_delisted: bool = False
    perp_dexes: list[str] = field(default_factory=list)
    exclude_perp_dexes: list[str] = field(default_factory=list)
    rate_limit_safety_fraction: float = 0.80
    rate_limit_default_cooldown_seconds: float = 60.0


@dataclass(frozen=True)
class IntradayConfig:
    source: str = "bybit"
    category: str = "linear"
    quote_coin: str = "USDT"
    settle_coin: str = "USDT"
    min_turnover24h_usdt: float = 10_000_000
    timeframes: list[str] = field(default_factory=lambda: ["1D", "4H", "1H", "15m", "5m"])
    closed_bars: int = 1200
    atr_period: int = 14
    request_timeout: float = 30
    concurrency: int = 4
    max_retries: int = 4
    backoff_base: float = 1
    backoff_max: float = 30
    jitter: float = 0.25
    requests_per_second: float = 5
    refresh_cache: bool = False
    use_system_proxy: bool = False


@dataclass(frozen=True)
class AppConfig:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    moex: MoexConfig = field(default_factory=MoexConfig)
    bybit: BybitConfig = field(default_factory=BybitConfig)
    hyperliquid: HyperliquidConfig = field(default_factory=HyperliquidConfig)
    intraday: IntradayConfig = field(default_factory=IntradayConfig)


T = TypeVar("T")


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"Section [{name}] must be a TOML table")
    return value


def _load(cls: type[T], data: dict[str, Any], name: str) -> T:
    try:
        return cls(**_section(data, name))
    except TypeError as exc:
        raise ValueError(f"Invalid [{name}] configuration: {exc}") from exc


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        raise FileNotFoundError(f"Config file does not exist: {path}")
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    config = AppConfig(
        intraday=_load(IntradayConfig, data, "intraday"),
        general=_load(GeneralConfig, data, "general"),
        cache=_load(CacheConfig, data, "cache"),
        http=_load(HttpConfig, data, "http"),
        moex=_load(MoexConfig, data, "moex"),
        bybit=_load(BybitConfig, data, "bybit"),
        hyperliquid=_load(HyperliquidConfig, data, "hyperliquid"),
    )
    validate_config(config)
    return config


def validate_config(config: AppConfig) -> None:
    import math

    intraday = config.intraday
    if (intraday.source, intraday.category, intraday.quote_coin, intraday.settle_coin) != ("bybit", "linear", "USDT", "USDT"):
        raise ValueError("INTRADAY supports only Bybit linear USDT perpetuals")
    if not intraday.timeframes or len(set(intraday.timeframes)) != len(intraday.timeframes) or set(intraday.timeframes) - {"1D", "4H", "1H", "15m", "5m"}:
        raise ValueError("Invalid or duplicate intraday timeframes")
    for name in ("closed_bars", "concurrency", "max_retries", "atr_period"):
        value = getattr(intraday, name)
        if type(value) is not int or value <= 0:
            raise ValueError(f"intraday.{name} must be a positive integer")
    if intraday.atr_period != 14:
        raise ValueError("atr_wilder_14 requires atr_period=14")
    for name in ("min_turnover24h_usdt", "request_timeout", "requests_per_second", "backoff_base", "backoff_max", "jitter"):
        value = getattr(intraday, name)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"Invalid intraday.{name}")
    if intraday.request_timeout <= 0 or intraday.requests_per_second <= 0:
        raise ValueError("Intraday timeout and request rate must be positive")
    positive = {
        "general.history_closed_bars": config.general.history_closed_bars,
        "general.compact_closed_bars": config.general.compact_closed_bars,
        "general.liquidity_lookback_closed_bars": config.general.liquidity_lookback_closed_bars,
        "http.timeout_seconds": config.http.timeout_seconds,
        "http.max_retries": config.http.max_retries,
        "moex.max_workers": config.moex.max_workers,
        "bybit.max_workers": config.bybit.max_workers,
        "hyperliquid.max_workers": config.hyperliquid.max_workers,
    }
    invalid = [name for name, value in positive.items() if value <= 0]
    if invalid:
        raise ValueError(f"Configuration values must be positive: {', '.join(invalid)}")
    if config.general.compact_closed_bars > config.general.history_closed_bars:
        raise ValueError("compact_closed_bars cannot exceed history_closed_bars")
    if config.http.max_backoff_seconds < 0 or config.http.backoff_seconds < 0:
        raise ValueError("HTTP backoff values cannot be negative")
    if not 0 < config.hyperliquid.rate_limit_safety_fraction <= 1:
        raise ValueError("hyperliquid.rate_limit_safety_fraction must be in (0, 1]")
    if config.hyperliquid.rate_limit_default_cooldown_seconds < 0:
        raise ValueError("hyperliquid.rate_limit_default_cooldown_seconds cannot be negative")
