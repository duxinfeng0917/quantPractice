# MINIMAX-W (00100.HK) Short Squeeze Monitoring & Trading System

This project is a specialized quantitative trading tool designed for Hong Kong stock **short-selling arbitrage**, specifically targeting **MINIMAX-W (00100.HK)**. It monitors short-selling ratios, capital flows, and order book depth to identify short-squeeze risks and entry points, while managing simulated or real positions.

## Project Overview

- **Purpose**: Identify short-squeeze risks and short-selling opportunities for 00100.HK using multi-source signals.
- **Main Technologies**:
  - **Python 3.11+**: Core logic and data processing.
  - **Futu OpenD API**: Real-time market data (Level 2 quotes, capital distribution).
  - **HKEX Scraping**: Daily short-selling data retrieval.
  - **SQLite**: Persistent storage for historical data and signals (`short_data.db`).
  - **Pandas**: Data analysis and indicator calculation.
  - **Bash/Makefile**: Automation and process management.

### Architecture

1.  **Monitor (`short_squeeze_monitor.py`)**: A real-time engine that tracks four signal categories:
    - **HKEX Short Ratio**: Daily congestion indicator.
    - **Capital Flow**: Large order net inflow/outflow.
    - **Order Book Depth**: Ask/Bid imbalance and depth shrinkage.
    - **Historical Momentum**: Short cost-basis vs. current price.
2.  **Position Manager (`short_position_manager.py`)**: Manages active short positions, calculates PnL, and provides exit signals.
3.  **Paper Trader (`paper_trader.py`)**: A simulated trading bot that executes trades based on monitor signals and configuration.
4.  **Data Layer**: SQLite database (`short_data.db`) shared across processes.

---

## Building and Running

### Prerequisites

- **Futu OpenD**: Must be installed and running locally on port `11111`.
- **Python Environment**: Python 3.11+ recommended.

### Key Commands

| Task | Command |
| :--- | :--- |
| **Install Dependencies** | `make install` or `pip install -r requirements.txt` |
| **Initial Data Backfill** | `make backfill` (Fetches last 10 days of HKEX data) |
| **Start Monitor** | `make monitor` (Runs in background via `start.sh`) |
| **Start Paper Trader** | `make trader` (Runs in background via `start.sh`) |
| **Check Status** | `make status` |
| **Stop All Processes** | `make stop` |
| **View Logs** | `make log-monitor` or `make log-trader` |
| **Linting** | `make lint` (Uses Ruff) |

---

## Development Conventions

- **Language**: Python 3.11+ with type hints.
- **Style & Linting**: Adheres to `ruff` rules (configured in `pyproject.toml`).
- **Configuration**:
  - Environment variables (e.g., `FUTU_TRADE_PWD`) in `.env` (copied from `config/example.env`).
  - Trading logic parameters in `config/trader_config.json` (supports hot-reloading).
- **Data Persistence**:
  - **Database**: `short_data.db` (SQLite) stores HKEX history, capital flows, and signals.
  - **Snapshots**: `short_position.json` stores current position state for recovery.
- **Logging**: All logs are written to the `logs/` directory, rotated daily.
- **Process Management**: Uses `start.sh` to manage `nohup` processes for background operation.

## Key Files

- `short_squeeze_monitor.py`: The heart of the signal generation system.
- `short_position_manager.py`: Standalone CLI for managing short positions.
- `paper_trader.py`: Automated bot for simulated trading.
- `start.sh`: Robust shell script for daily operations.
- `Makefile`: Convenient shortcuts for common tasks.
- `config/trader_config.json`: Tunable thresholds for the trading algorithm.
