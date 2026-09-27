from __future__ import annotations

import json
import subprocess
import time
import uuid
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pandas as pd

from ..cancellation import CancellationRequested, CancellationToken
from ..concurrency import BoundedExecutor
from ..export import slice_closed_and_current
from ..features import FEATURE_IDS
from ..progress import StageProgress
from ..utils import safe_path_component
from .export import (CATALOG_COLUMNS, LATEST_COLUMNS, SERIES_COLUMNS, UNIVERSE_COLUMNS,
    RETURNS, calculate, publish, serialize, snapshot_readme, universe_row,
    validate_snapshot, write_csv, write_json, load_reusable_series)
from .provider import BybitPublicClient, CandleStore, TIMEFRAMES, WARMUP_BARS, eligible_instruments, now_iso


def _calculate_worker(frame, previous=None, reuse_export=0):
    """CPU worker: same feature engine, no network or filesystem side effects."""
    return calculate(frame, CancellationToken(), previous, reuse_export)


def run_intraday(config, root: Path, *, refresh_cache=False, no_cache=False,
                 cancellation_token=None, client=None, resume_staging=None, profile="intraday"):
    if profile not in {"intraday", "intraday_hyperliquid"}:
        raise ValueError("Invalid intraday profile")
    token = cancellation_token or CancellationToken()
    hyperliquid = profile == "intraday_hyperliquid"
    settings = config.intraday_hyperliquid if hyperliquid else config.intraday
    output = root / "output" / profile
    output.mkdir(parents=True, exist_ok=True)
    if resume_staging is not None:
        resume_staging = Path(resume_staging).resolve()
        if resume_staging.parent != (output / ".staging").resolve() or not resume_staging.is_dir():
            raise ValueError("Resume source must be an existing project INTRADAY staging directory")
    # Exclusive per-profile lock avoids concurrent publication/cache mutations.
    lock = output / ".run.lock"
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(now_iso())
    try:
        if client is None:
            if hyperliquid:
                from .hyperliquid import HyperliquidPublicClient
                client = HyperliquidPublicClient(config, token)
            else:
                client = BybitPublicClient(config, token)
        return _run(config, root, token, client,
                    refresh_cache or settings.refresh_cache, no_cache, resume_staging, profile)
    finally:
        lock.unlink(missing_ok=True)


def _run(config, root, token, client, refresh_cache, no_cache, resume_staging=None, profile="intraday"):
    start_clock, started = time.perf_counter(), now_iso()
    snapshot = started.replace(":", "").replace("-", "").replace("+0000", "Z") + "_" + uuid.uuid4().hex[:8]
    hyperliquid = profile == "intraday_hyperliquid"
    settings = config.intraday_hyperliquid if hyperliquid else config.intraday
    output = root / "output" / profile
    staging = output / ".staging" / snapshot
    staging.mkdir(parents=True)
    print(f"INTRADAY snapshot={snapshot}", flush=True)
    timings = dict(discovery=0.0, ticker_snapshot=0.0, instrument_metadata=0.0,
        history_download=0.0, history_download_by_timeframe={}, final_refresh=0.0,
        feature_calculation=0.0, export=0.0, validation=0.0, total=0.0)
    if hyperliquid:
        timings.update(final_refresh_network_worker_seconds=0.0, final_refresh_feature_worker_seconds=0.0, publication=0.0)
    failures = []
    store_class = CandleStore
    if hyperliquid:
        from .hyperliquid import HyperliquidCandleStore, eligible_hyperliquid
        store_class = HyperliquidCandleStore
    store = store_class(root, client, token, config.cache.enabled and not no_cache, refresh_cache)
    threshold = settings.min_turnover24h_usd if hyperliquid else settings.min_turnover24h_usdt
    select = eligible_hyperliquid if hyperliquid else eligible_instruments
    universe_columns = UNIVERSE_COLUMNS
    if hyperliquid:
        from .hyperliquid_export import UNIVERSE_COLUMNS as universe_columns, universe_row as hl_universe_row
    feature_pool = ProcessPoolExecutor(max_workers=settings.feature_workers) if settings.feature_workers > 1 else None

    def compute(frame, previous=None, reuse_export=0):
        token.raise_if_requested()
        if feature_pool is None:
            return calculate(frame, token, previous, reuse_export)
        result = feature_pool.submit(_calculate_worker, frame, previous, reuse_export).result()
        token.raise_if_requested()
        return result

    def jobs(items, function, stage, workers=None):
        progress = StageProgress("intraday", stage, len(items))
        progress.start()
        count = 0
        with BoundedExecutor(items, function, max_workers=workers or settings.concurrency,
                             thread_name_prefix="intraday", cancellation_token=token) as executor:
            while executor.has_pending:
                completed = executor.next_completed()
                for item, future in completed:
                    token.raise_if_requested()
                    count += 1
                    yield item, future, progress
                progress.update(count, **client.metrics())
        progress.complete(processed=count)

    def single(function, stage):
        for _, future, _ in jobs([None], lambda _: function(), stage, 1):
            result = future.result()
        return result

    def failure(symbol, tf, stage, exc):
        failures.append(dict(symbol=symbol, timeframe=tf, stage=stage,
            error_type=type(exc).__name__, reason=str(exc), retries=getattr(exc, "retries", 0)))

    try:
        stage = time.perf_counter()
        instruments = single(client.instruments, "instrument metadata")
        metadata_at = now_iso()
        timings["instrument_metadata"] = time.perf_counter() - stage
        stage = time.perf_counter()
        initial = single(client.tickers, "ticker snapshot")
        eligibility_at = now_iso()
        timings["ticker_snapshot"] = time.perf_counter() - stage
        eligible = select(instruments, initial, threshold)
        paths = [safe_path_component(item["symbol"]).casefold() for item in eligible]
        if len(set(paths)) != len(paths):
            raise ValueError("Instrument filenames collide on Windows")
        if hyperliquid:
            client.selected = [item["symbol"] for item in eligible]
        timings["discovery"] = timings["instrument_metadata"] + timings["ticker_snapshot"]
        print(f"INTRADAY discovered={len(instruments)} eligible={len(eligible)} threshold={threshold}", flush=True)
        histories, calculated, fetched, refreshed, errors = {}, {}, {}, {}, {}
        timeframes = sorted(settings.timeframes, key=lambda tf: -TIMEFRAMES[tf][1])

        def download(item, tf):
            return store.fetch(item["symbol"], tf, settings.closed_bars)

        for tf in timeframes:
            stage = time.perf_counter()
            for item, future, progress in jobs(eligible, lambda item: download(item, tf), f"history {tf}"):
                key = (item["symbol"], tf)
                try:
                    frame, fetched[key] = future.result()
                    histories[key] = frame
                except CancellationRequested:
                    raise
                except Exception as exc:
                    errors[key] = str(exc)
                    failure(*key, "history", exc)
                    progress.warning(item["symbol"], str(exc))
            elapsed = time.perf_counter() - stage
            timings["history_download_by_timeframe"][tf] = elapsed
            timings["history_download"] += elapsed

        reusable, resume_rejections = {}, []
        if resume_staging is not None:
            for key in sorted(histories):
                path = resume_staging / "series" / safe_path_component(key[0]) / (key[1] + ".csv")
                if path.is_file():
                    try:
                        reusable[key] = load_reusable_series(path, *key, source="hyperliquid" if hyperliquid else "bybit")
                    except Exception as exc:
                        resume_rejections.append(dict(symbol=key[0], timeframe=key[1], reason=str(exc)))
            print(f"INTRADAY resume: {len(reusable)} validated staged series reused; {len(resume_rejections)} rejected", flush=True)
        stage = time.perf_counter()
        def initial_features(key):
            frame = histories[key]
            omitted_prefix = max(0, int(frame.is_closed.sum()) - settings.closed_bars) if key in reusable else 0
            return compute(frame, reusable.get(key), omitted_prefix)

        for key, future, progress in jobs(sorted(histories), initial_features, "features", settings.feature_workers):
            try:
                calculated[key] = future.result()
            except CancellationRequested:
                raise
            except Exception as exc:
                errors[key] = str(exc)
                failure(*key, "features", exc)
                progress.warning(str(key), str(exc))
        timings["feature_calculation"] = time.perf_counter() - stage
        reused_series_count = len(reusable)
        reusable.clear()

        # Export during final pass so 5m does not wait for expensive full feature calculations.
        catalog, latest = [], []
        refresh_start = time.perf_counter()
        for tf in timeframes:
            def refresh(item):
                key = (item["symbol"], tf)
                if key not in calculated:
                    return None
                begin = time.perf_counter()
                frame, stamp = store.fetch(*key, settings.closed_bars, previous=histories[key])
                network = time.perf_counter() - begin
                begin = time.perf_counter()
                frame = compute(frame, previous=calculated[key])
                return frame, stamp, network, time.perf_counter() - begin

            for item, future, progress in jobs(eligible, refresh, f"final refresh/export {tf}"):
                key = (item["symbol"], tf)
                frame = calculated.get(key)
                partial = False
                try:
                    result = future.result()
                    if result is not None:
                        frame, refreshed[key], network_seconds, feature_seconds = result
                        if hyperliquid:
                            timings["final_refresh_network_worker_seconds"] += network_seconds
                            timings["final_refresh_feature_worker_seconds"] += feature_seconds
                except CancellationRequested:
                    raise
                except Exception as exc:
                    partial = True
                    errors[key] = str(exc)
                    failure(*key, "final_refresh", exc)
                    progress.warning(str(key), str(exc))
                entry = dict(snapshot_id=snapshot, generated_at_utc="", symbol=key[0], timeframe=tf,
                    status="FAILED", relative_path="", requested_closed_bars=settings.closed_bars,
                    closed_bars=0, has_1200_closed_bars="false", has_provisional_bar="false",
                    history_fetched_at_utc=fetched.get(key, ""), final_refresh_at_utc=refreshed.get(key, ""),
                    feature_status="FAILED", error=errors.get(key, ""))
                export_start = time.perf_counter()
                if frame is not None:
                    try:
                        frame = slice_closed_and_current(frame, settings.closed_bars)
                        frame["snapshot_id"] = snapshot
                        for name, value in dict(source="hyperliquid" if hyperliquid else "bybit", market="perpetual" if hyperliquid else "linear_perpetual", symbol=key[0],
                            instrument_name=item.get("displayName") or key[0], contract_type=item["contractType"],
                            dex=item.get("dex", ""), quote_currency=item["quoteCoin"], turnover_currency=item["quoteCoin"], timeframe=tf).items():
                            frame[name] = value
                        closed = frame.loc[frame.is_closed]
                        current = frame.loc[frame.provisional]
                        gaps = int((frame.timestamp.diff().dropna() > pd.Timedelta(milliseconds=TIMEFRAMES[tf][1])).sum())
                        relative = Path("series") / safe_path_component(key[0]) / (tf + ".csv")
                        serialized = serialize(frame)
                        if hyperliquid:
                            serialized["turnover"] = serialized.turnover.fillna("N/A")
                        write_csv(staging / relative, serialized, SERIES_COLUMNS)
                        entry.update(status="PARTIAL" if partial or gaps else "READY", relative_path=relative.as_posix(),
                            closed_bars=len(closed), has_1200_closed_bars=str(len(closed) >= 1200).lower(),
                            has_provisional_bar=str(not current.empty).lower(), first_timestamp=frame.iloc[0].timestamp.isoformat(),
                            last_closed_timestamp=closed.iloc[-1].timestamp.isoformat() if not closed.empty else "",
                            provisional_timestamp=current.iloc[-1].timestamp.isoformat() if not current.empty else "",
                            feature_status="CALCULATED", gap_count=gaps, missing_current_bar=str(current.empty).lower())
                        for state, subset in (("CLOSED", serialized.loc[frame.is_closed]), ("PROVISIONAL", serialized.loc[frame.provisional])):
                            if not subset.empty:
                                latest.append(dict(subset.iloc[-1], candle_state=state, bars_available=len(closed)))
                    except CancellationRequested:
                        raise
                    except Exception as exc:
                        entry.update(status="FAILED", relative_path="", error=str(exc), feature_status="FAILED")
                        failure(*key, "export", exc)
                        progress.warning(str(key), str(exc))
                catalog.append(entry)
                timings["export"] += time.perf_counter() - export_start
                histories.pop(key, None)
                calculated.pop(key, None)
        # Fresh funding/book-top is fetched after the tail pass, for the fixed eligible set.
        tickers = single(client.tickers, "final bulk ticker/funding")
        ticker_at = now_iso()
        if any(item["symbol"] not in tickers for item in eligible):
            raise ValueError("Final ticker snapshot missing eligible instruments")
        timings["final_refresh"] = time.perf_counter() - refresh_start
        universe = [(hl_universe_row if hyperliquid else universe_row)(item, tickers[item["symbol"]], initial[item["symbol"]], snapshot,
            threshold, metadata_at, ticker_at, eligibility_at) for item in eligible]
        catalog.sort(key=lambda row: (row["symbol"], timeframes.index(row["timeframe"])))
        latest.sort(key=lambda row: (row["symbol"], timeframes.index(row["timeframe"]), row["candle_state"]))
        counts = Counter(row["status"] for row in catalog)
        symbol_states = []
        for item in eligible:
            states = [row["status"] for row in catalog if row["symbol"] == item["symbol"]]
            symbol_states.append("READY" if all(s == "READY" for s in states) else "FAILED" if all(s == "FAILED" for s in states) else "PARTIAL")
        symbol_counts = Counter(symbol_states)
        try:
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            commit = "unknown"
        completed = now_iso()
        for row in catalog:
            row["generated_at_utc"] = completed
        manifest = dict(schema_version="1.0", builder_version="2.0", git_commit=commit, profile=profile,
            resumed_from_snapshot=resume_staging.name if resume_staging else None, reused_series_count=reused_series_count,
            snapshot_id=snapshot, started_at_utc=started, completed_at_utc=completed,
            final_refresh_at_utc=ticker_at, provider="hyperliquid" if hyperliquid else "bybit", market="perpetual" if hyperliquid else "linear_perpetual",
            quote_or_settle="per-instrument metadata" if hyperliquid else "USDT",
            liquidity_filter=dict(field="turnover24h", threshold_usdt=threshold,
                                  eligibility_at_utc=eligibility_at), timeframes=timeframes,
            requested_closed_bars=settings.closed_bars, warmup_bars=WARMUP_BARS,
            features=list(FEATURE_IDS), additional_features=["atr_wilder_14", *RETURNS],
            discovered_contracts=len(instruments), eligible_contracts=len(eligible),
            successful_symbols=symbol_counts["READY"], partial_symbols=symbol_counts["PARTIAL"], failed_symbols=symbol_counts["FAILED"],
            expected_series_count=len(catalog), successful_series_count=counts["READY"] + counts["PARTIAL"],
            partial_series_count=counts["PARTIAL"], failed_series_count=counts["FAILED"],
            freshness=dict(policy="per-series fetch timestamps; consumer methodology defines allowed age",
                           earliest_final_refresh_at_utc=min(refreshed.values(), default=None), latest_final_refresh_at_utc=max(refreshed.values(), default=None)),
            paths={name: name for name in ("universe.csv", "catalog.csv", "latest_features.csv", "run_report.json", "series/")})
        if hyperliquid:
            manifest["liquidity_filter"] = dict(field="dayNtlVlm", threshold_usd=threshold, eligibility_at_utc=eligibility_at)
            manifest["dexes"] = client.dexes
            manifest["candle_turnover_policy"] = "N/A when absent; never close times volume"
        applicable = sum((i.get("status"), i.get("contractType"), i.get("quoteCoin"), i.get("settleCoin")) ==
                         ("Trading", "LinearPerpetual", "USDT", "USDT") for i in instruments)
        report = dict(profile=profile, snapshot_id=snapshot, started_at_utc=started, completed_at_utc=completed,
            resumed_from_snapshot=resume_staging.name if resume_staging else None,
            reused_series_count=reused_series_count, resume_rejections=resume_rejections,
            config_summary=dict(cache_enabled=store.enabled, refresh_cache=store.refresh,
                concurrency=settings.concurrency, requests_per_second=settings.requests_per_second,
                feature_workers=settings.feature_workers,
                use_system_proxy=settings.use_system_proxy, max_retries=settings.max_retries,
                request_timeout=settings.request_timeout),
            counts=dict(discovered=len(instruments), eligible=len(eligible), rejected_by_liquidity=(len(instruments) if hyperliquid else applicable)-len(eligible),
                ready_symbols=symbol_counts["READY"], partial_symbols=symbol_counts["PARTIAL"], failed_symbols=symbol_counts["FAILED"],
                successful_series=counts["READY"]+counts["PARTIAL"], partial_series=counts["PARTIAL"], failed_series=counts["FAILED"]),
            counts_by_timeframe={tf: dict(Counter(row["status"] for row in catalog if row["timeframe"] == tf)) for tf in timeframes},
            failures=failures, elapsed_seconds=timings, **client.metrics())
        write_csv(staging / "catalog.csv", catalog, CATALOG_COLUMNS)
        write_csv(staging / "universe.csv", universe, universe_columns)
        write_csv(staging / "latest_features.csv", latest, LATEST_COLUMNS)
        consumer_file = "APX_HYPERLIQUID_INTRADAY_CONSUMER.md" if hyperliquid else "APX_INTRADAY_MARKETDATA_CONSUMER.md"
        consumer = (root / "docs" / consumer_file).read_text(encoding="utf-8")
        if hyperliquid:
            report["book_errors"] = client.book_errors
            report["config_summary"].pop("requests_per_second")
            report["config_summary"].update(rate_limit_safety_fraction=settings.rate_limit_safety_fraction,
                weight_budget_per_minute=int(1200 * settings.rate_limit_safety_fraction),
                min_turnover24h_usd=threshold)
        write_json(staging / "manifest.json", manifest)
        write_json(staging / "run_report.json", report)
        (staging / "README_INTRADAY_MARKET_DATA.md").write_text(snapshot_readme(manifest, consumer), encoding="utf-8")
        validation_start = time.perf_counter()
        single(lambda: validate_snapshot(staging), "validation")
        timings["validation"] = time.perf_counter() - validation_start
        completed = now_iso()
        manifest["completed_at_utc"] = report["completed_at_utc"] = completed
        for row in catalog:
            row["generated_at_utc"] = completed
        write_csv(staging / "catalog.csv", catalog, CATALOG_COLUMNS)
        write_json(staging / "manifest.json", manifest)
        (staging / "README_INTRADAY_MARKET_DATA.md").write_text(snapshot_readme(manifest, consumer), encoding="utf-8")
        timings["total"] = time.perf_counter() - start_clock
        # Settle the report's own size as part of the exact byte count.
        for _ in range(3):
            report["snapshot_bytes"] = sum(path.stat().st_size for path in staging.rglob("*") if path.is_file())
            write_json(staging / "run_report.json", report)
        token.raise_if_requested()
        publish(staging, output, snapshot, report=report if hyperliquid else None)
        print(f"INTRADAY published {snapshot} in {timings['total']:.1f}s", flush=True)
        return report
    except BaseException as exc:
        write_json(staging / "failure.json", dict(snapshot_id=snapshot, error_type=type(exc).__name__,
            reason=str(exc), failures=failures, elapsed_seconds=time.perf_counter()-start_clock, **client.metrics()))
        raise
    finally:
        if feature_pool is not None:
            feature_pool.shutdown(wait=True, cancel_futures=True)
