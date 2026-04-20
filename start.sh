#!/usr/bin/env bash
# ============================================================
# MINIMAX-W 做空系统 — 每日启动脚本
# 用法：
#   bash start.sh monitor                      # 后台启动逼空监控器
#   bash start.sh trader                       # 后台启动模拟交易（动态目标价）
#   bash start.sh trader --dry-run             # dry-run 模式（只看信号不下单）
#   bash start.sh trader --qty 500             # 指定最大仓位
#   bash start.sh all                          # 同时启动 monitor + trader
#   bash start.sh all --dry-run                # 同时启动，trader 用 dry-run
#   bash start.sh stop                         # 停止所有相关进程
#   bash start.sh status                       # 查看进程状态 + 今日日志
#   bash start.sh log [monitor|trader]         # 实时查看日志（tail -f）
# ============================================================

set -euo pipefail

# 自动加载 .env 文件（若存在）
if [[ -f ".env" ]]; then
  set -a
  source .env
  set +a
fi

DATE=$(date +%Y%m%d)
LOG_DIR="logs"
mkdir -p "$LOG_DIR"

MONITOR_SCRIPT="short_squeeze_monitor.py"
TRADER_SCRIPT="paper_trader.py"
MONITOR_NOHUP="$LOG_DIR/monitor_stdout_${DATE}.log"
TRADER_NOHUP="$LOG_DIR/trader_stdout_${DATE}.log"

# ── 辅助函数 ──────────────────────────────────────────────
start_monitor() {
  if pgrep -f "$MONITOR_SCRIPT" > /dev/null 2>&1; then
    echo "[警告] monitor 已在运行，PID: $(pgrep -f $MONITOR_SCRIPT)"
    echo "       如需重启，先运行: bash start.sh stop"
    return 1
  fi
  nohup python3 -u "$MONITOR_SCRIPT" >> "$MONITOR_NOHUP" 2>&1 &
  echo "[启动] monitor  PID=$!"
  echo "       stdout  → $MONITOR_NOHUP"
  echo "       日志    → $LOG_DIR/short_monitor_${DATE}.log"
}

start_trader() {
  # $@ 接收额外参数（如 --dry-run --qty 500）
  if pgrep -f "$TRADER_SCRIPT" > /dev/null 2>&1; then
    echo "[警告] trader 已在运行，PID: $(pgrep -f $TRADER_SCRIPT)"
    echo "       如需重启，先运行: bash start.sh stop"
    return 1
  fi

  # 检查 trader_config.json 是否存在，给出提示
  if [[ ! -f "trader_config.json" ]]; then
    echo "[提示] 未找到 trader_config.json，将使用代码默认阈值"
    echo "       可复制模板：cp trader_config.json.example trader_config.json"
  fi

  nohup python3 -u "$TRADER_SCRIPT" "$@" >> "$TRADER_NOHUP" 2>&1 &
  echo "[启动] trader   PID=$!"
  echo "       参数    → $*"
  echo "       stdout  → $TRADER_NOHUP"
  echo "       日志    → $LOG_DIR/paper_trader_${DATE}.log"
  echo "       配置    → trader_config.json (热更新，修改后60秒内生效)"
}

# ── 主命令分支 ────────────────────────────────────────────
case "${1:-}" in

  monitor)
    start_monitor
    ;;

  trader)
    shift
    start_trader "$@"
    ;;

  all)
    # 同时启动 monitor 和 trader，trader 透传额外参数
    shift
    echo "=== 启动 monitor ==="
    start_monitor || true
    echo ""
    echo "=== 启动 trader ==="
    start_trader "$@" || true
    echo ""
    echo "=== 启动完成，查看状态 ==="
    sleep 1
    bash "$0" status
    ;;

  stop)
    echo "[停止] 终止所有相关进程..."
    pkill -f "$MONITOR_SCRIPT" && echo "  monitor 已终止" || echo "  monitor 未运行"
    pkill -f "$TRADER_SCRIPT"  && echo "  trader  已终止" || echo "  trader  未运行"
    ;;

  status)
    echo "=== 进程状态 ==="
    if pgrep -fa "$MONITOR_SCRIPT" 2>/dev/null; then
      : # pgrep 已打印
    else
      echo "  monitor: 未运行"
    fi
    if pgrep -fa "$TRADER_SCRIPT" 2>/dev/null; then
      :
    else
      echo "  trader:  未运行"
    fi
    echo ""
    echo "=== 今日日志文件 ($DATE) ==="
    ls -lh "$LOG_DIR"/*"${DATE}"* 2>/dev/null || echo "  (暂无今日日志)"
    ;;

  log)
    # 实时查看日志
    TARGET="${2:-monitor}"
    case "$TARGET" in
      monitor) tail -f "$MONITOR_NOHUP" ;;
      trader)  tail -f "$TRADER_NOHUP"  ;;
      *)
        echo "用法: bash start.sh log [monitor|trader]"
        exit 1
        ;;
    esac
    ;;

  *)
    echo "用法: bash start.sh <命令> [参数]"
    echo ""
    echo "命令:"
    echo "  monitor              后台启动逼空监控器"
    echo "  trader [参数]        后台启动模拟交易机器人"
    echo "  all [trader参数]     同时启动 monitor + trader"
    echo "  stop                 停止所有相关进程"
    echo "  status               查看进程状态 + 今日日志"
    echo "  log [monitor|trader] 实时查看日志 (tail -f)"
    echo ""
    echo "trader 可用参数:"
    echo "  --dry-run            只打印信号，不实际下单"
    echo "  --qty <股数>         最大仓位（默认 1000）"
    echo "  --stop <价格>        固定止损价（默认动态 +4%）"
    echo "  --target1 <价格>     固定第一目标价（默认动态 -1.5%）"
    echo "  --target2 <价格>     固定第二目标价（默认动态 -3%）"
    echo ""
    echo "示例:"
    echo "  bash start.sh all                    # 开盘前一键启动"
    echo "  bash start.sh all --dry-run          # 测试模式"
    echo "  bash start.sh trader --qty 500       # 半仓上限"
    echo "  bash start.sh log monitor            # 实时监控日志"
    echo "  bash start.sh stop                   # 收盘后停止"
    exit 1
    ;;

esac
