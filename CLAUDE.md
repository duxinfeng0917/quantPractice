# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment

Python environment is managed via **conda** (configured in `.vscode/settings.json`). Activate the appropriate conda env before running scripts.

## Running Scripts

```bash
# 数据拉取（Demo）
python Demo1.py   # fetch US stock OHLCV via akshare (EastMoney backend, no API key)
python Demo2.py   # fetch stock data via yfinance (Yahoo Finance)

# 做空套利系统（需 Futu OpenD 运行）
python3 short_squeeze_monitor.py backfill   # 补抓历史 HKEX 卖空数据（首次运行必做）
python3 short_squeeze_monitor.py            # 启动逼空监控 + 做空信号
python3 short_squeeze_monitor.py signals    # 查看近期触发信号
python3 short_squeeze_monitor.py export     # 导出数据 CSV

python3 short_position_manager.py --entry 897 --qty 1000 --stop 950  # 启动持仓管理器
python3 short_position_manager.py --cover --cover-qty 500 --cover-price 870  # 记录平仓
```

## Dependencies

- `akshare` — Chinese financial data library; used to pull US equities from EastMoney (东方财富).
- `yfinance` — Yahoo Finance wrapper; used for both US and A-share tickers.
- `futu-api` — Futu OpenAPI SDK; requires Futu OpenD running locally on `127.0.0.1:11111`.
- `requests`, `lxml` — used by the HKEX scraper in `short_squeeze_monitor.py`.
- `pandas` — implicit dependency of all data scripts.

## Architecture

Two layers: data demo scripts (`Demo*.py`) and a HK short-selling system (`short_squeeze_monitor.py` + `short_position_manager.py`).

**Short-selling system data flow:**
```
HKEX website ──scrape──► short_squeeze_monitor.py ──write──► short_data.db
Futu OpenD   ──API────►        (runs continuously)               │
                                                                  │
                         short_position_manager.py ◄──read───────┘
                               (runs after entry)
```

- **`short_squeeze_monitor.py`**: four-signal squeeze detector. `scrape_hkex_short()` parses the `<pre>`-formatted Daily Quotations file from HKEX (URL: `d{YYMMDD}e.htm`) via the `#short_selling` anchor — not HTML tables. Futu `get_capital_distribution()` and `get_order_book()` provide intraday signals. `analyze_hkex_short_momentum()` computes weighted short cost basis and momentum ratio from DB history. Two separate scoring engines: squeeze risk (0–100) and short entry (0–100).

- **`short_position_manager.py`**: standalone P&L tracker. Reads `short_data.db` for the cost line, polls Futu every 30s for price/orderbook/capital flow, and runs `evaluate_cover()` to score five cover triggers. Position state persists across restarts via `short_position.json`.

- **`short_data.db`**: shared SQLite; tables: `hkex_daily`, `capital_flow`, `orderbook_snapshots`, `price_history`, `signals`.

## Data Source Notes

- HKEX short sell data updates after **17:00 HKT** each trading day; intraday value is N/A.
- Futu `get_capital_distribution()` returns **cumulative daily HKD values** (not 万HKD) — divide by 10,000 for display.
- Futu ordinary accounts cannot access real-time securities lending pool or borrowing rates; the four proxy signals substitute for those.
- akshare pulls from EastMoney — may be slow outside mainland China. yfinance pulls from Yahoo Finance — global but rate-limited.
