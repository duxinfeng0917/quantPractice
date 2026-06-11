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
from shared_config import HKEX_COST_WINDOW, STOCKS, DEFAULT_STOCK

import math
import unicodedata

import requests
import pandas as pd
from futu import OpenQuoteContext, SubType, RET_OK, Market

# ═══════════════════════════════════════════════════════════
# 一、配置
# ═══════════════════════════════════════════════════════════
# 以下由 --stock 参数在启动时覆盖（见 shared_config.py STOCKS 字典）
SYMBOL        = STOCKS[DEFAULT_STOCK]["symbol"]
STOCK_CODE    = STOCKS[DEFAULT_STOCK]["stock_code"]
STOCK_NAME    = STOCKS[DEFAULT_STOCK]["name"]
OPEND_HOST    = "127.0.0.1"
OPEND_PORT    = 11111

REALTIME_INTERVAL = STOCKS[DEFAULT_STOCK]["poll_interval"]
HKEX_FETCH_HOUR   = 17            # 每日几点后拉取 HKEX 数据（港股 16:00 收盘，17:00 数据稳定）

DB_PATH = STOCKS[DEFAULT_STOCK]["db_path"]

# 港股连续交易时段（不含开盘/收盘集合竞价），仅在此区间打分
# 09:00-09:30 开盘集合竞价、12:00-13:00 午休、16:00-16:10 收盘集合竞价均跳过
HK_TRADING_SESSIONS = [
    (datetime.time(9, 30),  datetime.time(12, 0)),
    (datetime.time(13, 0),  datetime.time(16, 0)),
]
STALE_DATA_ROUNDS = 5             # 价格+大单累计连续 N 轮未变 → 视为数据停滞，跳过打分
BIG_NET_STALE_ROUNDS = 5          # 大单累计单独连续 N 轮未变 → "大单净流入"维度跳过计分
                                  # (Bug 14：实盘 2026-05-20 10:00-10:03 大单冻在 -766.4 万 5 分钟，
                                  #  价格 698→701 上行，旧 stale 守门是 AND（价格+大单都不变）
                                  #  导致价格一变就解除停滞，但大单仍陈旧，继续打 +15 分)
BIG_NET_STALE_BAND_HKD = 1_500_000  # 大单"冻结"容差带（Bug 25 + Bug 28）：Futu 常在 2~3 个缓存值
                                  # 间小幅抖动，精确相等检测每次被重置导致冻结凑不满轮数。改为锚点
                                  # ±此绝对额(HKD)内即视为未实质变化。
                                  # Bug 28（2026-06-08 实盘）：原用 ±6%×累计额，下午累计涨到 +8,000万
                                  #  时带宽达 ±480万，把真实流动(15:33 一笔 +183万 主动大单)也吞成"抖动"，
                                  #  维度①冻结 280+ 轮(≈1 小时)对下午派发资金流半瞎。改为绝对额：实测
                                  #  06-08 抖动多 ≤100万(中位89万)、真实流动 ≥200万，150万卡其间。
API_FAIL_TOLERANCE_ROUNDS = 2     # 资金流/摆盘 API 连续失败 N 轮 → 跳过打分
                                  # (Futu 失败时旧值会被保留，若不守门则评分基于陈旧快照。
                                  #  实盘 2026-05-18 09:36:56-09:37:45 案例：3 轮 API 失败，
                                  #  评分用 09:36:30 的冻结数据，错过 +3.5% 拉升预警)

# 逼空信号阈值
SHORT_RATIO_WINDOW   = 5          # 卖空占比趋势回看天数
SHORT_RATIO_RISE_MIN = 3          # 连续上升至少 N 天后才判断为"高位"
ASK_DEPTH_SHRINK_PCT = 30.0       # 卖盘深度较近期基准下降超过此值 → 触发信号（%）
ASK_DEPTH_WINDOW     = 60         # 卖盘深度滚动基准窗口（轮次，15s × 60 ≈ 15 分钟）
ASK_DEPTH_SMOOTH_K   = 3          # 卖盘深度信号触发用近 K 轮中位数（过滤单次挂撤单噪音）
# ── 回测调权（迭代四十五，2026-06-09）────────────────────────
# 跨 5 标的(含 +21.5% 上涨样本) + 去重叠(30min) + 块自助法(块=标的×交易日)回测：
#   ASK_DEPTH_SHRINK 去重叠后 471 事件/59 块（全样本最大独立样本），
#   +60m/EOD/NXT 点估计≈0、95%CI 全跨 0 → 统计上是噪声。
# 故从 25/12 降权至 12/6（保留为弱上下文,不归零,便于随数据复核回滚）。
ASK_DEPTH_SHRINK_PTS      = 12    # 原 25：卖盘深度骤减(逼空维度)，回测证 null 降权
ASK_DEPTH_SHRINK_PTS_WEAK = 6     # 原 12：卖盘深度明显下降
ASK_DEPTH_LOG_COOLDOWN_SECS = 60  # 同一深度骤减事件的 WARNING 日志/dashboard signal 冷却（秒）
BIGFLOW_REVERSAL_MIN = 2          # 大单净流入连续正值 N 轮 → 触发反转信号
BIGFLOW_WINDOW       = 10         # 大单净流入反转/加速判断窗口（轮次）
BIGFLOW_STREAK_WINDOW = 120       # streak 计数窗口；与 BIGFLOW_WINDOW 解耦，避免 streak 被截断为 10
# 大单累计虽负但近期 Δ 转买入（Bug 16）：单一累计静态值掩盖近期资金转向
# capital_flow 表已按 update_time 去重，相邻两行约 1 分钟跨度，故 recent[0]-recent[1] 就是近 1 分钟 Δ
BIG_NET_DELTA_THRESHOLD = 500_000  # 单轮 Δ ≥ 50 万港元才算显著买入（小于此值视为噪音）
BIG_NET_REBUY_PRICE_PCT = 0.3      # 同期价格涨幅 ≥ 此 % 才视为方向咬合（防纯撤单噪音）
# Bug 20：出货式拉升检测的前置守门——已涨完才停滞 ≠ 出货
BIGFLOW_PUMP_STAGNANT_PCT = 0.3    # 近期窗口涨幅 < 此 % 视为停滞
BIGFLOW_PUMP_INTRADAY_MIN = 2.0    # 但日内累计涨幅 ≥ 此 % 时，停滞解释为高位整理而非出货
# Bug 21：日内已涨但价格已从近期高点回落 → 真出货（Bug 20 的反例边界）
BIGFLOW_PUMP_PEAK_PULLBACK_PCT = 1.5  # 从近 N 轮峰值回落 ≥ 此 % 视为顶部已现，覆盖 Bug 20 路径
BIGFLOW_PUMP_PEAK_WINDOW = 30        # 近期峰值参考窗口（轮次，约 7.5 分钟）

# 做空信号阈值
SHORT_SAFE_SQUEEZE   = 25         # 逼空评分超过此值时禁止新开空单
SHORT_EXIT_SQUEEZE   = 40         # 逼空评分超过此值时触发离场警报
SHORT_ASK_SURGE_PCT  = 80.0       # 卖盘深度较均值上升超过此值 → 大卖单出现（%）
SHORT_IMB_THRESHOLD  = -0.30      # 失衡度低于此值视为持续卖压
SHORT_IMB_ROUNDS     = 2          # 连续 N 轮失衡度 < 阈值方触发
SHORT_ENTRY_MIN      = 55         # 做空入场评分门槛（满分 100）
SHORT_MAIN_FRESH_MIN = 40         # 背景注水门（Bug 24）：ENTRY 要求主信号分 ≥ 此值。
                                  # support_score 全是日级静态量（成本线/动能比/爆量），整日恒定
                                  # 与盘中此刻无关。低于此值的 ENTRY = 背景独自堆出的"已跌透"滞后
                                  # 画像而非新鲜入场触发，降级 CAUTION。(40 需至少一个强盘中维度：
                                  #  卖深骤增25 / 大单转负30，纯靠 持续为负15+价格低于日高15 凑不到)
SHORT_PRICE_WINDOW   = 10         # 价格历史窗口（轮次）
SHORT_RATIO_PRICE_CONFIRM_WIN = 30 # 高位拐头价格确认窗口（轮次，约 7.5 分钟）
SHORT_BLOCK_OVERRIDE_WIN      = 30 # BLOCKED 放行判断的价格/资金流窗口（轮次）
SHORT_BLOCK_PRICE_DROP_PCT    = 0.3       # 窗口内价格下跌 ≥ 此 % 视为确认下行
SHORT_BLOCK_BIGFLOW_THRESHOLD = -30_000_000  # 大单累计流出 < 此值（HKD）视为持续净流出
# 通道 C：从日内高点回落（迭代二十六，2026-05-29 实盘）
# 实盘 15:22:45 ENTRY 90 后 30 秒被误杀：通道 A 要求"价格下行 AND 大单流出"，
# 但价格虽然 -4% 但大单累计仍 +533 万（午盘逼空进场尚未完全反转），AND 失败。
# 通道 C 仅看"从日内峰值急速回落"——一旦回落超过阈值即视为 capitulation，
# 逼空假设已被价格行为否定，与资金流方向无关。
SHORT_BLOCK_SESSION_PULLBACK_PCT = 3.0   # 从日内最高点回落 ≥ 此 % 即放行 BLOCKED
# 通道 C 的"仍在低位"与条件（A2，2026-06-08）：纯静态"距日高回落"在价格跌下来后卡在
# 区间时会每轮常开放行——日高是几小时前的旧 anchor，回落%恒超阈值（实盘 10:07-10:13
# 价格 490-498 横盘、距日高 522 恒 -4.6%，"安全门放行"逐轮刷屏）。capitulation 只在价格
# 压着日内低点时成立；自日内低点反弹超过此 % 视为已企稳/反弹、capitulation 结束，收回放行。
SHORT_BLOCK_CAPITULATION_REBOUND_PCT = 1.0  # 自日内最低点反弹 ≤ 此 % 才算仍在 capitulation
SHORT_SQUEEZE_LOOKBACK = 4        # 逼空安全门回看 N 轮（取峰值，避免均值稀释）
SHORT_TRAP_IMB_BLOCK = 0.30       # 摆盘失衡度 > 此值 → 疑似诱空，强制降级
SHORT_TRAP_IMB_SUPPRESS = 0.10    # 卖盘骤增时若摆盘失衡度 > 此值 → 不计 +25 分
SHORT_MICRO_REVERSAL_RATIO = 0.10 # 维度1: |latest_net| < earlier 正值峰值 × 此比 → 微小反转，降权
SHORT_IMB_FLIP_WINDOW = 6         # 失衡度极性翻转检测回看轮数
SHORT_IMB_FLIP_MIN    = 2         # 近 N 轮翻转 ≥ 此值 → 视为挂单博弈，禁止 ENTRY
SHORT_IMB_FLIP_BAND   = 0.10      # |imb| ≤ 此值视为中性轮，不参与翻转计数
SHORT_IMB_SMOOTH_K    = 3         # trap_suspect 判定用近 K 轮失衡度中位数
# 追空守门（Failsafe 3，2026-06-01 实盘 00100 派发尾声案例）
# 实盘价格 710（自日高 906 回落 21.6%、已破成本线），做空分却因滞后/背景分虚高至 98。
# 做空分高 ≠ 该做空——这种分是"已经跌透"的回声而非"还会跌"的预测。
# 仅以"自日内高深度回落"为闸：到此深度时下跌空间已耗尽，新开空属追在尾部。
# v1 曾叠加 big_net_stale 作为 AND 条件 → 13:47-13:48 实盘 ENTRY 漏网：大单冻结
# 但尚在 5 轮 stale 检测窗口内（维度1 仍计 +15"持续为负"），big_net_stale=False
# 致 AND 断裂，同时 imb 短暂转中性/负 Failsafe1 也未拦。故 v2 改为纯回落闸。
# 阈值 15% 卡在当日"仍有效续跌空单"(-8%~-12%) 与"枯竭底部"(-21%) 之间的空档：
# 824 破位(-8%)、798 二段(-12%) 均 < 15% 保留；710 底部(-21%) ≥ 15% 降级。
SHORT_CHASE_PULLBACK_PCT = 15.0   # 自日内高点回落 ≥ 此 % → 追空降级（深度回落即枯竭，不再叠加其他条件）

# 价格反弹逼空维度（捕捉"被踏空"场景）
# Why: 原逼空评分仅看卖盘骤减/摆盘偏多/大单加速，对"价格已实际反转向上"完全无感。
# 实盘 2026-05-18 09:32:58 ENTRY 触发价 765.5 → 09:38:30 反弹至 792.5 (+3.5%)，
# 期间逼空评分始终 ≤ 20，毫无离场预警。本维度专门捕捉这种逆向走势。
PRICE_REVERSAL_WINDOW       = 30      # 反弹基准窗口（轮次，15s × 30 ≈ 7.5 分钟）
PRICE_REVERSAL_PCT_LIGHT    = 0.8     # 反弹 ≥ 此 % → 计 LIGHT 分
PRICE_REVERSAL_PCT_MED      = 1.5     # 反弹 ≥ 此 % → 计 MED 分
PRICE_REVERSAL_PCT_HEAVY    = 2.5     # 反弹 ≥ 此 % → 计 HEAVY 分
PRICE_REVERSAL_SCORE_LIGHT  = 8
PRICE_REVERSAL_SCORE_MED    = 15
PRICE_REVERSAL_SCORE_HEAVY  = 25

# 资金结构背离检测（中小单 vs 大单）
CAPITAL_STRUCT_WINDOW = 5              # 回看最近 N 条 capital_flow 记录
CAPITAL_STRUCT_DIVERGE_ROUNDS = 3      # 中小单连续反向 ≥ N 轮触发背离信号
CAPITAL_STRUCT_SMALL_THRESHOLD = -200_000  # 中小单合计净额 < 此值视为散户净流出（HKD）

# 散户撤退预警（small_net 从日内峰值回落）
RETAIL_RETREAT_MIN_PEAK     = 5_000_000     # 日内 small_net 峰值 ≥ 此值才检测（HKD，过滤无意义小峰）
RETAIL_RETREAT_PCT          = 0.10          # 从峰值回落 ≥ 此比例触发
RETAIL_RETREAT_PCT_HEAVY    = 0.20          # 重度回落阈值
# 资金效率检测（大单流入但价格无响应）
CAPITAL_EFFICIENCY_WINDOW   = 8             # 回看窗口（capital_flow 行数，约 8 分钟）
CAPITAL_EFFICIENCY_MIN_INFLOW = 3_000_000   # 窗口内大单累计 Δ ≥ 此值才评估（HKD）
CAPITAL_EFFICIENCY_PRICE_THRESHOLD = 0.3    # 同期价格涨幅 < 此 % 视为低效（拆单/吸筹嫌疑）
# 卖而不跌裁决（净流出 + 价格守位/反弹 → 被动吸筹/诱空，对称于派发模式的"卖而跌"）
# 实盘 2026-06-04 00981：大单+中单累计 -9,188 万（全档主动卖），但价格 80.35→81.5
# V 形反弹未跟跌。微观成因：主力挂被动买单接货时，对手主动卖单按 aggressor 计入"流出"，
# 故被动吸筹在资金流里显示为净流出——「负的中单 ≠ 主力在卖」。既有维度全部漏过此象限：
# 派发模式要价格跌、Failsafe4 要 mid_net 为正、资金效率要大单流入。本裁决抬升逼空风险
# 喂安全门、抑制顺势追空；价格破窗口低点则二次否决（卖压已兑现=真派发，放行做空）。
# HKD 阈值与本档量级相关，换标的（尤其低价/小成交股）需重标定。
SELL_NO_DROP_WINDOW       = 8            # 回看窗口（capital_flow 行数，约 8 分钟）
SELL_NO_DROP_MIN_OUTFLOW  = -3_000_000  # 窗口内 (大单+中单) Δ ≤ 此值视为持续净流出（HKD）
SELL_NO_DROP_PRICE_FLOOR  = -0.10       # 同期价格涨幅 ≥ 此 % 视为"未跟跌"（守位/反弹）
SELL_NO_DROP_SCORE        = 12          # 抬升逼空风险分（喂做空安全门，BLOCK 顺势追空）
# ─────────────────────────────────────────────────────────
# L2 逐笔冰山检测（Tier 1，2026-06-04）——用成交(execution)而非报价(quote)分辨被动吸筹/派发
# 报价层（挂单深度/集中度）是 spoofing 重灾区（00981 06-04 卖深 500↔135,500 来回甩），
# 故只信"真金白银吃出来的成交"：主动卖量大但价不跌 + 买一被吃量远超显示量 = 买侧冰山吸筹；
# 主动买量大但价滞涨 + 卖一被吃量远超显示量 = 卖侧冰山派发。需订阅 SubType.TICKER。
# 门槛用成交额（HKD notional）而非绝对股数：股数阈值无法跨价位 scale——50,000 股对 10 HKD
# 票=50万 HKD（合理），对 500 HKD 的 00100=2,500万 HKD/分钟（永不触发，整个执行层失明）。
# 改用 notional 后对所有票按金钱口径统一，与本文件其余资金流门槛（CAPITAL_EFFICIENCY_MIN_INFLOW
# =3M/8轮）一致：4 轮窗口取其半 1.5M HKD。后续若各标的活跃度差异大，可提为 STOCKS per-stock 配置。
ICEBERG_WINDOW          = 4          # 回看 tick_flow 行数（每行≈一个轮询窗口的聚合）
ICEBERG_MIN_NOTIONAL    = 1_500_000  # 窗口内主导方主动成交额 ≥ 此值（HKD）才评估
ICEBERG_DOMINANCE       = 1.5        # 一方主动量 ≥ 另一方 × 此倍数 视为单边主导
ICEBERG_PRICE_FLOOR     = -0.10      # 吸筹：同期价格涨幅 ≥ 此 %（未跟跌）
ICEBERG_PRICE_CAP       = 0.10       # 派发：同期价格涨幅 ≤ 此 %（滞涨）
ICEBERG_REFILL_MULT     = 2.0        # 被动方成交量 ≥ 最优档显示量 × 此倍数 → 冰山补单（执行级铁证）
ICEBERG_STRONG_SCORE    = 15         # 卖侧冰山【派发】(看空)：强补单确认。回测方向对但不显著→保持
ICEBERG_WEAK_SCORE      = 8          # 卖侧冰山【派发】(看空)：弱(无补单铁证)→保持
# 买侧冰山【吸筹】(看多)单列：迭代四十五回测——跨标的一致性 0/3、点估计反向，
# 即"主动卖被吃价不跌"在本数据里没有看多预测力(多为下跌中接飞刀)。
# 故吸筹分从沿用派发的 15/8 降权至 8/4(不归零,留作弱抑制,随数据复核)。
ICEBERG_ACCUM_STRONG_SCORE = 8       # 原沿用 15
ICEBERG_ACCUM_WEAK_SCORE   = 4       # 原沿用 8
BIG_FLOW_REBUY_PTS         = 5       # 原 10：大单回补(看多)，迭代四十五回测跨标的 0/3 反向→降权
# 迭代四十六(2026-06-10)：DISTRIBUTION_PUMP_SUSPECT(出货式拉升,原做空 +20)归零
#   ① 三重计数：其门槛(HKEX占比≥8 或 动能≥1.5)与 卖空动能比[+20]/爆量[+15] 同源——
#      一个卖空数据经三处入账,做空背景虚高(06-10 做空分恒钉 85~100,仅靠背景注水降级挡住)。
#   ② 回测反向：DISTRIBUTION_PUMP_SUSPECT n=217,+60m/EOD/NXT 前向收益全为正(NXT+0.23)，
#      触发后价格反而偏涨(更像轧空前蓄势而非派发)→ 加"支撑做空"分方向是反的。
#   故归零中性化(仍 db_save_signal 供回测追踪;不再进做空理由列表)。弱支链(+10)非同源,保留。
DISTRIBUTION_PUMP_PTS      = 0       # 原 20
ICEBERG_TICK_FETCH_NUM  = 1000       # get_rt_ticker 单次拉取上限（Futu 上限，去重靠 sequence）
# L2 十档深度形态（迭代三十八 #1）：get_order_book(num=10) 抓十档却只用聚合总深度，丢了
# "挂单分布形态"——卖墙集中在卖一(压价/诱空)还是均匀分布(真实供给)。下列阈值判定形态，
# 当前仅作展示 + 信息性提示，不改数值评分（待实盘标定后再考虑计分）。
BOOK_WALL_CONC_HIGH     = 0.55       # 卖一/买一显示量 ÷ 本侧总深度 ≥ 此值 → 单档墙（集中）
BOOK_TOP3_CONC_HIGH     = 0.80       # 前3档量 ÷ 本侧总深度 ≥ 此值 → 浅档堆叠（深档空虚）
# L2 逐笔大单识别（迭代三十八 #2）：fetch_ticks 把窗口内所有成交按方向求和，单笔大单扫盘
# 与一堆散单求和无法区分。下列阈值把"主动大单"单独拎出来计数/累量（执行级主力足迹）。
# 门槛用单笔成交额（HKD notional）而非股数：股数无法跨价位 scale——2000 股对 10 HKD 票=2万
# HKD（散单也算"大单"），对 00100(~500 HKD)=100万 HKD（合理）。改 notional 后对所有票按金钱
# 口径统一。注：此维度当前仅展示（仪表盘「主动大单」/「↳累计净」），不进评分。
LARGE_TICK_NOTIONAL     = 1_000_000  # 单笔成交额 ≥ 此值（HKD）视为主动大单（=00100 旧 2000 股等效）
LARGE_TICK_CUM_WINDOW   = 40         # 主动大单累计净视图回看轮数（≈10 分钟）：薄盘里单笔
                                     # 大单主宰单轮窗口、主买/主卖比值剧烈翻面，累计净额
                                     # （主买大单股数 − 主卖大单股数）才看得清真实净方向
# L2 经纪队列足迹（Tier 2，2026-06-04；迭代三十二改判断依据）——同一机构持续单边压最优档
# = 机构被动挂单足迹。报价层可幌单，故经纪足迹**绝不单独加分**：仅当 Tier-1 冰山（执行级
# 成交证据）同向已触发时作交叉验证加成。
#
# 判断依据 v2（v1 对大票失效，实盘 00981 06-04 验证后重写）：
#   ① 按 broker_name 聚合，非 broker_id——同一机构常占多个 ID（荷银占 17 买档/22 卖档，
#      按 ID 会被拆散看不出集中度）。
#   ② 只看最优档（broker_pos==1）的席位数。
#   ③ 用买卖两侧净不对称过滤做市商——荷银/巴克莱两侧都重仓（做市报价，非方向性）；
#      净不对称 = 该机构本侧席位数 − 其对侧席位数，>0 才是单边方向性足迹。
BROKER_FOOTPRINT_WINDOW     = 6   # 回看 broker_queue 行数（每行≈一个轮询）
BROKER_FOOTPRINT_MIN_ROUNDS = 3   # 同一机构单边领先最优档 ≥ 此轮数（窗口内）视为持续足迹（放宽：4→3 观察）
BROKER_FOOTPRINT_MIN_NET    = 2   # 净不对称（本侧 − 对侧席位数）均值 ≥ 此值才算单边压盘（滤做市商/噪音）
BROKER_FOOTPRINT_BONUS      = 6   # 交叉验证加成（仅在 Tier-1 同向冰山已触发时计入）

# ─────────────────────────────────────────────────────────
# 主力嫌疑分（控盘特征筛查）—— 纯背景画像，不参与做空/逼空打分
# 用途：挑出像 00100 MINIMAX（价格钉扎 601.00 + 摩根士丹利长期独占卖一 + 薄盘）这类
# 单一席位控盘的标的，避开像 00981 中芯（盘深、席位轮换、价格连续）的纯市场化大盘。
# 三个子维度加权（满分 100，仅展示）：① 价格钉扎 50 ② 席位集中 35 ③ 盘薄 15。
# 注意：盘薄阈值按"股数"判定，与股价量级相关，换低价/高价股需重标定 MF_THIN_*。
MF_WINDOW              = 12      # 三维统一回看窗口（轮）
MF_MIN_ROUNDS          = 6       # 样本不足此数则不评估（返回 0）
MF_PIN_BAND_PCT        = 0.6     # 窗口内 (max-min)/median ≤ 此 % → 视为价格被夹在窄带（满分线 0%）
MF_PIN_MODE_RATIO      = 0.5     # 单一价格出现轮数占比 ≥ 此值 → 钉扎（精确同价，主力按价铁证）
MF_SEAT_MIN_NET        = 3       # 席位净不对称 ≥ 此值才计入"该轮有主导席位"（滤 1 席噪音）
MF_SEAT_DOM_RATIO      = 0.5     # 单一【机构】席位独占某侧轮数占比 ≥ 此值 → 集中（满分线 0.8）
MF_INST_SIDE_RATIO     = 0.6     # 某侧被【外资/机构大行】主导轮数占比 ≥ 此值 → 机构压盘足迹（抓轮换大行）
MF_THIN_DEPTH_SHARES   = 30_000  # 卖盘深度中位 ≤ 此股数 → 薄盘易控；≤ 半值给满分
# 外资/机构大行席位关键词（用于"机构连续压一侧"判定，及压卖=派发/压买=吸筹的方向读）。
# 命名取 Futu 经纪席位常见简称的稳定子串；漏判（少一家）比误判安全，宁缺毋滥。
INSTITUTIONAL_BROKER_KEYWORDS = (
    "摩根", "高盛", "巴克莱", "瑞银", "汇丰", "美林", "花旗", "德意志", "富瑞",
    "中国国际金融", "中金", "中银国际", "招银国际", "法国巴黎", "野村", "大和",
    "麦格理", "瑞信", "中信证券国际", "建银国际", "海通国际",
)
# 零售聚合商（散户大本营）：占某档最优只是散单汇集，非控盘。用于派发/吸筹对峙检测里
# 判定"散户在接盘/出货"那一侧（迭代三十九）。
RETAIL_BROKER_KEYWORDS = (
    "富途", "老虎", "华盛", "uSmart", "盈立", "微牛", "雪盈", "长桥",
)
# 派发对峙检测（迭代三十九）：报价层(盘口偏多·散户挂买接盘) vs 成交层(机构派发·大单净
# 流出+压卖一)打架的显式识别。补"卖盘骤增因失衡偏多被判诱空不计分"的盲区——那种场景常
# 常不是诱空，而是真派发被散户厚买盘暂时托住。纯信息性提示，不改数值评分（避免与 主力
# 嫌疑「压卖一」、sell_no_drop 双计）。
STANDOFF_IMB_MIN = 0.15   # 近3轮失衡度中位 ≥ 此值视为盘口偏多（散户挂买接盘）
# 主力嫌疑分联动做空安全门：强控盘 + 价格钉扎 → 做空 ENTRY 降级 CAUTION（防控盘股诱空逼空）
MF_CONTROL_GATE        = 60      # 主力嫌疑分 ≥ 此值视为强控盘
MF_PIN_GATE            = 30      # 且价格钉扎子分 ≥ 此值（确为"钉价"而非仅薄盘）才触发降级
# ─────────────────────────────────────────────────────────
# 散户 FOMO 警报（价格窄幅震荡 + 散户加速流入）
RETAIL_FOMO_WINDOW          = 8             # 回看窗口
RETAIL_FOMO_PRICE_RANGE_PCT = 0.5           # 价格区间 < 此 % 视为窄幅震荡
RETAIL_FOMO_MIN_INFLOW      = 500_000       # 窗口内 small_net 净流入 ≥ 此值触发
# 中单拆单方差检测（大单冻结期间 mid_net 稳定单向）
MID_SPLIT_WINDOW            = 5             # 回看窗口
MID_SPLIT_MIN_FLOW          = 300_000       # 单轮 |mid_net Δ| 中位 ≥ 此值才评估
MID_SPLIT_CV_THRESHOLD      = 0.6           # 变异系数 < 此值视为节奏稳定（拆单痕迹）

# 隐藏主力吸筹否决（Failsafe 4）— 大单冻结时，中单/拆单加速买入 + 价格上行 → 否决做空 ENTRY
# 实盘 2026-06-03 10:29-10:31 MINIMAX-W(00100)：大单累计冻结 19+ 轮被跳过，失衡度
# reverse-诱空（盘口偏空但价格涨）把做空主信号顶到 47，叠加日级背景 53 → 假 ENTRY=76/100。
# 同期中单(拆单)累计 +194→+397 万持续加速买入、中小单转正、价格 683.5→690 反弹创新高
# ——隐藏主力在吸筹、价格印证上行，本该看多/至少不做空。既有 failsafe（诱空/翻转/追空）
# 均按失衡度或距日内高跌幅判定，全部漏过。本 failsafe 直接以"中单加速买入 + 价格不跌反涨"
# 的方向矛盾否决 ENTRY。仅在 big_net_stale（大单看不清、中单成为隐藏主力探针）时生效，
# 避免误杀大单真实净流出支撑的合法做空。
# 触发为"加速买入(Δ) 或 已累积高位(level)"二选一 —— mid_net 常先回落再拉升（实盘
# 10:25:45 +136→10:31 +397 万），短窗 Δ 会被中途回落稀释，故并入绝对累计水位兜底。
# 两个 HKD 阈值与本档量级相关，换标的（尤其低价/小成交股）需重标定。
MID_ACCUM_WINDOW           = 10            # 中单累计回看窗口（capital_flow 行数，约 10 分钟）
MID_ACCUM_MIN_DELTA        = 1_000_000     # 窗口内 mid_net 净买入增量 ≥ 此值视为"加速买入"（HKD）
MID_ACCUM_MIN_LEVEL        = 2_000_000     # 或：mid_net 累计 ≥ 此值视为"已累积显著多头"（HKD）
MID_ACCUM_PRICE_WINDOW     = 12            # 价格回看窗口（price_history 行数，约 3 分钟）
MID_ACCUM_PRICE_RISE_PCT   = 0.2           # 同期价格涨幅 ≥ 此 % 视为"价格印证上行"（弱档：降级 CAUTION）
# 强档双确认 → 升级 BLOCKED 禁止开空（迭代：2026-06-10 00100 全天误报教训）。
# 实盘 00100 价格 418→447 单边新高，中单累计 +1,863→+3,032 万持续吸筹、价格印证上行，
# 但旧逻辑仅降级 CAUTION，做空分仍被日级背景（空头深套+卖空爆量）注水维持 80-100，
# 持续诱导追空。结论：吸筹水位大且价格印证强时，CAUTION 不足以遏制背景注水高分，需直接禁空。
# 仅当"大幅吸筹水位 + 强价格印证"同时成立才升级，避免误杀价格下行中的合法做空
# （做空发生在价格下跌时，price_rise_pct 为负，本档天然不触发，故不伤害晨盘 430→418 类做空）。
MID_ACCUM_STRONG_LEVEL_MULT  = 2.0         # 中单累计 ≥ MID_ACCUM_MIN_LEVEL × 此倍数 视为"大幅吸筹水位"
MID_ACCUM_STRONG_PRICE_RISE_PCT = 0.5      # 同期价格涨幅 ≥ 此 % 视为"强价格印证"

# ─────────────────────────────────────────────────────────
# 派发模式三条件共振（评分盲区补丁）
# 实盘 2026-05-29 10:04-10:06：逼空评分 26（正常区间），但同期：
#   ① 大单累计 -1,754 万创日内新低（破开盘第一波 -1,425）
#   ② 散户从日内峰值 +2,114 万撤 859 万 (40.7%)
#   ③ 中小单累计从 +1,277 万跌至 +162 万（已消化 87%）
#   ④ 价格从 873 高点跌至 846（-3.1%）
# 评分系统全程未识别——逼空维度都在"反弹/卖盘骤减"等噪音上，
# 无任何专门捕捉"派发末期"的维度。本组三条件 AND 触发：
# - 不计入 squeeze 分（与逼空逻辑无关）
# - 进入做空入场主信号（+25 分），表明"利空已确认"
# - 同时作为 BLOCKED 安全门的第二条放行路径
# ─────────────────────────────────────────────────────────
DISTRIBUTION_BIGNET_LOW_RATIO   = 0.92      # latest big_net 距日内最低值 ≤ 8% → 视作"接近新低"
DISTRIBUTION_BIGNET_MIN_DEPTH   = -5_000_000  # 日内最低值需 ≤ 此值才有派发意义（HKD，过滤无意义微负）
DISTRIBUTION_SMALL_RETREAT_PCT  = 0.30      # 散户峰值回落 ≥ 30%
DISTRIBUTION_MIDSMALL_DECAY_PCT = 0.60      # 中小单累计从峰值回落 ≥ 60%（85% 是 10:04 实际值，60% 早期触发）
DISTRIBUTION_MIDSMALL_MIN_PEAK  = 5_000_000   # 中小单峰值 ≥ 此值才检测（HKD）
DISTRIBUTION_SCORE              = 25        # 触发后给做空主信号加分

# 派发模式粘滞机制（迭代二十五，2026-05-29 实盘补丁）
# 实盘 10:51:26/41 派发模式触发后，10:51:56 大单累计从 -6,718.9 回补到 -6,151.8
# （仅 +253 万），即脱离 0.92 阈值失效；但同期散户撤退 120%、中小单 -4,917 万、
# 卖盘骤增 1400%——派发实际更猛烈。粘滞机制：一旦三条件命中过，未来 N 轮内即使
# 大单暂时回补脱离阈值仍保持 confirmed，但分数减半，避免过度依赖陈旧触发。
DISTRIBUTION_STICKY_ROUNDS      = 5         # 触发后保持 N 轮（约 75 秒，跨过单一脉冲回补窗口）
DISTRIBUTION_STICKY_SCORE       = 12        # 粘滞期间的派发分（原 25 的一半）

# 极性翻转 failsafe 豁免阈值（迭代二十五）
# 实盘 10:53:13 派发末期 BLOCKED → 0 分：失衡度 6 轮翻转 2 次触发 Failsafe 2，
# 但同期价格 819（日内底），大单 -6,152 万仍流出，中小单 -5,159 万创新低。
# 翻转更可能是薄盘流动性稀薄抖动而非主力博弈——派发模式生效或薄盘期豁免。
THIN_ASK_DEPTH_FOR_FLIP_SKIP    = 300       # 卖盘深度 < N 股时跳过翻转 failsafe


# ═══════════════════════════════════════════════════════════
# 二、日志
# ═══════════════════════════════════════════════════════════
import os as _os
_LOG_DIR  = "logs"
_LOG_DATE = datetime.date.today().strftime("%Y%m%d")
_LOG_FILE = _os.path.join(_LOG_DIR, f"short_monitor_{_LOG_DATE}.log")
_os.makedirs(_LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),                          # 与 print() 同流，nohup 重定向后顺序一致
        logging.FileHandler(_LOG_FILE, encoding="utf-8"),           # 按日期独立日志文件
    ],
)
log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# 三、数据库
# ═══════════════════════════════════════════════════════════
def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    # broker_queue v1→v2 迁移（迭代三十二）：v1 存 bid1_id/ask1_id（按 ID、仅队首席位），
    # 实盘证明对大票失效（同名机构多 ID 被拆散、做市商两侧挂单被误判）。v2 改存按 name
    # 聚合的最优档净不对称。旧表仅当日观测、从未触发，无历史价值，直接 DROP 重建。
    try:
        _bq_cols = [r[1] for r in conn.execute("PRAGMA table_info(broker_queue)")]
        if _bq_cols and "bid1_id" in _bq_cols:
            conn.execute("DROP TABLE broker_queue")
    except sqlite3.OperationalError:
        pass
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
            imbalance     REAL,   -- (bid-ask)/(bid+ask)，正值偏多
            best_bid_vol  REAL,   -- 买一显示量（股，迭代三十八 #1）
            best_ask_vol  REAL,   -- 卖一显示量（股）
            bid_top3      REAL,   -- 前3档买量合计（股）
            ask_top3      REAL    -- 前3档卖量合计（股）
        );

        CREATE TABLE IF NOT EXISTS tick_flow (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT,
            sell_vol      REAL,   -- 窗口内主动卖出量（打在买盘，股）
            buy_vol       REAL,   -- 窗口内主动买入量（打在卖盘，股）
            price_first   REAL,   -- 窗口首笔成交价
            price_last    REAL,   -- 窗口末笔成交价
            best_bid_vol  REAL,   -- 抓取时买一显示量（股）
            best_ask_vol  REAL,   -- 抓取时卖一显示量（股）
            large_buy_vol REAL,   -- 窗口内主动大单买量（单笔成交额≥LARGE_TICK_NOTIONAL，股，迭代三十八 #2）
            large_sell_vol REAL,  -- 窗口内主动大单卖量（股）
            large_buy_cnt INTEGER,-- 窗口内主动大单买笔数
            large_sell_cnt INTEGER-- 窗口内主动大单卖笔数
        );

        CREATE TABLE IF NOT EXISTS broker_queue (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT,
            bid_top_name  TEXT,   -- 买一档净不对称最强的机构名（吸筹足迹候选）
            bid_top_net   INTEGER,-- 该机构 买一档席位数 − 其卖一档席位数（>0=单边压买盘）
            ask_top_name  TEXT,   -- 卖一档净不对称最强的机构名（派发足迹候选）
            ask_top_net   INTEGER,-- 该机构 卖一档席位数 − 其买一档席位数（>0=单边压卖盘）
            bid_inst      INTEGER,-- 买一档机构大行席位数（主力方向，迭代四十）
            bid_retail    INTEGER,-- 买一档零售聚合商席位数（散户方向）
            ask_inst      INTEGER,-- 卖一档机构大行席位数
            ask_retail    INTEGER -- 卖一档零售聚合商席位数
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

        CREATE TABLE IF NOT EXISTS monitor_state (
            id            INTEGER PRIMARY KEY CHECK (id = 1),
            ts            TEXT,
            squeeze_score INTEGER,
            short_score   INTEGER,
            short_signal  TEXT,
            price         REAL,
            ask_depth     REAL,
            imbalance     REAL,
            big_net       REAL
        );
    """)
    # 既有 DB 增量迁移（迭代三十八）：CREATE IF NOT EXISTS 不会给已存在的表补列，
    # 故对老库逐列 ALTER ADD（幂等：先查 PRAGMA，缺则加）。新列默认 NULL，旧读取按列名
    # 取值不受影响；所有 INSERT 已改列名显式，老库补 NULL 不报错。
    def _add_cols(table: str, cols: dict[str, str]):
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    _add_cols("orderbook_snapshots", {
        "best_bid_vol": "REAL", "best_ask_vol": "REAL",
        "bid_top3": "REAL", "ask_top3": "REAL",
    })
    _add_cols("tick_flow", {
        "large_buy_vol": "REAL", "large_sell_vol": "REAL",
        "large_buy_cnt": "INTEGER", "large_sell_cnt": "INTEGER",
    })
    _add_cols("broker_queue", {
        "bid_inst": "INTEGER", "bid_retail": "INTEGER",
        "ask_inst": "INTEGER", "ask_retail": "INTEGER",
    })
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
                      bid: float, ask: float, imb: float,
                      best_bid_vol: float = 0.0, best_ask_vol: float = 0.0,
                      bid_top3: float = 0.0, ask_top3: float = 0.0):
    # 列名显式（迭代三十八）：表已扩列，positional VALUES 会列数不符；显式列名让本函数
    # 与 paper_trader 的精简写入（只填基础 4 列）都能在扩列后的表上正常工作。
    conn.execute(
        "INSERT INTO orderbook_snapshots "
        "(ts, bid_depth, ask_depth, imbalance, best_bid_vol, best_ask_vol, bid_top3, ask_top3) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (ts, bid, ask, imb, best_bid_vol, best_ask_vol, bid_top3, ask_top3),
    )
    conn.commit()


def db_save_tick(conn: sqlite3.Connection, ts: str,
                 sell_vol: float, buy_vol: float,
                 price_first: float, price_last: float,
                 best_bid_vol: float, best_ask_vol: float,
                 large_buy_vol: float = 0.0, large_sell_vol: float = 0.0,
                 large_buy_cnt: int = 0, large_sell_cnt: int = 0):
    conn.execute(
        "INSERT INTO tick_flow "
        "(ts, sell_vol, buy_vol, price_first, price_last, best_bid_vol, best_ask_vol, "
        "large_buy_vol, large_sell_vol, large_buy_cnt, large_sell_cnt) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ts, sell_vol, buy_vol, price_first, price_last, best_bid_vol, best_ask_vol,
         large_buy_vol, large_sell_vol, large_buy_cnt, large_sell_cnt),
    )
    conn.commit()


def db_get_recent_ticks(
    conn: sqlite3.Connection, n: int, since_ts: Optional[str] = None,
) -> list[tuple]:
    """返回最近 n 条 tick_flow 聚合行（最新在前），默认仅限当日交易时段。

    每行为 (sell_vol, buy_vol, price_first, price_last, best_bid_vol, best_ask_vol)。
    与其它日内窗口同理：跨日窗口会把昨日尾盘成交污染冰山判定，故默认 today() 过滤。
    """
    if since_ts is None:
        since_ts = datetime.date.today().isoformat()
    rows = conn.execute(
        "SELECT sell_vol, buy_vol, price_first, price_last, best_bid_vol, best_ask_vol "
        "FROM tick_flow WHERE ts >= ? ORDER BY id DESC LIMIT ?",
        (since_ts, n),
    ).fetchall()
    return rows


def db_get_large_tick_cum(
    conn: sqlite3.Connection, n: int, since_ts: Optional[str] = None,
) -> tuple[float, float, int, int]:
    """近 n 轮主动大单累计（迭代四十一）：返回 (主买大单股, 主卖大单股, 主买笔, 主卖笔)。

    单独查 large_* 列（不动 db_get_recent_ticks 的 6 列 SELECT，保冰山位置索引）。默认当日。
    """
    if since_ts is None:
        since_ts = datetime.date.today().isoformat()
    rows = conn.execute(
        "SELECT large_buy_vol, large_sell_vol, large_buy_cnt, large_sell_cnt "
        "FROM tick_flow WHERE ts >= ? ORDER BY id DESC LIMIT ?",
        (since_ts, n),
    ).fetchall()
    lb = sum((r[0] or 0) for r in rows)
    ls = sum((r[1] or 0) for r in rows)
    cb = sum((r[2] or 0) for r in rows)
    cs = sum((r[3] or 0) for r in rows)
    return lb, ls, int(cb), int(cs)


def db_save_broker(conn: sqlite3.Connection, ts: str,
                   bid_top_name: Optional[str], bid_top_net: int,
                   ask_top_name: Optional[str], ask_top_net: int,
                   bid_inst: int = 0, bid_retail: int = 0,
                   ask_inst: int = 0, ask_retail: int = 0):
    # 列名显式（迭代四十）：表已扩席位分类列，positional VALUES 会列数不符。
    conn.execute(
        "INSERT INTO broker_queue "
        "(ts, bid_top_name, bid_top_net, ask_top_name, ask_top_net, "
        "bid_inst, bid_retail, ask_inst, ask_retail) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (ts, bid_top_name, bid_top_net, ask_top_name, ask_top_net,
         bid_inst, bid_retail, ask_inst, ask_retail),
    )
    conn.commit()


def db_get_recent_brokers(
    conn: sqlite3.Connection, n: int, since_ts: Optional[str] = None,
) -> list[tuple]:
    """返回最近 n 条 (bid_top_name, bid_top_net, ask_top_name, ask_top_net)，最新在前，默认仅限当日。

    与冰山同理：经纪足迹是日内概念，跨日窗口会把昨日席位污染判定，故 today() 过滤。
    """
    if since_ts is None:
        since_ts = datetime.date.today().isoformat()
    rows = conn.execute(
        "SELECT bid_top_name, bid_top_net, ask_top_name, ask_top_net "
        "FROM broker_queue WHERE ts >= ? ORDER BY id DESC LIMIT ?",
        (since_ts, n),
    ).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def db_save_signal(conn: sqlite3.Connection, sig_type: str,
                   detail: str, score: int, cooldown_secs: int = 300):
    """写入信号，同类型信号在 cooldown_secs 秒内不重复记录。"""
    cutoff = (datetime.datetime.now() - datetime.timedelta(seconds=cooldown_secs)
              ).isoformat(timespec="seconds")
    existing = conn.execute(
        "SELECT id FROM signals WHERE signal_type=? AND ts>=? LIMIT 1",
        (sig_type, cutoff),
    ).fetchone()
    if existing:
        return
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO signals VALUES (NULL,?,?,?,?)",
        (ts, sig_type, detail, score),
    )
    conn.commit()


def db_write_monitor_state(
    conn: sqlite3.Connection,
    squeeze_score: int, short_score: int, short_signal: str,
    price: Optional[float], ask_depth: Optional[float],
    imbalance: Optional[float], big_net: Optional[float],
):
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT OR REPLACE INTO monitor_state VALUES (1,?,?,?,?,?,?,?,?)",
        (ts, squeeze_score, short_score, short_signal,
         price, ask_depth, imbalance, big_net),
    )
    conn.commit()


def db_get_recent_hkex(conn: sqlite3.Connection, n: int) -> list[float]:
    """返回最近 n 个交易日的卖空占比（最新在后）。"""
    rows = conn.execute(
        "SELECT short_ratio FROM hkex_daily ORDER BY date DESC LIMIT ?", (n,)
    ).fetchall()
    return [r[0] for r in reversed(rows)]


def db_get_recent_ask_depth(conn: sqlite3.Connection, n: int,
                            since_ts: Optional[str] = None) -> list[float]:
    """返回最近 n 轮 ask_depth（最新在前），默认仅限当日交易时段。

    Why 当日过滤：orderbook_snapshots 跨交易日累积，滚动窗口若越界会把上一
    交易日的盘口深度混入基准。实盘 2026-06-03 09:30 06082 案例：当日真实盘口
    仅几百~几千股，但 60 轮窗口被 06-02 收盘前 ~13 万股厚盘口主导，median 基准
    ≈133,000，导致每轮触发假"卖盘深度骤减 99% [+25 分]"，逼空评分被噪音顶到 49~65。
    """
    if since_ts is None:
        since_ts = datetime.date.today().isoformat()
    rows = conn.execute(
        "SELECT ask_depth FROM orderbook_snapshots WHERE ts >= ? "
        "ORDER BY id DESC LIMIT ?", (since_ts, n),
    ).fetchall()
    return [r[0] for r in rows]


def db_get_recent_big_net(conn: sqlite3.Connection, n: int,
                          since_ts: Optional[str] = None) -> list[float]:
    """返回最近 n 条 big_net（最新在前），默认仅限当日。

    big_net 是日内累计 HKD、每个交易日从 0 重置；跨日读取会把昨日大额累计混入
    今日的动能/连续性/资金效率判断与开盘首轮 Δ，产生巨幅假信号。
    """
    if since_ts is None:
        since_ts = datetime.date.today().isoformat()
    rows = conn.execute(
        "SELECT big_net FROM capital_flow WHERE ts >= ? ORDER BY id DESC LIMIT ?",
        (since_ts, n),
    ).fetchall()
    return [r[0] for r in rows]


def db_get_recent_capital_structure(
    conn: sqlite3.Connection, n: int, since_ts: Optional[str] = None,
) -> list[tuple[float, float, float]]:
    """返回最近 n 条 (big_net, mid_net, small_net)，最新在前，默认仅限当日。

    与 big_net 同理：三档均为日内累计值、每日清零，跨日窗口会污染背离/FOMO/拆单检测。
    """
    if since_ts is None:
        since_ts = datetime.date.today().isoformat()
    rows = conn.execute(
        "SELECT big_net, mid_net, small_net FROM capital_flow "
        "WHERE ts >= ? ORDER BY id DESC LIMIT ?",
        (since_ts, n),
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def db_get_session_small_net_peak(
    conn: sqlite3.Connection, since_ts: str,
) -> Optional[float]:
    """返回 since_ts 起 small_net 累计的日内峰值；无数据时返回 None。"""
    row = conn.execute(
        "SELECT MAX(small_net) FROM capital_flow WHERE ts >= ?", (since_ts,)
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def db_get_session_big_net_low(
    conn: sqlite3.Connection, since_ts: str,
) -> Optional[float]:
    """返回 since_ts 起 big_net 累计的日内最低值（最负值）；无数据时返回 None。

    与 small_net_peak 对称——派发模式检测需要"大单累计是否创日内新低"作为
    机构持续出货的硬证据。当 latest big_net ≈ session_low 时，说明今日大单
    流出已达极值，未见回补意愿。
    """
    row = conn.execute(
        "SELECT MIN(big_net) FROM capital_flow WHERE ts >= ?", (since_ts,)
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def db_get_session_mid_small_peak(
    conn: sqlite3.Connection, since_ts: str,
) -> Optional[float]:
    """返回 since_ts 起 (mid_net + small_net) 累计的日内峰值；无数据时返回 None。

    非机构资金（中单 + 散单）整体峰值。当总值从峰值大幅回落时，说明非机构
    接盘者已被消化大半——这是派发末期的关键特征，区别于单纯散户撤退。
    """
    row = conn.execute(
        "SELECT MAX(mid_net + small_net) FROM capital_flow WHERE ts >= ?",
        (since_ts,),
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def db_save_price(conn: sqlite3.Connection, ts: str, price: float):
    # 拒绝 0/None/NaN：Futu 网络异常时 last_price 偶尔会回 0，会污染下游所有价差计算
    if price is None or not math.isfinite(price) or price <= 0:
        log.warning(f"[价格异常] 拒绝写入 price={price!r} @ {ts}")
        return
    conn.execute("INSERT INTO price_history VALUES (NULL,?,?)", (ts, price))
    conn.commit()


def db_get_recent_prices(conn: sqlite3.Connection, n: int,
                         since_ts: Optional[str] = None) -> list[float]:
    """返回最近 n 轮价格，最新在后（时间升序）。过滤掉 0/None 历史脏值，默认仅限当日。

    当日过滤：反弹/动能窗口是日内概念，跨日会把昨日收盘价当作"近期低点/高点"，
    在开盘首段产生失真的反转信号。日内 anchor 见 db_get_session_high/low。
    """
    if since_ts is None:
        since_ts = datetime.date.today().isoformat()
    rows = conn.execute(
        "SELECT price FROM price_history WHERE ts >= ? ORDER BY id DESC LIMIT ?",
        (since_ts, n),
    ).fetchall()
    return [r[0] for r in reversed(rows)
            if r[0] is not None and r[0] > 0 and math.isfinite(r[0])]


def db_get_session_high(conn: sqlite3.Connection,
                        since_ts: str) -> Optional[float]:
    """
    返回 since_ts (含)之后所有价格的最大值；无数据时返回 None。

    Why: `db_get_recent_prices` 滑动窗口取 max 在持续下跌时会被价格自身拉低
    （高点滚出窗口），导致"较近期高点下跌 X%"信号严重失真。日内 anchor 不会漂移。
    """
    row = conn.execute(
        "SELECT MAX(price) FROM price_history WHERE ts >= ?", (since_ts,)
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def db_get_session_low(conn: sqlite3.Connection,
                       since_ts: str) -> Optional[float]:
    """返回 since_ts (含) 之后所有价格的最小值；无数据时返回 None。

    与 session_high 对称——出货式拉升检测需用日内 anchor 衡量已实现涨幅，
    避免滑动窗口在主升浪后被自身拉高导致 0 涨幅的假象。
    """
    row = conn.execute(
        "SELECT MIN(price) FROM price_history WHERE ts >= ? AND price > 0", (since_ts,)
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def db_count_imb_flips(conn: sqlite3.Connection, n: int,
                       neutral_band: float) -> int:
    """
    统计最近 n 轮 orderbook_snapshots.imbalance 的极性翻转次数。

    |imb| ≤ neutral_band 的中性轮过滤掉（不参与翻转计数），避免在 0 附近抖动
    被误算成翻转。返回值 0 表示稳定方向；≥2 通常意味着挂单博弈/操纵性信号。

    Spike 过滤：单点孤立尖峰（v[i] 与左右邻居方向都相反）视为 1-tick 挂撤噪声，
    从序列中剔除后再计数。否则一个孤立 spike 会贡献 2 次翻转（进入+离开），把
    "实际只有 1 次摆盘异动" 误算为"高频博弈"，锁死真实信号 60s+（实盘 2026-05-15
    14:13:43 → 14:14:29 案例：单个 +0.591 spike 把后续 60 秒清晰偏空信号全部 BLOCKED）。
    """
    since_ts = datetime.date.today().isoformat()
    rows = conn.execute(
        "SELECT imbalance FROM orderbook_snapshots WHERE ts >= ? "
        "ORDER BY id DESC LIMIT ?", (since_ts, n),
    ).fetchall()
    series = [r[0] for r in reversed(rows) if abs(r[0]) > neutral_band]

    # 剔除孤立尖峰：a[i] 与两个邻居方向都相反 → 1-tick spike，不计入翻转
    filtered: list[float] = []
    for i, v in enumerate(series):
        is_spike = (
            0 < i < len(series) - 1
            and (series[i - 1] > 0) != (v > 0)
            and (series[i + 1] > 0) != (v > 0)
        )
        if not is_spike:
            filtered.append(v)

    flips = 0
    for i in range(1, len(filtered)):
        if (filtered[i] > 0) != (filtered[i - 1] > 0):
            flips += 1
    return flips


# ═══════════════════════════════════════════════════════════
# 三-B、交易时段判断
# ═══════════════════════════════════════════════════════════
def _is_trading_hours(now: datetime.datetime) -> bool:
    """
    判断当前是否在港股连续交易时段。
    跳过：周末、盘前、开盘集合竞价、午休、收盘集合竞价（CAS）、盘后。

    Why: CAS 期间盘口为集合挂单汇总（实盘 2026-05-15 案例：16:01 后卖盘深度
    暴增到 4-5 万股、价格"卡死"在 IEP 800），脚本会把集合挂单误判为"大卖单
    涌入"持续发出 ENTRY=55 假信号 7+ 分钟。仅在连续交易时段打分能避免此问题。
    """
    if now.weekday() >= 5:    # 周末
        return False
    t = now.time()
    return any(start <= t < end for start, end in HK_TRADING_SESSIONS)


def _trading_phase_label(now: datetime.datetime) -> str:
    """返回当前所处交易时段的可读标签，用于日志输出。"""
    if now.weekday() >= 5:
        return "周末休市"
    t = now.time()
    if t < datetime.time(9, 0):
        return "盘前"
    if t < datetime.time(9, 30):
        return "开盘集合竞价"
    if datetime.time(12, 0) <= t < datetime.time(13, 0):
        return "午休"
    if datetime.time(16, 0) <= t < datetime.time(16, 10):
        return "收盘集合竞价(CAS)"
    if t >= datetime.time(16, 10):
        return "盘后"
    return "非交易时段"


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


def scrape_hkex_short(date: datetime.date, stock_code: Optional[str] = None
                      ) -> Optional[dict]:
    """
    爬取 HKEX Daily Quotations 中的 SHORT SELLING TURNOVER 段落。

    文件格式为固定宽度预格式化文本（<pre> 标签），数据行示例：
        100 MINIMAX-W     323,100   274,762,770   2,149,528   1,869,742,390
    列顺序：CODE  NAME  SHORT_VOL(SH)  SHORT_VALUE($)  TOTAL_VOL(SH)  TOTAL_VALUE($)

    股票代码在文件中为纯整数（100），无前导零。
    """
    stock_code = stock_code or STOCK_CODE
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
    返回各级别净流入（原始 HKD，非万港元；显示时需 / 10000）。

    ⚠ 核心逻辑：按"主动方(Aggressor)"分类，非"谁拿到货"。
    - 成交打在卖一价 → 主动买 → 计入流入 (capital_in)
    - 成交打在买一价 → 主动卖 → 计入流出 (capital_out)
    故：大单流入可能包含"散户主动买入机构挂的被动卖单"（机构出货）。

    Futu OpenD 通常每分钟才刷新一次，按 update_time 去重，
    避免同一快照被多次写入 capital_flow 表，污染信号窗口。
    """
    ret, data = ctx.get_capital_distribution(SYMBOL)
    if ret != RET_OK or data.empty:
        log.warning(f"get_capital_distribution 失败: {data}")
        return None

    row = data.iloc[0]
    update_time = row.get("update_time") if "update_time" in row.index else None

    def _f(col: str) -> float:
        return float(row.get(col, 0) or 0)

    big_in  = _f("capital_in_big")
    big_out = _f("capital_out_big")
    mid_in  = _f("capital_in_mid")
    mid_out = _f("capital_out_mid")
    sml_in  = _f("capital_in_small")
    sml_out = _f("capital_out_small")

    big_net   = big_in - big_out
    mid_net   = mid_in - mid_out
    small_net = sml_in - sml_out

    # 去重：相同 update_time 视为同一快照；缺失时退化为三元组比较。
    cache_key = update_time if update_time else (big_net, mid_net, small_net)
    if getattr(fetch_capital_flow, "_last_key", None) == cache_key:
        return getattr(fetch_capital_flow, "_last_result", None)

    ts = datetime.datetime.now().isoformat(timespec="seconds")
    db_save_capital(conn, ts, big_in, big_out, big_net, mid_net, small_net)

    result = {"ts": ts, "big_net": big_net, "mid_net": mid_net, "small_net": small_net}
    fetch_capital_flow._last_key = cache_key
    fetch_capital_flow._last_result = result
    return result


def analyze_capital_flow(conn: sqlite3.Connection) -> tuple[int, list[str]]:
    """
    大单净流入逼空信号检测：
    - 连续 BIGFLOW_REVERSAL_MIN 轮 big_net > 0，且此前有负值 → 反转信号
    - 大单净流入持续放大 → 加速信号

    Futu 资金流约每分钟才刷新；fetch_capital_flow 已按 update_time 去重。
    若 capital_flow 表自上次评分后没有新行，直接复用上一轮的 score/signals，
    避免同一份数据被反复打分、产生 dashboard 噪音。
    """
    latest_id_row = conn.execute(
        "SELECT id FROM capital_flow ORDER BY id DESC LIMIT 1"
    ).fetchone()
    latest_id = latest_id_row[0] if latest_id_row else None
    if latest_id is not None and getattr(analyze_capital_flow, "_last_id", None) == latest_id:
        return getattr(analyze_capital_flow, "_last_result", (0, []))

    history = db_get_recent_big_net(conn, BIGFLOW_WINDOW)
    if len(history) < BIGFLOW_REVERSAL_MIN + 1:
        result = (0, [])
        analyze_capital_flow._last_id = latest_id
        analyze_capital_flow._last_result = result
        return result

    score = 0
    signals: list[str] = []

    recent  = history[:BIGFLOW_REVERSAL_MIN]   # 最新 N 轮（最新在前）
    earlier = history[BIGFLOW_REVERSAL_MIN:]    # 更早期

    all_recent_positive  = all(v > 0 for v in recent)
    had_earlier_negative = any(v < 0 for v in earlier)

    # 计算实际连续正值轮数（独立窗口，避免被 BIGFLOW_WINDOW 截断为 ≤10）
    streak_history = db_get_recent_big_net(conn, BIGFLOW_STREAK_WINDOW)
    streak = 0
    for v in streak_history:
        if v > 0:
            streak += 1
        else:
            break

    if all_recent_positive and had_earlier_negative:
        pts = 25
        total_inflow = recent[0] / 10000   # 取最新累计值（日内累计额，不应叠加多轮）
        msg = (f"大单净流入反转：连续 {streak} 轮正值 "
               f"当前累计 {total_inflow:+,.1f} 万港元 [+{pts}分]")
        score += pts
        signals.append(msg)
        log.warning(f"[资金反转] {msg}")
        db_save_signal(conn, "BIG_FLOW_REVERSAL", msg, pts)

    elif all_recent_positive:
        # 持续净流入但未经历负值阶段：按 streak 长度分档（保守降权）。
        # 旧值 5/10/15 让逼空评分常态有 +15 底分，与"持续上行"市场常态混淆。
        # 反转 + 持续仍给 +25，纯持续顶格 +12。
        #
        # Bug 13 防护：价格同步性核查
        # 2026-05-18 实盘：大单净流入 +2,121 → +5,052 万持续加速正值，但价格
        # 仅 777 → 796.5（+2.5% 后无法突破日高 827.5），次日跳空 -8%。机构
        # 在用大买单接散户卖单出货 —— 统计上"主动买入"但实际是出货。
        # 故当流入持续正值而同期价格停滞（< 0.3%）时，不计逼空风险分。
        #
        # Bug 20 反向修正（2026-05-26）：主升浪后的高位整理也会"局部停滞"
        # 但日内累计已大涨，是真逼空不是出货。改为同时核查日内累计涨幅：
        #   局部停滞 + 日内大涨 → 高位整理（仍计分）
        #   局部停滞 + 日内未涨 → 真出货式拉升（不计分）
        #
        # Bug 21 边界修复（2026-05-26）：日内涨幅是滞后指标。当价格已从近期
        # 高点回落时，"日内已涨"仍成立但盘面方向已逆转——机构在高位接散户卖单。
        # 5/26 14:00 实盘：价格 836→806（-3.7%），大单累计仍 +810 万。
        # 加第三道守门：从近 N 轮峰值回落 ≥ 1.5% → 顶部已现，覆盖 Bug 20 路径。
        sync_window = min(streak, 30)
        sync_prices = db_get_recent_prices(conn, sync_window)
        price_pct = 0.0
        have_sync = len(sync_prices) >= 5 and sync_prices[0] > 0
        if have_sync:
            price_pct = (sync_prices[-1] - sync_prices[0]) / sync_prices[0] * 100

        current_close = sync_prices[-1] if sync_prices else None
        session_low_anchor = db_get_session_low(
            conn, datetime.date.today().isoformat()
        )
        intraday_gain = 0.0
        if (current_close is not None
                and session_low_anchor is not None
                and session_low_anchor > 0):
            intraday_gain = (current_close - session_low_anchor) \
                            / session_low_anchor * 100

        # Bug 21：近期峰值回落核查（独立窗口，不复用 sync_window 以保证语义稳定）
        peak_window_prices = db_get_recent_prices(conn, BIGFLOW_PUMP_PEAK_WINDOW)
        pullback_from_peak = 0.0
        if (current_close is not None and len(peak_window_prices) >= 5):
            recent_peak = max(peak_window_prices)
            if recent_peak > 0:
                pullback_from_peak = (recent_peak - current_close) \
                                     / recent_peak * 100

        is_local_stagnant = have_sync and price_pct < BIGFLOW_PUMP_STAGNANT_PCT
        is_intraday_rallied = intraday_gain >= BIGFLOW_PUMP_INTRADAY_MIN
        is_pulled_back_from_peak = (
            pullback_from_peak >= BIGFLOW_PUMP_PEAK_PULLBACK_PCT
        )

        # 三元判断：
        #   停滞 + 日内未涨           → 真出货（Bug 13 旧路径）
        #   停滞 + 日内已涨 + 已回落  → 真出货（Bug 21 新路径，顶部出货）
        #   停滞 + 日内已涨 + 未回落  → 高位整理（Bug 20 路径）
        is_distribution_suspect = is_local_stagnant and (
            not is_intraday_rallied or is_pulled_back_from_peak
        )

        if is_distribution_suspect:
            # 迭代二十六：从峰值回落幅度大于"价格 capitulation 阈值"时升级文案
            # 并主动扣减 squeeze 分。实盘 15:22-15:28 价格 917→840 (-8.4%)，但
            # 此前 28 轮持续触发"疑似顶部出货 不计分"——pullback 已超 5% 时
            # 应升级为"破位已确认"+ 给 squeeze 减分，让 ENTRY 自然解锁。
            capitulated = (
                is_intraday_rallied
                and pullback_from_peak >= SHORT_BLOCK_SESSION_PULLBACK_PCT
            )
            if capitulated:
                # 破位已确认：squeeze 扣 -15 分（覆盖原本可能加的持续正值分）
                damper_pts = -15
                score += damper_pts
                msg = (f"⚠⚠ 大单净流入持续 {streak} 轮但价格已从近 "
                       f"{BIGFLOW_PUMP_PEAK_WINDOW} 轮峰值回落 "
                       f"{pullback_from_peak:.2f}%（≥ {SHORT_BLOCK_SESSION_PULLBACK_PCT}%），"
                       f"顶部出货已确认 [逼空 {damper_pts} 分]")
            elif is_intraday_rallied and is_pulled_back_from_peak:
                # Bug 21 文案：从峰值轻度回落（1.5%-3%），疑似顶部出货
                msg = (f"⚠ 大单净流入持续 {streak} 轮但价格已从近 "
                       f"{BIGFLOW_PUMP_PEAK_WINDOW} 轮峰值回落 "
                       f"{pullback_from_peak:.2f}%，疑似顶部出货，不计逼空分")
            else:
                # Bug 13 原文案：低位停滞
                msg = (f"⚠ 大单净流入持续 {streak} 轮但价格 {price_pct:+.2f}%"
                       f"（近 {sync_window} 轮停滞），疑似出货式拉升，不计逼空分")
            signals.append(msg)
            db_save_signal(conn, "BIGFLOW_PUMP_SUSPECT", msg, 0)
        else:
            if streak >= 16:
                pts = 12
            elif streak >= 8:
                pts = 8
            elif streak >= 4:
                pts = 5
            else:
                pts = 3
            if is_local_stagnant and is_intraday_rallied:
                # Bug 20 路径：局部停滞但日内已涨且未从峰值回落，是真高位整理
                msg = (f"大单净流入持续正值 {streak} 轮"
                       f"（日内已涨 {intraday_gain:+.1f}%，"
                       f"近 {sync_window} 轮高位整理 {price_pct:+.2f}%）[+{pts}分]")
            else:
                msg = f"大单净流入持续正值 {streak} 轮 [+{pts}分]"
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

    # Bug 16: 累计仍负但近期 Δ 显著转买入 + 价格咬合 → 资金方向转向，空头回补/多头进场
    # 实盘 2026-05-20 10:19-10:25：大单累计 -1083→-988→-894 万（5 分钟 +189 万买入），
    # 价格 698→708 (+1.4%)，但因累计仍负，旧逻辑只在 analyze_short_entry 维度 1 加
    # +15"持续为负"，对真实的资金转向毫无感知。
    if (len(recent) >= 2
            and recent[0] < 0
            and (recent[0] - recent[1]) >= BIG_NET_DELTA_THRESHOLD):
        flow_delta = recent[0] - recent[1]
        sync_prices = db_get_recent_prices(conn, 8)
        if len(sync_prices) >= 5 and sync_prices[0] > 0:
            price_pct = (sync_prices[-1] - sync_prices[0]) / sync_prices[0] * 100
            if price_pct >= BIG_NET_REBUY_PRICE_PCT:
                pts = BIG_FLOW_REBUY_PTS
                msg = (f"大单累计 {recent[0]/10000:+,.1f} 万仍为负但近期 Δ "
                       f"{flow_delta/10000:+,.1f} 万买入，价格 {price_pct:+.2f}% "
                       f"咬合 → 空头回补/多头进场 [+{pts}分]")
                score += pts
                signals.append(msg)
                db_save_signal(conn, "BIG_FLOW_REBUY", msg, pts)

    result = (score, signals)
    analyze_capital_flow._last_id = latest_id
    analyze_capital_flow._last_result = result
    return result


# ═══════════════════════════════════════════════════════════
# 五-B、资金结构背离检测（中小单 vs 大单）
# ═══════════════════════════════════════════════════════════
def analyze_capital_structure(conn: sqlite3.Connection) -> tuple[int, list[str]]:
    """
    检测大单与中小单（散户）资金流的背离：

    - 大单正 + 中小单持续负 → 主力接散户抛盘（出货嫌疑），逼空评分折扣
    - 大单正 + 中小单同向正 → 全档位同向买入（真实逼空确认），无异常
    - 大单冻结 + 中小单净出 → 散户出逃，主力观望，警惕回落

    返回 (score_adjustment, signals)：
      score_adjustment < 0 表示应减分（给逼空评分降权）
      score_adjustment > 0 表示支撑做空加分（出货确认）

    迭代二十六改动（2026-05-29）：背离/方向判定从"累计值符号"改为"窗口 Δ 符号"。
    实盘 15:14-15:28 误报：上午累计 −5,500 万的中小单留在 capital_flow 里，下午
    单看 Δ 已大幅净买入（mid_net 从 −2,261 涨到 +2,392 万），但旧逻辑仍按累计
    符号判定为"持续净流出"。修法：相邻两条记录差 = 该段时间的净流量，更真实。
    """
    # 需要 N+1 条记录算出 N 个 Δ
    rows = db_get_recent_capital_structure(conn, CAPITAL_STRUCT_DIVERGE_ROUNDS + 1)
    if len(rows) < CAPITAL_STRUCT_DIVERGE_ROUNDS + 1:
        return 0, []

    # 计算相邻 Δ：rows[0] 最新, rows[-1] 最旧 → 算 N 个增量
    deltas: list[tuple[float, float, float]] = []  # (big_delta, mid_delta, small_delta)
    for i in range(CAPITAL_STRUCT_DIVERGE_ROUNDS):
        big_d   = rows[i][0] - rows[i + 1][0]
        mid_d   = rows[i][1] - rows[i + 1][1]
        small_d = rows[i][2] - rows[i + 1][2]
        deltas.append((big_d, mid_d, small_d))

    # 最新一轮的"瞬时方向"用于文案展示
    latest_retail_delta = deltas[0][1] + deltas[0][2]

    big_positive_count = sum(1 for bd, _, _ in deltas if bd > 0)
    retail_negative_count = sum(
        1 for _, md, sd in deltas if (md + sd) < CAPITAL_STRUCT_SMALL_THRESHOLD
    )

    score = 0
    signals: list[str] = []

    # 场景 1：大单 Δ 持续正 + 中小单 Δ 持续负 → 当前段时间的出货背离
    if (big_positive_count >= CAPITAL_STRUCT_DIVERGE_ROUNDS
            and retail_negative_count >= CAPITAL_STRUCT_DIVERGE_ROUNDS):
        avg_retail_delta = statistics.mean((md + sd) for _, md, sd in deltas)
        score = 15
        msg = (
            f"⚠ 资金结构背离：大单 Δ 持续正但中小单 Δ 连续 "
            f"{retail_negative_count} 轮净流出（均值 "
            f"{avg_retail_delta/10000:+,.1f} 万/轮），散户出逃，出货嫌疑 "
            f"[支撑做空+{score}分]"
        )
        signals.append(msg)
        db_save_signal(conn, "CAPITAL_STRUCT_DIVERGE", msg, score)
        return score, signals

    # 场景 2：大单 Δ 正 + 中小单 Δ 弱流出（未达阈值但方向相反）→ 轻度警告
    retail_neg_mild = sum(1 for _, md, sd in deltas if (md + sd) < 0)
    if (big_positive_count >= CAPITAL_STRUCT_DIVERGE_ROUNDS
            and retail_neg_mild >= CAPITAL_STRUCT_DIVERGE_ROUNDS):
        score = -8
        msg = (
            f"资金结构偏离：大单 Δ 正但中小单 Δ {retail_neg_mild} 轮为负"
            f"（本轮 {latest_retail_delta/10000:+,.1f} 万），逼空折扣 {score} 分"
        )
        signals.append(msg)
        return score, signals

    # 场景 3：全档位 Δ 同向正值 → 真实买盘确认（信息性，不加分）
    all_positive = all(bd > 0 and (md + sd) > 0 for bd, md, sd in deltas)
    if all_positive:
        msg = (
            f"资金全档 Δ 同向流入 {len(deltas)} 轮"
            f"（本轮中小单 Δ {latest_retail_delta/10000:+,.1f} 万），买盘真实"
        )
        signals.append(msg)

    return score, signals


# ═══════════════════════════════════════════════════════════
# 五-C、散户撤退预警（small_net 从日内峰值回落）
# ═══════════════════════════════════════════════════════════
def analyze_retail_retreat(conn: sqlite3.Connection) -> tuple[int, list[str]]:
    """
    检测散户接盘力量耗尽：当 small_net 累计从日内峰值回落超过阈值时，
    散户买盘已见顶，常出现在分批出货的末段——价格随后破位概率高。

    返回正值 = 支撑做空分（接盘耗尽 → 顶部已现）。
    """
    today_iso = datetime.date.today().isoformat()
    peak = db_get_session_small_net_peak(conn, today_iso)
    if peak is None or peak < RETAIL_RETREAT_MIN_PEAK:
        return 0, []

    rows = db_get_recent_capital_structure(conn, 1)
    if not rows:
        return 0, []
    _, _, small_now = rows[0]

    retreat = peak - small_now
    if retreat <= 0:
        return 0, []
    retreat_pct = retreat / peak

    if retreat_pct >= RETAIL_RETREAT_PCT_HEAVY:
        pts = 20
        msg = (
            f"⚠ 散户接盘耗尽：small_net 从日内峰值 {peak/10000:+,.1f} 万"
            f"回落 {retreat/10000:.1f} 万 ({retreat_pct*100:.1f}%) [支撑做空+{pts}分]"
        )
        db_save_signal(conn, "RETAIL_RETREAT_HEAVY", msg, pts)
        return pts, [msg]
    if retreat_pct >= RETAIL_RETREAT_PCT:
        pts = 12
        msg = (
            f"⚠ 散户买盘见顶：small_net 从日内峰值 {peak/10000:+,.1f} 万"
            f"回落 {retreat/10000:.1f} 万 ({retreat_pct*100:.1f}%) [支撑做空+{pts}分]"
        )
        db_save_signal(conn, "RETAIL_RETREAT", msg, pts)
        return pts, [msg]
    return 0, []


# ═══════════════════════════════════════════════════════════
# 五-C2、派发模式三条件共振（评分盲区补丁，2026-05-29 实盘案例）
# ═══════════════════════════════════════════════════════════
def analyze_distribution_mode(
    conn: sqlite3.Connection,
) -> tuple[int, bool, list[str]]:
    """
    派发末期识别：当下面三条件同时成立时，今日已确认机构出货 + 散户被消化：
      ① 大单累计接近或创日内新低（机构持续净流出，无回补）
      ② 散户 small_net 从日内峰值大幅回落（接盘者撤退）
      ③ 中小单合计累计从日内峰值大幅回落（非机构资金整体撤退，区别于单一
         散户撤退——后者可能伴随中单接盘，本条排除"主力换手"假象）

    Why: 现有 retail_retreat 只看 small_net，触发后只进入 support_score
    背景分，不能解开 BLOCKED。但 2026-05-29 10:04 实盘显示派发末期评分系统
    完全失灵（squeeze=26 "正常"），需要一个独立维度同时：
      - 给做空主信号加分（不再依赖反弹/卖盘骤减这种被噪音污染的维度）
      - 解开 BLOCKED 安全门（"派发已确认 → 逼空假设已死"）

    返回 (score, confirmed_flag, signals)：
      - score：触发时给 25 分（主信号），否则 0
      - confirmed_flag：是否完整命中三条件，供 BLOCKED 放行使用
    """
    today_iso = datetime.date.today().isoformat()

    big_low = db_get_session_big_net_low(conn, today_iso)
    small_peak = db_get_session_small_net_peak(conn, today_iso)
    mid_small_peak = db_get_session_mid_small_peak(conn, today_iso)
    if big_low is None or small_peak is None or mid_small_peak is None:
        return 0, False, []

    rows = db_get_recent_capital_structure(conn, 1)
    if not rows:
        return 0, False, []
    big_now, mid_now, small_now = rows[0]
    mid_small_now = (mid_now or 0) + (small_now or 0)

    # ① 大单累计接近日内最低值（用比例比较，避免阈值漂移）。同时要求日内
    # 最低值本身要足够深，过滤"全天微动"场景。
    if big_low > DISTRIBUTION_BIGNET_MIN_DEPTH:
        return 0, False, []
    big_at_low = big_now <= big_low * DISTRIBUTION_BIGNET_LOW_RATIO  # 注意负数比较

    # ② 散户从峰值回落（独立判定，与 retail_retreat 阈值不同：派发模式要求更严）
    if small_peak < RETAIL_RETREAT_MIN_PEAK:
        return 0, False, []
    small_retreat_pct = (small_peak - small_now) / small_peak if small_peak > 0 else 0
    small_retreated = small_retreat_pct >= DISTRIBUTION_SMALL_RETREAT_PCT

    # ③ 中小单合计累计从峰值回落
    if mid_small_peak < DISTRIBUTION_MIDSMALL_MIN_PEAK:
        return 0, False, []
    midsmall_decay_pct = (
        (mid_small_peak - mid_small_now) / mid_small_peak if mid_small_peak > 0 else 0
    )
    midsmall_decayed = midsmall_decay_pct >= DISTRIBUTION_MIDSMALL_DECAY_PCT

    if not (big_at_low and small_retreated and midsmall_decayed):
        return 0, False, []

    pts = DISTRIBUTION_SCORE
    msg = (
        f"派发模式确认：大单累计 {big_now/10000:+,.1f} 万(日内低 "
        f"{big_low/10000:+,.1f}) + 散户撤 {small_retreat_pct*100:.0f}% "
        f"+ 中小单撤 {midsmall_decay_pct*100:.0f}% → 机构出货已确认 [+{pts}分]"
    )
    db_save_signal(conn, "DISTRIBUTION_MODE", msg, pts)
    return pts, True, [msg]


# ═══════════════════════════════════════════════════════════
# 五-D、资金效率检测（大单流入但价格无响应 → 拆单/吸筹嫌疑）
# ═══════════════════════════════════════════════════════════
def analyze_capital_efficiency(
    conn: sqlite3.Connection, current_price: Optional[float],
) -> tuple[int, list[str]]:
    """
    资金效率 = 价格涨幅 / 大单累计净流入。
    窗口内大单显著流入但价格几乎不动 → 流入被流动性吸收，机构在另一面卖出。
    与 Bug 13 出货式检测互补——它看 streak，本函数看 Δ。

    返回负值 = squeeze_damper（折扣逼空分，防假逼空）。
    """
    if current_price is None:
        return 0, []

    bn_history = db_get_recent_big_net(conn, CAPITAL_EFFICIENCY_WINDOW)
    if len(bn_history) < CAPITAL_EFFICIENCY_WINDOW:
        return 0, []

    # capital_flow 是累计值，窗口内 Δ = 最新 - 最旧
    big_net_delta = bn_history[0] - bn_history[-1]
    if big_net_delta < CAPITAL_EFFICIENCY_MIN_INFLOW:
        return 0, []

    prices = db_get_recent_prices(conn, CAPITAL_EFFICIENCY_WINDOW * 4)
    if len(prices) < 10 or prices[0] <= 0:
        return 0, []
    price_pct = (prices[-1] - prices[0]) / prices[0] * 100

    if price_pct >= CAPITAL_EFFICIENCY_PRICE_THRESHOLD:
        return 0, []

    pts = -8
    msg = (
        f"资金效率低：近 {CAPITAL_EFFICIENCY_WINDOW} 轮大单净流入 "
        f"{big_net_delta/10000:+,.1f} 万但价格仅 {price_pct:+.2f}% "
        f"（吸筹/拆单嫌疑），逼空折扣 {pts} 分"
    )
    db_save_signal(conn, "CAPITAL_EFFICIENCY_LOW", msg, pts)
    return pts, [msg]


# ═══════════════════════════════════════════════════════════
# 五-D2、卖而不跌裁决（净流出 + 价格守位/反弹 → 被动吸筹/诱空）
# ═══════════════════════════════════════════════════════════
def analyze_sell_no_drop(
    conn: sqlite3.Connection, current_price: Optional[float],
) -> tuple[int, list[str]]:
    """
    「资金方向 × 价格」背离的"卖而不跌"象限裁决器，对称于 analyze_distribution_mode
    的"卖而跌"。微观成因：主力挂被动买单接货时，对手主动卖单按 aggressor 计入"流出"，
    故「(大单+中单) 持续净流出 + 价格不跌反涨」≠ 主力在卖，而是有大资金被动吸收 →
    吸筹/诱空，应抬升逼空风险、抑制顺势追空（[[capital-flow-tiers-intent]]）。

    判定（全部成立）：
      ① 窗口内 (big_net + mid_net) Δ ≤ SELL_NO_DROP_MIN_OUTFLOW（持续净流出）
      ② 最新 (big_net + mid_net) < 0（确为净流出格局，排除高位回吐）
      ③ 同期价格涨幅 ≥ SELL_NO_DROP_PRICE_FLOOR（守位/反弹，未跟跌）
      ④ 二次否决：最新价 > 窗口最低价（未破位；破位=卖压已兑现，真派发不拦）

    返回正值 = 抬升逼空风险（喂做空安全门，BLOCK 顺势追空）。
    """
    if current_price is None:
        return 0, []

    rows = db_get_recent_capital_structure(conn, SELL_NO_DROP_WINDOW)
    if len(rows) < SELL_NO_DROP_WINDOW:
        return 0, []

    # rows[0] 最新、rows[-1] 最旧；三档均为日内累计值，故窗口 Δ = 最新 - 最旧
    bigmid_now   = rows[0][0] + rows[0][1]
    bigmid_delta = bigmid_now - (rows[-1][0] + rows[-1][1])
    # ①持续净流出 + ②确为净流出格局（排除从高位正值回吐的"派发中但未转负"）
    if bigmid_delta > SELL_NO_DROP_MIN_OUTFLOW or bigmid_now >= 0:
        return 0, []

    prices = db_get_recent_prices(conn, SELL_NO_DROP_WINDOW * 4)
    if len(prices) < 10 or prices[0] <= 0:
        return 0, []
    price_pct = (prices[-1] - prices[0]) / prices[0] * 100
    if price_pct < SELL_NO_DROP_PRICE_FLOOR:
        return 0, []  # ③价格跟跌 → 不是"卖而不跌"，是真派发

    window_low = min(prices)
    if window_low <= 0 or current_price <= window_low:
        return 0, []  # ④破位：卖压已兑现 → 真派发，放行做空

    pts = SELL_NO_DROP_SCORE
    msg = (
        f"⚠ 卖而不跌：近 {SELL_NO_DROP_WINDOW} 轮大单+中单净流出 "
        f"{bigmid_delta/10000:+,.1f} 万但价格 {price_pct:+.2f}%（守 {window_low:.2f} 未破）"
        f"——被动吸筹/诱空嫌疑，逼空风险 +{pts} 分"
    )
    db_save_signal(conn, "SELL_NO_DROP", msg, pts)
    log.warning(f"[卖而不跌] {msg}")
    return pts, [msg]


# ═══════════════════════════════════════════════════════════
# 五-D3、L2 逐笔冰山检测（执行级被动吸筹 / 真出货裁决）
# ═══════════════════════════════════════════════════════════
def analyze_iceberg_absorption(
    conn: sqlite3.Connection,
) -> tuple[int, int, list[str]]:
    """
    用逐笔成交（execution）而非挂单报价（quote）分辨被动吸筹 vs 真出货——报价层可幌单，
    成交是真金白银吃出来的，难造假（[[capital-flow-tiers-intent]] aggressor 一节）。

    - **买侧冰山吸筹**：主动卖主导（砸买盘）但价格不跌，且买一被吃量 ≫ 显示量（补单）
      → 有大资金挂被动买单接货 → 抬升逼空风险、抑制顺势追空。
    - **卖侧冰山派发**：主动买主导（吃卖盘）但价格滞涨，且卖一被吃量 ≫ 显示量（补单）
      → 主力挂被动卖单出货 → 支撑做空。

    返回 (squeeze_pts, support_pts, signals)：二者按价格方向互斥，单次最多一个非零。
    """
    rows = db_get_recent_ticks(conn, ICEBERG_WINDOW)
    if len(rows) < ICEBERG_WINDOW:
        return 0, 0, []

    # rows[0] 最新、rows[-1] 最旧
    sell_tot = sum(r[0] for r in rows)
    buy_tot  = sum(r[1] for r in rows)
    price_first = rows[-1][2]
    price_last  = rows[0][3]
    if price_first <= 0:
        return 0, 0, []
    price_pct = (price_last - price_first) / price_first * 100
    best_bid_vol = rows[0][4]
    best_ask_vol = rows[0][5]

    # 门槛改用成交额（notional）：主导方股数 × 最新成交价，跨价位可比（薄盘高价股不再失明）
    sell_notional = sell_tot * price_last
    buy_notional  = buy_tot * price_last

    # 买侧冰山吸筹：主动卖主导 + 价格未跟跌 + 买一被吃量远超显示量
    if (sell_notional >= ICEBERG_MIN_NOTIONAL and sell_tot >= buy_tot * ICEBERG_DOMINANCE
            and price_pct >= ICEBERG_PRICE_FLOOR):
        refill = sell_tot / best_bid_vol if best_bid_vol > 0 else float("inf")
        if refill >= ICEBERG_REFILL_MULT:
            pts, tag = ICEBERG_ACCUM_STRONG_SCORE, f"买一补单 {refill:.1f}×"
        else:
            pts, tag = ICEBERG_ACCUM_WEAK_SCORE, "无明显补单"
        msg = (f"⚠ L2买侧冰山吸筹：主动卖 {sell_tot:,.0f} 股(>买 {buy_tot:,.0f}) "
               f"但价 {price_pct:+.2f}%（{tag}）→ 被动吸筹，逼空风险 +{pts} 分")
        db_save_signal(conn, "ICEBERG_ACCUMULATION", msg, pts)
        log.warning(f"[L2冰山吸筹] {msg}")
        return pts, 0, [msg]

    # 卖侧冰山派发：主动买主导 + 价格滞涨 + 卖一被吃量远超显示量
    if (buy_notional >= ICEBERG_MIN_NOTIONAL and buy_tot >= sell_tot * ICEBERG_DOMINANCE
            and price_pct <= ICEBERG_PRICE_CAP):
        refill = buy_tot / best_ask_vol if best_ask_vol > 0 else float("inf")
        if refill >= ICEBERG_REFILL_MULT:
            pts, tag = ICEBERG_STRONG_SCORE, f"卖一补单 {refill:.1f}×"
        else:
            pts, tag = ICEBERG_WEAK_SCORE, "无明显补单"
        msg = (f"⚠ L2卖侧冰山派发：主动买 {buy_tot:,.0f} 股(>卖 {sell_tot:,.0f}) "
               f"但价 {price_pct:+.2f}%（{tag}）→ 被动派发 [支撑做空+{pts}分]")
        db_save_signal(conn, "ICEBERG_DISTRIBUTION", msg, pts)
        log.warning(f"[L2冰山派发] {msg}")
        return 0, pts, [msg]

    return 0, 0, []


# ═══════════════════════════════════════════════════════════
# 五-D4、L2 经纪队列足迹（Tier 2，单一席位反复占据最优档 = 机构被动足迹）
# ═══════════════════════════════════════════════════════════
def analyze_broker_footprint(
    conn: sqlite3.Connection, ice_squeeze_pts: int, ice_support_pts: int,
) -> tuple[int, int, list[str]]:
    """
    Tier 2 经纪队列足迹：同一机构持续单边压最优档 = 机构被动挂单的足迹。

    **报价层可幌单，故经纪足迹绝不单独加分**——仅当 Tier-1 冰山（执行级成交证据）
    同向已触发时，作为交叉验证加成抬升置信度（[[capital-flow-tiers-intent]] L2 一节）。
    若本轮无冰山，直接返回 (0,0,[])，避免假接货墙凭报价骗出诱多/诱空分。

    判断依据 v2（按机构名 + 最优档 + 净不对称，见 BROKER_FOOTPRINT_* 常量注释）：
      - 买侧：同一机构在窗口内 ≥MIN_ROUNDS 轮是买侧净不对称最强者，且其净值均值 ≥MIN_NET
        + Tier-1 买侧冰山吸筹 → 加成逼空风险（抑制追空）。
      - 卖侧：对称——同机构持续单边压卖盘 + Tier-1 卖侧冰山派发 → 加成支撑做空。

    返回 (squeeze_bonus, support_bonus, signals)；非冰山轮恒为 (0,0,[])。
    """
    if ice_squeeze_pts <= 0 and ice_support_pts <= 0:
        return 0, 0, []   # 无执行级冰山证据 → 经纪足迹不单独计分（反幌单铁律）

    rows = db_get_recent_brokers(conn, BROKER_FOOTPRINT_WINDOW)
    if len(rows) < BROKER_FOOTPRINT_WINDOW:
        return 0, 0, []

    def _persistent(name_net_pairs: list) -> tuple[Optional[str], int, float]:
        """找窗口内「领先轮数最多」的机构，返回 (name, 领先轮数, 这些轮净值均值)。
        仅统计净不对称 ≥ MIN_NET 的轮次——做市商/噪音轮（净≈0）不计入领先。"""
        rounds: dict = {}
        nets: dict = {}
        for nm, net in name_net_pairs:
            if nm is None or net is None or net < BROKER_FOOTPRINT_MIN_NET:
                continue
            rounds[nm] = rounds.get(nm, 0) + 1
            nets.setdefault(nm, []).append(net)
        best, best_cnt = None, 0
        for nm, c in rounds.items():
            if c > best_cnt:
                best, best_cnt = nm, c
        avg = sum(nets[best]) / len(nets[best]) if best else 0.0
        return best, best_cnt, avg

    # 买侧吸筹加成：仅当 Tier-1 买侧冰山已触发
    if ice_squeeze_pts > 0:
        name, cnt, avg = _persistent([(r[0], r[1]) for r in rows])
        if name is not None and cnt >= BROKER_FOOTPRINT_MIN_ROUNDS:
            pts = BROKER_FOOTPRINT_BONUS
            msg = (f"⚠ L2经纪足迹：{name} 近 {BROKER_FOOTPRINT_WINDOW} 轮 {cnt} 轮单边压买盘"
                   f"（净均 +{avg:.1f} 席）+ 冰山吸筹同向 → 机构被动吸筹足迹，逼空风险 +{pts} 分")
            db_save_signal(conn, "BROKER_FOOTPRINT_BID", msg, pts)
            log.warning(f"[L2经纪足迹·买] {msg}")
            return pts, 0, [msg]

    # 卖侧派发加成：仅当 Tier-1 卖侧冰山已触发
    if ice_support_pts > 0:
        name, cnt, avg = _persistent([(r[2], r[3]) for r in rows])
        if name is not None and cnt >= BROKER_FOOTPRINT_MIN_ROUNDS:
            pts = BROKER_FOOTPRINT_BONUS
            msg = (f"⚠ L2经纪足迹：{name} 近 {BROKER_FOOTPRINT_WINDOW} 轮 {cnt} 轮单边压卖盘"
                   f"（净均 +{avg:.1f} 席）+ 冰山派发同向 → 机构被动派发足迹 [支撑做空+{pts}分]")
            db_save_signal(conn, "BROKER_FOOTPRINT_ASK", msg, pts)
            log.warning(f"[L2经纪足迹·卖] {msg}")
            return 0, pts, [msg]

    return 0, 0, []


# ═══════════════════════════════════════════════════════════
# 五-D2、主力嫌疑分（控盘特征筛查，纯背景画像）
# ═══════════════════════════════════════════════════════════
def _broker_is_institutional(name: Optional[str]) -> bool:
    """席位名是否属外资/机构大行（按关键词子串）。供"机构连续压一侧"判定。"""
    if not name:
        return False
    return any(k in name for k in INSTITUTIONAL_BROKER_KEYWORDS)


def _broker_is_retail(name: Optional[str]) -> bool:
    """席位名是否属零售聚合商（富途/老虎…）。该侧最优档由散户散单汇集而成。"""
    if not name:
        return False
    return any(k in name for k in RETAIL_BROKER_KEYWORDS)


def read_seat_structure(state: "MonitorState") -> Optional[str]:
    """席位结构即时读（迭代四十，纯展示）：把最优档席位拆成 主力(机构大行) × 散户(零售
    聚合商) 两条方向线。机构压哪侧=主力意图，散户挂哪侧=散户在接盘还是出货。

    返回 `主力 机构压卖一N席(派发) | 散户 挂买M席(接盘)` 之类，None=无 L2 经纪数据。
    """
    brk = state.latest_broker or {}
    bi, br = brk.get("bid_inst", 0) or 0, brk.get("bid_retail", 0) or 0
    ai, ar = brk.get("ask_inst", 0) or 0, brk.get("ask_retail", 0) or 0
    if not (bi or br or ai or ar):
        return None
    # 主力（机构大行）方向：压哪侧
    if ai > bi:
        mf = f"机构压卖{ai}"
    elif bi > ai:
        mf = f"机构压买{bi}"
    elif bi or ai:
        mf = f"机构均衡{bi}/{ai}"
    else:
        mf = "机构无"
    # 散户（零售聚合商）方向：挂哪侧
    if br > ar:
        rt = f"散户买{br}"
    elif ar > br:
        rt = f"散户卖{ar}"
    elif br or ar:
        rt = f"散户{br}/{ar}"
    else:
        rt = "散户无"
    # 方向裁决（机构压卖+散户买=派发对峙；机构压买+散户卖=吸筹对峙）
    verdict = ""
    if ai > bi and br > ar:
        verdict = " →派发对峙"
    elif bi > ai and ar > br:
        verdict = " →吸筹对峙"
    return f"{mf} · {rt}{verdict}"


def detect_distribution_standoff(
    conn: sqlite3.Connection, state: "MonitorState",
) -> Optional[str]:
    """派发对峙·散户接盘检测（迭代三十九，纯信息性，不计分）。

    四件套同时成立 → 报价层(盘口偏多)被成交层(机构派发)证伪：
      ① 机构压卖一（主力嫌疑席位维度 dom_side=='卖一'，已机构化过滤）
      ② 盘口偏多（近3轮失衡度中位 ≥ STANDOFF_IMB_MIN，散户挂买接盘）
      ③ 大单持续净流出（近3轮 big_net 全负，主力主动派发）
      ④ 散户占买一（富途/老虎等零售聚合商在买一，可选增强）

    需 L2 经纪席位（dom_side 来自 broker_queue）；LV1 无 broker 时自然不触发。
    返回提示文案，或 None。
    """
    # ① 机构压卖一（缺 L2 broker 时 dom_side 为空 → 直接退出）
    if state.latest_mf_tags.get("dom_side") != "卖一":
        return None
    # ② 盘口偏多
    imb_rows = conn.execute(
        "SELECT imbalance FROM orderbook_snapshots ORDER BY id DESC LIMIT 3"
    ).fetchall()
    if len(imb_rows) < 3:
        return None
    imb_med = statistics.median(r[0] for r in imb_rows)
    if imb_med < STANDOFF_IMB_MIN:
        return None
    # ③ 大单持续净流出（近3轮全负）
    big_nets = db_get_recent_big_net(conn, 3)
    if not (len(big_nets) >= 3 and all(v < 0 for v in big_nets)):
        return None
    # ④ 散户占买一（迭代四十：用稳健的零售席位计数，不再依赖单一净不对称席位——
    #   机构净值常盖过散户席位导致 bid_top_name 不是散户、漏判）
    brk = state.latest_broker or {}
    bid_retail = brk.get("bid_retail", 0) or 0
    who = f"散户挂买一{bid_retail}席接盘" if bid_retail >= 1 else "厚买盘接盘"
    return (f"派发对峙：盘口偏多(失衡+{imb_med:.2f}) + 大单净流出 + 机构压卖一，"
            f"{who} → 盘口'多'是派发燃料非利好；勿在此接货，"
            f"待钉价失守/失衡转负再确认下行")


def analyze_main_force_control(
    conn: sqlite3.Connection,
) -> tuple[int, str, list[str], dict]:
    """
    控盘嫌疑度评分（0-100）——筛出主力控盘的标的，纯背景画像 +（联动做空门）。

    三个子维度（来自仪表盘已有数据，无需新订阅）：
      ① 价格钉扎 (0-50)：窗口内价格被夹在极窄带，或反复精确停在同一价。
         散户做不到把价格焊在某点，长时间钉扎 = 控盘方在按价（00100 钉 601.00）。
      ② 席位集中 (0-35)：某侧最优档被【外资/机构大行】持续主导（同侧统计、防清算行；
         零售聚合商如富途占一侧只是散单汇集，非控盘，故按机构名单过滤）。两法取强：
         单一机构独占 / 多家机构轮换压同侧（富瑞·摩根·巴克莱"轮换大行"压卖）。
         压卖一=派发·压价，压买一=吸筹·护盘（方向读）。
      ③ 盘薄 (0-15)：卖盘深度中位很小 → 小资金即可拨动 → 易控。

    返回 (score, label, signals, tags)。tags={pin,seat,thin,dom_side,dom_kind} 供
    做空安全门（强控盘+钉扎降级 ENTRY）与仪表盘方向读使用。
    """
    prices  = db_get_recent_prices(conn, MF_WINDOW)
    brokers = db_get_recent_brokers(conn, MF_WINDOW)
    depths  = db_get_recent_ask_depth(conn, MF_WINDOW)
    sigs: list[str] = []

    # ── ① 价格钉扎 ────────────────────────────────────────────
    pin_score = 0
    if len(prices) >= MF_MIN_ROUNDS:
        med = statistics.median(prices)
        if med > 0:
            band_pct = (max(prices) - min(prices)) / med * 100
            # 窄带分：band 0% → 50，band ≥ MF_PIN_BAND_PCT → 0，线性
            band_score = max(0.0, 1 - band_pct / MF_PIN_BAND_PCT) * 50
            # 精确同价分：最高频价格的占比，≥ MF_PIN_MODE_RATIO 起，满占比 → 50
            mode_cnt = max(prices.count(p) for p in set(prices))
            mode_ratio = mode_cnt / len(prices)
            mode_score = 0.0
            if mode_ratio >= MF_PIN_MODE_RATIO:
                mode_score = (mode_ratio - MF_PIN_MODE_RATIO) / (1 - MF_PIN_MODE_RATIO) * 50
            pin_score = int(round(min(max(band_score, mode_score), 50)))
            if pin_score >= 12:
                detail = (f"价格钉扎：近 {len(prices)} 轮带宽 {band_pct:.2f}%"
                          f"，最高频价 {statistics.mode(prices):.3g} 占 {mode_ratio*100:.0f}% [+{pin_score}]")
                sigs.append(detail)

    # ── ② 席位集中（某侧被【机构大行】持续主导）──────────────────────
    # 按买/卖侧分别统计（清算行如荷银两侧来回切，分侧后自动滤除）。仅计机构大行——
    # 零售聚合商（富途/老虎…）占一侧是散单汇集而非控盘，按机构名单过滤掉。两法取强：
    #   single — 单一机构独占某侧（00100 中国国际金融 8/8 钉买一）
    #   inst   — 多家机构轮换压同侧（富瑞·摩根·巴克莱轮流压卖，单名 <50% 但机构合计高）
    seat_score = 0
    seat_side = seat_kind = None
    seat_desc = ""
    if len(brokers) >= MF_MIN_ROUNDS:
        n = len(brokers)
        sides: dict = {"买一": [], "卖一": []}
        for bid_nm, bid_net, ask_nm, ask_net in brokers:
            if bid_nm and (bid_net or 0) >= MF_SEAT_MIN_NET:
                sides["买一"].append(bid_nm)
            if ask_nm and (ask_net or 0) >= MF_SEAT_MIN_NET:
                sides["卖一"].append(ask_nm)
        for side, names in sides.items():
            inst_names = [nm for nm in names if _broker_is_institutional(nm)]
            if not inst_names:
                continue
            inst_r = len(inst_names) / n          # 该侧被机构大行主导的轮数占比
            cnt: dict = {}                        # 机构内部单名计数
            for nm in inst_names:
                cnt[nm] = cnt.get(nm, 0) + 1
            top_name = max(cnt, key=cnt.get)
            single_r = cnt[top_name] / n
            cands = []  # (score, kind, desc)
            if single_r >= MF_SEAT_DOM_RATIO:
                cands.append((min((single_r - MF_SEAT_DOM_RATIO) / (0.8 - MF_SEAT_DOM_RATIO), 1.0) * 35,
                              "single", f"{top_name[:6]} 独占{side} {cnt[top_name]}/{n}（{single_r*100:.0f}%）"))
            if inst_r >= MF_INST_SIDE_RATIO:
                cands.append((min((inst_r - MF_INST_SIDE_RATIO) / (0.95 - MF_INST_SIDE_RATIO), 1.0) * 32,
                              "inst", f"外资/机构大行连续压{side} {inst_r*100:.0f}%"))
            for sc, kind, desc in cands:
                sc = int(round(sc))
                if sc > seat_score:
                    seat_score, seat_side, seat_kind, seat_desc = sc, side, kind, desc
        if seat_score > 0:
            flow = "压卖→派发/压价" if seat_side == "卖一" else "压买→吸筹/护盘"
            sigs.append(f"席位集中：{seat_desc} → {flow} [+{seat_score}]")

    # ── ③ 盘薄 ────────────────────────────────────────────────
    thin_score = 0
    if len(depths) >= MF_MIN_ROUNDS:
        med_depth = statistics.median(depths)
        if 0 < med_depth <= MF_THIN_DEPTH_SHARES:
            # med ≤ 半阈值 → 满分 15；半阈值~阈值 → 线性 15→0
            half = MF_THIN_DEPTH_SHARES / 2
            if med_depth <= half:
                thin_score = 15
            else:
                thin_score = int(round((MF_THIN_DEPTH_SHARES - med_depth) / half * 15))
            if thin_score > 0:
                sigs.append(f"盘薄：卖盘深度中位 {med_depth:,.0f} 股（≤{MF_THIN_DEPTH_SHARES:,}）易控 [+{thin_score}]")

    score = min(pin_score + seat_score + thin_score, 100)
    if score >= 60:
        label = "强控盘嫌疑"
    elif score >= 35:
        label = "疑似控盘"
    elif score >= 20:
        label = "轻微控盘特征"
    else:
        label = "无明显控盘(市场化)"
    tags = {"pin": pin_score, "seat": seat_score, "thin": thin_score,
            "dom_side": seat_side, "dom_kind": seat_kind}
    return score, label, sigs, tags


# ═══════════════════════════════════════════════════════════
# 五-E、散户 FOMO 警报（窄幅震荡中散户加速流入）
# ═══════════════════════════════════════════════════════════
def analyze_retail_fomo(
    conn: sqlite3.Connection, current_price: Optional[float],
) -> tuple[int, list[str]]:
    """
    价格窄幅震荡（< 0.5%）期间 small_net 加速流入 → 散户在高位 FOMO 接盘。
    与"散户撤退"对称：FOMO 是撤退的前奏，提前预警。

    返回正值 = 支撑做空分（接盘前兆，顶部可能形成中）。
    """
    if current_price is None:
        return 0, []

    rows = db_get_recent_capital_structure(conn, RETAIL_FOMO_WINDOW)
    if len(rows) < RETAIL_FOMO_WINDOW:
        return 0, []

    prices = db_get_recent_prices(conn, RETAIL_FOMO_WINDOW * 4)
    if len(prices) < 10:
        return 0, []
    price_max = max(prices)
    price_min = min(prices)
    if price_min <= 0:
        return 0, []
    price_range_pct = (price_max - price_min) / price_min * 100
    if price_range_pct >= RETAIL_FOMO_PRICE_RANGE_PCT:
        return 0, []

    small_net_delta = rows[0][2] - rows[-1][2]
    if small_net_delta < RETAIL_FOMO_MIN_INFLOW:
        return 0, []

    pts = 8
    msg = (
        f"⚠ 散户 FOMO：价格 {price_range_pct:.2f}% 窄幅震荡，"
        f"近 {RETAIL_FOMO_WINDOW} 轮散户净流入 {small_net_delta/10000:+,.1f} 万 "
        f"[支撑做空+{pts}分]"
    )
    db_save_signal(conn, "RETAIL_FOMO", msg, pts)
    return pts, [msg]


# ═══════════════════════════════════════════════════════════
# 五-F、中单拆单方差检测（大单冻结期间 mid_net 节奏稳定单向）
# ═══════════════════════════════════════════════════════════
def analyze_mid_split(
    conn: sqlite3.Connection, big_net_stale: bool,
) -> tuple[int, list[str]]:
    """
    大单冻结期间，若中单逐 tick 变化呈现"稳定单向 + 低变异系数"，则是
    程序化拆单的典型节奏（散户做不到这种规律性）。

    变异系数（CV）= 标准差 / 均值绝对值。CV 小 = 节奏稳定。

    返回正值 = 支撑做空分（拆单出货）。
    """
    if not big_net_stale:
        return 0, []

    rows = db_get_recent_capital_structure(conn, MID_SPLIT_WINDOW + 1)
    if len(rows) < MID_SPLIT_WINDOW + 1:
        return 0, []

    # 计算 mid_net 逐轮 Δ
    mid_series = [r[1] for r in rows]
    deltas = [mid_series[i] - mid_series[i + 1] for i in range(MID_SPLIT_WINDOW)]

    if not all(d < 0 for d in deltas) and not all(d > 0 for d in deltas):
        return 0, []

    abs_deltas = [abs(d) for d in deltas]
    median_abs = statistics.median(abs_deltas)
    if median_abs < MID_SPLIT_MIN_FLOW:
        return 0, []

    mean_abs = statistics.mean(abs_deltas)
    if mean_abs == 0:
        return 0, []
    cv = statistics.pstdev(abs_deltas) / mean_abs
    if cv >= MID_SPLIT_CV_THRESHOLD:
        return 0, []

    direction = "净流出" if deltas[0] < 0 else "净流入"
    pts = 10
    msg = (
        f"⚠ 中单拆单痕迹：大单冻结期间中单连续 {MID_SPLIT_WINDOW} 轮{direction}"
        f"（均额 {mean_abs/10000:+,.1f} 万, CV {cv:.2f}）[支撑做空+{pts}分]"
    )
    db_save_signal(conn, "MID_SPLIT_DETECTED", msg, pts)
    return pts, [msg]


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

    # 最优档显示量（供 L2 冰山检测对比"被吃量 vs 显示量"判补单）
    best_bid_vol = float(bid_list[0][1]) if bid_list else 0.0
    best_ask_vol = float(ask_list[0][1]) if ask_list else 0.0
    # 十档形态（迭代三十八 #1）：前3档量 → 判"浅档堆叠 vs 深档分布"。逐档量也回传给
    # 仪表盘做即时形态展示（不入库，避免每 15s 存 20 个数）。
    bid_top3 = sum(float(item[1]) for item in bid_list[:3])
    ask_top3 = sum(float(item[1]) for item in ask_list[:3])
    bid_vols = [float(item[1]) for item in bid_list]
    ask_vols = [float(item[1]) for item in ask_list]

    ts = datetime.datetime.now().isoformat(timespec="seconds")
    db_save_orderbook(conn, ts, bid_depth, ask_depth, imbalance,
                      best_bid_vol, best_ask_vol, bid_top3, ask_top3)

    return {"ts": ts, "bid_depth": bid_depth,
            "ask_depth": ask_depth, "imbalance": imbalance,
            "best_bid_vol": best_bid_vol, "best_ask_vol": best_ask_vol,
            "bid_top3": bid_top3, "ask_top3": ask_top3,
            "bid_vols": bid_vols, "ask_vols": ask_vols}


def fetch_ticks(ctx: OpenQuoteContext, conn: sqlite3.Connection,
                ob: Optional[dict]) -> Optional[dict]:
    """
    拉取逐笔成交（需 SubType.TICKER 订阅），按 sequence 去重只统计上次轮询后的新成交，
    聚合本窗口主动买/卖量并结合最优档显示量存入 tick_flow，供 analyze_iceberg_absorption
    做执行级冰山判定。

    Aggressor 语义（Futu ticker_direction）：BUY=主动买（打卖盘）、SELL=主动卖（打买盘）。
    逐笔是增强信号，失败仅返回 None、不阻断核心打分。Futu 单次上限 1000 笔，
    极活跃标的两次轮询间成交 > 1000 会少计（不影响方向判定，只是低估量级）。
    """
    try:
        ret, data = ctx.get_rt_ticker(SYMBOL, num=ICEBERG_TICK_FETCH_NUM)
    except Exception as e:                      # 未订阅/网络异常等，静默跳过
        log.debug(f"get_rt_ticker 异常: {e}")
        return None
    if ret != RET_OK or data is None or getattr(data, "empty", True):
        return None
    if "sequence" not in data.columns or "ticker_direction" not in data.columns:
        return None

    data = data.sort_values("sequence")
    last_seq = getattr(fetch_ticks, "_last_seq", None)
    if last_seq is None:
        # 首拉（含进程重启）：get_rt_ticker 一次性返回历史积压（至多 1000 笔），若直接
        # 聚合成一个窗口行，会把"主动买/卖量"灌虚、污染之后 ICEBERG_WINDOW 轮的冰山求和
        # （实盘 2026-06-04 重启后冰山补单倍数虚高至 55×）。故首拉仅用于建立去重基线
        # _last_seq，不写 tick_flow、不计入窗口；下一轮起只统计真正的新成交。
        fetch_ticks._last_seq = int(data["sequence"].max())
        return None
    data = data[data["sequence"] > last_seq]
    if data.empty:
        return None
    fetch_ticks._last_seq = int(data["sequence"].max())

    dir_col = data["ticker_direction"].astype(str).str.upper()
    vol = data["volume"].astype(float)
    is_sell = dir_col.str.contains("SELL")
    is_buy  = dir_col.str.contains("BUY")
    sell_vol = float(vol[is_sell].sum())
    buy_vol  = float(vol[is_buy].sum())

    # 主动大单识别（迭代三十八 #2，迭代四十二改 notional）：单笔成交额 ≥ LARGE_TICK_NOTIONAL
    # 视为大单扫盘——执行级主力足迹，与一堆散单求和区分开（同样的窗口总量，一笔大单 vs 百笔
    # 散单含义相反）。门槛按金额而非股数，跨价位可比（price 缺失→NaN→不计大单，保守）。
    tick_notional = vol * data["price"].astype(float)
    is_large = tick_notional >= LARGE_TICK_NOTIONAL
    large_buy_vol  = float(vol[is_buy  & is_large].sum())
    large_sell_vol = float(vol[is_sell & is_large].sum())
    large_buy_cnt  = int((is_buy  & is_large).sum())
    large_sell_cnt = int((is_sell & is_large).sum())

    prices = [float(p) for p in data["price"].tolist()
              if p is not None and float(p) > 0]
    if not prices:
        return None
    price_first, price_last = prices[0], prices[-1]
    best_bid_vol = float(ob.get("best_bid_vol", 0.0)) if ob else 0.0
    best_ask_vol = float(ob.get("best_ask_vol", 0.0)) if ob else 0.0

    ts = datetime.datetime.now().isoformat(timespec="seconds")
    db_save_tick(conn, ts, sell_vol, buy_vol,
                 price_first, price_last, best_bid_vol, best_ask_vol,
                 large_buy_vol, large_sell_vol, large_buy_cnt, large_sell_cnt)
    return {"ts": ts, "sell_vol": sell_vol, "buy_vol": buy_vol,
            "best_bid_vol": best_bid_vol, "best_ask_vol": best_ask_vol,
            "large_buy_vol": large_buy_vol, "large_sell_vol": large_sell_vol,
            "large_buy_cnt": large_buy_cnt, "large_sell_cnt": large_sell_cnt}


def fetch_broker_queue(ctx: OpenQuoteContext, conn: sqlite3.Connection
                       ) -> Optional[dict]:
    """
    拉取经纪队列（需 SubType.BROKER 订阅），按机构名聚合最优档席位并算买卖净不对称，供
    analyze_broker_footprint 检测单一机构是否持续单边压盘（被动吸筹/派发足迹）。

    Futu get_broker_queue 返回 (ret, bid_frame, ask_frame)，列含 *_broker_name/_broker_pos。
    判断依据 v2（见 BROKER_FOOTPRINT_* 常量注释）：
      - 按 broker_name 聚合（非 id，同机构常占多 ID）；
      - 只看最优档 broker_pos==1 的席位数；
      - 净不对称 = 该机构本侧席位数 − 其对侧席位数（滤掉两侧都挂的做市商）。
    每侧取净不对称最大的机构及其净值入库。报价层增强信号，失败仅返回 None、不阻断打分。
    """
    try:
        ret, bid_frame, ask_frame = ctx.get_broker_queue(SYMBOL)
    except Exception as e:                      # 未订阅/网络异常/返回元数不符等
        log.debug(f"get_broker_queue 异常: {e}")
        return None
    if ret != RET_OK:
        return None

    def _best_counts(frame, name_col: str, pos_col: str) -> dict:
        """最优档(pos==min)各机构名的席位数 {name: count}。"""
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
        return None

    # 每侧取「本侧席位数 − 对侧席位数」最大的机构（净不对称 = 单边方向性，做市商两侧均衡→净≈0）
    def _top_net(side_cnt: dict, other_cnt: dict) -> tuple[Optional[str], int]:
        best_name, best_net = None, 0
        for nm, c in side_cnt.items():
            net = c - other_cnt.get(nm, 0)
            if net > best_net:
                best_name, best_net = nm, net
        return best_name, best_net

    bid_top_name, bid_top_net = _top_net(bid_cnt, ask_cnt)
    ask_top_name, ask_top_net = _top_net(ask_cnt, bid_cnt)

    # 席位分类（迭代四十）：把最优档席位分成 机构大行(主力方向) / 零售聚合商(散户方向)。
    # 原代码只取单一净不对称最强席位，散户侧（富途等）常被机构净值盖过而丢失——这里独立
    # 统计两类席位数，得到稳健的"谁在买一/卖一"主力×散户画像。
    def _classify(cnt: dict) -> tuple[int, int]:
        inst   = sum(c for nm, c in cnt.items() if _broker_is_institutional(nm))
        retail = sum(c for nm, c in cnt.items() if _broker_is_retail(nm))
        return inst, retail

    bid_inst, bid_retail = _classify(bid_cnt)
    ask_inst, ask_retail = _classify(ask_cnt)

    ts = datetime.datetime.now().isoformat(timespec="seconds")
    db_save_broker(conn, ts, bid_top_name, bid_top_net, ask_top_name, ask_top_net,
                   bid_inst, bid_retail, ask_inst, ask_retail)
    return {"ts": ts, "bid_top_name": bid_top_name, "bid_top_net": bid_top_net,
            "ask_top_name": ask_top_name, "ask_top_net": ask_top_net,
            "bid_inst": bid_inst, "bid_retail": bid_retail,
            "ask_inst": ask_inst, "ask_retail": ask_retail}


def analyze_order_book(conn: sqlite3.Connection,
                        current_ask: float) -> tuple[int, list[str]]:
    """
    卖盘深度骤减信号：
    - 当前 ask_depth 较近期基准（中位数）下降超过 ASK_DEPTH_SHRINK_PCT% → 空头回补/做空意愿减弱
    - 买盘深度 > 卖盘深度（正失衡）→ 多头主动接盘

    基准用中位数（而非均值）以避免连续低值样本把均值拉下、形成自喂养警报。
    日志/dashboard signal 受 ASK_DEPTH_LOG_COOLDOWN_SECS 节流；评分仍每轮计算
    （评分代表当前状态，不应被冷却抑制）。
    """
    history = db_get_recent_ask_depth(conn, ASK_DEPTH_WINDOW)
    score = 0
    signals: list[str] = []

    # 用近 K 轮中位数代替单点采样，避免稀薄盘口的挂/撤单噪音被放大成 +25 分信号
    if len(history) >= ASK_DEPTH_SMOOTH_K + 4 and current_ask > 0:
        smoothed_current = statistics.median(history[:ASK_DEPTH_SMOOTH_K])
        baseline = statistics.median(history[ASK_DEPTH_SMOOTH_K:])
        if baseline > 0 and smoothed_current > 0:
            shrink_pct = (baseline - smoothed_current) / baseline * 100
            now_ts = time.time()
            last_log_ts = getattr(analyze_order_book, "_last_log_ts", 0.0)
            should_log = (now_ts - last_log_ts) >= ASK_DEPTH_LOG_COOLDOWN_SECS
            if shrink_pct >= ASK_DEPTH_SHRINK_PCT:
                pts = ASK_DEPTH_SHRINK_PTS
                msg = (f"卖盘深度骤减 {shrink_pct:.1f}% "
                       f"(近{ASK_DEPTH_SMOOTH_K}轮中位 {smoothed_current:,.0f} "
                       f"vs 基准 {baseline:,.0f} 股) [+{pts}分]")
                score += pts
                signals.append(msg)
                if should_log:
                    log.warning(f"[摆盘预警] {msg}")
                    analyze_order_book._last_log_ts = now_ts
                db_save_signal(conn, "ASK_DEPTH_SHRINK", msg, pts)
            elif shrink_pct >= ASK_DEPTH_SHRINK_PCT * 0.6:
                pts = ASK_DEPTH_SHRINK_PTS_WEAK
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


def describe_book_shape(ob: Optional[dict]) -> Optional[str]:
    """十档形态即时画像（迭代三十八 #1，纯展示/信息性，不改数值评分）。

    用逐档分布区分：卖墙集中在卖一(压价/诱空嫌疑) vs 浅档堆叠 vs 深档均匀(真实供给)。
    返回紧凑文案如 `卖墙卖一62%·深档薄 | 买盘均匀`，None 表示无数据。
    """
    if not ob:
        return None

    def _side(depth: float, best: float, top3: float, wall_tag: str,
              even_tag: str) -> Optional[str]:
        if not depth or depth <= 0:
            return None
        wall = best / depth
        top3r = top3 / depth
        if wall >= BOOK_WALL_CONC_HIGH:
            return f"{wall_tag}{wall*100:.0f}%"
        if top3r >= BOOK_TOP3_CONC_HIGH:
            return f"{even_tag}浅档{top3r*100:.0f}%"
        return f"{even_tag}匀"

    ask = _side(ob.get("ask_depth", 0) or 0, ob.get("best_ask_vol", 0) or 0,
                ob.get("ask_top3", 0) or 0, "卖墙卖一", "卖")
    bid = _side(ob.get("bid_depth", 0) or 0, ob.get("best_bid_vol", 0) or 0,
                ob.get("bid_top3", 0) or 0, "买墙买一", "买")
    parts = [p for p in (ask, bid) if p]
    return " | ".join(parts) if parts else None


# ═══════════════════════════════════════════════════════════
# 六-B、信号  价格反弹逼空维度
# ═══════════════════════════════════════════════════════════
def analyze_price_reversal(
    conn: sqlite3.Connection,
    current_price: Optional[float],
) -> tuple[int, list[str]]:
    """
    价格反弹逼空维度：当前价相对近 PRICE_REVERSAL_WINDOW 轮低点反弹幅度越大，
    说明短期已转入逆向走势，做空成本上升 → 计入逼空分。

    采用滚动窗口低点而非 session_low，让信号在反弹企稳后自然衰减，
    避免日内一次低点把后续整天都标为"高风险"。
    """
    if current_price is None:
        return (0, [])
    prices = db_get_recent_prices(conn, PRICE_REVERSAL_WINDOW)
    if len(prices) < 3:
        return (0, [])
    low = min(prices)
    if low <= 0:
        return (0, [])
    rebound_pct = (current_price - low) / low * 100
    if rebound_pct < PRICE_REVERSAL_PCT_LIGHT:
        return (0, [])

    if rebound_pct >= PRICE_REVERSAL_PCT_HEAVY:
        pts = PRICE_REVERSAL_SCORE_HEAVY
    elif rebound_pct >= PRICE_REVERSAL_PCT_MED:
        pts = PRICE_REVERSAL_SCORE_MED
    else:
        pts = PRICE_REVERSAL_SCORE_LIGHT

    msg = (f"价格自近 {PRICE_REVERSAL_WINDOW} 轮低点 {low:.2f} 反弹 "
           f"{rebound_pct:+.2f}%（当前 {current_price:.2f}）[+{pts}分]")
    return (pts, [msg])


# ═══════════════════════════════════════════════════════════
# 七、信号④  卖空占比趋势（N 日连涨后拐头）
# ═══════════════════════════════════════════════════════════
def analyze_short_ratio_trend(
    conn: sqlite3.Connection,
    state: Optional["MonitorState"] = None,
) -> tuple[int, int, list[str]]:
    """
    卖空占比趋势分析。

    返回: (squeeze_score, short_support, signals)
    - squeeze_score 进入逼空评分
    - short_support 进入做空入场支撑分

    评分规则：
    - 连续 SHORT_RATIO_RISE_MIN 天上升后出现下降 → 通常是空头回补、逼空启动
      信号；但若同时段日内价格未上涨（首尾对比 last <= first），说明空头已
      清算完毕、行情进入下跌——逼空假设被价格行为否定，此时 +25 转入做空
      支撑而非逼空风险。
    - 占比处于历史高位（>25%）→ 做空拥挤，逼空风险累积
    - 占比极端高位（>35%）→ 强加分

    Bug 13 路径锁：高位拐头分流（squeeze vs support）依赖 SHORT_RATIO_PRICE_
    CONFIRM_WIN 滚动窗口的价格首尾对比，窗口约 7.5 分钟，会随时间滚动导致
    "下行→上行"翻转，让同一事实在评分两侧反复跳变。state 提供后，本函数
    在同一日同一 (prev, latest) 元组下锁定首次判定的路径直到次日。
    """
    ratios = db_get_recent_hkex(conn, SHORT_RATIO_WINDOW + 2)
    score = 0
    support = 0
    signals: list[str] = []

    if len(ratios) < SHORT_RATIO_RISE_MIN + 1:
        return 0, 0, []

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

    # 逼空启动：高位拐头向下（空头回补开始）—— 需价格行为二阶确认
    if consecutive_rises >= SHORT_RATIO_RISE_MIN and latest < prev:
        drop = prev - latest
        pts = 25

        today_str = datetime.date.today().isoformat()
        lock_key  = (round(prev, 4), round(latest, 4))

        # 命中路径锁：直接复用当日已锁定的判定
        if (state is not None
                and state._ratio_lock_date == today_str
                and state._ratio_lock_key == lock_key
                and state._ratio_lock_path in ("squeeze", "support")):
            if state._ratio_lock_path == "squeeze":
                msg = (f"卖空占比高位拐头：连涨 {consecutive_rises} 日后回落 "
                       f"{prev:.2f}% → {latest:.2f}% (↓{drop:.2f}pp) [+{pts}分]"
                       f" (路径锁定·当日)")
                score += pts
                signals.append(msg)
            else:
                msg = (f"卖空占比高位拐头（{prev:.2f}%→{latest:.2f}%）"
                       f"空头已清算且行情走弱 [支撑做空+{pts}分] (路径锁定·当日)")
                support += pts
                signals.append(msg)
        else:
            prices = db_get_recent_prices(conn, SHORT_RATIO_PRICE_CONFIRM_WIN)
            have_prices = len(prices) >= 5 and prices[0] > 0
            price_uptrend = (not have_prices) or prices[-1] >= prices[0]
            chosen_path = "squeeze" if price_uptrend else "support"

            if price_uptrend:
                # 价格上行（或样本不足）→ 维持原有逼空启动语义
                msg = (f"卖空占比高位拐头：连涨 {consecutive_rises} 日后回落 "
                       f"{prev:.2f}% → {latest:.2f}% (↓{drop:.2f}pp) [+{pts}分]")
                score += pts
                signals.append(msg)
                log.warning(f"[趋势反转] {msg}")
                db_save_signal(conn, "SHORT_RATIO_PEAK", msg, pts)
            else:
                # 价格下行 → 空头已清算 + 行情走弱，逼空假设被否，转入做空支撑
                move_pct = (prices[-1] - prices[0]) / prices[0] * 100
                msg = (f"卖空占比高位拐头（{prev:.2f}%→{latest:.2f}%）但日内"
                       f"价格 {move_pct:+.2f}%，空头已清算且行情走弱 "
                       f"[支撑做空+{pts}分]")
                support += pts
                signals.append(msg)
                db_save_signal(conn, "SHORT_RATIO_PEAK_BEARISH", msg, pts)

            # 锁定本日判定，避免窗口滚动反复翻转
            if state is not None:
                state._ratio_lock_date = today_str
                state._ratio_lock_key  = lock_key
                state._ratio_lock_path = chosen_path
                log.info(
                    f"[拐头路径锁] {today_str} {lock_key} → {chosen_path}"
                )

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

    return score, support, signals


# ═══════════════════════════════════════════════════════════
# 八、HKEX 历史卖空深度分析
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
    COST_WINDOW     = HKEX_COST_WINDOW  # 与 paper_trader.py 共享，见 shared_config.py
    COST_WINDOW_MID = 20  # 中期成本线窗口：月度视角，判断中期空头整体盈亏
    MOMENTUM_WINDOW = 5   # 动能/爆量基准：一个完整交易周

    rows = conn.execute(
        """SELECT date, short_volume, short_value, short_ratio
           FROM hkex_daily ORDER BY date DESC LIMIT ?""",
        (max(COST_WINDOW_MID, MOMENTUM_WINDOW + 1),),
    ).fetchall()

    if len(rows) < 2:
        return 0, 0, [], {}

    # 整理数据（最新在前）
    dates        = [r[0] for r in rows]
    short_vols   = [r[1] for r in rows]
    short_vals   = [r[2] for r in rows]
    short_ratios = [r[3] for r in rows]

    # ── 维度1：双窗口加权空头成本线 ───────────────────────
    def _vwap(vols, vals, n):
        n = min(n, len(vols))
        tv, tvol = sum(vals[:n]), sum(vols[:n])
        return tv / tvol if tvol > 0 else None

    weighted_cost     = _vwap(short_vols, short_vals, COST_WINDOW)      # 10日
    weighted_cost_mid = _vwap(short_vols, short_vals, COST_WINDOW_MID)  # 20日

    # ── 维度2：卖空动能比 ──────────────────────────────────
    latest_ratio = short_ratios[0]
    avg_ratio_5d = statistics.mean(short_ratios[1:MOMENTUM_WINDOW + 1])
    momentum_ratio = (latest_ratio / avg_ratio_5d) if avg_ratio_5d > 0 else 1.0

    # ── 维度3：卖空量爆量比 ───────────────────────────────
    latest_vol = short_vols[0]
    avg_vol_5d = statistics.mean(short_vols[1:MOMENTUM_WINDOW + 1])
    volume_surge = (latest_vol / avg_vol_5d) if avg_vol_5d > 0 else 1.0

    # ── 评分（做空支撑分 / 逼空风险加成分）──────────────
    short_support = 0    # 支持做空入场的分数
    squeeze_risk  = 0    # 需叠加到逼空评分的分数
    signals: list[str] = []

    # 价格 vs 空头成本线（10日短期线）
    if weighted_cost and current_price:
        gap_pct = (weighted_cost - current_price) / weighted_cost * 100
        if gap_pct > 5:
            pts = 15
            msg = (f"价格({current_price:.1f}) 低于10日成本线({weighted_cost:.1f}) "
                   f"{gap_pct:.1f}%，近期空头盈利 [支撑做空+{pts}分]")
            short_support += pts
            signals.append(msg)
        elif gap_pct < -3:
            pts = 20
            msg = (f"价格({current_price:.1f}) 高于10日成本线({weighted_cost:.1f}) "
                   f"{abs(gap_pct):.1f}%，近期空头亏损 [逼空风险+{pts}分]")
            squeeze_risk += pts
            signals.append(msg)
            db_save_signal(conn, "SQUEEZE_COST_BREACH", msg, pts)
        else:
            signals.append(
                f"价格({current_price:.1f}) 接近10日成本线({weighted_cost:.1f})，关键博弈区"
            )

    # 价格 vs 空头成本线（20日中期线，额外加成/扣分）
    if weighted_cost_mid and current_price:
        gap_mid_pct = (weighted_cost_mid - current_price) / weighted_cost_mid * 100
        if gap_mid_pct > 5:
            pts = 8
            msg = (f"价格低于20日成本线({weighted_cost_mid:.1f}) "
                   f"{gap_mid_pct:.1f}%，中期空头亦盈利 [支撑做空+{pts}分]")
            short_support += pts
            signals.append(msg)
        elif gap_mid_pct < -3:
            # 超越幅度越大说明旧空头出清概率越高，逼空风险反而递减
            # 每超出5%扣2分，最低保留2分
            pts = max(10 - int(abs(gap_mid_pct) / 5) * 2, 2)
            msg = (f"价格高于20日成本线({weighted_cost_mid:.1f}) "
                   f"{abs(gap_mid_pct):.1f}%，中期空头全面亏损 [逼空风险+{pts}分]")
            squeeze_risk += pts
            signals.append(msg)
            db_save_signal(conn, "SQUEEZE_COST_BREACH_MID", msg, pts)

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
        # Why: momentum_ratio < 0.6 表示今日卖空占比远低于 5 日均值，空头筹码
        # 正在退场。空头减仓 = 后续逼空燃料减少，应归入做空支撑而非逼空风险。
        pts = 10
        msg = (f"卖空动能比 {momentum_ratio:.2f}× 空头撤退中，"
               f"逼空燃料减少 [支撑做空+{pts}分]")
        short_support += pts
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
        "weighted_cost":     weighted_cost,
        "weighted_cost_mid": weighted_cost_mid,
        "momentum_ratio":    momentum_ratio,
        "volume_surge":      volume_surge,
        "avg_ratio_5d":      avg_ratio_5d,
        "latest_ratio":      latest_ratio,
    }
    return min(short_support, 50), squeeze_risk, signals, stats


# ═══════════════════════════════════════════════════════════
# 八-B、出货式拉升检测（Bug 13 防护）
# ═══════════════════════════════════════════════════════════
def analyze_distribution_pump(
    conn: sqlite3.Connection,
    current_price: Optional[float],
    hkex_stats: dict,
) -> tuple[int, list[str]]:
    """
    出货式拉升识别（Bug 13）：横向交叉验证防止"大单净流入持续正值"被误读为多头。

    机构在高位用大单接散户卖盘出货时呈现的复合特征：
        - 大单净流入持续正值（streak ≥ 8 轮）
        - 反弹幅度逐次递减（peaks[0] > peaks[1] > peaks[2]）
        - 价格无法突破日内高点
        - HKEX 卖空占比仍处高位 / 动能比偏多（真实空头未撤）
        - 可选：失衡度高频翻转（盘口博弈）

    满足主条件 → 返回做空入场支撑分 +20。
    弱信号（反弹乏力 + 失衡度翻转）→ +10。

    2026-05-18 实盘：5 个指标全部命中，但当时无此检测维度，做空 ENTRY
    信号被假"流入加速"信号反复证伪，错过日高出货后次日跳空 -8% 的机会。
    """
    if current_price is None:
        return 0, []

    # streak 独立计算（避免依赖外部传参）
    streak_history = db_get_recent_big_net(conn, BIGFLOW_STREAK_WINDOW)
    streak = 0
    for v in streak_history:
        if v > 0:
            streak += 1
        else:
            break
    if streak < 8:
        return 0, []

    prices = db_get_recent_prices(conn, 60)
    if len(prices) < 30:
        return 0, []

    # 条件1：反弹幅度递减（把 ~15 分钟价格切成 3 段，每段 max 单调递减）
    seg = len(prices) // 3
    peaks = [max(prices[i * seg:(i + 1) * seg]) for i in range(3)]
    rebound_fading = peaks[2] < peaks[1] and peaks[1] <= peaks[0]

    # 条件2：未突破日内高点（留 0.5% 缓冲）
    session_high = db_get_session_high(
        conn, datetime.date.today().isoformat()
    )
    near_session_high = bool(
        session_high and current_price < session_high * 0.995
    )

    # 条件3：HKEX 真实空头仍在（占比高位 或 动能比 ≥ 1.5×）
    latest_ratio = hkex_stats.get("latest_ratio") or 0
    momentum = hkex_stats.get("momentum_ratio") or 0
    hkex_bearish = latest_ratio >= 8.0 or momentum >= 1.5

    if rebound_fading and near_session_high and hkex_bearish:
        # 迭代四十六：归零中性化。原 +20 既与 动能比/爆量 三重计数 HKEX 同源数据,
        # 回测又显示触发后前向收益为正(反向)。仍写库供回测追踪,但不计分、不进做空理由。
        pts = DISTRIBUTION_PUMP_PTS
        msg = (
            f"出货式拉升形态(中性·回测反向不计分)：大单流入持续 {streak} 轮但反弹递减"
            f"（{peaks[0]:.1f}→{peaks[1]:.1f}→{peaks[2]:.1f}），"
            f"HKEX 占比 {latest_ratio:.1f}%（动能 {momentum:.2f}×）"
        )
        db_save_signal(conn, "DISTRIBUTION_PUMP_SUSPECT", msg, pts)
        return pts, ([msg] if pts else [])

    # 弱信号：反弹乏力 + 盘口高频翻转
    if rebound_fading:
        flips = db_count_imb_flips(conn, 12, SHORT_IMB_FLIP_BAND)
        if flips >= 3:
            pts = 10
            msg = (
                f"⚠ 拉升乏力（{peaks[0]:.1f}→{peaks[2]:.1f}）+ "
                f"盘口翻转 {flips} 次，机构博弈嫌疑 [支撑做空+{pts}分]"
            )
            return pts, [msg]

    return 0, []


# ═══════════════════════════════════════════════════════════
# 九、做空信号引擎
# ═══════════════════════════════════════════════════════════

def apply_short_entry_failsafes(
    conn: sqlite3.Connection,
    score: int,
    sig_type: str,
    current_imbalance: float,
    signals: list[str],
    distribution_active: bool = False,
    ask_depth: Optional[float] = None,
    current_price: Optional[float] = None,
    big_net_stale: bool = False,
) -> tuple[int, str, list[str]]:
    """
    ENTRY 信号的 failsafe 集合（Bug 6 + Bug 9 + 迭代二十五 + 追空守门 + 隐藏吸筹否决），需在两处调用：

    1. `analyze_short_entry` 末尾：拦截主信号 score ≥ SHORT_ENTRY_MIN 的 ENTRY；
    2. 主循环 ENTRY 升级后：当合并 hkex/s1/pump 支撑分把 HOLD 推到 ENTRY 时，
       analyze_short_entry 内部 sig_type 始终是 HOLD，failsafe 永不触发——
       Bug 18 真实案例：2026-05-20 13:36-13:42 实盘做空 63-78(ENTRY)，
       但 imbalance 整段 +0.92~+0.97（极端买盘强势），Failsafe 1 因被
       绕过未降级，应该 CAUTION。修法：主循环升级后再调用一次本函数。

    迭代二十五豁免：派发模式生效 OR 卖盘深度 < THIN_ASK_DEPTH_FOR_FLIP_SKIP 时
    跳过 Failsafe 2。实盘 2026-05-29 10:53:13 案例：派发末期 BLOCKED→0 分误杀，
    但同期资金面持续恶化（大单 -6,152 万、中小单 -5,159 万创新低），翻转更可能
    是薄盘流动性稀薄抖动而非主力博弈。

    返回 BLOCKED 时丢弃 signals 只保留 reason（与原 in-place 行为一致）。
    """
    # Failsafe 1：摆盘明显偏多时强制降级（Bug 6 防底部追空）
    if sig_type == "ENTRY" and current_imbalance > SHORT_TRAP_IMB_BLOCK:
        signals.append(
            f"⚠ 摆盘失衡度 {current_imbalance:+.3f} 明显偏多（>{SHORT_TRAP_IMB_BLOCK}），"
            f"买盘强势，ENTRY 降级为 CAUTION 防诱空"
        )
        sig_type = "CAUTION"

    # Failsafe 2：失衡度高频翻转 → 挂单博弈（Bug 9 + 迭代二十五豁免）
    # 2026-05-12 实盘 ENTRY=98 触发瞬间 imbalance 6 轮内翻转 3 次，
    # 价格 1.5 分钟反弹 +1.1%。高频翻转是主力挂单/撤单博弈的特征。
    if sig_type == "ENTRY":
        thin_book = ask_depth is not None and ask_depth < THIN_ASK_DEPTH_FOR_FLIP_SKIP
        if distribution_active or thin_book:
            # 豁免路径：派发末期或薄盘期翻转视为流动性抖动，不 BLOCKED
            skip_reason = ("派发模式生效" if distribution_active
                           else f"薄盘期（卖深 {ask_depth:.0f} < {THIN_ASK_DEPTH_FOR_FLIP_SKIP} 股）")
            signals.append(
                f"ℹ 翻转 failsafe 已豁免：{skip_reason}，翻转视为流动性抖动而非主力博弈"
            )
        else:
            flip_count = db_count_imb_flips(
                conn, SHORT_IMB_FLIP_WINDOW, SHORT_IMB_FLIP_BAND
            )
            if flip_count >= SHORT_IMB_FLIP_MIN:
                reason = (f"近 {SHORT_IMB_FLIP_WINDOW} 轮失衡度极性翻转 "
                          f"{flip_count} 次，识别为挂单博弈/操纵性信号，禁止开空")
                db_save_signal(conn, "SHORT_IMB_FLIP_TRAP", reason, 0)
                return 0, "BLOCKED", [reason]

    # Failsafe 3：追空守门 — 自日内高深度回落后，ENTRY 是"追在最低点"而非新鲜入场
    # 做空分高常常只是滞后/背景分（距高跌幅、低于成本线、累计流出）堆出来的，
    # 描述的是"已经跌透"，不是"还会跌"。一旦自日内高回落超过阈值，下跌空间已耗尽，
    # 强制降级避免在底部诱导追空。纯以回落幅度为闸——不叠加 big_net_stale 等条件，
    # 否则会在"冻结但尚未达 stale 窗口"的间隙漏网（v1 实盘 13:47-13:48 教训）。
    if sig_type == "ENTRY" and current_price is not None:
        session_high = db_get_session_high(conn, datetime.date.today().isoformat())
        if session_high and session_high > 0:
            pullback_pct = (session_high - current_price) / session_high * 100
            if pullback_pct >= SHORT_CHASE_PULLBACK_PCT:
                msg = (
                    f"⚠ 价格已自日内高 {session_high:.1f} 回落 {pullback_pct:.1f}%"
                    f"（≥{SHORT_CHASE_PULLBACK_PCT}%），下跌空间已耗尽，"
                    f"ENTRY 降级为 CAUTION 防追空"
                )
                signals.append(msg)
                db_save_signal(conn, "SHORT_CHASE_GUARD", msg, 0)
                sig_type = "CAUTION"

    # Failsafe 4：隐藏主力吸筹否决 —— 中单(拆单)加速买入 + 价格上行（2026-06-03 00100 假信号）
    # 大单冻结时无法看清主力公开方向，中单(拆单)成为隐藏主力的探针。当中单累计净买入
    # 且持续加速、同期价格不跌反涨，做空 ENTRY 与"资金方向 + 价格印证"双双矛盾——这是
    # 隐藏主力吸筹而非派发。既有 failsafe 只看失衡度/距高跌幅，对这种"盘口偏空但价格涨、
    # 中单偷偷买"的 reverse-诱空组合无感（Bug 12 同源）。
    # 二段分级（迭代：2026-06-10 00100 全天误报）：
    #   弱档（accel 或 level、价涨≥0.2%）→ ENTRY 降级 CAUTION（原行为）。
    #   强档（大幅吸筹水位 + 价涨≥0.5% 强印证）→ 直接 BLOCKED 禁止开空。
    # 改因：00100 当日吸筹水位巨大、价格连创新高，但仅降级 CAUTION 时做空分仍被日级
    # 背景注水维持 80-100，持续误导追空（价格 418→447 单边）。强档双确认时 CAUTION 不够，
    # 需禁空。允许从 CAUTION 进一步升级 BLOCKED（背景注水的高分常已先被压成 CAUTION）。
    if (sig_type in ("ENTRY", "CAUTION") and big_net_stale and current_price is not None):
        cap = db_get_recent_capital_structure(conn, MID_ACCUM_WINDOW)
        prices = db_get_recent_prices(conn, MID_ACCUM_PRICE_WINDOW)
        if len(cap) >= 3 and len(prices) >= 3 and prices[0] > 0:
            mid_latest = cap[0][1]                 # 中单累计（最新）
            mid_delta  = cap[0][1] - cap[-1][1]    # 窗口内中单净买入增量（最新 − 最旧）
            price_rise_pct = (current_price - prices[0]) / prices[0] * 100
            accel = mid_delta >= MID_ACCUM_MIN_DELTA       # 加速买入
            level = mid_latest >= MID_ACCUM_MIN_LEVEL      # 已累积显著多头
            if (mid_latest > 0 and (accel or level)
                    and price_rise_pct >= MID_ACCUM_PRICE_RISE_PCT):
                basis = (f"近 {MID_ACCUM_WINDOW} 轮加速买入 {mid_delta/10000:+,.1f} 万"
                         if accel else f"累计已达 {mid_latest/10000:+,.1f} 万显著多头")
                # 强档：大幅吸筹水位 + 强价格印证双确认 → 升级 BLOCKED（与 Failsafe 2 同形：
                # 返回 0 分、丢弃其它 signals 只留禁空 reason）。
                strong = (mid_latest >= MID_ACCUM_MIN_LEVEL * MID_ACCUM_STRONG_LEVEL_MULT
                          and price_rise_pct >= MID_ACCUM_STRONG_PRICE_RISE_PCT)
                if strong:
                    reason = (
                        f"⛔ 中单(拆单){basis}，同期价格 +{price_rise_pct:.2f}% 上行"
                        f"——隐藏主力吸筹+价格印证（强档双确认），禁止开空防逆势追空"
                    )
                    db_save_signal(conn, "SHORT_MID_ACCUM_BLOCK", reason, 0)
                    return 0, "BLOCKED", [reason]
                # 弱档：仅在 ENTRY 时降级 CAUTION（已是 CAUTION 则不重复降级）
                if sig_type == "ENTRY":
                    msg = (
                        f"⚠ 中单(拆单){basis}，同期价格 +{price_rise_pct:.2f}% 上行"
                        f"——大单冻结期隐藏主力吸筹、价格印证，ENTRY 降级为 CAUTION 防逆势追空"
                    )
                    signals.append(msg)
                    db_save_signal(conn, "SHORT_MID_ACCUM_VETO", msg, 0)
                    sig_type = "CAUTION"

    return score, sig_type, signals


def analyze_short_entry(
    conn: sqlite3.Connection,
    squeeze_score: int,
    current_price: Optional[float],
    current_ask: float,
    current_imbalance: float,
    recent_max_squeeze: Optional[int] = None,
    big_net_stale: bool = False,
    distribution_score: int = 0,
    distribution_confirmed: bool = False,
    distribution_sigs: Optional[list[str]] = None,
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

    诱空保护：
        - 卖盘骤增但摆盘仍明显偏多 → 视为挂大卖单后撤单的诱空陷阱，不予加分
        - 摆盘失衡度 > +0.30（买盘强势）→ 即使评分够也降级到 CAUTION
        - recent_max_squeeze：近 N 轮逼空评分峰值，避免均值滑动稀释绕过安全门

    安全门设计（多通道放行）：
        BLOCKED 默认在 effective_squeeze > SHORT_SAFE_SQUEEZE 时触发。
        以下任一通道命中即放行：
          (A) 价格已确认下行 AND 大单持续净流出 → 行情否定逼空假设
          (B) 派发模式三条件确认 → 机构出货已坐实，逼空燃料反向
          (C) 价格从日内最高点回落 ≥ X% → 价格 capitulation，与资金流方向无关
        放行后继续走正常评分，避免 HKEX 日级静态信号锁死整个交易日。
    """
    # ── 安全门：逼空风险过高直接拦截（用近 N 轮峰值，避免被均值稀释）──
    effective_squeeze = max(squeeze_score, recent_max_squeeze or 0)
    if effective_squeeze > SHORT_SAFE_SQUEEZE:
        # 通道 A：价格已确认下行 + 大单持续净流出
        prices = db_get_recent_prices(conn, SHORT_BLOCK_OVERRIDE_WIN)
        price_confirmed_down = False
        price_move_pct = 0.0
        if len(prices) >= 5 and prices[0] > 0:
            price_move_pct = (prices[-1] - prices[0]) / prices[0] * 100
            price_confirmed_down = price_move_pct <= -SHORT_BLOCK_PRICE_DROP_PCT

        big_nets = db_get_recent_big_net(conn, 3)
        # big_net 是当日累计 HKD；最新值低于阈值且近 3 轮全部为负，视为持续净流出
        bigflow_confirmed_out = (
            len(big_nets) >= 1
            and big_nets[0] <= SHORT_BLOCK_BIGFLOW_THRESHOLD
            and all(v < 0 for v in big_nets)
        )
        channel_a = price_confirmed_down and bigflow_confirmed_out
        # 通道 B：派发模式三条件共振（2026-05-29 10:04 评分盲区案例补丁）
        channel_b = distribution_confirmed
        # 通道 C：从日内最高点急速回落（迭代二十六，2026-05-29 15:23 误杀案例）
        # 实盘 15:23:46 价格 870 vs 日高 917（−5.1%）但大单仍 +533 万，通道 A AND
        # 失败导致今天唯一一个真 ENTRY (15:22:45) 被压缩到 30 秒。通道 C 只看价格，
        # 一旦从日内峰值回落超过阈值即视为 capitulation。
        today_iso = datetime.date.today().isoformat()
        session_high = db_get_session_high(conn, today_iso)
        session_low = db_get_session_low(conn, today_iso)
        channel_c = False
        pullback_pct = 0.0
        rebound_pct = 0.0
        if (session_high is not None and current_price is not None
                and session_high > 0):
            pullback_pct = (session_high - current_price) / session_high * 100
            # A2：除"距日高回落够深"外，还要求价格仍压在日内低点附近——否则跌后横盘/反弹
            # 期日高旧 anchor 会让通道 C 每轮常开。自日内低点反弹超过阈值 = capitulation 结束。
            if session_low is not None and session_low > 0:
                rebound_pct = (current_price - session_low) / session_low * 100
                still_at_lows = rebound_pct <= SHORT_BLOCK_CAPITULATION_REBOUND_PCT
            else:
                still_at_lows = True  # 无低点数据（开盘首样本）时不收紧，保持原行为
            channel_c = (pullback_pct >= SHORT_BLOCK_SESSION_PULLBACK_PCT
                         and still_at_lows)

        if channel_a or channel_b or channel_c:
            # 放行：逼空假设被行情或资金结构否定
            if channel_b:
                override_msg = (
                    f"⚠ 逼空分={squeeze_score}（峰值 {recent_max_squeeze}）超线，"
                    f"但派发模式三条件确认 → 放行做空入场"
                )
            elif channel_a:
                override_msg = (
                    f"⚠ 逼空分={squeeze_score}（峰值 {recent_max_squeeze}）超线，"
                    f"但日内价格 {price_move_pct:+.2f}%、大单累计 "
                    f"{big_nets[0] / 10000:+,.0f} 万持续流出 → 放行做空入场"
                )
            else:
                # session_low 可能为 None（still_at_lows 兜底放行）→ 文案需防 NoneType 格式化崩溃
                low_part = (f"、仍压日内低 {session_low:.1f}（反弹 {rebound_pct:.2f}%）"
                            if session_low is not None else "")
                override_msg = (
                    f"⚠ 逼空分={squeeze_score}（峰值 {recent_max_squeeze}）超线，"
                    f"但价格从日内高 {session_high:.1f} 回落 {pullback_pct:.2f}%{low_part}"
                    f"→ 价格 capitulation，放行做空入场"
                )
            log.warning(f"[安全门放行] {override_msg}")
            db_save_signal(conn, "SHORT_BLOCK_OVERRIDE", override_msg, 0)
            # 不 return，继续走正常的评分流程
        else:
            if recent_max_squeeze and recent_max_squeeze > squeeze_score:
                reason = (f"逼空评分={squeeze_score}（近 {SHORT_SQUEEZE_LOOKBACK} "
                          f"轮峰值 {recent_max_squeeze}）超过安全线 "
                          f"{SHORT_SAFE_SQUEEZE}，禁止开空")
            else:
                reason = (f"逼空评分={squeeze_score} 超过安全线 "
                          f"{SHORT_SAFE_SQUEEZE}，禁止开空")
            return 0, "BLOCKED", [reason]

    score   = 0
    signals: list[str] = []

    # ── 维度 1：大单净流入方向 ─────────────────────────────
    # Bug 14: 大单 cumulative 多轮冻结时本维度跳过，避免基于陈旧值持续加分
    big_nets = db_get_recent_big_net(conn, BIGFLOW_WINDOW)
    if big_net_stale:
        signals.append("ℹ 大单累计冻结多轮，'大单净流入'维度本轮跳过")
    elif len(big_nets) >= 4:
        latest_net  = big_nets[0]
        earlier_net = big_nets[1:5]
        had_positive = any(v > 0 for v in earlier_net)

        if latest_net < 0 and had_positive:
            # 微小反转过滤：|latest_net| 占 earlier 正值峰值 < 比例阈值 → 视为噪音波动
            # （Bug 9：2026-05-12 实盘 +252万 → -15.8万 的"反转"实际是 6% 微小波动）
            recent_peak = max((v for v in earlier_net if v > 0), default=0)
            is_micro = (recent_peak > 0
                        and abs(latest_net) < recent_peak * SHORT_MICRO_REVERSAL_RATIO)
            if is_micro:
                pts = 5
                ratio_pct = abs(latest_net) / recent_peak * 100
                msg = (f"⚠ 大单净流入轻微转负：{latest_net / 10000:+,.1f} 万 "
                       f"(仅占近期峰值 {recent_peak / 10000:+,.1f} 万的 "
                       f"{ratio_pct:.1f}%)，疑似噪音 [+{pts}分]")
                score += pts
                signals.append(msg)
            else:
                pts = 30
                msg = (f"大单净流入由正转负：{latest_net / 10000:+,.1f} 万港元 [+{pts}分]")
                score += pts
                signals.append(msg)
                db_save_signal(conn, "SHORT_BIGFLOW_REVERSAL", msg, pts)
        elif latest_net < 0:
            # Bug 16: 累计虽负但近期 Δ 显著转买入 → 资金方向已转，不再算"持续为负"
            if (len(big_nets) >= 2
                    and (big_nets[0] - big_nets[1]) >= BIG_NET_DELTA_THRESHOLD):
                flow_delta = big_nets[0] - big_nets[1]
                signals.append(
                    f"ℹ 大单累计 {latest_net/10000:+,.1f} 万但近期 Δ "
                    f"{flow_delta/10000:+,.1f} 万买入，不计'持续为负'分"
                )
            else:
                pts = 15
                msg = f"大单净流入持续为负：{latest_net / 10000:+,.1f} 万港元 [+{pts}分]"
                score += pts
                signals.append(msg)

    # ── 维度 2：卖盘深度骤增（大卖单出现）──────────────────
    # 用近 K 轮中位数代替单点采样；trap_suspect 同样用近 K 轮失衡度中位数，
    # 避免一秒的盘口快照决定 +25 分的归属。
    ask_history = db_get_recent_ask_depth(conn, ASK_DEPTH_WINDOW)
    imb_rows_trap = conn.execute(
        "SELECT imbalance FROM orderbook_snapshots ORDER BY id DESC LIMIT ?",
        (SHORT_IMB_SMOOTH_K,),
    ).fetchall()
    smoothed_imb = (statistics.median(r[0] for r in imb_rows_trap)
                    if imb_rows_trap else current_imbalance)
    if len(ask_history) >= ASK_DEPTH_SMOOTH_K + 4 and current_ask > 0:
        smoothed_ask = statistics.median(ask_history[:ASK_DEPTH_SMOOTH_K])
        avg_ask = statistics.median(ask_history[ASK_DEPTH_SMOOTH_K:])
        if avg_ask > 0 and smoothed_ask > 0:
            surge_pct = (smoothed_ask - avg_ask) / avg_ask * 100
            # 诱空检测：卖盘骤增但摆盘仍明显偏多 → 大卖单与买盘强势同时存在，
            # 通常是挂单制造空头跟风后立即撤单的套路，不予加分。
            trap_suspect = smoothed_imb > SHORT_TRAP_IMB_SUPPRESS
            if surge_pct >= SHORT_ASK_SURGE_PCT:
                if trap_suspect:
                    msg = (f"⚠ 卖盘骤增 {surge_pct:.1f}% 但近{SHORT_IMB_SMOOTH_K}"
                           f"轮失衡度中位 {smoothed_imb:+.3f} 偏多，疑似诱空挂单，不计分")
                    signals.append(msg)
                    db_save_signal(conn, "SHORT_ASK_SURGE_TRAP", msg, 0)
                else:
                    pts = 25
                    msg = (f"卖盘深度骤增 {surge_pct:.1f}%（近{ASK_DEPTH_SMOOTH_K}"
                           f"轮中位 {smoothed_ask:,.0f} vs 均值 {avg_ask:,.0f} 股），"
                           f"大卖单涌入 [+{pts}分]")
                    score += pts
                    signals.append(msg)
                    db_save_signal(conn, "SHORT_ASK_SURGE", msg, pts)
            elif surge_pct >= SHORT_ASK_SURGE_PCT * 0.5:
                if trap_suspect:
                    msg = (f"⚠ 卖盘上升 {surge_pct:.1f}% 但近{SHORT_IMB_SMOOTH_K}"
                           f"轮失衡度中位 {smoothed_imb:+.3f} 偏多，疑似诱空，不计分")
                    signals.append(msg)
                else:
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

    # ── 维度 4：价格低于日内高点（下行动能）─────────────────
    # anchor 用日内 session_high（today 00:00 起的全体价格 max），
    # 避免滑动窗口在持续下跌时把基准自我拉低导致信号失真。
    prices = db_get_recent_prices(conn, SHORT_PRICE_WINDOW)
    session_high = db_get_session_high(
        conn, datetime.date.today().isoformat()
    )
    if session_high and current_price and session_high > 0:
        drop_pct = (session_high - current_price) / session_high * 100
        if drop_pct >= 0.5:
            pts = 15
            msg = (f"价格较日内高点下跌 {drop_pct:.2f}%"
                   f"（当前 {current_price} vs 日高 {session_high}）[+{pts}分]")
            score += pts
            signals.append(msg)
        elif drop_pct >= 0.2:
            pts = 7
            msg = f"价格轻微回落 {drop_pct:.2f}% [+{pts}分]"
            score += pts
            signals.append(msg)

    # ── 维度 5：最新连续下行轮次 ───────────────────────────
    # 从最新 tick 向回数连续 down ticks，与 peak 位置解耦，避免窗口滑动重置 streak。
    if len(prices) >= 3:
        down_streak = 0
        for i in range(len(prices) - 1, 0, -1):
            if prices[i] < prices[i - 1]:
                down_streak += 1
            else:
                break
        if down_streak >= 2:
            pts = 10
            msg = f"最新连续下行 {down_streak} 轮 [+{pts}分]"
            score += pts
            signals.append(msg)

    # ── 维度 6：派发模式确认（评分盲区补丁）─────────────────
    # 维度 1-5 在派发末期常常各自不达阈值（大单 cumulative 冻结、价格已先跌、
    # 反弹幅度小、卖盘深度回正），结果"实际利空已确认但评分仍在'正常'区间"。
    # 派发模式作为独立主信号，解开这个盲区。
    if distribution_score > 0:
        score += distribution_score
        if distribution_sigs:
            signals.extend(distribution_sigs)

    score = min(score, 100)
    if score >= SHORT_ENTRY_MIN:
        sig_type = "ENTRY"
    elif score >= int(SHORT_ENTRY_MIN * 0.6):
        sig_type = "CAUTION"
    else:
        sig_type = "HOLD"

    # Failsafe 1/2 抽为独立函数（Bug 18 修复）。主循环合并 support 后升级 ENTRY 时
    # 需再次调用，否则当 analyze_short_entry 内部 score < 阈值时 failsafe 永不触发。
    # 迭代二十五：传 distribution_confirmed + ask_depth 启用翻转 failsafe 豁免
    return apply_short_entry_failsafes(
        conn, score, sig_type, current_imbalance, signals,
        distribution_active=distribution_confirmed,
        ask_depth=current_ask if current_ask > 0 else None,
        current_price=current_price,
        big_net_stale=big_net_stale,
    )


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

    # 2. 卖盘深度骤减（护盾消失）—— 用近 K 轮中位数过滤稀薄盘口噪音
    ask_history = db_get_recent_ask_depth(conn, ASK_DEPTH_WINDOW)
    if len(ask_history) >= ASK_DEPTH_SMOOTH_K + 4:
        smoothed_ask = statistics.median(ask_history[:ASK_DEPTH_SMOOTH_K])
        avg_ask = statistics.median(ask_history[ASK_DEPTH_SMOOTH_K:ASK_DEPTH_SMOOTH_K + 5])
        if avg_ask > 0 and smoothed_ask > 0:
            shrink_pct = (avg_ask - smoothed_ask) / avg_ask * 100
            if shrink_pct >= 40:
                urgency = max(urgency, 65)
                msg = f"卖盘深度骤减 {shrink_pct:.1f}%，空头回补迹象，建议减仓"
                reasons.append(msg)
                db_save_signal(conn, "SHORT_EXIT_ASK_SHRINK", msg, 65)

    # 3. 大单净流入强势转正
    # Bug 13 防护：仅当价格同步突破近期高点时才视为真"主力托盘"信号。
    # 若流入转正但价格未破高 → 可能是出货式托盘（机构接散户卖单同时
    # 后续会继续砸盘），此时持空仓应继续观察，不应被假信号骗去止损。
    big_nets = db_get_recent_big_net(conn, 5)
    if len(big_nets) >= 3:
        recent = big_nets[:3]
        if all(v > 0 for v in recent):
            prices = db_get_recent_prices(conn, 30)
            breakout = False
            if len(prices) >= 10:
                window_high = max(prices[:-3])
                breakout = prices[-1] >= window_high * 1.002

            if not breakout:
                # 价格未破高 → 出货式托盘嫌疑，仅提示不计 urgency
                reasons.append(
                    f"⚠ 大单转正但价格未破近期高（{recent[0]/10000:+,.1f} 万），"
                    f"疑似出货式托盘，持空仓继续观察"
                )
            elif recent[0] > recent[1] * 1.5 and recent[1] > 0:
                urgency = max(urgency, 70)
                msg = (f"大单净流入加速转正（{recent[0]/10000:+,.1f} 万）"
                       f"且价格突破近期高，主力托盘迹象")
                reasons.append(msg)
                db_save_signal(conn, "SHORT_EXIT_BIGFLOW", msg, 70)
            else:
                urgency = max(urgency, 45)
                reasons.append(
                    f"大单净流入连续 3 轮为正（{recent[0]/10000:+,.1f} 万）"
                    f"且价格突破近期高"
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
# 十、综合评分与仪表盘
# ═══════════════════════════════════════════════════════════

# ── 持仓模型（手动建仓，命令行传入）────────────────────────
@dataclass
class HeldShort:
    """记录用户手动建立的空头持仓，用于在监控仪表盘中输出平仓建议。"""
    entry_price:  float          # 建仓成本价（HKD）
    qty:          int            # 持仓股数
    stop_pct:     float = 0.035  # 止损：入场价 +3.5%
    target1_pct:  float = 0.015  # 第一目标：入场价 -1.5%（建议减仓 50%）
    target2_pct:  float = 0.030  # 第二目标：入场价 -3.0%（建议全部平仓）
    target1_done: bool  = False  # 第一目标是否已触发

    @property
    def stop(self) -> float:
        return round(self.entry_price * (1 + self.stop_pct), 1)

    @property
    def target1(self) -> float:
        return round(self.entry_price * (1 - self.target1_pct), 1)

    @property
    def target2(self) -> float:
        return round(self.entry_price * (1 - self.target2_pct), 1)

    def unrealized_pnl(self, price: float) -> float:
        return (self.entry_price - price) * self.qty

    def pnl_pct(self, price: float) -> float:
        return (self.entry_price - price) / self.entry_price * 100


# ── 平仓建议评估（持仓模式专用）────────────────────────────
@dataclass
class CoverAdvice:
    action:   str          # COVER_ALL / COVER_HALF / HOLD / STOP_LOSS
    urgency:  int          # 0-100
    reasons:  list[str]
    pnl:      float        # 当前浮动盈亏（HKD）
    pnl_pct:  float        # 盈亏百分比


def evaluate_cover_signal(
    held:          HeldShort,
    price:         float,
    squeeze_score: int,
    imbalance:     float,
    conn:          sqlite3.Connection,
) -> CoverAdvice:
    """
    对手动持仓输出平仓建议，综合五个维度：

    优先级（高→低）：
      P1  价格触及止损线              → STOP_LOSS  立即全平
      P2  逼空评分 ≥ 35 且有浮盈      → COVER_ALL  立即全平（趁还有利润）
      P3  信号双反转（大单+失衡）     → COVER_ALL  全平
      P4  信号单反转 或 逼空 25-35   → COVER_HALF 减仓 50%
      P5  价格 ≤ 第二目标价           → COVER_ALL  锁利全平
      P6  价格 ≤ 第一目标价           → COVER_HALF 减仓 50%
      --  以上均未触发               → HOLD
    """
    pnl     = held.unrealized_pnl(price)
    pnl_pct = held.pnl_pct(price)
    reasons: list[str] = []
    urgency = 0
    action  = "HOLD"

    # ── P1：止损 ──────────────────────────────────────────────
    if price >= held.stop:
        gap = (price - held.entry_price) / held.entry_price * 100
        reasons.append(
            f"价格({price}) ≥ 止损线({held.stop})，亏损 {abs(pnl_pct):.2f}%，立即止损"
        )
        return CoverAdvice("STOP_LOSS", 100, reasons, pnl, pnl_pct)

    # ── P2：逼空高分 + 有浮盈 → 立即全平 ─────────────────────
    if squeeze_score >= 35 and pnl > 0:
        urgency = min(60 + (squeeze_score - 35) * 2, 95)
        reasons.append(
            f"逼空评分={squeeze_score}≥35 且浮盈={pnl:+,.0f} HKD，"
            f"建议立即锁定利润全平"
        )
        action = "COVER_ALL"

    # ── P3/P4：信号反转检测 ───────────────────────────────────
    reversal_signals = []

    # 大单净流入是否转正
    big_nets = db_get_recent_big_net(conn, 4)
    if len(big_nets) >= 3:
        recent    = big_nets[:2]
        had_neg   = any(v < 0 for v in big_nets[2:])
        if all(v > 0 for v in recent) and had_neg:
            avg = statistics.mean(recent)
            reversal_signals.append(
                f"大单净流入反转为正 {avg/10000:+,.1f}万（托盘迹象）"
            )

    # 失衡度是否连续高位
    imb_rows = conn.execute(
        "SELECT imbalance FROM orderbook_snapshots ORDER BY id DESC LIMIT 3"
    ).fetchall()
    if len(imb_rows) >= 2:
        recent_imb = [r[0] for r in imb_rows[:2]]
        if all(v > 0.70 for v in recent_imb):
            reversal_signals.append(
                f"失衡度持续高位 {statistics.mean(recent_imb):+.3f}（买方接管）"
            )

    if len(reversal_signals) >= 2:
        urgency = max(urgency, 80)
        reasons += reversal_signals
        reasons.append("双信号反转，建议全部平仓")
        action = "COVER_ALL"
    elif len(reversal_signals) == 1 and action == "HOLD":
        urgency = max(urgency, 45)
        reasons += reversal_signals
        reasons.append("单信号反转，建议减仓 50%")
        action = "COVER_HALF"

    # ── P4b：逼空中等风险（25-35）+ 有浮盈 → 减仓 ────────────
    if 25 <= squeeze_score < 35 and pnl > 0 and action == "HOLD":
        urgency = max(urgency, 40)
        reasons.append(
            f"逼空评分={squeeze_score}（预警区间），有浮盈，建议减仓 50% 锁利"
        )
        action = "COVER_HALF"

    # ── P5：第二目标价 ────────────────────────────────────────
    if price <= held.target2:
        urgency = max(urgency, 75)
        reasons.append(
            f"价格({price}) ≤ 第二目标({held.target2})，盈利 {pnl_pct:.2f}%，全部平仓锁利"
        )
        action = "COVER_ALL"

    # ── P6：第一目标价（仅未减仓时触发）─────────────────────
    elif price <= held.target1 and not held.target1_done:
        urgency = max(urgency, 55)
        reasons.append(
            f"价格({price}) ≤ 第一目标({held.target1})，盈利 {pnl_pct:.2f}%，建议减仓 50%"
        )
        if action == "HOLD":
            action = "COVER_HALF"

    # ── 无信号：持仓安全，显示持仓状态 ──────────────────────
    if not reasons:
        gap_to_t1 = price - held.target1
        gap_to_stop = held.stop - price
        reasons.append(
            f"持仓安全 | 距目标①还差 {gap_to_t1:.1f} HKD | "
            f"距止损还有 {gap_to_stop:.1f} HKD"
        )

    return CoverAdvice(action, urgency, reasons, pnl, pnl_pct)


@dataclass
class MonitorState:
    last_hkex_date:    Optional[str]   = None
    last_price:        Optional[float] = None
    latest_hkex_ratio: Optional[float] = None
    latest_big_net:    Optional[float] = None
    recent_big_net_delta: Optional[float] = None   # 近一个 capital_flow 快照间的 Δ（约 1 分钟）
    latest_mid_net:    Optional[float] = None
    latest_small_net:  Optional[float] = None
    latest_ask_depth:  Optional[float] = None
    latest_imbalance:  Optional[float] = None
    short_score:       int             = 0
    main_signal_score: int             = 0   # 主信号分（维度 1-5），反映"此刻"盘面
    support_score:     int             = 0   # 日级背景分（HKEX + 成本线 + pump），整日基本固定
    short_signal:      str             = "HOLD"
    exit_urgency:      int             = 0
    weighted_cost:     Optional[float] = None   # 空头10日加权成本线
    weighted_cost_mid: Optional[float] = None   # 空头20日加权成本线
    momentum_ratio:    Optional[float] = None   # 卖空动能比
    volume_surge:      Optional[float] = None   # 卖空量爆量比
    in_position:       bool            = False     # 手动标记是否持有空仓
    recent_squeeze_scores: list[int]   = field(default_factory=list)  # 近 N 轮逼空评分（最旧在前）
    # 数据停滞检测：价格 + 大单累计连续未变 → Futu 无新成交，跳过打分
    _prev_price:       Optional[float] = None
    _prev_big_net:     Optional[float] = None
    _stale_count:      int             = 0
    # 大单累计单独停滞检测（Bug 14 / Bug 25）：与上面 AND 守门并行，价格动了但大单仍冻结时，
    # 只跳过"大单净流入"维度，其它维度照常打分。_prev_big_net_only 是带容差冻结判定的锚点，
    # 仅在新值突破 ±BIG_NET_STALE_BAND_HKD 带宽时才更新（否则视为抖动、不重置计数）。
    _prev_big_net_only:    Optional[float] = None
    _big_net_stale_count:  int             = 0
    # 派发模式粘滞计数（迭代二十五）：三条件命中过即设为 DISTRIBUTION_STICKY_ROUNDS，
    # 每轮减一；> 0 时即使当轮三条件不全命中也保持 confirmed，但分数减半。
    _distribution_sticky_left: int         = 0
    # 卖空占比拐头路径锁（Bug 13）：当日首次判定走 squeeze 还是 support 后锁定，
    # 避免 SHORT_RATIO_PRICE_CONFIRM_WIN 滚动窗口让同一事实在评分两侧反复翻转
    _ratio_lock_date:  Optional[str]       = None  # 锁定生效日期 YYYY-MM-DD
    _ratio_lock_key:   Optional[tuple]     = None  # (prev_ratio, latest_ratio) 元组
    _ratio_lock_path:  Optional[str]       = None  # 'squeeze' 或 'support'
    # API 失败计数：连续失败 ≥ API_FAIL_TOLERANCE_ROUNDS 时跳过打分
    _capital_fail_count:   int         = 0
    _orderbook_fail_count: int         = 0
    # L2 微观结构最新快照（迭代三十三：仪表盘 L2 面板）
    latest_tick:    Optional[dict] = None  # fetch_ticks 返回：sell_vol/buy_vol/best_bid_vol/best_ask_vol/large_*
    latest_ob:      Optional[dict] = None  # fetch_order_book 返回：含十档逐档量（迭代三十八 #1 形态展示）
    tick_window:    Optional[dict] = None  # 近 N 轮逐笔聚合（迭代三十八 #4 平滑展示）：buy/sell/大单累计
    latest_broker:  Optional[dict] = None  # fetch_broker_queue 返回：bid_top_name/net、ask_top_name/net
    latest_ice_sq:  int = 0                # 本轮冰山吸筹分（>0=买侧冰山吸筹）
    latest_ice_sup: int = 0                # 本轮冰山派发分（>0=卖侧冰山派发）
    latest_brk_sq:  int = 0                # 本轮经纪足迹买侧加成
    latest_brk_sup: int = 0                # 本轮经纪足迹卖侧加成
    latest_mf_score: int = 0               # 主力嫌疑分（控盘特征）
    latest_mf_label: str = "无明显控盘(市场化)"
    latest_mf_sigs: list = field(default_factory=list)  # 命中的控盘子维度明细
    latest_mf_tags: dict = field(default_factory=dict)  # {pin,seat,thin,dom_side,dom_kind}
    standoff_tag: Optional[str] = None  # 派发对峙·散户接盘提示（迭代三十九，信息性）


# 仪表盘内容区显示宽度（迭代三十八 #3）：框线 ║␣␣<58>␣␣║，内容区 = 58 个显示列。
# CJK 字符占 2 列，原 f-string 用字符数对齐导致右框线参差（实盘日志可见）。
DASH_INNER_W = 58


def _disp_width(s: str) -> int:
    """字符串的终端显示宽度：东亚宽/全角字符计 2 列，其余计 1 列。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _pad_disp(s: str, width: int = DASH_INNER_W) -> str:
    """按显示宽度截断+右填充到 width 列（CJK 安全），用于框内对齐。"""
    out, w = "", 0
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
        if w + cw > width:
            break
        out += c
        w += cw
    return out + " " * (width - w)


def _box(content: str) -> str:
    """渲染一整行框内文本：║␣<内容按显示宽对齐>␣║（单空边距，内容区 = 宽-2 列）。"""
    return f"║ {_pad_disp(content, DASH_INNER_W - 2)} ║"


def print_dashboard(
    state:          MonitorState,
    squeeze_score:  int,
    squeeze_signals: list[str],
    short_score:    int,
    short_signal:   str,
    short_sigs:     list[str],
    exit_urgency:   int,
    exit_reasons:   list[str],
    cover_advice:   Optional["CoverAdvice"] = None,
    held:           Optional["HeldShort"]   = None,
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

    # ── L2 微观结构面板字符串（迭代三十三；三十八优化 #1/#2/#4）──────
    # #4 逐笔改用近 N 轮窗口聚合：单轮 buy/sell 噪音极大（常见 0/0 ∞、40× 单帧极值）
    tw = state.tick_window
    if tw and (tw["buy"] or tw["sell"]):
        bv, sv = tw["buy"], tw["sell"]
        if bv >= sv:
            ratio = f"主买 {bv/sv:.1f}×" if sv > 0 else "主买 ∞"
        else:
            ratio = f"主卖 {sv/bv:.1f}×" if bv > 0 else "主卖 ∞"
        l2_tick = f"近{tw['rounds']}轮 买{bv:,.0f}/卖{sv:,.0f} ({ratio})"
    else:
        l2_tick = "—（逐笔无数据）"
    # #2 主动大单逐笔（单笔成交额 ≥ LARGE_TICK_NOTIONAL = 执行级主力足迹，与散单求和区分）
    if tw and (tw["lbuy_cnt"] or tw["lsell_cnt"]):
        l2_large = (f"主买 {tw['lbuy_cnt']}笔/{tw['lbuy']:,.0f}股  "
                    f"主卖 {tw['lsell_cnt']}笔/{tw['lsell']:,.0f}股")
    else:
        l2_large = f"无（本轮无 ≥{LARGE_TICK_NOTIONAL/10000:,.0f}万 单笔）"
    # 主动大单近 N 轮累计净（迭代四十一）：薄盘里单笔大单主宰单轮、比值剧烈翻面，累计净
    # 额才看得清真实净方向（不被比值来回翻晃干扰）。
    if tw and (tw.get("cum_lbuy_cnt") or tw.get("cum_lsell_cnt")):
        clb, cls = tw.get("cum_lbuy", 0), tw.get("cum_lsell", 0)
        net = clb - cls
        verdict = f"净买+{net:,.0f}" if net > 0 else (f"净卖{net:,.0f}" if net < 0 else "净平")
        l2_large_cum = (f"近{LARGE_TICK_CUM_WINDOW}轮 买{clb:,.0f}/卖{cls:,.0f} {verdict}股")
    else:
        l2_large_cum = f"近{LARGE_TICK_CUM_WINDOW}轮无大单"
    # 最优档挂量
    tk = state.latest_tick
    if tk:
        l2_book = (f"买一 {tk.get('best_bid_vol',0) or 0:,.0f} / "
                   f"卖一 {tk.get('best_ask_vol',0) or 0:,.0f} 股")
    else:
        l2_book = "—"
    # #1 十档形态（卖墙集中 vs 均匀分布）
    l2_shape = describe_book_shape(state.latest_ob) or "—"
    if state.latest_ice_sq > 0:
        l2_ice = f"买侧吸筹 [+{state.latest_ice_sq}] 被动接货→抑制追空"
    elif state.latest_ice_sup > 0:
        l2_ice = f"卖侧派发 [+{state.latest_ice_sup}] 被动出货→支撑做空"
    else:
        l2_ice = "无"
    brk = state.latest_broker
    if brk and (brk.get("bid_top_name") or brk.get("ask_top_name")):
        bn = (brk.get("bid_top_name") or "—")[:6]
        an = (brk.get("ask_top_name") or "—")[:6]
        bnet, anet = brk.get("bid_top_net", 0) or 0, brk.get("ask_top_net", 0) or 0
        bmark = "▲" if state.latest_brk_sq > 0 else " "
        amark = "▼" if state.latest_brk_sup > 0 else " "
        l2_brk = f"买{bmark}{bn}+{bnet}  卖{amark}{an}+{anet} 席"
    else:
        l2_brk = "—（本轮无具名席位）"
    l2_seat = read_seat_structure(state) or "—（无 L2 经纪数据）"
    l2_lines = [
        f"[④] L2逐笔(主动): {l2_tick}",
        f"      主动大单   : {l2_large}",
        f"      ↳累计净    : {l2_large_cum}",
        f"      最优档挂量 : {l2_book}",
        f"      十档形态   : {l2_shape}",
        f"      L2冰山     : {l2_ice}",
        f"      经纪净不对称: {l2_brk}",
        f"      席位结构   : {l2_seat}",
        f"[★] 主力嫌疑   : {state.latest_mf_score:3d}/100  {state.latest_mf_label}",
    ]

    # ── 头部数值（迭代三十八 #3：全部经 _box 按显示宽对齐，消除 CJK 错位）──
    price_s = str(state.last_price) if state.last_price else "N/A"
    hkex_s  = str(state.latest_hkex_ratio) if state.latest_hkex_ratio is not None else "N/A"
    mom_s   = f"{state.momentum_ratio:.2f}×" if state.momentum_ratio else "N/A"
    big_s   = f"{state.latest_big_net/10000:+,.1f} 万" if state.latest_big_net is not None else "N/A"
    delta_s = f"{state.recent_big_net_delta/10000:+,.1f} 万" if state.recent_big_net_delta is not None else "—"
    mid_s   = f"{state.latest_mid_net/10000:+,.1f} 万" if state.latest_mid_net is not None else "N/A"
    small_s = f"{state.latest_small_net/10000:+,.1f} 万" if state.latest_small_net is not None else "N/A"
    ask_s   = f"{state.latest_ask_depth:,.0f} 股" if state.latest_ask_depth is not None else "N/A"
    imb_s   = f"{state.latest_imbalance:+.3f}" if state.latest_imbalance is not None else "N/A"
    c10_s   = f"{state.weighted_cost:.1f}" if state.weighted_cost else "N/A"
    c20_s   = f"{state.weighted_cost_mid:.1f}" if state.weighted_cost_mid else "N/A"
    head_lines = [
        f"[①] HKEX占比 {hkex_s}%   动能 {mom_s}",
        f"[②] 大单净流入 {big_s}   近Δ {delta_s}",
        f"      中单(拆单) {mid_s}",
        f"      散单(散户) {small_s}",
        f"[③] 卖盘深度 {ask_s}   失衡 {imb_s}",
        f"      空头成本 10日 {c10_s} / 20日 {c20_s}",
    ]

    now = datetime.datetime.now().strftime("%m-%d %H:%M")
    bar58 = "═" * DASH_INNER_W
    print(f"\n╔{bar58}╗")
    print(_box(f"{STOCK_NAME} {STOCK_CODE}.HK  做空仪表盘  {now}"))
    print(f"╠{bar58}╣")
    print(_box(f"最新价 {price_s}"))
    print(f"╠{bar58}╣")
    for _l in head_lines:
        print(_box(_l))
    print(f"╠{bar58}╣")
    for _l in l2_lines:
        print(_box(_l))
    # 主力嫌疑命中明细（≥轻微控盘才展开，避免市场化票刷屏）
    if state.latest_mf_score >= 20:
        for s in state.latest_mf_sigs:
            print(f"║   ★ {s}")
    # 派发对峙·散户接盘（迭代三十九）：报价层 vs 成交层打架的显式警示，最高优先级展示
    if state.standoff_tag:
        print(f"║  ⚖ {state.standoff_tag}")

    print(f"╠{bar58}╣")
    print(_box(f"【逼空风险】[{bar(squeeze_score)}] {squeeze_score:3d}/100"))
    print(_box(sq_level.strip()))

    if squeeze_signals:
        for s in squeeze_signals:
            print(f"║   ⚠ {s}")

    # Bug 19：拆分展示做空总分 = 主信号（盘中此刻）+ 日级背景（整日固定）
    # 用户应关注主信号分，避免被日级地板 48 分顶高的总分误导
    score_split = f"主{state.main_signal_score} 背{state.support_score}"
    print(f"╠{bar58}╣")
    print(_box(f"【做空入场】[{bar(short_score)}] {short_score:3d}/100 ({score_split})"))
    print(_box(signal_label.strip()))

    if short_sigs:
        for s in short_sigs:
            print(f"║   → {s}")

    if state.in_position:
        print(f"╠{bar58}╣")
        print(_box(f"【持仓离场风险】紧迫度 {exit_urgency:3d}/100  {exit_label.strip()}"))
        for r in exit_reasons:
            print(f"║   !! {r}")

    # ── 手动持仓面板（--held-short 模式）────────────────────
    if cover_advice is not None and held is not None and state.last_price is not None:
        price = state.last_price

        action_label = {
            "COVER_ALL":  "!! 建议立即全部平仓 !!",
            "COVER_HALF": "!  建议减仓 50%     !",
            "STOP_LOSS":  "!! 触及止损，立即平仓!!",
            "HOLD":       "   持仓安全，继续观望 ",
        }.get(cover_advice.action, "─────────────────────")

        pnl_arrow = "▲" if cover_advice.pnl >= 0 else "▼"
        cost_gap  = held.stop - price

        def bar(v: int, w: int = 20) -> str:
            n = min(int(v / 100 * w), w)
            return "█" * n + "░" * (w - n)

        print(f"╠{bar58}╣")
        print(_box(f"【手动持仓平仓建议】成本 {held.entry_price:.1f} × {held.qty:,} 股"))
        print(f"╠{bar58}╣")
        print(_box(f"浮动盈亏  : {pnl_arrow} {abs(cover_advice.pnl):,.0f} HKD  ({cover_advice.pnl_pct:+.2f}%)"))
        print(_box(f"目标①    : {held.target1:.1f}  (-{held.target1_pct*100:.1f}%)  "
                   f"{'✓已触发' if held.target1_done else '○未触发'}"))
        print(_box(f"目标②    : {held.target2:.1f}  (-{held.target2_pct*100:.1f}%)"))
        print(_box(f"止损线    : {held.stop:.1f}  (+{held.stop_pct*100:.1f}%)  距当前 {cost_gap:.1f} HKD"))
        print(f"╠{bar58}╣")
        print(_box(f"平仓紧迫度 [{bar(cover_advice.urgency)}] {cover_advice.urgency:3d}/100"))
        print(_box(action_label.strip()))
        for r in cover_advice.reasons:
            prefix = "!!" if cover_advice.action in ("COVER_ALL", "STOP_LOSS") else " →"
            print(f"║ {prefix} {r}")

    print(f"╚{bar58}╝")


# ═══════════════════════════════════════════════════════════
# 十一、主监控循环
# ═══════════════════════════════════════════════════════════
def run_monitor(held_short: Optional[HeldShort] = None):
    if held_short:
        log.info(
            f"启动监控（持仓模式）: {SYMBOL} | "
            f"成本={held_short.entry_price} 数量={held_short.qty}股 | "
            f"止损={held_short.stop} 目标①={held_short.target1} ②={held_short.target2}"
        )
    else:
        log.info(f"启动监控: {SYMBOL}，实时轮询 {REALTIME_INTERVAL}s")
    log.info("提示：启动后输入 'p' 回车可切换持仓状态（标记是否持有空仓）")
    conn  = init_db(DB_PATH)
    state = MonitorState()
    if held_short:
        state.in_position = True     # 自动标记持仓
    ctx   = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)

    # 核心订阅（QUOTE/ORDER_BOOK/TICKER）——TICKER 逐笔在 LV1 即可用，是 Tier-1 冰山数据源。
    ret, err = ctx.subscribe(
        [SYMBOL], [SubType.QUOTE, SubType.ORDER_BOOK, SubType.TICKER]
    )
    if ret != RET_OK:
        log.warning(f"核心订阅失败: {err}（将使用快照模式；逐笔冰山检测可能不可用）")

    # 经纪队列（Tier-2 足迹）必须单独订阅：LV1 权限不支持，且若并入上面那批会触发
    # 整批原子失败（连 TICKER 都订不上，拖垮 Tier-1）。失败则降级为纯 Tier-1。
    ret_brk, err_brk = ctx.subscribe([SYMBOL], [SubType.BROKER])
    broker_available = (ret_brk == RET_OK)
    if not broker_available:
        log.info(f"经纪队列订阅不可用（{err_brk}）；Tier-2 足迹关闭，仅用 Tier-1 逐笔冰山")

    try:
        while True:
            now = datetime.datetime.now()
            today_str = now.date().isoformat()

            # ── 每日 HKEX 爬取（收盘后） ──────────────────────────
            # 独立于交易时段判断，因为爬取窗口（17:00 后）本身就在盘后
            if (state.last_hkex_date != today_str
                    and now.hour >= HKEX_FETCH_HOUR):
                ratio = fetch_hkex_and_store(conn, ctx, now.date())
                if ratio is not None:
                    state.last_hkex_date    = today_str
                    state.latest_hkex_ratio = round(ratio, 4)

            # ── 交易时段守门：非连续交易时段直接跳过打分 ──────────
            # 避免在 CAS（16:00-16:10）/ 集合竞价 / 午休时把集合挂单当真实信号
            if not _is_trading_hours(now):
                phase = _trading_phase_label(now)
                log.info(f"[{phase}] {now.strftime('%H:%M:%S')} 跳过打分（数据非连续交易语义）")
                time.sleep(REALTIME_INTERVAL)
                continue

            # ── 获取最新价格并存入历史 ─────────────────────────────
            ret_q, qdata = ctx.get_stock_quote(code_list=[SYMBOL])
            if ret_q == RET_OK and not qdata.empty:
                _lp = float(qdata.iloc[0]["last_price"])
                # Futu 网络抖动时 last_price 偶尔回 0/NaN，直接丢弃本轮报价
                if math.isfinite(_lp) and _lp > 0:
                    state.last_price = _lp
                    db_save_price(conn, now.isoformat(timespec="seconds"), _lp)
                else:
                    log.warning(f"[报价异常] last_price={_lp!r}，跳过本轮价格更新")

            # ── 信号②：资金流向 ───────────────────────────────────
            cf = fetch_capital_flow(ctx, conn)
            if cf:
                state.latest_big_net = cf["big_net"]
                state.latest_mid_net = cf["mid_net"]
                state.latest_small_net = cf["small_net"]
                state._capital_fail_count = 0
                # 近期 Δ：capital_flow 表已按 update_time 去重，相邻两行约 1 分钟跨度
                _bn_recent = db_get_recent_big_net(conn, 2)
                if len(_bn_recent) >= 2:
                    state.recent_big_net_delta = _bn_recent[0] - _bn_recent[1]
                else:
                    state.recent_big_net_delta = None
            else:
                state._capital_fail_count += 1

            # ── 信号③：摆盘深度 ───────────────────────────────────
            ob = fetch_order_book(ctx, conn)
            if ob:
                state.latest_ask_depth = ob["ask_depth"]
                state.latest_imbalance = ob["imbalance"]
                state.latest_ob = ob          # 十档形态展示（迭代三十八 #1）
                state._orderbook_fail_count = 0
            else:
                state._orderbook_fail_count += 1

            # ── L2 逐笔成交（Tier 1 冰山检测数据源；增强信号，失败不阻断）──
            tk = fetch_ticks(ctx, conn, ob)
            if tk:
                state.latest_tick = tk
            # 近 ICEBERG_WINDOW 轮逐笔聚合（迭代三十八 #4）：单轮 buy/sell 噪音极大
            # （常见 0/0 ∞、40× 等单帧极值），仪表盘改用窗口聚合展示。每行
            # = (sell_vol, buy_vol, price_first, price_last, best_bid_vol, best_ask_vol)。
            _trows = db_get_recent_ticks(conn, ICEBERG_WINDOW)
            if _trows:
                _wbuy  = sum(r[1] for r in _trows)
                _wsell = sum(r[0] for r in _trows)
                # 主动大单近 N 轮累计净（迭代四十一）：薄盘单笔大单主宰单轮、比值剧烈翻面，
                # 累计净额才看得清真实方向。
                _clb, _cls, _ccb, _ccs = db_get_large_tick_cum(conn, LARGE_TICK_CUM_WINDOW)
                state.tick_window = {
                    "rounds": len(_trows), "buy": _wbuy, "sell": _wsell,
                    "lbuy":  (state.latest_tick or {}).get("large_buy_vol", 0.0),
                    "lsell": (state.latest_tick or {}).get("large_sell_vol", 0.0),
                    "lbuy_cnt":  (state.latest_tick or {}).get("large_buy_cnt", 0),
                    "lsell_cnt": (state.latest_tick or {}).get("large_sell_cnt", 0),
                    "cum_lbuy": _clb, "cum_lsell": _cls,
                    "cum_lbuy_cnt": _ccb, "cum_lsell_cnt": _ccs,
                }
            # ── L2 经纪队列（Tier 2 足迹；仅在 BROKER 订阅可用时拉取，LV1 自动跳过）──
            if broker_available:
                bq = fetch_broker_queue(ctx, conn)
                if bq:
                    state.latest_broker = bq

            # ── 数据新鲜度守门：核心 API 连续失败 → 跳过打分 ──────
            # Futu API 失败时 state 旧值会被保留，若不守门则评分基于陈旧快照
            # （实盘 2026-05-18 09:36:56-09:37:45 案例：3 轮 API 失败，
            # 评分用 09:36:30 的冻结数据，错过 +3.5% 拉升预警）
            if (state._capital_fail_count >= API_FAIL_TOLERANCE_ROUNDS
                    or state._orderbook_fail_count >= API_FAIL_TOLERANCE_ROUNDS):
                log.warning(
                    f"[API 失效] 资金流失败 {state._capital_fail_count} 轮 / "
                    f"摆盘失败 {state._orderbook_fail_count} 轮 ≥ "
                    f"{API_FAIL_TOLERANCE_ROUNDS}，跳过打分（避免基于陈旧快照）"
                )
                time.sleep(REALTIME_INTERVAL)
                continue

            # ── 数据停滞检测：价格 + 大单累计连续 N 轮未变 → 跳过 ─
            # Futu 在市场暂停 / 网络断流 / 极低流动性时会返回相同快照，
            # 此时打分基于陈旧数据，输出的"持续偏空/偏多"等信号无意义。
            # 优化3：但"主力钉价"也是价格+大单冻结——若本轮仍有主动成交（逐笔有量），
            # 说明市场在交易、只是价格被焊住，不是断流，不应跳过（控盘钉价正是该警觉之时）。
            _tick_active = bool(tk) and (
                (tk.get("buy_vol", 0) or 0) + (tk.get("sell_vol", 0) or 0)) > 0
            if (state.last_price is not None
                    and state.last_price == state._prev_price
                    and state.latest_big_net == state._prev_big_net
                    and not _tick_active):
                state._stale_count += 1
            else:
                state._stale_count = 0
                state._prev_price   = state.last_price
                state._prev_big_net = state.latest_big_net

            if state._stale_count >= STALE_DATA_ROUNDS:
                log.warning(
                    f"[数据停滞] 价格 {state.last_price} + 大单累计连续 "
                    f"{state._stale_count} 轮未更新，跳过打分（市场暂停/网络异常？）"
                )
                time.sleep(REALTIME_INTERVAL)
                continue

            # ── 大单累计单独停滞检测（Bug 14 / Bug 25）──────────────
            # 价格在动但大单 cumulative 多轮不变 → Futu 该数值陈旧（无新大单或
            # API 复用旧值），此时"大单净流入持续为负"维度的+15 分基于陈旧
            # 状态，不应继续计入。其它维度（盘口/价格行为）照常打分。
            # Bug 25：精确相等检测会被 Futu 在 2~3 个缓存值间的小幅抖动反复重置，
            # 导致冻结凑不满轮数。改为锚点带容差——新值落在锚点 ±BAND 内即视为
            # 未实质变化（_prev_big_net_only 复用为锚点，仅在突破带宽时才更新）。
            # Bug 28：BAND 用绝对额(HKD)而非 ±%×累计额——后者随累计膨胀会吞掉真实流动。
            _bn = state.latest_big_net
            _anchor = state._prev_big_net_only
            if (_bn is not None and _anchor is not None
                    and abs(_bn - _anchor) <= BIG_NET_STALE_BAND_HKD):
                state._big_net_stale_count += 1
            else:
                state._big_net_stale_count = 0
                state._prev_big_net_only   = _bn
            big_net_stale = state._big_net_stale_count >= BIG_NET_STALE_ROUNDS
            if big_net_stale:
                log.info(
                    f"[大单停滞] 累计净额冻结于 {state.latest_big_net/10000:+,.1f} 万"
                    f"已 {state._big_net_stale_count} 轮，'大单净流入'维度本轮跳过"
                )

            # ── HKEX 历史卖空动能分析（日级，每轮都算）────────────
            hkex_support, hkex_squeeze_risk, hkex_sigs, hkex_stats = \
                analyze_hkex_short_momentum(conn, state.last_price)
            if hkex_stats:
                state.weighted_cost     = hkex_stats.get("weighted_cost")
                state.weighted_cost_mid = hkex_stats.get("weighted_cost_mid")
                state.momentum_ratio    = hkex_stats.get("momentum_ratio")
                state.volume_surge      = hkex_stats.get("volume_surge")

            # ── 逼空评分（含 HKEX 成本线风险项 + 价格反弹维度）──
            s1, s1_support, sg1 = analyze_short_ratio_trend(conn, state)
            s2, sg2 = analyze_capital_flow(conn)
            s3, sg3 = analyze_order_book(conn, state.latest_ask_depth or 0)
            s_rev, sg_rev = analyze_price_reversal(conn, state.last_price)
            # 资金结构背离（大单 vs 中小单）：正值=出货确认（支撑做空），负值=逼空折扣
            s_struct, sg_struct = analyze_capital_structure(conn)
            # 散户撤退（接盘耗尽）→ 支撑做空
            s_retreat, sg_retreat = analyze_retail_retreat(conn)
            # 派发模式三条件确认（独立主信号，可解开 BLOCKED 安全门）
            s_dist, dist_confirmed, sg_dist = analyze_distribution_mode(conn)
            # 粘滞机制（迭代二十五）：本轮触发 → 重置 N 轮窗口；本轮未触发但 sticky
            # > 0 → 保持 confirmed，分数减半。避免大单累计瞬时回补让派发判定失效。
            if dist_confirmed:
                state._distribution_sticky_left = DISTRIBUTION_STICKY_ROUNDS
            elif state._distribution_sticky_left > 0:
                state._distribution_sticky_left -= 1
                dist_confirmed = True
                s_dist = DISTRIBUTION_STICKY_SCORE
                sg_dist = [
                    f"派发模式粘滞中（剩 {state._distribution_sticky_left} 轮），"
                    f"维持 confirmed 但分数减半 [+{s_dist}分]"
                ]
            # 资金效率（大单流入但价格无响应）→ 逼空折扣
            s_eff, sg_eff = analyze_capital_efficiency(conn, state.last_price)
            # 卖而不跌（大单+中单净流出但价格守位/反弹）→ 被动吸筹/诱空，抬升逼空风险
            s_snd, sg_snd = analyze_sell_no_drop(conn, state.last_price)
            # L2 逐笔冰山（执行级）：吸筹 → 抬升逼空(s_ice_sq)，派发 → 支撑做空(s_ice_sup)
            s_ice_sq, s_ice_sup, sg_ice = analyze_iceberg_absorption(conn)
            # L2 经纪足迹（Tier 2）：单一席位反复占据最优档，仅在同向冰山已触发时加成
            s_brk_sq, s_brk_sup, sg_brk = analyze_broker_footprint(
                conn, s_ice_sq, s_ice_sup)
            # L2 面板快照（迭代三十三）：供仪表盘每轮展示当前微观结构状态
            state.latest_ice_sq, state.latest_ice_sup = s_ice_sq, s_ice_sup
            state.latest_brk_sq, state.latest_brk_sup = s_brk_sq, s_brk_sup
            # 主力嫌疑分（控盘特征筛查，纯背景画像，不参与下方任何打分）
            (state.latest_mf_score, state.latest_mf_label,
             state.latest_mf_sigs, state.latest_mf_tags) = \
                analyze_main_force_control(conn)
            # 散户 FOMO（窄幅震荡中加速流入）→ 支撑做空
            s_fomo, sg_fomo = analyze_retail_fomo(conn, state.last_price)
            # 中单拆单（大单冻结期间 mid_net 节奏稳定）→ 支撑做空
            s_split, sg_split = analyze_mid_split(conn, big_net_stale)

            squeeze_damper = (-s_struct if s_struct < 0 else 0) + (-s_eff if s_eff < 0 else 0)
            squeeze_score = max(
                min(s1 + s2 + s3 + hkex_squeeze_risk + s_rev + s_snd + s_ice_sq
                    + s_brk_sq - squeeze_damper, 100),
                0,
            )
            # sg1 中可能包含「支撑做空」语义（价格下行时的反转计分），分流展示
            sg1_squeeze = [s for s in sg1 if "支撑做空" not in s]
            sg1_support = [s for s in sg1 if "支撑做空" in s]
            # sg_struct 中：支撑做空 → 支撑面板；其它（折扣/全档同向）→ 逼空面板
            sg_struct_support = [s for s in sg_struct if "支撑做空" in s]
            sg_struct_squeeze = [s for s in sg_struct if "支撑做空" not in s]
            # 资金效率信号属于"折扣"语义，归入逼空面板
            squeeze_signals = (sg1_squeeze + sg2 + sg3 + sg_rev + sg_struct_squeeze
                               + sg_eff + sg_snd + (sg_ice if s_ice_sq else [])
                               + (sg_brk if s_brk_sq else [])
                               + [s for s in hkex_sigs if "逼空" in s or "亏损" in s])

            # ── 维护近 N 轮逼空评分历史（供做空安全门取峰值）─────
            state.recent_squeeze_scores.append(squeeze_score)
            if len(state.recent_squeeze_scores) > SHORT_SQUEEZE_LOOKBACK:
                state.recent_squeeze_scores = \
                    state.recent_squeeze_scores[-SHORT_SQUEEZE_LOOKBACK:]
            # 用第二高（trimmed max）代替最高，对单次 spike 鲁棒：
            # 一次诱多刷盘把分数冲到 33 后立即回落，不应锁死后续 60 秒入场窗口。
            # 仅当 ≥2 轮真实持续高位时才会触发 BLOCKED（实盘 2026-05-15 14:17:05
            # 案例：[33,33,33,25] 仍 BLOCKED 正确；[33,8,0,0] 不再误 BLOCKED）。
            _sorted = sorted(state.recent_squeeze_scores)
            recent_max_squeeze = _sorted[-2] if len(_sorted) >= 2 else _sorted[-1]

            # ── 做空入场评分（HKEX 动能分叠加）──────────────────
            short_score, short_signal, short_sigs = analyze_short_entry(
                conn, squeeze_score,
                state.last_price,
                state.latest_ask_depth or 0,
                state.latest_imbalance or 0,
                recent_max_squeeze=recent_max_squeeze,
                big_net_stale=big_net_stale,
                distribution_score=s_dist,
                distribution_confirmed=dist_confirmed,
                distribution_sigs=sg_dist,
            )
            # Bug 13 防护：出货式拉升横向交叉验证（仅追加支撑分，不升级 BLOCKED/CAUTION）
            pump_support, pump_sigs = analyze_distribution_pump(
                conn, state.last_price, hkex_stats
            )
            # 拆分展示（Bug 19）：主信号分（维度 1-5）vs 日级背景分（HKEX + 成本线 + pump）
            main_signal_score = short_score   # analyze_short_entry 返回的就是主信号分
            struct_support = s_struct if s_struct > 0 else 0
            support_score = (hkex_support + s1_support + pump_support + struct_support
                             + s_retreat + s_fomo + s_split + s_ice_sup + s_brk_sup)
            short_score = min(main_signal_score + support_score, 100)
            short_sigs  = (short_sigs
                           + [s for s in hkex_sigs if "支撑做空" in s]
                           + sg1_support
                           + pump_sigs
                           + sg_struct_support
                           + sg_retreat + sg_fomo + sg_split
                           + (sg_ice if s_ice_sup else [])
                           + (sg_brk if s_brk_sup else []))
            # 仅在 HOLD 时根据 HKEX 叠加分升级；BLOCKED/CAUTION 由 analyze_short_entry
            # 内的安全门和 failsafe 决定，不允许在此被覆盖回 ENTRY，避免诱空陷阱。
            if short_signal == "HOLD":
                if short_score >= SHORT_ENTRY_MIN:
                    short_signal = "ENTRY"
                elif short_score >= int(SHORT_ENTRY_MIN * 0.6):
                    short_signal = "CAUTION"
            # Bug 18: 升级到 ENTRY 后必须再走一次 failsafe。analyze_short_entry 内
            # 部基于主信号 score 判定 sig_type，当合并 support 才达到入场门槛时，
            # 内部 sig_type 始终是 HOLD，Failsafe 1/2 永不触发 → imbalance>0.30
            # 的极端买盘环境仍输出 ENTRY。本调用补回该检查。
            # 迭代二十五：传 distribution_confirmed + ask_depth 启用翻转 failsafe 豁免
            short_score, short_signal, short_sigs = apply_short_entry_failsafes(
                conn, short_score, short_signal,
                state.latest_imbalance or 0, short_sigs,
                distribution_active=dist_confirmed,
                ask_depth=state.latest_ask_depth,
                current_price=state.last_price,
                big_net_stale=big_net_stale,
            )
            # 主力嫌疑分联动安全门（优化2）：强控盘且属"逼空型"时，做空 ENTRY 降级 CAUTION。
            # 仅针对真正套牢空头的两种控盘——① 机构护盘压买一（吸筹/托底，价格不跌反弹，
            # 追空被逼）② 价格硬钉扎（焊死防守，做空无下行边际）。机构压卖一(派发)不在此列
            # ——那是合法做空环境，且已有摆盘失衡 failsafe 兜底，不重复降级。仅降级不解禁。
            _mf_pin  = state.latest_mf_tags.get("pin", 0)
            _dom_side = state.latest_mf_tags.get("dom_side")
            _squeeze_type = (_dom_side == "买一") or (_mf_pin >= MF_PIN_GATE)
            if (short_signal == "ENTRY"
                    and state.latest_mf_score >= MF_CONTROL_GATE
                    and _squeeze_type):
                short_signal = "CAUTION"
                _why = "机构护盘压买一(吸筹托底)" if _dom_side == "买一" else f"价格硬钉扎(钉扎分 {_mf_pin})"
                short_sigs.append(
                    f"⚠ 主力嫌疑 {state.latest_mf_score}/100 强控盘·{_why}，"
                    f"控盘逼空风险，ENTRY 降级 CAUTION"
                )
            # 背景注水门（Bug 24）：support_score 全是日级静态量（HKEX 成本线/动能比/
            # 爆量），整日恒定、与盘中此刻无关。若 ENTRY 仅靠背景堆出（主信号分不足），
            # 实为"已跌透"的滞后画像而非新鲜入场触发。实盘 2026-06-05 13:05-13:34 主
            # 信号 27~30 + 背景 43 反复输出 73~98 假 ENTRY。要求至少一个强盘中维度兜底，
            # 否则 ENTRY 降级 CAUTION。仅降级不解禁，不与上方 BLOCKED/CAUTION 冲突。
            if (short_signal == "ENTRY"
                    and main_signal_score < SHORT_MAIN_FRESH_MIN):
                short_signal = "CAUTION"
                short_sigs.append(
                    f"⚠ 主信号分仅 {main_signal_score}（< {SHORT_MAIN_FRESH_MIN}），"
                    f"做空总分由日级背景({support_score})堆出而非盘中新鲜触发，"
                    f"ENTRY 降级 CAUTION 防背景注水"
                )
            state.short_score  = short_score
            state.short_signal = short_signal
            state.main_signal_score = main_signal_score
            state.support_score     = support_score
            # 派发对峙·散户接盘检测（迭代三十九，信息性提示，不改评分）
            state.standoff_tag = detect_distribution_standoff(conn, state)

            # ── 写入共享评分供 paper_trader 读取 ─────────────────
            db_write_monitor_state(
                conn, squeeze_score, short_score, short_signal,
                state.last_price, state.latest_ask_depth,
                state.latest_imbalance, state.latest_big_net,
            )

            # ── 持仓离场风险（仅在持仓时评估）───────────────────────
            exit_urgency, exit_reasons = 0, []
            if state.in_position:
                exit_urgency, exit_reasons = analyze_short_exit(
                    conn, squeeze_score
                )
                state.exit_urgency = exit_urgency

            # ── 手动持仓平仓建议（--held-short 模式）─────────────
            cover_advice = None
            if held_short is not None and state.last_price is not None:
                cover_advice = evaluate_cover_signal(
                    held_short, state.last_price,
                    squeeze_score, state.latest_imbalance or 0,
                    conn,
                )
                # 触发了减仓建议时记录信号
                if cover_advice.action in ("COVER_ALL", "STOP_LOSS"):
                    db_save_signal(
                        conn, f"COVER_{cover_advice.action}",
                        cover_advice.reasons[0], cover_advice.urgency,
                    )
                elif cover_advice.action == "COVER_HALF" and not held_short.target1_done:
                    db_save_signal(
                        conn, "COVER_HALF",
                        cover_advice.reasons[0], cover_advice.urgency,
                    )

            # ── 打印仪表盘 ────────────────────────────────────────
            print_dashboard(
                state, squeeze_score, squeeze_signals,
                short_score, short_signal, short_sigs,
                exit_urgency, exit_reasons,
                cover_advice=cover_advice,
                held=held_short,
            )
            _imb_str = f"{state.latest_imbalance:.3f}" if state.latest_imbalance is not None else "N/A"
            _retail_net = None
            if state.latest_mid_net is not None and state.latest_small_net is not None:
                _retail_net = state.latest_mid_net + state.latest_small_net
            _retail_str = f"{_retail_net/10000:+,.1f}万" if _retail_net is not None else "N/A"
            log.info(
                f"逼空={squeeze_score} | 做空={short_score}({short_signal}) | "
                f"离场紧迫={exit_urgency} | 持仓={state.in_position} | "
                f"大单净={state.latest_big_net} | 中小单={_retail_str} | "
                f"卖深={state.latest_ask_depth} | 失衡={_imb_str}"
            )

            time.sleep(REALTIME_INTERVAL)

    except KeyboardInterrupt:
        log.info("用户中断，退出监控。")
    finally:
        ctx.close()
        conn.close()


# ═══════════════════════════════════════════════════════════
# 十二、辅助命令
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


def cmd_backfill(days: int = 40):
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
    import argparse as _ap

    _p = _ap.ArgumentParser(
        description="MINIMAX-W 做空监控系统",
        formatter_class=_ap.RawDescriptionHelpFormatter,
        epilog="""
子命令：
  (无)       启动实时监控
  backfill   补抓历史 HKEX 卖空数据
  signals    查看近期触发信号
  export     导出数据 CSV

持仓模式（监控的同时提供平仓建议）：
  python3 short_squeeze_monitor.py --held-short 865 --held-qty 1000
  python3 short_squeeze_monitor.py --held-short 865 --held-qty 1000 --stop-pct 3.5 --t1-pct 1.5 --t2-pct 3.0
"""
    )
    _p.add_argument("--stock", "-s", default=DEFAULT_STOCK,
                    metavar="CODE",
                    help=f"股票代码，支持: {', '.join(STOCKS)}（默认 {DEFAULT_STOCK}）")
    _p.add_argument("cmd", nargs="?", default="monitor",
                    choices=["monitor", "backfill", "signals", "export"],
                    help="子命令（默认 monitor）")
    _p.add_argument("--held-short", type=float, default=0,
                    metavar="PRICE", help="手动建仓成本价，启用持仓平仓建议面板")
    _p.add_argument("--held-qty",   type=int,   default=1000,
                    metavar="QTY",   help="持仓股数（默认 1000）")
    _p.add_argument("--stop-pct",   type=float, default=3.5,
                    metavar="PCT",   help="止损百分比，默认 3.5（%%）")
    _p.add_argument("--t1-pct",     type=float, default=1.5,
                    metavar="PCT",   help="第一目标利润百分比，默认 1.5（%%）")
    _p.add_argument("--t2-pct",     type=float, default=3.0,
                    metavar="PCT",   help="第二目标利润百分比，默认 3.0（%%）")

    _args = _p.parse_args()

    # ── 应用股票配置 ──────────────────────────────────────────
    _stock_cfg = STOCKS.get(_args.stock)
    if not _stock_cfg:
        print(f"未知股票代码 {_args.stock!r}，支持: {', '.join(STOCKS)}", file=sys.stderr)
        sys.exit(1)
    SYMBOL            = _stock_cfg["symbol"]
    STOCK_CODE        = _stock_cfg["stock_code"]
    STOCK_NAME        = _stock_cfg["name"]
    DB_PATH           = _stock_cfg["db_path"]
    REALTIME_INTERVAL = _stock_cfg["poll_interval"]

    # 构建持仓对象
    _held = None
    if _args.held_short > 0:
        _held = HeldShort(
            entry_price  = _args.held_short,
            qty          = _args.held_qty,
            stop_pct     = _args.stop_pct   / 100,
            target1_pct  = _args.t1_pct     / 100,
            target2_pct  = _args.t2_pct     / 100,
        )

    if _args.cmd == "monitor":
        run_monitor(held_short=_held)
    elif _args.cmd == "signals":
        cmd_signals()
    elif _args.cmd == "export":
        cmd_export()
    elif _args.cmd == "backfill":
        cmd_backfill()
