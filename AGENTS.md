# Agent Instructions

This repository is the independent, deliberately small daily CSV builder for MOEX, Bybit and Hyperliquid.

- Reference projects are `C:\Python\MOEXPortfolioBuilder`, `C:\Python\BybitPortfolioBuilder`, `C:\Python\HyperliquidPortfolioBuilder`, and `C:\Python\MarketDataVault`; treat all four as strictly read-only.
- Use Python 3.11 and install dependencies only into this project's `venv` via `setup.bat` or `venv\Scripts\python.exe -m pip`.
- Never install packages globally and never perform destructive or state-changing Git/GitHub operations without an explicit user command.
- Preserve the v1 scope: discovery -> closed-bar liquidity filter -> 1D OHLCV download -> exactly 29 MarketDataVault features -> CSV snapshot. Do not add UI, database, DuckDB, server/API, other timeframes, portfolio logic, Google integrations, or extra APX indicators.
- Preserve anti-look-ahead, current-candle provisional semantics, metadata-based Bybit contract typing, Hyperliquid multi-DEX isolation, source-specific turnover semantics, deterministic structural JSON, partial-failure isolation, and `_building`/`current`/`previous` publication.
- Keep long live runs observable: flushed progress/heartbeat at least every 10-15 seconds, stage/source/counters/elapsed summaries, and immediate short per-symbol warnings.
- Run the offline suite after substantive changes. Keep live tests separate and update `PROJECT_STATE.md` after meaningful offline/live/performance runs.

Ready means the offline suite passes; small live smoke has covered MOEX, Bybit USDT+USDC and Hyperliquid default+additional DEX where the live universe permits it; full/compact/catalog/status/partial failure are verified; and current run evidence plus known issues are recorded in `PROJECT_STATE.md`.

