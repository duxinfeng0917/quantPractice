# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment

Python environment is managed via **conda** (configured in `.vscode/settings.json`). Activate the appropriate conda env before running scripts.

## Running Scripts

```bash
# 做空套利系统（需 Futu OpenD 运行）
bash start.sh all                        # 一键后台启动 monitor + paper_trader（默认股票 00100）
STOCK=02513 bash start.sh all            # 智谱AI（02513）启动
bash start.sh status                     # 查看进程状态
bash start.sh stop                       # 停止当前股票进程

python3 short_squeeze_monitor.py backfill   # 补抓历史 HKEX 卖空数据（首次运行必做）
python3 short_squeeze_monitor.py            # 启动逼空监控 + 做空信号（默认 00100）
python3 short_squeeze_monitor.py --stock 02513  # 指定股票
python3 short_squeeze_monitor.py signals    # 查看近期触发信号
python3 short_squeeze_monitor.py export     # 导出数据 CSV

python3 short_position_manager.py --entry 897 --qty 1000 --stop 950  # 启动持仓管理器（00100）
python3 short_position_manager.py --stock 02513 --entry 50 --qty 1000  # 切换股票
python3 short_position_manager.py --cover --cover-qty 500 --cover-price 870  # 记录平仓
```

## Dependencies

- Python 3.11+（与 `pyproject.toml` 一致）
- `futu-api` — Futu OpenAPI SDK; requires Futu OpenD running locally on `127.0.0.1:11111`.
- `requests`, `lxml` — used by the HKEX scraper in `short_squeeze_monitor.py`.
- `pandas` — implicit dependency of all data scripts.

完整依赖见 `requirements.txt` / `pyproject.toml`，可用 `make install` 安装。

## Architecture

HK short-selling system: `short_squeeze_monitor.py` + `paper_trader.py` + `short_position_manager.py`, orchestrated by `start.sh`.

**Data flow:**
```
HKEX website ──scrape──► short_squeeze_monitor.py ──write──► short_data.db
Futu OpenD   ──API────►        (runs continuously)               │
                                                          ┌───────┘
                                                          ▼
                         paper_trader.py ◄── monitor_state table (scores)
                         short_position_manager.py ◄── hkex_daily (cost line)
```

- **`short_squeeze_monitor.py`**: four-signal squeeze detector; supports `--stock <code>` for multi-stock. `scrape_hkex_short()` parses the `<pre>`-formatted Daily Quotations file from HKEX (URL: `d{YYMMDD}e.htm`) via the `#short_selling` anchor — not HTML tables. Futu `get_capital_distribution()` and `get_order_book()` provide intraday signals. Two separate scoring engines: squeeze risk (0–100) and short entry (0–100). Writes current scores to `monitor_state` table for `paper_trader.py` to read.

- **`paper_trader.py`**: automated simulated-trading bot. Reads `monitor_state` from DB every 15s, executes paper orders when scoring conditions are met. Thresholds hot-reload from `config/trader_config.json` every 60s.

- **`short_position_manager.py`**: standalone P&L tracker. Supports `--stock <code>`. Reads the per-stock DB for the cost line, polls Futu every 30s for price/orderbook/capital flow, and runs `evaluate_cover()` to score five cover triggers. Position state persists across restarts via `short_position_<code>.json`.

- **`shared_config.py`**: STOCKS dict (per-stock symbol, db_path, poll_interval) + shared constants. Add new stocks here — all three main scripts read from this.

- **DB files**: per-stock SQLite (`short_data.db` for `00100`, `short_data_02513.db` for `02513`); tables: `hkex_daily`, `capital_flow`, `orderbook_snapshots`, `price_history`, `signals`, `monitor_state`, `paper_trades`.

## Data Source Notes

- HKEX short sell data updates after **17:00 HKT** each trading day; intraday value is N/A.
- Futu `get_capital_distribution()` returns **cumulative daily HKD values** (not 万HKD) — divide by 10,000 for display.
- Futu ordinary accounts cannot access real-time securities lending pool or borrowing rates; the four proxy signals substitute for those.
