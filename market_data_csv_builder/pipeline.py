from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .config import AppConfig
from .cancellation import CancellationRequested, CancellationToken
from .concurrency import BoundedExecutor
from .export import SnapshotWriter
from .features import calculate_features
from .liquidity import calculate_average_turnover
from .models import Candle, Instrument, LiquidityResult, ReadyDataset, UniverseRecord
from .progress import StageProgress
from .sources import BybitSource, HyperliquidSource, MarketDataSource, MoexSource
from .utils import utc_iso

logger = logging.getLogger(__name__)


def build_sources(
    config: AppConfig,
    root: Path,
    selected: str,
    *,
    refresh_cache: bool,
    no_cache: bool,
    cancellation_token: CancellationToken,
) -> list[MarketDataSource]:
    sources: list[MarketDataSource] = []
    if selected in {"all", "moex"} and config.moex.enabled:
        sources.append(MoexSource(config, root, refresh_cache=refresh_cache, no_cache=no_cache, cancellation_token=cancellation_token))
    if selected in {"all", "bybit"} and config.bybit.enabled:
        sources.append(BybitSource(config, root, refresh_cache=refresh_cache, no_cache=no_cache, cancellation_token=cancellation_token))
    if selected in {"all", "hyperliquid"} and config.hyperliquid.enabled:
        sources.append(HyperliquidSource(config, root, refresh_cache=refresh_cache, no_cache=no_cache, cancellation_token=cancellation_token))
    if not sources:
        raise ValueError(f"No enabled source matches --source {selected}")
    return sources


def run_pipeline(
    config: AppConfig,
    root: Path,
    *,
    selected_source: str,
    limit: int | None,
    refresh_cache: bool,
    no_cache: bool,
    as_of: datetime,
    sources: list[MarketDataSource] | None = None,
    cancellation_token: CancellationToken | None = None,
) -> dict[str, Any]:
    token = cancellation_token or CancellationToken()
    token.raise_if_requested()
    started_clock = time.perf_counter()
    started_at = datetime.now(UTC)
    snapshot_id = started_at.strftime("%Y%m%dT%H%M%SZ")
    writer = SnapshotWriter(root, config.general.output_dir)
    writer.begin()
    active_sources = sources or build_sources(
        config,
        root,
        selected_source,
        refresh_cache=refresh_cache,
        no_cache=no_cache,
        cancellation_token=token,
    )
    source_metrics = {
        source.name: {
            "discovered": 0,
            "passed_liquidity_filter": 0,
            "READY": 0,
            "below_threshold": 0,
            "download_failed": 0,
            "feature_failed": 0,
            "export_failed": 0,
            "elapsed_discovery_seconds": 0.0,
            "elapsed_liquidity_stage_seconds": 0.0,
            "elapsed_full_history_download_seconds": 0.0,
            "elapsed_feature_calculation_seconds": 0.0,
            "elapsed_export_seconds": 0.0,
        }
        for source in active_sources
    }
    universe: list[UniverseRecord] = []
    records: dict[tuple[str, str, str, str], UniverseRecord] = {}
    ready: list[ReadyDataset] = []
    discovered: dict[str, list[Instrument]] = {}

    for source in active_sources:
        token.raise_if_requested()
        stage = time.perf_counter()
        progress = StageProgress(source.name, "discovery")
        progress.start()
        try:
            with BoundedExecutor(
                [source],
                lambda item: item.discover(),
                max_workers=1,
                thread_name_prefix=f"{source.name}-discovery",
                cancellation_token=token,
            ) as jobs:
                items: list[Instrument] = []
                while jobs.has_pending:
                    completed = jobs.next_completed()
                    if not completed:
                        progress.update(
                            0,
                            status="waiting for API",
                            **_rate_limit_metrics(source),
                        )
                        continue
                    _item, future = completed[0]
                    items = future.result()
            if limit is not None:
                items = items[:limit]
            discovered[source.name] = items
            source_metrics[source.name]["discovered"] = len(items)
            for instrument in items:
                record = UniverseRecord(
                    source=instrument.source,
                    market=instrument.market,
                    symbol=instrument.symbol,
                    contract_type=instrument.contract_type,
                    dex=instrument.dex,
                    avg_daily_turnover=None,
                    threshold=source.threshold,
                    status="DISCOVERED",
                )
                universe.append(record)
                records[instrument.key] = record
        except CancellationRequested:
            raise
        except Exception as exc:
            logger.exception("%s discovery failed", source.name)
            progress.warning("", str(exc))
            discovered[source.name] = []
            universe.append(
                UniverseRecord(
                    source=source.name,
                    market="",
                    symbol="",
                    contract_type="",
                    dex="",
                    avg_daily_turnover=None,
                    threshold=source.threshold,
                    status="DISCOVERY_FAILED",
                    reason=str(exc),
                    error_type=type(exc).__name__,
                )
            )
        source_metrics[source.name]["elapsed_discovery_seconds"] = time.perf_counter() - stage
        progress.complete(
            instruments=source_metrics[source.name]["discovered"],
            **_rate_limit_metrics(source),
        )

    passed: dict[str, list[tuple[Instrument, LiquidityResult]]] = {}
    for source in active_sources:
        token.raise_if_requested()
        stage = time.perf_counter()
        selected: list[tuple[Instrument, LiquidityResult]] = []
        instruments = discovered[source.name]
        progress = StageProgress(source.name, "liquidity scan", len(instruments))
        progress.start()
        processed = failed = below = 0
        with BoundedExecutor(
            instruments,
            lambda instrument: source.fetch_daily_candles(
                instrument, config.general.liquidity_lookback_closed_bars, as_of
            ),
            max_workers=source.max_workers,
            thread_name_prefix=f"{source.name}-liq",
            cancellation_token=token,
        ) as jobs:
            while jobs.has_pending:
                completed = jobs.next_completed()
                if not completed:
                    progress.update(
                        processed,
                        passed=len(selected),
                        below=below,
                        failed=failed,
                        **_rate_limit_metrics(source),
                    )
                    continue
                for instrument, future in completed:
                    record = records[instrument.key]
                    try:
                        candles = future.result()
                        liquidity = calculate_average_turnover(
                            candles, config.general.liquidity_lookback_closed_bars
                        )
                        record.avg_daily_turnover = liquidity.average_turnover
                        if liquidity.average_turnover < source.threshold:
                            record.status = "BELOW_LIQUIDITY_THRESHOLD"
                            record.reason = (
                                f"average {liquidity.average_turnover:.6g} below threshold "
                                f"{source.threshold:.6g}; {liquidity.closed_bars_used} closed bars"
                            )
                            source_metrics[source.name]["below_threshold"] += 1
                            below += 1
                        else:
                            record.status = "PASSED_LIQUIDITY_FILTER"
                            record.reason = f"{liquidity.closed_bars_used} closed bars used"
                            selected.append((instrument, liquidity))
                    except CancellationRequested:
                        raise
                    except Exception as exc:
                        _fail(record, "DOWNLOAD_FAILED", "liquidity download/calculation", exc)
                        source_metrics[source.name]["download_failed"] += 1
                        failed += 1
                        progress.warning(instrument.symbol, str(exc))
                    processed += 1
                progress.update(
                    processed,
                    passed=len(selected),
                    below=below,
                    failed=failed,
                    **_rate_limit_metrics(source),
                )
        passed[source.name] = sorted(selected, key=lambda item: item[0].key)
        source_metrics[source.name]["passed_liquidity_filter"] = len(selected)
        source_metrics[source.name]["elapsed_liquidity_stage_seconds"] = time.perf_counter() - stage
        progress.complete(
            processed=processed,
            passed=len(selected),
            below=below,
            failed=failed,
            **_rate_limit_metrics(source),
        )

    histories: dict[tuple[str, str, str, str], tuple[Instrument, LiquidityResult, list[Candle]]] = {}
    for source in active_sources:
        token.raise_if_requested()
        stage = time.perf_counter()
        total = len(passed[source.name])
        progress = StageProgress(source.name, "full history download", total)
        progress.start()
        processed = success = failed = 0
        with BoundedExecutor(
            passed[source.name],
            lambda item: source.fetch_daily_candles(
                item[0], config.general.history_closed_bars, as_of
            ),
            max_workers=source.max_workers,
            thread_name_prefix=f"{source.name}-history",
            cancellation_token=token,
        ) as jobs:
            while jobs.has_pending:
                completed = jobs.next_completed()
                if not completed:
                    progress.update(
                        processed,
                        success=success,
                        failed=failed,
                        **_rate_limit_metrics(source),
                    )
                    continue
                for (instrument, liquidity), future in completed:
                    record = records[instrument.key]
                    try:
                        candles = future.result()
                        if not candles:
                            raise ValueError("source returned no daily candles")
                        histories[instrument.key] = (instrument, liquidity, candles)
                        success += 1
                    except CancellationRequested:
                        raise
                    except Exception as exc:
                        _fail(record, "DOWNLOAD_FAILED", "full-history download", exc)
                        source_metrics[source.name]["download_failed"] += 1
                        failed += 1
                        progress.warning(instrument.symbol, str(exc))
                    processed += 1
                progress.update(
                    processed,
                    success=success,
                    failed=failed,
                    **_rate_limit_metrics(source),
                )
        source_metrics[source.name]["elapsed_full_history_download_seconds"] = time.perf_counter() - stage
        progress.complete(
            processed=processed,
            success=success,
            failed=failed,
            **_rate_limit_metrics(source),
        )

    calculated: dict[tuple[str, str, str, str], pd.DataFrame] = {}
    for source in active_sources:
        token.raise_if_requested()
        stage = time.perf_counter()
        source_histories = [item for key, item in sorted(histories.items()) if key[0] == source.name]
        progress = StageProgress(source.name, "feature calculation", len(source_histories))
        progress.start()
        processed = success = failed = 0
        with BoundedExecutor(
            source_histories,
            lambda item: calculate_features(
                candles_to_frame(item[0], item[2]), cancellation_token=token
            ),
            max_workers=min(source.max_workers, 4),
            thread_name_prefix=f"{source.name}-features",
            cancellation_token=token,
        ) as jobs:
            while jobs.has_pending:
                completed = jobs.next_completed()
                if not completed:
                    progress.update(
                        processed,
                        success=success,
                        failed=failed,
                        **_rate_limit_metrics(source),
                    )
                    continue
                for item, future in completed:
                    instrument, _liquidity, _candles = item
                    key = instrument.key
                    try:
                        calculated[key] = future.result()
                        success += 1
                    except CancellationRequested:
                        raise
                    except Exception as exc:
                        _fail(records[key], "FEATURE_CALCULATION_FAILED", "feature calculation", exc)
                        source_metrics[source.name]["feature_failed"] += 1
                        failed += 1
                        progress.warning(instrument.symbol, str(exc))
                    processed += 1
                progress.update(
                    processed,
                    success=success,
                    failed=failed,
                    **_rate_limit_metrics(source),
                )
        source_metrics[source.name]["elapsed_feature_calculation_seconds"] += time.perf_counter() - stage
        progress.complete(
            processed=processed,
            success=success,
            failed=failed,
            **_rate_limit_metrics(source),
        )

    for source in active_sources:
        token.raise_if_requested()
        stage = time.perf_counter()
        source_keys = [key for key in sorted(calculated) if key[0] == source.name]
        progress = StageProgress(source.name, "export", len(source_keys))
        progress.start()
        processed = success = failed = 0
        for key in source_keys:
            token.raise_if_requested()
            instrument, liquidity, _candles = histories[key]
            frame = calculated[key]
            record = records[key]
            try:
                full_path, compact_path = writer.write_dataset(
                    instrument, frame, config.general.compact_closed_bars
                )
                token.raise_if_requested()
                closed_count = int(frame["is_closed"].sum())
                current_present = bool((~frame["is_closed"]).any())
                ready.append(
                    ReadyDataset(
                        instrument=instrument,
                        average_turnover=liquidity.average_turnover,
                        turnover_currency=liquidity.turnover_currency,
                        closed_bars_available=closed_count,
                        first_timestamp=frame.iloc[0]["timestamp"].isoformat(),
                        last_timestamp=frame.iloc[-1]["timestamp"].isoformat(),
                        current_candle_present=current_present,
                        full_path=full_path,
                        compact_path=compact_path,
                    )
                )
                record.status = "READY"
                record.reason = "dataset exported"
                source_metrics[source.name]["READY"] += 1
                success += 1
            except CancellationRequested:
                raise
            except Exception as exc:
                _fail(record, "EXPORT_FAILED", "CSV export", exc)
                source_metrics[source.name]["export_failed"] += 1
                failed += 1
                progress.warning(instrument.symbol, str(exc))
            processed += 1
            progress.update(
                processed,
                READY=success,
                failed=failed,
                **_rate_limit_metrics(source),
            )
        source_metrics[source.name]["elapsed_export_seconds"] += time.perf_counter() - stage
        progress.complete(
            processed=processed,
            READY=success,
            failed=failed,
            **_rate_limit_metrics(source),
        )

    token.raise_if_requested()
    for source in active_sources:
        source_metrics[source.name].update(_rate_limit_metrics(source))
    completed_at = datetime.now(UTC)
    counts = _overall_counts(source_metrics)
    report: dict[str, Any] = {
        "snapshot_id": snapshot_id,
        "started_at": utc_iso(started_at),
        "completed_at": utc_iso(completed_at),
        "as_of": utc_iso(as_of),
        "config_summary": {
            "selected_source": selected_source,
            "limit_per_source": limit,
            "history_closed_bars": config.general.history_closed_bars,
            "compact_closed_bars": config.general.compact_closed_bars,
            "liquidity_lookback_closed_bars": config.general.liquidity_lookback_closed_bars,
            "cache_enabled": config.cache.enabled and not no_cache,
            "refresh_cache": refresh_cache,
            "thresholds": {
                "moex_rub": config.moex.threshold_rub,
                "bybit_usd_equivalent": config.bybit.threshold_usd,
                "hyperliquid_usd_equivalent": config.hyperliquid.threshold_usd,
            },
        },
        "sources": source_metrics,
        "counts": {
            **counts,
            "failed": counts["download_failed"] + counts["feature_failed"] + counts["export_failed"],
            "dataset_csv_files_created": len(ready) * 2,
            "latest_snapshot_rows": len(ready),
            "total_csv_files_created": len(ready) * 2 + 3,
        },
        "elapsed_seconds": {
            "discovery": sum(item["elapsed_discovery_seconds"] for item in source_metrics.values()),
            "liquidity_stage": sum(item["elapsed_liquidity_stage_seconds"] for item in source_metrics.values()),
            "full_history_download": sum(item["elapsed_full_history_download_seconds"] for item in source_metrics.values()),
            "feature_calculation": sum(item["elapsed_feature_calculation_seconds"] for item in source_metrics.values()),
            "export": sum(item["elapsed_export_seconds"] for item in source_metrics.values()),
            "total": time.perf_counter() - started_clock,
        },
        "failures": [item.as_dict() for item in universe if item.status not in {"READY", "BELOW_LIQUIDITY_THRESHOLD"}],
        "current_snapshot_path": str((root / config.general.output_dir / "current").resolve()),
    }
    writer.finalize_metadata(
        snapshot_id=snapshot_id,
        generated_at=utc_iso(completed_at),
        ready=ready,
        universe=universe,
        report=report,
    )
    token.raise_if_requested()
    if not ready:
        raise RuntimeError(
            f"No datasets were successfully generated; diagnostic snapshot remains at {writer.building}"
        )
    current = writer.publish()
    report["current_snapshot_path"] = str(current)
    return report


def candles_to_frame(instrument: Instrument, candles: list[Candle]) -> pd.DataFrame:
    rows = []
    for candle in sorted(candles, key=lambda item: item.timestamp):
        rows.append(
            {
                "source": instrument.source,
                "market": instrument.market,
                "symbol": instrument.symbol,
                "instrument_name": instrument.instrument_name,
                "contract_type": instrument.contract_type,
                "dex": instrument.dex,
                "quote_currency": instrument.quote_currency,
                "timestamp": candle.timestamp,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
                "turnover": candle.turnover,
                "turnover_currency": candle.turnover_currency,
                "is_closed": candle.is_closed,
                "provisional": candle.provisional,
            }
        )
    return pd.DataFrame(rows)


def _fail(record: UniverseRecord, status: str, stage: str, exc: Exception) -> None:
    record.status = status
    record.reason = f"{stage}: {exc}"
    record.error_type = type(exc).__name__


def _overall_counts(metrics: dict[str, dict[str, Any]]) -> dict[str, int]:
    names = (
        "discovered",
        "passed_liquidity_filter",
        "READY",
        "below_threshold",
        "download_failed",
        "feature_failed",
        "export_failed",
    )
    return {name: int(sum(source[name] for source in metrics.values())) for name in names}


def _rate_limit_metrics(source: MarketDataSource) -> dict[str, float | int]:
    provider = getattr(source, "rate_limit_metrics", None)
    if not callable(provider):
        return {}
    metrics = provider()
    if not isinstance(metrics, dict):
        return {}
    names = (
        "rate_limit_wait_seconds",
        "http_429_count",
        "requests_sent",
        "estimated_api_weight",
        "waiting_threads",
    )
    return {name: metrics[name] for name in names if name in metrics}
