from __future__ import annotations

import argparse
import logging
import signal
from pathlib import Path
from typing import Sequence

from .cancellation import CancellationRequested, CancellationToken
from .config import load_config
from .pipeline import run_pipeline
from .utils import parse_as_of


def build_parser(root: Path) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build daily MOEX, Bybit and Hyperliquid OHLCV + 29-feature CSV snapshots."
    )
    parser.add_argument("--config", type=Path, default=root / "config.toml")
    parser.add_argument("--source", choices=("all", "moex", "bybit", "hyperliquid"), default="all")
    parser.add_argument("--limit", type=int, help="Maximum instruments per selected source (smoke/performance testing).")
    parser.add_argument("--refresh-cache", action="store_true", help="Ignore cached reads and replace cache entries.")
    parser.add_argument("--no-cache", action="store_true", help="Disable cache reads and writes for this run.")
    parser.add_argument("--as-of", help="ISO date or timezone-aware ISO datetime for reproducible historical testing.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    parser = build_parser(root)
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    if args.refresh_cache and args.no_cache:
        parser.error("--refresh-cache and --no-cache are mutually exclusive")
    cancellation_token = CancellationToken(
        lambda: print("Cancellation requested. Stopping active work...", flush=True)
    )
    previous_sigint = signal.getsignal(signal.SIGINT)

    def handle_sigint(_signum, _frame) -> None:
        cancellation_token.request()

    signal.signal(signal.SIGINT, handle_sigint)
    try:
        config_path = args.config if args.config.is_absolute() else (Path.cwd() / args.config)
        config = load_config(config_path.resolve())
        logging.basicConfig(
            level=getattr(logging, config.general.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        report = run_pipeline(
            config,
            root,
            selected_source=args.source,
            limit=args.limit,
            refresh_cache=args.refresh_cache,
            no_cache=args.no_cache,
            as_of=parse_as_of(args.as_of),
            cancellation_token=cancellation_token,
        )
    except CancellationRequested:
        return 130
    except KeyboardInterrupt:
        cancellation_token.request()
        return 130
    except Exception:
        logging.getLogger(__name__).exception("Build failed")
        return 1
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
    _print_summary(report)
    return 0


def _print_summary(report: dict[str, object]) -> None:
    print("\nRun summary")
    print("source        discovered  passed  READY  below  dl_failed  feature_failed")
    sources = report["sources"]
    assert isinstance(sources, dict)
    for name, raw in sources.items():
        metrics = raw
        assert isinstance(metrics, dict)
        print(
            f"{name:<13} {metrics['discovered']:>10}  {metrics['passed_liquidity_filter']:>6}  "
            f"{metrics['READY']:>5}  {metrics['below_threshold']:>5}  "
            f"{metrics['download_failed']:>9}  {metrics['feature_failed']:>14}"
        )
    rate_limited = [
        (name, metrics)
        for name, metrics in sources.items()
        if isinstance(metrics, dict) and "requests_sent" in metrics
    ]
    if rate_limited:
        print("\nAPI rate limiting")
        for name, metrics in rate_limited:
            print(
                f"  {name}: requests_sent={metrics['requests_sent']}, "
                f"estimated_api_weight={metrics['estimated_api_weight']}, "
                f"rate_limit_wait_seconds={float(metrics['rate_limit_wait_seconds']):.3f}, "
                f"http_429_count={metrics['http_429_count']}"
            )
    elapsed = report["elapsed_seconds"]
    assert isinstance(elapsed, dict)
    print("\nElapsed seconds")
    for key in ("discovery", "liquidity_stage", "full_history_download", "feature_calculation", "export", "total"):
        print(f"  {key}: {float(elapsed[key]):.3f}")
    print(f"Snapshot: {report['snapshot_id']}")
    print(f"Current:  {report['current_snapshot_path']}")


if __name__ == "__main__":
    raise SystemExit(main())
