#!/usr/bin/env python3
"""
信号回测脚手架（roadmap B1）— 单标的 + 跨标的验证
=====================================================
回放 signals + price_history，统计每类 signal_type 触发后
  +N 分钟 / 当日收盘 / 次日收盘 的前向收益分布、胜率，
并与「无条件基线」（随机入场的同期前向收益）对比。

★ 关键思想：单只票若处单边行情（如 00100 近期 -44%），任何信号的
  「裸胜率」都被趋势污染。所以：
    - 单标的：看 exc = μret − 该标的基线（剔除趋势漂移）
    - 跨标的：把「各标的内的 exc」池化，并看 **方向一致性 agree**
      —— 真边际应在多只票上 exc 同向；趋势红利不会。

用法:
    python backtest_signals.py                         # 单标的(默认 short_data.db)
    python backtest_signals.py --all                   # 跨全部标的(shared_config)
    python backtest_signals.py --dbs short_data.db short_data_06082.db
    python backtest_signals.py --type ICEBERG_DISTRIBUTION --all
    python backtest_signals.py --min-score 8 --all
"""
from __future__ import annotations
import argparse
import csv as _csv
import datetime as dt
import os
import sqlite3
import sys
from bisect import bisect_left, bisect_right

import numpy as np

# ── 信号方向意图表（可按需增改）─────────────────────────────
DIRECTION = {
    # —— 看空 / 派发 / 反向指标 ——
    "ICEBERG_DISTRIBUTION":   "BEAR",
    "BROKER_FOOTPRINT_ASK":   "BEAR",
    "BIGFLOW_PUMP_SUSPECT":   "BEAR",
    "RETAIL_FOMO":            "BEAR",
    "RETAIL_RETREAT":         "BEAR",
    "RETAIL_RETREAT_HEAVY":   "BEAR",
    "DISTRIBUTION_MODE":      "BEAR",
    "CAPITAL_STRUCT_DIVERGE": "BEAR",
    "BIG_FLOW_REVERSAL":      "BEAR",
    "CAPITAL_EFFICIENCY_LOW": "BEAR",
    "SELL_NO_DROP":           "BEAR",
    # —— 看多 / 吸筹 / 逼空 ——
    "ICEBERG_ACCUMULATION":   "BULL",
    "BROKER_FOOTPRINT_BID":   "BULL",
    "ASK_DEPTH_SHRINK":       "BULL",
    "BIG_FLOW_REBUY":         "BULL",
    "SHORT_EXIT_SQUEEZE":     "BULL",
    # —— 中性 / 过滤（不判胜负）——
    "SHORT_BLOCK_OVERRIDE":   None,
    "SHORT_ASK_SURGE_TRAP":   None,
    "SHORT_IMB_FLIP_TRAP":    None,
}

DEFAULT_HORIZONS_MIN = [5, 15, 30, 60]
FWD_TOLERANCE_MIN = 8
MIN_N_PER_INSTRUMENT = 5      # 跨标的：某标的内样本≥该值才计入一致性投票
RIGOR_DEDUP_MIN = 30          # --rigor 默认去重叠冷却期(分钟)
RIGOR_BOOTSTRAP = 2000        # --rigor 默认自助重采样次数
RIGOR_LABELS = ["+60m", "EOD", "NXT"]   # 显著性检验关注的时段


def _parse_ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace(" ", "T"))


def _stock_label(db_path: str) -> str:
    try:
        import shared_config as sc
        for code, cfg in sc.STOCKS.items():
            if os.path.basename(cfg.get("db_path", "")) == os.path.basename(db_path):
                return f"{code} {cfg.get('name','')}"
    except Exception:
        pass
    return os.path.basename(db_path)


def load_prices(conn):
    rows = conn.execute(
        "SELECT ts, price FROM price_history WHERE price IS NOT NULL ORDER BY ts"
    ).fetchall()
    ts = np.array([_parse_ts(r[0]).timestamp() for r in rows], dtype=float)
    px = np.array([float(r[1]) for r in rows], dtype=float)
    dates = [_parse_ts(r[0]).date() for r in rows]
    day_last: dict[dt.date, int] = {}
    for i, d in enumerate(dates):
        day_last[d] = i
    return ts, px, dates, day_last, sorted(day_last.keys())


def price_at_or_before(ts_arr, px_arr, t):
    i = bisect_right(ts_arr, t) - 1
    return (px_arr[i], i) if i >= 0 else (None, -1)


def price_forward(ts_arr, px_arr, dates, entry_idx, t_target, tol_sec):
    i = bisect_left(ts_arr, t_target)
    if i >= len(ts_arr):
        return None
    if dates[i] != dates[entry_idx] or ts_arr[i] - t_target > tol_sec:
        return None
    return px_arr[i]


def next_day_close(day_last, sorted_days, entry_date):
    j = bisect_right(sorted_days, entry_date)
    return day_last[sorted_days[j]] if j < len(sorted_days) else None


def forward_returns(ts_arr, px_arr, dates, day_last, sorted_days, t0, horizons_min, tol_sec):
    entry_px, ei = price_at_or_before(ts_arr, px_arr, t0)
    out = {}
    if entry_px is None or entry_px <= 0:
        return None, out
    for h in horizons_min:
        fp = price_forward(ts_arr, px_arr, dates, ei, t0 + h * 60, tol_sec)
        out[f"+{h}m"] = (fp / entry_px - 1) * 100 if fp else np.nan
    out["EOD"] = (px_arr[day_last[dates[ei]]] / entry_px - 1) * 100
    nidx = next_day_close(day_last, sorted_days, dates[ei])
    out["NXT"] = (px_arr[nidx] / entry_px - 1) * 100 if nidx is not None else np.nan
    return entry_px, out


def compute_baseline(ts_arr, px_arr, dates, day_last, sorted_days, horizons_min, tol_sec, step=4):
    labels = [f"+{h}m" for h in horizons_min] + ["EOD", "NXT"]
    acc = {k: [] for k in labels}
    for i in range(0, len(ts_arr), step):
        _, rets = forward_returns(ts_arr, px_arr, dates, day_last, sorted_days,
                                  ts_arr[i], horizons_min, tol_sec)
        for k, v in rets.items():
            if v == v:
                acc[k].append(v)
    return {k: (np.mean(v) if v else np.nan) for k, v in acc.items()}


def analyze_db(db_path, horizons, labels, type_filter, min_score, since, tol_sec):
    """返回该标的: per_type{raw, exc}, baseline, meta(信号数/价格数/期间涨跌)。"""
    conn = sqlite3.connect(db_path)
    ts_arr, px_arr, dates, day_last, sorted_days = load_prices(conn)
    if len(ts_arr) == 0:
        conn.close()
        return None
    base = compute_baseline(ts_arr, px_arr, dates, day_last, sorted_days, horizons, tol_sec)

    q = "SELECT ts, signal_type, score FROM signals WHERE 1=1"
    params = []
    if type_filter:
        q += " AND signal_type=?"; params.append(type_filter)
    if min_score is not None:
        q += " AND score>=?"; params.append(min_score)
    if since:
        q += " AND ts>=?"; params.append(since)
    sig_rows = conn.execute(q + " ORDER BY ts", params).fetchall()

    label_txt = _stock_label(db_path)
    per_type = {}    # stype -> {'raw':{label:[]}, 'exc':{label:[]}}
    events = {}      # stype -> [ {ts, day, exc:{label:val}} ]  (用于去重叠+自助法)
    for ts_s, stype, _score in sig_rows:
        epoch = _parse_ts(ts_s).timestamp()
        _, rets = forward_returns(ts_arr, px_arr, dates, day_last, sorted_days,
                                  epoch, horizons, tol_sec)
        if not rets:
            continue
        d = per_type.setdefault(stype, {'raw': {k: [] for k in labels},
                                        'exc': {k: [] for k in labels}})
        ev_exc = {}
        for k in labels:
            if rets[k] == rets[k]:
                d['raw'][k].append(rets[k])
                if base[k] == base[k]:
                    e = rets[k] - base[k]
                    d['exc'][k].append(e)
                    ev_exc[k] = e
        events.setdefault(stype, []).append(
            {"ts": epoch, "day": _parse_ts(ts_s).date(), "inst": label_txt, "exc": ev_exc})
    conn.close()
    trend = (px_arr[-1] / px_arr[0] - 1) * 100
    meta = {"label": label_txt, "n_sig": len(sig_rows),
            "n_px": len(ts_arr), "d0": dates[0], "d1": dates[-1], "trend": trend}
    return {"per_type": per_type, "events": events, "baseline": base, "meta": meta}


def win_pct(vals, direction):
    if not vals or direction not in ("BEAR", "BULL"):
        return None
    if direction == "BEAR":
        return 100 * np.mean([1 if v < 0 else 0 for v in vals])
    return 100 * np.mean([1 if v > 0 else 0 for v in vals])


# ════════════════════════════════════════════════════════════
def report_single(res, labels):
    m = res["meta"]; base = res["baseline"]; per_type = res["per_type"]
    print(f"\n[单标的] {m['label']}  信号 {m['n_sig']}  价格 {m['n_px']}  "
          f"{m['d0']}→{m['d1']}  期间涨跌 {m['trend']:+.1f}%")
    print("说明: μret/win  (exc 行=μret−基线)\n")
    hdr = f"{'signal_type':<24}{'dir':<4}{'n':>5}  " + "  ".join(f"{l:>16}" for l in labels)
    print(hdr); print("-" * len(hdr))
    bl = f"{'<BASELINE>':<24}{'-':<4}{'-':>5}  " + "  ".join(
        f"{(f'{base[k]:+6.2f}' if base[k]==base[k] else '—'):>16}" for k in labels)
    print(bl); print("-" * len(hdr))
    for stype in sorted(per_type, key=lambda s: -max(len(per_type[s]['raw'][k]) for k in labels)):
        d = per_type[stype]; direction = DIRECTION.get(stype, "?")
        dtxt = {"BEAR": "空", "BULL": "多", None: "中", "?": "?"}[direction]
        nmax = max(len(d['raw'][k]) for k in labels)
        cells = []
        for k in labels:
            raw = d['raw'][k]
            if not raw:
                cells.append(f"{'—':>16}"); continue
            w = win_pct(raw, direction)
            wt = f"{w:.0f}%" if w is not None else "-"
            cells.append(f"{np.mean(raw):+6.2f}/{wt:>4}".rjust(16))
        print(f"{stype:<24}{dtxt:<4}{nmax:>5}  " + "  ".join(cells))
        if direction in ("BEAR", "BULL"):
            exc = [f"{np.mean(d['exc'][k]):+5.2f}" if d['exc'][k] else "—" for k in labels]
            print(f"{'  └ exc':<24}{'':<4}{'':>5}  " + "  ".join(f"{e:>16}" for e in exc))
    print("-" * len(hdr))


def report_pooled(results, labels):
    """跨标的：池化各标的内 exc，并算方向一致性。"""
    insts = [r for r in results if r]
    print(f"\n[跨标的验证] 标的数: {len(insts)}")
    print(f"{'标的':<16}{'信号':>6}{'价格':>7}  {'期间':<24}{'涨跌':>8}")
    for r in insts:
        m = r["meta"]
        print(f"{m['label']:<16}{m['n_sig']:>6}{m['n_px']:>7}  "
              f"{str(m['d0'])+'→'+str(m['d1']):<24}{m['trend']:>+7.1f}%")

    # 汇总每个 signal_type：池化 exc + 每标的内均值(用于一致性投票)
    agg = {}  # stype -> {label:[pooled exc...]}, and per-inst mean for agreement
    inst_mean = {}  # stype -> {label: [per-inst mean exc ...]}
    raw_pool = {}   # stype -> {label:[raw...]}  for win%
    for r in insts:
        for stype, d in r["per_type"].items():
            a = agg.setdefault(stype, {k: [] for k in labels})
            im = inst_mean.setdefault(stype, {k: [] for k in labels})
            rp = raw_pool.setdefault(stype, {k: [] for k in labels})
            for k in labels:
                if d['exc'][k]:
                    a[k].extend(d['exc'][k])
                    if len(d['exc'][k]) >= MIN_N_PER_INSTRUMENT:
                        im[k].append(np.mean(d['exc'][k]))
                rp[k].extend(d['raw'][k])

    print("\nPOOLED  exc=各标的内(ret−基线)池化均值  agree=NXT上exc同向的标的数/计票标的数")
    hdr = f"{'signal_type':<24}{'dir':<4}{'n':>6}  " + "  ".join(f"{l+'_exc':>12}" for l in labels) + "   agree(NXT)"
    print(hdr); print("-" * len(hdr))
    for stype in sorted(agg, key=lambda s: -max(len(agg[s][k]) for k in labels)):
        direction = DIRECTION.get(stype, "?")
        dtxt = {"BEAR": "空", "BULL": "多", None: "中", "?": "?"}[direction]
        nmax = max(len(agg[stype][k]) for k in labels)
        cells = [f"{np.mean(agg[stype][k]):+6.2f}".rjust(12) if agg[stype][k] else f"{'—':>12}"
                 for k in labels]
        # 一致性：NXT 上每标的均值的符号是否与意图一致
        ag = "-"
        means = inst_mean[stype]["NXT"]
        if means and direction in ("BEAR", "BULL"):
            want_neg = direction == "BEAR"
            good = sum(1 for x in means if (x < 0) == want_neg)
            ag = f"{good}/{len(means)}" + (" ✓" if len(means) >= 2 and good == len(means) else "")
        print(f"{stype:<24}{dtxt:<4}{nmax:>6}  " + "  ".join(cells) + f"   {ag}")
    print("-" * len(hdr))
    print("解读: 跨标的 exc 同向(agree 满票✓)且 exc 量级大 = 真边际,非单只票趋势红利。")
    print("      agree 标的数少(<2)或 n 小 = 证据不足。 dir='?' 需在脚本顶部 DIRECTION 补类。\n")


def dedup_events(evs, cooldown_sec):
    """同标的内贪婪最小间隔去重叠：按时间走,保留一条后冷却期内同类型跳过。"""
    by_inst = {}
    for e in evs:
        by_inst.setdefault(e["inst"], []).append(e)
    kept = []
    for inst, lst in by_inst.items():
        lst.sort(key=lambda x: x["ts"])
        last = -1e18
        for e in lst:
            if e["ts"] - last >= cooldown_sec:
                kept.append(e); last = e["ts"]
    return kept


def block_bootstrap(events, label, direction, B):
    """以 (标的,交易日) 为块重采样,返回 (点估计, lo, hi, 块数, 是否显著)。"""
    blocks = {}
    for e in events:
        if label in e["exc"]:
            blocks.setdefault((e["inst"], e["day"]), []).append(e["exc"][label])
    bids = list(blocks)
    if len(bids) < 2:
        return None
    allv = [v for b in bids for v in blocks[b]]
    point = float(np.mean(allv))
    rng = np.random.default_rng(42)
    means = np.empty(B)
    for i in range(B):
        pick = rng.integers(0, len(bids), len(bids))
        vals = []
        for j in pick:
            vals.extend(blocks[bids[j]])
        means[i] = np.mean(vals)
    lo, hi = np.percentile(means, [2.5, 97.5])
    if direction == "BEAR":
        sig = hi < 0
    elif direction == "BULL":
        sig = lo > 0
    else:
        sig = False
    return point, float(lo), float(hi), len(bids), sig


def report_rigor(results, cooldown_min, B):
    """去重叠 + 块自助法显著性表(以 +60m/EOD/NXT 为重点时段)。"""
    cooldown = cooldown_min * 60
    # 汇总每类型的事件(跨标的),并去重叠
    all_events = {}
    for r in results:
        if not r:
            continue
        for stype, evs in r["events"].items():
            all_events.setdefault(stype, []).extend(evs)
    deduped = {s: dedup_events(e, cooldown) for s, e in all_events.items()}

    print(f"\n[严谨模式] 去重叠冷却={cooldown_min}min  自助重采样={B}次  "
          f"块=(标的,交易日)  CI=95%")
    print("判定: 看空信号 CI 上界<0 / 看多信号 CI 下界>0 → 统计显著 ✓\n")
    hdr = (f"{'signal_type':<24}{'dir':<4}{'evt':>5}{'blk':>5}  "
           + "  ".join(f"{l+' exc[95%CI]':>26}" for l in RIGOR_LABELS) + "  显著")
    print(hdr); print("-" * len(hdr))

    def cell(res):
        if res is None:
            return f"{'(块<2)':>26}"
        pt, lo, hi, _, _ = res
        return f"{pt:+5.2f} [{lo:+5.2f},{hi:+5.2f}]".rjust(26)

    rows = []
    for stype, evs in deduped.items():
        direction = DIRECTION.get(stype, "?")
        if direction not in ("BEAR", "BULL"):
            continue
        res = {l: block_bootstrap(evs, l, direction, B) for l in RIGOR_LABELS}
        nxt = res["NXT"]
        nblk = nxt[3] if nxt else 0
        sig_nxt = nxt[4] if nxt else False
        rows.append((stype, direction, len(evs), nblk, res, sig_nxt))

    rows.sort(key=lambda x: (-int(x[5]), -x[2]))   # 显著优先,再按事件数
    for stype, direction, nevt, nblk, res, sig_nxt in rows:
        dtxt = {"BEAR": "空", "BULL": "多"}[direction]
        sigtxt = "✓显著" if sig_nxt else ("✗" )
        # 三段里有几段显著
        nsig = sum(1 for l in RIGOR_LABELS if res[l] and res[l][4])
        line = (f"{stype:<24}{dtxt:<4}{nevt:>5}{nblk:>5}  "
                + "  ".join(cell(res[l]) for l in RIGOR_LABELS)
                + f"  {sigtxt}({nsig}/3)")
        print(line)
    print("-" * len(hdr))
    print("解读: 去重叠后 evt(事件数)远小于原始触发数才正常;NXT CI 完全在 0 一侧=该信号")
    print("      在剔除趋势+尊重日内聚类后仍有显著边际。块数 blk 太小(<5)证据仍弱。\n")


def main():
    ap = argparse.ArgumentParser(description="信号回测脚手架(单/跨标的)")
    ap.add_argument("--db", default="short_data.db")
    ap.add_argument("--dbs", nargs="+", default=None, help="多个 DB 路径(跨标的)")
    ap.add_argument("--all", action="store_true", help="用 shared_config 全部标的")
    ap.add_argument("--type", default=None)
    ap.add_argument("--min-score", type=int, default=None)
    ap.add_argument("--since", default=None)
    ap.add_argument("--horizons", type=int, nargs="+", default=DEFAULT_HORIZONS_MIN)
    ap.add_argument("--rigor", action="store_true",
                    help="去重叠+块自助法显著性检验(剔除日内聚类红利)")
    ap.add_argument("--dedup", type=int, default=None, metavar="MIN",
                    help=f"去重叠冷却期(分钟),默认随 --rigor 为 {RIGOR_DEDUP_MIN}")
    ap.add_argument("--bootstrap", type=int, default=None, metavar="N",
                    help=f"自助重采样次数,默认随 --rigor 为 {RIGOR_BOOTSTRAP}")
    args = ap.parse_args()

    labels = [f"+{h}m" for h in args.horizons] + ["EOD", "NXT"]
    tol = FWD_TOLERANCE_MIN * 60

    # 决定 DB 列表
    if args.all:
        import shared_config as sc
        dbs = [cfg["db_path"] for cfg in sc.STOCKS.values()
               if os.path.exists(cfg["db_path"])]
    elif args.dbs:
        dbs = args.dbs
    else:
        dbs = [args.db]

    results = []
    for db in dbs:
        if not os.path.exists(db):
            print(f"[!] 跳过不存在的 DB: {db}", file=sys.stderr); continue
        r = analyze_db(db, args.horizons, labels, args.type, args.min_score, args.since, tol)
        if r:
            results.append(r)

    if not results:
        print("[!] 无可用数据", file=sys.stderr); sys.exit(1)

    if len(results) == 1:
        report_single(results[0], labels)
    else:
        report_pooled(results, labels)

    if args.rigor or args.dedup is not None or args.bootstrap is not None:
        cooldown = args.dedup if args.dedup is not None else RIGOR_DEDUP_MIN
        B = args.bootstrap if args.bootstrap is not None else RIGOR_BOOTSTRAP
        report_rigor(results, cooldown, B)


if __name__ == "__main__":
    main()
