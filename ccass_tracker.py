#!/usr/bin/env python3
"""
ccass_tracker.py
================
抓取 HKEX 披露易「中央结算系统持股纪录」(CCASS shareholding)，按**参与者(券商/托管行)级**
统计某股票某日持股，并支持**隔日 diff**——看货从哪家通道进/出，与盘中经纪队列足迹交叉验证。

数据源：https://www3.hkexnews.hk/sdw/search/searchsdw.aspx（ASP.NET 表单 POST）
特性：T+1 更新；**参与者/托管行级**（非单一账户——汇丰/花旗代理人下汇集众多客户）；免费公开。
仿 short_squeeze_monitor.scrape_hkex_short 的 requests + 正则解析风格。

用法：
  python3 ccass_tracker.py 00100 --date 2026-06-11            # 当日 Top 持股榜
  python3 ccass_tracker.py 00100 --diff 2026-06-10 2026-06-11 # 隔日持股变动榜(谁增谁减)
  python3 ccass_tracker.py 00100 --date 2026-06-11 --top 30   # 调显示条数
"""
import argparse
import datetime
import logging
import re
import sqlite3
import sys
from html import unescape
from typing import Optional

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ccass")

CCASS_URL = "https://www3.hkexnews.hk/sdw/search/searchsdw.aspx"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": CCASS_URL,
}
DB_PATH = "ccass_data.db"


# ── 抓取 ─────────────────────────────────────────────────────
def scrape_ccass(stock_code: str, date: datetime.date) -> Optional[dict]:
    """
    抓某股票某日的 CCASS 参与者持股。返回 {date, total, rows:[{pid,name,shares,pct}...]}；
    非交易日/无数据/网络异常 → None。

    流程：GET 取 ASP.NET 隐藏 token → POST(股票代码+日期) → 正则解析持股表。
    表结构：每 <tr> 五个 mobile-list-body 值顺序为 [pid, name, address, shareholding, pct]。
    """
    code5 = stock_code.zfill(5)
    s = requests.Session()
    try:
        g = s.get(CCASS_URL, headers=_HEADERS, timeout=15)
        g.raise_for_status()
    except requests.RequestException as e:
        log.warning(f"CCASS GET 失败: {e}")
        return None

    def _hidden(name: str) -> str:
        m = re.search(r'id="%s"[^>]*value="([^"]*)"' % re.escape(name), g.text)
        return m.group(1) if m else ""

    form = {
        "__VIEWSTATE":          _hidden("__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": _hidden("__VIEWSTATEGENERATOR"),
        "__EVENTTARGET":        "btnSearch",
        "__EVENTARGUMENT":      "",
        "today":                _hidden("today"),
        "sortBy":               "shareholding",
        "sortDirection":        "desc",
        "txtStockCode":         code5,
        "txtShareholdingDate":  date.strftime("%Y/%m/%d"),
        "btnSearch.x":          "30",
        "btnSearch.y":          "10",
    }
    try:
        p = s.post(CCASS_URL, data=form, headers=_HEADERS, timeout=20)
        p.raise_for_status()
    except requests.RequestException as e:
        log.warning(f"CCASS POST 失败: {e}")
        return None

    text = p.text
    if "No data found" in text or "No record" in text:
        log.warning(f"CCASS {stock_code} {date}: 无数据（非交易日或该日未公布）")
        return None

    m = re.search(r"<tbody>(.*?)</tbody>", text, re.S)
    if not m:
        log.warning(f"CCASS {stock_code} {date}: 未找到持股表（页面结构可能变更）")
        return None

    rows = []
    for tr in re.findall(r"<tr>(.*?)</tr>", m.group(1), re.S):
        bodies = re.findall(r'mobile-list-body">([^<]*)<', tr)
        if len(bodies) < 5:
            continue
        pid   = unescape(bodies[0]).strip()
        name  = unescape(bodies[1]).strip()
        shares = int(unescape(bodies[3]).strip().replace(",", "") or 0)
        pct   = float(unescape(bodies[4]).strip().rstrip("%") or 0)
        if pid:
            rows.append({"pid": pid, "name": name, "shares": shares, "pct": pct})

    if not rows:
        log.warning(f"CCASS {stock_code} {date}: 解析到 0 行")
        return None

    total = sum(r["shares"] for r in rows)
    log.info(f"CCASS {stock_code} {date}: {len(rows)} 个参与者，合计 {total:,} 股")
    return {"date": date.isoformat(), "total": total, "rows": rows}


# ── 存储 ─────────────────────────────────────────────────────
def init_db(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ccass_holdings (
            stock_code TEXT, date TEXT, pid TEXT,
            name TEXT, shares INTEGER, pct REAL,
            PRIMARY KEY (stock_code, date, pid)
        )""")
    conn.commit()
    return conn


def save(conn: sqlite3.Connection, stock_code: str, data: dict):
    conn.executemany(
        "INSERT OR REPLACE INTO ccass_holdings VALUES (?,?,?,?,?,?)",
        [(stock_code, data["date"], r["pid"], r["name"], r["shares"], r["pct"])
         for r in data["rows"]],
    )
    conn.commit()


def load(conn: sqlite3.Connection, stock_code: str, date_iso: str) -> dict:
    cur = conn.execute(
        "SELECT pid,name,shares,pct FROM ccass_holdings WHERE stock_code=? AND date=?",
        (stock_code, date_iso))
    rows = [{"pid": p, "name": n, "shares": sh, "pct": pc} for p, n, sh, pc in cur]
    return {pid_row["pid"]: pid_row for pid_row in rows}


def ensure(conn: sqlite3.Connection, stock_code: str, date: datetime.date) -> Optional[dict]:
    """DB 有则用，无则抓取入库。返回 {pid: row}。"""
    cached = load(conn, stock_code, date.isoformat())
    if cached:
        return cached
    data = scrape_ccass(stock_code, date)
    if not data:
        return None
    save(conn, stock_code, data)
    return {r["pid"]: r for r in data["rows"]}


# ── 展示 ─────────────────────────────────────────────────────
def cmd_date(stock_code: str, date: datetime.date, top: int):
    conn = init_db()
    holdings = ensure(conn, stock_code, date)
    if not holdings:
        print(f"无 {stock_code} {date} 的 CCASS 数据"); return
    rows = sorted(holdings.values(), key=lambda r: -r["shares"])
    total = sum(r["shares"] for r in rows)
    print("\n" + "═" * 74)
    print(f" CCASS 持股榜  {stock_code}  {date}   参与者 {len(rows)} 家 / 合计 {total:,} 股")
    print("═" * 74)
    print(f" {'参与者':<8}{'名称':<34}{'持股':>14}{'占比':>8}")
    print("─" * 74)
    for r in rows[:top]:
        print(f" {r['pid']:<8}{r['name'][:32]:<34}{r['shares']:>14,}{r['pct']:>7.2f}%")
    print("═" * 74)
    print(" 注：参与者=券商/托管行，非单一账户；T+1 数据，用于复盘货的进出通道。\n")


def cmd_diff(stock_code: str, d1: datetime.date, d2: datetime.date, top: int):
    conn = init_db()
    h1 = ensure(conn, stock_code, d1)
    h2 = ensure(conn, stock_code, d2)
    if not h1 or not h2:
        print(f"缺少 {d1} 或 {d2} 的数据，无法 diff"); return
    pids = set(h1) | set(h2)
    deltas = []
    for pid in pids:
        s1 = h1.get(pid, {}).get("shares", 0)
        s2 = h2.get(pid, {}).get("shares", 0)
        name = (h2.get(pid) or h1.get(pid))["name"]
        if s2 - s1 != 0:
            deltas.append((s2 - s1, pid, name, s1, s2))
    deltas.sort(key=lambda x: -abs(x[0]))

    print("\n" + "═" * 78)
    print(f" CCASS 持股变动  {stock_code}   {d1} → {d2}   （+ 进货 / − 出货）")
    print("═" * 78)
    print(f" {'参与者':<8}{'名称':<30}{'变动':>13}{d1.strftime('%m-%d'):>11}{d2.strftime('%m-%d'):>11}")
    print("─" * 78)
    inc = [d for d in deltas if d[0] > 0][:top]
    dec = [d for d in deltas if d[0] < 0][:top]
    for tag, group in ((" ▲ 增持(进货)", inc), (" ▼ 减持(出货)", dec)):
        print(tag)
        for delta, pid, name, s1, s2 in group:
            print(f" {pid:<8}{name[:28]:<30}{delta:>+13,}{s1:>11,}{s2:>11,}")
    print("═" * 78)
    net_in  = sum(d[0] for d in deltas if d[0] > 0)
    net_out = sum(d[0] for d in deltas if d[0] < 0)
    print(f" 当日累计：增持 {net_in:+,} 股 / 减持 {net_out:+,} 股 / 净 {net_in+net_out:+,} 股")
    print(" 注：CCASS 总量恒定，增减相抵≈0（仅托管搬仓也会体现）；关注**单家骤变**+对照盘口卖一足迹。\n")


def cmd_latest(stock_code: str, top: int):
    """自动回溯近 10 天，找最近两个有 CCASS 数据的交收日并 diff（跳过周末/假期/未公布日）。
    供定时任务用：命令固定、无需手算日期、T+1 未公布时自动退到上一对。"""
    conn = init_db()
    found = []
    d = datetime.date.today()
    for _ in range(10):
        if ensure(conn, stock_code, d):
            found.append(d)
            if len(found) == 2:
                break
        d -= datetime.timedelta(days=1)
    if len(found) < 2:
        print(f"近 10 日内找不到两个有数据的交收日（{stock_code}）"); return
    cmd_diff(stock_code, found[1], found[0], top)   # (较旧, 较新)


def _pdate(s: str) -> datetime.date:
    return datetime.datetime.strptime(s, "%Y-%m-%d").date()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="HKEX CCASS 参与者持股追踪")
    ap.add_argument("stock", help="股票代码，如 00100")
    ap.add_argument("--date", help="查某日持股榜 (YYYY-MM-DD)")
    ap.add_argument("--diff", nargs=2, metavar=("D1", "D2"), help="两日持股变动 (YYYY-MM-DD YYYY-MM-DD)")
    ap.add_argument("--latest", action="store_true", help="自动 diff 最近两个有数据的交收日（定时任务用）")
    ap.add_argument("--top", type=int, default=20, help="显示条数，默认 20")
    args = ap.parse_args()

    if args.latest:
        cmd_latest(args.stock, args.top)
    elif args.diff:
        cmd_diff(args.stock, _pdate(args.diff[0]), _pdate(args.diff[1]), args.top)
    elif args.date:
        cmd_date(args.stock, _pdate(args.date), args.top)
    else:
        print("请用 --date 或 --diff，详见 -h", file=sys.stderr)
        sys.exit(1)
