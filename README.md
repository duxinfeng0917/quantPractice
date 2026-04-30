# 港股做空套利系统

面向**富途普通账户**的港股融券做空数据套利工具，多股票支持（默认 MINIMAX-W `00100`，可扩展至 `02513` 等）。  
通过程序化监控卖空占比、资金流向、摆盘深度，识别逼空风险和做空入场时机，并实时管理空头持仓盈亏。

---

## 快速开始

### 环境要求

```bash
# Python 3.11+，conda 管理环境
pip install -r requirements.txt
# 或使用 Make
make install
```

### 前置条件

1. 下载并启动 **Futu OpenD**（富途量化交易网关）
   - 下载地址：https://openapi.futunn.com/futu-api-doc/quick/opend-base.html
   - 默认监听 `127.0.0.1:11111`
   - 需登录有**港股实时行情**权限的富途账户

2. 网络可访问 `www.hkex.com.hk`（用于爬取每日卖空数据）

3. 实盘交易需在 `.env` 中配置 `FUTU_TRADE_PWD`（参考 `config/example.env`）

### 三步启动

```bash
# 第一步：补抓历史 HKEX 卖空数据（首次运行，建立趋势基准）
make backfill
# 等价：python3 short_squeeze_monitor.py backfill

# 第二步：一键后台启动 monitor + paper_trader
make all
# 等价：bash start.sh all
# 切换股票：STOCK=02513 bash start.sh all

# 第三步（开仓后）：启动持仓管理器
python3 short_position_manager.py --entry 897 --qty 1000 --stop 950
# 切换股票：python3 short_position_manager.py --stock 02513 --entry 50 --qty 1000
```

---

## 脚本说明

### `short_squeeze_monitor.py` — 逼空监控 + 做空信号

实时监控四路信号，输出**逼空风险评分**和**做空入场评分**。

**四路信号来源**

| 信号 | 数据来源 | 更新频率 | 作用 |
|------|---------|---------|------|
| ① HKEX 卖空占比 | 港交所网页爬取 | 每日 17:00 后 | 替代融券余量，判断做空拥挤度 |
| ② 大单净流入 | 富途 `get_capital_distribution` | 每分钟 | 大资金方向反转 → 逼空初期信号 |
| ③ 摆盘深度 | 富途 `get_order_book` | 每分钟 | 卖盘骤减 → 空头回补代理指标 |
| ④ 卖空历史动能 | HKEX DB（加权成本线） | 每日 | 空头成本 vs 当前价，判断挤压压力 |

**评分体系**

```
逼空风险评分 (0-100)        做空入场评分 (0-100)
  ≥ 70 → 强警报，禁止开空     ≥ 55 → ENTRY 入场信号
  ≥ 50 → 警报，减仓观察       ≥ 33 → CAUTION 信号积累
  ≥ 30 → 预警，关注异动       逼空分 > 25 → BLOCKED 禁止
  < 30 → 正常，持续监控
```

**命令**

```bash
python3 short_squeeze_monitor.py              # 启动实时监控（默认）
python3 short_squeeze_monitor.py backfill     # 补抓近 10 个交易日 HKEX 数据
python3 short_squeeze_monitor.py signals      # 查看最近 30 条触发信号
python3 short_squeeze_monitor.py export       # 导出摆盘/资金/HKEX 数据到 CSV
```

**后台运行**

```bash
# tmux 方式（可随时重新连接查看仪表盘）
tmux new -s monitor
python3 short_squeeze_monitor.py
# Ctrl+B → D 分离；tmux attach -t monitor 重新连接

# nohup 方式
nohup python3 short_squeeze_monitor.py >> nohup.out 2>&1 &
tail -f short_monitor.log   # 查看实时日志
```

---

### `short_position_manager.py` — 空头持仓管理器

开仓后独立运行，专注于**实时盈亏计算**和**平仓时机判断**。

**功能**
- 实时计算未实现 / 已实现盈亏（HKD 和百分比）
- 读取 `short_data.db` 中的空头加权成本线
- 监控 5 类平仓信号，综合评分 ≥ 70 触发强提示

**5 类平仓信号**

| 信号 | 触发条件 | 评分 |
|------|---------|------|
| 止损触发 | 价格 ≥ 止损价 | +80 |
| 成本线突破 | 价格 ≥ 空头加权成本线 | +60 |
| 第二目标价 | 价格 ≤ target2 | +70 |
| 第一目标价 | 价格 ≤ target1 | +40 |
| 摆盘反转 | 失衡度 ≥ +0.20 连续 2 轮 | +30 |
| 卖盘深度骤减 | 深度较均值 ↓45% | +25 |
| 大单激增 | 单轮净流入 > 15,000 万 HKD | +30 |
| 收盘保护 | 15:30 / 15:45 / 15:55 HKT | +10~20 |

**命令**

```bash
# 新建持仓
python3 short_position_manager.py \
    --entry 897 --qty 1000 \
    --stop 950 --target1 870 --target2 850

# 恢复上次持仓（读取 short_position.json）
python3 short_position_manager.py

# 记录部分平仓（第一目标价 870，平仓 500 股）
python3 short_position_manager.py --cover --cover-qty 500 --cover-price 870

# 记录全部平仓（第二目标价 850，剩余 500 股）
python3 short_position_manager.py --cover --cover-qty 500 --cover-price 850
```

**参数说明**

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--entry` | — | 开仓均价（必填，首次运行） |
| `--qty` | — | 持仓股数（必填，首次运行） |
| `--stop` | 950.0 | 止损价，超过此价立即平仓 |
| `--target1` | 870.0 | 第一目标价（建议平仓 50%） |
| `--target2` | 850.0 | 第二目标价（建议全部平仓） |
| `--interval` | 30 | 轮询间隔（秒） |

---

## 数据流与文件

```
HKEX 网站 ──爬取──► short_squeeze_monitor.py
富途 OpenD ──API──►        │
                           ▼
                     short_data.db  ◄── 共享 SQLite
                           │
                    short_position_manager.py
                           │
                     short_position.json  （持仓快照）
```

**生成文件**

| 文件 | 说明 |
|------|------|
| `short_data.db` / `short_data_<code>.db` | 每只股票一个 SQLite 数据库（路径由 `shared_config.STOCKS[code]["db_path"]` 决定） |
| `short_position_<code>.json` | 持仓状态快照，程序重启后自动恢复 |
| `logs/short_monitor_YYYYMMDD.log` | 监控器日志（按日期切分） |
| `logs/paper_trader_YYYYMMDD.log` | 模拟交易机器人日志 |
| `logs/position_manager_YYYYMMDD.log` | 持仓管理器日志 |
| `logs/monitor_stdout_<code>_YYYYMMDD.log` | `start.sh` 后台运行的 stdout 重定向 |
| `logs/trader_stdout_<code>_YYYYMMDD.log` | 同上 |

**DB 表结构**

| 表名 | 内容 |
|------|------|
| `hkex_daily` | HKEX 每日卖空量、卖空额、总成交量、卖空占比 |
| `capital_flow` | 大/中/散单净流入快照 |
| `orderbook_snapshots` | 买卖盘深度、失衡度快照 |
| `price_history` | 实时价格历史 |
| `signals` | 所有触发信号记录（含类型、详情、评分） |
| `monitor_state` | 监控器实时评分快照（单行），供 `paper_trader.py` 跨进程读取 |
| `paper_trades` | 模拟交易机器人下单/平仓记录及当时的决策依据评分 |

---

## 典型操作流程

```
开市前
  └─ backfill 确认昨日 HKEX 数据已入库

09:30 开市
  └─ short_squeeze_monitor.py 后台启动

盘中
  ├─ 做空入场评分 ≥ 55 且逼空评分 < 25
  │    └─ 参考入场，启动 short_position_manager.py
  │
  └─ 监控仪表盘关注：
       · 大单净流入方向（正负切换）
       · 摆盘失衡度（-0.6 偏空有利，转正须警惕）
       · 空头成本线（947.7）与当前价差距

15:30 收盘前
  └─ 持仓管理器触发"收盘保护"提示，评估是否持仓过夜

16:00 收盘
  └─ 继续持有则关注隔夜风险

17:00 HKEX 数据更新
  └─ 监控器自动拉取当日卖空占比，刷新成本线与动能比
```

---

## 关键指标参考（基于当前数据）

| 指标 | 当前值（04-16） | 参考意义 |
|------|----------------|---------|
| 空头加权成本线 | ~947.7 HKD | 空头群体平均建仓成本，突破此位须止损 |
| 5 日卖空占比均值 | ~8.75% | 超过 1.5× 均值（>13%）视为拥挤 |
| 当日卖空动能比 | 1.72× | ≥1.5× 空头积极，≥1.8× 极度活跃 |
| 摆盘失衡度安全区 | -0.3 以下 | 偏空有利于做空，转正须减仓 |

---

## 注意事项

- 本系统为**辅助决策工具**，不构成投资建议，操作风险自担
- 卖空占比数据来自 HKEX，每个交易日 **17:00 后更新前一日数据**，非实时
- 大单净流入（`get_capital_distribution`）为富途日内累计值，**单位为港元（HKD）**，非万元
- 富途普通账户无法获取实时融券余量和借券费率，本系统以上述四路信号作为替代指标
- 港股做空须通过券商申请融券，确保账户有相应资格和额度

---

## 其他文件

| 文件 | 说明 |
|------|------|
| `shared_config.py` | 股票配置表（STOCKS 字典）+ 共享常量，新增股票在此添加 |
| `start.sh` | 后台启动 / 停止 / 状态查看（支持 `STOCK=<code>` 环境变量切换） |
| `Makefile` | 常用命令封装（monitor、trader、all、stop、status、backfill、lint 等） |
| `config/trader_config.json` | 模拟交易机器人可热更新阈值参数（修改后 60 秒内自动生效） |
| `config/example.env` | 环境变量模板（复制为 `.env` 后填写 `FUTU_TRADE_PWD`）|
| `docs/DEVLOG.md` | 项目迭代记录与决策回顾 |
| `docs/CONVERSATION_LOG.md` | 与用户对话的关键节点存档 |
