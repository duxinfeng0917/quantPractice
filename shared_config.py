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
    },
}
DEFAULT_STOCK = "00100"

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
