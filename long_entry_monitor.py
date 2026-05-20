"""
long_entry_monitor.py  (做多入场监控 v1)
==========================================
针对不支持做空的港股（如 思格新能 06656.HK），通过实时盘口分析判断买入时机。

四路评分维度（满分 90）：
  ① 大单净流入加速 (30)     —— 累计为正且近 K 轮 Δ 持续放大（机构持续吸筹）
  ② 摆盘失衡度持续为正 (20) —— 连续 N 轮 imbalance > 阈值（买盘主动）
  ③ 卖盘萎缩 + 买盘堆积 (25) —— 卖压缓解 + 接货意愿增强
  ④ 超卖反弹 (15)            —— 前段下跌 + 后段买盘突增（低吸窗口）

复用 short_squeeze_monitor.py 的 Futu/DB 基础设施；与做空监控
互不干扰，使用独立的 long_monitor_state 表存当前评分。

运行：
    python long_entry_monitor.py --stock 06656           # 启动监控
    python long_entry_monitor.py --stock 06656 signals   # 查看近期 LONG_* 信号
    python long_entry_monitor.py --stock 06656 export    # 导出快照 CSV
"""
from __future__ import annotations

import sys
import time
import logging
import sqlite3
import datetime
import statistics
import os as _os
from dataclasses import dataclass
from typing import Optional

from futu import OpenQuoteContext, SubType, RET_OK

from shared_config import STOCKS, DEFAULT_STOCK
import short_squeeze_monitor as ssm
from short_squeeze_monitor import (
    init_db,
    db_save_price,
    db_save_signal,
    db_get_recent_big_net,
    db_get_recent_ask_depth,
    db_get_recent_prices,
    db_count_imb_flips,
    fetch_capital_flow,
    fetch_order_book,
    _is_trading_hours,
    _trading_phase_label,
    BIGFLOW_WINDOW,
    BIG_NET_DELTA_THRESHOLD,
    ASK_DEPTH_WINDOW,
    ASK_DEPTH_SMOOTH_K,
    STALE_DATA_ROUNDS,
    BIG_NET_STALE_ROUNDS,
    API_FAIL_TOLERANCE_ROUNDS,
)

# ═══════════════════════════════════════════════════════════
# 一、配置
# ═══════════════════════════════════════════════════════════
# 由 --stock 参数在启动时覆盖
SYMBOL        = STOCKS[DEFAULT_STOCK]["symbol"]
STOCK_CODE    = STOCKS[DEFAULT_STOCK]["stock_code"]
STOCK_NAME    = STOCKS[DEFAULT_STOCK]["name"]
OPEND_HOST    = "127.0.0.1"
OPEND_PORT    = 11111
REALTIME_INTERVAL = STOCKS[DEFAULT_STOCK]["poll_interval"]
DB_PATH       = STOCKS[DEFAULT_STOCK]["db_path"]

# ── 做多评分阈值 ────────────────────────────────────────────
LONG_ENTRY_MIN              = 50      # 入场评分门槛（满分 90）
LONG_IMB_THRESHOLD          = 0.30    # 失衡度高于此值视为持续买压
LONG_IMB_ROUNDS             = 2       # 连续 N 轮失衡度 > 阈值方触发
LONG_IMB_EXTREME            = 0.70    # 失衡度 > 此值视为极端（疑似挂单陷阱）
LONG_BID_GROW_PCT           = 30.0    # 买盘较基准上升此值 → 接货
LONG_ASK_SHRINK_PCT         = 30.0    # 卖盘较基准下降此值 → 卖压缓解
LONG_OBSERVER_IMB_BAND      = -0.10   # 卖盘萎缩但 imb < 此值 → 撤单观望，不计分
LONG_PRICE_WINDOW           = 10      # 价格历史窗口（轮次）
LONG_REBOUND_DROP_ROUNDS    = 3       # 前段下跌至少 N 轮（用 N+1 个采样点）
LONG_REBOUND_REBOUND_ROUNDS = 2       # 后段反弹至少 N 轮
LONG_REBOUND_DROP_PCT       = -0.3    # 前段下跌幅度阈值（%，负值）
LONG_REBOUND_UP_PCT         = 0.1     # 后段反弹幅度阈值（%）
LONG_PUMP_GUARD_PCT         = 3.0     # 日内涨幅 ≥ 此 % → 强制降级 CAUTION（防追高）
LONG_BIGFLOW_SYNC_WIN       = 8       # 大单加速维度的价格咬合窗口
LONG_PRICE_SYNC_THRESHOLD   = 0.0     # 大单买入时价格需 ≥ 此 % 才咬合（不咬合降权）
LONG_TRAP_EXTREME_PRICE_WIN = 3       # 极端失衡度+价格未涨判定窗口

# 复用 short 模块的挂单博弈检测常量
LONG_IMB_FLIP_WINDOW = ssm.SHORT_IMB_FLIP_WINDOW
LONG_IMB_FLIP_MIN    = ssm.SHORT_IMB_FLIP_MIN
LONG_IMB_FLIP_BAND   = ssm.SHORT_IMB_FLIP_BAND


# ═══════════════════════════════════════════════════════════
# 二、日志
# ═══════════════════════════════════════════════════════════
_LOG_DIR  = "logs"
_LOG_DATE = datetime.date.today().strftime("%Y%m%d")
_LOG_FILE = _os.path.join(_LOG_DIR, f"long_monitor_{_LOG_DATE}.log")
_os.makedirs(_LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# 三、DB 扩展（独立表，不污染 monitor_state）
# ═══════════════════════════════════════════════════════════
def ensure_long_state_table(conn: sqlite3.Connection):
    """新增 long_monitor_state 单行表，与做空脚本的 monitor_state 隔离。"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS long_monitor_state (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            ts          TEXT,
            long_score  INTEGER,
            long_signal TEXT,
            price       REAL,
            ask_depth   REAL,
            bid_depth   REAL,
            imbalance   REAL,
            big_net     REAL
        );
    """)
    conn.commit()


def db_get_recent_bid_depth(conn: sqlite3.Connection, n: int) -> list[float]:
    rows = conn.execute(
        "SELECT bid_depth FROM orderbook_snapshots ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()
    return [r[0] for r in rows]


def db_write_long_state(
    conn: sqlite3.Connection,
    long_score: int, long_signal: str,
    price: Optional[float],
    ask_depth: Optional[float], bid_depth: Optional[float],
    imbalance: Optional[float], big_net: Optional[float],
):
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT OR REPLACE INTO long_monitor_state VALUES (1,?,?,?,?,?,?,?,?)",
        (ts, long_score, long_signal, price,
         ask_depth, bid_depth, imbalance, big_net),
    )
    conn.commit()


def db_get_session_open_price(conn: sqlite3.Connection) -> Optional[float]:
    """获取今日第一笔价格（用于日内涨幅计算）。ts 是 ISO 字符串，按字典序对比即可。"""
    today_str = datetime.date.today().isoformat()
    row = conn.execute(
        "SELECT price FROM price_history WHERE ts >= ? ORDER BY id ASC LIMIT 1",
        (today_str,)
    ).fetchone()
    return row[0] if row else None


# ═══════════════════════════════════════════════════════════
# 四、做多评分核心
# ═══════════════════════════════════════════════════════════
def analyze_long_entry(
    conn: sqlite3.Connection,
    current_price: Optional[float],
    current_ask: float,
    current_bid: float,
    current_imbalance: float,
    big_net_stale: bool = False,
    intraday_change_pct: float = 0.0,
) -> tuple[int, str, list[str]]:
    """
    做多入场评分（0-90）及信号类型。

    信号类型：
        ENTRY   — 评分 ≥ LONG_ENTRY_MIN
        CAUTION — 评分 ≥ LONG_ENTRY_MIN×0.6
        HOLD    — 条件不足

    评分维度：
        1. 大单净流入加速     最高 30 分
        2. 摆盘持续偏多       最高 20 分
        3. 卖盘萎缩+买盘堆积  最高 25 分
        4. 超卖反弹           最高 15 分
    """
    score = 0
    signals: list[str] = []

    # ── 维度 1：大单净流入加速 ─────────────────────────────
    # big_nets 按 id DESC 返回，big_nets[0] 是最新，Δ = newer - older
    big_nets = db_get_recent_big_net(conn, BIGFLOW_WINDOW)
    if big_net_stale:
        signals.append("ℹ 大单累计冻结多轮，'大单加速'维度本轮跳过")
    elif len(big_nets) >= 5:
        latest_net = big_nets[0]
        deltas = [big_nets[i] - big_nets[i + 1]
                  for i in range(min(4, len(big_nets) - 1))]
        if deltas:
            delta_median = statistics.median(deltas)

            sync_prices = db_get_recent_prices(conn, LONG_BIGFLOW_SYNC_WIN)
            price_pct = 0.0
            if len(sync_prices) >= 5 and sync_prices[0] > 0:
                price_pct = (sync_prices[-1] - sync_prices[0]) / sync_prices[0] * 100
            sync_ok = price_pct >= LONG_PRICE_SYNC_THRESHOLD

            if latest_net > 0 and delta_median >= BIG_NET_DELTA_THRESHOLD:
                # 满档：Δ 中位 ≥ 50 万且累计为正
                pts = 30 if sync_ok else 20
                tag = "" if sync_ok else "⚠ "
                suffix = "" if sync_ok else "（价格未跟涨，降权）"
                msg = (f"{tag}大单 Δ 中位 {delta_median/10000:+,.1f} 万持续买入，"
                       f"累计 {latest_net/10000:+,.1f} 万，"
                       f"价格 {price_pct:+.2f}%{suffix} [+{pts}分]")
                score += pts
                signals.append(msg)
                db_save_signal(conn, "LONG_BIGFLOW_ACCEL", msg, pts)
            elif latest_net > 0 and delta_median >= BIG_NET_DELTA_THRESHOLD / 2:
                # 中档：Δ 中位 ≥ 25 万
                pts = 20 if sync_ok else 10
                tag = "" if sync_ok else "⚠ "
                suffix = "" if sync_ok else "（价格未跟涨，降权）"
                msg = (f"{tag}大单 Δ 中位 {delta_median/10000:+,.1f} 万显著买入，"
                       f"累计 {latest_net/10000:+,.1f} 万{suffix} [+{pts}分]")
                score += pts
                signals.append(msg)
            elif latest_net > 0:
                # 弱档：仅累计为正
                pts = 10
                msg = f"大单累计 {latest_net/10000:+,.1f} 万持续为正但 Δ 弱 [+{pts}分]"
                score += pts
                signals.append(msg)

    # ── 维度 2：摆盘失衡度持续为正 ──────────────────────────
    imb_rows = conn.execute(
        "SELECT imbalance FROM orderbook_snapshots ORDER BY id DESC LIMIT ?",
        (LONG_IMB_ROUNDS,),
    ).fetchall()
    if len(imb_rows) >= LONG_IMB_ROUNDS:
        all_pos = all(r[0] > LONG_IMB_THRESHOLD for r in imb_rows)
        if all_pos:
            avg_imb = statistics.mean(r[0] for r in imb_rows)
            # 诱多陷阱：失衡度极端但价格未跟涨 → 大买单挂出后撤单的可能
            trap_prices = db_get_recent_prices(conn, LONG_TRAP_EXTREME_PRICE_WIN)
            if (avg_imb > LONG_IMB_EXTREME
                    and len(trap_prices) >= 2
                    and trap_prices[-1] <= trap_prices[0]):
                msg = (f"⚠ 失衡度极端 {avg_imb:+.3f} 但近 "
                       f"{LONG_TRAP_EXTREME_PRICE_WIN} 轮价格未涨，疑似诱多挂单，不计分")
                signals.append(msg)
                db_save_signal(conn, "LONG_IMB_TRAP", msg, 0)
            else:
                pts = 20
                msg = (f"摆盘持续偏多 {LONG_IMB_ROUNDS} 轮，"
                       f"均值失衡度 {avg_imb:+.3f} [+{pts}分]")
                score += pts
                signals.append(msg)
        elif current_imbalance > LONG_IMB_THRESHOLD:
            pts = 8
            msg = f"当前摆盘偏多：失衡度 {current_imbalance:+.3f} [+{pts}分]"
            score += pts
            signals.append(msg)

    # ── 维度 3：卖盘萎缩 + 买盘堆积 ────────────────────────
    ask_history = db_get_recent_ask_depth(conn, ASK_DEPTH_WINDOW)
    bid_history = db_get_recent_bid_depth(conn, ASK_DEPTH_WINDOW)

    # 卖盘萎缩
    ask_shrink_pts = 0
    if len(ask_history) >= ASK_DEPTH_SMOOTH_K + 4 and current_ask > 0:
        smoothed_ask = statistics.median(ask_history[:ASK_DEPTH_SMOOTH_K])
        baseline_ask = statistics.median(ask_history[ASK_DEPTH_SMOOTH_K:])
        if baseline_ask > 0 and smoothed_ask > 0:
            shrink_pct = (baseline_ask - smoothed_ask) / baseline_ask * 100
            observer_mode = current_imbalance < LONG_OBSERVER_IMB_BAND
            if shrink_pct >= LONG_ASK_SHRINK_PCT:
                if observer_mode:
                    msg = (f"⚠ 卖盘萎缩 {shrink_pct:.1f}% 但失衡度 "
                           f"{current_imbalance:+.3f} 偏空，疑似撤单观望，不计分")
                    signals.append(msg)
                else:
                    ask_shrink_pts = 15
                    msg = (f"卖盘萎缩 {shrink_pct:.1f}% "
                           f"(近{ASK_DEPTH_SMOOTH_K}轮中位 {smoothed_ask:,.0f} "
                           f"vs 基准 {baseline_ask:,.0f} 股) [+{ask_shrink_pts}分]")
                    signals.append(msg)
                    db_save_signal(conn, "LONG_ASK_SHRINK", msg, ask_shrink_pts)
            elif shrink_pct >= LONG_ASK_SHRINK_PCT * 0.5 and not observer_mode:
                ask_shrink_pts = 7
                msg = f"卖盘明显萎缩 {shrink_pct:.1f}% [+{ask_shrink_pts}分]"
                signals.append(msg)

    # 买盘堆积
    bid_grow_pts = 0
    if len(bid_history) >= ASK_DEPTH_SMOOTH_K + 4 and current_bid > 0:
        smoothed_bid = statistics.median(bid_history[:ASK_DEPTH_SMOOTH_K])
        baseline_bid = statistics.median(bid_history[ASK_DEPTH_SMOOTH_K:])
        if baseline_bid > 0 and smoothed_bid > 0:
            grow_pct = (smoothed_bid - baseline_bid) / baseline_bid * 100
            if grow_pct >= LONG_BID_GROW_PCT:
                bid_grow_pts = 10
                msg = (f"买盘堆积 {grow_pct:.1f}% "
                       f"(近{ASK_DEPTH_SMOOTH_K}轮中位 {smoothed_bid:,.0f} "
                       f"vs 基准 {baseline_bid:,.0f} 股) [+{bid_grow_pts}分]")
                signals.append(msg)
                db_save_signal(conn, "LONG_BID_GROW", msg, bid_grow_pts)
            elif grow_pct >= LONG_BID_GROW_PCT * 0.5:
                bid_grow_pts = 4
                msg = f"买盘明显增长 {grow_pct:.1f}% [+{bid_grow_pts}分]"
                signals.append(msg)

    score += ask_shrink_pts + bid_grow_pts

    # ── 维度 4：超卖反弹 ───────────────────────────────────
    # prices 是时间升序（最旧在前，最新在后）
    prices = db_get_recent_prices(conn, LONG_PRICE_WINDOW)
    needed = LONG_REBOUND_DROP_ROUNDS + LONG_REBOUND_REBOUND_ROUNDS + 1
    if len(prices) >= needed:
        drop_segment = prices[:LONG_REBOUND_DROP_ROUNDS + 1]
        rebound_segment = prices[-(LONG_REBOUND_REBOUND_ROUNDS + 1):]

        drop_pct = ((drop_segment[-1] - drop_segment[0]) / drop_segment[0] * 100
                    if drop_segment[0] > 0 else 0.0)
        rebound_pct = ((rebound_segment[-1] - rebound_segment[0])
                       / rebound_segment[0] * 100 if rebound_segment[0] > 0 else 0.0)

        if drop_pct <= LONG_REBOUND_DROP_PCT and rebound_pct >= LONG_REBOUND_UP_PCT:
            # 买入端验证：买盘突增 OR 大单 Δ 显著
            bid_surge = False
            if len(bid_history) >= ASK_DEPTH_SMOOTH_K + 4:
                bid_now = statistics.median(bid_history[:ASK_DEPTH_SMOOTH_K])
                bid_base = statistics.median(bid_history[ASK_DEPTH_SMOOTH_K:])
                if bid_base > 0 and (bid_now - bid_base) / bid_base * 100 >= 30:
                    bid_surge = True

            big_delta_ok = False
            if not big_net_stale and len(big_nets) >= 2:
                delta = big_nets[0] - big_nets[1]
                if delta >= BIG_NET_DELTA_THRESHOLD:
                    big_delta_ok = True

            if bid_surge and big_delta_ok:
                pts = 15
                msg = (f"超卖反弹：前 {drop_pct:.2f}% / 后 {rebound_pct:+.2f}%，"
                       f"买盘突增 + 大单 Δ 显著 [+{pts}分]")
                score += pts
                signals.append(msg)
                db_save_signal(conn, "LONG_REBOUND", msg, pts)
            elif bid_surge or big_delta_ok:
                pts = 7
                kind = "买盘突增" if bid_surge else "大单 Δ 显著"
                msg = (f"超卖反弹：前 {drop_pct:.2f}% / 后 {rebound_pct:+.2f}%，"
                       f"{kind} [+{pts}分]")
                score += pts
                signals.append(msg)

    score = min(score, 90)
    if score >= LONG_ENTRY_MIN:
        sig_type = "ENTRY"
    elif score >= int(LONG_ENTRY_MIN * 0.6):
        sig_type = "CAUTION"
    else:
        sig_type = "HOLD"

    return apply_long_entry_failsafes(
        conn, score, sig_type, current_imbalance, intraday_change_pct, signals
    )


def apply_long_entry_failsafes(
    conn: sqlite3.Connection,
    score: int,
    sig_type: str,
    current_imbalance: float,
    intraday_change_pct: float,
    signals: list[str],
) -> tuple[int, str, list[str]]:
    """
    做多 ENTRY 守门：
      Failsafe 1: 失衡度极性翻转过频 → ENTRY → CAUTION（挂单博弈）
      Failsafe 2: 日内涨幅过高 → ENTRY → CAUTION（追高守门）
    """
    if sig_type != "ENTRY":
        return score, sig_type, signals

    flips = db_count_imb_flips(conn, LONG_IMB_FLIP_WINDOW, LONG_IMB_FLIP_BAND)
    if flips >= LONG_IMB_FLIP_MIN:
        msg = (f"⚠ 失衡度近 {LONG_IMB_FLIP_WINDOW} 轮翻转 {flips} 次 "
               f"≥ {LONG_IMB_FLIP_MIN}，疑似挂单博弈，降级 CAUTION")
        signals.append(msg)
        db_save_signal(conn, "LONG_IMB_FLIP_GUARD", msg, 0)
        return score, "CAUTION", signals

    if intraday_change_pct >= LONG_PUMP_GUARD_PCT:
        msg = (f"⚠ 日内涨幅 {intraday_change_pct:+.2f}% ≥ {LONG_PUMP_GUARD_PCT}%，"
               f"已飞起，降级 CAUTION 防追高")
        signals.append(msg)
        db_save_signal(conn, "LONG_PUMP_GUARD", msg, 0)
        return score, "CAUTION", signals

    return score, sig_type, signals


# ═══════════════════════════════════════════════════════════
# 五、监控状态 & 仪表盘
# ═══════════════════════════════════════════════════════════
@dataclass
class MonitorStateLong:
    last_price:           Optional[float] = None
    session_open_price:   Optional[float] = None
    intraday_change_pct:  float           = 0.0
    latest_big_net:       Optional[float] = None
    recent_big_net_delta: Optional[float] = None
    latest_ask_depth:     Optional[float] = None
    latest_bid_depth:     Optional[float] = None
    latest_imbalance:     Optional[float] = None
    long_score:           int             = 0
    long_signal:          str             = "HOLD"
    _prev_price:          Optional[float] = None
    _prev_big_net:        Optional[float] = None
    _stale_count:         int             = 0
    _prev_big_net_only:   Optional[float] = None
    _big_net_stale_count: int             = 0
    _capital_fail_count:  int             = 0
    _orderbook_fail_count: int            = 0


def print_dashboard_long(
    state: MonitorStateLong,
    long_score: int,
    long_signal: str,
    long_sigs: list[str],
):
    def bar(v: int, w: int = 20, max_v: int = 90) -> str:
        n = min(int(v / max_v * w), w)
        return "█" * n + "░" * (w - n)

    signal_label = {
        "ENTRY":   "▶▶ 入  场  信  号 ◀◀",
        "CAUTION": "── 信号积累中 观望 ──",
        "HOLD":    "── 条件不足  继续等 ──",
    }.get(long_signal, "──────────────────────")

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"""
╔══════════════════════════════════════════════════════════╗
║   {STOCK_NAME} ({STOCK_CODE}.HK)  做多入场监控   {now}  ║
╠══════════════════════════════════════════════════════════╣
║  最新价   : {str(state.last_price or 'N/A'):>10}  日内 {state.intraday_change_pct:+.2f}%        ║
╠══════════════════════════════════════════════════════════╣
║  [①] 大单净流入(累计): {str(f"{state.latest_big_net/10000:+,.1f} 万" if state.latest_big_net is not None else "N/A"):>16}  ║
║      近Δ            : {str(f"{state.recent_big_net_delta/10000:+,.1f} 万" if state.recent_big_net_delta is not None else "—"):>16}  ║
║  [②] 摆盘失衡度     : {str(f"{state.latest_imbalance:+.3f}" if state.latest_imbalance is not None else "N/A"):>10}                ║
║  [③] 买盘深度       : {str(f"{state.latest_bid_depth:,.0f} 股" if state.latest_bid_depth is not None else "N/A"):>16}  ║
║      卖盘深度       : {str(f"{state.latest_ask_depth:,.0f} 股" if state.latest_ask_depth is not None else "N/A"):>16}  ║
╠══════════════════════════════════════════════════════════╣
║  【做多入场】[{bar(long_score)}]  {long_score:3d}/90        ║
║  {signal_label:<52}  ║""")

    if long_sigs:
        for s in long_sigs:
            print(f"║   → {s[:52]:<52}  ║")

    print("╚══════════════════════════════════════════════════════════╝")


# ═══════════════════════════════════════════════════════════
# 六、主监控循环
# ═══════════════════════════════════════════════════════════
def run_monitor_long():
    log.info(f"启动做多监控: {SYMBOL}，实时轮询 {REALTIME_INTERVAL}s")

    # fetch_capital_flow / fetch_order_book 使用 short_squeeze_monitor 模块内的
    # SYMBOL 全局变量，启动时同步覆盖，避免它们访问默认股票数据
    ssm.SYMBOL            = SYMBOL
    ssm.STOCK_CODE        = STOCK_CODE
    ssm.STOCK_NAME        = STOCK_NAME
    ssm.DB_PATH           = DB_PATH
    ssm.REALTIME_INTERVAL = REALTIME_INTERVAL

    conn = init_db(DB_PATH)
    ensure_long_state_table(conn)
    state = MonitorStateLong()
    ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)

    ret, err = ctx.subscribe([SYMBOL], [SubType.QUOTE, SubType.ORDER_BOOK])
    if ret != RET_OK:
        log.warning(f"订阅失败: {err}（将使用快照模式）")

    try:
        while True:
            now = datetime.datetime.now()

            # ── 交易时段守门 ─────────────────────────────────────
            if not _is_trading_hours(now):
                phase = _trading_phase_label(now)
                log.info(f"[{phase}] {now.strftime('%H:%M:%S')} 跳过打分（数据非连续交易语义）")
                time.sleep(REALTIME_INTERVAL)
                continue

            # ── 价格 ─────────────────────────────────────────
            ret_q, qdata = ctx.get_stock_quote(code_list=[SYMBOL])
            if ret_q == RET_OK and not qdata.empty:
                state.last_price = float(qdata.iloc[0]["last_price"])
                db_save_price(conn, now.isoformat(timespec="seconds"),
                              state.last_price)

            # ── 日内涨幅（首笔价做基准）──────────────────────
            if state.session_open_price is None:
                state.session_open_price = db_get_session_open_price(conn)
            if state.session_open_price and state.last_price:
                state.intraday_change_pct = (
                    (state.last_price - state.session_open_price)
                    / state.session_open_price * 100
                )

            # ── 资金流向 ─────────────────────────────────────
            cf = fetch_capital_flow(ctx, conn)
            if cf:
                state.latest_big_net = cf["big_net"]
                state._capital_fail_count = 0
                _bn_recent = db_get_recent_big_net(conn, 2)
                if len(_bn_recent) >= 2:
                    state.recent_big_net_delta = _bn_recent[0] - _bn_recent[1]
                else:
                    state.recent_big_net_delta = None
            else:
                state._capital_fail_count += 1

            # ── 摆盘 ─────────────────────────────────────────
            ob = fetch_order_book(ctx, conn)
            if ob:
                state.latest_ask_depth = ob["ask_depth"]
                state.latest_bid_depth = ob["bid_depth"]
                state.latest_imbalance = ob["imbalance"]
                state._orderbook_fail_count = 0
            else:
                state._orderbook_fail_count += 1

            # ── API 失败守门 ─────────────────────────────────
            if (state._capital_fail_count >= API_FAIL_TOLERANCE_ROUNDS
                    or state._orderbook_fail_count >= API_FAIL_TOLERANCE_ROUNDS):
                log.warning(
                    f"[API 失效] 资金流失败 {state._capital_fail_count} 轮 / "
                    f"摆盘失败 {state._orderbook_fail_count} 轮 ≥ "
                    f"{API_FAIL_TOLERANCE_ROUNDS}，跳过打分（避免基于陈旧快照）"
                )
                time.sleep(REALTIME_INTERVAL)
                continue

            # ── 数据停滞守门 ─────────────────────────────────
            if (state.last_price is not None
                    and state.last_price == state._prev_price
                    and state.latest_big_net == state._prev_big_net):
                state._stale_count += 1
            else:
                state._stale_count = 0
                state._prev_price   = state.last_price
                state._prev_big_net = state.latest_big_net

            if state._stale_count >= STALE_DATA_ROUNDS:
                log.warning(
                    f"[数据停滞] 价格 {state.last_price} + 大单累计连续 "
                    f"{state._stale_count} 轮未更新，跳过打分"
                )
                time.sleep(REALTIME_INTERVAL)
                continue

            # ── 大单累计单独停滞守门 ────────────────────────
            if (state.latest_big_net is not None
                    and state.latest_big_net == state._prev_big_net_only):
                state._big_net_stale_count += 1
            else:
                state._big_net_stale_count = 0
                state._prev_big_net_only   = state.latest_big_net
            big_net_stale = state._big_net_stale_count >= BIG_NET_STALE_ROUNDS
            if big_net_stale:
                log.info(
                    f"[大单停滞] 累计冻结 {state._big_net_stale_count} 轮"
                    f"，大单加速维度本轮跳过"
                )

            # ── 评分 ─────────────────────────────────────────
            long_score, long_signal, long_sigs = analyze_long_entry(
                conn,
                state.last_price,
                state.latest_ask_depth or 0,
                state.latest_bid_depth or 0,
                state.latest_imbalance or 0,
                big_net_stale=big_net_stale,
                intraday_change_pct=state.intraday_change_pct,
            )
            state.long_score  = long_score
            state.long_signal = long_signal

            # ── 写 DB ───────────────────────────────────────
            db_write_long_state(
                conn, long_score, long_signal, state.last_price,
                state.latest_ask_depth, state.latest_bid_depth,
                state.latest_imbalance, state.latest_big_net,
            )

            # ── 输出 ────────────────────────────────────────
            print_dashboard_long(state, long_score, long_signal, long_sigs)
            _imb_str = (f"{state.latest_imbalance:.3f}"
                        if state.latest_imbalance is not None else "N/A")
            log.info(
                f"做多={long_score}({long_signal}) | "
                f"日内={state.intraday_change_pct:+.2f}% | "
                f"大单净={state.latest_big_net} | 卖深={state.latest_ask_depth} | "
                f"买深={state.latest_bid_depth} | 失衡={_imb_str}"
            )

            time.sleep(REALTIME_INTERVAL)

    except KeyboardInterrupt:
        log.info("用户中断，退出监控。")
    finally:
        ctx.close()
        conn.close()


# ═══════════════════════════════════════════════════════════
# 七、辅助命令
# ═══════════════════════════════════════════════════════════
def cmd_signals(n: int = 30):
    import pandas as pd
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql(
        f"SELECT ts, signal_type, detail, score FROM signals "
        f"WHERE signal_type LIKE 'LONG_%' ORDER BY id DESC LIMIT {n}",
        conn,
    )
    conn.close()
    print(df.to_string(index=False))


def cmd_export(out_csv: str = "long_snapshots_export.csv"):
    import pandas as pd
    conn = sqlite3.connect(DB_PATH)
    df_ob = pd.read_sql("SELECT * FROM orderbook_snapshots ORDER BY id", conn)
    df_cf = pd.read_sql("SELECT * FROM capital_flow ORDER BY id", conn)
    conn.close()
    df_ob.to_csv("orderbook_" + out_csv, index=False)
    df_cf.to_csv("capital_"   + out_csv, index=False)
    print(f"已导出: orderbook_{out_csv}, capital_{out_csv}")


# ═══════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse as _ap

    _p = _ap.ArgumentParser(
        description="港股做多入场监控系统（盘口实时分析）",
        formatter_class=_ap.RawDescriptionHelpFormatter,
        epilog="""
子命令：
  (无)       启动实时监控
  signals    查看近期 LONG_* 触发信号
  export     导出快照 CSV
"""
    )
    _p.add_argument("--stock", "-s", default=DEFAULT_STOCK,
                    metavar="CODE",
                    help=f"股票代码，支持: {', '.join(STOCKS)}（默认 {DEFAULT_STOCK}）")
    _p.add_argument("cmd", nargs="?", default="monitor",
                    choices=["monitor", "signals", "export"],
                    help="子命令（默认 monitor）")

    _args = _p.parse_args()

    _stock_cfg = STOCKS.get(_args.stock)
    if not _stock_cfg:
        print(f"未知股票代码 {_args.stock!r}，支持: {', '.join(STOCKS)}",
              file=sys.stderr)
        sys.exit(1)
    SYMBOL            = _stock_cfg["symbol"]
    STOCK_CODE        = _stock_cfg["stock_code"]
    STOCK_NAME        = _stock_cfg["name"]
    DB_PATH           = _stock_cfg["db_path"]
    REALTIME_INTERVAL = _stock_cfg["poll_interval"]

    if _args.cmd == "monitor":
        run_monitor_long()
    elif _args.cmd == "signals":
        cmd_signals()
    elif _args.cmd == "export":
        cmd_export()
