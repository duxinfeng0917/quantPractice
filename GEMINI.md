# MINIMAX-W / Multi-Stock HK Short Squeeze Monitoring & Trading System

This project is a quantitative trading tool for Hong Kong stock **short-selling arbitrage**. It monitors short-selling ratios, capital flows, and order book depth to identify short-squeeze risks and entry points, managing simulated or real positions via a paper-trading bot.

## Project Overview

- **Purpose**: Identify short-squeeze risks and short-selling opportunities for HK stocks using four proxy signals (HKEX ordinary account has no direct access to borrow rates or lending pool).
- **Supported Stocks**: Configurable via `shared_config.py`; currently `00100` (MINIMAX-W) and `02513` (质谱). Add new stocks by appending to the `STOCKS` dict.
- **Main Technologies**:
  - **Python 3.11+**: Core logic and data processing.
  - **Futu OpenD API**: Real-time market data (Level 2 quotes, capital distribution).
  - **HKEX Scraping**: Daily short-selling data retrieval from `www.hkex.com.hk`.
  - **SQLite**: Persistent storage (`short_data.db` per stock).
  - **Pandas**: Data analysis and indicator calculation.
  - **Bash/Makefile**: Automation and process management.

### Architecture

```
HKEX website ──scrape──► short_squeeze_monitor.py ──write──► short_data.db
Futu OpenD   ──API────►        (runs continuously)               │
                                                          ┌───────┘
                                                          ▼
                         paper_trader.py ◄── monitor_state table (scores)
                         short_position_manager.py ◄── hkex_daily (cost line)
```

1. **Monitor (`short_squeeze_monitor.py`)**: Real-time engine tracking four signal categories. Supports `--stock <code>` for multi-stock operation. Writes current squeeze/short scores to `monitor_state` DB table every cycle.
   - **HKEX Short Ratio**: Daily congestion indicator (scrapes `<pre>`-formatted file via `#short_selling` anchor, not HTML tables).
   - **Capital Flow**: Large order net inflow/outflow via Futu `get_capital_distribution()` (returns cumulative daily HKD, not 万HKD).
   - **Order Book Depth**: Ask/Bid imbalance and depth shrinkage via `get_order_book()`.
   - **Historical Momentum**: Weighted short cost-basis vs. current price from `hkex_daily` history.
2. **Paper Trader (`paper_trader.py`)**: Automated simulated-trading bot. Reads `monitor_state` every 15s, executes paper orders when scoring conditions are met. Thresholds hot-reload from `config/trader_config.json` every 60s.
3. **Position Manager (`short_position_manager.py`)**: Manages active short positions, calculates PnL, and provides exit signals. State persists across restarts via `short_position.json`.
4. **Data Layer**: Per-stock SQLite database (path from `shared_config.STOCKS`), tables: `hkex_daily`, `capital_flow`, `orderbook_snapshots`, `price_history`, `signals`, `monitor_state`.

---

## Building and Running

### Prerequisites

- **Futu OpenD**: Must be installed and running locally on port `11111`.
- **Python Environment**: Python 3.11+ recommended; managed via conda.
- **Network**: Must be able to reach `www.hkex.com.hk`.

### Key Commands

| Task | Command |
| :--- | :--- |
| **Install Dependencies** | `make install` or `pip install -r requirements.txt` |
| **Initial Data Backfill** | `make backfill` (fetches last 10 trading days of HKEX data) |
| **Start Monitor + Trader** | `make all` or `bash start.sh all` |
| **Start Monitor Only** | `make monitor` or `bash start.sh monitor` |
| **Start Paper Trader Only** | `make trader` or `bash start.sh trader` |
| **Check Status** | `make status` or `bash start.sh status` |
| **Stop Processes** | `make stop` or `bash start.sh stop` |
| **View Logs** | `make log-monitor` / `make log-trader` |
| **Different Stock** | `STOCK=02513 bash start.sh all` |
| **Dry-run Mode** | `bash start.sh all --dry-run` |
| **Linting** | `make lint` (uses Ruff) |

---

## Development Conventions

- **Language**: Python 3.11+ with type hints.
- **Style & Linting**: Adheres to `ruff` rules (configured in `pyproject.toml`).
- **Configuration**:
  - Environment variables (e.g., `FUTU_TRADE_PWD`) in `.env` (copied from `config/example.env`).
  - Trading logic parameters in `config/trader_config.json` (supports hot-reloading every 60s).
  - Stock registry in `shared_config.py` (STOCKS dict + shared constants).
- **Data Persistence**:
  - **Database**: Per-stock SQLite (path from `shared_config.STOCKS[code]["db_path"]`).
  - **Snapshots**: `short_position.json` stores current position state for recovery.
- **Logging**: All logs written to `logs/` directory, rotated daily.
- **Process Management**: `start.sh` manages `nohup` background processes; supports per-stock isolation.

## Key Files

| File | Purpose |
| :--- | :--- |
| `short_squeeze_monitor.py` | Signal generation, squeeze/entry scoring, DB writer |
| `paper_trader.py` | Automated simulated-trading bot |
| `short_position_manager.py` | Standalone CLI for managing live short positions |
| `shared_config.py` | Stock registry (`STOCKS` dict) + shared constants |
| `start.sh` | Daily operations script (start/stop/status/log) |
| `Makefile` | Shortcut aliases for common `start.sh` / script commands |
| `config/trader_config.json` | Tunable thresholds for the trading algorithm (hot-reload) |
| `config/example.env` | Environment variable template |
