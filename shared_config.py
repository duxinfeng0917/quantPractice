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
    # "02513": {
    #     "symbol":        "HK.02513",
    #     "stock_code":    "02513",
    #     "name":          "质谱",
    #     "db_path":       "short_data_02513.db",
    #     "poll_interval": 60,
    # },
}
DEFAULT_STOCK = "00100"
