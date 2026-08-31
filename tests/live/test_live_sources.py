from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from market_data_csv_builder.config import load_config
from market_data_csv_builder.sources import BybitSource, HyperliquidSource, MoexSource


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("MARKET_DATA_CSV_BUILDER_LIVE") != "1", reason="set MARKET_DATA_CSV_BUILDER_LIVE=1"),
]


@pytest.mark.parametrize("source_type", [MoexSource, BybitSource, HyperliquidSource])
def test_live_discovery_and_one_daily_series(source_type) -> None:
    root = Path(__file__).resolve().parents[2]
    config = load_config(root / "config.toml")
    source = source_type(config, root, no_cache=True)
    instruments = source.discover()
    assert instruments
    candles = source.fetch_daily_candles(instruments[0], 3, datetime.now(UTC))
    assert candles
    assert all(item.high >= max(item.open, item.close, item.low) for item in candles)
