.PHONY: monitor trader all stop status log-monitor log-trader backfill install lint

# ── 启动 ─────────────────────────────────────────────────────
monitor:
	bash start.sh monitor

trader:
	bash start.sh trader

all:
	bash start.sh all

# ── 停止 / 状态 ───────────────────────────────────────────────
stop:
	bash start.sh stop

status:
	bash start.sh status

# ── 日志 ─────────────────────────────────────────────────────
log-monitor:
	bash start.sh log monitor

log-trader:
	bash start.sh log trader

# ── 数据 ─────────────────────────────────────────────────────
backfill:
	python3 short_squeeze_monitor.py backfill

signals:
	python3 short_squeeze_monitor.py signals

export:
	python3 short_squeeze_monitor.py export && mv *.csv data/exports/ 2>/dev/null || true

# ── 开发 ─────────────────────────────────────────────────────
install:
	pip install -r requirements.txt

lint:
	ruff check .

typecheck:
	mypy short_squeeze_monitor.py paper_trader.py short_position_manager.py
