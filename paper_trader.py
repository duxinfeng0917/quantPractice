"""
paper_trader.py
===============
MINIMAX-W (HK.00100) 模拟账户自动做空交易机器人

基于 short_squeeze_monitor.py 的信号引擎，在富途模拟 MARGIN 账户上自动下单。

入场规则（4 条件全满足）：
    1. 做空入场评分 ≥ 65（HIGH_ENTRY_SCORE）
    2. 连续 2 轮维持 ENTRY 信号（排除单轮噪声）
    3. 逼空评分 < 20（SAFE_SQUEEZE_SCORE）
    4. 摆盘失衡度 < +0.60（ENTRY_IMB_THRESHOLD）— 排除订单簿极度偏多的情况

仓位管理：
    · 评分 65–74 → 开仓 50%（HALF_QTY = 500 股）
    · 评分 ≥ 75  → 开仓 100%（FULL_QTY = 1000 股）

平仓规则：
    · 第一目标价 → 平仓 50%，更新止损至入场价
    · 第二目标价 → 全部平仓，锁定利润
    · 超过止损价 → 立即全部平仓，限制亏损
    · 逼空评分 ≥ 35 → 紧急平仓，防止逼空

依赖：
    pip install futu-api

用法：
    python3 paper_trader.py                         # 默认参数启动
    python3 paper_trader.py --qty 1000 --stop 950   # 指定最大仓位和止损
    python3 paper_trader.py --dry-run               # 只打印信号，不实际下单
"""

from __future__ import annotations

import sys
import time
import logging
import sqlite3
import datetime
import statistics
import argparse
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from futu import (
    OpenQuoteContext,
    OpenSecTradeContext,
    SubType,
    RET_OK,
    TrdEnv,
    TrdSide,
    OrderType,
    TrdMarket,
)

# ═══════════════════════════════════════════════════════════
# 一、配置
# ═══════════════════════════════════════════════════════════
SYMBOL        = "HK.00100"
STOCK_CODE    = "00100"
OPEND_HOST    = "127.0.0.1"
OPEND_PORT    = 11111

# 富途模拟 MARGIN 账户
SIM_ACC_ID    = 18982257
SIM_ENV       = TrdEnv.SIMULATE
SIM_MARKET    = TrdMarket.HK

DB_PATH       = "short_data.db"       # 与 short_squeeze_monitor.py 共享
TRADER_LOG    = "paper_trader.log"
POLL_INTERVAL = 60                    # 轮询秒数

# ── 入场条件 ──────────────────────────────────────────────
HIGH_ENTRY_SCORE    = 65              # 最低入场评分
SAFE_SQUEEZE_SCORE  = 20             # 最大允许逼空评分
ENTRY_IMB_THRESHOLD = 0.60           # 失衡度低于此值才允许入场（排除极度偏多）
ENTRY_CONFIRM_ROUNDS = 2             # 连续 ENTRY 信号轮数

# ── 仓位管理 ──────────────────────────────────────────────
FULL_QTY   = 1000                     # 满仓股数
HALF_QTY   = 500                      # 半仓股数
DEFAULT_STOP    = 950.0
DEFAULT_TARGET1 = 870.0
DEFAULT_TARGET2 = 850.0

# ── 平仓触发 ──────────────────────────────────────────────
EMERGENCY_SQUEEZE = 35               # 逼空评分超此值 → 紧急平仓

# ── 信号引擎参数（与 short_squeeze_monitor.py 保持一致）──
SHORT_SAFE_SQUEEZE   = 25
SHORT_EXIT_SQUEEZE   = 40
SHORT_ASK_SURGE_PCT  = 80.0
SHORT_IMB_THRESHOLD  = -0.30
SHORT_IMB_ROUNDS     = 2
SHORT_ENTRY_MIN      = 55
SHORT_PRICE_WINDOW   = 10
ASK_DEPTH_WINDOW     = 20
BIGFLOW_WINDOW       = 10
SHORT_RATIO_WINDOW   = 5
SHORT_RATIO_RISE_MIN = 3
ASK_DEPTH_SHRINK_PCT = 30.0
BIGFLOW_REVERSAL_MIN = 2


# ═══════════════════════════════════════════════════════════
# 二、日志
# ═══════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(TRADER_LOG, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# 三、状态机
# ═══════════════════════════════════════════════════════════
class TraderState(Enum):
    IDLE        = auto()   # 无持仓，等待入场信号
    CONFIRM_1   = auto()   # 第一轮 ENTRY 信号，等待确认
    IN_POSITION = auto()   # 持仓中
    COVERING    = auto()   # 部分平仓后仍持仓（target1 已触）


@dataclass
class Position:
    entry_price:  float
    qty:          int
    entry_time:   str
    stop_price:   float
    target1:      float
    target2:      float
    covered_qty:  int   = 0
    realized_pnl: float = 0.0

    @property
    def open_qty(self) -> int:
        return self.qty - self.covered_qty

    def unrealized_pnl(self, price: float) -> float:
        return (self.entry_price - price) * self.open_qty

    def pnl_pct(self, price: float) -> float:
        return (self.entry_price - price) / self.entry_price * 100


@dataclass
class BotState:
    trader_state:    TraderState = TraderState.IDLE
    position:        Optional[Position] = None
    confirm_rounds:  int = 0              # 连续 ENTRY 信号计数
    last_entry_score: int = 0
    last_squeeze:    int = 0
    last_imbalance:  float = 0.0
    ask_history:     list[float] = field(default_factory=list)
    target1_done:    bool = False         # 第一目标价是否已平仓


# ═══════════════════════════════════════════════════════════
# 四、数据库
# ═══════════════════════════════════════════════════════════
def init_trade_db(conn: sqlite3.Connection):
    """在共享 DB 中新建交易记录表（若不存在）。"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT,
            action      TEXT,   -- SHORT_OPEN / COVER_PARTIAL / COVER_FULL / COVER_STOP / COVER_SQUEEZE
            price       REAL,
            qty         INTEGER,
            pnl         REAL,   -- 本次成交盈亏（平仓时）
            total_pnl   REAL,   -- 累计已实现盈亏
            entry_score INTEGER,
            squeeze_score INTEGER,
            imbalance   REAL,
            note        TEXT
        );
    """)
    conn.commit()


def log_trade(conn: sqlite3.Connection, action: str, price: float,
              qty: int, pnl: float, total_pnl: float,
              entry_score: int, squeeze_score: int,
              imbalance: float, note: str = ""):
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO paper_trades VALUES (NULL,?,?,?,?,?,?,?,?,?,?)",
        (ts, action, price, qty, pnl, total_pnl,
         entry_score, squeeze_score, imbalance, note),
    )
    conn.commit()
    log.info(
        f"[TRADE] {action} | 价格={price} 数量={qty} | "
        f"本次盈亏={pnl:+,.0f} 累计={total_pnl:+,.0f} | {note}"
    )


# ── 以下 DB 读取函数与 short_squeeze_monitor.py 完全相同 ──

def db_get_recent_big_net(conn: sqlite3.Connection, n: int) -> list[float]:
    rows = conn.execute(
        "SELECT big_net FROM capital_flow ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()
    return [r[0] for r in rows]


def db_get_recent_ask_depth(conn: sqlite3.Connection, n: int) -> list[float]:
    rows = conn.execute(
        "SELECT ask_depth FROM orderbook_snapshots ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()
    return [r[0] for r in rows]


def db_get_recent_prices(conn: sqlite3.Connection, n: int) -> list[float]:
    rows = conn.execute(
        "SELECT price FROM price_history ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()
    return [r[0] for r in reversed(rows)]


def db_get_recent_hkex(conn: sqlite3.Connection, n: int) -> list[float]:
    rows = conn.execute(
        "SELECT short_ratio FROM hkex_daily ORDER BY date DESC LIMIT ?", (n,)
    ).fetchall()
    return [r[0] for r in reversed(rows)]


def db_save_orderbook(conn: sqlite3.Connection, ts: str, bid: float,
                      ask: float, imb: float):
    conn.execute(
        "INSERT INTO orderbook_snapshots VALUES (NULL,?,?,?,?)",
        (ts, bid, ask, imb),
    )
    conn.commit()


def db_save_capital(conn: sqlite3.Connection, ts: str,
                    big_in: float, big_out: float,
                    big_net: float, mid_net: float, small_net: float):
    conn.execute(
        "INSERT INTO capital_flow VALUES (NULL,?,?,?,?,?,?)",
        (ts, big_in, big_out, big_net, mid_net, small_net),
    )
    conn.commit()


def db_save_price(conn: sqlite3.Connection, ts: str, price: float):
    conn.execute("INSERT INTO price_history VALUES (NULL,?,?)", (ts, price))
    conn.commit()


def db_save_signal(conn: sqlite3.Connection, sig_type: str,
                   detail: str, score: int):
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO signals VALUES (NULL,?,?,?,?)",
        (ts, sig_type, detail, score),
    )
    conn.commit()


# ═══════════════════════════════════════════════════════════
# 五、信号引擎（从 short_squeeze_monitor.py 提取的核心函数）
# ═══════════════════════════════════════════════════════════

def analyze_hkex_momentum(conn: sqlite3.Connection,
                           current_price: Optional[float]
                           ) -> tuple[int, int, dict]:
    """计算 HKEX 历史空头动能，返回 (做空支撑分, 逼空风险分, stats)。"""
    rows = conn.execute(
        "SELECT date, short_volume, short_value, short_ratio "
        "FROM hkex_daily ORDER BY date DESC LIMIT 10"
    ).fetchall()

    if len(rows) < 2:
        return 0, 0, {}

    short_vols   = [r[1] for r in rows]
    short_vals   = [r[2] for r in rows]
    short_ratios = [r[3] for r in rows]

    n = min(len(rows), 6)
    total_val = sum(short_vals[:n])
    total_vol = sum(short_vols[:n])
    weighted_cost = (total_val / total_vol) if total_vol > 0 else None

    latest_ratio = short_ratios[0]
    avg_ratio_5d = statistics.mean(short_ratios[1:min(6, len(short_ratios))])
    momentum_ratio = (latest_ratio / avg_ratio_5d) if avg_ratio_5d > 0 else 1.0

    latest_vol = short_vols[0]
    avg_vol_5d = statistics.mean(short_vols[1:min(6, len(short_vols))])
    volume_surge = (latest_vol / avg_vol_5d) if avg_vol_5d > 0 else 1.0

    short_support = 0
    squeeze_risk  = 0

    if weighted_cost and current_price:
        gap_pct = (weighted_cost - current_price) / weighted_cost * 100
        if gap_pct > 5:
            short_support += 15
        elif gap_pct < -3:
            squeeze_risk += 20

    if momentum_ratio >= 1.8:
        short_support += 20
    elif momentum_ratio >= 1.5:
        short_support += 12
    elif momentum_ratio < 0.6:
        squeeze_risk += 10

    if volume_surge >= 2.5:
        short_support += 15
    elif volume_surge >= 1.8:
        short_support += 8

    return min(short_support, 50), squeeze_risk, {
        "weighted_cost":  weighted_cost,
        "momentum_ratio": momentum_ratio,
        "volume_surge":   volume_surge,
        "latest_ratio":   latest_ratio,
    }


def compute_squeeze_score(conn: sqlite3.Connection,
                           current_ask: float,
                           current_price: Optional[float]) -> tuple[int, list[str]]:
    """综合逼空评分（0-100）。"""
    score   = 0
    reasons = []

    # HKEX 历史动能（逼空风险部分）
    _, sq_risk, _ = analyze_hkex_momentum(conn, current_price)
    score += sq_risk

    # 卖空占比趋势（高位 + 拐头）
    ratios = db_get_recent_hkex(conn, SHORT_RATIO_WINDOW + 2)
    if len(ratios) >= SHORT_RATIO_RISE_MIN + 1:
        latest = ratios[-1]
        prev   = ratios[-2]
        history = ratios[:-1]
        consecutive_rises = 0
        for i in range(len(history) - 1, 0, -1):
            if history[i] > history[i - 1]:
                consecutive_rises += 1
            else:
                break
        if consecutive_rises >= SHORT_RATIO_RISE_MIN and latest < prev:
            pts = 25
            score += pts
            reasons.append(f"卖空占比高位拐头 [{prev:.2f}%→{latest:.2f}%] [+{pts}]")
        if latest >= 35:
            score += 15
        elif latest >= 25:
            score += 8

    # 卖盘深度骤减（空头回补）
    ask_history = db_get_recent_ask_depth(conn, ASK_DEPTH_WINDOW)
    if len(ask_history) >= 5 and current_ask > 0:
        avg_ask = statistics.mean(ask_history[:ASK_DEPTH_WINDOW])
        if avg_ask > 0:
            shrink = (avg_ask - current_ask) / avg_ask * 100
            if shrink >= ASK_DEPTH_SHRINK_PCT:
                score += 25
                reasons.append(f"卖盘深度骤减 {shrink:.1f}% [+25]")
            elif shrink >= ASK_DEPTH_SHRINK_PCT * 0.6:
                score += 12

    # 大单净流入反转
    history = db_get_recent_big_net(conn, BIGFLOW_WINDOW)
    if len(history) >= BIGFLOW_REVERSAL_MIN + 1:
        recent  = history[:BIGFLOW_REVERSAL_MIN]
        earlier = history[BIGFLOW_REVERSAL_MIN:]
        if all(v > 0 for v in recent) and any(v < 0 for v in earlier):
            score += 25
            reasons.append(f"大单净流入反转 [+25]")
        elif all(v > 0 for v in recent):
            score += 10

    return min(score, 100), reasons


def compute_entry_score(conn: sqlite3.Connection,
                         squeeze_score: int,
                         current_price: Optional[float],
                         current_ask: float,
                         current_imbalance: float) -> tuple[int, str, list[str]]:
    """做空入场评分（0-100），含安全门。"""
    if squeeze_score > SHORT_SAFE_SQUEEZE:
        return 0, "BLOCKED", [
            f"逼空评分={squeeze_score} 超安全线 {SHORT_SAFE_SQUEEZE}，禁止开空"
        ]

    score   = 0
    signals = []

    # 维度 1：大单净流入方向
    big_nets = db_get_recent_big_net(conn, BIGFLOW_WINDOW)
    if len(big_nets) >= 4:
        latest_net   = big_nets[0]
        had_positive = any(v > 0 for v in big_nets[1:5])
        if latest_net < 0 and had_positive:
            score += 30
            signals.append(f"大单净流入由正转负 {latest_net/10000:+,.1f}万 [+30]")
            db_save_signal(conn, "SHORT_BIGFLOW_REVERSAL",
                           f"net={latest_net/10000:.1f}万", 30)
        elif latest_net < 0:
            score += 15
            signals.append(f"大单净流入持续负 {latest_net/10000:+,.1f}万 [+15]")

    # 维度 2：卖盘深度骤增
    ask_history = db_get_recent_ask_depth(conn, ASK_DEPTH_WINDOW)
    if len(ask_history) >= 5 and current_ask > 0:
        avg_ask = statistics.mean(ask_history[1:])
        if avg_ask > 0:
            surge = (current_ask - avg_ask) / avg_ask * 100
            if surge >= SHORT_ASK_SURGE_PCT:
                score += 25
                signals.append(f"卖盘深度骤增 {surge:.1f}% [+25]")
                db_save_signal(conn, "SHORT_ASK_SURGE",
                               f"surge={surge:.1f}%", 25)
            elif surge >= SHORT_ASK_SURGE_PCT * 0.5:
                score += 12
                signals.append(f"卖盘深度上升 {surge:.1f}% [+12]")

    # 维度 3：摆盘持续偏空
    imb_rows = conn.execute(
        "SELECT imbalance FROM orderbook_snapshots ORDER BY id DESC LIMIT ?",
        (SHORT_IMB_ROUNDS,),
    ).fetchall()
    if len(imb_rows) >= SHORT_IMB_ROUNDS:
        if all(r[0] < SHORT_IMB_THRESHOLD for r in imb_rows):
            avg_imb = statistics.mean(r[0] for r in imb_rows)
            score += 20
            signals.append(f"摆盘持续偏空 {SHORT_IMB_ROUNDS}轮 均值{avg_imb:.3f} [+20]")
        elif current_imbalance < SHORT_IMB_THRESHOLD:
            score += 8
            signals.append(f"当前摆盘偏空 {current_imbalance:.3f} [+8]")

    # 维度 4：价格低于近期高点
    prices = db_get_recent_prices(conn, SHORT_PRICE_WINDOW)
    if len(prices) >= 3 and current_price:
        recent_high = max(prices)
        if recent_high > 0:
            drop = (recent_high - current_price) / recent_high * 100
            if drop >= 0.5:
                score += 15
                signals.append(f"价格低于高点 {drop:.2f}% [+15]")
            elif drop >= 0.2:
                score += 7
                signals.append(f"价格轻微回落 {drop:.2f}% [+7]")

    # 维度 5：高点拒绝后连续下行
    if len(prices) >= 4:
        peak_idx = prices.index(max(prices))
        if 0 < peak_idx < len(prices) - 1:
            post = prices[peak_idx + 1:]
            drops = sum(1 for i in range(len(post) - 1) if post[i+1] < post[i])
            if drops >= 2:
                score += 10
                signals.append(f"高点拒绝后连跌 {drops}轮 [+10]")

    # HKEX 动能支撑
    hkex_sup, _, _ = analyze_hkex_momentum(conn, current_price)
    score += hkex_sup

    score = min(score, 100)
    if score >= HIGH_ENTRY_SCORE:
        sig_type = "ENTRY"
    elif score >= int(SHORT_ENTRY_MIN * 0.6):
        sig_type = "CAUTION"
    else:
        sig_type = "HOLD"

    return score, sig_type, signals


# ═══════════════════════════════════════════════════════════
# 六、富途行情拉取
# ═══════════════════════════════════════════════════════════
def fetch_market_data(quote_ctx: OpenQuoteContext,
                      conn: sqlite3.Connection
                      ) -> tuple[Optional[float], float, float, float]:
    """
    拉取最新价、卖盘深度、买盘深度、失衡度。
    同时写入 DB，供信号引擎使用。
    返回 (price, ask_depth, bid_depth, imbalance)
    """
    ts  = datetime.datetime.now().isoformat(timespec="seconds")
    price = None

    # 最新价
    ret, qdata = quote_ctx.get_stock_quote([SYMBOL])
    if ret == RET_OK and not qdata.empty:
        price = float(qdata.iloc[0]["last_price"])
        db_save_price(conn, ts, price)

    # 摆盘
    ask_depth = bid_depth = imbalance = 0.0
    ret, ob = quote_ctx.get_order_book(SYMBOL, num=10)
    if ret == RET_OK:
        bid_depth = sum(float(x[1]) for x in ob.get("Bid", []))
        ask_depth = sum(float(x[1]) for x in ob.get("Ask", []))
        total     = bid_depth + ask_depth
        imbalance = (bid_depth - ask_depth) / total if total > 0 else 0.0
        db_save_orderbook(conn, ts, bid_depth, ask_depth, imbalance)

    # 资金流向
    ret, cf = quote_ctx.get_capital_distribution(SYMBOL)
    if ret == RET_OK and not cf.empty:
        row = cf.iloc[0]
        def _f(col):
            return float(row.get(col, 0) or 0)
        big_in  = _f("capital_in_big")
        big_out = _f("capital_out_big")
        mid_net = _f("capital_in_mid") - _f("capital_out_mid")
        sml_net = _f("capital_in_small") - _f("capital_out_small")
        db_save_capital(conn, ts, big_in, big_out,
                        big_in - big_out, mid_net, sml_net)

    return price, ask_depth, bid_depth, imbalance


# ═══════════════════════════════════════════════════════════
# 七、富途交易执行
# ═══════════════════════════════════════════════════════════
def place_short_order(trade_ctx: OpenSecTradeContext,
                      price: float, qty: int, dry_run: bool) -> bool:
    """卖出开空（TrdSide.SELL）。"""
    log.info(f"[下单] 开空 {qty}股 @ {price}  dry_run={dry_run}")
    if dry_run:
        return True

    ret, data = trade_ctx.place_order(
        price      = price,
        qty        = qty,
        code       = SYMBOL,
        trd_side   = TrdSide.SELL,
        order_type = OrderType.NORMAL,
        trd_env    = SIM_ENV,
        acc_id     = SIM_ACC_ID,
    )
    if ret == RET_OK:
        order_id = data["order_id"].iloc[0]
        log.info(f"[下单成功] order_id={order_id}")
        return True
    else:
        log.error(f"[下单失败] {data}")
        return False


def place_cover_order(trade_ctx: OpenSecTradeContext,
                      price: float, qty: int, dry_run: bool) -> bool:
    """买入平仓（TrdSide.BUY）。"""
    log.info(f"[下单] 平仓 {qty}股 @ {price}  dry_run={dry_run}")
    if dry_run:
        return True

    ret, data = trade_ctx.place_order(
        price      = price,
        qty        = qty,
        code       = SYMBOL,
        trd_side   = TrdSide.BUY,
        order_type = OrderType.NORMAL,
        trd_env    = SIM_ENV,
        acc_id     = SIM_ACC_ID,
    )
    if ret == RET_OK:
        order_id = data["order_id"].iloc[0]
        log.info(f"[平仓成功] order_id={order_id}")
        return True
    else:
        log.error(f"[平仓失败] {data}")
        return False


# ═══════════════════════════════════════════════════════════
# 八、仪表盘
# ═══════════════════════════════════════════════════════════
def print_dashboard(bot: BotState, price: Optional[float],
                    squeeze: int, entry: int,
                    sig_type: str, imbalance: float,
                    signals: list[str], dry_run: bool):
    now = datetime.datetime.now().strftime("%H:%M:%S")
    state_str = bot.trader_state.name
    pos = bot.position

    def bar(v: int, width: int = 20) -> str:
        n = min(int(v / 100 * width), width)
        return "█" * n + "░" * (width - n)

    mode_tag = " [DRY-RUN]" if dry_run else " [LIVE-SIM]"

    print(f"\n╔══════════════════════════════════════════════════════════╗")
    print(f"║  MINIMAX-W 模拟自动交易  {now}{mode_tag:<16}  ║")
    print(f"╠══════════════════════════════════════════════════════════╣")
    print(f"║  状态：{state_str:<12}  确认轮: {bot.confirm_rounds}/{ENTRY_CONFIRM_ROUNDS}                  ║")
    if price:
        print(f"║  当前价：{price:<8}  失衡度：{imbalance:+.3f}                        ║")
    print(f"╠══════════════════════════════════════════════════════════╣")
    print(f"║  做空评分 [{bar(entry)}] {entry:3d}  ({sig_type:<8})║")
    print(f"║  逼空评分 [{bar(squeeze)}] {squeeze:3d}                    ║")

    if pos and price:
        pnl     = pos.unrealized_pnl(price)
        pnl_pct = pos.pnl_pct(price)
        print(f"╠══════════════════════════════════════════════════════════╣")
        print(f"║  开仓均价：{pos.entry_price:<8.2f}  持仓量：{pos.open_qty:>6,} 股          ║")
        print(f"║  未实现盈亏：{pnl:>+12,.0f} HKD  ({pnl_pct:+.2f}%)        ║")
        print(f"║  已实现盈亏：{pos.realized_pnl:>+12,.0f} HKD                     ║")
        stop_gap = (pos.stop_price - price) / price * 100
        print(f"║  止损：{pos.stop_price:<6.2f} (距离 {stop_gap:+.2f}%)  "
              f"目标①：{pos.target1}  ②：{pos.target2}     ║")

    if signals:
        print(f"╠══════════════════════════════════════════════════════════╣")
        for s in signals[:4]:
            print(f"║  → {s[:54]:<54} ║")
    print(f"╚══════════════════════════════════════════════════════════╝")


# ═══════════════════════════════════════════════════════════
# 九、主交易循环
# ═══════════════════════════════════════════════════════════
def run(args):
    dry_run = args.dry_run
    max_qty = args.qty
    stop_price  = args.stop
    target1     = args.target1
    target2     = args.target2

    log.info(
        f"模拟交易启动 | "
        f"acc_id={SIM_ACC_ID} env={SIM_ENV} "
        f"最大仓位={max_qty}股 止损={stop_price} "
        f"目标①={target1} ②={target2} "
        f"dry_run={dry_run}"
    )

    # 连接数据库
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    init_trade_db(conn)

    # 连接富途行情
    quote_ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
    ret, err = quote_ctx.subscribe([SYMBOL],
                                    [SubType.QUOTE, SubType.ORDER_BOOK])
    if ret != RET_OK:
        log.warning(f"订阅行情失败: {err}")

    # 连接富途交易（模拟）
    trade_ctx = OpenSecTradeContext(
        filter_trdmarket = SIM_MARKET,
        host = OPEND_HOST,
        port = OPEND_PORT,
        security_firm = None,
    )

    bot = BotState()

    try:
        while True:
            # ── 拉取市场数据 ──────────────────────────────────
            price, ask_depth, bid_depth, imbalance = fetch_market_data(
                quote_ctx, conn
            )
            if price is None:
                log.warning("价格数据未就绪，跳过本轮")
                time.sleep(POLL_INTERVAL)
                continue

            # ── 计算评分 ──────────────────────────────────────
            squeeze_score, sq_reasons = compute_squeeze_score(
                conn, ask_depth, price
            )
            entry_score, sig_type, en_signals = compute_entry_score(
                conn, squeeze_score, price, ask_depth, imbalance
            )
            all_signals = sq_reasons + en_signals

            # ── 打印仪表盘 ────────────────────────────────────
            print_dashboard(bot, price, squeeze_score, entry_score,
                            sig_type, imbalance, all_signals, dry_run)

            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # 状态机逻辑
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

            if bot.trader_state == TraderState.IDLE:
                # ── 入场信号判断 ──────────────────────────────
                entry_ok = (
                    sig_type == "ENTRY"
                    and entry_score >= HIGH_ENTRY_SCORE
                    and squeeze_score < SAFE_SQUEEZE_SCORE
                    and imbalance < ENTRY_IMB_THRESHOLD
                )
                if entry_ok:
                    bot.confirm_rounds += 1
                    bot.last_entry_score = entry_score
                    bot.last_squeeze     = squeeze_score
                    bot.last_imbalance   = imbalance
                    log.info(
                        f"[CONFIRM {bot.confirm_rounds}/{ENTRY_CONFIRM_ROUNDS}] "
                        f"入场评分={entry_score} 逼空={squeeze_score} "
                        f"失衡={imbalance:.3f}"
                    )
                    if bot.confirm_rounds >= ENTRY_CONFIRM_ROUNDS:
                        # ── 确认入场，计算仓位并下单 ──────────
                        qty = (HALF_QTY if entry_score < 75 else FULL_QTY)
                        qty = min(qty, max_qty)

                        ok = place_short_order(trade_ctx, price, qty, dry_run)
                        if ok:
                            bot.position = Position(
                                entry_price = price,
                                qty         = qty,
                                entry_time  = datetime.datetime.now().isoformat("seconds"),
                                stop_price  = stop_price,
                                target1     = target1,
                                target2     = target2,
                            )
                            bot.trader_state   = TraderState.IN_POSITION
                            bot.confirm_rounds = 0
                            log.warning(
                                f"[入场] 做空 {qty}股 @ {price} | "
                                f"入场评分={entry_score} 仓位={'半仓' if qty == HALF_QTY else '满仓'}"
                            )
                            log_trade(
                                conn, "SHORT_OPEN", price, qty, 0.0, 0.0,
                                entry_score, squeeze_score, imbalance,
                                f"仓位={'半仓' if qty == HALF_QTY else '满仓'} score={entry_score}"
                            )
                        else:
                            # 下单失败，重置确认计数
                            bot.confirm_rounds = 0
                else:
                    # 信号中断，重置计数
                    if bot.confirm_rounds > 0:
                        log.info(f"[信号中断] 重置确认计数（当前 sig_type={sig_type}）")
                    bot.confirm_rounds = 0

            elif bot.trader_state in (TraderState.IN_POSITION, TraderState.COVERING):
                pos = bot.position
                assert pos is not None

                # ── 平仓信号判断（优先级从高到低）────────────

                # A：止损触发
                if price >= pos.stop_price:
                    log.warning(
                        f"[止损] 价格 {price} ≥ 止损 {pos.stop_price}，"
                        f"立即全部平仓 {pos.open_qty}股"
                    )
                    ok = place_cover_order(
                        trade_ctx, price, pos.open_qty, dry_run
                    )
                    if ok:
                        pnl = pos.unrealized_pnl(price)
                        pos.realized_pnl += pnl
                        pos.covered_qty   = pos.qty
                        log_trade(
                            conn, "COVER_STOP", price, pos.open_qty,
                            pnl, pos.realized_pnl,
                            bot.last_entry_score, squeeze_score, imbalance,
                            f"止损触发 价格={price} 止损线={pos.stop_price}"
                        )
                        bot.position     = None
                        bot.trader_state = TraderState.IDLE
                        bot.target1_done = False

                # B：逼空紧急平仓
                elif squeeze_score >= EMERGENCY_SQUEEZE:
                    log.warning(
                        f"[紧急平仓] 逼空评分 {squeeze_score} ≥ {EMERGENCY_SQUEEZE}，"
                        f"立即全部平仓 {pos.open_qty}股"
                    )
                    ok = place_cover_order(
                        trade_ctx, price, pos.open_qty, dry_run
                    )
                    if ok:
                        pnl = pos.unrealized_pnl(price)
                        pos.realized_pnl += pnl
                        pos.covered_qty   = pos.qty
                        log_trade(
                            conn, "COVER_SQUEEZE", price, pos.open_qty,
                            pnl, pos.realized_pnl,
                            bot.last_entry_score, squeeze_score, imbalance,
                            f"逼空紧急平仓 squeeze={squeeze_score}"
                        )
                        bot.position     = None
                        bot.trader_state = TraderState.IDLE
                        bot.target1_done = False

                # C：第二目标价 → 全部平仓
                elif price <= pos.target2 and pos.open_qty > 0:
                    log.info(
                        f"[目标②] 价格 {price} ≤ {pos.target2}，"
                        f"全部平仓 {pos.open_qty}股"
                    )
                    ok = place_cover_order(
                        trade_ctx, price, pos.open_qty, dry_run
                    )
                    if ok:
                        pnl = pos.unrealized_pnl(price)
                        pos.realized_pnl += pnl
                        pos.covered_qty   = pos.qty
                        log_trade(
                            conn, "COVER_FULL", price, pos.open_qty,
                            pnl, pos.realized_pnl,
                            bot.last_entry_score, squeeze_score, imbalance,
                            f"第二目标价 {pos.target2}"
                        )
                        bot.position     = None
                        bot.trader_state = TraderState.IDLE
                        bot.target1_done = False

                # D：第一目标价 → 平仓 50%
                elif (price <= pos.target1
                      and not bot.target1_done
                      and bot.trader_state == TraderState.IN_POSITION):
                    half = pos.open_qty // 2
                    if half > 0:
                        log.info(
                            f"[目标①] 价格 {price} ≤ {pos.target1}，"
                            f"平仓 50% ({half}股)"
                        )
                        ok = place_cover_order(trade_ctx, price, half, dry_run)
                        if ok:
                            pnl = (pos.entry_price - price) * half
                            pos.realized_pnl += pnl
                            pos.covered_qty  += half
                            # 止损上移至入场价（锁定利润）
                            pos.stop_price    = pos.entry_price
                            bot.target1_done  = True
                            bot.trader_state  = TraderState.COVERING
                            log_trade(
                                conn, "COVER_PARTIAL", price, half,
                                pnl, pos.realized_pnl,
                                bot.last_entry_score, squeeze_score, imbalance,
                                f"第一目标价 {pos.target1}，止损移至 {pos.entry_price}"
                            )
                            log.info(
                                f"剩余仓位 {pos.open_qty}股，"
                                f"止损已上移至入场价 {pos.entry_price}"
                            )

                else:
                    # 持仓观望
                    pnl = pos.unrealized_pnl(price)
                    log.info(
                        f"[持仓] 价格={price} 盈亏={pnl:+,.0f} | "
                        f"逼空={squeeze_score} 入场评分={entry_score}"
                    )

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        log.info("用户中断。")
    finally:
        quote_ctx.close()
        trade_ctx.close()
        conn.close()
        log.info("连接已关闭。")


# ═══════════════════════════════════════════════════════════
# 十、入口
# ═══════════════════════════════════════════════════════════
def parse_args():
    p = argparse.ArgumentParser(description="MINIMAX-W 模拟自动做空机器人")
    p.add_argument("--qty",     type=int,   default=FULL_QTY,
                   help=f"最大仓位股数，默认 {FULL_QTY}")
    p.add_argument("--stop",    type=float, default=DEFAULT_STOP,
                   help=f"止损价，默认 {DEFAULT_STOP}")
    p.add_argument("--target1", type=float, default=DEFAULT_TARGET1,
                   help=f"第一目标价，默认 {DEFAULT_TARGET1}")
    p.add_argument("--target2", type=float, default=DEFAULT_TARGET2,
                   help=f"第二目标价，默认 {DEFAULT_TARGET2}")
    p.add_argument("--interval", type=int,  default=POLL_INTERVAL,
                   help=f"轮询间隔秒数，默认 {POLL_INTERVAL}")
    p.add_argument("--dry-run", action="store_true",
                   help="仅模拟信号输出，不实际下单")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    POLL_INTERVAL = args.interval
    run(args)
