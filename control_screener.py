"""
control_screener.py
====================
输入一篮子股票代码，筛选出"像 MINIMAX-W(00100) 这类次新小盘、易被主力控盘"的标的，
避开"像中芯国际(00981) 这类纯流动性大盘"。

两段式（对应 [[main-force-control-screening]] 的"先粗筛 → 再盘中确认"工作流）：

  ── Stage 1 静态温床筛（默认，任何时间可跑）──────────────────────
     复用 watchlist_scanner 的日级抓取，算"控盘温床分"(0-100)：
       小流通市值 + 次新 + 低日均成交 + W/B/SW 类 + 高单价 + 高卖空占比。
     这一层就能把 00100（次新/小盘/高卖空 → 高分）和 00981（老股/超大盘/高流动 →
     低分）清晰分开。结论：控盘温床 / 中性 / 流动性大盘(避开)。

  ── Stage 2 盘中控盘确认（--probe，需开盘 + 盘口订阅）──────────────
     对候选做一小段 burst 轮询（默认 8 轮 × 8s ≈ 1 分钟/只），采集价格 + 卖盘深度 +
     经纪最优档席位，喂入临时内存库，复用 short_squeeze_monitor.analyze_main_force_control
     得"主力嫌疑分"(0-100：价格钉扎 + 同侧席位垄断 + 薄盘)。这是控盘的盘口铁证。

运行：
    python3 control_screener.py                      # 扫描 shared_config.WATCHLIST（静态）
    python3 control_screener.py 00100 00981 09660    # 指定一篮子（静态）
    python3 control_screener.py --probe              # 静态 + 盘中控盘确认（开盘时跑）
    python3 control_screener.py --probe --rounds 10 --interval 6 00100 00981

前置：Futu OpenD 运行；静态层需网络可达 hkex.com.hk；--probe 的席位维度需 LV2 BROKER 权限。
"""

from __future__ import annotations

import sys
import time
import json
import sqlite3
import argparse
import datetime
import logging
import statistics
from dataclasses import dataclass, field, asdict
from typing import Optional

from futu import OpenQuoteContext, RET_OK, SubType, KLType, KL_FIELD

from shared_config import WATCHLIST
import short_squeeze_monitor as ssm
import watchlist_scanner as wls

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("control")
logging.getLogger("short_squeeze_monitor").setLevel(logging.WARNING)
logging.getLogger("scanner").setLevel(logging.WARNING)

OPEND_HOST = "127.0.0.1"
OPEND_PORT = 11111

# ── 控盘温床分阈值（分层，越苛刻分越高）────────────────────────────
# 经实盘标定（2026-06-05 00100/00981）：HKD 日均成交额对高价股是劣信号（00100 600 元、
# 16 亿/日看着不低，但盘口仅 ~1 万股极易控）。故主权重给「次新 + 高价 + 薄盘口(股数) + W类」，
# 成交额/流通市值仅作弱辅助（W 类自由流通盘 API 取不准，circular_market_val 近似总股本）。
AGE_TIERS    = [(1.0, 22), (2.0, 15), (3.0, 8)]      # 上市年限（年），越新越像次新控盘
PRICE_TIERS  = [(400, 14), (150, 9), (50, 4)]        # 单价（港元，降序），高价=每手金额大、散户少、易控
BOOK_TIERS   = [(30_000, 22), (80_000, 14), (200_000, 7)]  # 买+卖十档总深度（股），越薄越易控（盘中一次性快照）
TURN_TIERS   = [(3e8, 8), (8e8, 4)]                  # 日均成交额（HKD），弱辅助：仅滤真正沉睡小票
SHORT_TIERS  = [(10.0, 8), (6.0, 4)]                 # HKEX 卖空占比近 5 日均（%，降序），高卖空=逼空温床
W_CLASS_PTS  = 8

INCUBATOR_HOT   = 55   # ≥ → 控盘温床（次新小盘，重点盘中确认）
INCUBATOR_AVOID = 30   # < → 流动性大盘（避开）

# ── 盘中 probe 默认参数 ────────────────────────────────────────
PROBE_ROUNDS   = 8
PROBE_INTERVAL = 8     # 秒


@dataclass
class ControlCandidate:
    code: str
    cand: "wls.Candidate"               # 复用 watchlist_scanner 的静态画像
    book_depth: Optional[float] = None  # 买+卖十档总深度（股），盘中一次性快照
    incubator_score: int = 0            # 控盘温床分（静态）
    incubator_breakdown: list = field(default_factory=list)
    mf_score: Optional[int] = None      # 主力嫌疑分（盘中 probe，未跑则 None）
    mf_label: Optional[str] = None
    mf_sigs: list = field(default_factory=list)
    verdict: str = ""
    notes: list = field(default_factory=list)


# ═══════════════════════════════════════════════════════════
# Stage 1 — 控盘温床分（静态，复用 watchlist_scanner 抓取）
# ═══════════════════════════════════════════════════════════
def _tier_score(value: Optional[float], tiers: list, smaller_is_better: bool = True) -> tuple[int, Optional[float]]:
    """按分层表给分，返回 (分, 命中阈值)。tiers 已按苛刻度降序（阈值小在前）。"""
    if value is None:
        return 0, None
    for thresh, pts in tiers:
        hit = value < thresh if smaller_is_better else value > thresh
        if hit:
            return pts, thresh
    return 0, None


def score_incubator(cc: ControlCandidate) -> None:
    c = cc.cand
    s = 0
    bk: list[str] = []

    pts, _ = _tier_score(c.listing_years, AGE_TIERS)
    s += pts
    bk.append(f"上市 {c.listing_years:.1f} 年 +{pts}" if c.listing_years is not None else "上市年限 缺失 +0")

    pts, _ = _tier_score(c.last_price, PRICE_TIERS, smaller_is_better=False)
    s += pts
    bk.append(f"单价 {c.last_price:.1f} +{pts}" if c.last_price is not None else "单价 缺失 +0")

    # 薄盘口（盘中一次性快照；非交易时段常缺失 → +0 并提示）
    pts, _ = _tier_score(cc.book_depth, BOOK_TIERS)
    s += pts
    if cc.book_depth is not None:
        bk.append(f"盘口深度 {cc.book_depth:,.0f} 股 +{pts}")
    else:
        bk.append("盘口深度 缺失(非交易时段?) +0")

    if c.is_w_class:
        s += W_CLASS_PTS
        bk.append(f"W/B/SW 类 +{W_CLASS_PTS}")

    pts, _ = _tier_score(c.hkex_ratio_avg, SHORT_TIERS, smaller_is_better=False)
    s += pts
    bk.append(f"卖空占比 5日均 {c.hkex_ratio_avg:.1f}% +{pts}" if c.hkex_ratio_avg is not None else "卖空占比 缺失 +0")

    turn_yi = (c.avg_daily_turnover / 1e8) if c.avg_daily_turnover else None
    pts, _ = _tier_score(c.avg_daily_turnover, TURN_TIERS)
    s += pts
    bk.append(f"日均成交 {turn_yi:.1f} 亿 +{pts}" if turn_yi is not None else "日均成交 缺失 +0")

    # 流通市值仅作展示（W 类 API 取不准，不计分）
    cap_yi = (c.free_float_cap / 1e8) if c.free_float_cap else None
    if cap_yi is not None:
        bk.append(f"[参考]流通市值 {cap_yi:.0f} 亿")

    cc.incubator_score = min(s, 100)
    cc.incubator_breakdown = bk


def fetch_book_depth(ctx: OpenQuoteContext, cc: ControlCandidate) -> None:
    """一次性取买+卖十档总深度（股）。盘中有效；非交易时段可能为空/陈旧 → 留 None。"""
    sym = f"HK.{cc.code}"
    ret, _ = ctx.subscribe([sym], [SubType.ORDER_BOOK])
    if ret != RET_OK:
        return
    try:
        ret_b, ob = ctx.get_order_book(sym, num=10)
        if ret_b == RET_OK:
            bid_d = sum(float(i[1]) for i in ob.get("Bid", []))
            ask_d = sum(float(i[1]) for i in ob.get("Ask", []))
            if bid_d + ask_d > 0:
                cc.book_depth = bid_d + ask_d
    finally:
        try:
            ctx.unsubscribe([sym], [SubType.ORDER_BOOK])
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════
# Stage 2 — 盘中控盘确认（burst → 主力嫌疑分）
# ═══════════════════════════════════════════════════════════
def _broker_top(ctx: OpenQuoteContext, sym: str) -> tuple:
    """取经纪队列最优档买/卖各自净不对称最强的席位，返回 (bid_name,bid_net,ask_name,ask_net)。

    复刻 short_squeeze_monitor.fetch_broker_queue 的聚合逻辑（按名 + 最优档 + 净不对称），
    但不写库、不依赖 ssm 的 SYMBOL 全局，供 screener 独立 burst 用。失败返回全 None。
    """
    try:
        ret, bid_frame, ask_frame = ctx.get_broker_queue(sym)
    except Exception:
        return None, None, None, None
    if ret != RET_OK:
        return None, None, None, None

    def _best_counts(frame, name_col, pos_col) -> dict:
        try:
            if frame is None or getattr(frame, "empty", True) \
                    or name_col not in frame.columns or pos_col not in frame.columns:
                return {}
            best = frame[frame[pos_col] == frame[pos_col].min()]
            counts: dict = {}
            for nm in best[name_col].tolist():
                if nm is None or str(nm).strip() == "":
                    continue
                counts[str(nm)] = counts.get(str(nm), 0) + 1
            return counts
        except Exception:
            return {}

    bid_cnt = _best_counts(bid_frame, "bid_broker_name", "bid_broker_pos")
    ask_cnt = _best_counts(ask_frame, "ask_broker_name", "ask_broker_pos")
    if not bid_cnt and not ask_cnt:
        return None, None, None, None

    def _top_net(side_cnt, other_cnt):
        best_name, best_net = None, 0
        for nm, cnt in side_cnt.items():
            net = cnt - other_cnt.get(nm, 0)
            if net > best_net:
                best_name, best_net = nm, net
        return best_name, best_net

    bn, bnet = _top_net(bid_cnt, ask_cnt)
    an, anet = _top_net(ask_cnt, bid_cnt)
    return bn, bnet, an, anet


def _make_probe_db() -> sqlite3.Connection:
    """临时内存库，表结构与 short_squeeze_monitor 一致，供 analyze_main_force_control 读取。"""
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE price_history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, price REAL);
        CREATE TABLE broker_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,
            bid_top_name TEXT, bid_top_net INTEGER, ask_top_name TEXT, ask_top_net INTEGER);
        CREATE TABLE orderbook_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,
            bid_depth REAL, ask_depth REAL, imbalance REAL);
    """)
    return c


def probe_intraday(ctx: OpenQuoteContext, cc: ControlCandidate,
                   rounds: int, interval: int) -> None:
    """对单只标的做 burst 轮询，落入临时库，算主力嫌疑分。"""
    sym = f"HK.{cc.code}"
    ret, err = ctx.subscribe([sym], [SubType.QUOTE, SubType.ORDER_BOOK])
    if ret != RET_OK:
        cc.notes.append(f"probe 订阅失败: {err}")
        return
    ctx.subscribe([sym], [SubType.BROKER])  # LV2 才有，失败忽略（席位维度自动缺省）

    db = _make_probe_db()
    today = datetime.date.today().isoformat()
    got = 0
    for r in range(rounds):
        ts = f"{today}T{datetime.datetime.now():%H:%M:%S}.{r:03d}"
        # 价格
        ret_s, snap = ctx.get_market_snapshot([sym])
        if ret_s == RET_OK and not snap.empty:
            px = float(snap.iloc[0].get("last_price", 0) or 0)
            if px > 0:
                db.execute("INSERT INTO price_history VALUES (NULL,?,?)", (ts, px))
        # 盘口深度
        ret_b, ob = ctx.get_order_book(sym, num=10)
        if ret_b == RET_OK:
            bid_d = sum(float(i[1]) for i in ob.get("Bid", []))
            ask_d = sum(float(i[1]) for i in ob.get("Ask", []))
            tot = bid_d + ask_d
            imb = (bid_d - ask_d) / tot if tot > 0 else 0.0
            db.execute("INSERT INTO orderbook_snapshots VALUES (NULL,?,?,?,?)",
                       (ts, bid_d, ask_d, imb))
        # 经纪席位
        bn, bnet, an, anet = _broker_top(ctx, sym)
        if bn or an:
            db.execute("INSERT INTO broker_queue VALUES (NULL,?,?,?,?,?)",
                       (ts, bn, bnet or 0, an, anet or 0))
        db.commit()
        got += 1
        if r < rounds - 1:
            time.sleep(interval)

    # 样本不足 MF_MIN_ROUNDS 时打分恒为 0，会误显示"未见控盘足迹"；此时不打分、留 None
    n_price = db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    if n_price < ssm.MF_MIN_ROUNDS:
        cc.notes.append(
            f"probe 有效样本 {n_price} < {ssm.MF_MIN_ROUNDS}，不足以判控盘"
            f"（增大 --rounds，建议 ≥{ssm.MF_MIN_ROUNDS}）"
        )
    else:
        score, label, sigs, _tags = ssm.analyze_main_force_control(db)
        cc.mf_score, cc.mf_label, cc.mf_sigs = score, label, sigs
    db.close()
    try:
        ctx.unsubscribe([sym], [SubType.QUOTE, SubType.ORDER_BOOK, SubType.BROKER])
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
# 裁决
# ═══════════════════════════════════════════════════════════
def decide(cc: ControlCandidate, probed: bool) -> None:
    inc = cc.incubator_score
    if inc < INCUBATOR_AVOID:
        cc.verdict = "流动性大盘 · 避开"
        return
    base = "控盘温床" if inc >= INCUBATOR_HOT else "中性偏温床"

    if not probed or cc.mf_score is None:
        if inc >= INCUBATOR_HOT:
            cc.verdict = f"{base} · 重点盘中确认(--probe)"
        else:
            cc.verdict = f"{base} · 可观察"
        return

    mf = cc.mf_score
    if inc >= INCUBATOR_HOT and mf >= 35:
        cc.verdict = "▶▶ 强候选：次新小盘 + 盘中控盘确认"
    elif inc >= INCUBATOR_HOT and mf >= 20:
        cc.verdict = "→ 候选：温床符合，盘中见轻微控盘"
    elif inc >= INCUBATOR_HOT:
        cc.verdict = "温床符合，但盘中未见控盘足迹（非控盘时段/数据不足？）"
    elif mf >= 35:
        cc.verdict = "盘中控盘明显但温床一般，留意"
    else:
        cc.verdict = f"{base} · 暂无盘口控盘证据"


# ═══════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════
def run(codes: list[str], probe: bool, rounds: int, interval: int) -> list[ControlCandidate]:
    if probe:
        now = datetime.datetime.now().time()
        if not (datetime.time(9, 30) <= now <= datetime.time(16, 10)):
            log.warning("当前非港股交易时段，--probe 的盘口/席位数据可能为空（仅静态层有效）")

    log.info("连接 Futu OpenD...")
    ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
    results: list[ControlCandidate] = []
    try:
        for i, code in enumerate(codes, 1):
            log.info(f"[{i}/{len(codes)}] {code} 静态画像...")
            cand = wls.Candidate(code=code)
            try:
                wls.fetch_basic_info(ctx, code, cand)
                wls.fetch_snapshot(ctx, code, cand)
                wls.fetch_avg_turnover(ctx, code, cand)
                wls.fetch_recent_short_ratios(code, cand)
            except Exception as e:
                cand.notes.append(f"静态抓取异常: {e}")
            cc = ControlCandidate(code=code, cand=cand, notes=list(cand.notes))
            try:
                fetch_book_depth(ctx, cc)
            except Exception as e:
                cc.notes.append(f"盘口深度抓取异常: {e}")
            score_incubator(cc)

            if probe and cc.incubator_score >= INCUBATOR_AVOID:
                log.info(f"      └ probe 盘中控盘 {rounds}轮×{interval}s...")
                try:
                    probe_intraday(ctx, cc, rounds, interval)
                except Exception as e:
                    cc.notes.append(f"probe 异常: {e}")
            decide(cc, probed=probe)
            results.append(cc)
    finally:
        ctx.close()
    return results


def format_table(results: list[ControlCandidate], probed: bool) -> str:
    rows = sorted(results, key=lambda x: (-(x.mf_score or -1) if probed else 0,
                                          -x.incubator_score))
    out: list[str] = []
    out.append("═" * 72)
    out.append(f" 控盘标的筛选  {datetime.datetime.now():%Y-%m-%d %H:%M:%S}"
               f"   共 {len(results)} 只" + ("（含盘中 probe）" if probed else "（静态）"))
    out.append("═" * 72)
    for rank, c in enumerate(rows, 1):
        head = f"#{rank:<2} {c.code} {c.cand.name:<10} 温床 {c.incubator_score:>3}/100"
        if probed and c.mf_score is not None:
            head += f" | 主力嫌疑 {c.mf_score:>3}/100 {c.mf_label}"
        out.append(f"\n{head}")
        out.append(f"    裁决: {c.verdict}")
        out.append("    温床: " + " | ".join(c.incubator_breakdown))
        if probed and c.mf_sigs:
            for s in c.mf_sigs:
                out.append(f"    ★ {s}")
        if c.cand.hkex_ratios:
            recent = ", ".join(f"{r:.1f}%" for r in c.cand.hkex_ratios)
            out.append(f"    近 {len(c.cand.hkex_ratios)} 日卖空占比: [{recent}]")
        for n in c.notes:
            out.append(f"    ℹ {n}")
    out.append("\n" + "─" * 72)
    n_strong = sum(1 for c in rows if c.verdict.startswith("▶▶"))
    n_hot = sum(1 for c in rows if c.incubator_score >= INCUBATOR_HOT)
    n_avoid = sum(1 for c in rows if c.incubator_score < INCUBATOR_AVOID)
    out.append(f" 汇总: {n_hot} 只控盘温床, {n_strong} 只盘中强确认, {n_avoid} 只流动性大盘(避开)")
    out.append(" 提示: 富途融券池/借券利率需人工确认；--probe 席位维度需 LV2 权限")
    out.append("─" * 72)
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="控盘标的筛选（次新小盘控盘 vs 流动性大盘）")
    ap.add_argument("codes", nargs="*", help="股票代码（5 位），缺省用 shared_config.WATCHLIST")
    ap.add_argument("--probe", action="store_true", help="加盘中控盘确认（需开盘）")
    ap.add_argument("--rounds", type=int, default=PROBE_ROUNDS, help=f"probe 轮数（默认 {PROBE_ROUNDS}）")
    ap.add_argument("--interval", type=int, default=PROBE_INTERVAL, help=f"probe 间隔秒（默认 {PROBE_INTERVAL}）")
    args = ap.parse_args()

    codes = args.codes or WATCHLIST
    if not codes:
        log.error("无候选代码：传参或在 shared_config.WATCHLIST 添加")
        sys.exit(1)

    if args.probe and args.rounds < ssm.MF_MIN_ROUNDS:
        log.warning(f"--rounds {args.rounds} < 主力嫌疑分最低样本 {ssm.MF_MIN_ROUNDS}，"
                    f"盘中分将无法计算（仅冒烟测试用），实判请用 --rounds ≥{ssm.MF_MIN_ROUNDS}")
    log.info(f"筛选 {len(codes)} 只: {codes}（probe={args.probe}）")
    results = run(codes, args.probe, args.rounds, args.interval)
    print("\n" + format_table(results, args.probe) + "\n")

    fname = f"control_scan_{datetime.date.today():%Y%m%d}.json"
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(
            {"scan_time": datetime.datetime.now().isoformat(), "probe": args.probe,
             "results": [{**asdict(c.cand), "incubator_score": c.incubator_score,
                          "mf_score": c.mf_score, "mf_label": c.mf_label,
                          "verdict": c.verdict} for c in results]},
            f, ensure_ascii=False, indent=2, default=str,
        )
    log.info(f"明细已保存: {fname}")


if __name__ == "__main__":
    main()
