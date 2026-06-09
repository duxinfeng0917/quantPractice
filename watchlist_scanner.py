"""
watchlist_scanner.py
====================
按"六维筛选模型"扫描候选股池，输出 Top N 候选标的及评分明细。

筛选维度（与 memory/squeeze_template_minimax_00100.md 一致）：
    ① HKEX 卖空占比（近 5 日均值，≥ 8% 加分）
    ② 自由流通市值（< 200 亿港元加分）
    ③ 上市时间（< 2 年加分）
    ④ 股权结构（W/B/SW 类加分）
    ⑤ 日均成交额（< 5 亿港元加分）
    ⑥ 单价（> 100 港元加分）
    （借券池紧张 / 题材热度 需人工确认，输出仅提示，不入自动评分）

运行：
    python3 watchlist_scanner.py            # 扫描 shared_config.WATCHLIST
    python3 watchlist_scanner.py 00100 02513  # 临时指定股票

前置条件：
    1. Futu OpenD 已运行（127.0.0.1:11111）
    2. 网络可访问 www.hkex.com.hk
    3. 最佳运行时间：每日 17:30 后（HKEX 当日数据已公布）
"""

from __future__ import annotations

import sys
import json
import datetime
import logging
import statistics
from dataclasses import dataclass, field, asdict
from typing import Optional

from futu import OpenQuoteContext, RET_OK, Market, SecurityType, KLType, KL_FIELD

from shared_config import WATCHLIST, STOCKS
import short_squeeze_monitor as ssm  # 复用 scrape_hkex_short

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("scanner")
# 静音 ssm 模块在 import 时挂的多余 handler 输出
logging.getLogger("short_squeeze_monitor").setLevel(logging.WARNING)


# ─── 评分阈值（与 squeeze_template_minimax_00100.md 一致）────
HKEX_RATIO_THRESHOLD       = 8.0          # %
HKEX_RATIO_LOOKBACK_DAYS   = 5            # 近 N 个交易日均值
FREE_FLOAT_CAP_THRESHOLD   = 200e8        # 港元，自由流通市值
LISTING_YEARS_THRESHOLD    = 2.0          # 年
DAILY_TURNOVER_THRESHOLD   = 5e8          # 港元，日均成交额
PRICE_THRESHOLD            = 100.0        # 港元，单价

# 评分权重
W_HKEX_RATIO  = 3      # 每 1% × 3
W_SMALL_CAP   = 15
W_NEW_LISTING = 10
W_W_CLASS     = 10
W_LOW_LIQ     = 5
W_HIGH_PRICE  = 5

# Top N 输出
# 注意：模板（memory/squeeze_template_minimax_00100.md）写"80 入池 / 90 重点"
# 是含人工维度（借券池 +20 / 题材 +10）的总分。本扫描器仅产出自动分（满分 73），
# 故阈值下调 20 分对应。00100 真标本满分自动 = 73。
TOP_N = 5
WATCH_THRESHOLD = 60   # 自动分入池阈值（人工补 20-30 分后接近 80）
FOCUS_THRESHOLD = 70   # 自动分重点阈值


@dataclass
class Candidate:
    code: str
    name: str = "?"
    last_price: Optional[float] = None
    free_float_cap: Optional[float] = None     # 港元
    total_market_cap: Optional[float] = None
    listing_date: Optional[str] = None
    listing_years: Optional[float] = None
    is_w_class: Optional[bool] = None
    avg_daily_turnover: Optional[float] = None  # 港元，近 20 日均
    hkex_ratios: list[float] = field(default_factory=list)  # 近 N 日卖空占比 %
    hkex_ratio_avg: Optional[float] = None

    score: int = 0
    breakdown: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════
# 静态信息
# ═══════════════════════════════════════════════════════════
def fetch_basic_info(ctx: OpenQuoteContext, code: str, cand: Candidate) -> None:
    """名称 + 上市日期 + W 类判定。"""
    sym = f"HK.{code}"
    ret, df = ctx.get_stock_basicinfo(
        market=Market.HK, stock_type=SecurityType.STOCK, code_list=[sym]
    )
    if ret != RET_OK or df.empty:
        cand.notes.append(f"get_stock_basicinfo 失败: {df if ret != RET_OK else 'empty'}")
        # 回退到 STOCKS 配置里的名字
        if code in STOCKS:
            cand.name = STOCKS[code].get("name", "?")
        return

    row = df.iloc[0]
    cand.name = str(row.get("name", "?"))
    listing = str(row.get("listing_date", ""))
    if listing and listing != "nan":
        cand.listing_date = listing
        try:
            d = datetime.date.fromisoformat(listing[:10])
            cand.listing_years = (datetime.date.today() - d).days / 365.25
        except ValueError:
            pass

    # W/B/SW 类判定：股票名称后缀（港股标准命名）
    name_lower = cand.name.upper()
    cand.is_w_class = (
        name_lower.endswith("-W") or name_lower.endswith("-B")
        or name_lower.endswith("-SW") or "-W " in name_lower
    )


def fetch_snapshot(ctx: OpenQuoteContext, code: str, cand: Candidate) -> None:
    """价格 + 流通市值。"""
    sym = f"HK.{code}"
    ret, df = ctx.get_market_snapshot([sym])
    if ret != RET_OK or df.empty:
        cand.notes.append(f"get_market_snapshot 失败: {df if ret != RET_OK else 'empty'}")
        return

    row = df.iloc[0]
    cand.last_price = float(row.get("last_price", 0)) or None

    # 流通市值字段名在 futu-api 不同版本中可能为 circular_market_val / outstanding_market_val
    for key in ("circular_market_val", "outstanding_market_val"):
        v = row.get(key)
        if v is not None and float(v) > 0:
            cand.free_float_cap = float(v)
            break
    total = row.get("total_market_val")
    if total is not None and float(total) > 0:
        cand.total_market_cap = float(total)

    # 若 snapshot 没拿到流通市值，回退用 总市值（保守，仍可比较）
    if cand.free_float_cap is None and cand.total_market_cap is not None:
        cand.free_float_cap = cand.total_market_cap
        cand.notes.append("流通市值缺失，回退使用总市值")


def fetch_avg_turnover(ctx: OpenQuoteContext, code: str, cand: Candidate,
                       days: int = 20) -> None:
    """近 N 个交易日日均成交额。"""
    sym = f"HK.{code}"
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days * 2)  # 多取几天容忍非交易日
    ret, kl, _ = ctx.request_history_kline(
        sym,
        start=start.isoformat(), end=end.isoformat(),
        ktype=KLType.K_DAY, autype=None,
        fields=[KL_FIELD.TRADE_VAL],   # 成交额；用字符串 "turnover" 会报 fields 类型错
        max_count=days * 2,
    )
    if ret != RET_OK or kl.empty:
        cand.notes.append(f"request_history_kline 失败: {kl if ret != RET_OK else 'empty'}")
        return

    turnovers = [float(v) for v in kl["turnover"].tolist()[-days:] if v and float(v) > 0]
    if turnovers:
        cand.avg_daily_turnover = statistics.mean(turnovers)


def fetch_recent_short_ratios(code: str, cand: Candidate,
                              days: int = HKEX_RATIO_LOOKBACK_DAYS) -> None:
    """近 N 个交易日 HKEX 卖空占比。复用 short_squeeze_monitor.scrape_hkex_short。"""
    today = datetime.date.today()
    ratios: list[float] = []
    days_back = 0
    days_checked = 0
    while len(ratios) < days and days_checked < 20:
        d = today - datetime.timedelta(days=days_back)
        days_back += 1
        days_checked += 1
        # 跳过周末
        if d.weekday() >= 5:
            continue
        try:
            hkex = ssm.scrape_hkex_short(d, stock_code=code)
        except Exception as e:
            cand.notes.append(f"HKEX {d} 抓取异常: {e}")
            continue
        if hkex is None:
            continue
        total_vol = hkex.get("total_volume", 0.0)
        short_vol = hkex.get("short_volume", 0.0)
        if total_vol > 0:
            ratios.append(short_vol / total_vol * 100)

    cand.hkex_ratios = ratios
    if ratios:
        cand.hkex_ratio_avg = statistics.mean(ratios)


# ═══════════════════════════════════════════════════════════
# 评分
# ═══════════════════════════════════════════════════════════
def score(cand: Candidate) -> None:
    s = 0
    bk: list[str] = []

    # 维度 1: HKEX 卖空占比
    if cand.hkex_ratio_avg is not None:
        if cand.hkex_ratio_avg >= HKEX_RATIO_THRESHOLD:
            pts = int(cand.hkex_ratio_avg * W_HKEX_RATIO)
            s += pts
            bk.append(f"HKEX 占比 {cand.hkex_ratio_avg:.2f}% (≥{HKEX_RATIO_THRESHOLD}%) +{pts}")
        else:
            bk.append(f"HKEX 占比 {cand.hkex_ratio_avg:.2f}% (<{HKEX_RATIO_THRESHOLD}%) +0")
    else:
        bk.append("HKEX 占比 数据缺失 +0")

    # 维度 2: 自由流通市值
    if cand.free_float_cap is not None and cand.free_float_cap < FREE_FLOAT_CAP_THRESHOLD:
        s += W_SMALL_CAP
        bk.append(f"流通市值 {cand.free_float_cap/1e8:.0f} 亿 (<200 亿) +{W_SMALL_CAP}")
    elif cand.free_float_cap is not None:
        bk.append(f"流通市值 {cand.free_float_cap/1e8:.0f} 亿 (≥200 亿) +0")

    # 维度 3: 上市时间
    if cand.listing_years is not None and cand.listing_years < LISTING_YEARS_THRESHOLD:
        s += W_NEW_LISTING
        bk.append(f"上市 {cand.listing_years:.1f} 年 (<2 年) +{W_NEW_LISTING}")
    elif cand.listing_years is not None:
        bk.append(f"上市 {cand.listing_years:.1f} 年 (≥2 年) +0")

    # 维度 4: W 类
    if cand.is_w_class:
        s += W_W_CLASS
        bk.append(f"W/B/SW 类 +{W_W_CLASS}")

    # 维度 5: 日均成交额
    if cand.avg_daily_turnover is not None and cand.avg_daily_turnover < DAILY_TURNOVER_THRESHOLD:
        s += W_LOW_LIQ
        bk.append(f"日均成交 {cand.avg_daily_turnover/1e8:.1f} 亿 (<5 亿) +{W_LOW_LIQ}")
    elif cand.avg_daily_turnover is not None:
        bk.append(f"日均成交 {cand.avg_daily_turnover/1e8:.1f} 亿 (≥5 亿) +0")

    # 维度 6: 单价
    if cand.last_price is not None and cand.last_price > PRICE_THRESHOLD:
        s += W_HIGH_PRICE
        bk.append(f"单价 {cand.last_price:.1f} (>100) +{W_HIGH_PRICE}")
    elif cand.last_price is not None:
        bk.append(f"单价 {cand.last_price:.1f} (≤100) +0")

    cand.score = s
    cand.breakdown = bk


# ═══════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════
def scan(codes: list[str]) -> list[Candidate]:
    log.info(f"连接 Futu OpenD...")
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    try:
        candidates: list[Candidate] = []
        for i, code in enumerate(codes, 1):
            log.info(f"[{i}/{len(codes)}] 扫描 {code}...")
            cand = Candidate(code=code)
            try:
                fetch_basic_info(ctx, code, cand)
                fetch_snapshot(ctx, code, cand)
                fetch_avg_turnover(ctx, code, cand)
                fetch_recent_short_ratios(code, cand)
                score(cand)
            except Exception as e:
                log.warning(f"{code} 扫描异常: {e}")
                cand.notes.append(f"扫描异常: {e}")
            candidates.append(cand)
    finally:
        ctx.close()
    return candidates


def format_table(cands: list[Candidate]) -> str:
    cands_sorted = sorted(cands, key=lambda c: -c.score)
    lines: list[str] = []
    lines.append("═" * 70)
    lines.append(f" 候选股扫描结果 {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    lines.append(f" 共扫描 {len(cands)} 只，按综合评分降序")
    lines.append("═" * 70)

    for rank, c in enumerate(cands_sorted, 1):
        tag = "▶▶ 重点" if c.score >= FOCUS_THRESHOLD else \
              "→ 入池"   if c.score >= WATCH_THRESHOLD else "  "
        lines.append(f"\n#{rank:<2}  [{c.score:>3} 分]  {tag}  {c.code}  {c.name}")
        for b in c.breakdown:
            lines.append(f"     {b}")
        if c.hkex_ratios:
            recent = ", ".join(f"{r:.2f}%" for r in c.hkex_ratios)
            lines.append(f"     近 {len(c.hkex_ratios)} 日 HKEX 占比: [{recent}]")
        lines.append(f"     ⚠ 富途融券池状态需人工确认（系统无法获取）")
        if c.notes:
            for note in c.notes:
                lines.append(f"     ℹ {note}")

    # 汇总
    n_focus = sum(1 for c in cands_sorted if c.score >= FOCUS_THRESHOLD)
    n_watch = sum(1 for c in cands_sorted if c.score >= WATCH_THRESHOLD)
    lines.append("\n" + "─" * 70)
    lines.append(
        f" 汇总: {n_focus} 只 ≥ {FOCUS_THRESHOLD} 分（重点）, "
        f"{n_watch} 只 ≥ {WATCH_THRESHOLD} 分（入池）"
    )
    lines.append("─" * 70)
    return "\n".join(lines)


def save_json(cands: list[Candidate], path: str) -> None:
    data = {
        "scan_time": datetime.datetime.now().isoformat(),
        "candidates": [asdict(c) for c in cands],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    log.info(f"明细已保存: {path}")


def main():
    codes = sys.argv[1:] if len(sys.argv) > 1 else WATCHLIST
    if not codes:
        log.error("WATCHLIST 为空，请在 shared_config.py 中添加候选代码")
        sys.exit(1)

    log.info(f"开始扫描 {len(codes)} 只候选: {codes}")
    cands = scan(codes)

    output = format_table(cands)
    print("\n" + output + "\n")

    fname = f"watchlist_scan_{datetime.date.today():%Y%m%d}.json"
    save_json(cands, fname)


if __name__ == "__main__":
    main()
