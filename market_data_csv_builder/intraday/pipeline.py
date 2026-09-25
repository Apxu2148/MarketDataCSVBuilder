from __future__ import annotations

import json
import subprocess
import time
import uuid
from collections import Counter
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
    validate_snapshot, write_csv, write_json)
from .provider import BybitPublicClient, CandleStore, TIMEFRAMES, WARMUP_BARS, eligible_instruments, now_iso


def run_intraday(config, root: Path, *, refresh_cache=False, no_cache=False,
                 cancellation_token=None, client=None):
    token = cancellation_token or CancellationToken()
    settings = config.intraday
    output = root / "output/intraday"
    output.mkdir(parents=True, exist_ok=True)
    # Exclusive per-profile lock avoids concurrent publication/cache mutations.
    lock = output / ".run.lock"
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(now_iso())
    try:
        return _run(config, root, token, client or BybitPublicClient(config, token),
                    refresh_cache or settings.refresh_cache, no_cache)
    finally:
        lock.unlink(missing_ok=True)


def _run(config, root, token, client, refresh_cache, no_cache):
    start_clock, started = time.perf_counter(), now_iso()
    snapshot = started.replace(":", "").replace("-", "").replace("+0000", "Z") + "_" + uuid.uuid4().hex[:8]
    settings = config.intraday
    output = root / "output/intraday"
    staging = output / ".staging" / snapshot
    staging.mkdir(parents=True)
    print(f"INTRADAY snapshot={snapshot}", flush=True)
    timings = dict(discovery=0.0, ticker_snapshot=0.0, instrument_metadata=0.0,
        history_download=0.0, history_download_by_timeframe={}, final_refresh=0.0,
        feature_calculation=0.0, export=0.0, validation=0.0, total=0.0)
    failures = []
    store = CandleStore(root, client, token, config.cache.enabled and not no_cache, refresh_cache)

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
        eligible = eligible_instruments(instruments, initial, settings.min_turnover24h_usdt)
        timings["discovery"] = timings["instrument_metadata"] + timings["ticker_snapshot"]
        print(f"INTRADAY discovered={len(instruments)} eligible={len(eligible)} threshold={settings.min_turnover24h_usdt}", flush=True)
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

        stage = time.perf_counter()
        for key, future, progress in jobs(sorted(histories), lambda key: calculate(histories[key], token), "features"):
            try:
                calculated[key] = future.result()
            except CancellationRequested:
                raise
            except Exception as exc:
                errors[key] = str(exc)
                failure(*key, "features", exc)
                progress.warning(str(key), str(exc))
        timings["feature_calculation"] = time.perf_counter() - stage

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
                frame = calculate(frame, token, previous=calculated[key])
                return frame, stamp, network, time.perf_counter() - begin

            for item, future, progress in jobs(eligible, refresh, f"final refresh/export {tf}"):
                key = (item["symbol"], tf)
                frame = calculated.get(key)
                partial = False
                try:
                    result = future.result()
                    if result is not None:
                        frame, refreshed[key], _, _ = result
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
                        for name, value in dict(source="bybit", market="linear_perpetual", symbol=key[0],
                            instrument_name=item.get("displayName") or key[0], contract_type="LinearPerpetual",
                            dex="", quote_currency="USDT", turnover_currency="USDT", timeframe=tf).items():
                            frame[name] = value
                        closed = frame.loc[frame.is_closed]
                        current = frame.loc[frame.provisional]
                        gaps = int((frame.timestamp.diff().dropna() > pd.Timedelta(milliseconds=TIMEFRAMES[tf][1])).sum())
                        relative = Path("series") / safe_path_component(key[0]) / (tf + ".csv")
                        serialized = serialize(frame)
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
        universe = [universe_row(item, tickers[item["symbol"]], initial[item["symbol"]], snapshot,
            settings.min_turnover24h_usdt, metadata_at, ticker_at, eligibility_at) for item in eligible]
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
        manifest = dict(schema_version="1.0", builder_version="2.0", git_commit=commit, profile="intraday",
            snapshot_id=snapshot, started_at_utc=started, completed_at_utc=completed,
            final_refresh_at_utc=ticker_at, provider="bybit", market="linear_perpetual", quote_or_settle="USDT",
            liquidity_filter=dict(field="turnover24h", threshold_usdt=settings.min_turnover24h_usdt,
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
        applicable = sum((i.get("status"), i.get("contractType"), i.get("quoteCoin"), i.get("settleCoin")) ==
                         ("Trading", "LinearPerpetual", "USDT", "USDT") for i in instruments)
        report = dict(profile="intraday", snapshot_id=snapshot, started_at_utc=started, completed_at_utc=completed,
            config_summary=dict(cache_enabled=store.enabled, refresh_cache=store.refresh,
                concurrency=settings.concurrency, requests_per_second=settings.requests_per_second,
                use_system_proxy=settings.use_system_proxy, max_retries=settings.max_retries,
                request_timeout=settings.request_timeout),
            counts=dict(discovered=len(instruments), eligible=len(eligible), rejected_by_liquidity=applicable-len(eligible),
                ready_symbols=symbol_counts["READY"], partial_symbols=symbol_counts["PARTIAL"], failed_symbols=symbol_counts["FAILED"],
                successful_series=counts["READY"]+counts["PARTIAL"], partial_series=counts["PARTIAL"], failed_series=counts["FAILED"]),
            counts_by_timeframe={tf: dict(Counter(row["status"] for row in catalog if row["timeframe"] == tf)) for tf in timeframes},
            failures=failures, elapsed_seconds=timings, **client.metrics())
        write_csv(staging / "catalog.csv", catalog, CATALOG_COLUMNS)
        write_csv(staging / "universe.csv", universe, UNIVERSE_COLUMNS)
        write_csv(staging / "latest_features.csv", latest, LATEST_COLUMNS)
        consumer = (root / "docs/APX_INTRADAY_MARKETDATA_CONSUMER.md").read_text(encoding="utf-8")
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
        publish(staging, output, snapshot)
        print(f"INTRADAY published {snapshot} in {timings['total']:.1f}s", flush=True)
        return report
    except BaseException as exc:
        write_json(staging / "failure.json", dict(snapshot_id=snapshot, error_type=type(exc).__name__,
            reason=str(exc), failures=failures, elapsed_seconds=time.perf_counter()-start_clock, **client.metrics()))
        raise
