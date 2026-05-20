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

*本文档记录截止：2026-05-20。*
