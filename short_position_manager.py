"""
short_position_manager.py
=========================
空头持仓管理器 — 实时盈亏计算 + 平仓信号监控

功能：
  · 实时计算未实现盈亏（HKD / 百分比）
  · 从 short_data.db 读取空头加权成本线
  · 监控 5 类平仓信号：目标价 / 止损 / 摆盘反转 / 大单托盘 / 收盘保护
  · 声音/日志报警（评分 ≥ 70 → 强提示）

依赖：
    pip install futu-api

用法：
    # 新建仓位（首次运行）
    python3 short_position_manager.py --entry 897 --qty 1000

    # 加载已有仓位文件
    python3 short_position_manager.py

    # 指定所有参数
    python3 short_position_manager.py \\
        --entry 897 --qty 1000 \\
        --stop 950 \\
        --target1 870 --target2 850 \\
        --interval 30
"""

from __future__ import annotations

import os
import sys
import json
import time
import logging
import sqlite3
import datetime
import argparse
import statistics
from dataclasses import dataclass, field, asdict
from typing import Optional

from futu import OpenQuoteContext, SubType, RET_OK

# ═══════════════════════════════════════════════════════════
# 一、默认配置
# ═══════════════════════════════════════════════════════════
SYMBOL         = "HK.00100"
OPEND_HOST     = "127.0.0.1"
OPEND_PORT     = 11111
DB_PATH        = "short_data.db"       # 与 short_squeeze_monitor.py 共享
POSITION_FILE  = "short_position.json" # 持仓快照，跨进程恢复用
POLL_INTERVAL  = 30                    # 轮询秒数（建议 30s，比监控器更频繁）

# 默认止损 / 目标（可被命令行覆盖）
DEFAULT_STOP    = 950.0
DEFAULT_TARGET1 = 870.0
DEFAULT_TARGET2 = 850.0

# 平仓评分阈值
COVER_ALERT_SCORE  = 70   # 强提示
COVER_WARN_SCORE   = 45   # 预警

# 摆盘信号阈值
IMB_REVERSAL_THRESHOLD = 0.20   # 失衡度反转到此值以上 → 买方接管
IMB_REVERSAL_ROUNDS    = 2      # 连续 N 轮触发
ASK_COLLAPSE_PCT       = 45.0   # 卖盘深度较均值骤减 → 卖方撤退
ASK_WINDOW             = 15     # 滚动均值窗口（轮）

# 大单异动阈值
BIGFLOW_SURGE_THRESHOLD = 15_000 * 10_000  # 单轮大单净流入超过此值（HKD）

# 港股收盘提示时间点（24H, HKT = UTC+8）
CLOSING_WARN_TIMES = [(15, 30), (15, 45), (15, 55)]

# ═══════════════════════════════════════════════════════════
# 二、日志
# ═══════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("position_manager.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# 三、持仓数据模型
# ═══════════════════════════════════════════════════════════
@dataclass
class ShortPosition:
    symbol:       str
    entry_price:  float          # 开仓均价（HKD）
    qty:          int            # 股数
    entry_time:   str            # ISO 时间戳
    stop_price:   float          # 止损价（超过此价平仓）
    target1:      float          # 第一目标价（部分平仓）
    target2:      float          # 第二目标价（全部平仓）
    covered_qty:  int   = 0      # 已平仓股数
    realized_pnl: float = 0.0   # 已实现盈亏（HKD）

    @property
    def open_qty(self) -> int:
        return self.qty - self.covered_qty

    def unrealized_pnl(self, current_price: float) -> float:
        """做空盈亏：入场价 > 当前价 为正（盈利）"""
        return (self.entry_price - current_price) * self.open_qty

    def pnl_pct(self, current_price: float) -> float:
        if self.entry_price == 0:
            return 0.0
        return (self.entry_price - current_price) / self.entry_price * 100

    def save(self, path: str = POSITION_FILE):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str = POSITION_FILE) -> "ShortPosition":
        with open(path, encoding="utf-8") as f:
            return cls(**json.load(f))


# ═══════════════════════════════════════════════════════════
# 四、运行时状态
# ═══════════════════════════════════════════════════════════
@dataclass
class RuntimeState:
    current_price:   Optional[float] = None
    ask_depth:       Optional[float] = None
    bid_depth:       Optional[float] = None
    imbalance:       Optional[float] = None
    big_net:         Optional[float] = None   # 累计大单净流入（HKD）
    weighted_cost:   Optional[float] = None   # 空头加权成本线
    momentum_ratio:  Optional[float] = None
    latest_ratio:    Optional[float] = None
    ask_history:     list[float]     = field(default_factory=list)
    imb_history:     list[float]     = field(default_factory=list)
    big_net_history: list[float]     = field(default_factory=list)
    warned_times:    set             = field(default_factory=set)  # 已提示的时间点


# ═══════════════════════════════════════════════════════════
# 五、从 DB 读取 HKEX 成本线
# ═══════════════════════════════════════════════════════════
def load_weighted_cost(db_path: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    从 short_data.db 计算空头加权成本线（近 6 日）。
    返回 (weighted_cost, momentum_ratio, latest_ratio)
    """
    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        rows = conn.execute(
            "SELECT short_volume, short_value, short_ratio "
            "FROM hkex_daily ORDER BY date DESC LIMIT 6"
        ).fetchall()
        conn.close()
    except Exception as e:
        log.warning(f"读取 DB 失败: {e}")
        return None, None, None

    if not rows:
        return None, None, None

    vols   = [r[0] for r in rows]
    vals   = [r[1] for r in rows]
    ratios = [r[2] for r in rows]

    total_val = sum(vals)
    total_vol = sum(vols)
    weighted_cost = total_val / total_vol if total_vol > 0 else None

    avg_ratio_5d  = statistics.mean(ratios[1:]) if len(ratios) > 1 else ratios[0]
    momentum      = ratios[0] / avg_ratio_5d    if avg_ratio_5d > 0 else None

    return weighted_cost, momentum, ratios[0]


# ═══════════════════════════════════════════════════════════
# 六、平仓信号引擎
# ═══════════════════════════════════════════════════════════
@dataclass
class CoverSignal:
    score:      int
    level:      str          # COVER_NOW / REDUCE / HOLD
    reasons:    list[str]
    urgent:     bool = False


def evaluate_cover(pos: ShortPosition, st: RuntimeState) -> CoverSignal:
    """
    综合评估是否应该平仓，返回 0-100 的评分及原因。

    平仓信号分类：
        COVER_NOW (≥70) — 强烈建议立即平仓
        REDUCE    (≥45) — 建议部分平仓 / 减仓
        HOLD      (<45) — 维持空仓
    """
    score   = 0
    reasons: list[str] = []
    urgent  = False

    if st.current_price is None:
        return CoverSignal(0, "HOLD", ["价格数据未就绪"], False)

    price = st.current_price

    # ── 信号 A：止损触发（最高优先级）─────────────────────
    if price >= pos.stop_price:
        pts = 80
        msg = f"!! 价格 {price} ≥ 止损价 {pos.stop_price}，立即止损平仓 !!"
        score += pts
        urgent = True
        reasons.append(msg)
        log.warning(f"[止损] {msg}")

    # ── 信号 B：接近成本线（空头开始亏损）────────────────
    elif st.weighted_cost and price >= st.weighted_cost * 0.97:
        gap = (price - st.weighted_cost) / st.weighted_cost * 100
        if price >= st.weighted_cost:
            pts = 60
            msg = f"价格({price}) 突破空头成本线({st.weighted_cost:.1f})，空头群体亏损 {abs(gap):.1f}%"
        else:
            pts = 35
            msg = f"价格({price}) 距成本线({st.weighted_cost:.1f})仅剩 {abs(gap):.1f}%，高度危险区"
        score += pts
        reasons.append(msg)
        if pts >= 60:
            log.warning(f"[成本线] {msg}")

    # ── 信号 C：第二目标价（全部平仓获利）────────────────
    if price <= pos.target2:
        pts = 70
        pnl = pos.unrealized_pnl(price)
        pnl_pct = pos.pnl_pct(price)
        msg = (f"价格({price}) ≤ 第二目标({pos.target2})，"
               f"建议全部平仓锁定利润 {pnl:+,.0f} HKD ({pnl_pct:+.2f}%)")
        score += pts
        reasons.append(msg)
        log.info(f"[目标价] {msg}")

    # ── 信号 D：第一目标价（部分平仓）────────────────────
    elif price <= pos.target1:
        pts = 40
        pnl = pos.unrealized_pnl(price)
        pnl_pct = pos.pnl_pct(price)
        msg = (f"价格({price}) ≤ 第一目标({pos.target1})，"
               f"建议平仓 50% 锁定利润 {pnl:+,.0f} HKD ({pnl_pct:+.2f}%)")
        score += pts
        reasons.append(msg)

    # ── 信号 E：摆盘失衡反转 ──────────────────────────────
    if len(st.imb_history) >= IMB_REVERSAL_ROUNDS:
        recent_imb = st.imb_history[-IMB_REVERSAL_ROUNDS:]
        if all(v >= IMB_REVERSAL_THRESHOLD for v in recent_imb):
            avg_imb = statistics.mean(recent_imb)
            pts = 30
            msg = (f"摆盘持续偏多 {IMB_REVERSAL_ROUNDS} 轮，"
                   f"均值失衡度 {avg_imb:+.3f}（买方接管）")
            score += pts
            reasons.append(msg)
            log.warning(f"[摆盘反转] {msg}")

    # ── 信号 F：卖盘深度骤减（卖方撤退）──────────────────
    if len(st.ask_history) >= 5 and st.ask_depth is not None:
        avg_ask = statistics.mean(st.ask_history[-min(ASK_WINDOW, len(st.ask_history)):-1])
        if avg_ask > 0:
            shrink = (avg_ask - st.ask_depth) / avg_ask * 100
            if shrink >= ASK_COLLAPSE_PCT:
                pts = 25
                msg = (f"卖盘深度骤减 {shrink:.1f}%"
                       f"（{st.ask_depth:,.0f} vs 均值 {avg_ask:,.0f} 股），"
                       f"卖方主动撤单")
                score += pts
                reasons.append(msg)

    # ── 信号 G：大单净流入突然放大 ───────────────────────
    if len(st.big_net_history) >= 3 and st.big_net is not None:
        prev_avg = statistics.mean(st.big_net_history[-3:-1])
        if (st.big_net > BIGFLOW_SURGE_THRESHOLD
                and st.big_net > prev_avg * 2
                and prev_avg > 0):
            pts = 30
            msg = (f"大单净流入激增至 {st.big_net/10000:+,.1f} 万"
                   f"（前均值 {prev_avg/10000:+,.1f} 万），主力加速托盘")
            score += pts
            reasons.append(msg)
            log.warning(f"[托盘异动] {msg}")

    # ── 信号 H：收盘时间保护 ──────────────────────────────
    now_hkt = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    for warn_h, warn_m in CLOSING_WARN_TIMES:
        key = f"{warn_h}:{warn_m:02d}"
        if (now_hkt.hour == warn_h
                and now_hkt.minute >= warn_m
                and key not in st.warned_times):
            pts = 20 if warn_h == 15 and warn_m == 55 else 10
            msg = f"港股 {key} HKT，收盘前 {16*60 - warn_h*60 - warn_m} 分钟，建议评估是否持仓过夜"
            score += pts
            reasons.append(msg)
            st.warned_times.add(key)

    score = min(score, 100)
    if score >= COVER_NOW_SCORE:
        level = "COVER_NOW"
    elif score >= COVER_WARN_SCORE:
        level = "REDUCE"
    else:
        level = "HOLD"

    return CoverSignal(score=score, level=level, reasons=reasons, urgent=urgent)


# ═══════════════════════════════════════════════════════════
# 七、仪表盘
# ═══════════════════════════════════════════════════════════
COVER_NOW_SCORE = COVER_ALERT_SCORE   # 统一引用


def print_dashboard(pos: ShortPosition, st: RuntimeState, sig: CoverSignal):
    if st.current_price is None:
        return

    price    = st.current_price
    pnl      = pos.unrealized_pnl(price)
    pnl_pct  = pos.pnl_pct(price)
    realized = pos.realized_pnl
    total    = pnl + realized

    def bar(v: int, width: int = 20) -> str:
        n = min(int(v / 100 * width), width)
        return "█" * n + "░" * (width - n)

    # 颜色级别符号
    if sig.level == "COVER_NOW":
        level_str = "!! 立即平仓 !!"
    elif sig.level == "REDUCE":
        level_str = "!  建议减仓 !"
    else:
        level_str = "   持仓观望  "

    pnl_arrow  = "▲" if pnl >= 0 else "▼"
    cost_gap   = ""
    if st.weighted_cost:
        gap_pct = (st.weighted_cost - price) / st.weighted_cost * 100
        cost_gap = f"低于成本线 {gap_pct:.1f}%" if gap_pct >= 0 else f"高于成本线 {abs(gap_pct):.1f}% ⚠"

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 距止损距离
    stop_gap_pct = (pos.stop_price - price) / price * 100

    print(f"""
╔══════════════════════════════════════════════════════════╗
║   MINIMAX-W  空头持仓管理器   {now}        ║
╠══════════════════════════════════════════════════════════╣
║  开仓均价  : {pos.entry_price:>8.2f}    持仓量  : {pos.open_qty:>8,} 股          ║
║  当前价格  : {price:>8.2f}    方  向  : 做空（Short）            ║
╠══════════════════════════════════════════════════════════╣
║  未实现盈亏 : {pnl_arrow} {abs(pnl):>12,.0f} HKD  ({pnl_pct:+.2f}%)        ║
║  已实现盈亏 : {realized:>+14,.0f} HKD                          ║
║  总计盈亏  : {total:>+14,.0f} HKD                          ║
╠══════════════════════════════════════════════════════════╣
║  目标价①  : {pos.target1:>8.2f}  (距当前 {(price-pos.target1)/price*100:+.2f}%)               ║
║  目标价②  : {pos.target2:>8.2f}  (距当前 {(price-pos.target2)/price*100:+.2f}%)               ║
║  止 损 价  : {pos.stop_price:>8.2f}  (距当前 {stop_gap_pct:+.2f}%)               ║
╠══════════════════════════════════════════════════════════╣
║  空头成本线 : {str(f"{st.weighted_cost:.1f}" if st.weighted_cost else "N/A"):>8}  {cost_gap:<28}  ║
║  卖空动能比 : {str(f"{st.momentum_ratio:.2f}×" if st.momentum_ratio else "N/A"):>8}  摆盘失衡: {str(f"{st.imbalance:+.3f}" if st.imbalance is not None else "N/A"):>8}           ║
║  大单净流入 : {str(f"{st.big_net/10000:+,.1f}万" if st.big_net is not None else "N/A"):>14}  卖盘深度: {str(f"{st.ask_depth:,.0f}" if st.ask_depth else "N/A"):>8}  ║
╠══════════════════════════════════════════════════════════╣
║  平仓评分  [{bar(sig.score)}]  {sig.score:3d}/100       ║
║  建  议：{level_str:<46}  ║""")

    if sig.reasons:
        print("╠══════════════════════════════════════════════════════════╣")
        for r in sig.reasons:
            prefix = "!!" if sig.urgent and r == sig.reasons[0] else " →"
            print(f"║ {prefix} {r[:54]:<54} ║")
    print("╚══════════════════════════════════════════════════════════╝")

    if sig.urgent:
        # 终端响铃（部分终端支持）
        print("\a", end="", flush=True)


# ═══════════════════════════════════════════════════════════
# 八、主监控循环
# ═══════════════════════════════════════════════════════════
def run(pos: ShortPosition, interval: int):
    log.info(
        f"持仓监控启动 | 开仓 {pos.entry_price} × {pos.qty}股 | "
        f"止损 {pos.stop_price} | 目标① {pos.target1} ② {pos.target2}"
    )

    st  = RuntimeState()
    ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)

    ret, err = ctx.subscribe([SYMBOL], [SubType.QUOTE, SubType.ORDER_BOOK])
    if ret != RET_OK:
        log.warning(f"订阅失败: {err}")

    # 初始加载成本线
    st.weighted_cost, st.momentum_ratio, st.latest_ratio = load_weighted_cost(DB_PATH)

    try:
        while pos.open_qty > 0:
            # ── 价格 ─────────────────────────────────────────
            ret_q, qdata = ctx.get_stock_quote(code_list=[SYMBOL])
            if ret_q == RET_OK and not qdata.empty:
                st.current_price = float(qdata.iloc[0]["last_price"])

            # ── 摆盘 ─────────────────────────────────────────
            ret_ob, obdata = ctx.get_order_book(SYMBOL, num=10)
            if ret_ob == RET_OK:
                bid_depth = sum(float(x[1]) for x in obdata.get("Bid", []))
                ask_depth = sum(float(x[1]) for x in obdata.get("Ask", []))
                total     = bid_depth + ask_depth
                st.bid_depth  = bid_depth
                st.ask_depth  = ask_depth
                st.imbalance  = (bid_depth - ask_depth) / total if total > 0 else 0.0
                st.ask_history.append(ask_depth)
                st.imb_history.append(st.imbalance)

            # ── 资金流向 ──────────────────────────────────────
            ret_cf, cfdata = ctx.get_capital_distribution(SYMBOL)
            if ret_cf == RET_OK and not cfdata.empty:
                row = cfdata.iloc[0]
                big_in  = float(row.get("capital_in_big",  0) or 0)
                big_out = float(row.get("capital_out_big", 0) or 0)
                st.big_net = big_in - big_out
                st.big_net_history.append(st.big_net)

            # ── 每 10 轮刷新成本线（HKEX 数据每日更新）─────────
            if len(st.ask_history) % 10 == 0:
                st.weighted_cost, st.momentum_ratio, st.latest_ratio = \
                    load_weighted_cost(DB_PATH)

            # ── 平仓信号评估 ──────────────────────────────────
            sig = evaluate_cover(pos, st)

            # ── 打印仪表盘 ────────────────────────────────────
            print_dashboard(pos, st, sig)
            log.info(
                f"价格={st.current_price} | 盈亏={pos.unrealized_pnl(st.current_price or pos.entry_price):+,.0f} | "
                f"平仓评分={sig.score}({sig.level}) | "
                f"失衡={st.imbalance:.3f} | 卖深={st.ask_depth}"
            )

            # ── 强提示时暂停等待用户操作 ─────────────────────
            if sig.level == "COVER_NOW":
                log.warning(f"=== 强烈建议立即平仓！原因: {sig.reasons[0]} ===")

            time.sleep(interval)

    except KeyboardInterrupt:
        log.info("用户中断监控。")
    finally:
        ctx.close()
        pos.save()
        log.info(f"持仓状态已保存至 {POSITION_FILE}")


# ═══════════════════════════════════════════════════════════
# 九、部分平仓记录
# ═══════════════════════════════════════════════════════════
def record_partial_cover(pos: ShortPosition, cover_qty: int, cover_price: float):
    """记录部分平仓，更新已实现盈亏。"""
    cover_qty   = min(cover_qty, pos.open_qty)
    pnl         = (pos.entry_price - cover_price) * cover_qty
    pos.covered_qty  += cover_qty
    pos.realized_pnl += pnl
    pos.save()
    log.info(
        f"平仓记录: {cover_qty}股 @ {cover_price} | "
        f"本次盈亏 {pnl:+,.0f} HKD | 累计已实现 {pos.realized_pnl:+,.0f} HKD"
    )


# ═══════════════════════════════════════════════════════════
# 十、入口
# ═══════════════════════════════════════════════════════════
def parse_args():
    p = argparse.ArgumentParser(description="空头持仓管理器")
    p.add_argument("--entry",    type=float, help="开仓均价（HKD）")
    p.add_argument("--qty",      type=int,   help="持仓股数")
    p.add_argument("--stop",     type=float, default=DEFAULT_STOP,    help=f"止损价，默认 {DEFAULT_STOP}")
    p.add_argument("--target1",  type=float, default=DEFAULT_TARGET1, help=f"第一目标价，默认 {DEFAULT_TARGET1}")
    p.add_argument("--target2",  type=float, default=DEFAULT_TARGET2, help=f"第二目标价，默认 {DEFAULT_TARGET2}")
    p.add_argument("--interval", type=int,   default=POLL_INTERVAL,   help=f"轮询间隔秒数，默认 {POLL_INTERVAL}")
    p.add_argument("--cover",    action="store_true", help="记录部分平仓操作")
    p.add_argument("--cover-qty",   type=int,   default=0,   help="平仓股数（配合 --cover 使用）")
    p.add_argument("--cover-price", type=float, default=0.0, help="平仓价格")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()

    # 优先从文件加载持仓
    if os.path.exists(POSITION_FILE) and not args.entry:
        pos = ShortPosition.load()
        log.info(f"已从 {POSITION_FILE} 加载持仓：{pos.entry_price} × {pos.qty}股")
    elif args.entry and args.qty:
        pos = ShortPosition(
            symbol      = SYMBOL,
            entry_price = args.entry,
            qty         = args.qty,
            entry_time  = datetime.datetime.now().isoformat(timespec="seconds"),
            stop_price  = args.stop,
            target1     = args.target1,
            target2     = args.target2,
        )
        pos.save()
        log.info(f"新建持仓：{pos.entry_price} × {pos.qty}股 已保存")
    else:
        print(
            "用法:\n"
            "  # 新建持仓\n"
            "  python3 short_position_manager.py --entry 897 --qty 1000\n\n"
            "  # 自定义止损和目标\n"
            "  python3 short_position_manager.py --entry 897 --qty 1000 \\\n"
            "      --stop 950 --target1 870 --target2 850\n\n"
            "  # 恢复已有持仓\n"
            "  python3 short_position_manager.py\n\n"
            "  # 记录部分平仓\n"
            "  python3 short_position_manager.py --cover --cover-qty 500 --cover-price 870\n"
        )
        sys.exit(0)

    # 记录部分平仓模式
    if args.cover:
        if args.cover_qty > 0 and args.cover_price > 0:
            record_partial_cover(pos, args.cover_qty, args.cover_price)
            print(f"平仓记录完成，剩余持仓：{pos.open_qty} 股")
        else:
            print("请指定 --cover-qty 和 --cover-price")
        sys.exit(0)

    run(pos, args.interval)
