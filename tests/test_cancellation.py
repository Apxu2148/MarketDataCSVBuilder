from __future__ import annotations

import threading
import signal
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError

import pytest

from market_data_csv_builder import cli
from market_data_csv_builder.cancellation import CancellationRequested, CancellationToken
from market_data_csv_builder.concurrency import BoundedExecutor
from market_data_csv_builder.config import AppConfig, CacheConfig, GeneralConfig, HttpConfig
from market_data_csv_builder.http import JsonHttpClient
from market_data_csv_builder.models import Instrument
from market_data_csv_builder.pipeline import run_pipeline


def test_cancellation_message_is_emitted_once(capsys) -> None:
    token = CancellationToken(
        lambda: print("Cancellation requested. Stopping active work...", flush=True)
    )
    assert token.request()
    assert not token.request()
    assert capsys.readouterr().out.count("Cancellation requested. Stopping active work...") == 1


def test_bounded_executor_stops_submitting_and_cancels_pending_work() -> None:
    token = CancellationToken()
    started: list[int] = []
    lock = threading.Lock()

    def work(item: int) -> int:
        with lock:
            started.append(item)
        while not token.wait(0.01):
            pass
        token.raise_if_requested()
        return item

    timer = threading.Timer(0.05, token.request)
    timer.start()
    try:
        with pytest.raises(CancellationRequested):
            with BoundedExecutor(
                range(100),
                work,
                max_workers=3,
                thread_name_prefix="cancel-test",
                cancellation_token=token,
            ) as jobs:
                while jobs.has_pending:
                    jobs.next_completed(timeout=0.02)
    finally:
        timer.cancel()
    assert 1 <= len(started) <= 3


def test_http_cancellation_prevents_retry_and_backoff(monkeypatch, tmp_path: Path) -> None:
    token = CancellationToken()
    attempts = 0

    def fake_urlopen(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        token.request()
        raise URLError("synthetic network interruption")

    monkeypatch.setattr("market_data_csv_builder.http.urlopen", fake_urlopen)
    client = JsonHttpClient(
        cache=CacheConfig(enabled=False),
        http=HttpConfig(timeout_seconds=1, max_retries=5, backoff_seconds=10),
        root=tmp_path,
        no_cache=True,
        cancellation_token=token,
    )
    with pytest.raises(CancellationRequested):
        client.get_json("https://example.invalid/test", {})
    assert attempts == 1


def test_http_reads_fresh_cache_entry_without_network(monkeypatch, tmp_path: Path) -> None:
    client = JsonHttpClient(
        cache=CacheConfig(enabled=True, ttl_hours=1),
        http=HttpConfig(timeout_seconds=1, max_retries=1),
        root=tmp_path,
    )
    url = "https://example.invalid/cached"
    cache_key = f"GET\n{url}"
    payload = {"universe": [{"name": "BTC"}]}
    client._write_cache(client._cache_path(cache_key), payload)

    def unexpected_urlopen(*_args, **_kwargs):
        raise AssertionError("fresh cache entry should avoid the network")

    monkeypatch.setattr("market_data_csv_builder.http.urlopen", unexpected_urlopen)
    assert client.get_json(url, {}) == payload


class BlockingSource:
    name = "blocking"
    max_workers = 2
    threshold = 1.0

    def __init__(self, token: CancellationToken) -> None:
        self.token = token
        self.started = 0
        self.lock = threading.Lock()

    def discover(self) -> list[Instrument]:
        return [
            Instrument("blocking", "perpetual", f"TEST{index}", f"TEST{index}", "Perpetual", "default", "USD")
            for index in range(50)
        ]

    def fetch_daily_candles(self, _instrument, _closed_bars, _as_of):
        with self.lock:
            self.started += 1
        while not self.token.wait(0.01):
            pass
        self.token.raise_if_requested()
        raise AssertionError("unreachable")


def test_pipeline_cancellation_keeps_current_and_does_not_queue_universe(tmp_path: Path) -> None:
    current = tmp_path / "output/current"
    current.mkdir(parents=True)
    (current / "marker.txt").write_text("published", encoding="utf-8")
    (current / "latest_snapshot.csv").write_text("old snapshot\n", encoding="utf-8")
    token = CancellationToken()
    source = BlockingSource(token)
    config = AppConfig(
        general=GeneralConfig(
            history_closed_bars=20,
            compact_closed_bars=5,
            liquidity_lookback_closed_bars=3,
            output_dir="output",
        )
    )
    timer = threading.Timer(0.05, token.request)
    timer.start()
    try:
        with pytest.raises(CancellationRequested):
            run_pipeline(
                config,
                tmp_path,
                selected_source="all",
                limit=None,
                refresh_cache=False,
                no_cache=True,
                as_of=datetime(2026, 8, 30, tzinfo=UTC),
                sources=[source],
                cancellation_token=token,
            )
    finally:
        timer.cancel()
    assert 1 <= source.started <= source.max_workers
    assert (current / "marker.txt").read_text(encoding="utf-8") == "published"
    assert (current / "latest_snapshot.csv").read_text(encoding="utf-8") == "old snapshot\n"
    assert (tmp_path / "output/_building").exists()


def test_cli_returns_130_for_cooperative_cancellation(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "load_config", lambda _path: AppConfig())

    def cancel_pipeline(*_args, **kwargs):
        token = kwargs["cancellation_token"]
        signal.raise_signal(signal.SIGINT)
        token.raise_if_requested()

    monkeypatch.setattr(cli, "run_pipeline", cancel_pipeline)
    assert cli.main(["--source", "hyperliquid", "--limit", "1"]) == 130
    assert "Cancellation requested. Stopping active work..." in capsys.readouterr().out
