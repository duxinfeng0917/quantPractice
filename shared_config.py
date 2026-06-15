"""
shared_config.py
================
paper_trader.py 与 short_squeeze_monitor.py 共用的常量。
修改此文件即可同步影响两个脚本，避免两处维护不一致。
"""

# HKEX 加权空头成本线窗口（交易日数）
# 10日 = 约两个完整交易周，是港股空头分析的标准窗口，能更好地平衡灵敏度与稳定性。
# 两个脚本必须使用同一窗口，否则会对"空头是否亏损"得出相反结论。
HKEX_COST_WINDOW = 10

# ── 股票配置表 ──────────────────────────────────────────────
# 新增股票只需在此追加一条记录，脚本通过 --stock CODE 选择。
STOCKS: dict = {
    "00100": {
        "symbol":        "HK.00100",   # Futu OpenAPI 格式
        "stock_code":    "00100",       # HKEX 爬虫用（无前导零对应 100）
        "name":          "MINIMAX-W",
        "db_path":       "short_data.db",
        "poll_interval": 15,            # 实时轮询间隔（秒）
    },
    "02513": {
        "symbol":        "HK.02513",
        "stock_code":    "02513",
        "name":          "智谱AI",
        "db_path":       "short_data_02513.db",
        "poll_interval": 60,
    },
    "06082": {
        "symbol":        "HK.06082",
        "stock_code":    "06082",
        "name":          "壁仞科技",
        "db_path":       "short_data_06082.db",
        "poll_interval": 30,
    },
    "06656": {
        "symbol":        "HK.06656",
        "stock_code":    "06656",
        "name":          "思格新能",
        "db_path":       "short_data_06656.db",
        "poll_interval": 30,
    },
    "00981": {
        "symbol":        "HK.00981",
        "stock_code":    "00981",
        "name":          "中芯国际",
        "db_path":       "short_data_00981.db",
        "poll_interval": 30,
        # 中芯是大盘高流动股，成交额远超 00100、盘口动辄上万股 → 必须放大量级阈值，
        # 否则 HKD 门槛被秒击穿(噪声)、盘薄维度永不触发(控盘画像作废)。
        # 下列为「占位示例，未经回测」——启用前务必 backtest_signals.py 标定后再填真值：
        # "flow_scale":  8.0,   # 所有 HKD 绝对额阈值 ×8
        # "depth_scale": 10.0,  # 盘薄/挂量股数阈值 ×10
        # "calibration": {"MF_THIN_DEPTH_SHARES": 300_000},  # 个别项再精修(覆盖缩放结果)
    },
}
DEFAULT_STOCK = "00100"

# ── Per-stock 阈值校准 ───────────────────────────────────────
# short_squeeze_monitor.py 顶部一批"绝对量级"阈值是按 00100(~400 HKD 薄盘可做空)标定的，
# 换标的会系统性失真(详见 DEVLOG 迭代四十九)。两个缩放因子覆盖绝大多数情况，均默认 1.0(=00100)：
#   flow_scale  : 缩放所有"HKD 绝对额"阈值 —— 随标的每分钟成交额线性放缩
#                 (例：成交额是 00100 的 3 倍 → flow_scale=3.0；1/5 → 0.2)
#   depth_scale : 缩放"股数"类阈值(盘薄/挂量) —— 随流通盘 & 1/股价放缩
#                 (例：低价厚盘股盘口动辄几万股 → depth_scale 调大)
# 在 STOCKS[code] 里加 "flow_scale"/"depth_scale" 即可。需逐项微调时再加
# "calibration": {KEY: 显式值}(在缩放之后生效，覆盖该项)。
# ⚠ 标定务必先跑 backtest_signals.py 验证，勿凭感觉拍 —— 跨标的预测力本就弱(迭代四十五 0/3)。
#
# CALIBRATABLE：注册哪些 monitor 常量可被 per-stock 覆盖，及其缩放类别(flow=按成交额 / depth=按股数)。
# 白名单作用：config 里写错 KEY 不会静默篡改无关全局，而是被 apply_stock_calibration 警告跳过。
# 校准基准：00100(参考标的)的典型量级。`probe-scale` 命令用 target/baseline 算建议缩放因子：
#   flow_scale  ≈ 目标每分钟成交额 ÷ turnover_per_min
#   depth_scale ≈ 目标卖盘十档深度中位 ÷ ask_depth_shares
# ⚠ 下面是基于 00100 实盘日志的估计值；可用 `short_squeeze_monitor.py probe-scale --stock 00100`
#   在盘中重新实测校正(probe 会打印实测原始值)。
CALIBRATION_BASELINE: dict[str, float] = {
    "turnover_per_min": 3_000_000,   # 00100 每分钟成交额(HKD)估计
    "ask_depth_shares": 2_000,       # 00100 卖盘十档总深度中位(股)估计
}

CALIBRATABLE: dict[str, str] = {
    # —— HKD 绝对额(随成交额放缩) → flow ——
    "BIG_NET_DELTA_THRESHOLD":        "flow",  # 单轮 Δ 显著买入门槛
    "SHORT_BLOCK_BIGFLOW_THRESHOLD":  "flow",  # 安全门放行：大单持续净流出
    "CAPITAL_STRUCT_SMALL_THRESHOLD": "flow",  # 中小单净流出判定
    "RETAIL_RETREAT_MIN_PEAK":        "flow",  # 散户撤退检测最小峰值
    "CAPITAL_EFFICIENCY_MIN_INFLOW":  "flow",  # 资金效率：大单 Δ 评估门槛
    "SELL_NO_DROP_MIN_OUTFLOW":       "flow",  # 卖而不跌：净流出门槛
    "ICEBERG_MIN_NOTIONAL":           "flow",  # 冰山：窗口主导方成交额门槛
    "LARGE_TICK_NOTIONAL":            "flow",  # 主动大单：单笔成交额门槛
    "RETAIL_FOMO_MIN_INFLOW":         "flow",  # 散户 FOMO：净流入门槛
    "MID_SPLIT_MIN_FLOW":             "flow",  # 中单拆单：单轮 Δ 评估门槛
    "MID_ACCUM_MIN_DELTA":            "flow",  # 中单吸筹：加速买入增量
    "MID_ACCUM_MIN_LEVEL":            "flow",  # 中单吸筹：累计显著多头水位
    "DISTRIBUTION_BIGNET_MIN_DEPTH":  "flow",  # 派发：大单日内最低值深度门槛
    "DISTRIBUTION_MIDSMALL_MIN_PEAK": "flow",  # 派发：中小单峰值门槛
    # —— 股数(随流通盘/股价放缩) → depth ——
    "MF_THIN_DEPTH_SHARES":           "depth", # 主力嫌疑：盘薄易控股数
    "THIN_ASK_DEPTH_FOR_FLIP_SKIP":   "depth", # 翻转 failsafe 豁免：薄盘股数
}

# ── 候选股扫描池（watchlist_scanner.py 用）─────────────────────
# 仅需股票代码（5 位数字字符串），name/上市日期等由 Futu API 在扫描时拉取。
# 添加新候选只需在此追加代码，不需要完整的 STOCKS 配置。
# 默认包含已监控的 STOCKS 全集 + 用户感兴趣的港股 W 类 / 次新 AI 类候选。
WATCHLIST: list[str] = [
    # ─ 已监控（继承 STOCKS） ─
    "00100",  # MINIMAX-W
    "02513",  # 智谱AI
    "06082",  # 壁仞科技
    "06656",  # 思格新能
    # ─ 候选池（未监控，待扫描评分） ─
    # 注意：以下代码仅基于"港股 W 类 / 次新 AI 题材"标准添加，请用富途客户端确认后再纳入
    # "09660",  # 地平线机器人-W（AI 芯片，2024 IPO）
    # "00020",  # 商汤-W（AI 视觉）
    # "01024",  # 快手-W
    # 用户可在此追加自己的候选代码
]
