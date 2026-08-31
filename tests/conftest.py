from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest


@pytest.fixture
def candle_frame() -> pd.DataFrame:
    rows = []
    start = datetime(2020, 1, 1, tzinfo=UTC)
    for index in range(1201):
        close = 100.0 + index * 0.1 + ((index % 7) - 3) * 0.01
        rows.append(
            {
                "timestamp": start + timedelta(days=index),
                "is_closed": index < 1200,
                "open": close - 0.2,
                "high": close + 1.0 + (2.0 if index in {100, 600, 1100} else 0.0),
                "low": close - 1.0 - (2.0 if index in {200, 700, 1050} else 0.0),
                "close": close,
            }
        )
    return pd.DataFrame(rows)

