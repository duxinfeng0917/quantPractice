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

# 信号阈值
SHORT_RATIO_WINDOW   = 5          # 卖空占比趋势回看天数
SHORT_RATIO_RISE_MIN = 3          # 连续上升至少 N 天后才判断为"高位"
ASK_DEPTH_SHRINK_PCT = 30.0       # 卖盘深度较近期均值下降超过此值 → 触发信号（%）
ASK_DEPTH_WINDOW     = 20         # 卖盘深度滚动均值窗口（轮次）
BIGFLOW_REVERSAL_MIN = 2          # 大单净流入连续正值 N 轮 → 触发反转信号
BIGFLOW_WINDOW       = 10         # 大单净流入趋势窗口（轮次）


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
    爬取指定日期 HKEX 卖空成交统计，返回目标股票数据。
    返回 None 表示该日无数据（非交易日或尚未更新）。

    HKEX 表格列（顺序固定）：
        Stock Code | Stock Name | Short Sell Vol | Short Sell Turnover
    总成交量需另行计算或通过富途行情补充；此处以 Short Sell Vol 为主。
    """
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

    try:
        tables = pd.read_html(resp.text, flavor="lxml")
    except Exception as e:
        log.warning(f"HKEX HTML 解析失败 ({date}): {e}")
        return None

    # 找包含 stock_code 的表格
    for tbl in tables:
        tbl.columns = [str(c).strip() for c in tbl.columns]
        # 统一列名：取第一列作为代码列
        code_col = tbl.columns[0]
        tbl[code_col] = tbl[code_col].astype(str).str.strip().str.zfill(5)
        match = tbl[tbl[code_col] == stock_code.zfill(5)]
        if match.empty:
            continue

        row = match.iloc[0]
        cols = list(tbl.columns)

        def _num(idx: int) -> float:
            try:
                return float(str(row.iloc[idx]).replace(",", "").replace("–", "0"))
            except (ValueError, IndexError):
                return 0.0

        # HKEX 标准列顺序：代码、名称、卖空量、卖空金额
        short_vol  = _num(2)   # Short Sell Quantity (shares)
        short_val  = _num(3)   # Short Sell Turnover (HKD)

        return {
            "date":         date.isoformat(),
            "short_volume": short_vol,
            "short_value":  short_val,
        }

    log.warning(f"HKEX 表格中未找到 {stock_code} ({date})")
    return None


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

    # 从富途拿当日总成交量（盘后 K 线）
    total_vol = 0.0
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
        f"金额={hkex['short_value']:,.0f} 占比={ratio:.2f}%"
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
# 八、综合评分与仪表盘
# ═══════════════════════════════════════════════════════════
@dataclass
class MonitorState:
    last_hkex_date: Optional[str] = None   # 上次成功爬取 HKEX 的日期
    last_price:     Optional[float] = None
    latest_hkex_ratio: Optional[float] = None
    latest_big_net: Optional[float] = None
    latest_ask_depth: Optional[float] = None
    latest_imbalance: Optional[float] = None


def print_dashboard(state: MonitorState, score: int, signals: list[str]):
    bar_len = min(score // 5, 20)
    bar     = "█" * bar_len + "░" * (20 - bar_len)
    if score >= 70:
        level = "!! 强警报 !! 逼空概率极高"
    elif score >= 50:
        level = "!  警  报 ! 多信号共振"
    elif score >= 30:
        level = "   预  警   关注异动"
    else:
        level = "   正  常   持续监控"

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"""
╔══════════════════════════════════════════════════════════╗
║    MINIMAX-W (00100.HK)  逼空监控仪表盘   {now}  ║
╠══════════════════════════════════════════════════════════╣
║  最新价         : {str(state.last_price or 'N/A'):>10}                        ║
╠══════════════════════════════════════════════════════════╣
║  [①] HKEX 卖空占比 (今日)  : {str(state.latest_hkex_ratio or 'N/A'):>8} %               ║
║  [②] 大单净流入 (累计)      : {str(f"{state.latest_big_net/10000:+,.1f} 万" if state.latest_big_net is not None else "N/A"):>16}             ║
║  [③] 卖盘深度               : {str(f"{state.latest_ask_depth:,.0f} 股" if state.latest_ask_depth is not None else "N/A"):>16}             ║
║      摆盘失衡度             : {str(f"{state.latest_imbalance:+.3f}" if state.latest_imbalance is not None else "N/A"):>8}                  ║
╠══════════════════════════════════════════════════════════╣
║  逼空评分  [{bar}]  {score:3d}/100          ║
║  状  态：{level:<44}  ║""")
    if signals:
        print("╠══════════════════════════════════════════════════════════╣")
        for s in signals:
            print(f"║  · {s[:54]:<54}  ║")
    print("╚══════════════════════════════════════════════════════════╝")


# ═══════════════════════════════════════════════════════════
# 九、主监控循环
# ═══════════════════════════════════════════════════════════
def run_monitor():
    log.info(f"启动监控: {SYMBOL}，实时轮询 {REALTIME_INTERVAL}s")
    conn  = init_db(DB_PATH)
    state = MonitorState()
    ctx   = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)

    # 订阅实时行情（用于价格）
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
                    state.last_hkex_date   = today_str
                    state.latest_hkex_ratio = round(ratio, 4)

            # ── 获取最新价格 ───────────────────────────────────────
            ret_q, qdata = ctx.get_stock_quote(code_list=[SYMBOL])
            if ret_q == RET_OK and not qdata.empty:
                state.last_price = float(qdata.iloc[0]["last_price"])

            # ── 信号②：资金流向 ───────────────────────────────────
            cf = fetch_capital_flow(ctx, conn)
            if cf:
                state.latest_big_net = cf["big_net"]

            # ── 信号③：摆盘深度 ───────────────────────────────────
            ob = fetch_order_book(ctx, conn)
            if ob:
                state.latest_ask_depth  = ob["ask_depth"]
                state.latest_imbalance  = ob["imbalance"]

            # ── 汇总评分 ──────────────────────────────────────────
            score   = 0
            signals: list[str] = []

            s1, sg1 = analyze_short_ratio_trend(conn)
            s2, sg2 = analyze_capital_flow(conn)
            s3, sg3 = analyze_order_book(conn, state.latest_ask_depth or 0)

            score   = min(s1 + s2 + s3, 100)
            signals = sg1 + sg2 + sg3

            # ── 打印仪表盘 ────────────────────────────────────────
            print_dashboard(state, score, signals)
            log.info(
                f"评分={score} | 卖空占比={state.latest_hkex_ratio}% | "
                f"大单净={state.latest_big_net} | "
                f"卖深={state.latest_ask_depth} | "
                f"失衡={state.latest_imbalance}"
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
