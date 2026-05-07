#!/usr/bin/env bash
# ============================================================
# 做空系统 — 每日启动脚本
#
# 股票选择（任选一种方式）：
#   1. 修改本文件顶部 STOCK 变量（永久默认）
#   2. 环境变量临时覆盖：STOCK=02513 bash start.sh all
#
# 用法：
#   bash start.sh monitor                      # 后台启动逼空监控器
#   bash start.sh trader                       # 后台启动模拟交易（动态目标价）
#   bash start.sh trader --dry-run             # dry-run 模式（只看信号不下单）
#   bash start.sh trader --qty 500             # 指定最大仓位
#   bash start.sh all                          # 同时启动 monitor + trader
#   bash start.sh all --dry-run                # 同时启动，trader 用 dry-run
#   bash start.sh stop                         # 停止当前 STOCK 的相关进程
#   bash start.sh stop all                     # 停止所有股票的相关进程
#   bash start.sh status                       # 查看进程状态 + 今日日志
#   bash start.sh log [monitor|trader]         # 实时查看日志（tail -f）
#
# 示例（多股票）：
#   STOCK=02513 bash start.sh all --dry-run    # 智谱AI dry-run
#   STOCK=00100 bash start.sh all              # MINIMAX-W 正式启动
# ============================================================

set -euo pipefail

# ── 股票配置（修改此处或用 STOCK= 环境变量覆盖）──────────────
STOCK="${STOCK:-00100}"

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
# 日志文件名含股票代码，支持多股票同时运行不串日志
MONITOR_NOHUP="$LOG_DIR/monitor_stdout_${STOCK}_${DATE}.log"
TRADER_NOHUP="$LOG_DIR/trader_stdout_${STOCK}_${DATE}.log"

PYTHON="/Users/duxinfeng/miniconda3/envs/finance_env/bin/python"

# ── 辅助函数 ──────────────────────────────────────────────
start_monitor() {
  # 检查同一股票的 monitor 是否已在运行
  if pgrep -f "${MONITOR_SCRIPT}.*--stock ${STOCK}\|${MONITOR_SCRIPT}.*${STOCK}" > /dev/null 2>&1; then
    echo "[警告] monitor(${STOCK}) 已在运行，PID: $(pgrep -f "${MONITOR_SCRIPT}.*${STOCK}")"
    echo "       如需重启，先运行: bash start.sh stop"
    return 1
  fi
  nohup "$PYTHON" -u "$MONITOR_SCRIPT" --stock "$STOCK" >> "$MONITOR_NOHUP" 2>&1 &
  echo "[启动] monitor(${STOCK})  PID=$!"
  echo "       stdout  → $MONITOR_NOHUP"
  echo "       日志    → $LOG_DIR/short_monitor_${DATE}.log"
}

start_trader() {
  # $@ 接收额外参数（如 --dry-run --qty 500）
  if pgrep -f "${TRADER_SCRIPT}.*--stock ${STOCK}\|${TRADER_SCRIPT}.*${STOCK}" > /dev/null 2>&1; then
    echo "[警告] trader(${STOCK}) 已在运行，PID: $(pgrep -f "${TRADER_SCRIPT}.*${STOCK}")"
    echo "       如需重启，先运行: bash start.sh stop"
    return 1
  fi

  if [[ ! -f "config/trader_config.json" ]]; then
    echo "[提示] 未找到 config/trader_config.json，将使用代码默认阈值"
  fi

  nohup "$PYTHON" -u "$TRADER_SCRIPT" --stock "$STOCK" "$@" >> "$TRADER_NOHUP" 2>&1 &
  echo "[启动] trader(${STOCK})   PID=$!"
  echo "       参数    → --stock $STOCK $*"
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
    shift
    echo "=== 启动 monitor(${STOCK}) ==="
    start_monitor || true
    echo ""
    echo "=== 启动 trader(${STOCK}) ==="
    start_trader "$@" || true
    echo ""
    echo "=== 启动完成，查看状态 ==="
    sleep 1
    bash "$0" status
    ;;

  stop)
    if [[ "${2:-}" == "all" ]]; then
      echo "[停止] 终止所有股票的相关进程..."
      pkill -f "$MONITOR_SCRIPT" && echo "  monitor (all) 已终止" || echo "  monitor 未运行"
      pkill -f "$TRADER_SCRIPT"  && echo "  trader  (all) 已终止" || echo "  trader  未运行"
    else
      echo "[停止] 终止 ${STOCK} 相关进程..."
      pkill -f "${MONITOR_SCRIPT}.*${STOCK}" && echo "  monitor(${STOCK}) 已终止" || echo "  monitor(${STOCK}) 未运行"
      pkill -f "${TRADER_SCRIPT}.*${STOCK}"  && echo "  trader(${STOCK})  已终止" || echo "  trader(${STOCK})  未运行"
    fi
    ;;

  status)
    echo "=== 进程状态（当前 STOCK=${STOCK}）==="
    if pgrep -fa "$MONITOR_SCRIPT" 2>/dev/null; then
      :
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
    echo "用法: STOCK=<代码> bash start.sh <命令> [参数]"
    echo ""
    echo "股票配置（任选其一）:"
    echo "  修改脚本顶部  STOCK=\"00100\"          永久默认"
    echo "  环境变量      STOCK=02513 bash start.sh all   临时覆盖"
    echo ""
    echo "命令:"
    echo "  monitor              后台启动逼空监控器"
    echo "  trader [参数]        后台启动模拟交易机器人"
    echo "  all [trader参数]     同时启动 monitor + trader"
    echo "  stop                 停止当前 STOCK 的相关进程"
    echo "  stop all             停止所有股票的相关进程"
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
    echo "  bash start.sh all                          # 00100 一键启动"
    echo "  STOCK=02513 bash start.sh all              # 智谱AI 一键启动"
    echo "  STOCK=02513 bash start.sh all --dry-run    # 智谱AI 测试模式"
    echo "  bash start.sh all --dry-run                # 00100 测试模式"
    echo "  bash start.sh log monitor                  # 实时监控日志"
    echo "  bash start.sh stop                         # 停止当前股票"
    echo "  bash start.sh stop all                     # 停止全部"
    exit 1
    ;;

esac
