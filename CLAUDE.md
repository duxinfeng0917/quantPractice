# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment

Python environment is managed via **conda** (configured in `.vscode/settings.json`). Activate the appropriate conda env before running scripts.

## Running Scripts

```bash
python Demo1.py   # fetch US stock OHLCV via akshare (EastMoney backend, no API key)
python Demo2.py   # fetch stock data via yfinance (Yahoo Finance)
```

## Dependencies

- `akshare` — Chinese financial data library; used to pull US equities from EastMoney (东方财富). Resolves tickers to EastMoney format (e.g. `"MSFT"` → `"105.MSFT"`).
- `yfinance` — Yahoo Finance wrapper; used for both US and A-share tickers (e.g. `"600519.SS"` for Kweichow Moutai).
- `pandas` — implicit dependency of both data libraries.

## Architecture

This is an exploratory/practice repo with standalone scripts — no shared modules, no test suite, no package structure yet.

- **Demo1.py**: akshare-based fetcher. `get_us_symbol()` resolves plain tickers against the full EastMoney US-stock list; falls back to NASDAQ prefix `105`. `fetch_history()` calls `ak.stock_us_hist()` with split-adjusted (`qfq`) prices. Column names come back in Chinese and are mapped to standard labels at display time.
- **Demo2.py**: yfinance one-liner; downloads OHLCV for a given symbol and date range.

## Data Source Notes

- akshare pulls from EastMoney servers — may be slower or blocked outside mainland China.
- yfinance pulls from Yahoo Finance — works globally but subject to rate limits.
- Neither source requires an API key.
