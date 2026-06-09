# 项目迭代记录 — MINIMAX-W 港股做空套利系统

> 记录时间：2026-04-16  
> 股票标的：MINIMAX-W（HK.00100）  
> 账户类型：富途普通账户 + 模拟 MARGIN 账户  
> 开发语言：Python 3.10+  

---

## 目录

1. [项目背景与动机](#1-项目背景与动机)
2. [迭代一：基础框架与信号设计](#2-迭代一基础框架与信号设计)
3. [迭代二：从机构接口降级到普通账户实用版](#3-迭代二从机构接口降级到普通账户实用版)
4. [迭代三：加入做空入场信号引擎](#4-迭代三加入做空入场信号引擎)
5. [迭代四：HKEX 历史数据深度分析](#5-迭代四hkex-历史数据深度分析)
6. [迭代五：独立持仓管理器](#6-迭代五独立持仓管理器)
7. [迭代六：模拟账户自动交易机器人](#7-迭代六模拟账户自动交易机器人)
8. [关键 Bug 与修复记录](#8-关键-bug-与修复记录)
9. [实盘监控日志分析（2026-04-16）](#9-实盘监控日志分析2026-04-16)
10. [信号阈值调优过程](#10-信号阈值调优过程)
11. [技术决策复盘](#11-技术决策复盘)
12. [未来迭代方向](#12-未来迭代方向)

---

## 1. 项目背景与动机

### 1.1 初始需求

针对港股 MINIMAX-W（代码：00100），利用富途（Futu）API 编写 Python 脚本，进行融券池与做空数据套利：

- 监控融券余量变化，判断空头可借入规模
- 对比实时卖空数据与历史均值，识别异常空头拥挤
- 计算短期价格动量，配合做空信号过滤
- 设计逼空预警系统，在高风险时段自动告警

### 1.2 约束条件发现

**关键发现：富途普通账户无法获取以下数据**：

| 数据 | 机构/高级接口 | 普通账户 |
|------|-------------|---------|
| 实时融券余量 | `get_short_sale_condition()` | ❌ 无权限 |
| 借券费率（annualized） | 机构专属 | ❌ 无权限 |
| Level 2 摆盘（10档） | 需开通 | 仅 5档 |
| 港股做空余额日报 | 需订阅 | ❌ |

**解决方案**：改为爬取 HKEX 公开数据 + 富途普通账户可用的资金流向/摆盘接口，构建四路替代信号。

---

## 2. 迭代一：基础框架与信号设计

### 2.1 四路信号架构

```
信号①  HKEX 每日卖空占比    ← 港交所网页爬取（替代融券余量）
信号②  大单净流入方向        ← 富途 get_capital_distribution()
信号③  摆盘失衡检测          ← 富途 get_order_book()
信号④  卖空占比趋势           ← HKEX DB 历史趋势
```

### 2.2 数据存储设计

选用 SQLite（`short_data.db`）作为共享数据库，理由：

- 零配置，无需额外服务
- 两个脚本跨进程共享数据
- 便于导出 CSV 分析

**表结构**：

```sql
hkex_daily          -- 每日卖空成交量、金额、总量、占比
capital_flow        -- 大/中/散单净流入快照
orderbook_snapshots -- 买卖盘深度、失衡度快照
price_history       -- 实时价格历史
signals             -- 所有触发信号记录
```

### 2.3 逼空评分体系（0-100）

```
≥ 70 → 强警报，禁止开空
≥ 50 → 警报，减仓观察
≥ 30 → 预警，关注异动
< 30 → 正常，持续监控
```

---

## 3. 迭代二：从机构接口降级到普通账户实用版

### 3.1 HKEX 爬虫设计

**重要发现：HKEX 数据格式为 `<pre>` 预格式化文本，非 HTML 表格。**

原始错误尝试：
```python
# 错误！HKEX 文件没有 <table> 标签
tables = pd.read_html(resp.text)
```

**最终方案**：定位 `#short_selling` 锚点 → 截取 `<pre>` 段落 → 正则解析固定宽度文本：

```python
m = re.search(r'<a\s+name\s*=\s*["\']?\s*short_selling\s*["\']?\s*>', text, re.I)
section_clean = re.sub(r'<[^>]+>', '', text[m.end():])
code_int = str(int(stock_code))  # "100" 而非 "00100"（文件内无前导零）
pattern = re.compile(
    r'^\s+' + re.escape(code_int) + r'\b'
    r'.+?([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)',
    re.MULTILINE
)
```

**数据行格式示例**：
```
   100 MINIMAX-W     323,100   274,762,770   2,149,528   1,869,742,390
   代码  名称         卖空量      卖空金额      总成交量      总成交金额
```

### 3.2 URL 格式

```
https://www.hkex.com.hk/eng/stat/smstat/dayquot/d{YYMMDD}e.htm
# 例如 2026-04-15 → d260415e.htm
```

### 3.3 非交易日处理

- HTTP 404 → 静默跳过（非交易日，如公众假期）
- 2026-04-07（清明节假期）、2026-04-06（复活节）均正常返回 404

---

## 4. 迭代三：加入做空入场信号引擎

### 4.1 背景

前两轮迭代主要设计"逼空预警"（何时不能做空），但缺少"什么时候应该做空"的主动信号。在 14:11 前后收到大量 CAUTION/ENTRY 信号后，用户明确提出需要做空入场评分引擎。

### 4.2 入场评分维度（5维，总分100）

| 维度 | 内容 | 最高分 |
|------|------|--------|
| 1 | 大单净流入由正转负 | 30 |
| 2 | 卖盘深度骤增（大卖单涌入） | 25 |
| 3 | 摆盘持续偏空（失衡度 < -0.30 连续2轮） | 20 |
| 4 | 价格低于近期高点（下行动能） | 15 |
| 5 | 高点拒绝后连续下行形态 | 10 |

**安全门设计**（最高优先级）：
```python
if squeeze_score > SHORT_SAFE_SQUEEZE:  # 25分
    return 0, "BLOCKED", [...]
```
即逼空风险过高时，无论入场评分多高，一律拦截。

### 4.3 信号类型

```
ENTRY   — ≥ 55分，可考虑入场
CAUTION — ≥ 33分，信号积累中
BLOCKED — 逼空评分超安全线
HOLD    — 条件不足
```

### 4.4 HKEX 基线分问题发现

**重要发现**：每次评分时，HKEX 历史数据总会贡献固定约 +27 分：
- 卖空动能比 ≥ 1.5× → +12 分
- 卖空量爆量 ≥ 1.8× → +8 分（或其他组合）
- 价格低于空头成本线 → +15 分

这意味着实际入场门槛 55 分中，有 ~27 分来自"历史基线"，实时信号只需再贡献 ~28 分。**这使得门槛偏低，容易产生误信号。**

---

## 5. 迭代四：HKEX 历史数据深度分析

### 5.1 三个量化维度

```python
# 维度1：加权空头成本线（近6日加权均价）
weighted_cost = Σ(short_value_i) / Σ(short_volume_i)  # ≈ 947.7 HKD

# 维度2：卖空动能比
momentum_ratio = 最新日占比 / 前5日均值占比  # 1.72× 时极度活跃

# 维度3：卖空量爆量比
volume_surge = 最新日卖空量 / 前5日均值卖空量
```

### 5.2 成本线的战略意义

- **价格 < 成本线**：空头群体整体盈利，继续持仓意愿强，下行压力持续
- **价格 ≈ 成本线**：关键博弈区，双方力量均衡
- **价格 > 成本线**：空头群体整体亏损，被迫回补风险上升（逼空触发条件）

当日（2026-04-16）加权成本线 ≈ **947.7 HKD**，开盘价约 914，空头整体盈利 ~3.6%。

### 5.3 数据刷新时机

HKEX 每日卖空数据于 **17:00 HKT 后更新前一交易日数据**，监控器在 `HKEX_FETCH_HOUR=17` 时自动拉取。

---

## 6. 迭代五：独立持仓管理器

### 6.1 设计原则

**与监控器解耦**：`short_position_manager.py` 完全独立运行，开仓后单独启动，通过 `short_data.db` 获取成本线数据。

### 6.2 8类平仓信号与评分

| 信号 | 触发条件 | 评分 |
|------|---------|------|
| 止损触发 | 价格 ≥ 止损价 | +80 |
| 成本线突破 | 价格 ≥ 空头加权成本线 | +60 |
| 接近成本线 | 价格 ≥ 成本线×97% | +35 |
| 第二目标价 | 价格 ≤ target2 | +70 |
| 第一目标价 | 价格 ≤ target1 | +40 |
| 摆盘反转 | 失衡度 ≥ +0.20 连续2轮 | +30 |
| 卖盘深度骤减 | 深度较均值↓45% | +25 |
| 大单激增 | 净流入 > 15,000万 HKD/轮 | +30 |
| 收盘保护 | 15:30/15:45/15:55 HKT | +10~20 |

评分 ≥ 70 → 强提示立即平仓  
评分 ≥ 45 → 建议减仓  
评分 < 45 → 继续持仓

### 6.3 持仓状态持久化

使用 `short_position.json` 保存持仓快照，程序重启后自动恢复：

```json
{
  "symbol": "HK.00100",
  "entry_price": 897.0,
  "qty": 1000,
  "entry_time": "2026-04-16T14:53:00",
  "stop_price": 950.0,
  "target1": 870.0,
  "target2": 850.0,
  "covered_qty": 0,
  "realized_pnl": 0.0
}
```

---

## 7. 迭代六：模拟账户自动交易机器人

### 7.1 富途账户调查结果

通过 `get_account_list()` 查询到两个模拟账户：

| acc_id | 类型 | 市场 | 可否做空 |
|--------|------|------|---------|
| 12134325 | SIMULATE CASH | STOCK | ❌（现金账户无法融券） |
| 18982257 | SIMULATE MARGIN | OPTION | ✅（保证金账户，可卖空港股） |

使用 `acc_id=18982257`，`TrdEnv.SIMULATE`。

### 7.2 入场规则升级（4条件全满足）

| 条件 | 旧阈值 | 新阈值 | 理由 |
|------|--------|--------|------|
| 做空入场评分 | ≥ 55 | ≥ 65 | HKEX 基线贡献 ~27 分，实际门槛需提高 |
| 连续轮数 | 1轮 | 2轮 | 排除单轮噪声（如 14:50 的误报） |
| 逼空评分上限 | < 25 | < 20 | 更严格的安全保护 |
| 失衡度条件 | 无 | < -0.50 | 确保持续明显的卖压 |

### 7.3 状态机设计

```
IDLE
 │
 ├─ sig_type=="ENTRY" AND score≥65 AND squeeze<20 AND imb<-0.50
 │  → confirm_rounds += 1
 │
 ├─ confirm_rounds >= 2
 │  → 计算仓位（score 65-74: 500股, score≥75: 1000股）
 │  → place_short_order() → 转入 IN_POSITION
 │
IN_POSITION / COVERING
 │
 ├─ 价格 ≥ stop_price → COVER_STOP → 全平 → IDLE
 ├─ squeeze ≥ 35      → COVER_SQUEEZE → 全平 → IDLE
 ├─ 价格 ≤ target2    → COVER_FULL → 全平 → IDLE
 └─ 价格 ≤ target1    → COVER_PARTIAL → 平50% → COVERING
                        → 止损上移至入场价（锁定利润）
```

### 7.4 交易记录表

新增 `paper_trades` 表记录所有模拟交易：

```sql
CREATE TABLE paper_trades (
    id, ts, action,    -- SHORT_OPEN / COVER_PARTIAL / COVER_FULL / COVER_STOP / COVER_SQUEEZE
    price, qty, pnl,   -- 成交价、数量、本次盈亏
    total_pnl,         -- 累计已实现盈亏
    entry_score, squeeze_score, imbalance,  -- 决策依据存档
    note
);
```

### 7.5 Dry-run 模式

`--dry-run` 参数：信号计算、状态机、日志全部正常运行，仅跳过实际 `place_order()` 调用，便于测试验证信号逻辑。

---

## 8. 关键 Bug 与修复记录

### Bug 1：ImportError — futu 包冲突

**现象**：
```
ImportError: cannot import name 'OpenQuoteContext' from 'futu'
```

**原因**：系统中同时安装了 `futu`（0.0.1，残留 stub 包）和 `futu_api`（10.2.6218），两者都向 `futu/` 目录写文件，产生冲突。

**解决**：卸载 stub 包后，`futu_api` 的 `__init__.py` 正常接管，问题自动消失。

---

### Bug 2：ECONNREFUSED on port 11111

**现象**：
```
ConnectionRefusedError: [Errno 111] Connection refused (127.0.0.1:11111)
```

**原因**：Futu OpenD 网关未启动。富途 API 架构为：
```
Python 脚本 ──TCP──► Futu OpenD（本地网关） ──加密通道──► 富途服务器
```

**解决**：下载、安装 Futu OpenD 并启动，登录有港股实时行情权限的账户。

---

### Bug 3：HKEX backfill 失败 — "No tables found"

**现象**：
```
ValueError: No tables found
```

**原因**：`pd.read_html()` 只能解析 `<table>` 元素，但 HKEX 每日报价文件的数据全部在 `<pre>` 预格式化文本块中，根本没有 HTML 表格。

**修复前**：
```python
tables = pd.read_html(StringIO(resp.text))  # 报错
```

**修复后**：改用正则解析 `<pre>` 段落（详见第3节）

---

### Bug 4：NameError — ctx 未定义

**现象**：
```
NameError: name 'ctx' is not defined
```

**原因**：用户在调试时将行情拉取代码直接写在模块顶层（函数体外），`ctx` 变量只在函数内定义。

**修复**：删除模块级的调试代码（原文件 79-81 行）。

---

### Bug 5：资金流向单位错误

**现象**：界面显示 `大单净流入: +49,143,410 万港元`（实际金额异常巨大）

**原因**：Futu `get_capital_distribution()` 返回**原始 HKD**（港元），而非"万港元"。注释和代码中误写为"万港元"单位，导致显示时未除以 10,000。

**修复**：
```python
# 修复前
f"{state.latest_big_net:+,.1f} 万"

# 修复后
f"{state.latest_big_net / 10000:+,.1f} 万"
```

---

### Bug 6：HKEX 股票代码格式问题

**现象**：backfill 成功但找不到 MINIMAX-W 数据，返回 `None`

**原因**：`STOCK_CODE = "00100"`（5位带前导零），但 HKEX 文件内数据行使用纯整数 `100`（无前导零）。

**修复**：
```python
code_int = str(int(stock_code))  # "00100" → "100"
```

---

### Bug 7：`evaluate_cover()` 中引用未定义常量

**现象**：
```
NameError: name 'COVER_NOW_SCORE' is not defined
```

**原因**：`evaluate_cover()` 函数引用了 `COVER_NOW_SCORE`，但该常量定义在函数之后。Python 函数调用时才查找名称，但常量定义顺序在文件中不一致。

**修复**：将 `COVER_NOW_SCORE = COVER_ALERT_SCORE` 移至函数定义之前，或在函数内直接使用 `COVER_ALERT_SCORE`。

---

### Bug 8：滚动窗口跨交易日越界 → 假"卖盘深度骤减"刷屏（2026-06-03 06082 实盘）

**现象**：06082（壁仞科技）首日监控，开盘后每 30s 触发 `[摆盘预警] 卖盘深度骤减 91~99.5%（vs 基准 ~133,000 股）[+25分]`，逼空评分被持续顶到 49~65、做空入场全程 BLOCKED。但当日真实盘口仅几百~几千股，并无任何隔夜空头回补。

**根因**：`db_get_recent_ask_depth()` 等 5 个滚动窗口读取函数用 `ORDER BY id DESC LIMIT n` 取最近 n 行，**不按交易日过滤**。开盘时 60 轮窗口大部分由上一交易日（06-02）收盘前的厚盘口（avg 75,691、max 159,400 股）填充，median 基准≈133,000，把当日薄盘口拿去比 → 每轮假触发 +25 分。同类问题波及 `big_net`/`capital_structure`（日内累计值每日清零，跨日读取会污染动能/连续性/效率判断与开盘首轮 Δ）、`prices`（跨日把昨收当"近期高低点"）、`imb_flips`。

**修复**：5 个函数统一改为 **session-anchored**（`WHERE ts >= 当日 ISO`，默认当日、可传 `since_ts` 覆盖），与既有 `db_get_session_high/low/*_peak` 一致。开盘首段样本不足时信号自然不触发（正确行为），而非比对昨日数据。`paper_trader.py` 内的同名副本同步修复；`long_entry_monitor.py` 因从 `short_squeeze_monitor` import 自动继承。

**验证**（用 06082 真实库回放 09:30 时点）：OLD 基准恒为 133,000 → 骤减 98~99%；NEW 在今日样本 <7 时不触发，K=10 时基准 2,200、骤减 9.1%（低于 30% 阈值，不再误报）。

---

## 9. 实盘监控日志分析（2026-04-16）

### 9.1 关键时间节点复盘

| 时间（HKT） | 事件 | 评分 | 失衡度 | 分析 |
|------------|------|------|--------|------|
| 12:54 | 首次监控日志 | 逼空13 | - | 开盘正常，卖空信号初步积累 |
| 13:06 | 卖盘深度骤增至 17,220 | 逼空35 | - | 系统触发 BLOCKED，正确拦截 |
| 13:10 | 卖盘恢复正常 | 逼空下降 | - | 短暂异动，非真实逼空 |
| 14:11 | CAUTION 信号 | 入场62 | -0.78 | 入场积累中，评分首次突破60 |
| 14:13 | ENTRY 信号 | 入场62 | -0.76 | 两轮CAUTION，但评分未达65 |
| 14:50 | 卖盘骤减 8,520 | 逼空35 | - | 系统正确触发 BLOCKED，非真实逼空 |
| 14:51 | 卖盘恢复 | 逼空下降 | - | 仅1分钟短暂扰动 |
| **14:53** | **ENTRY 信号** | **入场72** | **-0.786** | **最佳入场点①** |
| **14:54** | **ENTRY 信号** | **入场75** | **-0.811** | **最佳入场点②（连续2轮确认）** |
| 14:55 | 价格914 → 911 | - | - | 入场后价格下行，信号有效 |
| 14:57 | 价格继续下行 | - | - | 后续到达 911-912 区间 |

### 9.2 最优入场分析（14:53-14:54）

```
14:53:
  · 做空评分 72/100（满仓信号）
  · 逼空评分 13/100（安全区）
  · 摆盘失衡度 -0.786（持续强卖压）
  · 大单净流入方向：负值（主力出逃）
  · 当前价格：约 914 HKD

14:54（第二轮确认）：
  · 做空评分 75/100（升至满仓门槛）
  · 摆盘失衡度 -0.811（进一步恶化）
  → 若使用新系统：CONFIRM_1 → CONFIRM_2 → 执行做空 1000 股 @ ~914
  → 后续价格：911-912（约 +2-3 HKD/股，1000股 ≈ +2000~3000 HKD）
```

### 9.3 14:50 假信号分析

```
14:50：卖盘深度从约 14,000 骤降至 8,520（↓约39%）
  → 逼空评分触发 35（超过安全线25）
  → 系统正确返回 BLOCKED

14:51：卖盘恢复至正常水平
  → 逼空评分回落
  → 证明14:50是单轮噪声，系统响应正确

教训：单轮数据异动不代表趋势，
      连续2轮确认机制在此处意义重大。
```

---

## 10. 信号阈值调优过程

### 10.1 入场评分门槛：55 → 65

**原因**：  
- HKEX 历史基线始终贡献 ~27 分（动能比+爆量+成本线各自稳定得分）
- 55 分门槛中有 ~27 分是"免费的历史基准分"，实时信号只需再凑 28 分即可触发
- 实际回看 14:11-14:13 的 62 分信号，信号质量偏弱，将门槛提升至 65 可过滤掉这类边界情况

**影响**：  
- 触发频率降低，但信号精度提升
- 结合2轮确认后，虚假信号大幅减少

### 10.2 连续确认轮数：1 → 2

**原因**：  
- 14:50 的单轮 BLOCKED 信号证明，单轮异动极易产生噪声
- 同理，单轮 ENTRY 也可能是噪声
- 2轮连续确认要求信号在 2 分钟内持续稳定

**代价**：  
- 入场信号滞后约 60 秒（1个轮询间隔）
- 对于日内做空，这是可以接受的代价

### 10.3 逼空安全线：25 → 20

**原因**：  
- 原安全线 25 相对宽松，部分边界情况下逼空评分 25-30 时仍可开空
- 收紧至 20 提供更大的安全缓冲

### 10.4 失衡度条件：无 → < -0.50

**原因**：  
- 摆盘失衡度 < -0.50 意味着买卖盘比例达到较为极端的卖压状态
- 引入此条件确保入场时存在明显的结构性卖压，而非随机波动

---

## 11. 技术决策复盘

### 11.1 为什么选择 SQLite 而非内存缓存

**决策**：所有信号数据写入 SQLite，不使用全局变量或内存队列。

**优点**：
- 两个独立脚本（monitor + manager）可共享数据，无需 IPC
- 程序崩溃后数据不丢失，重启可继续分析
- 方便导出 CSV 做回测分析（`export` 命令）

**缺点**：
- 频繁写入带来轻微 I/O 开销（每分钟约 3 次写操作，可忽略）

### 11.2 为什么不用 WebSocket 订阅而用轮询

**决策**：使用 `time.sleep(60)` 轮询，而非 Futu 的推送回调。

**理由**：
- 港股深度行情日内变化在分钟级别已足够捕捉趋势
- 轮询架构代码更简单、更易 debug
- 推送架构需要多线程同步，增加复杂度

**改进建议**：在高频交易需求时可切换为回调架构。

### 11.3 为什么将 paper_trader 与 monitor 分开

**决策**：不在 `short_squeeze_monitor.py` 中直接集成交易逻辑，而是新建 `paper_trader.py`。

**理由**：
- 单一职责原则：monitor 专注信号生成和记录，trader 专注决策执行
- 可以独立运行 monitor 而不会意外触发交易
- `paper_trader.py` 复用 DB 中的数据，通过 SQLite 实现解耦
- 便于未来替换信号引擎而不影响交易逻辑

### 11.4 仓位大小设计依据

| 评分范围 | 仓位 | 逻辑 |
|---------|------|------|
| 65–74 | 500 股（半仓） | 信号较弱，控制风险 |
| ≥ 75 | 1000 股（满仓） | 信号强烈，放大收益 |

**参考依据**：  
- 14:53 得分 72（半仓 500 股）
- 14:54 得分 75（满仓 1000 股）
- 实际最优点是 14:54 的满仓信号

---

## 12. 未来迭代方向

### 短期（可在下次对话实现）

- [ ] **回测框架**：使用历史 DB 数据回放信号，统计入场评分与后续收益的相关性
- [ ] **止损优化**：ATR（平均真实波幅）动态止损，替代固定价格止损
- [ ] **时间过滤**：避免在开盘前30分钟（09:30-10:00）和收盘前30分钟（15:00-15:30）入场
- [ ] **交易日历集成**：自动识别港股公众假期，避免 backfill 失败日志噪声

### 中期

- [ ] **多标的扩展**：将信号引擎泛化，支持任意港股代码参数化
- [ ] **Web 仪表盘**：将终端打印升级为 Flask/Streamlit 实时图表
- [ ] **盈亏统计**：基于 `paper_trades` 表的胜率、盈亏比、夏普率统计
- [ ] **真实账户迁移**：将 `TrdEnv.SIMULATE` 改为 `TrdEnv.REAL`，完成实盘对接

### 长期

- [ ] **机器学习信号融合**：用历史信号数据训练分类器，替代手工评分权重
- [ ] **跨品种套利**：结合 MINIMAX-W 对应正股（若存在）的价差机会

---

## 13. 迭代七：动态目标价 + 信号反转平仓 + --held-short 模式

### 13.1 背景

2026-04-17 基于实盘分析，发现三个系统性问题：

1. **`confirm_rounds` 始终为 0**（10:08 日志）：入场评分 80，但状态机不推进，原因是失衡度条件 `< -0.50` 永不满足（日志显示失衡度均为 +0.40 ~ +0.81）
2. **固定目标价过时**（870/850 已接近或高于当前价位）：做空成本 865 时，系统仍用旧目标价，止盈逻辑失效
3. **无法为手动持仓提供离场建议**：用户手动做空后，监控器无法提供"何时平仓"的建议，只能看到信号但不知如何操作

### 13.2 paper_trader.py 改动

**失衡度条件放宽**：
```python
# 旧：仅允许极端卖压
ENTRY_IMB_THRESHOLD = -0.50

# 新：阻断极端买压，其余情况允许入场
ENTRY_IMB_THRESHOLD = 0.60
```

**原因**：正失衡度（买单多）≠ 价格上涨。大量买单挂在盘口，但市价卖单连续成交，价格仍可下行。旧条件误将"卖压形式"与"价格方向"绑定，导致 10:08 的有效信号被阻断。

**动态目标价计算**（`_calc_targets(entry, args)`）：

```python
TARGET1_PCT = 0.015   # -1.5%（第一目标价）
TARGET2_PCT = 0.030   # -3.0%（第二目标价）
STOP_PCT    = 0.040   # +4.0%（止损）

target1 = entry * (1 - TARGET1_PCT)   # 865 → 852
target2 = entry * (1 - TARGET2_PCT)   # 865 → 839
stop    = entry * (1 + STOP_PCT)      # 865 → 900
```

若用户传入 `--target1/--target2/--stop` 则使用手动值，否则自动计算（默认为 0）。

**信号反转平仓逻辑**（`detect_reversal_signal()`）：

| 条件 | 阈值 | 含义 |
|------|------|------|
| 大单净连续转正 | 2 轮 | 主力开始流入，空头不利 |
| 失衡度连续偏多 | ≥ 0.75 持续 2 轮 | 买盘极度压倒卖盘 |

- 双信号同时满足 → `COVER_REVERSAL`（全平）
- 单信号满足 → 先减仓 50%（`partial_exit`）

**状态机新增 COVER_REVERSAL 动作**：

```
B: 逼空评分 ≥ 35 → COVER_SQUEEZE
C: 反转信号检测 → COVER_REVERSAL（新增）
D: 价格 ≤ target2  → COVER_FULL
E: 价格 ≤ target1  → COVER_PARTIAL（止损上移至入场价）
F: 价格 ≥ stop     → COVER_STOP
```

### 13.3 short_squeeze_monitor.py 新增 --held-short 模式

**`HeldShort` 数据类**：
```python
@dataclass
class HeldShort:
    entry_price: float
    qty: int
    stop_pct: float = 0.035    # 止损：+3.5%
    target1_pct: float = 0.015 # 第一目标价：-1.5%
    target2_pct: float = 0.030 # 第二目标价：-3.0%
    target1_done: bool = False
    # 属性：stop, target1, target2, unrealized_pnl(), pnl_pct()
```

**`evaluate_cover_signal()` — 6维离场评分**：

| 优先级 | 触发条件 | 建议动作 | 紧迫度 |
|--------|---------|---------|--------|
| P1 | 价格 ≥ 止损价 | STOP_LOSS | 100 |
| P2 | 逼空≥35 且有利润 | COVER_ALL | 60+ |
| P3 | 大单反转+失衡反转（双信号） | COVER_ALL | 80 |
| P4 | 单反转信号 or 逼空25-35 | COVER_HALF | 40-45 |
| P5 | 价格 ≤ target2 | COVER_ALL | 75 |
| P6 | 价格 ≤ target1（未执行） | COVER_HALF | 55 |

**仪表盘新增持仓面板**（`print_dashboard()` 扩展）：
```
╔═══════════════════════════════════╗
║  持仓管理建议                        ║
║  持仓: 865 × 1000股                 ║
║  当前盈亏: +21,000 HKD (+2.4%)      ║
║  目标①: 852 | 目标②: 839 | 止损: 900║
║  紧迫度: ██████░░░░ 60              ║
║  建议: COVER_HALF — 逼空风险上升     ║
╚═══════════════════════════════════╝
```

**命令行参数**：
```bash
# 标准模式（无持仓，仅看信号）
python3 short_squeeze_monitor.py

# 持仓模式（有手动持仓，获取平仓建议）
python3 short_squeeze_monitor.py --held-short 865 --held-qty 1000
python3 short_squeeze_monitor.py --held-short 865 --held-qty 1000 --stop-pct 3.5 --t1-pct 1.5 --t2-pct 3.0
```

持仓模式下：`state.in_position=True` 自动设置 → 入场信号评分不显示，改为平仓建议面板。

---

## 14. 实盘日志分析（2026-04-17）

### 14.1 10:08 confirm_rounds=0 问题复盘

**现象**：10:08 入场评分=80，但 `confirm_rounds` 未从 0 推进。

**排查过程**：
```
10:05 失衡度 +0.81   # 状态机检查：0.81 > -0.50 → 不满足，confirm=0
10:07 失衡度 +0.67   # 0.67 > -0.50 → 不满足，confirm=0
10:08 失衡度 +0.54   # 0.54 > -0.50 → 不满足，confirm=0  ← 问题所在
10:11 失衡度 +0.38   # 0.38 > -0.50 → 不满足，confirm=0
```

**根本原因**：条件 `imbalance < -0.50` 永远不满足，因为摆盘实际是偏买盘（买单挂单多）。但价格仍在下行，说明市价卖单持续成交，失衡度为正不代表价格上涨。

**修复**：将条件改为 `imbalance < +0.60`（仅阻断极端买压），允许中性偏买盘环境下入场。

### 14.2 14:11-14:13 844 价位再次触发 ENTRY 信号分析

**现象**：价格 844（已从日高 900+ 下跌约 6%），信号评分仍 62 分触发 ENTRY。

**技术上的正确性**：
```
· 逼空风险 18 < 25（安全线）
· 卖盘骤增：+90%~+140% vs 均值 → +25分
· 大单净流负值（空方主导）→ +15分
· HKEX 动能支撑 → +15分
· 价格低于近期高点 → +7分
  合计 62分 ≥ 55分（旧阈值），触发 ENTRY
```

**结构性问题——信号引擎的「价格盲点」**：

信号引擎每轮**独立评估技术指标**，不感知以下上下文：
- 当前价 844 = 成本 865 的 +2.4% 浮盈（逼近 target1 约 852）
- 此时出现的大量卖盘，对新开空者是入场信号，对持仓者恰恰是**止盈信号**
- 引擎对"新开仓视角"和"已持仓视角"不加区分

**解决方案**：
- 有持仓时使用 `--held-short` 模式，`state.in_position=True` 会跳过入场评分逻辑，改为输出平仓建议
- 844 价位用 `--held-short 865` 模式运行时，系统会显示"target1 即将触及，建议 COVER_HALF"

**核心教训**：
> 相同的市场信号（卖盘骤增 + 大单负值），对"空仓者"是入场信号，对"持仓者"是离场信号。
> 系统的语境需要通过参数（`--held-short`）人工注入，引擎本身不知道用户处于哪种状态。

---

## 附录：文件清单

| 文件 | 功能 | 创建时间 |
|------|------|---------|
| `Demo1.py` | akshare 拉取美股 OHLCV | 初始 |
| `Demo2.py` | yfinance 拉取历史数据 | 初始 |
| `short_squeeze_monitor.py` | 逼空监控 + 做空信号引擎（主程序） | 迭代一 |
| `short_position_manager.py` | 空头持仓管理器（开仓后独立运行） | 迭代五 |
| `paper_trader.py` | 模拟账户自动交易机器人 | 迭代六 |
| `short_data.db` | SQLite 共享数据库 | 迭代一自动创建 |
| `short_position.json` | 持仓状态快照（跨进程恢复） | 迭代五运行时创建 |
| `CLAUDE.md` | Claude Code 工作上下文说明 | 初始化 |
| `README.md` | 项目使用文档 | 迭代五后 |
| `DEVLOG.md` | 本文件，项目迭代记录 | 迭代六后 |

---

---

## 迭代八：项目结构规范化（2026-04-22）

**背景：** 项目文件全部堆在根目录，无依赖清单，.gitignore 为空，存在泄露 .env/DB 的风险。

**变更内容：**
- `.gitignore` — 完整覆盖 `*.log`、`*.db`、`.env`、`__pycache__/`、`nohup.out`、`*.csv`、`data/`、`logs/`
- `pyproject.toml` + `requirements.txt` — 声明依赖（futu-api, pandas, requests, lxml, akshare, yfinance）
- `config/trader_config.json` — 从根目录迁移至 `config/`；同步更新 `paper_trader.py:104` 和 `start.sh`
- `config/example.env` — 新增 .env 模板，替代原 `trader_config.json.example` 提示
- `data/exports/` — 历史 CSV 导出文件迁移至此；`Makefile` 的 `export` 目标自动输出到此目录
- `docs/` — `DEVLOG.md`、`CONVERSATION_LOG.md` 迁移至此
- `logs/.gitkeep` — 确保空日志目录被 git 追踪
- `Makefile` — 封装常用命令（monitor, trader, all, stop, status, backfill, lint 等）

**决策理由：** 保持主脚本在根目录（避免破坏 start.sh 和 import 路径），仅对配置、数据、文档做目录归类，实现最小侵入式规范化。

---

## 迭代九：资金流向快照按 update_time 去重（2026-04-28）

**背景：** 盘中观察到 `大单净流入` 字段在多轮（15s 间隔）内完全保持不变（如 -10913740.0 持续 ~2 分钟）。Futu OpenD 对 `get_capital_distribution` 大约每分钟才刷新一次，主循环每 15s 调用一次会把同一份缓存快照重复写入 `capital_flow` 表。

**后果：**
1. SQLite `capital_flow` 表灌入大量重复行，膨胀且无信息量。
2. `analyze_capital_flow` 取 `recent = history[:BIGFLOW_REVERSAL_MIN=2]` 时，相邻两条很可能是同一份缓存，导致"连续 2 轮净流入正值"实质上等价于"同一缓存值刚好为正"，反转信号过于敏感、判断质量低。

**变更内容：**
- `short_squeeze_monitor.py:fetch_capital_flow` — 读取 Futu 返回行的 `update_time` 字段作为去重键；若缺失则退化为 `(big_net, mid_net, small_net)` 三元组比较。同 key 时跳过 DB 写入并直接返回上次缓存的 dict（仍含旧 ts）。缓存挂在函数对象的 `_last_key` / `_last_result` 属性上，避免引入模块级全局。
- 函数 docstring 修正单位描述：原始 HKD，非万港元（呼应 `known_bugs_and_gotchas.md` Bug 3）。

**决策理由：** 选择函数属性而非 `MonitorState` 字段，原因是缓存只与 `fetch_capital_flow` 的私有去重逻辑相关，调用方仍能像之前一样把返回 dict 直接赋给 `state.latest_big_net`，不需要感知去重机制。返回缓存 dict 而非 `None` 是为了不破坏 `state.latest_big_net = cf["big_net"]` 的现有写法。

---

## 迭代十：底部追空陷阱修复（2026-04-30）

**背景：** 2026-04-30 09:43–09:44 一段实盘日志中，价格于 686.5–688.5 区间被打出 ENTRY 信号（做空评分一度高达 88），随后 1 分钟内反弹至 696.5（+1.4%），属于典型"底部追空"。复盘发现三处逻辑漏洞：

1. **「卖盘深度骤增 +25 分」是反向信号**：[`analyze_short_entry`](short_squeeze_monitor.py) 维度 2 把 13,420 股大卖单计为做空利好 +25 分，但下一轮卖盘骤减到 1,720 股（-77%）——这是"挂大卖单制造空头跟风、瞬间撤单拉升"的逼空套路，方向恰好相反。
2. **逼空安全门被均值滑动稀释**：09:43:04 时逼空=43（BLOCKED），09:43:19 突然降到 18（PASS），原因是新出现的 8,540 股大卖单把 `ASK_DEPTH_WINDOW` 均值拉高，骤减信号自动失效；与此同时摆盘失衡度 +0.451 明显偏多（红旗）却被忽略。
3. **HKEX 叠加分覆盖 failsafe**：`run_monitor` 末段无条件 `if short_score >= SHORT_ENTRY_MIN: short_signal = "ENTRY"`，会把 `analyze_short_entry` 内部刚降级的 CAUTION/BLOCKED 重新升回 ENTRY。

**变更内容：**
- `short_squeeze_monitor.py` 新增三个常量：
  - `SHORT_SQUEEZE_LOOKBACK = 4` — 逼空安全门回看 N 轮（取峰值）
  - `SHORT_TRAP_IMB_BLOCK = 0.30` — 摆盘失衡度阈值，超过即降级
  - `SHORT_TRAP_IMB_SUPPRESS = 0.10` — 卖盘骤增时若失衡度 > 此值，不计 +25 分
- `analyze_short_entry` 签名新增 `recent_max_squeeze`：安全门用 `max(squeeze_score, recent_max_squeeze)`，避免被均值滑动稀释一次性绕过。
- 维度 2 加入诱空检测：当 `current_imbalance > SHORT_TRAP_IMB_SUPPRESS` 且卖盘骤增时，输出"⚠ 疑似诱空挂单"提示但**不计分**，并记录 `SHORT_ASK_SURGE_TRAP` 类型信号入库。
- 评分末尾加 failsafe：`sig_type == "ENTRY"` 且 `current_imbalance > SHORT_TRAP_IMB_BLOCK` → 强制降级 CAUTION。
- `MonitorState.recent_squeeze_scores: list[int]` 滑动窗口字段；`run_monitor` 每轮 append 当前 squeeze_score，截断到 N 轮，传 max 给 `analyze_short_entry`。
- `run_monitor` 末段升级逻辑改为只在 `short_signal == "HOLD"` 时根据叠加分升级，禁止覆盖 BLOCKED/CAUTION。

**回归验证（基于 09:43–09:44 日志）：**
- 09:43:19：recent_max_squeeze = max(43, 18) = 43 > 25 → BLOCKED ✓
- 09:43:34/49/04/19：current_imbalance 在 +0.276 ~ +0.451 区间，全部 > +0.10，维度 2 +25 分被诱空检测吞掉；即便分数勉强 ≥55，imbalance > +0.30 时由 failsafe 降级 CAUTION。

**决策理由：** 优先用"摆盘失衡度方向"判别诱空，而非依赖"卖盘持续性"。后者无法识别本案——09:43:34 起 5 轮卖盘都维持在 12k+ 股，持续性检查同样会通过。摆盘失衡度直接反映买卖双方真实力量对比，对挂单撤单套路免疫。

## 迭代十一：警报噪音治理 — 自喂养基准 / 复刻评分 / 文案常量化（2026-05-07）

**背景：** 2026-05-07 13:47–13:49 一段实盘日志中，dashboard 在 8 个连续 polling 周期里反复打出"卖盘深度骤减 +25 分"和"大单净流入持续正值 2 轮 +10 分"，逼空评分卡在 65/73 高位约 2 分钟。复盘三处问题：

1. **`ASK_DEPTH_WINDOW=20` × 15s = 5 分钟太短**，且 `analyze_order_book` 用算术均值作为基准。新出现的低深度样本会被一并塞进窗口、连续把均值拽下，形成"基准追低值"的自喂养循环：均值从 1,659 → 1,196 一路掉，警报却始终显示 30%+ 骤减，每轮重复 +25 分。
2. **「持续正值 2 轮」文案永远写 2 轮**——直接用了常量 `BIGFLOW_REVERSAL_MIN`，没追踪真实连续轮数；连续 30 轮和连续 2 轮看起来一模一样。
3. **Futu `get_capital_distribution` 约 1 分钟才刷新**，`fetch_capital_flow` 已按 `update_time` 去重 DB 写入，但 `analyze_capital_flow` 每 15s 仍读同一窗口、再次落 +10 分到 dashboard，dashboard 出现 4 倍假"事件"密度。

**变更内容（`short_squeeze_monitor.py`）：**

- 新增常量 `ASK_DEPTH_LOG_COOLDOWN_SECS = 60`；`ASK_DEPTH_WINDOW` 由 20 → 60（≈15 分钟）。
- `analyze_order_book`：基准由 `statistics.mean` 改为 `statistics.median`（对极端低值鲁棒）；`log.warning` + 通过函数静态属性 `_last_log_ts` 实现 60s 节流；评分仍每轮计算（评分 = 当前状态，不能被冷却抑制）。
- `analyze_short_entry` 维度 2 同步改用中位数（与 `analyze_order_book` 保持基准一致），`analyze_short_exit` 设计意图是持仓时短窗口快速反应（5 样本），保留均值。
- `analyze_capital_flow`：循环计算真实 `streak`（最新往前数连续 `> 0` 轮数）；持续正值按 streak 长度分档（≥2: +5, ≥4: +10, ≥8: +15）；反转信号文案也改用真实 streak。
- `analyze_capital_flow`：函数入口查 `capital_flow` 表最新 `id`，与函数静态属性 `_last_id` 比对相等则直接返回缓存的 `(score, signals)`，避免同一份数据反复评分。

**未改动（已正确）：** `db_save_signal` 已具备 300s 冷却机制（v2 引入），DB 信号表不会因高频报警重复写入。

**回归验证（手工心算 13:47-13:49 日志）：**
- 中位数基准：在 8 轮 ask_depth ∈ [240, 860] 的样本中位数会更接近 600 而非 1,200；`shrink_pct` 大约 0–30%，多数 polling 周期不会再触发 +25 分。
- streak 文案：当 capital flow 实际连续正值 N 轮时，dashboard 准确显示 N 而非常量 2。
- 资金流复用：3 分钟内同一 `capital_flow.id` 会让 `analyze_capital_flow` 直接返回缓存，dashboard 不再每轮重新打"+10 分"。

**决策理由：** 把"评分"和"日志/dashboard 事件"解耦——评分必须每轮真实反映当前观测，但用户感知的"事件"应只在状态变化时触发。中位数基准比扩大窗口更便宜：保留 5 分钟窗口也能用，但 15 分钟+中位数对真正长时间的薄盘期更鲁棒。

## 迭代十二：高点基准漂移 / 持续正值底分降权（2026-05-12）

**背景：** 2026-05-12 09:30–09:40 一段实盘日志中暴露两个失真：

1. **维度 4「价格较近期高点下跌 X%」基准随价格漂移。** `recent_high = max(prices)` 取的是 `SHORT_PRICE_WINDOW=10` 滑动窗口内的最大值。当价格连续下跌时旧高点被滚出窗口，"高点"从 742.0 → 741.0 → 739.5 → 736.0 → 735.5 → 734.0 一路被价格自身拉低；累计跌幅 2%+ 的过程中 `drop_pct` 始终显示 ~0.95%，做空信号严重低估。同样的问题影响维度 5 的 `peak_idx`：peak 滑出窗口时整个 streak 重置（日志可见 6 轮 → 重置 → 2/3/3/4 轮）。
2. **「持续正值 +15 分」成为逼空评分的常态底分。** `streak` 在 `BIGFLOW_WINDOW=10` 截断下永远显示 10 轮、加 15 分。逼空评分常态 15+ 起步、易触发 BLOCKED；但港股持续上行本不该等同于"逼空风险"。

**变更内容（`short_squeeze_monitor.py`）：**

- 新增 `db_get_session_high(conn, since_ts)` 辅助函数；查询 `price_history` 中 `ts >= since_ts` 的 `MAX(price)`。
- `analyze_short_entry` 维度 4：`recent_high = max(prices)` → `session_high = db_get_session_high(conn, today.isoformat())`，anchor 不会随价格漂移；文案"较近期高点"改"较日内高点"。
- `analyze_short_entry` 维度 5：用"最新连续下行 N 轮"替代"高点拒绝后连续下行"；从 `prices[-1]` 向回数 down ticks，与 peak 位置解耦，避免窗口滑动导致 streak 重置。
- 新增常量 `BIGFLOW_STREAK_WINDOW = 120`；`analyze_capital_flow` 的 streak 计数改用此独立窗口（旧版本受 `BIGFLOW_WINDOW=10` 截断导致 streak 永远 ≤ 10）。
- 持续正值分档降权：`>=2:+5, >=4:+10, >=8:+15` → `>=2:+3, >=4:+5, >=8:+8, >=16:+12`；纯持续不反转的顶格降到 +12，反转 + 持续仍给 +25。

**回归验证（手工心算 09:30-09:40 日志）：**

- 维度 4 anchor：今日 09:30 后 session high ≈ 742.0；09:33 价格 732.0 时 drop_pct 应约 1.35%（与日志显示一致），09:37 价格 730.0 时应约 1.62%（旧版仅显示 0.75%，因为窗口高点已被拉到 735.5）。
- 维度 5 streak：09:32:42 价格序列 738.0 → 737.0 → 736.0 → 734.0 → 732.0 应给出 down_streak ≥ 4，不再因 peak_idx 滑出而重置。
- BIGFLOW streak：09:30 起 streak 真实值若为 30 轮，文案应显示 30 轮（不再封顶 10）；分值 +12（旧版 +15）。
- 逼空评分常态：纯持续正值（无反转、无加速、无摆盘连偏多）的最大贡献由 +15 降到 +12；与摆盘连偏多 +8 共存时常态从 23 降到 20，BLOCKED 触发门 25 不再被无脑顶到。

**未改动（保留前迭代决策）：** 反转 + 持续仍给 +25 / `BIGFLOW_WINDOW=10` 用于反转判断（earlier 段需要负值历史）/ `analyze_short_exit` 持仓评分逻辑独立。

**决策理由：** anchor 设计原则——任何"相对参照"（高点、基准、均值）都不能用滑动窗口的极值作为 anchor，否则在持续单向行情下 anchor 会被价格自身拉走，比较失真。streak 与 window 解耦同理：streak 是"事实"（真实连续轮数），window 是"判断条件"（够不够长），二者数学含义不同，混用一个常量造成数据失真。

## 迭代十三：诱空陷阱二次治理 — 微小反转过滤 + 失衡度翻转检测（2026-05-12）

**背景：** 2026-05-12 10:00:15 一段实盘日志（迭代十二上线后）暴露经典诱空场景：

| 时刻 | 价格 | 事件 | 状态 |
|---|---|---|---|
| 09:59:14 | 724.0 | 卖盘骤减 30.8% → BLOCKED | BLOCKED |
| 09:59:44 | 724.0 | BLOCKED 余威 | BLOCKED |
| **10:00:15** | **718.0** | **资金 +252万→-15.8万 + 卖盘暴增 657% → ENTRY 88** | **ENTRY** |
| 10:00:30 | 718.0 | ENTRY 86 | ENTRY |
| 10:00:45 | 718.5 | ENTRY 98 | ENTRY |
| 10:01:00 | 720.0 | 卖盘暴减 71% → CAUTION | CAUTION |
| 10:01:16 | 720.5 | 资金回正 +104.9万 + 失衡 +0.831 → BLOCKED | BLOCKED |
| 10:02:32 | **726.0** | 价格反弹回 ENTRY 上方 +1.1% | BLOCKED |

ENTRY 持续 45 秒 → 立刻反转 → 价格 1.5 分钟反弹 +1.1%。`paper_trader.ENTRY_CONFIRM_ROUNDS=2`（30s）救不了；如按 ENTRY=98 入场会被套。

复盘两个根因：

1. **维度 1「大单净流入由正转负 +30 分」对微小波动过敏。** 累计 +252万 → -15.8万 实际变化只占峰值 6%，本质是噪音波动，但被当成完整反转加 +30。
2. **失衡度高频翻转未识别为操纵特征。** 触发 ENTRY 前 6 轮 imbalance 序列：[+0.504, +0.379, +0.152, -0.315, +0.228, -0.277]——3 次极性翻转。这种"挂单—撤单—反向挂单"的博弈是主力诱空的特征，但触发 ENTRY 那一刻 imbalance=-0.277 偏空，所以原诱空保护（要求当下 imbalance > +0.10）失效。

**变更内容（`short_squeeze_monitor.py`）：**

- 新增常量：
  - `SHORT_MICRO_REVERSAL_RATIO = 0.10` — 微小反转判定阈值
  - `SHORT_IMB_FLIP_WINDOW = 6` / `SHORT_IMB_FLIP_MIN = 3` / `SHORT_IMB_FLIP_BAND = 0.10` — 失衡度翻转检测
- 新增 `db_count_imb_flips(conn, n, neutral_band)` helper：统计最近 n 轮 imbalance 极性翻转次数，过滤 `|imb| ≤ band` 的中性轮避免 0 附近抖动误算
- `analyze_short_entry` 维度 1：增加 `is_micro` 分支。`|latest_net| < max(earlier_net 正值) × ratio` 时只给 +5 分（带"⚠ 轻微转负 / 疑似噪音"文案），不计入 `SHORT_BIGFLOW_REVERSAL` DB 信号
- `analyze_short_entry` Failsafe 2：`sig_type == "ENTRY"` 时调用 `db_count_imb_flips`，flips ≥ `SHORT_IMB_FLIP_MIN` 直接 `return 0, "BLOCKED", [reason]`，落入 DB 信号 `SHORT_IMB_FLIP_TRAP`

**回归验证（手工心算 10:00:15 触发点）：**

- 维度 1 微小反转：earlier_net 全部 +252 万；`|−15.8| / 252 = 6.3% < 10%` → micro，+5 分（旧 +30）
- Failsafe 2 翻转计数：[+0.504, +0.379, +0.152, -0.315, +0.228, -0.277] 全部 |imb| > 0.10，符号 +++−+−，翻转 3 次 → ≥ `SHORT_IMB_FLIP_MIN` → BLOCKED
- 即便没有 Failsafe 2，仅微小反转生效后该轮分值约 5+25+15+10 = 55，仍可能擦边 ENTRY；Failsafe 2 是关键安全网

**未改动：** `SHORT_TRAP_IMB_SUPPRESS=0.10` 仍保护卖盘骤增遇当下偏多的诱空 / `SHORT_TRAP_IMB_BLOCK=0.30` 强制降级仍生效 / `SHORT_SQUEEZE_LOOKBACK=4` 维持。

**决策理由：** 反转信号的"幅度"和"方向"都重要。仅看方向（正→负）会被微小波动触发；用 `|reversal| / 历史峰值` 比例过滤后，要求反转幅度有意义才打满分。失衡度翻转检测则是把"挂单博弈"这种时间序列特征显式建模——单点 imbalance 看不出操纵，6 轮内翻转 3 次的频率几乎只可能是主力对倒。

## 迭代十四：逼空评分"日级地板"治理 — 安全门三元 AND 重构（2026-05-14）

**背景：** 2026-05-14 09:42–09:45 一段实盘日志：MINIMAX-W 价格从 866.5 → 832（−4%，3 分钟），大单净流入 −3,742 万 → −5,023 万（持续放大流出），但做空入场评分始终 0、BLOCKED。逼空评分 63 / 71 / 88 / 96 循环出现，从未跌破安全线 25。复盘发现：

| 信号 | 来源 | 性质 | 分 | 是否日内可变 |
|---|---|---|---|---|
| 卖空占比高位拐头 7.74→2.21% | `analyze_short_ratio_trend` | HKEX 日级 | +25 | 否（17:00 后更新） |
| 价格高于 10 日成本线 (745) | `analyze_hkex_short_momentum` | HKEX 日级 | +20 | 几乎不 |
| 价格高于 20 日成本线 (787.8) | 同上 | HKEX 日级 | +8 | 几乎不 |
| 卖空动能比 0.40× 空头撤退中 | 同上 | HKEX 日级 | +10 | 否 |
| **日级地板小计** | | | **63** | **整日固定** |

63 分的"地板"把 `SHORT_SAFE_SQUEEZE=25` 永远焊死，无论日内行情如何下跌都无法清除。三个具体的逻辑缺陷：

1. **「卖空动能比 < 0.6× 空头撤退中 +10 逼空风险」方向反了。** `momentum_ratio < 0.6` 表示今日卖空占比仅为 5 日均值 40%——空头**正在退场**。空头筹码减少 = 后续逼空燃料减少 = 逼空风险**下降**。当前代码加到 `squeeze_risk` 完全相反。
2. **「卖空占比高位拐头 +25」缺少价格行为二阶确认。** 设计假设是"空头开始回补 → 推升股价 → 逼空启动"，但价格在跌时说明空头已清算完毕、行情进入下跌，根本不是逼空。
3. **安全门是单维度阈值**——只看 `effective_squeeze > 25`，不看价格走向与资金流方向，无法被任何日内反向行情解锁。

**变更内容（`short_squeeze_monitor.py`）：**

- **Fix 1（[~L843](short_squeeze_monitor.py#L843)）：** `analyze_hkex_short_momentum` 中 `momentum_ratio < 0.6` 的 +10 由 `squeeze_risk` 改为 `short_support`，文案"[逼空风险+10分]"改"[支撑做空+10分]"。
- **Fix 2（[~L661](short_squeeze_monitor.py#L661)）：** `analyze_short_ratio_trend` 返回签名 `(score, signals)` → `(score, support, signals)`。高位拐头计分前查 `db_get_recent_prices(SHORT_RATIO_PRICE_CONFIRM_WIN=30)`：若首尾价格 `last >= first`（价格上行/横盘），+25 仍计入 `score`；若 `last < first`（价格下行），+25 转入 `support`，DB 信号改用 `SHORT_RATIO_PEAK_BEARISH`。主循环 [~L1499](short_squeeze_monitor.py#L1499) 同步解包并把支撑信号文案分流到做空展示区。
- **Fix 3（[~L937](short_squeeze_monitor.py#L937)）：** `analyze_short_entry` 的 BLOCKED 安全门从单维度阈值改为三元 AND——`effective_squeeze > SHORT_SAFE_SQUEEZE` 触发后再查：
  - 价格确认下行：近 `SHORT_BLOCK_OVERRIDE_WIN=30` 轮首尾跌幅 ≥ `SHORT_BLOCK_PRICE_DROP_PCT=0.3%`
  - 大单持续净流出：最新 `big_net ≤ SHORT_BLOCK_BIGFLOW_THRESHOLD=-3000 万 HKD` 且近 3 轮全部为负
  
  两项同时成立时**放行**（行情已确认下行，逼空假设被价格行为否定），日志打 `[安全门放行]` 并落 DB 信号 `SHORT_BLOCK_OVERRIDE`，继续走正常评分流程；否则维持原 BLOCKED 行为。

- 新增常量：`SHORT_RATIO_PRICE_CONFIRM_WIN`、`SHORT_BLOCK_OVERRIDE_WIN`、`SHORT_BLOCK_PRICE_DROP_PCT`、`SHORT_BLOCK_BIGFLOW_THRESHOLD`。

**回归验证（手工心算 2026-05-14 09:42–09:45 触发点）：**

- Fix 1：动能比 0.40× 不再 +10 到逼空 → 日级地板从 63 降到 53。
- Fix 2：09:42 段近 30 轮价格 866.5 → 832（−4%），符合下行确认；+25 高位拐头转入 short_support → 逼空进一步降到 28，且 `short_score` 加 25 分基础。
- Fix 3：剩余逼空 28 仍超 25 → 进入放行判定。价格 −4% ≥ 0.3% 阈值；big_net = −5023 万 ≤ −3000 万阈值，近 3 轮均负 → **放行**。`analyze_short_entry` 进入维度评分。
- 三处叠加后：维度 1（大单持续负 +15）+ 维度 4（session_high 跌幅 +15）+ 维度 5（连续下行 +10）+ s1_support（+25）+ hkex_support（动能 +10）≥ 55 → ENTRY 触发。

**未改动（保留前迭代决策）：** `SHORT_SAFE_SQUEEZE=25` / `SHORT_SQUEEZE_LOOKBACK=4`（仍取窗口峰值）/ Failsafe 1（摆盘偏多降级）/ Failsafe 2（imbalance 翻转 BLOCKED）/ `SHORT_TRAP_IMB_SUPPRESS=0.10` / 微小反转过滤。

**决策理由：** HKEX 日级静态信号（成本线、动能比、占比拐头）的价值是"环境分"，但不应该当作"日内不可变的逼空地板"使用。安全门需要让日内价格行为和资金流向有一票否决权——当行情已经证明逼空假设不成立时，安全门必须放行，否则就成了"宁可错过 100 个机会也不让一单进场"的设计缺陷。三元 AND 的语义是：只在"分高 + 行情未否定 + 资金未否定"三者同时成立时才拒绝入场。

*本文档记录截止：2026-05-14。*

## 迭代十五：API 失效守门 + 价格反弹逼空维度（2026-05-18）

**背景：** 2026-05-18 09:30–09:38 实盘日志暴露两个系统盲区：

| 时间 | 价格 | 卖深 | 失衡 | 大单净 | 状态 |
|---|---|---|---|---|---|
| 09:32:58 | 770.0 | 10840 | -0.599 | +92.4万 | ENTRY=86 |
| 09:33:44 | 765.5 | 12920 | -0.682 | +92.4万 | ENTRY=98（局部低点） |
| 09:36:56 | 777.5 | 1600（冻结） | +0.509（冻结） | 499万（冻结） | **Futu API 超时①** |
| 09:37:23 | 777.5 | 1600（冻结） | +0.509（冻结） | 499万（冻结） | **API 超时②** |
| 09:37:45 | 777.5 | 1600（冻结） | +0.509（冻结） | 499万（冻结） | **网络中断（双 API 失败）** |
| 09:38:00 | **788.0**⚡ | 960 | +0.638 | 499万 | 价格跳升 +10.5 HKD |
| 09:38:30 | **792.5** | 3680 | +0.10 | +650万 | 距 765.5 反弹 **+3.5%**，逼空仍仅 20 |

两个具体缺陷：

1. **API 失败时数据"假静止"。** `fetch_capital_flow` / `fetch_order_book` 失败返回 `None`，但 `state.latest_*` 旧值被保留。仪表盘照常输出，看不出 09:36:56→09:37:45 这 3 轮其实是冻结快照。现有 `STALE_DATA_ROUNDS=5` 守门要求"价格 + 大单"都不变，3 轮 API 失败 < 5 轮门槛，无效。
2. **逼空评分对"价格反向突破"完全无感。** 09:33:44 局部低点 765.5 → 09:38:30 792.5（+3.5%、+27 HKD），全程逼空评分 ≤ 20，从未升级 BLOCKED 或离场紧迫。原逼空维度只盯卖盘骤减/摆盘偏多/大单加速，被踏空时它毫无意识。

**变更内容（`short_squeeze_monitor.py`）：**

- **Fix 1（[~L64](short_squeeze_monitor.py#L64), [~L1463](short_squeeze_monitor.py#L1463), [~L1686-L1715](short_squeeze_monitor.py#L1686-L1715)）：** 新增 `API_FAIL_TOLERANCE_ROUNDS=2`。`MonitorState` 加 `_capital_fail_count` / `_orderbook_fail_count` 两个计数器。主循环里 `cf is None` / `ob is None` 时计数 +1，成功时归零；任一计数 ≥ 2 时记 `[API 失效]` WARNING 并 `continue` 跳过本轮打分/dashboard/落库，避免基于冻结快照评分。与原 `STALE_DATA_ROUNDS=5` 守门互补——前者守"显式 API 失败"，后者守"低流动性导致数据天然不变"。

- **Fix 2（[~L97](short_squeeze_monitor.py#L97), [~L744-L780](short_squeeze_monitor.py#L744-L780), [~L1730](short_squeeze_monitor.py#L1730)）：** 新增 `analyze_price_reversal(conn, current_price)` 函数：取近 `PRICE_REVERSAL_WINDOW=30` 轮（≈7.5 分钟）滚动低点，按当前价反弹幅度三档计分：≥0.8% → +8、≥1.5% → +15、≥2.5% → +25。结果加入逼空评分 `squeeze_score`，文案进入 `squeeze_signals` 展示。
  - 选用"滚动窗口低点"而非"session_low"：让信号在反弹企稳后自然衰减，避免日内一次低点把后续整天都标为"高风险"。
  - 阈值校准：09:32:58→09:38:30 反弹 +3.5% → +25 分，叠加既有"大单净流入加速 +13"/"摆盘偏多 +8" → 逼空升至 ≥46，**超过 `SHORT_EXIT_SQUEEZE=40` 离场线**，持仓模式下立即触发 `analyze_short_exit` 止损建议。

**回归验证（手工心算 2026-05-18 09:33-09:38 案例）：**

- 09:36:56-09:37:45：3 次 API 失败连续触发 → `_capital_fail_count=3 ≥ 2`，第 2 轮起跳过打分，dashboard 不再输出陈旧快照。
- 09:38:00：API 恢复，price=788、ask_depth=960、imbalance=+0.638 重新有效；`analyze_price_reversal` 取近 30 轮低点 765.5，rebound=(788-765.5)/765.5*100=2.94% → **HEAVY 档 +25 逼空分**。
- 09:38:30：reb=(792.5-765.5)/765.5*100=3.53% → 同样 +25。即便其他维度全 0，逼空也达 25 触发 BLOCKED；叠加摆盘持续偏多 +8、大单加速 +8 后可达 41+，超过 `SHORT_EXIT_SQUEEZE=40` 触发"立即止损"。

**单元测试：**

```python
# tier 边界（保守降级、浮点安全）
+0.5% → +0  | +1.5% → +8  | +2.5% → +15 | +3.5% → +25
# 真实场景：765.5 低点 → 792.5 当前
score=25, "价格自近 30 轮低点 765.50 反弹 +3.53%（当前 792.50）[+25分]"
# 横盘 / 历史不足 → 0
```

**未改动（保留前迭代决策）：** `SHORT_SAFE_SQUEEZE=25` / `SHORT_EXIT_SQUEEZE=40` / `STALE_DATA_ROUNDS=5`（与新 API 守门互补）/ 安全门三元 AND（迭代十四）/ Failsafe 1/2 / 微小反转过滤。

**决策理由：** 原系统设计假设 API 永远可用、且逼空风险只通过盘口结构表现。实盘证明两者都不成立——Futu API 在 09:36-09:37 短暂中断属常态（业务时段网络抖动），而"被踏空"是做空策略最常见的失败模式，必须有专门维度捕捉。`analyze_price_reversal` 把"价格反向走势"显式纳入逼空评分，比单纯依赖卖盘/资金流的"二阶推断"更直接、更可解释。

## 迭代十六：拐头路径锁 + 大单单独冻结守门 + 累计大单方向感知（2026-05-20）

**背景：** 2026-05-20 09:58–10:03 实盘日志暴露两个独立但叠加的缺陷，使做空入场评分在 5 分钟内从 78(ENTRY) 跳到 23(BLOCKED) 又回到 78：

| 时间 | 价格 | 失衡 | 大单净 | 做空 | 逼空 | 备注 |
|---|---|---|---|---|---|---|
| 09:58:43 | 698.0 | +0.14 | -766.4 万 | 88(CAUTION) | 8 | 拐头 +25 走 "支撑做空" |
| 09:59:28 | 697.5 | +0.19 | **-766.4 万** | 78(ENTRY) | 8 | 大单冻结但仍按 +15 计入 |
| 10:00:13 | 698.0 | +0.26 | **-766.4 万** | **23(BLOCKED)** | **33** | 同一拐头事实改走 "逼空风险 +25" |
| 10:01:14 | 698.0 | +0.55 | **-766.4 万** | 23(BLOCKED) | 33 | 大单仍冻结；"持续为负 +15" 仍在加 |
| 10:03:45 | 701.0 | +0.67 | **-766.4 万** | 23(BLOCKED) | 33 | 大单冻结已 5 分钟 |

两个具体缺陷：

1. **拐头评分路径随滚动窗口翻转。** [analyze_short_ratio_trend](short_squeeze_monitor.py#L802-L885) 对"卖空占比连涨 N 日后回落"用 +25 计分，但根据 `SHORT_RATIO_PRICE_CONFIRM_WIN=30` 轮（≈7.5 min）滚动窗口的首尾价格判定走 `score`（逼空风险）还是 `support`（支撑做空）。窗口随时间滚动，价格 697.5→701.0 这一点点动作就让窗口首尾对比从"下行"翻成"上行"——**同一个 HKEX 拐头事实在评分两侧反复横跳**，每轮重新加 +25。这是日志里同一个文案 09:59 在做空支撑、10:00 出现在逼空风险的根因。
2. **大单累计单独冻结无守门。** `STALE_DATA_ROUNDS=5` 守门的判定是 AND（价格 + 大单都不变），价格只要动 0.5 HKD 就清零计数。但 Futu `get_capital_distribution()` 返回的累计净额可以独立冻结（无新大单 / API 复用旧值），实盘大单 -766.4 万 5 分钟没动。下游 [analyze_short_entry](short_squeeze_monitor.py#L1237) 的维度 1 "大单净流入持续为负 +15" 没有"该值是否陈旧"的判定，**继续基于陈旧累计值无差别加分**。10:01:29-44 触发过两次"数据停滞"但 10:01:59 价格动了就解除，大单仍冻结、计分仍继续。

**变更内容（`short_squeeze_monitor.py`）：**

- **Fix 1（[~L802-L885](short_squeeze_monitor.py#L802-L885), [~L1636-L1644](short_squeeze_monitor.py#L1636-L1644), [~L1866](short_squeeze_monitor.py#L1866)）：** 拐头路径锁。`MonitorState` 新增 `_ratio_lock_date` / `_ratio_lock_key` / `_ratio_lock_path` 三个字段；`analyze_short_ratio_trend` 接受可选 `state` 参数，当日首次判定 (prev, latest) 元组下走哪条路径后写入锁。后续轮命中相同 (date, prev, latest) 时直接复用 `squeeze` 或 `support` 路径，文案附 "(路径锁定·当日)"。次日 HKEX 数据更新（元组变化）或日期变化时自然失效重锁。
- **Fix 2（[~L64](short_squeeze_monitor.py#L64), [~L1641-L1642](short_squeeze_monitor.py#L1641-L1642), [~L1854-L1872](short_squeeze_monitor.py#L1854-L1872), [~L1140](short_squeeze_monitor.py#L1140), [~L1252-L1255](short_squeeze_monitor.py#L1252-L1255), [~L1894](short_squeeze_monitor.py#L1894)）：** 大单单独冻结守门。新增常量 `BIG_NET_STALE_ROUNDS=5`。`MonitorState` 加 `_prev_big_net_only` / `_big_net_stale_count`，与原 AND 守门并行追踪 `latest_big_net` 独立连续未变轮数。≥ 5 轮时主循环 `[大单停滞]` INFO 提示，并把 `big_net_stale=True` 传入 `analyze_short_entry`；维度 1 收到该 flag 时跳过整段（写入信息行 "ℹ 大单累计冻结多轮，'大单净流入'维度本轮跳过"），其它维度照常打分。

**回归验证（手工心算 2026-05-20 09:58-10:03 案例）：**

- 09:58:43：HKEX 拐头首次出现，价格窗口下行 → 锁 `support` 路径，做空入场支撑 +25。
- 10:00:13：价格窗口滚动到上行；命中 `_ratio_lock_key=(16.23, 6.68)` + `_ratio_lock_date=2026-05-20` → 复用 `support` 路径，**不再切换到逼空风险**，做空评分稳定。
- 09:58–10:03：大单从 -766.4 万开始连续未变；09:58:43→10:00:43 累积 8 轮后 `_big_net_stale_count ≥ 5` 触发 → 维度 1 (+15) 不再叠加；做空评分实际取决于价格行为 / 盘口 / 成本线，不再被陈旧大单"持续为负"持续顶高。

**未改动：** `SHORT_RATIO_PRICE_CONFIRM_WIN=30`（首次判定仍使用，仅"复用"环节走锁）、`STALE_DATA_ROUNDS=5`（与新单独冻结守门并行）、安全门三元 AND（迭代十四）、`analyze_short_entry` 维度 2-5。

### Fix 3：累计大单方向感知盲区（Bug 16）

**背景：** 2026-05-20 10:19-10:25 实盘日志（与 Bug 14/15 同段）暴露：

| 时间 | 价格 | 大单累计 | 单轮 Δ | 状态 |
|---|---|---|---|---|
| 10:19:54 | 698.0 | -1083.2 万 | — | 逼空 66 / 做空 23 |
| 10:20:55 | 701.0 | -988.0 万 | **+95 万** | 逼空 53 |
| 10:21:55 | 705.0 | -893.9 万 | **+94 万** | 逼空 48 |
| 10:25:12 | 705.5 | -893.9 万 | 0 | 逼空 33 |

近 5 分钟实际 +189 万买入 + 价格 +1.4%，**方向已转买入**，但 `analyze_short_entry` 维度 1 仅按累计静态值判"持续为负 +15"，对资金转向无感；`analyze_capital_flow` 也只看"由正转负"和"持续正值"，对"累计为负但单轮转正"完全没有维度。仪表盘 ②大单净流入 只展示静态累计，单轮 Δ 完全被淹没——用户看到"大单流出"却得到逼空风险升高的提示，理解链路断裂。

**变更内容（`short_squeeze_monitor.py`）：**

- **常量**（[~L80-L84](short_squeeze_monitor.py#L80-L84)）：新增 `BIG_NET_DELTA_THRESHOLD=500_000`（单轮 Δ ≥ 50 万港元算显著买入）、`BIG_NET_REBUY_PRICE_PCT=0.3`（同期价格涨幅 ≥ 0.3% 算方向咬合）。
- **State**（[~L1631](short_squeeze_monitor.py#L1631)）：`recent_big_net_delta` 字段存近一个 capital_flow 快照间 Δ（约 1 分钟）。`capital_flow` 表已按 `update_time` 去重（Bug 5 修复），相邻两行天然跨 1 分钟，`db_get_recent_big_net(conn, 2)` 取首尾差就是真实 Δ。
- **主循环**（[~L1867-L1872](short_squeeze_monitor.py#L1867-L1872)）：`cf` 成功后查 `_bn_recent = db_get_recent_big_net(conn, 2)`，写入 `state.recent_big_net_delta`。
- **Dashboard**（[~L1701](short_squeeze_monitor.py#L1701)）：②大单净流入 行后追加 "近Δ ±XX.X万"，让"累计静态值"和"近期方向"并排可见。
- **`analyze_capital_flow`**（[~L676-L696](short_squeeze_monitor.py#L676-L696)）：新增分支——`recent[0] < 0` 且 `recent[0] - recent[1] >= THRESHOLD` 且近 8 轮价格涨幅 ≥ 0.3% 时，落 +10 逼空风险分 + `BIG_FLOW_REBUY` DB 信号，文案 "大单累计 X 万仍为负但近期 Δ +Y 万买入，价格 +Z% 咬合 → 空头回补/多头进场"。
- **`analyze_short_entry` 维度 1**（[~L1287-L1297](short_squeeze_monitor.py#L1287-L1297)）：`elif latest_net < 0` 分支前置检查近期 Δ；若 `big_nets[0] - big_nets[1] >= THRESHOLD` 则不计 "持续为负 +15"，改输出说明行 "ℹ 大单累计 X 万但近期 Δ +Y 万买入，不计'持续为负'分"。其它维度照常打分。

**回归验证（手工心算 2026-05-20 10:19-10:25 案例）：**

- 10:20:55 这一轮：`recent=[-988万, -1083.2万]`，`Δ = +95.2 万 ≥ 50 万`；近 8 轮价格 698→701，涨幅 +0.43% ≥ 0.3% → 触发 `BIG_FLOW_REBUY` +10 逼空风险分，文案展示资金方向转向。
- 同轮 `analyze_short_entry` 维度 1：检测 `Δ +95.2 万`，跳过 +15"持续为负"，转写说明行；做空入场评分降低（不再被陈旧累计静态值顶高）。
- Dashboard ②：`-988.0 万   近Δ +95.2万`，两个数字并排，用户一眼看到"累计虽负但近期在买"。

**未改动：** `BIGFLOW_REVERSAL_MIN=2`、`BIGFLOW_WINDOW=10`、Bug 13 出货式拉升检测（流入持续正 + 价格停滞 → 不计分）、`analyze_short_entry` 维度 2-5。

**决策理由：**
- **累计静态值是滞后量**。 日内累计混了早盘大额、午前波动、近 5 分钟实际成交，靠它判方向必然滞后。"近期 Δ + 价格咬合" 是真实方向的最简近似。
- **方向矩阵替代单维度判定**。 大单方向 × 价格方向有 4 种组合（详见 [[known_bugs_and_gotchas]] Bug 16 条目），单看大单或单看价格都会被反向利用。本 Fix 把"累计负 + 单轮转正 + 价格涨"建模成显式维度，与 Bug 13 待修的"累计正 + 价格停滞 = 出货式拉升" 对称——同一个矩阵的另一格。
- **仪表盘必须展示"评分用的真实量"**。 评分用近期 Δ，仪表盘也得展示近期 Δ；不能评分用一个数、展示另一个数，否则用户永远不能验证评分的合理性。

### Fix 4：Failsafe 1/2 被 support 升级绕过（Bug 18）

**背景：** 2026-05-20 13:36-13:42 实盘日志，连续 7 分钟做空入场显示 63-78(ENTRY)，但 imbalance 整段稳定在 **+0.92 ~ +0.97**（极端买盘强势，远超 `SHORT_TRAP_IMB_BLOCK=0.30`），按 Bug 6 设计应强制降级 CAUTION 防底部追空，但实际整段没降级。

| 时间 | 价格 | imb | 大单累计 | 做空 |
|---|---|---|---|---|
| 13:36:08 | 693.5 | +0.918 | -1811 万（冻结开始）| **78(ENTRY)** |
| 13:36:23 | 693.5 | +0.924 | -1811 万（冻结跳过）| 63(ENTRY) |
| 13:39:55 | 696.0 | +0.598 | -1811 万 | 63(ENTRY) |
| 13:40:40 | 694.0 | +0.946 | -1811 万 | **73(ENTRY)** |
| 13:42:11 | 693.0 | +0.956 | -1915 万（Δ-104万）| 48(BLOCKED·逼空 33) |

**根因（评分汇合链路）：**

1. `analyze_short_entry` 内部 `score` 只计维度 1-5 的主信号分。Bug 14 修复后维度 1 在大单冻结时跳过 → 维度 1=0、维度 2/3/5=0、维度 4 价格跌 +15 → `score=15`。
2. `sig_type` 由内部 score 判定：`15 < 33 → HOLD`。
3. [Failsafe 1（line 1428）](short_squeeze_monitor.py#L1428): `if sig_type == "ENTRY" and current_imbalance > 0.30` → sig_type 是 HOLD，**永不触发**。
4. 主循环 [line 2011-2024](short_squeeze_monitor.py#L2011-L2024) 把 `short_score = 15 + hkex_support(23) + s1_support(25) = 63`，HOLD 升级 ENTRY。
5. 升级后**不再回头检查 imbalance**，Failsafe 1/2 完全被绕过。

**变更内容（`short_squeeze_monitor.py`）：**

- **抽出 [apply_short_entry_failsafes](short_squeeze_monitor.py#L1197-L1238)：** Failsafe 1/2 从 `analyze_short_entry` 内部抽为独立函数，签名 `(conn, score, sig_type, imbalance, signals) -> (score, sig_type, signals)`，BLOCKED 时丢弃 signals 只保留 reason（与原 in-place 行为一致）。
- **`analyze_short_entry` 末尾**（[~L1486](short_squeeze_monitor.py#L1486)）：替换原内联 Failsafe 1/2 为 `return apply_short_entry_failsafes(...)`，行为不变。
- **主循环 ENTRY 升级后**（[~L2030-L2037](short_squeeze_monitor.py#L2030-L2037)）：合并 hkex/s1/pump 支撑分并升级 ENTRY 后，再次调用 `apply_short_entry_failsafes(conn, short_score, short_signal, state.latest_imbalance, short_sigs)`，让"被升级到 ENTRY"也经历同样的诱空保护。

**回归验证（手工心算 2026-05-20 13:36:08 案例）：**

- `analyze_short_entry` 返回 `(score=15, sig_type=HOLD, signals=[价格跌 +15])`，Failsafe 1 因 sig_type=HOLD 不触发。
- 主循环 `short_score = 15+23+25 = 63 ≥ 55`，HOLD 升级 ENTRY。
- 调用 `apply_short_entry_failsafes(conn, 63, "ENTRY", +0.918, signals)` → Failsafe 1 检测 `+0.918 > 0.30`，sig_type 降级 CAUTION，附加"⚠ 摆盘失衡度 +0.918 明显偏多 …"。
- 最终输出：做空 63(**CAUTION**) 而非 63(ENTRY) ✓

**未改动：** Failsafe 1/2 阈值（`SHORT_TRAP_IMB_BLOCK=0.30`、`SHORT_IMB_FLIP_WINDOW=6` / `SHORT_IMB_FLIP_MIN=2` / `SHORT_IMB_FLIP_BAND=0.10`）、`SHORT_ENTRY_MIN=55`、"仅在 HOLD 时升级"的合并规则（Bug 6）。

**决策理由：**
- **failsafe 必须紧贴最终 sig_type，而不是 in-flight 状态**。原设计假设 sig_type 在 analyze_short_entry 内已经定型，主循环只是搬运；实际加了 support 合并后，sig_type 在主循环阶段才定型，failsafe 也必须跟到这里。
- **support 合并应保留 score 来源信息但不改变保护机制**。把 score 拆成"主信号 + support"是为了让"卖空占比高位拐头"等日级背景分参与最终评分，但保护机制（防底部追空、防挂单博弈）只看终态环境，不区分 score 来源——因此 failsafe 必须在终态上重跑。

### Fix 5：仪表盘展示主信号 vs 背景分（Bug 19）

**背景：** 2026-05-20 14:14-14:16 实盘日志，做空入场显示 **63/100**，但其中：

| 来源 | 分 | "此刻"有效性 |
|---|---|---|
| 维度 4 价格距日高 -5.34% | +15 | 此刻盘面 |
| 维度 1/2/3/5 | 0 | 大单冻结跳过 / 卖盘骤增被诱空检测拦截 / 失衡度极端偏多反向 / 价格横盘无连续下行 |
| **主信号分** | **15** | — |
| HKEX 拐头空头清算（路径锁·当日） | +25 | 昨日数据 |
| 距 10 日成本线 -10.4% | +15 | 10 日均线 |
| 距 20 日成本线 -10.7% | +8 | 20 日均线 |
| **日级背景分** | **48** | 整日基本不变 |
| **总分** | **63** | |

总分 63 看起来"高位接近 ENTRY 阈值 55"，但**真实盘中信号只有 15 分**，48 分是早上 9 点之前就锁定的日级背景。Bug 10（迭代十四）修复了"日级地板焊死安全门"的逻辑层面，但展示层一直是合并显示——用户读到 63 容易误判"此刻多个信号共振"。

**变更内容（`short_squeeze_monitor.py`）：**

- **State**（[~L1639-L1640](short_squeeze_monitor.py#L1639-L1640)）：`MonitorState` 新增 `main_signal_score` / `support_score` 两个字段，分别记录维度 1-5 主信号分和（hkex_support + s1_support + pump_support）背景分。
- **主循环**（[~L2018-L2024](short_squeeze_monitor.py#L2018-L2024)）：在合并 `short_score = main + support` 之前，分别保留两个变量；最终 `apply_short_entry_failsafes` 之后写回 state。
- **Dashboard**（[~L1823-L1826](short_squeeze_monitor.py#L1823-L1826)）：【做空入场】行末追加 `(主XX + 背景XX)` 拆分，例如 `63/100  (主15 + 背景48)`。

**读法约定：**
- 主 ≥ 30 + failsafe 放行 → 真正盘中有共振，可考虑
- 主 < 20 + 总分 ≥ 55 → 大部分由日级背景顶高，盘中信号薄弱，应忽略总分高位
- failsafe 触发的 CAUTION/BLOCKED 永远优先于总分阈值

**未改动：** 评分逻辑（主信号和背景的具体计分维度）、failsafe 阈值、ENTRY 升级规则。

**决策理由：**
- **展示要与决策语义对齐**。Bug 10 在逻辑层承认"日级背景 ≠ 盘中信号"（设了三元 AND 安全门放行机制），但展示层仍合并显示总分；用户决策时只看到总分，绕开了 Bug 10 的语义。本 Fix 让展示层和逻辑层口径一致。
- **不修改评分本身**。日级背景分还在加（评分对回测、阈值标定等下游消费方有意义），仅在展示时拆开让人看清结构。这样既保持原有评分机制完整，又避免读者被合并值误导。

## 迭代十七：新增 long_entry_monitor.py — 不可做空标的的做多入场监控（2026-05-20）

### 背景
- 思格新能 (06656.HK) 已添加到 STOCKS 配置，但**不支持买空**。
- 做空套利系统对它无效；用户需要一个对称的「做多入场」分析工具，复用同一份盘口数据但用相反逻辑评分。

### 设计原则：复用而非分叉
- 新建 `long_entry_monitor.py`，**import** `short_squeeze_monitor.py` 的基础设施（`init_db` / `db_save_*` / `fetch_capital_flow` / `fetch_order_book` / `_is_trading_hours` 等）。
- DB 表完全复用：`orderbook_snapshots` / `capital_flow` / `price_history` / `signals` 同一套；做空和做多脚本各自的信号通过 `signal_type` 前缀（`LONG_*`）区分。
- 新增独立的 `long_monitor_state` 单行表（与 `monitor_state` 隔离），避免做空脚本写入的状态被做多脚本覆盖。
- 启动时通过 `ssm.SYMBOL = SYMBOL` 同步覆盖 short 模块的全局变量，使 import 进来的 `fetch_*` 函数读取正确股票。

### 四路评分维度（满分 90）

| 维 | 名称 | 满分 | 触发逻辑 |
|---|---|---|---|
| ① | 大单净流入加速 | 30 | 近 4 轮 Δ 中位 ≥ 50 万且累计为正 → 30；中位 ≥ 25 万 → 20；仅累计正但 Δ 弱 → 10。配套价格咬合：若 sync_prices 涨幅 < 0% → 全档降权一档。 |
| ② | 摆盘失衡度持续为正 | 20 | 连续 2 轮 imb > 0.30 → 20；仅当前轮满足 → 8。 |
| ③ | 卖盘萎缩 + 买盘堆积 | 25 | 近 K 轮卖盘中位较 60 轮基准 ↓≥30% → +15；买盘 ↑≥30% → +10。双向同时满足封顶 25。 |
| ④ | 超卖反弹 | 15 | 前 3 轮跌幅 ≤ -0.3% AND 后 2 轮反弹 ≥ +0.1% AND (买盘突增 OR 大单 Δ ≥ 50 万) → 15；单条件满足 → 7。 |

**信号分档**：ENTRY ≥ 50；CAUTION ≥ 30（=50×0.6）；HOLD。

### Failsafe（防虚假买入）

| 守门 | 触发 | 动作 |
|---|---|---|
| 诱多①：大单买但价不涨 | 维度 ① 触发，但 sync_prices 涨幅 < 0% | 维度 ① 降一档 |
| 诱多②：极端正失衡但价格未涨 | imb 均值 > 0.70 且近 3 轮价格未上涨 | 维度 ② 不计分，记 `LONG_IMB_TRAP` |
| 观望盘陷阱 | 卖盘萎缩但当前 imb < -0.10 | 维度 ③ 卖盘萎缩部分不计分 |
| 挂单博弈 | 复用 `db_count_imb_flips`，近 6 轮翻转 ≥ 2 | ENTRY → CAUTION，记 `LONG_IMB_FLIP_GUARD` |
| 追高守门 | 日内涨幅 ≥ +3% | ENTRY → CAUTION，记 `LONG_PUMP_GUARD` |
| 数据停滞 / API 失败 | 与 short 同条件 | 跳过本轮打分 |
| 非交易时段 | `_is_trading_hours` False | 跳过打分 |

### 关键设计决策

**满分 90 而非 100**：4 维相加封顶 = 90，余 10 分给后续扩展（MA/VWAP 类背景维度）。阈值 50/90 ≈ 55%，与做空 55/100 占比对齐。

**不加日级背景分**：HKEX 卖空数据是空头工具，对做多帮助有限；首版只用盘中实时维度，避免引入与做多决策无关的噪音。

**不复用 monitor_state 加列**：short 脚本用 `INSERT OR REPLACE VALUES (1,?,?,...)` 8 字段位置写入，加列会破坏向后兼容。新建独立表后，做空/做多可**并行运行**写各自状态而互不干扰。

### CLI
```bash
python long_entry_monitor.py --stock 06656            # 启动监控
python long_entry_monitor.py --stock 06656 signals    # 查看 LONG_* 触发记录
python long_entry_monitor.py --stock 06656 export     # 导出快照 CSV
```

### MVP 未覆盖
- 持仓追踪 / 平仓建议（类似 `short_position_manager.py` 的做多版本）。
- 集成到 `paper_trader.py` 自动下单。
- 日级背景信号（MA、VWAP、近 N 日成本均价）。

### 冒烟测试结果
四个合成场景验证通过：
1. 综合利多盘面（大单加速 + 失衡偏多 + 双向盘口 + 反弹）→ Score 62/90 = ENTRY ✓
2. 评分够但日内涨 3.9% → 追高守门降级 CAUTION ✓
3. 持续卖压 → Score 0 = HOLD ✓
4. 极端失衡 +0.765 但价格停滞 → 诱多陷阱识别，维度 ② 不计分 = HOLD ✓

## 迭代十八：脏价格守门 — 拒绝 Futu 异常报价污染价差计算（2026-05-26）

### 背景

实盘 2026-05-26 11:18:56 监控进程崩溃：

```
File "short_squeeze_monitor.py", line 1284, in analyze_short_entry
    price_move_pct = (prices[-1] - prices[0]) / prices[0] * 100
ZeroDivisionError: float division by zero
```

崩溃前 90 秒日志：
```
11:17:33 [WARNING] get_capital_distribution 失败: 获取资金分布请求超时
11:17:59 [WARNING] get_capital_distribution 失败: 获取资金分布请求超时
11:18:25 [WARNING] get_capital_distribution 失败: 获取资金分布请求超时
11:18:40 [WARNING] get_capital_distribution 失败: 网络中断
11:18:40 [WARNING] get_order_book 失败: 网络中断
```

### 根因

- Futu OpenD 网络抖动期间，`get_stock_quote` 仍返回 RET_OK，但 `last_price` 字段偶尔回 `0`/`NaN`。
- `db_save_price` 未校验直接写入 `price_history`。
- 安全门放行分支取最近 `SHORT_BLOCK_OVERRIDE_WIN` 轮价格，仅检 `len(prices) >= 5`，未检 `prices[0] > 0` → 0 价做除数崩溃。
- 同一脆弱模式还存在于 [L649](short_squeeze_monitor.py#L649)、[L688](short_squeeze_monitor.py#L688)、[L913](short_squeeze_monitor.py#L913)，只是当时没踩中。

### 变更内容（`short_squeeze_monitor.py`）

- **源头守门**：`db_save_price` 拒绝 `price` 为 `None`/`NaN`/`<= 0`，记录 WARNING；`db_get_recent_prices` 兜底过滤历史脏值。
- **报价层守门**：主循环读 Futu `last_price` 后立即校验 `math.isfinite(_lp) and _lp > 0`，不合格直接跳过本轮 state/DB 更新（不影响后续维度，因为 `current_price` 沿用上一轮）。
- **除法守门**：四处 `(prices[-1] - prices[0]) / prices[0]` 全部加 `prices[0] > 0` 前置检查，与原 `len >= 5` 并联（[L649](short_squeeze_monitor.py#L649)、[L688](short_squeeze_monitor.py#L688)、[L900](short_squeeze_monitor.py#L900)、[L1284](short_squeeze_monitor.py#L1284)）。

### 决策理由

- **守门下沉到源头**。如果只在每个除法点加判断，新增使用价格的代码每次都得记得这件事；在 `db_save_price` / `db_get_recent_prices` 守门，下游就不需要逐个加固。除法点的 `prices[0] > 0` 是兜底——历史 DB 里可能已有 0 价快照。
- **抖动时段优雅退化而不崩溃**。Futu 网络抖动是常态，监控进程崩溃比拿到一轮"用上一轮价格"的近似结果代价高得多。

### 未改动

- 评分逻辑、阈值、安全门三元 AND 设计。
- `fetch_capital_flow` / `fetch_order_book` 的 API 失效守门（已有 `_capital_fail_count`、`_orderbook_fail_count` 机制）。

## 迭代十九：出货式拉升检测的反向漏判修复（Bug 20，2026-05-26）

### 背景

2026-05-26 13:36:43 实盘日志：

```
⚠ ⚠ 大单净流入持续 11 轮但价格 +0.25%（近 11 轮停滞），
   疑似出货式拉升，不计逼空分
```

此条触发时：
- 大单累计 +1,089 万（从早盘 -6,659 万一路转正）
- 价格 808（早盘低点 720，日内 +12.2%）
- 30 轮低点已从 720 漂到 778.5（底部连续抬高）
- 系统刚刚在 13:34:27 报出 **逼空 100/100**

**问题**：所谓"近 11 轮停滞 +0.25%"是事实，但解读为出货式拉升是错的——价格今天已经走完 +12% 主升浪，现在是高位整理。系统把"涨完之后停一下" 误判为 "涨不动了在出货"，逼空分因此被错误降权。

### 根因

[Bug 13 修复 (迭代十三)](#迭代十三诱空陷阱二次治理--微小反转过滤--失衡度翻转检测2026-05-12) 时只考虑了 5/18 那种"持续买入但价格不动 = 出货"的剧本。当时缺失对称的反例——"先大涨后停滞 = 高位整理"。两种盘面在"近期窗口停滞"这一个指标上完全相同，需要更高维度的 anchor 才能区分。

### 变更内容（`short_squeeze_monitor.py`）

- **新增常量** ([~L88-L90](short_squeeze_monitor.py#L88-L90))：`BIGFLOW_PUMP_STAGNANT_PCT=0.3`（已有阈值常量化）、`BIGFLOW_PUMP_INTRADAY_MIN=2.0`（日内累计涨幅前置条件）。
- **新增 `db_get_session_low`** ([~L316-L325](short_squeeze_monitor.py#L316-L325))：与 `db_get_session_high` 对称，返回日内最低价 anchor。SQL 加 `price > 0` 过滤已写入的零值脏数据。
- **`analyze_capital_flow` 出货检测改造** ([~L654-L705](short_squeeze_monitor.py#L654-L705))：
  ```
  is_local_stagnant     = 近 N 轮涨幅 < 0.3%
  is_intraday_rallied   = 距日内低点 ≥ 2%
  ─────────────────────────────────────
  停滞 + 日内未涨    → 真出货嫌疑（旧行为，不计分）
  停滞 + 日内已大涨  → 高位整理（Bug 20 新路径，仍按 streak 分档计分）
  非停滞              → 原有分档计分
  ```
  消息文案对"高位整理"路径添加 `日内已涨 +X.X% 近 N 轮高位整理 ±X.X%` 说明，让用户在仪表盘上能直接看到 anchor 信息。

### 决策理由

- **同一统计指标可以对应两种相反盘面，必须用更长周期 anchor 消歧**。"近 11 轮 +0.25%" 是个二阶导数信号；要识别它的方向意义，必须看一阶（日内累计涨跌幅）。这与 Bug 8 的 anchor 漂移修复同源——滑动窗口在持续单边行情中会自我误导。
- **不改变 Bug 13 的核心保护语义**。出货式拉升识别仍存在，只是加了前置条件。5/18 那个剧本（持续买入 + 全天涨幅 +2.5% 后停滞）依然会被识别为出货——因为 +2.5% 是边缘情况，恰好低于阈值 2.0% 的可调空间。如未来发现漏报可上调阈值至 3.0%。
- **对称性**：`db_get_session_low` 与 `db_get_session_high` 配对存在，避免单边 anchor 函数。

### 未改动

- Bug 13 已有的"持续正值底分 3-12"分档（仅在新增路径下复用）。
- `BIG_NET_DELTA_THRESHOLD` / `BIG_NET_REBUY_PRICE_PCT` 等其它资金流参数。

### 与今日日志的对照验证

13:36:43 这条用旧逻辑产生 +0 分（误判出货），新逻辑下：
- `price_pct ≈ +0.25%` < 0.3% → `is_local_stagnant = True`
- `session_low = 720, current = 808` → `intraday_gain ≈ +12.2%` ≥ 2.0% → `is_intraday_rallied = True`
- streak = 11 ≥ 8 → +8 分
- 文案：`大单净流入持续正值 11 轮（日内已涨 +12.2%，近 11 轮高位整理 +0.25%）[+8分]`

逼空总分在该轮原应为 65（实际报 65 但因维度 1 计 0 而非 8，少了 8 分）；新逻辑下回到正确的 73。

## 迭代二十：出货式拉升检测的顶部回落守门（Bug 21，2026-05-26）

### 背景

迭代十九刚修完 Bug 20（上行段误判出货）两小时后，13:50 之后实盘出现对称死角：

```
13:47:17  价格 836.5 (日内最高)
   ↓ -3.8%
14:05:14  价格 807.0
大单累计  +5,635 万 → +6,446 万 (+810 万买入)
```

价格已经见顶回落，大单仍在持续净流入——这是 Bug 13 设计要捕获的**真**出货式拉升场景。但 Bug 20 修复用 `日内累计涨幅 ≥ 2%` 作为前置条件，从近期低点 720 到 14:05 的 807 仍然 +12.1%——**条件成立 → 系统判定为"高位整理"继续给分**。

### 根因

`日内累计涨幅`是个**滞后指标**。当价格从 836 跌回 807 时：
- "距日内低点 720 还有 +12%" ✅ 仍然成立
- 但"距日内高点 836 已跌 -3.5%" ❌ 表明动能已逆转

仅看一个 anchor（low）不够。Bug 13/20/21 三条规则需要**对称的双 anchor**：低点决定"是否在主升过"，近期峰值决定"是否还在升"。

### 变更内容（`short_squeeze_monitor.py`）

- **新增常量** ([~L93-L95](short_squeeze_monitor.py#L93-L95))：
  ```python
  BIGFLOW_PUMP_PEAK_PULLBACK_PCT = 1.5  # 从近 N 轮峰值回落 ≥ 此 % 视为顶部已现
  BIGFLOW_PUMP_PEAK_WINDOW = 30        # 近期峰值参考窗口（约 7.5 分钟）
  ```
  独立窗口而非复用 `sync_window`，因 sync_window 跟 streak 联动（streak 越长窗口越大），会让 anchor 漂移失效。

- **`analyze_capital_flow` 改三元判断** ([~L676-L735](short_squeeze_monitor.py#L676-L735))：
  ```
  停滞 + 日内未涨               → 真出货（Bug 13 原路径）
  停滞 + 日内已涨 + 已从峰值回落  → 真出货（Bug 21 新增）
  停滞 + 日内已涨 + 未从峰值回落  → 高位整理（Bug 20 路径）
  ```
  Bug 21 路径用独立文案：`⚠ 大单净流入持续 N 轮但价格已从近 30 轮峰值回落 X.XX%，疑似顶部出货，不计逼空分`，区别于 Bug 13 的"低位停滞"语义。

### 用真实样本验证

| 实盘样本 | streak | 局部停滞 | 日内已涨 | 峰值回落 | 旧逻辑 | 新逻辑 |
|---|---|---|---|---|---|---|
| 13:36 上行段（808） | 11 | ✅ +0.25% | ✅ +12.2% | ❌ <0.5% | 出货（误） | 高位整理 +8 |
| 14:05 回落段（807） | 119 | ✅ -0.86% | ✅ +12.1% | ✅ -1.94% | 高位整理（误，Bug 20 路径） | **顶部出货 0** |

### 决策理由

- **anchor 必须成对**。任何"已经走了多远"的指标（距低点）必须配一个"最近在往哪走"的指标（距峰值）。Bug 8 修复滑动窗口 anchor 漂移是同一类教训：anchor 单边定位会被新数据自我误导。
- **阈值 1.5% 偏严**。多头主升过程中正常回踩 0.5-1% 不会触发，确保 Bug 20 路径在真上行段不会被反向误杀。如未来出现 1.2-1.5% 回踩后再创新高的"过严"案例，可下调到 1.2%。
- **不改变 Bug 13/20 的核心语义**。三条规则各管一段盘面：
  - Bug 13：低位横盘的出货
  - Bug 20：主升后的健康整理
  - Bug 21：见顶后的高位派发

### 未改动

- 其它 streak 分档计分（3-12 分）。
- `analyze_short_entry` 维度 1-5（消费 score，但不关心 score 来源路径）。
- 仪表盘展示层（新文案在 signals 列表自动呈现）。

## 迭代二十一：候选股扫描器 watchlist_scanner.py（2026-05-26）

### 背景

2026-05-26 实盘走完 00100 +19.6% 真逼空全过程后，沉淀出"找下一只 00100"的六维筛选模型（见 [[squeeze_template_minimax_00100]]）。模板是经验，还需要工具自动扫描候选池。

### 变更

- **`shared_config.py`** 新增 `WATCHLIST: list[str]` —— 仅代码（5 位字符串），扫描器自取静态信息。初版含已监控的 4 只 + 注释掉的备选候选（用户按需取消注释）。同时给 `STOCKS['00981']` 标记 TODO（疑似 SMIC 误填为思格新能）。
- **新建 `watchlist_scanner.py`**：
  - 复用 `short_squeeze_monitor.scrape_hkex_short` 抓 HKEX 近 5 日卖空占比
  - Futu API 拉静态信息：`get_stock_basicinfo`（上市日期 + W 类判定）、`get_market_snapshot`（流通市值）、`request_history_kline`（近 20 日均成交额）
  - 六维评分（HKEX 占比 / 流通市值 / 上市时间 / W 类 / 日成交 / 单价）
  - 输出格式：控制台表格 + `watchlist_scan_YYYYMMDD.json` 明细
  - 命令行：`python3 watchlist_scanner.py` 跑 WATCHLIST，`python3 watchlist_scanner.py 00100 02513` 临时指定

### 阈值校准

模板原本写"80 入池 / 90 重点"是含人工评分（借券池 +20 / 题材 +10）的总分。scanner 仅产出自动分（满分 73），故阈值下调对应：

| 阈值 | 自动分 | 含人工总分 |
|---|---|---|
| 入池 | 60 | 80 |
| 重点 | 70 | 90 |

00100 真标本自动分 = 73（HKEX 9.47% +28 / 流通 165 亿 +15 / 上市 1.2 年 +10 / W 类 +10 / 日成交 3.8 亿 +5 / 单价 805 +5）→ "重点"。验证：腾讯（00700 模拟）= 5 分，完全不入池。

### 设计原则

- **静态信息独立缓存**：扫描器拿的 `listing_date` / `market_cap` 都是日级数据，但 scanner 每天重跑一次也只多耗 N × 200ms（可接受），故不引入持久化缓存——简化优先于性能。
- **HKEX 占比要近 N 日均值**：单日数据噪声大（5 日均更稳）。复用 ssm 模块的 scraper 而非重写——降低维护负担。
- **借券池状态不入自动分**：富途普通账户无法实时查可卖空数量，强行硬编码会让评分失真。改为输出固定提示 `⚠ 富途融券池状态需人工确认`，避免误导。
- **WATCHLIST vs STOCKS 解耦**：STOCKS 是"持续监控池"（需 db_path / poll_interval 等运行时配置），WATCHLIST 是"扫描候选池"（只要代码）。两者交集是已监控股，差集是待评估股。

### 未来扩展（roadmap）

- 自动写入"近 N 日扫描历史"，画出每只股票的评分时间序列
- 当某只股票评分跨过 60 阈值时推送通知（Webhook / iOS 通知 / 企业微信）
- 加入"近 N 日股价波幅"维度（高波动股本身就是配方的一部分）

---

## 迭代二十二：资金结构背离维度（中小单 vs 大单，2026-05-28）

### 背景

2026-05-28 10:04-10:09 实盘日志显示价格从 835 缓涨至 843，期间大单累计冻结于
+337.3 万长达 27 轮（约 7 分钟），价格上行完全没有新增大资金推动。系统现有的
"出货式拉升检测"（Bug 13/20/21）只看大单 streak + 价格关系，无法回答"小单/散户
在做什么"——而 Futu `get_capital_distribution()` 实际返回了 `mid_net` 和
`small_net`，数据早已落入 `capital_flow` 表的 `mid_net`/`small_net` 列，分析侧
完全没用。

### 设计

新增 `analyze_capital_structure()`，三档资金横向交叉验证：

- **场景 1（出货确认）**：大单连续 ≥3 轮正值 + 中小单合计连续 ≥3 轮净流出
  超过 `-20 万` 阈值 → 主力接散户抛盘，输出 `+15 支撑做空分`，写
  `CAPITAL_STRUCT_DIVERGE` 信号。
- **场景 2（弱背离）**：大单正 + 中小单弱负但未达阈值 → 对逼空评分扣 `-8`
  作为折扣（不计支撑做空分），防止假逼空被打满。
- **场景 3（同向确认）**：大单 + 中小单全档同向流入 → 信息性提示"买盘真实"，
  不加分（不重复奖励，避免双计）。

### 变更内容（`short_squeeze_monitor.py`）

- 新增常量 `CAPITAL_STRUCT_WINDOW=5`、`CAPITAL_STRUCT_DIVERGE_ROUNDS=3`、
  `CAPITAL_STRUCT_SMALL_THRESHOLD=-200_000`。
- 新增 `db_get_recent_capital_structure()` 查询助手。
- 新增 `analyze_capital_structure()` 评分函数。
- `MonitorState` 增加 `latest_mid_net`、`latest_small_net` 字段。
- 主循环：`fetch_capital_flow` 后写入 state；评分阶段调用结构分析，负分计入
  `squeeze_damper` 抑制逼空总分，正分计入 `support_score`。
- 仪表盘新增 "中小单净额 (累计)" 行，括号右显示散单净额。
- 日志摘要追加 `中小单=±XX.X万` 字段。

### 决策理由

- **三档同向不加分**：现有"大单净流入持续正值"已经给到 +25/+12 等分数；如果三
  档同向再额外加分，会出现同一份"买入真"被双计入风险。改为仅打印"买盘真实"
  做信息标记。
- **背离走 support 而非 squeeze**：与现有 `analyze_distribution_pump` 的语义
  一致——出货嫌疑是支撑做空的论据，而不是"反向逼空"。这样 BLOCKED 阈值不会
  被打掉，但 ENTRY 路径能拿到这一分。
- **阈值 -20 万**：参考 `BIG_NET_DELTA_THRESHOLD=50 万` 的量级。中小单单轮净额
  通常比大单小一个量级，-20 万足以过滤噪音、又不至于太严苛。后续根据实盘
  调优。

### 与今日日志的对照（待验证）

理论上 2026-05-28 10:04-10:09 这段"大单冻结 + 价格缓涨"如果同期中小单是
持续净流出，新维度会输出场景 1 信号，把逼空分从 100 砍下，并给做空入场
+15 支撑分。需要下一交易日实盘验证该假设。

### 未改动

- `analyze_distribution_pump`（Bug 13）的横向交叉验证逻辑保留——它看的是反弹
  递减 + HKEX 占比，与资金结构是互补维度而非替代。
- `analyze_capital_flow` 维度的"大单净流入持续正值"加分不变——只在结构背离
  时通过 `squeeze_damper` 抵扣，不直接改 `analyze_capital_flow` 内部逻辑，
  避免引入 cross-function 状态。

---

## 迭代二十三：资金分析进阶四维（散户/拆单/效率，2026-05-28）

### 背景

迭代二十二（资金结构背离）只看大单 vs 中小单的方向背离，无法回答更细的问题：
散户什么时候耗尽？资金被流动性吸收没？中单是规律拆单还是随机噪音？2026-05-28
午盘实盘观察到几类典型场景缺失维度：

- 13:32-13:47 散户 small_net 从 +2,557 万见顶后开始回吐，但系统没有"撤退"信号
- 10:04-10:09 大单流入 +337 万但价格几乎不动，资金效率明显偏低
- 13:46:30 价格在 845-847 窄幅震荡时散户加速买入接盘，没有 FOMO 预警
- 全天 mid_net 每 15 分钟稳定流出 ~100 万，明显是拆单，但只有 capital_structure 一个维度兜底

### 设计

四个独立函数，全部接入主循环并聚合到 support_score / squeeze_damper：

| 函数 | 触发 | 评分语义 | 满分 |
|------|------|---------|------|
| `analyze_retail_retreat` | small_net 累计从日内峰值回落 ≥ 10%/20% | 支撑做空 | +12 / +20 |
| `analyze_capital_efficiency` | 近 8 轮大单 Δ ≥ 300 万但价格 < 0.3% | squeeze_damper | -8 |
| `analyze_retail_fomo` | 价格窄幅震荡 < 0.5% + 散户 8 轮净流入 ≥ 50 万 | 支撑做空 | +8 |
| `analyze_mid_split` | 大单冻结 + mid_net 5 轮同向 + CV < 0.6 + 中位 Δ ≥ 30 万 | 支撑做空 | +10 |

### 变更内容（`short_squeeze_monitor.py`）

- 新增 11 个常量（RETAIL_RETREAT_* / CAPITAL_EFFICIENCY_* / RETAIL_FOMO_* / MID_SPLIT_*）
- 新增 `db_get_session_small_net_peak()` DB 助手
- 新增 4 个分析函数（章节 "五-C/D/E/F"）
- 主循环：调用 4 个新函数，聚合到 squeeze_damper（资金效率）和 support_score（其余三个）
- 仪表盘自动展示新信号（沿用既有"⚠ ... 支撑做空+X分"渲染规则）

### 决策理由

- **散户撤退归 support 不归 squeeze damper**：它是出货确认的论据，不应直接打掉逼空总分；让 BLOCKED 安全门继续守门，但给 ENTRY 路径累加分数。
- **资金效率单独走 damper**：与"出货式拉升"（Bug 13）的语义一致——价格未响应资金时不应扩大逼空总分，而不是给做空加分。这两者一个是"流入但价不涨"（damper），一个是"流入持续 N 轮但价不涨"（Bug 13 / 顶部回落），互补。
- **拆单 CV 阈值 0.6**：实盘日志中机构拆单的 Δ 序列变异系数通常在 0.3-0.5；散户/噪音通常 > 1.0。0.6 是相对宽松的过滤线，先求"宁可漏不可错"。
- **散户 FOMO 给低分（+8）**：它是过程信号、未到结果（接盘耗尽是 retreat 才确认）。给低分避免与 retreat 双计。

### 与今日日志的对照（预期）

理论上 2026-05-28 13:32-13:47 这段午盘日志重跑后应该看到：
- `RETAIL_RETREAT` 在散户从 +2,557 → +2,300 万（回落 ~10%）后触发
- `CAPITAL_EFFICIENCY_LOW` 在午盘开盘后大单 +165 万但价格几乎不动时触发
- `RETAIL_FOMO` 在 13:46:30 诱多窗口触发（价格 845-847 + 散户拉升）
- `MID_SPLIT_DETECTED` 在 mid_net 稳定流出 -2,745 → -2,827 → -2,922 时触发

### 未改动

- 既有 5 个评分维度（analyze_short_entry 维度 1-5）不变
- BLOCKED 安全门三元 AND 逻辑不变——新维度仅累加 support，不能绕过安全门
- Bug 13 出货式拉升检测保留——它的 streak 视角与本次新增的 Δ 视角互补

## 迭代二十四：派发模式三条件共振（评分盲区补丁，2026-05-29）

### 背景

2026-05-29 开盘第一小时实盘日志暴露了既有评分系统的两类典型盲区：

**第一段 09:30:14—09:34（评分虚高 / 噪音误警）**
- 价格 869.5 → 848.5 单边下行 −2.5%
- 逼空评分多次冲到 100，主要由「卖盘深度骤减 +25」「大单累计静态值反复触发资金反转 +25」拼凑
- 实际是开盘集合竞价后的流动性稀薄期，所有"卖盘骤减""资金反转"维度都被噪音污染
- 唯一真信号（开盘瞬间大单脉冲 +2,871.6 万）后立即冻结，价格随后跌穿 848

**第三段 10:04:13—10:06:00（评分盲区 / 漏报）**
- 价格从 873 假突破高点跌至 846（−3.1%）
- 逼空评分维持 26-59 之间，多数时候在「正常」区间
- 同期实际正在发生今日最深一次派发：
  - 大单累计 −1,754 万（破开盘第一波 −1,425 的日内新低）
  - 散户从日内峰值 +2,114 万撤 859 万（40.7%）
  - 中小单累计从 +1,277 万跌至 +162 万（85% 已消化）
- 现有评分维度全是「逼空触发」语义（反弹、卖盘骤减、资金反转），没有任何专门捕捉"派发末期"的维度
- 散户撤退（迭代二十三）虽已触发但只计入 `support_score`，不进入做空主信号，也无法解开 BLOCKED

### 设计

新增独立的「派发模式」维度，**三条件 AND 共振**才触发：

| 条件 | 阈值 | 含义 |
|------|------|------|
| ① 大单累计接近日内最低 | `latest ≤ session_low × 0.92` 且 `session_low ≤ -500 万` | 机构持续净流出，无回补意愿 |
| ② 散户从峰值回落 | `(peak − now) / peak ≥ 30%` 且 `peak ≥ 500 万` | 接盘者已开始撤退 |
| ③ 中小单合计从峰值回落 | `(peak − now) / peak ≥ 60%` 且 `peak ≥ 500 万` | 非机构资金整体撤退（区别于"主力换手") |

触发后：
- 给做空入场**主信号**加 **+25 分**（与现有维度 1-5 同级）
- 作为 BLOCKED 安全门的**第二条放行路径**（与"价格下行 AND 大单流出"通道并联）

### 变更内容（`short_squeeze_monitor.py`）

- 新增 6 个常量：`DISTRIBUTION_BIGNET_LOW_RATIO` / `DISTRIBUTION_BIGNET_MIN_DEPTH` / `DISTRIBUTION_SMALL_RETREAT_PCT` / `DISTRIBUTION_MIDSMALL_DECAY_PCT` / `DISTRIBUTION_MIDSMALL_MIN_PEAK` / `DISTRIBUTION_SCORE`
- 新增 2 个 DB 助手：`db_get_session_big_net_low()` / `db_get_session_mid_small_peak()`
- 新增 `analyze_distribution_mode()`（章节"五-C2"）：返回 `(score, confirmed, sigs)`
- 修改 `analyze_short_entry()`：
  - 签名追加 `distribution_score / distribution_confirmed / distribution_sigs` 参数
  - 安全门放行从"三元 AND"改为"通道 A / 通道 B 双通道 OR"——通道 B 即派发模式
  - 末尾追加维度 6"派发模式确认"，把 distribution_score 累加到主信号分
- 主循环：在 `analyze_retail_retreat` 调用旁加 `analyze_distribution_mode(conn)`，结果传给 `analyze_short_entry`

### 决策理由

- **三条件 AND 共振 > 单维度阈值**：散户撤退（迭代二十三）触发后只是"接盘力量耗尽"嫌疑，可能伴随大单回补的真反转。三条件共振要求资金、散户、非机构资金同时确认，能可靠区分"派发末期" vs "诱多前散户离场"。
- **派发分进主信号不进 support**：迭代二十三的散户撤退归 support 是因为单维度证据不足以独立支撑 ENTRY；派发模式三条件齐至时证据强度等同于"卖盘骤增"或"连续下行"这类一级信号，应直接进主信号分。
- **作为 BLOCKED 第二通道**：通道 A 要求"价格已确认下行 −X%"，但在 10:04-10:06 这种"评分残留高 + 价格盘整"场景里通道 A 不一定开。派发模式从资金结构角度独立确认逼空假设已死，互补。
- **阈值偏严不偏松**：60% 中小单回落、30% 散户回落是相对保守的阈值（实盘 10:04 这两个指标分别是 87%/40%）。早期场景可能漏报，但能避免在"散户短暂撤退后又回补"的诱空陷阱里误触发。

### 与今日日志的对照（已验证）

用 2026-05-29 09:30-10:06 的关键节点重放（in-memory smoke test）：

```
big_low: -1,753.8 万 / small_peak: +2,114.4 万 / midsmall_peak: +1,914.4 万
analyze_distribution_mode → score=25, confirmed=True
信号文本：派发模式确认：大单累计 -1,753.8 万(日内低 -1,753.8) + 散户撤 33%
        + 中小单撤 80% → 机构出货已确认 [+25分]
```

未触发场景（散户没回落）正确返回 `(0, False, [])`。

理论上 09:30-09:34 第一段不会触发——大单累计 +272 → -80 万，散户 +70 万峰值过小，三条件全部不达阈值。这正符合预期：派发模式只在确实出现派发末期形态时介入，不污染其他场景的信号纯度。

### 未改动

- 既有 5 个做空入场维度（维度 1-5）不变
- 既有的散户撤退（迭代二十三）保留——它是更早期的预警（10% 撤退即触发），派发模式是更晚期的确认（30% + 三条件齐至）。两者形成"预警→确认"的两段式
- BLOCKED 通道 A（"价格下行 + 大单流出"）保留——它在"渐进式下跌"场景里仍是首选放行通道
- Bug 13 出货式拉升检测保留——它看 streak 视角，与本次"日内极值 + 散户/非机构资金对比"互补

---

## 迭代二十五：派发模式粘滞 + 翻转 failsafe 豁免 + 中小单显示分离（2026-05-29）

### 背景

迭代二十四（同日凌晨实施）的派发模式补丁实盘验证后，10:51-10:53 出现三个新问题：

**问题 A：派发模式补丁脱靶**
2026-05-29 10:51:26/41 派发模式触发后，10:51:56 大单累计从 −6,718.9 万回补到 −6,151.8 万（仅 +253 万），即脱离 `DISTRIBUTION_BIGNET_LOW_RATIO=0.92` 阈值失效。但同期：
- 散户撤退 120%（更深）
- 中小单 −4,917 万（更深）
- 卖盘骤增 1400%（派发末期最强信号之一）
- 价格 819.5（更低）

**派发实际更猛烈，但模式补丁误判结束**。

**问题 B：失衡极性翻转 Failsafe 2 误杀**
10:53:13 派发末期 BLOCKED → 0 分。理由"近 6 轮失衡度极性翻转 2 次 → 挂单博弈"。但同期：
- 价格 819（日内底）
- 大单累计 −6,152 万（仍流出）
- 中小单 −5,159 万（**今日最深**）
- 散户 −599 万（**今日最深**）

资金面在恶化，翻转更可能是**薄盘流动性稀薄的视觉抖动**，不是主力博弈。

**问题 C：中小单合并显示掩盖机构拆单**
仪表盘只显示 `中小单(累计) = mid_net + small_net`，散户单独，但中单未单列。11:05 实盘：
- 大单累计 −6,331 万（明面机构）
- 中小单 −5,485 万 / 散单 −963 万 → **隐含 mid_net = −4,522 万**

中单层级的净流出几乎与大单同等量级（4,522 / 6,331 = 71%），属于典型机构拆单伪装。**合计总流出 ≈ 1.18 亿港元**，比明面大单多一倍。但读盘时要心算 `中小单 − 散单` 才能得出中单数据。

### 设计

**A. 派发模式粘滞机制**

| 常量 | 值 | 含义 |
|------|----|------|
| `DISTRIBUTION_STICKY_ROUNDS` | 5 | 触发后保持 N 轮（约 75 秒，跨过单一脉冲回补窗口）|
| `DISTRIBUTION_STICKY_SCORE` | 12 | 粘滞期间的派发分（原 25 减半）|

`MonitorState` 加 `_distribution_sticky_left: int = 0`，主循环逻辑：
- 本轮三条件命中 → 重置 `sticky_left = N`
- 本轮未命中但 `sticky_left > 0` → 保持 `confirmed=True`、分数减半、`sticky_left -= 1`
- 全部脱离 → 回 0

**B. 翻转 failsafe 豁免**

| 常量 | 值 | 含义 |
|------|----|------|
| `THIN_ASK_DEPTH_FOR_FLIP_SKIP` | 300 | 卖盘深度 < N 股时跳过翻转 failsafe |

`apply_short_entry_failsafes` 签名追加 `distribution_active: bool` 和 `ask_depth: Optional[float]`，Failsafe 2 触发前先检查：派发模式生效 OR 薄盘期 → 直接跳过，记录豁免提示而不 BLOCKED。

**C. 仪表盘中小单分离**

把原本一行 `中小单净额 (累计) ... 散 X 万` 改为两行：
- `中单(拆单嫌疑) (累计) : mid_net`
- `散单(散户) (累计) : small_net`

突出"中单 ≠ 散户"——中单层级独立可见后，机构拆单痕迹（mid 大幅净出但 small 还在小幅震荡）一眼可辨。

### 变更内容（`short_squeeze_monitor.py`）

- 新增 3 个常量：`DISTRIBUTION_STICKY_ROUNDS` / `DISTRIBUTION_STICKY_SCORE` / `THIN_ASK_DEPTH_FOR_FLIP_SKIP`
- `MonitorState` 加 `_distribution_sticky_left: int = 0`
- 主循环 `analyze_distribution_mode` 调用后追加粘滞逻辑（触发重置 / 衰减 / 减半计分）
- `apply_short_entry_failsafes` 签名扩展 + Failsafe 2 豁免路径
- 两处调用点（`analyze_short_entry` 末尾 + 主循环 ENTRY 升级后）传 `distribution_active` 和 `ask_depth`
- 仪表盘渲染：`中小单` 单行合并 → 拆为 `中单(拆单嫌疑)` + `散单(散户)` 两行

### 决策理由

- **粘滞分数减半（25→12）**：避免过度依赖陈旧触发；只要资金面继续恶化（散户继续撤、中小单创新低），实际新触发会立刻重置回 25，无信号衰减问题。
- **粘滞 N=5 轮（约 75 秒）**：刚好跨过 capital_flow 单一脉冲回补窗口（实盘 10:51:56 → 10:52:12 仅 16 秒），同时不长到掩盖真反转。
- **豁免阈值卖盘深度 300 股**：实盘观察派发末期薄盘频繁出现 <200 股，普通时段一般 500+，300 是分界线。
- **派发触发期间 + 薄盘期都豁免**：双触发条件取 OR，覆盖两类典型场景。
- **中单标签"拆单嫌疑"**：不绝对断言（中单也可能是中户/小机构）；但加这个标签后用户的注意力会被引导到"中单方向 = 机构隐性意图"的解读。

### 与今日日志的对照（已验证）

A. 粘滞机制 smoke test：
```
触发帧（10:48 派发末期）: score=25, confirmed=True
回补脱阈帧（10:51:56 大单回补）: score=0, confirmed=False（旧行为）
粘滞启用后: score=12, confirmed=True, sticky_left 递减
```

B. 翻转 failsafe 豁免：函数签名 + 内部逻辑均通过 IDE 诊断 + smoke test。

C. 中小单分离：终端宽度对齐通过（中单行/散单行 vs 大单行同列）。

### 未改动

- 派发模式三条件主逻辑、阈值常量、五-C2 函数体未动
- BLOCKED 安全门通道 A（价格下行 + 大单流出）保留
- Failsafe 1（摆盘明显偏多降级）保留
- 既有的散户撤退 / FOMO / 中单拆单方差等维度全部保留
- 仪表盘整体结构、宽度、颜色未变

---

## 迭代二十六：通道 C + 结构背离 Δ 化 + 破位 capitulation 升级（2026-05-29）

### 背景

2026-05-29 尾盘 15:15-15:28 出现 5/29 全天**首次真逼空 + 急速崩塌**——价格从 917 跌到 840.5 (−8.4%) 仅 13 分钟。但评分系统给出的唯一一个时机正确的 ENTRY 信号（15:22:45 做空 90/100）**仅持续 30 秒就被误杀**，三个并发 bug 同时表现：

**Bug #9：安全门通道 A 在反转场景失灵**
15:23:46 时 effective_squeeze=26 触发 BLOCKED 检查：
- 通道 A 要求 `价格已下行 AND 大单持续净流出`
- 价格条件满足（−4.08%）但大单累计仍 +533 万（午盘逼空进场尚未反转完）
- AND 失败 → 通道 A 不开
- 通道 B 派发模式不触发（big_low=−6,718 万，big_now=+533 万差太远）
- **两条通道全断 → BLOCKED，主信号清零**

**Bug #6：资金结构背离用累计值导致单边反转场景误报**
`analyze_capital_structure` 用 `(mid + small) < THRESHOLD` 累计值判断净流出。上午累计的 −5,500 万一直留在 capital_flow 里，下半场实际是 mid 大幅净买入 +4,653 万（Δ 视角）。旧逻辑 15:14-15:28 持续误报"中小单连续 3 轮净流出"，给评分污染。

**Bug #5：出货式拉升判定文案不区分轻度回落 vs 真破位**
当价格从峰值回落 ≥ 1.5% 时，固定输出"疑似顶部出货，不计逼空分"。但 15:22-15:28 期间回落已达 5%-8.4%，文案仍是"疑似"，且**只是不加分，不主动减分**——squeeze 无法降下来配合 ENTRY 触发。

### 设计

**A. 通道 C（Bug #9 修复）**

| 常量 | 值 | 含义 |
|------|----|------|
| `SHORT_BLOCK_SESSION_PULLBACK_PCT` | 3.0 | 从日内最高点回落 ≥ 此 % 即放行 BLOCKED |

`apply_short_entry_failsafes` 的 BLOCKED 检查从"A OR B"扩为"A OR B OR C"：
- 通道 C 只看 `(session_high − current) / session_high ≥ 3.0%`
- 与资金流方向解耦——价格 capitulation 本身就是逼空假设被否定的证据
- 15:23:46 验证：(917 − 870) / 917 = 5.13% ≥ 3.0% ✓

**B. 资金结构背离 Δ 化（Bug #6 修复）**

`analyze_capital_structure` 改用相邻两条 capital_flow 记录的 Δ，不再用累计值：
- `big_delta[i] = rows[i].big_net − rows[i+1].big_net`（i 时段的大单净流量）
- `retail_delta[i] = (rows[i].mid+small) − (rows[i+1].mid+small)`
- 场景 1 触发条件改为"`big_delta` 持续正 AND `retail_delta` 持续低于阈值"
- 上半场累计的负值不再永久污染下半场判断

**C. 破位 capitulation 升级（Bug #5 修复）**

`analyze_capital_flow` 出货式拉升路径分三档：
- pullback < 1.5%：正常加 squeeze 分
- 1.5% ≤ pullback < 3.0%：不加分（旧"疑似顶部出货"，与当前 `BIGFLOW_PUMP_PEAK_PULLBACK_PCT` 一致）
- **pullback ≥ 3.0%（capitulated）**：升级文案"顶部出货已确认" + 主动给 squeeze 减 −15 分

减分会让 squeeze_score 下降，进而让 effective_squeeze 自然滑过 SHORT_SAFE_SQUEEZE=25 阈值，**ENTRY 自然解锁，不依赖通道 C 兜底**。

### 变更内容（`short_squeeze_monitor.py`）

- 新增 1 常量：`SHORT_BLOCK_SESSION_PULLBACK_PCT = 3.0`
- 修改 `apply_short_entry_failsafes`：BLOCKED 检查增加通道 C 分支（session_high 回落判定）
- 重构 `analyze_capital_structure`：累计值 → Δ 算法，三个场景统一改造
- 修改 `analyze_capital_flow` 出货式拉升分支：增加 `capitulated` 三档分级 + 负分扣减

### 决策理由

- **3.0% 阈值**：实盘 5/29 一天里出现两次显著从峰值回落——10:30 区间约 4% 和 15:22 区间 5-8%。1.5% 容易误触发（盘中正常回调），5.0% 又太严错过 15:22。3.0% 是分界。
- **通道 C 只看价格**：故意与资金流解耦——价格 capitulation 是市场已经做完决定的证据，资金流可能滞后（如本例大单累计还在正值但价格已破位）。
- **Δ 算法用 N+1 条记录**：原来 N 条算 N 个不严格独立的状态，新方法 N+1 条算 N 个真实增量，更稳健。
- **capitulated 扣 −15 分**：选择减分而非"全清零"——保留其他维度（如 streak 加速、卖盘骤增）的逻辑空间，只是把"持续正值"这个本来可能 +12 分的计分项反转为 −15。

### 与今日日志的对照（已验证）

Smoke test：
```
通道 C: session_high=917, current=870 → pullback=5.13% ≥ 3.0% ✓ 放行
capitulated: peak=917, current=840.5 → pullback=8.34% ≥ 3.0% ✓ 触发
```

旧代码下 15:23:46 BLOCKED → 新代码下应直接 ENTRY 放行。

### 未改动

- 派发模式、粘滞机制、翻转 failsafe 豁免（迭代二十四+二十五）保留
- 中单/散单分行显示保留
- 通道 A/B 保留——它们覆盖的场景（连续阴跌、派发末期）依然有效
- 维度 1-6 评分、Failsafe 1（摆盘偏多降级）、Bug 13/20/21 出货式拉升核心判断全部保留

---

## 迭代二十七：追空守门 Failsafe 3（2026-06-01）

### 背景

2026-06-01 实盘 00100：开盘 906 见顶，午盘派发到 710（自日高 -21.6%、破成本线
774.6），大单累计冻结于 -1.2 亿达 20+ 轮（砸盘已停）。但做空入场分被滞后/背景分
顶到 98（主55 = 距高跌幅 +15 + 派发 +25 + 大单持续负 +15；背景43 = 低于 10/20
日成本线等结构分）。**这种分描述的是"已经跌透"，不是"还会跌"**——若无买盘护盘
（本次靠 imb>0.3 偶然挡住），系统会在最低点诱导追空。

### 问题本质

做空分的主导项几乎全是滞后/结构性的：
- 维度4「距日高跌幅」给 +15，跌得越多分越高，恰在下跌空间最小时虚高；
- 背景「低于成本线 +23」只要价格在 774 下方就常态挂着，与"此刻该不该开空"无关；
- 大单「累计持续为负」是冻结值，记的是已发生的出货。

唯一反映"此刻"的实时项（摆盘失衡度）当时是 +0.8 偏多，方向与做空相反，系统也因此
把 ENTRY 降级 CAUTION——说明 98 是虚高骨架，真实意见是"别空"。

### 改动（v2，纯回落闸）

`apply_short_entry_failsafes` 新增 **Failsafe 3（追空守门）**：

```
sig_type == ENTRY  AND  pullback ≥ SHORT_CHASE_PULLBACK_PCT(15%)
    → ENTRY 降级 CAUTION，记 SHORT_CHASE_GUARD
```

- 新增常量 `SHORT_CHASE_PULLBACK_PCT = 15.0`
- 函数签名加 `current_price`；两处调用点（`analyze_short_entry` 末尾 + 主循环
  support 合并后）都传入，覆盖"主信号直接达标"和"合并背景才达标"两条路径。

### v1 的坑（先做错后修对）

v1 用 `回落≥12% AND big_net_stale` 双条件。当天实盘 13:47:37 / 13:48:07 仍漏网
ENTRY 在 711-712（-21%）：
- 大单冻结于 -13,777 万但**尚在 5 轮 stale 检测窗口内**——维度 1 还在计 +15
  "持续为负"，`big_net_stale=False`，AND 第二个条件不成立；
- 同期 imb 短暂转 -0.31 / +0.11，Failsafe 1（imb>0.3）也没拦。

两道闸同时失效，ENTRY 在最低点放行。**教训：追空守门必须是纯回落闸，不能叠加
big_net_stale——该 flag 有 5 轮检测延迟，会在"已冻结但未达 stale"的间隙漏网。**

### 决策理由

- **纯回落、不叠加条件**：深度回落本身就是"下跌空间已耗尽"的充分证据，任何附加
  AND 条件都会引入漏网间隙（v1 教训）。
- **15% 阈值**：当天"仍有效续跌空单"集中在 -8%~-12%（824 破位、798 二段），
  "枯竭底部"在 -21%，中间 -12%~-21% 是空档。15% 卡空档，既不误杀续跌、又能灭
  底部追空。

### 与今日日志的对照（已验证）

```
711.5 漏网ENTRY (回落21.5%, imb-0.31, 未stale) → 降级 CAUTION ✓（v1 漏网，v2 修复）
712.0 漏网ENTRY (回落21.4%, imb+0.11, 未stale) → 降级 CAUTION ✓（v1 漏网，v2 修复）
706   底部      (回落22.1%)                    → 降级 CAUTION ✓
833   824-破位  (回落8.1%)                      → 保留（好空单）✓
798   二段续跌  (回落11.9%)                     → 保留（好空单）✓
770   成本线    (回落14.9%, 刚好<15%)           → 保留 ✓
```

### 未改动

- F1（摆盘偏多降级）、F2（翻转 failsafe + 派发/薄盘豁免）保留，追空守门列其后
- 安全门通道 A/B/C、派发模式、迭代二十二~二十六各维度全部保留
- 仅对最终 sig_type 做 ENTRY→CAUTION 降级，不改分值、不改 BLOCKED 逻辑

---

## 迭代二十八：隐藏主力吸筹否决 Failsafe 4（中单/拆单买入 + 价格上行，2026-06-03）

### 背景

2026-06-03 实盘 00100（MINIMAX-W）10:29-10:31：大单累计冻结于 -209.5 万达 19+ 轮
被维度 1 跳过；失衡度呈 reverse-诱空（盘口偏空 -0.5 但价格不跌反涨）把做空主信号
顶到 47，叠加日级背景 53（低于 10/20 日成本线 +23、动能 0.43× +10、距高跌幅 +15、
散户撤退 +5）→ 假 ENTRY=76（10:30:02）/ 100（10:30:17）。**而同期价格正从 30 轮
低点 682 反弹到 690**，谁在那挂空单立即浮亏。

### 问题本质

大单冻结时系统看不清主力公开方向，但**中单(拆单)是隐藏主力的探针**——这段中单累计
+194→+397 万持续加速买入、中小单转正、价格创反弹新高，按"资金方向 + 价格印证"
框架是隐藏主力吸筹（看多），本该否决做空。但既有 failsafe 全部漏过：
- F1（imb>+0.3 降级）：当时 imb=-0.35 偏空，方向不对，不触发；
- F2（失衡度高频翻转）：翻转次数未达阈值；
- F3（追空守门）：自日内高 704 回落仅 2.4% < 15% 阈值，不触发；
- 它们都只看**失衡度/距高跌幅**，对"盘口偏空但价格涨、中单偷偷买"的组合无感
  （与未修复的 Bug 12 反向诱空同源）。

### 改动

`apply_short_entry_failsafes` 新增 **Failsafe 4（隐藏主力吸筹否决）**：

```
sig_type==ENTRY  AND  big_net_stale  AND  current_price 上行
  AND  mid_net 累计>0
  AND  (近 N 轮 mid_net 净买入 Δ ≥ MIN_DELTA  OR  mid_net 累计 ≥ MIN_LEVEL)
  AND  同期价格涨幅 ≥ PRICE_RISE_PCT
    → ENTRY 降级 CAUTION，记 SHORT_MID_ACCUM_VETO
```

- 新增常量：`MID_ACCUM_WINDOW=10`、`MID_ACCUM_MIN_DELTA=100万`、
  `MID_ACCUM_MIN_LEVEL=200万`、`MID_ACCUM_PRICE_WINDOW=12`、
  `MID_ACCUM_PRICE_RISE_PCT=0.2%`。
- **gate 在 `big_net_stale`**：仅当大单看不清、中单成为隐藏主力探针时生效，避免误杀
  大单真实净流出支撑的合法做空。函数签名加 `big_net_stale`，两处调用点都传入。
- **Δ 与 level 二选一**：mid_net 实盘常先回落再拉升（10:25:45 +136 → 10:31 +397），
  短窗 Δ 会被中途回落稀释（10:30:02 时 6 轮 Δ 仅 +35.7 万），故并入绝对累计水位兜底。

### 回放验证（短 short_data.db 真库，已验证）

```
10:30:02 假ENTRY=76  mid累计+261万(level命中) 价+0.29% → 降级 CAUTION ✓
10:30:17 假ENTRY=100 mid近10轮+120.7万(Δ命中) 价+0.51% → 降级 CAUTION ✓
10:30:32 假ENTRY=100 mid近10轮+120.7万(Δ命中) 价+0.58% → 降级 CAUTION ✓
```

### 决策理由

- **方向矛盾即否决**：做空 ENTRY 与"中单买入 + 价格上行"双双相反，是隐藏主力吸筹
  而非派发，应降级。与 Bug 16「大单方向矩阵」互补——那个管大单，这个管中单/拆单档。
- **降级 CAUTION 不 BLOCKED**：与 F1/F3 保护哲学一致，保留观察、不强杀。
- **HKD 阈值与本档量级相关**：换标的（尤其低价/小成交股）需重标定 MIN_DELTA/MIN_LEVEL。

### 未改动

- F1/F2/F3、安全门通道 A/B/C、派发模式、迭代二十二~二十七各维度全部保留。
- 仅对最终 sig_type 做 ENTRY→CAUTION 降级，不改分值、不改 BLOCKED 逻辑。
- 未修复的 Bug 12（反向诱空：巨型卖墙+失衡极负但价格不跌）仍待办——本次只补了
  "中单买入+价格涨"这一条路径，盘口侧的反向诱空仍需独立建模。

---

## 迭代二十九：卖而不跌裁决（净流出 + 价格守位/反弹 → 被动吸筹，2026-06-04）

### 背景

2026-06-04 实盘复盘 00981（中芯国际）09:30-09:42：大单累计 -3,111 万、**中单 -6,077 万**、
散单 -408 万——三档全是主动卖；但价格 80.35→81.5 走 V 形反弹未跟跌。当时分析一度把
"中单负值"误读成"主力在中单买入"。复盘纠错：`get_capital_distribution` 的 in/out 按
**aggressor（主动方）分类**——成交打卖一价=主动买=流入、打买一价=主动卖=流出。**主力
挂被动买单接货时，是对手主动卖单去砸他的买盘成交 → 每笔计入"流出"**，故被动吸筹在
资金流里天然显示为净流出。**「负的中单 ≠ 主力在卖的铁证」**，意图只能靠"资金方向 ×
价格"背离裁决。

### 问题本质

"资金方向 × 价格"四象限里，**「净流出 + 价格不跌反涨（卖而不跌）」这一格既有维度全部漏过**：
- `analyze_distribution_mode`（派发模式/卖而跌）：要求**价格跌**，此处价格涨，不触发；
- `Failsafe 4`（隐藏吸筹否决）：要求 **mid_net 为正**，此处中单为负，不触发；
- `analyze_capital_efficiency`（资金效率）：要求**大单流入**，此处大单流出，不触发。

00981 当天唯一拦住做空的是"卖空占比高位拐头"的逼空分（供给侧信号），换一只没有该信号
的票就会漏——会顺着"三档净流出"误判成派发去追空，而实际是有大资金被动吸收的吸筹/诱空。

### 改动（`short_squeeze_monitor.py`）

新增 **`analyze_sell_no_drop()`（五-D2）**，对称于派发模式的"卖而跌"，捕捉"卖而不跌"象限：

```
窗口内 (big_net+mid_net) Δ ≤ MIN_OUTFLOW（持续净流出）
  AND  最新 (big_net+mid_net) < 0（确为净流出格局，排除高位回吐）
  AND  同期价格涨幅 ≥ PRICE_FLOOR（守位/反弹，未跟跌）
  AND  最新价 > 窗口最低价（未破位）   ← ④二次否决：破位=卖压已兑现=真派发，放行做空
    → 逼空风险 +SCORE 分，记 SELL_NO_DROP
```

- 新增常量：`SELL_NO_DROP_WINDOW=8`、`SELL_NO_DROP_MIN_OUTFLOW=-300万`、
  `SELL_NO_DROP_PRICE_FLOOR=-0.10%`、`SELL_NO_DROP_SCORE=12`。
- **喂逼空评分**（`s_snd` 并入 `squeeze_score`、`sg_snd` 并入 `squeeze_signals`），经
  `recent_max_squeeze` → 做空安全门，BLOCK 顺势追空；语义与 `analyze_price_reversal` 一致。
- **价格破位二次否决**：与派发模式互为镜像——派发要价格跌才算出货，本裁决一旦价格破
  窗口低点就撤销（卖压已真兑现），避免在真派发里反向护盘。两者价格方向互斥，不会同轮双触发。

### 决策理由

- **数据源根因不可逆**：aggressor 分类下被动吸筹与主动派发都显示为流出，单看资金流符号
  分不开二者；唯一判别量是"资金方向 × 价格"背离 + 价格是否破位，故裁决器以价格为最终裁判。
- **抬升逼空分而非直接降级 ENTRY**：复用既有安全门链路，与 s_rev 同语义，避免新增否决路径。
- **HKD 阈值与本档量级相关**：换标的（尤其低价/小成交股）需重标定 MIN_OUTFLOW。

### 未改动

- 派发模式、Failsafe 1-4、安全门通道 A/B/C、迭代二十二~二十八各维度全部保留。
- 盘口侧反向诱空（巨型卖墙 spoofing + 失衡极负但价格不跌，Bug 12）仍待办——本裁决只用
  价格轨迹，不依赖盘口深度（00981 当天卖深 500↔135,500 是教科书幌单，深度不可信）。

---

## 迭代三十：L2 逐笔冰山检测 Tier 1（执行级被动吸筹/派发，2026-06-04）

### 背景

迭代二十九的 `analyze_sell_no_drop` 用「资金流 × 价格」背离判被动吸筹，但资金流
（`get_capital_distribution`）每分钟才刷新一次、且仍是 aggressor 累计——分不开"被动
吸筹"与"真出货"的根因（两者都显示为流出）只能靠价格裁决。要真正穿透，必须下沉到
**逐笔成交**：报价层（挂单深度/集中度）是 spoofing 重灾区（00981 06-04 卖深
500↔135,500 来回甩），唯有"真金白银吃出来的成交"难造假。

评估外部 AI 提的 L2 方案后定调：**它把优先级押反了**——主打"前三档挂单集中度"恰是最
可幌单的一层，`bid_conc>0.65 → +吸筹分` 会被假接货墙直接骗出诱多。正确做法是信成交
（execution）、不信报价（quote）。用户确认 Futu 订阅含 TICKER + BROKER，先落地 Tier 1。

### 改动（`short_squeeze_monitor.py`）

新增 **`analyze_iceberg_absorption()`（五-D3）** + 逐笔数据管道：

```
买侧冰山吸筹：主动卖主导(SELL≥BUY×DOMINANCE) + 价格未跟跌(≥PRICE_FLOOR)
  + 买一被吃量 ≥ 显示量×REFILL_MULT(补单铁证) → squeeze +15(强)/+8(弱)，抑制追空
卖侧冰山派发：主动买主导(BUY≥SELL×DOMINANCE) + 价格滞涨(≤PRICE_CAP)
  + 卖一被吃量 ≥ 显示量×REFILL_MULT → support做空 +15/+8
```

- **数据层**：订阅加 `SubType.TICKER`；新 `fetch_ticks()` 拉 `get_rt_ticker`、按
  `sequence` 去重只统计新成交、聚合主动买/卖量（Futu `ticker_direction` BUY=主动买/
  SELL=主动卖），结合 `fetch_order_book` 新带出的最优档显示量，写入新表 `tick_flow`。
  逐笔是增强信号，失败仅 None、不阻断核心打分（不进 API 失效守门）。
- **新常量**：`ICEBERG_WINDOW=4`、`MIN_VOL=50_000股`、`DOMINANCE=1.5`、
  `PRICE_FLOOR=-0.10%`、`PRICE_CAP=0.10%`、`REFILL_MULT=2.0`、`STRONG=15`、`WEAK=8`、
  `TICK_FETCH_NUM=1000`。
- **接线**：返回 `(squeeze_pts, support_pts, sigs)` 双路由——吸筹喂 `squeeze_score`
  （经安全门 BLOCK 追空，与 s_snd 同侧），派发喂 `support_score`（支撑做空）；二者按价格
  方向互斥，单轮最多一个非零。
- **DB 幂等**：`tick_flow` 经 `CREATE TABLE IF NOT EXISTS` 加表，存量库（短库 40093 行
  capital_flow）已验证无损。

### 验证

- `py_compile` 通过；`init_db` 对存量真库幂等加表、原数据无损。
- **合成单测 6 例全过**（真逐笔数据需下个交易时段实盘采集，本迭代上线前无历史 tick 可回放）：
  吸筹→(15,0)、派发→(0,15)、中性→(0,0)、**主动卖主导但价跟跌→(0,0)正确判真砸盘不误报**、
  量不足→(0,0)、窗口不足→(0,0)。

### 为什么不采纳"挂单集中度"主打方案

显示挂单是最易操纵层；集中度加分会重新引入刚在 `analyze_sell_no_drop` 躲掉的 spoofing
漏洞。集中度/委托笔数仅宜作**交叉验证或识别幌单**，绝不单独加分。Tier 2（经纪队列：
单一席位反复补买一=机构足迹）与 Tier 3（委托笔数）待后续按需补。

### 未改动

- 迭代二十二~二十九各维度、Failsafe 1-4、安全门通道 A/B/C 全部保留。
- 真逐笔阈值（`ICEBERG_MIN_VOL` 等股数）与本档成交活跃度强相关，换标的必须重标定；
  极活跃标的两次轮询间成交 > 1000 笔会少计量级（不影响方向判定）。

---

## 迭代三十一：L2 经纪队列足迹 Tier 2（机构席位足迹交叉验证，2026-06-04）

### 背景

迭代三十 Tier 1 用逐笔成交（execution）穿透 aggressor 二义性，落地后留 Tier 2/3「待按需补」。
Tier 2 = 经纪队列：`get_broker_queue` 暴露**哪个经纪席位**在买一/卖一档排队。单一席位反复
占据并补回最优档 = 机构被动挂单的足迹（散户做不到这种持续性）。

**关键定位（务必记住）**：经纪队列是**报价层（quote）**，与挂单深度同属 spoofing 重灾区。
故 Tier 2 **绝不单独加分**——只在 Tier-1 冰山（执行级成交证据）同向已触发时作交叉验证加成。
否则就重新引入了刚在 `analyze_sell_no_drop`/Tier-1 躲掉的"假接货墙骗诱多"漏洞。

### 改动（`short_squeeze_monitor.py`）

新增 **`analyze_broker_footprint()`（五-D4）** + 经纪队列数据管道：

```
ice_squeeze_pts<=0 AND ice_support_pts<=0 → 直接 (0,0,[])   ← 反幌单铁律：无冰山不计分
买侧：Tier-1 买侧冰山吸筹已触发 + 单一 bid1 席位 ≥MIN_ROUNDS/WINDOW 占据 → squeeze +BONUS
卖侧：Tier-1 卖侧冰山派发已触发 + 单一 ask1 席位 ≥MIN_ROUNDS/WINDOW 占据 → support +BONUS
```

- **数据层**：订阅加 `SubType.BROKER`；新 `fetch_broker_queue()` 拉 `get_broker_queue`
  （返回 `(ret, bid_frame, ask_frame)`），取每侧队首行 `broker_id` 写入新表 `broker_queue`。
  报价层增强信号，失败仅 None、不阻断核心打分（不进 API 失效守门）。
- **新常量**：`BROKER_FOOTPRINT_WINDOW=6`、`MIN_ROUNDS=4`、`BONUS=6`。
- **接线**：`analyze_broker_footprint(conn, s_ice_sq, s_ice_sup)` 紧跟冰山调用；`s_brk_sq`
  并入 `squeeze_score`（与冰山吸筹同侧）、`s_brk_sup` 并入 `support_score`。非冰山轮恒零。
- **席位过滤**：None/空/`"0"`（隐藏/未披露）席位剔除，避免把"无席位"当持续足迹。
- **DB 幂等**：`broker_queue` 经 `CREATE TABLE IF NOT EXISTS` 加表，00981 真库（4060 行
  capital_flow）已验证幂等加表、原数据无损。

### 验证

- `py_compile` 通过；真库幂等加表无损。
- **合成单测 6 例全过**（真经纪数据需下个交易时段采集，本迭代无历史 broker 可回放）：
  席位占据+冰山吸筹→(6,0)、**席位占据但无冰山→(0,0) 反幌单铁律生效**、席位不持续→(0,0)、
  卖侧席位+冰山派发→(0,6)、窗口不足→(0,0)、隐藏席位被过滤→(0,0)。

### ⚠ LV1 实盘核对 + 订阅原子失败修复（2026-06-04 11:38 盘中）

实盘连 OpenD 测试（00981，账户 **LV1 行情权限**）暴露：

1. **Tier-1 TICKER 在 LV1 完全可用**：`get_rt_ticker` 返回真实 `ticker_direction`(BUY/SELL/
   NEUTRAL)+`sequence`，逐笔冰山数据源没问题。`get_order_book` 在 LV1 仅返回买卖各 1 档，
   但冰山只需 best_bid/ask_vol（第 0 档），不受影响。
2. **Tier-2 BROKER 在 LV1 不支持**：`get_broker_queue` 报「LV1 权限下不支持获取经纪队列」，
   需 LV2 权限。Tier-2 在本账户上常驻降级，待用户升 LV2 才生效。
3. **真 bug（本次引入并修复）**：原把 `SubType.BROKER` 并进核心 subscribe 一批 →
   **LV1 下整批原子失败**（ret=-1），连 QUOTE/ORDER_BOOK/TICKER 都订不上，直接拖垮
   Tier-1。**修复**：BROKER 拆成独立 `subscribe` 调用，`broker_available` 标记其成败；
   主循环 `if broker_available: fetch_broker_queue(...)` 门控，失败则降级为纯 Tier-1。
   实测：核心订阅 OK、逐笔流动正常、`broker_queue` 恒 0 行、`analyze_broker_footprint`
   返回 (0,0) 不崩——优雅降级成立。详见 [[known-bugs-and-gotchas]]。
4. **当日升 L2 后 Tier-2 实盘跑通**（13:40）：BROKER 订阅 ret=0、`get_broker_queue` 返回
   `(ret, bid_frame, ask_frame)`，**列名核对为 `bid_broker_id`/`ask_broker_id`**（与
   `fetch_broker_queue` 一致，无需改码），`iloc[0]`=队首席位；`broker_queue` 逐轮入库、
   `analyze_broker_footprint` 在真席位序列上正确计算（窗口内无单一席位达 4/6 → 即便强制
   冰山旗标仍 (0,0)，无误报）；订单簿升 10 档。`MIN_ROUNDS`/`BONUS` 待真实机构吸筹案例标定。

### 为什么 Tier 2 只能加成、不能独立

显示挂单/经纪席位都可幌单；若让席位足迹独立计分，假接货墙挂个常驻席位即可骗出吸筹诱多。
Tier-1 冰山的"成交被吸收 + 补单铁证"是真金白银，Tier 2 在其之上确认"是同一机构在做"——
方向由 Tier-1 定，Tier 2 只提升置信度（+6）。Tier 3（委托笔数）同理，待按需补。

### 未改动

- 迭代二十二~三十各维度、Failsafe 1-4、安全门通道 A/B/C 全部保留。
- 真席位阈值与标的活跃度/做市结构相关，换标的需观察重标定；`get_broker_queue` 仅 HK 等
  披露经纪队列的市场可用，未订阅/不可用时静默降级为纯 Tier-1。

---

## 迭代三十二：Tier-2 经纪足迹判断依据重写（按名 + 最优档 + 净不对称，2026-06-04）

### 背景

迭代三十一上线当天升 L2 实盘观察 00981，Tier-2 一次没触发。复盘 `broker_queue` 发现 v1
判断依据对大票根本失效：

1. **按 `broker_id` 数会把同一机构拆散**：实盘 `get_broker_queue` 全帧显示**荷银占 17 个买档
   ID / 22 个卖档 ID**（同机构多席位 ID）。v1 只取队首 `iloc[0]` 的 id 且按 id 计数，荷银的
   集中度被打散成几十个"不同席位"，频次全是 1，永远不达阈值。
2. **做市商两侧都重仓**：荷银/巴克莱在买卖两侧同时挂大量席位（做市报价，非方向性）。只看
   一侧"谁席位多"会把做市商误判成"机构吸筹/派发足迹"。

### 改动（`short_squeeze_monitor.py`）

`fetch_broker_queue` + `analyze_broker_footprint` + `broker_queue` 表全部重写为 v2：

```
判断依据 v2 = 按 broker_name 聚合 + 只看最优档(broker_pos==1) + 买卖净不对称
  净不对称(本侧) = 该机构本侧最优档席位数 − 其对侧最优档席位数
    >0 = 单边方向性挂盘；≈0 = 两侧均衡的做市商（被过滤）
足迹 = 同一机构在窗口内 ≥MIN_ROUNDS 轮是本侧净不对称最强者、且净均 ≥MIN_NET
       + Tier-1 同向冰山已触发 → +BONUS
```

- **表 v2**：`broker_queue` 列从 `(bid1_id, ask1_id)` 改为 `(bid_top_name, bid_top_net,
  ask_top_name, ask_top_net)`。旧表仅当日观测、从未触发，无历史价值——`init_db` 检测到
  旧 schema（含 `bid1_id`）直接 DROP 重建（真库迁移已验证，其它表无损）。
- **常量**：`MIN_ROUNDS` 4→**3**（放宽观察）；新增 `MIN_NET=2`（净不对称下限，滤做市商/
  噪音轮）；`WINDOW=6`、`BONUS=6` 不变。
- 反幌单铁律、iceberg 门控、双路由（吸筹喂 squeeze / 派发喂 support）均保留。

### 验证

- `py_compile` 通过；真库 v1→v2 迁移无损（197 行旧表重建，capital_flow/tick_flow/signals 不变）。
- **合成单测 7 例全过**：荷银单边压买+冰山→(6,0)、**做市商两侧均衡→(0,0) 过滤生效**、
  仅 3/6 轮（放宽阈值）→触发、净<MIN_NET→(0,0)、无冰山门控→(0,0)、卖侧派发→(0,6)、窗口不足→(0,0)。
- **实盘 00981 15:18 验证**：捕捉到荷银从净压买盘(+9)翻转为**净压卖盘(+11→+13)**（早盘 14:04
  还是两侧均衡做市商 买17/卖22）——v1 完全看不到的方向性切换，v2 精准捕捉。强制冰山门控下
  卖侧荷银 4/6 轮净均 +12 席 → 派发足迹 +6。

### 未改动 / 待标定

- 迭代二十二~三十一各维度全部保留；Tier-2 仍只在同向冰山触发时加成，不单独计分。
- **`MIN_NET=2` 偏宽**（荷银净不对称常达 ±9~13）——属"放宽观察"刻意为之，先让它多触发、
  攒真实案例，再决定是否抬高 MIN_NET 或按净不对称大小分档 strong/weak。
- **同名风险**：富途/老虎等零售聚合商按 name 也会集中，但其为流量通道、不会"被动补单被吃"，
  iceberg 执行级门控（价不动 + 最优档被吃补单）会过滤掉非被动吸收的情形。

---

## 迭代三十三：仪表盘 L2 微观结构面板（2026-06-04）

### 背景

Tier-1/Tier-2 信号此前只在触发时混在【做空入场】信号列里、且常被 `[:52]` 截断
（"…（卖一补单"断尾）。L2 数据（逐笔主动买卖、最优档挂量、冰山状态、经纪净不对称）
没有一个常驻视图，无法每轮观察微观结构演化。

### 改动（`short_squeeze_monitor.py`）

新增仪表盘 **`[④]` L2 面板**，每轮常驻显示（不止触发时）：

```
[④] L2逐笔(主动): 买 X / 卖 Y 股 (主买/主卖 N×)     ← 逐笔 aggressor 力量对比
      最优档挂量 : 买一 A / 卖一 B 股                ← 补单判断的基准
      L2冰山     : 卖侧派发[+15]被动出货→支撑做空     ← Tier-1 执行级状态
      经纪净不对称: 买▲中国投资+3  卖▼荷银+12 席       ← Tier-2 足迹，▲▼标触发侧
```

- `MonitorState` 新增 6 字段：`latest_tick`/`latest_broker`（fetch 返回快照）、
  `latest_ice_sq`/`latest_ice_sup`/`latest_brk_sq`/`latest_brk_sup`（本轮分值）。
- 主循环捕获 `fetch_ticks`/`fetch_broker_queue` 返回写入 state；冰山/足迹分值在
  analyze 后写入。`print_dashboard` 渲染前算好 4 行 L2 字符串，插在 `[③]` 与逼空风险之间。
- **优雅降级**：逐笔无数据→"—（逐笔无数据）"；无 BROKER 权限→"需 L2/BROKER 权限"；
  冰山未触发→"无"。冷启动/LV1 均已验证不崩。

### 验证

- `py_compile` 通过；mock 满 L2 状态、冷启动、LV1(无 broker) 三场景渲染正常。

### 信号行截断修复（同迭代追加）

原信号渲染 `{s[:52]:<52}  ║` 把自由文本砍到 52 字符——L2 冰山等长消息的关键尾部（补单
倍数+结论"…（买一补单 88.5×）→ 被动吸筹"）被截断；且 CJK 双宽下右框本就对不齐。改为
`{s}`（逼空 ⚠ / 做空 → / 离场 !! / 平仓原因四处）：不截断、信号句子行不强制右框（表格
行仍对齐）。纯展示层。

### 未改动

- 评分/信号逻辑零改动，纯展示层；L2 面板只读 state 快照，不参与打分。

---

## 迭代三十四：修复逐笔首拉积压污染冰山窗口（2026-06-04）

实盘发现：`fetch_ticks` 重启后第一拉 `get_rt_ticker` 一次性返回历史积压（至多 1000 笔），
原逻辑直接聚合成一个 `tick_flow` 窗口行，把"主动买/卖量"灌虚，污染之后 `ICEBERG_WINDOW`(4)
轮的冰山求和——15:35 重启后冰山"卖一补单 55.3×"即此假象（到 15:36 积压行滚出窗口后冰山
即变"无"，印证）。**修复**：首拉（`_last_seq is None`）只用返回数据建立去重基线 `_last_seq`、
**返回 None 不落库、不计入窗口**，第 2 拉起只统计真正的新成交。纯数据层修复，不改评分逻辑。
详见 [[known-bugs-and-gotchas]]。

---

## 迭代三十五：主力嫌疑分（控盘特征筛查，纯背景画像，2026-06-05）

### 背景

2026-06-05 同时跑 00100（MINIMAX）与 00981（中芯）。00100 呈现单一主力控盘的典型指纹：
价格**精确钉扎 601.00** 连续 10+ 轮（系统反复打「守 601.00 未破」）、卖一被**摩根士丹利
长期独占**（每轮 +5~+9 席）、盘薄（卖深仅 ~1.1 万股），10:20 卖墙一撤价格立刻 601→604、
逼空跳 43。而 00981 价格连续游走、盘深 30~70 万股、券商席位（荷银/中投/创盈/富途…）不断
轮换——纯市场化大盘，无单一席位能控盘。用户希望自动挑出前者、避开后者。

### 改动（`short_squeeze_monitor.py`）

新增 `analyze_main_force_control(conn)`（五-D2 节）+ 常量 `MF_*`，复用已有 DB 数据
（price_history / broker_queue / orderbook_snapshots），三子维度加权满分 100：

1. **价格钉扎 (0-50)**：窗口内 `(max-min)/median` 窄带分，与「单一价精确占比」分取 max。
   散户无法把价格焊在某点，长时间钉扎=控盘按价。00100 带宽 0.33%/最高频价占 83% → +33。
2. **席位集中 (0-35)**：**按买/卖侧分别**统计同一席位独占最优档的轮数占比（净≥`MF_SEAT_MIN_NET`）。
   关键：分侧统计可自动滤掉清算行噪音——荷银随成交在买卖两侧来回切，任一侧占比都上不去；
   控盘席位（摩根士丹利）长期钉在卖一 → 100% → +35。
3. **盘薄 (0-15)**：卖盘深度中位 ≤ `MF_THIN_DEPTH_SHARES`(3 万股) → 易控。

四档定性：≥60 强控盘嫌疑 / ≥35 疑似 / ≥20 轻微 / else 市场化。仪表盘 L2 面板新增
`[★] 主力嫌疑 : NN/100 标签` 一行 + ≥20 时展开命中明细（★ 前缀）。

### 验证

内存 DB 离线回放两只票画像：**00100 → 83/100 强控盘嫌疑**（三维全中）、
**00981 → 0/100 无明显控盘(市场化)**（钉扎 0 因价格连续，荷银分侧后占比未过线）。
分侧统计是关键：未分侧时 00981 因荷银高频被误判 35；分侧后归零。

### 未改动 / 注意

- **纯背景画像，不参与逼空/做空任何打分**，仅供选股筛查（像 K 线扫描器）。
- 盘薄阈值按股数判定，与股价量级相关，换低价/高价股需重标定 `MF_THIN_DEPTH_SHARES`。
- 方法论速查（6 条控盘指纹 + 00100/00981 对比标本）见内存 [[main-force-control-screening]]。

详见 [[signal-thresholds]]、[[reading-playbook]]。

### 追加：control_screener.py 控盘标的筛选器（同日）

需求：输入一篮子代码，挑出"像 00100 这类次新小盘控盘"的、避开"像 00981 这类流动性大盘"。
新建 `control_screener.py`，两段式（不重复造轮子，复用既有抓取 + 评分）：

- **Stage 1 静态温床筛（默认，任何时间）**：复用 `watchlist_scanner` 的 `fetch_basic_info/
  fetch_snapshot/fetch_avg_turnover/fetch_recent_short_ratios` + 新增一次性盘口深度，算
  「控盘温床分」(0-100)。**实盘标定（00100/00981）后的权重**：次新 22/15/8 + 高价 14/9/4 +
  薄盘口(买卖十档股数) 22/14/7 + W类 8 + 高卖空 8/4 + 低 HKD 成交额 8/4(弱)。≥55 控盘温床 /
  <30 流动性大盘(避开)。
- **Stage 2 `--probe`（盘中确认）**：对温床候选做 burst 轮询（默认 8×8s），价格+卖深+经纪
  最优档落入临时内存库，复用 `short_squeeze_monitor.analyze_main_force_control` 出「主力嫌疑分」。
  席位维度需 LV2 BROKER；缺权限自动降级（钉扎+薄盘仍有效）。

**实盘标定关键教训（2026-06-05）**：① `circular_market_val` 对 W 类同股不同权≈总股本（00100
报 1890 亿），**自由流通市值取不准 → 移出评分仅作展示**；② **HKD 日均成交额对高价股是劣信号**
（00100 600 元、16 亿/日看着不低，但盘口仅 ~4 万股极易控）→ 降为弱权重；③ **盘口股数深度才是
控盘最干净的区分量**（00100 ~4 万股 vs 00981 ~94 万股，23×）→ 新增为主权重。④ 修复
`watchlist_scanner.fetch_avg_turnover`：`fields=["turnover"]` 报"fields 类型错"，应为
`[KL_FIELD.TRADE_VAL]`（两脚本共用，一并修好）。

实盘验证：**00100→温床 58（控盘温床·重点确认）、00981→温床 12（流动性大盘·避开）**，清晰分流。
`--probe` 盘中实盘（8 轮×6s，LV2）：**00100 主力嫌疑 92/100 强控盘**（钉扎 +43 带宽 0.08% / 中国
国际金融独占买一 8/8 +35 / 薄盘 +14）→ `▶▶ 强候选`。注：席位本轮在买一(吸筹托盘)，与早盘摩根
士丹利钉卖一(派发)相反——同侧逻辑正确，席位钉哪侧泄露主力当下意图。`--rounds < MF_MIN_ROUNDS(6)`
时样本不足、不打分并提示（不再误显示"未见足迹"）。运行见 CLAUDE.md。方法论见 [[main-force-control-screening]]。

---

## 迭代三十六：主力嫌疑分三项优化（席位机构化 + 联动做空门 + 钉价不误判停滞，2026-06-05）

### 背景

实盘 00100 10:38–10:45：价格钉死 601–603、薄盘 ~1.2 万股、卖一被**富瑞/摩根/巴克莱外资行
轮换压制**、主动卖 3–39× 被全吃不跌（派发）。暴露三个问题：① 席位维度只认单一名 ≥50%，
轮换大行单名 <50% → 漏判；② 主力嫌疑分"不参与打分"，但控盘钉价股正是做空被逼空高发区，
系统仍喊 ENTRY；③ 价格+大单冻结触发 STALE 跳过打分，但这其实是主力钉价（逐笔仍在成交）。

### 改动（`short_squeeze_monitor.py`）

1. **席位集中机构化**（`analyze_main_force_control` ②）：新增 `INSTITUTIONAL_BROKER_KEYWORDS`
   外资/机构大行名单 + `_broker_is_institutional`。席位只计机构大行（零售聚合商如富途占一侧
   是散单汇集、非控盘，过滤掉）。两法取强：单一机构独占某侧 / 多家机构轮换压同侧
   （`MF_INST_SIDE_RATIO`）。带方向读：**压卖一=派发·压价 / 压买一=吸筹·护盘**。函数返回值
   加第 4 元 `tags={pin,seat,thin,dom_side,dom_kind}`。
2. **联动做空安全门**：强控盘(`MF_CONTROL_GATE`=60) 且属"逼空型"——机构护盘压买一(吸筹托底)
   **或** 价格硬钉扎(`MF_PIN_GATE`=30)——时，做空 ENTRY 降级 CAUTION。机构压卖一(派发)不在此
   列（合法做空环境，已有摆盘失衡 failsafe 兜底）。仅降级不解禁。
3. **STALE 不误判钉价**：价格+大单冻结时，若本轮逐笔仍有主动成交量(`_tick_active`)，
   不计停滞——市场在交易、只是价格被焊住，正是该警觉之时，不应跳过打分。

### 验证（内存 DB 离线回放 + 分类器）

- 轮换外资压卖一（10:38 段）：**66/100 强控盘，dom=卖一/inst「外资连续压卖一 92%→派发」**
  （旧逻辑单名 <50% 给 0，漏判）。
- 护盘型（中金钉买一 8/8 + 钉价）：93/100，dom=买一，pin=43 → **做空 ENTRY 降级 ✓**。
- 派发型（外资压卖一，pin=22）→ 不降级（合法做空）✓。
- 00981 流动性大盘：0；分类器 富途/荷银=零售/清算✗、摩根=机构✓。
- `control_screener` 调用点同步改 4 元解包。

详见 [[main-force-control-screening]]、[[reading-playbook]]。

---

## 迭代三十七：修复两处做空评分注水缺陷（背景独自堆分 + 大单抖动绕过冻结，2026-06-05）

### 背景

实盘 00100 13:05–13:34（午休前后薄量）复盘日志：做空分在 0→58→73→85→98 反复横跳，价格
仅在 570–576 这条 1% 窄带内震荡、未破位。拆分发现两个评分缺陷把"已跌透"的滞后画像反复
误判为新鲜 ENTRY：

1. **背景注水（Bug 24）**：`short_score = main_signal_score + support_score`。`support_score`
   全是日级静态量（HKEX 10/20 日成本线 +15/+8、卖空动能比 3.04× +20、爆量），整日恒定 ~43、
   与盘中此刻无关。主信号仅 27~30 时，背景 43 独自把总分顶过 ENTRY 门槛（55）→ 反复输出
   73~98 假 ENTRY。背景应是上下文，不应单独触发入场。
2. **大单抖动绕过冻结（Bug 25）**：Bug 14 的冻结检测用精确相等（`big_net == _prev`），但 Futu
   常在 2~3 个缓存值间小幅抖动——日志中大单累计在 -3519/-3450/-3333 万循环跳变，每次跳变都把
   stale 计数器清零，冻结凑不满 `BIG_NET_STALE_ROUNDS(5)` → 维度1「持续为负」+15 分一直
   基于陈旧值计入主分。

### 改动（`short_squeeze_monitor.py`）

1. **背景注水门（Bug 24）**（新常量 `SHORT_MAIN_FRESH_MIN=40`）：合并 main+support 并完成 HOLD 升级、
   failsafe、主力嫌疑门之后，若 `short_signal=="ENTRY"` 但 `main_signal_score < 40`，降级
   CAUTION 并附注"总分由日级背景堆出而非盘中新鲜触发"。40 需至少一个强盘中维度兜底（卖深
   骤增 25 / 大单转负 30），纯靠 持续为负15+价格低于日高15(=30) 凑不到。仅降级不解禁。
2. **冻结检测带容差（Bug 25）**（新常量 `BIG_NET_STALE_BAND_PCT=0.06`）：把精确相等改为锚点带容差——
   `_prev_big_net_only` 复用为锚点，新值落在锚点 ±6% 内即视为未实质变化、累计 stale 计数，
   仅在突破带宽时才重置并重新锚定。捕获 -3519/-3450/-3333 这类循环抖动（spread ≤5.3%）。

### 验证（对照实盘日志逐轮推演）

- 午休 main=27~30 的假 ENTRY（13:29:38 / 13:30:54 / 13:31:09–24 / 13:33:25 等）→ `<40` →
  **全部降级 CAUTION**，与"整段窄幅震荡=不交易"结论一致。
- main=55（13:29:53，卖深骤增 86% +25 真盘中触发）→ ≥40 → **保留 ENTRY**，不误杀。
- 带容差：锚点 -3519 万，-3333(5.3%)/-3450(2.0%) 落在 ±6% 内 → 计数累积 → 5 轮后触发
  「大单停滞」跳过维度1，主信号进一步下探、更易落入 CAUTION。两修复叠加压制午休噪音。
- `python3 -c ast.parse` 语法通过。

### 未改动 / 注意

- 仅修两处命名缺陷，未审计 `s_struct/s_dist/s_snd` 等其它消费 big_net 的背景维度（同源
  陈旧风险存在但本次不扩范围）。
- 40 与 6% 为首版标定，待午盘/收盘实盘进一步回放校准。

详见 [[signal-thresholds]]、[[known-bugs-and-gotchas]]、[[feedback-signal-stability]]。

---

## 迭代三十八：L2 数据利用 + 仪表盘四项优化（2026-06-05）

### 背景

复盘后评估"L2 数据是否充分利用 / 仪表盘是否可优化"，发现四处：①`get_order_book(num=10)` 抓
十档却 `sum` 拍平成总深度、只留买一/卖一，丢了挂单分布形态；②`fetch_ticks` 把窗口内成交
按方向求和，单笔大单扫盘与百笔散单求和无法区分；③仪表盘 f-string 按字符数对齐，CJK 双宽
导致右框线参差（实盘日志可见）；④L2逐笔面板用单轮瞬时值，常显 `0/0 ∞`、`40×` 等单帧噪音。

### 改动（`short_squeeze_monitor.py`，`paper_trader.py`）

1. **#1 十档深度形态**：`fetch_order_book` 计算 `bid_top3/ask_top3` + 回传逐档量；
   `orderbook_snapshots` 扩 4 列（`best_bid_vol/best_ask_vol/bid_top3/ask_top3`），老库经
   `_add_cols` 幂等 ALTER 迁移。新函数 `describe_book_shape` 出"卖墙集中卖一X%(压价/诱空)
   vs 浅档堆叠 vs 均匀"即时画像，仪表盘新增「十档形态」行。常量 `BOOK_WALL_CONC_HIGH=0.55`
   / `BOOK_TOP3_CONC_HIGH=0.80`。**纯展示+信息性，不改数值评分**（待实盘标定后再考虑计分）。
2. **#2 逐笔大单识别**：`fetch_ticks` 按 `LARGE_TICK_VOL=2000` 股拆出主动大单买/卖量与笔数；
   `tick_flow` 扩 4 列（`large_buy/sell_vol`、`large_buy/sell_cnt`）。仪表盘新增「主动大单」行
   显示「主买 N笔/X股 主卖 M笔/Y股」。`db_get_recent_ticks` 的 SELECT 保持不变（6 元组），
   不破坏 `analyze_iceberg_absorption` 的位置索引。
3. **#3 CJK 对齐修复**：新增 `_disp_width`/`_pad_disp`/`_box`（按东亚宽字符显示列对齐），
   `DASH_INNER_W=58`。仪表盘头部/L2/逼空/做空/持仓/平仓所有框内行统一经 `_box` 渲染，
   边距收为单空格（内容区 56 列），消除 CJK 错位。审计：所有整框行显示宽度恒为 60。
4. **#4 逐笔平滑展示**：主循环每轮聚合近 `ICEBERG_WINDOW` 轮 tick → `state.tick_window`，
   L2逐笔行改显「近N轮 买X/卖Y (主买/卖N×)」，替换单轮瞬时极值。

**schema 兼容**：`db_save_orderbook` 改列名显式 INSERT；`paper_trader.py` 的同名副本同步改
列名显式（只写基础 4 列），避免扩列后 positional VALUES 列数不符报错。无 `SELECT *` 位置依赖
（仅 `cmd_export` 用，pandas 自适应多列）。

### 验证

- 真库副本 `_add_cols` 迁移：两表正确补列，新/旧两种 INSERT 都通过，`db_get_recent_ticks`
  仍返回 6 元组（iceberg 不破）。
- 渲染冒烟测试（mock state）：所有整框行显示宽度恒 60、`║` 对齐；长行（标题/逐笔/做空分）
  收紧后不再截断。两文件 `py_compile` 通过。

### 未改动 / 后续

- #1/#2 本轮只做"抓取+存储+展示+信息性提示"，**未接入数值评分**——share 阈值（墙集中度、
  大单 2000 股）须实盘采集后标定，与 iceberg 同理（首版无历史可回放）。计分接入留作后续。
- 阈值 `LARGE_TICK_VOL=2000`、`BOOK_WALL_CONC_HIGH=0.55` 为首版，换标的（尤其低价/小成交）
  必重标定。

详见 [[signal-thresholds]]、[[reading-playbook]]、[[capital-flow-tiers-intent]]。

---

## 迭代三十九：派发对峙·散户接盘检测（报价层 vs 成交层打架，2026-06-05）

### 背景

实盘 00100 15:08-15:10 尾盘：价格钉死 558（钉扎 +35）、摆盘失衡 **+0.25~+0.30（偏多）**、
卖盘骤增 ~100% 被诱空保护判"失衡偏多→不计分"。但同期大单累计 **-5,400 万持续净流出**、
L2 主动卖 5-6×、经纪 **J.P.摩根/高盛/摩根士丹利压卖一**派发、**富途证券国际 +11 席占买一**。
即：报价层（盘口偏多·散户挂买接盘）被成交层（机构派发）证伪——盘口那个"多"不是利好，
是派发燃料。现有诱空保护在这种"报价多+成交空"组合下偏保守，是盲区。

### 改动（`short_squeeze_monitor.py`）

新增 `detect_distribution_standoff(conn, state)`：四件套同时成立即出显式警示——
① 机构压卖一（复用主力嫌疑席位维度 `tags.dom_side=='卖一'`，已机构化过滤）② 盘口偏多
（近3轮失衡度中位 ≥ `STANDOFF_IMB_MIN=0.15`）③ 大单近3轮全负（持续净流出）④ 散户占买一
（新增 `RETAIL_BROKER_KEYWORDS` + `_broker_is_retail`，富途/老虎等零售聚合商，可选增强文案）。
仪表盘在主力嫌疑明细后加 `⚖` 整行警示（无右框、全文不截断）。**纯信息性，不改数值评分**
（避免与 主力嫌疑「压卖一」、`sell_no_drop` 双计）。需 L2 经纪席位，LV1 无 broker 自然不触发。

### 验证

- 离线回放该实盘场景（机构压卖一 + 富途买一 + 盘口偏多 + 大单净流出）→ 正确触发，文案
  含"散户(富途证券国际)在买一接盘"。
- 反例：机构压买一（吸筹）→ None；盘口偏空 → None。仪表盘渲染 `⚖` 行正常。

### 未改动 / 后续

- 只做了派发方向（机构卖·散户买）；对称的**吸筹对峙**（机构压买一·散户卖一·大单净流入）
  逻辑对称、留作后续按需补。
- `STANDOFF_IMB_MIN=0.15`、零售券商名单为首版；换标的/券商生态需校准名单。

详见 [[reading-playbook]]、[[capital-flow-tiers-intent]]、[[main-force-control-screening]]。

---

## 迭代四十：席位结构 = 主力×散户双向画像（充分利用 L2 经纪席位，2026-06-05）

### 背景

审计发现 `fetch_broker_queue` 已算出每侧最优档全体席位 `{name:count}`，却**只持久化单一净
不对称最强的那个席位**，把散户侧（富途/老虎）的席位数丢弃了。后果：①散户方向无稳健读数；
②刚加的派发对峙检测条件④（散户占买一）依赖 `bid_top_name` 是否恰好为散户——但机构净值常
盖过散户，导致漏判。席位队列天然能把"机构大行=主力方向 / 零售聚合商=散户方向"分开，没用上。

### 改动（`short_squeeze_monitor.py`）

1. **席位分类持久化**：`fetch_broker_queue` 新增 `_classify`，把最优档席位拆成机构(`_broker_is_
   institutional`) / 零售(`_broker_is_retail`) 两类计数，每侧各存。`broker_queue` 扩 4 列
   （`bid_inst/bid_retail/ask_inst/ask_retail`），老库经 `_add_cols` 幂等迁移，`db_save_broker`
   改列名显式 INSERT。`db_get_recent_brokers` 的 SELECT 不变（保 4 元组、不破 main_force/footprint）。
2. **席位结构读**：新函数 `read_seat_structure` 出每轮"主力 机构压卖N · 散户买M →派发对峙"
   双向画像（机构压哪侧=主力意图，散户挂哪侧=接盘/出货），含派发/吸筹对峙裁决。仪表盘 L2 面板
   新增「席位结构」行。纯展示。
3. **派发对峙更稳健**：`detect_distribution_standoff` 条件④改用 `bid_retail >= 1`（稳健席位计数）
   替代脆弱的单一 `bid_top_name` 判断。

### 验证

- 真库副本迁移：broker_queue 正确补 4 列，新 INSERT 通过，`db_get_recent_brokers` 仍 4 元组。
- `read_seat_structure` 离线：实盘场景（富途买一 11 席 + J.P.摩根/高盛压卖一）→
  `机构压卖3 · 散户买11 →派发对峙`；吸筹反例 → `机构压买4 · 散户卖5 →吸筹对峙`；无 L2 → None。
- 仪表盘渲染：席位结构行对齐（整框行恒 60 列）、不截断。两文件 `py_compile` 通过。

### 未改动 / 后续

- 仍**纯展示不计分**（与迭代三十八/三十九一致，避免与主力嫌疑席位维度双计）。要计分时可把
  "席位结构方向 + 持续轮数"做成 squeeze/support 维度，但需先标定。
- 零售券商名单 `RETAIL_BROKER_KEYWORDS` 为首版，换券商生态需补全。

详见 [[main-force-control-screening]]、[[reading-playbook]]、[[capital-flow-tiers-intent]]。

---

## 迭代四十一：主动大单累计净视图（穿透薄盘比值翻面，2026-06-05）

### 背景

实盘 00100 15:36-15:41 尾盘薄盘：L2 逐笔主买/主卖比值 90 秒内 25×买→11×卖→16×买剧烈翻面。
根因是薄盘里**单笔大单主宰整个窗口**（15:39:38 那轮"卖11,060"实为一笔 8,220 股主卖），分母太
小使比值结构性失真。单看比值会被来回翻晃误导，需要累计净额看真实方向。

### 改动（`short_squeeze_monitor.py`）

新常量 `LARGE_TICK_CUM_WINDOW=40`（≈10 分钟）+ `db_get_large_tick_cum`（单独查 `large_*` 列，
不动 `db_get_recent_ticks` 的 6 列 SELECT、不破冰山位置索引）。主循环聚合进 `state.tick_window`
（`cum_lbuy/cum_lsell/cum_*_cnt`）。仪表盘「主动大单」下新增「↳累计净」行：
`近40轮 买X/卖Y 净卖-Z股`，一眼看出主动大单累计净方向。纯展示。

### 验证

- `db_get_large_tick_cum` 离线：6 轮模拟（散小买 + 一笔 8,220 主卖）→ (4000,8220,2,1) 正确。
- 渲染：`↳累计净 : 近40轮 买4,000/卖8,220 净卖-4,220股`，整框行恒 60 列对齐。两文件编译通过。

详见 [[reading-playbook]]、[[capital-flow-tiers-intent]]。

---

## 迭代四十二：冰山门槛改成交额，修高价薄盘股执行层失明（2026-06-08）

### 背景

06-08 实盘 00100 10:07-10:13：教科书级派发吸收形态——主动买主导(主买比 7×→11×)、累计净买
+18,980 股，价格却在 490-498 原地磨、汇丰证券经纪持续压卖一——但仪表盘 `[④] L2冰山:无`
全程是"无"，[analyze_iceberg_absorption](../short_squeeze_monitor.py) 一次没触发。

根因：`ICEBERG_MIN_VOL=50_000`（股）是**绝对股数**门槛，无法跨价位 scale。00100 价 ~500 HKD，
4 轮窗口主动买最高才 ~15,000 股，离 5 万股差 3-8 倍，永远迈不过。连锁后果：经纪足迹
（`analyze_broker_footprint`）设计上"绝不单独加分，仅当冰山同向触发时交叉验证"——冰山触发
不了，汇丰压卖 5-6 席的派发足迹也跟着变死代码。整个 L2 执行裁决层（区分"钱进价不动 = 吸筹
还是派发"这个核心歧义的那层）在高价薄盘股上被静默关掉。

### 改动（`short_squeeze_monitor.py`）

`ICEBERG_MIN_VOL=50_000`（股）→ `ICEBERG_MIN_NOTIONAL=1_500_000`（HKD 成交额）。函数内新增
`sell_notional/buy_notional = 主导方股数 × price_last`，两处门槛改判成交额。1.5M 与本文件其余
资金流门槛对齐（`CAPITAL_EFFICIENCY_MIN_INFLOW=3M/8轮` 的半值，对应 4 轮窗口）。

### 验证

回放 06-08 当日 212 行 tick_flow：旧门槛全天**派发 0/吸筹 0**（确证失明）；新门槛**派发 37/
吸筹 3**，全部落在主动买主导+滞涨形态，卖一补单 19×-853×（显示 20 股被吃 1.7 万股=冰山铁证），
方向与人工读盘一致（派发≫吸筹）。`ast.parse` 通过，无 `ICEBERG_MIN_VOL` 残留引用。

### 改动 #2：主动大单门槛同样改成交额（LARGE_TICK_VOL → LARGE_TICK_NOTIONAL）

同源问题：`LARGE_TICK_VOL=2000`（股）对低价票把散单误标"大单"——06082(~60 HKD) 上 2000 股
仅 12万 HKD（历史 1909 笔被标 large）。改 `LARGE_TICK_NOTIONAL=1_000_000`（HKD），`fetch_ticks`
内按 `单笔 volume × price` 折算。注：此维度**仅展示**（仪表盘「主动大单」/「↳累计净」），不进评分，
故只是跨标的展示一致性修复。等效性核对：00100(~497) 旧 2000股=99万 ≈ 新 1M HKD=2,014股（**不变**）；
06082(~60) 新 1M=16,611股（大幅收紧到真主力足迹）。仪表盘空态文案 "≥2,000 股单笔"→"≥100万 单笔"。

### 未改动 / 后续

- per-stock 覆写（各标的活跃度差异大时提为 STOCKS 配置）暂未接线，注释留作扩展点。
- `refill` 在显示档量≈0 时会爆到几百倍（cosmetic），属既有展示问题，未动。
- 优化点 ②（capital_efficiency 用经纪足迹方向消歧"吸筹 vs 派发"、喂做空侧）暂缓——Bug 26
  修复后冰山层已能执行级消歧并加 support 分，②ROI 下降，待实盘观察后再定。

详见 [[known-bugs-and-gotchas]]、[[reading-playbook]]、[[capital-flow-tiers-intent]]。

---

## 迭代四十三：安全门通道 C 加"仍在低位"约束，修横盘期常开放行（2026-06-08）

### 背景

`apply_short_entry_failsafes` 的通道 C（capitulation 放行）原判据 `channel_c = pullback_pct >=
SHORT_BLOCK_SESSION_PULLBACK_PCT`，**纯静态**——只看"距日内高点回落 X%"。日高是几小时前的旧
anchor，价格大跌后卡在区间时回落% 恒超阈值 → 安全门**每轮常开**。实盘 06-08 10:07-10:13 价格
490-498 横盘、距日高 522 恒 -4.6%，"[安全门放行]" 逐轮刷屏（27/27 轮），逼空保护在跌后反弹期
形同虚设。

### 改动（`short_squeeze_monitor.py`）

新常量 `SHORT_BLOCK_CAPITULATION_REBOUND_PCT=1.0`。通道 C 增加与条件：除"距日高回落够深"外，
还要求价格**仍压在日内低点附近**——`rebound_pct = (price - session_low)/session_low ≤ 1%`。
自日内低点反弹超过 1% = capitulation 结束（企稳/反弹），收回放行。无 session_low（开盘首样本）
时不收紧、保持原行为。放行文案附"仍压日内低 X（反弹 Y%）"。

### 验证

回放 06-08 当日 576 行 price_history：通道 C 全日放行 **502→88 轮**；聚焦 10:07-10:13 刷屏段
**27→10 轮**，判别正确——价格压低点(490-494/反弹≤1%)仍放行（真 capitulation），反弹到 495-498
(反弹>1%)收回（企稳）。保留原 15:23 真 capitulation 用例（价在低点→仍触发）。`ast.parse` 通过。

### 设计原则

capitulation 是"价格正在低点下杀"的实时状态，不是"距某个旧高点的静态距离"。任何"回落/距高"
类放行闸都必须配"价格此刻是否仍在低位"的活性条件，否则旧 anchor 会让闸门在跌后横盘期永久顶开
——与 Bug 8/21「anchor 必须成对、距低点要配距峰值」同源。

### 随手补丁：通道 C 放行文案防 NoneType 崩溃

A2 加的兜底分支 `session_low is None → still_at_lows=True` 可使 channel_c 在 session_low 为
None 时为真，而放行文案直接 `f"{session_low:.1f}"` → 抛 TypeError 打挂整轮评分。虽然
`session_high>0` 实践中蕴含 session_low 非空（同源 price_history），但防御分支自身崩溃比不防御
更糟。修复：文案的"仍压日内低 X"段改为 session_low 非空时才拼接，None 时降级为空串。

详见 [[known-bugs-and-gotchas]]。

---

## 迭代四十四：大单冻结容差带改绝对额，修"百分比×累计"吞真实流动（Bug 28，2026-06-08）

### 背景

06-08 实盘 00100 下午：`大单停滞` 计数一路涨到 280+ 轮（≈1 小时），维度①整段跳过。但同期大单
其实在动（+8,091→7,778→7,961万），15:33:42 还出现一笔 **+182.7万 主动大单**（对应 L2 主买 8,020
股）——这是明确的主力进场，却被"冻结"逻辑当成抖动吞掉，维度①对下午的派发/进场资金流半瞎。

### 根因

Bug 25 的容差带写成 `abs(_bn - _anchor) <= abs(_anchor) × 6%`——锚点是**当日累计大单净额**。下午
累计涨到 +8,000万时，带宽 = ±480万，远超它本要吸收的 Futu 缓存抖动（实测 ≤100万）。且带宽随累计
膨胀、方向反了：越到尾盘越顽固。冻结期 `_prev_big_net_only` 只在突破时更新，单向慢漂（-180万级）
永远破不了 480万带 → 锚点死钉、连冻 180+ 轮。

### 改动（`short_squeeze_monitor.py`）

`BIG_NET_STALE_BAND_PCT=0.06` → `BIG_NET_STALE_BAND_HKD=1_500_000`（绝对额）。判定改
`abs(_bn - _anchor) <= BIG_NET_STALE_BAND_HKD`，去掉 `_anchor != 0` 守门（绝对额下 anchor=0 也成立）。
阈值标定：实测 06-08 capital_flow 相邻 Δ——抖动/小动多 ≤100万（中位89万），真实机构流动 ≥200万
（全天26次），150万卡其间，既吸抖动又放真单。

### 验证

回放当日 988 行 capital_flow：旧逻辑冻结跳过 814/988(82%)、最长连冻 **181 轮(≈45min)**；新逻辑
664/988(67%)、最长连冻 **83 轮(≈20min)**。15:33:42 的 +183万真单：旧逻辑(±467万)仍冻结漏掉，
新逻辑(±150万)解冻重锚、维度①恢复计分。缓存抖动轮(Δ+0.0万)新旧都冻结——Bug 25 未被破坏。
`ast.parse` 通过，无 `BIG_NET_STALE_BAND_PCT` 残留。

### 设计原则

任何"是否实质变化"的容差都该锚到**变化量本身的量级**（抖动幅度），绝不能锚到**累计基数的百分比**
——累计基数会随日内增长把容差撑大、把真实信号吞掉。与 Bug 26/③（绝对股数→notional）反向但同源：
那是"该用相对量却用了绝对量"，这是"该用绝对量却用了相对量"——**阈值的基准要对齐被判定的物理量**。

详见 [[known-bugs-and-gotchas]]。

---

*本文档记录截止：2026-06-08。*
