from .export import UNIVERSE_COLUMNS as BYBIT_COLUMNS, universe_row as bybit_row

UNIVERSE_COLUMNS = tuple(c for c in BYBIT_COLUMNS if c != "liquidity_threshold_usdt") + (
    "source", "dex", "native_symbol", "collateral_token", "liquidity_threshold_usd",
    "sz_decimals", "max_price_decimals", "max_price_significant_figures", "integer_price_exception",
    "oracle_price", "mid_price", "context_fetched_at_utc", "book_fetched_at_utc", "book_time_ms",
    "turnover24h_usd", "eligibility_turnover24h_usd", "collateral_usd_rate", "eligibility_collateral_usd_rate")


def universe_row(item, ticker, initial, snapshot, threshold, metadata_at, ticker_at, eligibility_at):
    row = bybit_row(item, ticker, initial, snapshot, threshold, metadata_at, ticker_at, eligibility_at)
    row.pop("liquidity_threshold_usdt")
    row.update(source="hyperliquid", dex=item["dex"], native_symbol=item["symbol"],
        collateral_token=item["collateral_token"], liquidity_threshold_usd=threshold,
        sz_decimals=item["sz_decimals"], max_price_decimals=item["max_price_decimals"],
        max_price_significant_figures=5, integer_price_exception="true",
        oracle_price=ticker.get("indexPrice"), mid_price=ticker.get("mid_price"),
        context_fetched_at_utc=ticker.get("context_fetched_at_utc"),
        book_fetched_at_utc=ticker.get("book_fetched_at_utc"), book_time_ms=ticker.get("book_time_ms"),
        turnover24h_usd=ticker["turnover24h_usd"], eligibility_turnover24h_usd=initial["turnover24h_usd"],
        collateral_usd_rate=ticker["collateral_usd_rate"], eligibility_collateral_usd_rate=initial["collateral_usd_rate"])
    return {key: "N/A" if value is None else value for key, value in row.items()}
