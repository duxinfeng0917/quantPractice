"""
short_squeeze_monitor.py  (普通账户实用版 v2)
=============================================
MINIMAX-W (00100.HK) 逼空行情程序化监控
面向富途普通账户，四路信号并行驱动：

  ① HKEX 网页爬取  —— 每日真实卖空成交量 / 卖空占比（替代融券余量）
  ② 资金流向分析   —— 大单净流入由负转正 → 逼空初期信号
  ③ 摆盘失衡检测   —— 卖盘深度骤减 → 空头回补、余量枯竭代理指标
  ④ 卖空占比趋势   —— 连续 N 日上升后拐头下降 → 逼空启动确认

依赖：
    pip install futu-api pandas requests lxml

前置条件：
    1. 安装并启动 Futu OpenD（https://openapi.futunn.com/futu-api-doc/）
       默认监听 127.0.0.1:11111，需登录有港股实时行情的账户
    2. 网络可访问 www.hkex.com.hk

运行：
    python short_squeeze_monitor.py             # 启动监控
    python short_squeeze_monitor.py signals     # 查看近期信号
    python short_squeeze_monitor.py export      # 导出快照 CSV
    python short_squeeze_monitor.py backfill    # 补抓历史 HKEX 数据（最近 10 日）
"""

from __future__ import annotations

import sys
import time
import logging
import sqlite3
import datetime
import statistics
from dataclasses import dataclass, field
from typing import Optional

import requests
import pandas as pd
from futu import OpenQuoteContext, SubType, RET_OK, Market

# ═══════════════════════════════════════════════════════════
# 一、配置
# ═══════════════════════════════════════════════════════════
SYMBOL        = "HK.00100"        # MINIMAX-W，港交所代码 00100
STOCK_CODE    = "00100"           # 纯代码，HKEX 爬虫用
OPEND_HOST    = "127.0.0.1"
OPEND_PORT    = 11111

# 轮询间隔
REALTIME_INTERVAL = 60            # 实时数据（摆盘/资金流向）轮询间隔（秒）
HKEX_FETCH_HOUR   = 17            # 每日几点后拉取 HKEX 数据（港股 16:00 收盘，17:00 数据稳定）

DB_PATH = "short_data.db"

# 逼空信号阈值
SHORT_RATIO_WINDOW   = 5          # 卖空占比趋势回看天数
SHORT_RATIO_RISE_MIN = 3          # 连续上升至少 N 天后才判断为"高位"
ASK_DEPTH_SHRINK_PCT = 30.0       # 卖盘深度较近期均值下降超过此值 → 触发信号（%）
ASK_DEPTH_WINDOW     = 20         # 卖盘深度滚动均值窗口（轮次）
BIGFLOW_REVERSAL_MIN = 2          # 大单净流入连续正值 N 轮 → 触发反转信号
BIGFLOW_WINDOW       = 10         # 大单净流入趋势窗口（轮次）

# 做空信号阈值
SHORT_SAFE_SQUEEZE   = 25         # 逼空评分超过此值时禁止新开空单
SHORT_EXIT_SQUEEZE   = 40         # 逼空评分超过此值时触发离场警报
SHORT_ASK_SURGE_PCT  = 80.0       # 卖盘深度较均值上升超过此值 → 大卖单出现（%）
SHORT_IMB_THRESHOLD  = -0.30      # 失衡度低于此值视为持续卖压
SHORT_IMB_ROUNDS     = 2          # 连续 N 轮失衡度 < 阈值方触发
SHORT_ENTRY_MIN      = 55         # 做空入场评分门槛（满分 100）
SHORT_PRICE_WINDOW   = 10         # 价格历史窗口（轮次）


# ═══════════════════════════════════════════════════════════
# 二、日志
# ═══════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("short_monitor.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# 三、数据库
# ═══════════════════════════════════════════════════════════
def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS hkex_daily (
            date          TEXT PRIMARY KEY,  -- YYYY-MM-DD
            short_volume  REAL,              -- 当日卖空成交量（股）
            short_value   REAL,              -- 当日卖空成交金额（港元）
            total_volume  REAL,              -- 当日总成交量（股）
            short_ratio   REAL               -- 卖空占比 = short_volume/total_volume (%)
        );

        CREATE TABLE IF NOT EXISTS capital_flow (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT,
            big_in        REAL,   -- 大单流入（万港元）
            big_out       REAL,   -- 大单流出（万港元）
            big_net       REAL,   -- 大单净流入
            mid_net       REAL,   -- 中单净流入
            small_net     REAL    -- 散单净流入
        );

        CREATE TABLE IF NOT EXISTS orderbook_snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT,
            bid_depth     REAL,   -- 买盘总深度（股）
            ask_depth     REAL,   -- 卖盘总深度（股）
            imbalance     REAL    -- (bid-ask)/(bid+ask)，正值偏多
        );

        CREATE TABLE IF NOT EXISTS signals (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT,
            signal_type   TEXT,
            detail        TEXT,
            score         INTEGER
        );

        CREATE TABLE IF NOT EXISTS price_history (
            id     INTEGER PRIMARY KEY AUTOINCREMENT,
            ts     TEXT,
            price  REAL
        );
    """)
    conn.commit()
    return conn


def db_save_hkex(conn: sqlite3.Connection, date: str, sv: float,
                 val: float, tv: float, ratio: float):
    conn.execute(
        "INSERT OR REPLACE INTO hkex_daily VALUES (?,?,?,?,?)",
        (date, sv, val, tv, ratio),
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


def db_save_orderbook(conn: sqlite3.Connection, ts: str,
                      bid: float, ask: float, imb: float):
    conn.execute(
        "INSERT INTO orderbook_snapshots VALUES (NULL,?,?,?,?)",
        (ts, bid, ask, imb),
    )
    conn.commit()


def db_save_signal(conn: sqlite3.Connection, sig_type: str,
                   detail: str, score: int):
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO signals VALUES (NULL,?,?,?,?)",
        (ts, sig_type, detail, score),
    )
    conn.commit()


def db_get_recent_hkex(conn: sqlite3.Connection, n: int) -> list[float]:
    """返回最近 n 个交易日的卖空占比（最新在后）。"""
    rows = conn.execute(
        "SELECT short_ratio FROM hkex_daily ORDER BY date DESC LIMIT ?", (n,)
    ).fetchall()
    return [r[0] for r in reversed(rows)]


def db_get_recent_ask_depth(conn: sqlite3.Connection, n: int) -> list[float]:
    rows = conn.execute(
        "SELECT ask_depth FROM orderbook_snapshots ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()
    return [r[0] for r in rows]


def db_get_recent_big_net(conn: sqlite3.Connection, n: int) -> list[float]:
    rows = conn.execute(
        "SELECT big_net FROM capital_flow ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()
    return [r[0] for r in rows]


def db_save_price(conn: sqlite3.Connection, ts: str, price: float):
    conn.execute("INSERT INTO price_history VALUES (NULL,?,?)", (ts, price))
    conn.commit()


def db_get_recent_prices(conn: sqlite3.Connection, n: int) -> list[float]:
    """返回最近 n 轮价格，最新在后（时间升序）。"""
    rows = conn.execute(
        "SELECT price FROM price_history ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()
    return [r[0] for r in reversed(rows)]


# ═══════════════════════════════════════════════════════════
# 四、信号①  HKEX 每日卖空数据爬取
# ═══════════════════════════════════════════════════════════
_HKEX_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.hkex.com.hk/",
}


def _hkex_url(date: datetime.date) -> str:
    """
    HKEX 每日卖空成交统计页面 URL。
    格式: d{YYMMDD}e.htm，例如 d260415e.htm
    """
    return (
        "https://www.hkex.com.hk/eng/stat/smstat/dayquot/"
        f"d{date.strftime('%y%m%d')}e.htm"
    )


def scrape_hkex_short(date: datetime.date, stock_code: str = STOCK_CODE
                      ) -> Optional[dict]:
    """
    爬取 HKEX Daily Quotations 中的 SHORT SELLING TURNOVER 段落。

    文件格式为固定宽度预格式化文本（<pre> 标签），数据行示例：
        100 MINIMAX-W     323,100   274,762,770   2,149,528   1,869,742,390
    列顺序：CODE  NAME  SHORT_VOL(SH)  SHORT_VALUE($)  TOTAL_VOL(SH)  TOTAL_VALUE($)

    股票代码在文件中为纯整数（100），无前导零。
    """
    import re as _re

    url = _hkex_url(date)
    try:
        resp = requests.get(url, headers=_HKEX_HEADERS, timeout=15)
        if resp.status_code == 404:
            log.debug(f"HKEX {date}: 404，可能为非交易日")
            return None
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning(f"HKEX 请求失败 ({date}): {e}")
        return None

    text = resp.text

    # 定位 short_selling 锚点段落
    m = _re.search(r'<a\s+name\s*=\s*["\']?\s*short_selling\s*["\']?\s*>', text, _re.I)
    if not m:
        log.warning(f"HKEX {date}: 未找到 short_selling 段落")
        return None

    # 截取该段落（取锚点后约 200 KB，足够覆盖所有股票）
    section = text[m.end():]
    # 去除 HTML 标签
    section_clean = _re.sub(r'<[^>]+>', '', section)

    # 目标代码（去除前导零，文件内为纯整数）
    code_int = str(int(stock_code))

    # 匹配：行首空白 + 代码 + 空白 + 名称 + 4 组逗号数字
    # 用 \b 精确匹配代码，避免将 100 匹配到 1001 等
    pattern = _re.compile(
        r'^\s+' + _re.escape(code_int) + r'\b'   # 代码
        r'.+?'                                     # 股票名（非贪婪）
        r'([\d,]+)\s+'                             # SHORT_VOL
        r'([\d,]+)\s+'                             # SHORT_VALUE
        r'([\d,]+)\s+'                             # TOTAL_VOL
        r'([\d,]+)',                               # TOTAL_VALUE
        _re.MULTILINE,
    )
    match = pattern.search(section_clean)
    if not match:
        log.warning(f"HKEX {date}: 数据中未找到代码 {code_int}（{stock_code}）")
        return None

    def _n(s: str) -> float:
        return float(s.replace(",", ""))

    short_vol   = _n(match.group(1))
    short_val   = _n(match.group(2))
    total_vol   = _n(match.group(3))
    total_val   = _n(match.group(4))

    log.debug(
        f"HKEX {date} 原始: 卖空量={short_vol:,.0f} 卖空额={short_val:,.0f} "
        f"总量={total_vol:,.0f} 总额={total_val:,.0f}"
    )
    return {
        "date":         date.isoformat(),
        "short_volume": short_vol,
        "short_value":  short_val,
        "total_volume": total_vol,
        "total_value":  total_val,
    }


def fetch_hkex_and_store(conn: sqlite3.Connection,
                          ctx: OpenQuoteContext,
                          date: datetime.date) -> Optional[float]:
    """
    爬取 HKEX 数据，并从富途获取当日总成交量来计算卖空占比。
    存入 DB，返回卖空占比（%）。
    """
    # 先查 DB，避免重复爬取
    existing = conn.execute(
        "SELECT short_ratio FROM hkex_daily WHERE date=?", (date.isoformat(),)
    ).fetchone()
    if existing:
        log.info(f"HKEX {date} 数据已在库中，跳过爬取")
        return existing[0]

    hkex = scrape_hkex_short(date)
    if hkex is None:
        return None

    # HKEX 文件已包含当日总成交量，无需再调富途 K 线
    total_vol = hkex.get("total_volume", 0.0)

    # 兜底：若 total_vol 为 0 则从富途 K 线补充
    if total_vol == 0 and ctx is not None:
        ret, kl = ctx.get_history_kline(
            SYMBOL,
            start=date.isoformat(), end=date.isoformat(),
            ktype="K_DAY", autype="qfq",
            fields=["volume"],
        )
        if ret == RET_OK and not kl.empty:
            total_vol = float(kl.iloc[-1]["volume"])

    ratio = (hkex["short_volume"] / total_vol * 100) if total_vol > 0 else 0.0

    db_save_hkex(conn, hkex["date"], hkex["short_volume"],
                 hkex["short_value"], total_vol, ratio)

    log.info(
        f"HKEX {date}: 卖空量={hkex['short_volume']:,.0f} "
        f"卖空额={hkex['short_value']:,.0f} "
        f"总量={total_vol:,.0f} 占比={ratio:.2f}%"
    )
    return ratio


# ═══════════════════════════════════════════════════════════
# 五、信号②  资金流向（大单净流入）
# ═══════════════════════════════════════════════════════════
def fetch_capital_flow(ctx: OpenQuoteContext, conn: sqlite3.Connection
                       ) -> Optional[dict]:
    """
    通过 get_capital_distribution 获取当日资金分布快照。
    返回各级别净流入（万港元）。
    普通账户可用，数据为日内累计值，每分钟刷新。
    """
    ret, data = ctx.get_capital_distribution(SYMBOL)
    if ret != RET_OK or data.empty:
        log.warning(f"get_capital_distribution 失败: {data}")
        return None

    row = data.iloc[0]
    # 字段：capital_in_big/mid/small, capital_out_big/mid/small（单位：万港元）
    def _f(col: str) -> float:
        return float(row.get(col, 0) or 0)

    big_in  = _f("capital_in_big")
    big_out = _f("capital_out_big")
    mid_in  = _f("capital_in_mid")
    mid_out = _f("capital_out_mid")
    sml_in  = _f("capital_in_small")
    sml_out = _f("capital_out_small")

    ts = datetime.datetime.now().isoformat(timespec="seconds")
    big_net   = big_in - big_out
    mid_net   = mid_in - mid_out
    small_net = sml_in - sml_out

    db_save_capital(conn, ts, big_in, big_out, big_net, mid_net, small_net)
    return {"ts": ts, "big_net": big_net, "mid_net": mid_net, "small_net": small_net}


def analyze_capital_flow(conn: sqlite3.Connection) -> tuple[int, list[str]]:
    """
    大单净流入逼空信号检测：
    - 连续 BIGFLOW_REVERSAL_MIN 轮 big_net > 0，且此前有负值 → 反转信号
    - 大单净流入持续放大 → 加速信号
    """
    history = db_get_recent_big_net(conn, BIGFLOW_WINDOW)
    if len(history) < BIGFLOW_REVERSAL_MIN + 1:
        return 0, []

    score = 0
    signals: list[str] = []

    recent  = history[:BIGFLOW_REVERSAL_MIN]   # 最新 N 轮（最新在前）
    earlier = history[BIGFLOW_REVERSAL_MIN:]    # 更早期

    all_recent_positive  = all(v > 0 for v in recent)
    had_earlier_negative = any(v < 0 for v in earlier)

    if all_recent_positive and had_earlier_negative:
        pts = 25
        total_inflow = sum(recent) / 10000
        msg = (f"大单净流入反转：连续 {BIGFLOW_REVERSAL_MIN} 轮正值 "
               f"累计 {total_inflow:+,.1f} 万港元 [+{pts}分]")
        score += pts
        signals.append(msg)
        log.warning(f"[资金反转] {msg}")
        db_save_signal(conn, "BIG_FLOW_REVERSAL", msg, pts)

    elif all_recent_positive:
        # 持续净流入但未经历负值阶段，温和加分
        pts = 10
        msg = f"大单净流入持续正值 {BIGFLOW_REVERSAL_MIN} 轮 [+{pts}分]"
        score += pts
        signals.append(msg)

    # 加速判断：最新值是否远大于前几轮均值
    if len(recent) >= 2 and recent[0] > 0:
        avg_prev = statistics.mean(recent[1:]) if len(recent) > 1 else recent[0]
        if avg_prev > 0 and recent[0] > avg_prev * 2:
            pts = 8
            msg = f"大单净流入加速：本轮 {recent[0]/10000:+,.1f} 万 vs 均值 {avg_prev/10000:+,.1f} 万 [+{pts}分]"
            score += pts
            signals.append(msg)

    return score, signals


# ═══════════════════════════════════════════════════════════
# 六、信号③  摆盘失衡（卖盘深度骤减）
# ═══════════════════════════════════════════════════════════
def fetch_order_book(ctx: OpenQuoteContext, conn: sqlite3.Connection
                     ) -> Optional[dict]:
    """
    拉取十档摆盘，计算买/卖盘总深度及失衡度。
    普通账户可用 Level 1（五档）；开通 Level 2 则有十档。
    """
    ret, data = ctx.get_order_book(SYMBOL, num=10)
    if ret != RET_OK:
        log.warning(f"get_order_book 失败: {data}")
        return None

    bid_list = data.get("Bid", [])
    ask_list = data.get("Ask", [])

    bid_depth = sum(float(item[1]) for item in bid_list)
    ask_depth = sum(float(item[1]) for item in ask_list)
    total = bid_depth + ask_depth
    imbalance = (bid_depth - ask_depth) / total if total > 0 else 0.0

    ts = datetime.datetime.now().isoformat(timespec="seconds")
    db_save_orderbook(conn, ts, bid_depth, ask_depth, imbalance)

    return {"ts": ts, "bid_depth": bid_depth,
            "ask_depth": ask_depth, "imbalance": imbalance}


def analyze_order_book(conn: sqlite3.Connection,
                        current_ask: float) -> tuple[int, list[str]]:
    """
    卖盘深度骤减信号：
    - 当前 ask_depth 比近期均值下降超过 ASK_DEPTH_SHRINK_PCT% → 空头回补/做空意愿减弱
    - 买盘深度 > 卖盘深度（正失衡）→ 多头主动接盘
    """
    history = db_get_recent_ask_depth(conn, ASK_DEPTH_WINDOW)
    score = 0
    signals: list[str] = []

    if len(history) >= 5 and current_ask > 0:
        avg_ask = statistics.mean(history[:ASK_DEPTH_WINDOW])
        if avg_ask > 0:
            shrink_pct = (avg_ask - current_ask) / avg_ask * 100
            if shrink_pct >= ASK_DEPTH_SHRINK_PCT:
                pts = 25
                msg = (f"卖盘深度骤减 {shrink_pct:.1f}% "
                       f"(当前 {current_ask:,.0f} vs 均值 {avg_ask:,.0f} 股) [+{pts}分]")
                score += pts
                signals.append(msg)
                log.warning(f"[摆盘预警] {msg}")
                db_save_signal(conn, "ASK_DEPTH_SHRINK", msg, pts)
            elif shrink_pct >= ASK_DEPTH_SHRINK_PCT * 0.6:
                pts = 12
                msg = (f"卖盘深度明显下降 {shrink_pct:.1f}% [+{pts}分]")
                score += pts
                signals.append(msg)

    # 买卖失衡
    imb_rows = conn.execute(
        "SELECT imbalance FROM orderbook_snapshots ORDER BY id DESC LIMIT 3"
    ).fetchall()
    if imb_rows:
        avg_imb = statistics.mean(r[0] for r in imb_rows)
        if avg_imb > 0.15:
            pts = 8
            msg = f"摆盘持续偏多: 近3轮平均失衡度 {avg_imb:.3f} [+{pts}分]"
            score += pts
            signals.append(msg)

    return score, signals


# ═══════════════════════════════════════════════════════════
# 七、信号④  卖空占比趋势（N 日连涨后拐头）
# ═══════════════════════════════════════════════════════════
def analyze_short_ratio_trend(conn: sqlite3.Connection) -> tuple[int, list[str]]:
    """
    卖空占比趋势分析：
    - 连续 SHORT_RATIO_RISE_MIN 天上升后出现下降 → 逼空启动信号（空头开始回补）
    - 占比处于历史高位（>25%）→ 做空拥挤，逼空风险累积
    - 占比极端高位（>35%）→ 强加分
    """
    ratios = db_get_recent_hkex(conn, SHORT_RATIO_WINDOW + 2)
    score = 0
    signals: list[str] = []

    if len(ratios) < SHORT_RATIO_RISE_MIN + 1:
        return 0, []

    latest  = ratios[-1]
    prev    = ratios[-2]
    history = ratios[:-1]

    # 判断此前是否连续上升
    consecutive_rises = 0
    for i in range(len(history) - 1, 0, -1):
        if history[i] > history[i - 1]:
            consecutive_rises += 1
        else:
            break

    # 逼空启动：高位拐头向下（空头回补开始）
    if consecutive_rises >= SHORT_RATIO_RISE_MIN and latest < prev:
        drop = prev - latest
        pts = 25
        msg = (f"卖空占比高位拐头：连涨 {consecutive_rises} 日后回落 "
               f"{prev:.2f}% → {latest:.2f}% (↓{drop:.2f}pp) [+{pts}分]")
        score += pts
        signals.append(msg)
        log.warning(f"[趋势反转] {msg}")
        db_save_signal(conn, "SHORT_RATIO_PEAK", msg, pts)

    # 做空拥挤（高位累积风险）
    if latest >= 35:
        pts = 15
        msg = f"卖空占比极端高位 {latest:.2f}% (≥35%) [+{pts}分]"
        score += pts
        signals.append(msg)
    elif latest >= 25:
        pts = 8
        msg = f"卖空占比高位 {latest:.2f}% (≥25%) [+{pts}分]"
        score += pts
        signals.append(msg)

    # 持续上升（风险累积中）
    if consecutive_rises >= SHORT_RATIO_RISE_MIN and latest >= prev:
        pts = 10
        msg = f"卖空占比已连续上升 {consecutive_rises} 日，当前 {latest:.2f}% [+{pts}分]"
        score += pts
        signals.append(msg)

    return score, signals


# ═══════════════════════════════════════════════════════════
# 七b、HKEX 历史卖空深度分析
# ═══════════════════════════════════════════════════════════

def analyze_hkex_short_momentum(
    conn: sqlite3.Connection,
    current_price: Optional[float],
) -> tuple[int, int, list[str], dict]:
    """
    利用 HKEX 历史卖空数据生成三个量化维度，并返回评分。

    维度1 — 加权空头成本线 (Weighted Short Cost Basis)
        = Σ(short_value) / Σ(short_volume)，近 N 日加权均价
        当前价 vs 成本线决定空头是否承压

    维度2 — 卖空动能比 (Short Momentum Ratio)
        = 最新日占比 / 5日均值占比
        > 1.5× 表示空头加速进场

    维度3 — 卖空量爆量 (Volume Surge)
        = 最新日卖空量 / 5日均值卖空量
        > 2× 表示大规模新增空仓

    返回: (做空支撑分, 逼空风险加成分, signals, stats字典)
    """
    rows = conn.execute(
        """SELECT date, short_volume, short_value, short_ratio
           FROM hkex_daily ORDER BY date DESC LIMIT 10"""
    ).fetchall()

    if len(rows) < 2:
        return 0, 0, [], {}

    # 整理数据（最新在前）
    dates        = [r[0] for r in rows]
    short_vols   = [r[1] for r in rows]
    short_vals   = [r[2] for r in rows]
    short_ratios = [r[3] for r in rows]

    n = min(len(rows), 6)

    # ── 维度1：加权空头成本线 ──────────────────────────────
    total_val = sum(short_vals[:n])
    total_vol = sum(short_vols[:n])
    weighted_cost = (total_val / total_vol) if total_vol > 0 else None

    # ── 维度2：卖空动能比 ──────────────────────────────────
    latest_ratio = short_ratios[0]
    avg_ratio_5d = statistics.mean(short_ratios[1:min(6, len(short_ratios))])
    momentum_ratio = (latest_ratio / avg_ratio_5d) if avg_ratio_5d > 0 else 1.0

    # ── 维度3：卖空量爆量比 ───────────────────────────────
    latest_vol = short_vols[0]
    avg_vol_5d = statistics.mean(short_vols[1:min(6, len(short_vols))])
    volume_surge = (latest_vol / avg_vol_5d) if avg_vol_5d > 0 else 1.0

    # ── 评分（做空支撑分 / 逼空风险加成分）──────────────
    short_support = 0    # 支持做空入场的分数
    squeeze_risk  = 0    # 需叠加到逼空评分的分数
    signals: list[str] = []

    # 价格 vs 空头成本线
    if weighted_cost and current_price:
        gap_pct = (weighted_cost - current_price) / weighted_cost * 100
        if gap_pct > 5:
            pts = 15
            msg = (f"价格({current_price:.1f}) 低于空头成本线({weighted_cost:.1f}) "
                   f"{gap_pct:.1f}%，空头整体盈利 [支撑做空+{pts}分]")
            short_support += pts
            signals.append(msg)
        elif gap_pct < -3:
            pts = 20
            msg = (f"价格({current_price:.1f}) 高于空头成本线({weighted_cost:.1f}) "
                   f"{abs(gap_pct):.1f}%，空头开始亏损 [逼空风险+{pts}分]")
            squeeze_risk += pts
            signals.append(msg)
            db_save_signal(conn, "SQUEEZE_COST_BREACH", msg, pts)
        else:
            signals.append(
                f"价格({current_price:.1f}) 接近空头成本线({weighted_cost:.1f})，"
                f"关键博弈区"
            )

    # 卖空动能比
    if momentum_ratio >= 1.8:
        pts = 20
        msg = (f"卖空动能比 {momentum_ratio:.2f}× (≥1.8×)，"
               f"最新占比{latest_ratio:.2f}% vs 5日均值{avg_ratio_5d:.2f}% "
               f"[支撑做空+{pts}分]")
        short_support += pts
        signals.append(msg)
    elif momentum_ratio >= 1.5:
        pts = 12
        msg = (f"卖空动能比 {momentum_ratio:.2f}× (≥1.5×) "
               f"[支撑做空+{pts}分]")
        short_support += pts
        signals.append(msg)
    elif momentum_ratio < 0.6:
        pts = 10
        msg = f"卖空动能比 {momentum_ratio:.2f}× 空头撤退中 [逼空风险+{pts}分]"
        squeeze_risk += pts
        signals.append(msg)

    # 卖空量爆量
    if volume_surge >= 2.5:
        pts = 15
        msg = (f"卖空量爆量 {volume_surge:.1f}×均值"
               f"（{latest_vol:,.0f} vs 均值{avg_vol_5d:,.0f}股）"
               f"[支撑做空+{pts}分]")
        short_support += pts
        signals.append(msg)
    elif volume_surge >= 1.8:
        pts = 8
        msg = f"卖空量明显放大 {volume_surge:.1f}×均值 [支撑做空+{pts}分]"
        short_support += pts
        signals.append(msg)

    stats = {
        "weighted_cost":  weighted_cost,
        "momentum_ratio": momentum_ratio,
        "volume_surge":   volume_surge,
        "avg_ratio_5d":   avg_ratio_5d,
        "latest_ratio":   latest_ratio,
    }
    return min(short_support, 50), squeeze_risk, signals, stats


# ═══════════════════════════════════════════════════════════
# 八、做空信号引擎
# ═══════════════════════════════════════════════════════════

def analyze_short_entry(
    conn: sqlite3.Connection,
    squeeze_score: int,
    current_price: Optional[float],
    current_ask: float,
    current_imbalance: float,
) -> tuple[int, str, list[str]]:
    """
    做空入场评分（0-100）及信号类型。

    信号类型：
        ENTRY   — 评分 ≥ SHORT_ENTRY_MIN，建议考虑入场
        CAUTION — 评分 ≥ SHORT_ENTRY_MIN×0.6，信号正在积累
        BLOCKED — 逼空评分超过安全线，禁止开空
        HOLD    — 条件不足，继续观望

    评分维度：
        1. 大单净流入由正转负          最高 30 分
        2. 卖盘深度骤增（大卖单出现）  最高 25 分
        3. 摆盘持续偏空                最高 20 分
        4. 价格低于近期高点            最高 15 分
        5. 高点拒绝后连续下行          最高 10 分
    """
    # ── 安全门：逼空风险过高直接拦截 ──────────────────────
    if squeeze_score > SHORT_SAFE_SQUEEZE:
        return 0, "BLOCKED", [
            f"逼空评分={squeeze_score} 超过安全线 {SHORT_SAFE_SQUEEZE}，禁止开空"
        ]

    score   = 0
    signals: list[str] = []

    # ── 维度 1：大单净流入方向 ─────────────────────────────
    big_nets = db_get_recent_big_net(conn, BIGFLOW_WINDOW)
    if len(big_nets) >= 4:
        latest_net  = big_nets[0]
        earlier_net = big_nets[1:5]
        had_positive = any(v > 0 for v in earlier_net)

        if latest_net < 0 and had_positive:
            pts = 30
            msg = (f"大单净流入由正转负：{latest_net / 10000:+,.1f} 万港元 [+{pts}分]")
            score += pts
            signals.append(msg)
            db_save_signal(conn, "SHORT_BIGFLOW_REVERSAL", msg, pts)
        elif latest_net < 0:
            pts = 15
            msg = f"大单净流入持续为负：{latest_net / 10000:+,.1f} 万港元 [+{pts}分]"
            score += pts
            signals.append(msg)

    # ── 维度 2：卖盘深度骤增（大卖单出现）──────────────────
    ask_history = db_get_recent_ask_depth(conn, ASK_DEPTH_WINDOW)
    if len(ask_history) >= 5 and current_ask > 0:
        avg_ask = statistics.mean(ask_history[1:])   # 排除当前轮
        if avg_ask > 0:
            surge_pct = (current_ask - avg_ask) / avg_ask * 100
            if surge_pct >= SHORT_ASK_SURGE_PCT:
                pts = 25
                msg = (f"卖盘深度骤增 {surge_pct:.1f}%（当前 {current_ask:,.0f} "
                       f"vs 均值 {avg_ask:,.0f} 股），大卖单涌入 [+{pts}分]")
                score += pts
                signals.append(msg)
                db_save_signal(conn, "SHORT_ASK_SURGE", msg, pts)
            elif surge_pct >= SHORT_ASK_SURGE_PCT * 0.5:
                pts = 12
                msg = f"卖盘深度明显上升 {surge_pct:.1f}% [+{pts}分]"
                score += pts
                signals.append(msg)

    # ── 维度 3：摆盘持续偏空 ───────────────────────────────
    imb_rows = conn.execute(
        "SELECT imbalance FROM orderbook_snapshots ORDER BY id DESC LIMIT ?",
        (SHORT_IMB_ROUNDS,),
    ).fetchall()
    if len(imb_rows) >= SHORT_IMB_ROUNDS:
        all_neg = all(r[0] < SHORT_IMB_THRESHOLD for r in imb_rows)
        if all_neg:
            avg_imb = statistics.mean(r[0] for r in imb_rows)
            pts = 20
            msg = (f"摆盘持续偏空 {SHORT_IMB_ROUNDS} 轮，"
                   f"均值失衡度 {avg_imb:.3f} [+{pts}分]")
            score += pts
            signals.append(msg)
        elif current_imbalance < SHORT_IMB_THRESHOLD:
            pts = 8
            msg = f"当前摆盘偏空：失衡度 {current_imbalance:.3f} [+{pts}分]"
            score += pts
            signals.append(msg)

    # ── 维度 4：价格低于近期高点（下行动能）────────────────
    prices = db_get_recent_prices(conn, SHORT_PRICE_WINDOW)
    if len(prices) >= 3 and current_price:
        recent_high = max(prices)
        if recent_high > 0:
            drop_pct = (recent_high - current_price) / recent_high * 100
            if drop_pct >= 0.5:
                pts = 15
                msg = (f"价格较近期高点下跌 {drop_pct:.2f}%"
                       f"（当前 {current_price} vs 高点 {recent_high}）[+{pts}分]")
                score += pts
                signals.append(msg)
            elif drop_pct >= 0.2:
                pts = 7
                msg = f"价格轻微回落 {drop_pct:.2f}% [+{pts}分]"
                score += pts
                signals.append(msg)

    # ── 维度 5：高点拒绝后连续下行形态 ─────────────────────
    if len(prices) >= 4:
        peak_idx = prices.index(max(prices))
        if 0 < peak_idx < len(prices) - 1:
            post_peak = prices[peak_idx + 1:]
            drops = sum(1 for i in range(len(post_peak) - 1)
                        if post_peak[i + 1] < post_peak[i])
            if drops >= 2:
                pts = 10
                msg = f"高点拒绝后连续下行 {drops} 轮 [+{pts}分]"
                score += pts
                signals.append(msg)

    score = min(score, 100)
    if score >= SHORT_ENTRY_MIN:
        sig_type = "ENTRY"
    elif score >= int(SHORT_ENTRY_MIN * 0.6):
        sig_type = "CAUTION"
    else:
        sig_type = "HOLD"

    return score, sig_type, signals


def analyze_short_exit(
    conn: sqlite3.Connection,
    squeeze_score: int,
) -> tuple[int, list[str]]:
    """
    做空离场风险评分（0-100）及原因，供已持有空仓时使用。

    紧迫度 ≥ 70 → 立即止损
    紧迫度 40-70 → 减仓
    紧迫度 < 40 → 继续持有
    """
    urgency = 0
    reasons: list[str] = []

    # 1. 逼空风险是最高优先级
    if squeeze_score >= SHORT_EXIT_SQUEEZE:
        urgency = max(urgency, 90)
        msg = f"!! 逼空评分={squeeze_score} 超过离场线 {SHORT_EXIT_SQUEEZE}，立即止损 !!"
        reasons.append(msg)
        db_save_signal(conn, "SHORT_EXIT_SQUEEZE", msg, urgency)
    elif squeeze_score >= SHORT_SAFE_SQUEEZE:
        urgency = max(urgency, 50)
        reasons.append(f"逼空风险上升至 {squeeze_score}，建议减仓")

    # 2. 卖盘深度骤减（护盾消失）
    ask_history = db_get_recent_ask_depth(conn, ASK_DEPTH_WINDOW)
    if len(ask_history) >= 5:
        current_ask = ask_history[0]
        avg_ask = statistics.mean(ask_history[1:6])
        if avg_ask > 0:
            shrink_pct = (avg_ask - current_ask) / avg_ask * 100
            if shrink_pct >= 40:
                urgency = max(urgency, 65)
                msg = f"卖盘深度骤减 {shrink_pct:.1f}%，空头回补迹象，建议减仓"
                reasons.append(msg)
                db_save_signal(conn, "SHORT_EXIT_ASK_SHRINK", msg, 65)

    # 3. 大单净流入强势转正
    big_nets = db_get_recent_big_net(conn, 5)
    if len(big_nets) >= 3:
        recent = big_nets[:3]
        if all(v > 0 for v in recent):
            # 加速转正更危险
            if recent[0] > recent[1] * 1.5 and recent[1] > 0:
                urgency = max(urgency, 70)
                msg = f"大单净流入加速转正（{recent[0]/10000:+,.1f} 万），主力托盘迹象"
                reasons.append(msg)
                db_save_signal(conn, "SHORT_EXIT_BIGFLOW", msg, 70)
            else:
                urgency = max(urgency, 45)
                reasons.append(
                    f"大单净流入连续 3 轮为正（{recent[0]/10000:+,.1f} 万）"
                )

    # 4. 摆盘持续转多
    imb_rows = conn.execute(
        "SELECT imbalance FROM orderbook_snapshots ORDER BY id DESC LIMIT 3"
    ).fetchall()
    if len(imb_rows) >= 3:
        avg_imb = statistics.mean(r[0] for r in imb_rows)
        if avg_imb > 0.30:
            urgency = max(urgency, 55)
            reasons.append(f"摆盘转多，近 3 轮均值失衡度 {avg_imb:+.3f}")

    return urgency, reasons


# ═══════════════════════════════════════════════════════════
# 九、综合评分与仪表盘
# ═══════════════════════════════════════════════════════════
@dataclass
class MonitorState:
    last_hkex_date:    Optional[str]   = None
    last_price:        Optional[float] = None
    latest_hkex_ratio: Optional[float] = None
    latest_big_net:    Optional[float] = None
    latest_ask_depth:  Optional[float] = None
    latest_imbalance:  Optional[float] = None
    short_score:       int             = 0
    short_signal:      str             = "HOLD"
    exit_urgency:      int             = 0
    weighted_cost:     Optional[float] = None   # 空头加权成本线
    momentum_ratio:    Optional[float] = None   # 卖空动能比
    volume_surge:      Optional[float] = None   # 卖空量爆量比
    in_position:       bool            = False     # 手动标记是否持有空仓


def print_dashboard(
    state:          MonitorState,
    squeeze_score:  int,
    squeeze_signals: list[str],
    short_score:    int,
    short_signal:   str,
    short_sigs:     list[str],
    exit_urgency:   int,
    exit_reasons:   list[str],
):
    def bar(v: int) -> str:
        n = min(v // 5, 20)
        return "█" * n + "░" * (20 - n)

    # 逼空状态标签
    if squeeze_score >= 70:
        sq_level = "!! 强警报 !! 逼空概率极高"
    elif squeeze_score >= 50:
        sq_level = "!  警  报 ! 多信号共振  "
    elif squeeze_score >= 30:
        sq_level = "   预  警   关注异动    "
    else:
        sq_level = "   正  常   持续监控    "

    # 做空信号标签
    signal_label = {
        "ENTRY":   "▶▶ 入  场  信  号 ◀◀",
        "CAUTION": "── 信号积累中 观望 ──",
        "BLOCKED": "✖✖ 禁  止  开  空 ✖✖",
        "HOLD":    "── 条件不足  继续等 ──",
    }.get(short_signal, "──────────────────────")

    # 离场紧迫度标签
    if exit_urgency >= 70:
        exit_label = "!! 立即止损 !!"
    elif exit_urgency >= 40:
        exit_label = "!  减仓观察 !"
    else:
        exit_label = "   持仓安全  "

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"""
╔══════════════════════════════════════════════════════════╗
║   MINIMAX-W (00100.HK)  做空监控仪表盘   {now}  ║
╠══════════════════════════════════════════════════════════╣
║  最新价         : {str(state.last_price or 'N/A'):>10}                        ║
╠══════════════════════════════════════════════════════════╣
║  [①] HKEX 卖空占比 (今日)  : {str(state.latest_hkex_ratio or 'N/A'):>8} %  动能{str(f"{state.momentum_ratio:.2f}×" if state.momentum_ratio else "N/A"):>6}  ║
║  [②] 大单净流入 (累计)      : {str(f"{state.latest_big_net/10000:+,.1f} 万" if state.latest_big_net is not None else "N/A"):>16}             ║
║  [③] 卖盘深度               : {str(f"{state.latest_ask_depth:,.0f} 股" if state.latest_ask_depth is not None else "N/A"):>16}             ║
║      摆盘失衡度             : {str(f"{state.latest_imbalance:+.3f}" if state.latest_imbalance is not None else "N/A"):>8}  空头成本线: {str(f"{state.weighted_cost:.1f}" if state.weighted_cost else "N/A"):>8}  ║
╠══════════════════════════════════════════════════════════╣
║  【逼空风险】[{bar(squeeze_score)}] {squeeze_score:3d}/100        ║
║  {sq_level:<52}  ║""")

    if squeeze_signals:
        for s in squeeze_signals:
            print(f"║   ⚠ {s[:52]:<52}  ║")

    print(f"""╠══════════════════════════════════════════════════════════╣
║  【做空入场】[{bar(short_score)}] {short_score:3d}/100        ║
║  {signal_label:<52}  ║""")

    if short_sigs:
        for s in short_sigs:
            print(f"║   → {s[:52]:<52}  ║")

    if state.in_position:
        print(f"""╠══════════════════════════════════════════════════════════╣
║  【持仓离场风险】紧迫度 {exit_urgency:3d}/100  {exit_label:<22}  ║""")
        for r in exit_reasons:
            print(f"║   !! {r[:51]:<51}  ║")

    print("╚══════════════════════════════════════════════════════════╝")


# ═══════════════════════════════════════════════════════════
# 九、主监控循环
# ═══════════════════════════════════════════════════════════
def run_monitor():
    log.info(f"启动监控: {SYMBOL}，实时轮询 {REALTIME_INTERVAL}s")
    log.info("提示：启动后输入 'p' 回车可切换持仓状态（标记是否持有空仓）")
    conn  = init_db(DB_PATH)
    state = MonitorState()
    ctx   = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)

    ret, err = ctx.subscribe([SYMBOL], [SubType.QUOTE, SubType.ORDER_BOOK])
    if ret != RET_OK:
        log.warning(f"订阅失败: {err}（将使用快照模式）")

    try:
        while True:
            now = datetime.datetime.now()
            today_str = now.date().isoformat()

            # ── 每日 HKEX 爬取（收盘后） ──────────────────────────
            if (state.last_hkex_date != today_str
                    and now.hour >= HKEX_FETCH_HOUR):
                ratio = fetch_hkex_and_store(conn, ctx, now.date())
                if ratio is not None:
                    state.last_hkex_date    = today_str
                    state.latest_hkex_ratio = round(ratio, 4)

            # ── 获取最新价格并存入历史 ─────────────────────────────
            ret_q, qdata = ctx.get_stock_quote(code_list=[SYMBOL])
            if ret_q == RET_OK and not qdata.empty:
                state.last_price = float(qdata.iloc[0]["last_price"])
                db_save_price(conn, now.isoformat(timespec="seconds"),
                              state.last_price)

            # ── 信号②：资金流向 ───────────────────────────────────
            cf = fetch_capital_flow(ctx, conn)
            if cf:
                state.latest_big_net = cf["big_net"]

            # ── 信号③：摆盘深度 ───────────────────────────────────
            ob = fetch_order_book(ctx, conn)
            if ob:
                state.latest_ask_depth = ob["ask_depth"]
                state.latest_imbalance = ob["imbalance"]

            # ── HKEX 历史卖空动能分析（日级，每轮都算）────────────
            hkex_support, hkex_squeeze_risk, hkex_sigs, hkex_stats = \
                analyze_hkex_short_momentum(conn, state.last_price)
            if hkex_stats:
                state.weighted_cost  = hkex_stats.get("weighted_cost")
                state.momentum_ratio = hkex_stats.get("momentum_ratio")
                state.volume_surge   = hkex_stats.get("volume_surge")

            # ── 逼空评分（含 HKEX 成本线风险项）─────────────────
            s1, sg1 = analyze_short_ratio_trend(conn)
            s2, sg2 = analyze_capital_flow(conn)
            s3, sg3 = analyze_order_book(conn, state.latest_ask_depth or 0)
            squeeze_score   = min(s1 + s2 + s3 + hkex_squeeze_risk, 100)
            squeeze_signals = sg1 + sg2 + sg3 + [s for s in hkex_sigs if "逼空" in s or "亏损" in s]

            # ── 做空入场评分（HKEX 动能分叠加）──────────────────
            short_score, short_signal, short_sigs = analyze_short_entry(
                conn, squeeze_score,
                state.last_price,
                state.latest_ask_depth or 0,
                state.latest_imbalance or 0,
            )
            short_score = min(short_score + hkex_support, 100)
            short_sigs  = short_sigs + [s for s in hkex_sigs if "支撑做空" in s]
            if short_score >= SHORT_ENTRY_MIN:
                short_signal = "ENTRY"
            elif short_score >= int(SHORT_ENTRY_MIN * 0.6) and short_signal == "HOLD":
                short_signal = "CAUTION"
            state.short_score  = short_score
            state.short_signal = short_signal

            # ── 持仓离场风险（仅在持仓时评估）───────────────────────
            exit_urgency, exit_reasons = 0, []
            if state.in_position:
                exit_urgency, exit_reasons = analyze_short_exit(
                    conn, squeeze_score
                )
                state.exit_urgency = exit_urgency

            # ── 打印仪表盘 ────────────────────────────────────────
            print_dashboard(
                state, squeeze_score, squeeze_signals,
                short_score, short_signal, short_sigs,
                exit_urgency, exit_reasons,
            )
            log.info(
                f"逼空={squeeze_score} | 做空={short_score}({short_signal}) | "
                f"离场紧迫={exit_urgency} | 持仓={state.in_position} | "
                f"大单净={state.latest_big_net} | "
                f"卖深={state.latest_ask_depth} | 失衡={state.latest_imbalance:.3f}"
                if state.latest_imbalance is not None else ""
            )

            time.sleep(REALTIME_INTERVAL)

    except KeyboardInterrupt:
        log.info("用户中断，退出监控。")
    finally:
        ctx.close()
        conn.close()


# ═══════════════════════════════════════════════════════════
# 十、辅助命令
# ═══════════════════════════════════════════════════════════
def cmd_signals(n: int = 30):
    """打印最近 n 条信号记录。"""
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql(
        f"SELECT ts, signal_type, detail, score FROM signals ORDER BY id DESC LIMIT {n}",
        conn,
    )
    conn.close()
    print(df.to_string(index=False))


def cmd_export(out_csv: str = "snapshots_export.csv"):
    """导出摆盘+资金流向快照到 CSV。"""
    conn = sqlite3.connect(DB_PATH)
    df_ob = pd.read_sql("SELECT * FROM orderbook_snapshots ORDER BY id", conn)
    df_cf = pd.read_sql("SELECT * FROM capital_flow ORDER BY id", conn)
    df_hk = pd.read_sql("SELECT * FROM hkex_daily ORDER BY date", conn)
    conn.close()

    df_ob.to_csv("orderbook_" + out_csv, index=False)
    df_cf.to_csv("capital_"   + out_csv, index=False)
    df_hk.to_csv("hkex_"     + out_csv, index=False)
    print(f"已导出: orderbook_{out_csv}, capital_{out_csv}, hkex_{out_csv}")


def cmd_backfill(days: int = 10):
    """
    补抓最近 N 个自然日的 HKEX 数据（跳过非交易日）。
    用于首次运行后初始化趋势分析所需的历史数据。
    """
    conn = init_db(DB_PATH)
    ctx  = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
    today = datetime.date.today()
    fetched = 0
    for delta in range(1, days + 1):
        d = today - datetime.timedelta(days=delta)
        if d.weekday() >= 5:          # 跳过周末
            continue
        ratio = fetch_hkex_and_store(conn, ctx, d)
        if ratio is not None:
            fetched += 1
        time.sleep(1)                  # 礼貌性延迟
    log.info(f"补抓完成，共获取 {fetched} 个交易日数据")
    ctx.close()
    conn.close()


# ═══════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "monitor"

    if cmd == "monitor":
        run_monitor()
    elif cmd == "signals":
        cmd_signals()
    elif cmd == "export":
        cmd_export()
    elif cmd == "backfill":
        cmd_backfill()
    else:
        print(
            "用法:\n"
            "  python short_squeeze_monitor.py             # 启动监控\n"
            "  python short_squeeze_monitor.py backfill    # 补抓历史 HKEX 数据\n"
            "  python short_squeeze_monitor.py signals     # 查看近期信号\n"
            "  python short_squeeze_monitor.py export      # 导出数据 CSV\n"
        )
