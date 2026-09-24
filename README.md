# A股 AI 智能选股系统（Phase 1）

基于 `E:\星空系统\股票系统\` 下多个开源项目重构的 A 股盘后选股与复盘系统。
本仓库当前为 **Phase 1：可运行的最小闭环**。

- 设计方案：[`E:\星空系统\A股AI选股系统_Phase1实施方案.md`](../A股AI选股系统_Phase1实施方案.md)
- 技术栈：Python 3.11 + DuckDB + Parquet + FastAPI + 单页 ECharts 前端 + DeepSeek（可选）
- 数据源：baostock（主）+ adata / akshare（辅），全部免费、无需 Token

---

## 一、它现在能做什么

一条命令完成「补数 → 因子 → 市场状态 → 选股 → 评分 → 报告 → 复盘」全链路：

```
python -m astock.cli daily
```

输出：

| 产物 | 位置 | 说明 |
|---|---|---|
| 控制台摘要 | stdout | 市场状态 + 三档候选（每档 5 只） |
| Markdown 报告 | `data/reports/report_<日期>.md` | 市场环境、候选表、推荐逻辑、评分拆解、风险提示 |
| 结构化结果 | `data/reports/report_<日期>.json` | 供 API 与前端消费 |
| 推荐记录 | DuckDB `ads_recommend` | 含策略名、参数版本，可追溯 |
| 复盘记录 | DuckDB `ads_review` | 次日自动回填 |

Web 界面（市场宽度趋势、涨跌停生态、候选股明细、复盘统计）：

```
python -m astock.cli serve
# 浏览器打开 http://127.0.0.1:8000
```

---

## 二、快速开始

### 1. 环境（已完成，无需重做）

已创建 conda 环境 `astock`（Python 3.11），依赖全部安装完毕。

如需在新机器重建：

```bat
scripts\setup_env.bat
```

> 注意：不要用本机 `base` 环境的 Python 3.13 —— `adata` 依赖的 mini-racer 在 3.13 上兼容性有风险。

### 2. 初始化数据库

```bat
D:\miniconda3\envs\astock\python.exe -m astock.cli initdb
```

### 3. 回填历史数据（首次执行一次）

```bat
scripts\run_backfill.bat
```

> **当前状态**：全市场回填已完成 —— **5263 / 5559 只，249.9 万根日线，覆盖率 94.7%**。
> 未覆盖的 296 只中，约 91% 是**已退市股票**（用 `python -m astock.cli check` 可核实），
> 属于正常情况而非采集失败。剩余少量可用 `backfill --retry-empty` 重试。
> 详见第 12 章「数据源限流与恢复」。

#### 怎么查看回填进度

**采集正在运行时**（数据库被独占）：

```bat
python scripts\monitor.py
```

输出包含：当前批次、进度条、已入库行数、失败数、剩余时间估算、数据源限流告警、
以及「采集任务是否真的在运行」的结论（通过读取进程命令行判断，不靠猜日志）。

**采集已停止时**（数据库空闲，数字最准）：

```bat
python -m astock.cli status
```

输出 `coverage` 字段即覆盖率，`stocks_covered` / `stocks_total` 为具体只数。

若主库正被占用，`status` **会自动降级**到展示层快照并打印占用进程的 PID，不会直接报错卡住。
也可以强制只读快照（任何任务运行期间都能用）：

```bat
python -m astock.cli status --snapshot
```

**两个命令都在** `data\logs\` 下对应的日志文件：

| 文件 | 内容 |
|---|---|
| `data\logs\astock.log` | 结构化日志（UTF-8，含所有模块的 INFO/DEBUG） |
| `data\logs\backfill.log` | 回填任务的 stdout（含 baostock 输出，中文可能因控制台代码页而乱码） |

全市场约 5559 只、近 2 年日线，实测单只中位 **1.67 秒**：

| 并发 | 预计耗时 |
|---|---|
| 1 进程 | 约 3.7 小时 |
| 4 进程 | 约 56 分钟 |
| 6 进程 | 约 38 分钟 |

**中断无所谓**：直接重跑即可续传，已入库的股票会自动跳过。

小样本先验证：

```bat
python -m astock.cli backfill --limit 300 --workers 6
```

### 4. 每日流程

```bat
python -m astock.cli daily
```

### 5. 查看界面

```bat
scripts\run_serve.bat
```

浏览器打开 `http://127.0.0.1:8000`。

> **重要说明（计算层与展示层是分开的）**
> DuckDB 对数据库文件是**独占锁**，服务与采集无法同时持有主库。
> 因此 Web 服务只读 `data/serving/*.parquet` 快照，**完全不碰主库**。
> 结果是：**回填 5500 只股票的同时，界面照常可用**。
> 界面显示的是「上一次 daily 导出的快照」，想看最新数据就再跑一次 `daily`
> （或单独执行 `python -m astock.cli export` 仅刷新快照，不重算选股）。

---

## 三、命令清单

| 命令 | 作用 |
|---|---|
| `initdb` | 初始化数据库表结构 |
| `backfill` | 回填历史日线（可重复执行，自动续传） |
| `backfill --retry-empty` | 重试此前被标记为「无数据」的股票 |
| `factor` | 重建因子表（`--all` 全量重建） |
| `regime` | 重算市场状态 |
| `daily` | 每日全流程（`--no-refresh` 跳过补数、`--llm` 启用 AI 解读） |
| `review` | T+1 复盘与统计（`--summary` 只看汇总） |
| `status` | 数据覆盖率与采集状态 |
| `check` | 数据体检：区分「真退市」与「采集失败」 |
| `backtest` | **样本外验证**：历史逐日回放，统计各策略真实胜率（见第 13 章） |
| `skills` | **策略 Skill 库**：`list` / `show` / `sync` / `stats`（见第 14 章） |
| `sector` | 同步行业映射（`--top 15` 顺便看板块强度排行） |
| `sector-strength` | 由本地日线计算板块强度（`--all` 全量回填历史） |
| `limit-pool` | **采集涨停/炸板池**（`--years 1` 回填历史，`--show 10` 看当日涨停板） |
| `dragon-tiger` | **采集龙虎榜**（每天 1 次请求，`--years 1` 回填历史） |
| `auction` | **盘前竞价**：展示候选股集合竞价与高开风险（交易日 09:25 后跑） |
| `finance` | 采集财务三表与指标（`--all` 全市场回填约 17 小时，建议夜间） |
| `attribution` | **复盘归因**：用 LLM 把「涨跌」变成「为什么」，按原因分类聚合 |
| `export` | 仅刷新展示层快照（不重算选股） |
| `serve` | 启动本地 Web 服务 |

诊断脚本：

| 脚本 | 作用 |
|---|---|
| `scripts\monitor.py` | 后台任务监控（自动识别当前在跑什么，给出进度与 ETA，并顶出失败告警） |
| `scripts\backup_data.py` | 数据备份（含锁检测与副本校验，见第 10 章） |
| `scripts\register_task.ps1` | 注册计划任务（含 `StartWhenAvailable`，见第 10 章） |
| `scripts\probe_source.py` | 数据源连通性诊断 |
| `scripts\probe_alt_source.py` | 备用通道探测（**直接调用系统真实适配器**，所见即系统所得） |
| `scripts\regime_matrix.py` | 市场状态 × 档位交叉表现，用于校准 `tier_weights` |
| `scripts\factor_ic.py` | **涨停因子有效性检验**：IC + 分组收益 + 多重检验校正 + 样本稳定性 |
| `scripts\dragon_ic.py` | **龙虎榜有效性检验**：字段填充率 + IC + 稳定性（数据准入验收工具） |
| `scripts\data_audit.py` | **数据资产体检**：台账/实际库/代码引用三方核对，自动检出「只写不读」的死数据 |
| `scripts\check_caliber.py` | **口径交叉校验**：上游涨停家数 vs 自算，防状态判定被口径问题带偏 |
| `scripts\run_dragon_backfill.bat` | 龙虎榜历史回填（后台跑，约 17 分钟/年） |
| `scripts\bench_collect.py` | 采集性能基准测试 |
| `scripts\run_tests.bat` | **一键跑全部自检**（见 `tests/` 目录） |
| `scripts\run_validation.bat` | 一键跑完整验证链：因子 → 市场状态 → 快照 → 回测 → Skill 库 |

测试脚本独立放在 `tests/` 目录（详见 `tests/README.md`）：

| 测试 | 验证什么 |
|---|---|
| `tests\test_report.py` | 报告生成回归（字段、空值、串档） |
| `tests\smoke_test.py` | 全部 API 端点 + **并发阶段** |
| `tests\test_serving_concurrency.py` | 展示层读取器的线程安全 |

---

## 四、目录结构

```
astock_ai/
├── config/settings.yaml     所有阈值与策略参数（集中管理，便于后续做参数优化）
├── astock/
│   ├── cli.py               命令入口
│   ├── storage/             DuckDB 访问层（唯一接触数据库的地方）
│   ├── data/                采集层：多源适配器 / 交易日历 / 股票池 / 采集器
│   ├── features/            因子层：SQL 窗口函数一次性算完全市场指标
│   ├── market/              市场状态识别
│   ├── strategy/            策略层：短线 / 波段 / 价值三档
│   ├── scoring/             评分融合
│   ├── recommend/           推荐流水线编排
│   ├── review/              T+1 复盘
│   ├── llm/                 DeepSeek 客户端 + token 控制
│   ├── report/              报告生成
│   ├── backtest/            样本外验证：回放引擎 / 统计指标 / 报告
│   └── skills/              Skill 库：策略档案 + 参数快照 + 历史成功率
├── api/server.py            FastAPI 接口（Phase 3 可直接复用给 Vue3）
├── web/index.html           单页前端（原生 JS + ECharts，无需构建）
├── skills/                  策略 Skill 落盘目录（每个策略一个子目录）
├── scripts/                 运维与诊断脚本（bat 入口、探测、备份、监控）
├── tests/                   可重复执行的测试（一键：scripts\run_tests.bat）
└── data/
    ├── astock.duckdb        主库
    ├── serving/             展示层快照（Web 只读这里）
    ├── backtest/            回测报告与明细 CSV
    ├── reports/             每日选股报告
    └── logs/                日志（astock.log 结构化 / backfill.log 采集 stdout）
```

---

## 五、数据层

### 表结构

| 表 | 用途 |
|---|---|
| `dim_stock` | 股票维度（名称、板块、上市日期、ST、状态） |
| `trade_calendar` | 交易日历 |
| `dwd_daily_bar` | 日 K 线（核心明细） |
| `dwd_index_bar` | 指数日线 |
| `dws_feature` | 因子宽表（全部由 SQL 窗口函数计算） |
| `dws_market_regime` | 每日市场状态快照 |
| `ads_recommend` | 推荐记录 |
| `ads_review` | 复盘记录 |
| `sys_collect_state` | 采集游标（断点续传核心） |
| `sys_llm_usage` | LLM token 消耗审计 |

### 针对「电脑不保证 24 小时开机」的设计

1. **幂等写入**：所有写入按主键 `INSERT OR REPLACE`，重复跑不产生脏数据。
2. **断点续传**：以「本地最新日期」为游标，中断后重跑自动只补缺失部分。
3. **自动补数**：`daily` 启动时比对交易日历与本地数据，若有缺口自动逐日补齐。
4. **失败不误判**：采集失败与「该股确实无数据」严格区分 —— 失败不会写 `EMPTY` 标记，下次还会重试。
5. **冷热分离**：超过 `storage.hot_days` 的明细可归档为 Parquet（`Storage.archive_parquet`）。

---

## 六、策略清单

### 波段档（3~20 日）

| 策略 | 来源 | 条件 |
|---|---|---|
| 海龟突破 | 移植 Sequoia-X | 20 日新高 + 成交额过亿 + 实体阳线真涨（防诱多） |
| 均线放量金叉 | 移植 Sequoia-X | MA5 上穿 MA20 + 量能 > 20 日均量 1.5 倍 |
| RPS 相对强度突破 | 移植 Sequoia-X | 120 日 RPS ≥ 90 + 接近 120 日高点 |
| 高窄旗形整理 | 移植 Sequoia-X | 40 日强动量 + 10 日极度收敛 + 缩量 + 高位抗跌 |
| 上升趋势跌停反包 | 移植 Sequoia-X | MA20 > MA60 + 今日放量跌停（错杀） |

### 短线档（1~3 日）

| 策略 | 来源 | 条件 |
|---|---|---|
| 涨停洗盘回踩 | 移植 Sequoia-X | 昨日涨停 + 今日放量收阴 + 不破昨收 |
| 次新股异动 | 新增 | 上市 ≤ 120 日 + 换手 ≥ 15% + 放量 + 涨幅 ≥ 3% |

> 短线档定位为**盘后生成次日观察池**，不做盘中实时信号（受限于机器不常开 + 免费源分钟数据不稳定）。

### 价值档（1 月~1 年）

| 策略 | 来源 | 条件 |
|---|---|---|
| 长期均线多头 | 新增（Phase 1 趋势代理） | MA20 > MA60 > MA120 + 未有效跌破 MA20 |

> Phase 2 接入 adata 财务数据后补充 ROE / 增速 / 估值分位等真基本面因子（参考 UZI-Skill 的多维框架）。

### 与 Sequoia-X 的差异

| 维度 | Sequoia-X | 本系统 |
|---|---|---|
| 计算方式 | 逐股 Python 循环 | **全市场 SQL 向量化**（快一个数量级） |
| 输出 | 裸代码列表 | 带分值 + 推荐理由的 Candidate |
| 存储 | SQLite | DuckDB + Parquet |
| 选股池 | 全市场 | 全 A 股 + 风险过滤，**保留次新股**（数据不足时降级而非剔除） |
| 策略参数 | 硬编码 | 全部外置到 `settings.yaml` |

---

## 七、市场状态识别

移植 market-breadth 的「宽度」思想（站上均线的股票占比），扩展为五状态：

| 状态 | 判定特征 | 主推档位（已按回测校准） |
|---|---|---|
| 趋势行情 | MA60 上方占比 ≥ 55% + 指数 20 日不跌 | 短线（0.50） |
| 情绪高潮 | 涨停 ≥ 80 家 + 炸板率 ≤ 20% | 短线（0.70） |
| 情绪退潮 | 跌停 ≥ 30 家，或炸板率 ≥ 45%，或涨停家数萎缩且宽度走弱 | 短线（0.55） |
| 震荡行情 | MA20 宽度在 35%~55% 之间 | 短线（0.55） |
| 事件驱动 | 单一板块涨停占比 > 30%（Phase 2 接入板块数据后启用） | 短线（0.60） |

输出**三档权重**，直接调制策略评分的「市场适配分」，实现「先判断环境、再选择策略」。

> ⚠️ **这套权重是实测校准的结果，不是直觉。**
> 最初的配置写的是「趋势行情加码波段（0.7）、情绪退潮防守价值（0.8）」，
> 但 485 个交易日的逐日回放证明这是**反的**：短线档在全部 5 种市场状态下
> T+1 平均收益都最高（趋势期 +1.05% vs 波段 +0.19%；退潮期 +1.69% vs 价值 +0.58%）。
> 校准依据与完整数据见 `config/settings.yaml` 中 `market_regime.tier_weights` 的注释，
> 随时可用 `python scripts\regime_matrix.py` 重新核算。

---

## 七点五、行业与板块强度

A股的收益很大程度由板块效应驱动。本系统接入了行业数据，但**采用了与直觉不同的做法**。

### 关键设计：只从接口拿「个股→行业」映射，板块强度自己算

免费板块接口的实测情况：东财被拦、申万**时好时坏**（同一天先成功 13/31，几分钟后连行业列表都失败）、
新浪稳定但只覆盖 48.9%（不含新股）、同花顺本机版本无成分接口。

接口快照还有两个致命局限：**只有当日、没有历史**，因此无法回测「板块效应是否有效」。

所以：

| 做什么 | 谁来做 | 为什么 |
|---|---|---|
| 个股 → 行业 映射 | 接口多源互补 | 相对静态的维度数据 |
| 板块日度强度 | **本地日线自算** | 可回填历史、可回测、不受接口稳定性影响 |

```bat
python -m astock.cli sector --refresh-primary   :: 切换主用来源后刷新标签
python -m astock.cli sector                     :: 同步映射（约 3 分钟）
python -m astock.cli sector --top 15            :: 顺便看当日板块强度排行
python -m astock.cli sector-strength            :: 由本地日线算板块强度（可回填）
```

### 实测结果

- **映射覆盖率 99.4%**（5523/5559）：申万 87% + 新浪 54%，两者互补；
  **推荐池覆盖率 短线/波段 100%、价值 91.7%**
- 申万成分接口顺带回填了 **4851 条基本面数据**（ROE/净利润增速/营收增速/PE/PB）——
  本来排在后续计划里的价值档数据，顺手就拿到了
- 板块强度回填了 **486 个交易日 × 两套分类体系**

### 一个必须如实指出的发现：板块动量很弱，且集中度到极值时会反转

用 486 天历史自算的板块强度做了两次检验：

**① 板块层面**：强度最高的板块，次日平均 +0.212%；最低的 +0.125%。有正向但差距仅 0.09pp，
且分位单调性不完美。

**② 个股层面**（更关键）：所属板块强度 ≥80 的股票次日 +0.218%，全市场基准 +0.152%，
超额 **+0.066pp**。但各分位的**胜率几乎相同**（48.7%~49.9%）、**中位收益全为 0** ——
这是典型的**赔率型弱信号**，靠极端值拉动，不是胜率型。

**③ 意外反转**：单个板块涨停家数占全市场 **22% 以上**（"事件驱动"状态）时，
该板块**次日平均 −0.10%**，而其它状态下的最强板块都是 +0.17%~+0.28% ——
**集中度到极值更像是分歧前兆，而非主线延续**。

### 因此的三个决策

1. **板块强度不参与打分**，只用于展示与归因（报告里显示"所属板块 + 当日强度"）
2. `event`（事件驱动）状态的权重从 `{short:0.60}` 回调到 `{short:0.50}`，
   因为"虹吸=可追"的假设被数据否定
3. `event` 状态阈值从拍脑袋的 30% **校准为实测的 22%**——原值 486 天里**从未触发过**，
   等于该状态形同虚设；22% 触发 47 天（9.7%），样本足以回测

> 后续若要利用板块信号，更可能有效的方向是「板块强度作为**过滤条件**」
> （剔除板块弱的票）而非加权；或反过来做**板块强度反转**（买弱卖强），
> 但这需要回测验证，见 TODO。

---

## 七点六、涨停池与情绪数据（同花顺源）

这是短线情绪档的核心数据——**涨停题材、封单金额、连板高度、首次涨停时间**，
免费源（akshare/adata/baostock）完全没有。

```bat
python -m astock.cli limit-pool                    :: 增量采集（补到最新交易日）
python -m astock.cli limit-pool --years 1          :: 回填最近一年（约 35 分钟）
python -m astock.cli limit-pool --date 2026-09-22  :: 采集指定日期
python -m astock.cli limit-pool --show 15          :: 顺便打印最新涨停板
python scripts\probe_hithink.py                    :: 连通性与字段诊断
```

数据落在 `dwd_limit_up`（涨停池明细）与 `dwd_limit_break`（炸板池明细）。
`daily` 会自动顺带采集当天（2 次请求，约 9 秒），无需手动跑。

### 实际数据长什么样

```
涨停板 2026-09-22（按封单额前 10）　连板分布：6板×1、5板×1、4板×1、3板×4、2板×17、1板×39
代码      名称        时间     连板    题材                           封单(亿)
600825  新华传媒      09:25  2连板   资产重组+图书发行+上海国资+"华"字辈       21.46
600664  哈药股份      09:31  2连板   创新药+医药工业+业绩增长               3.60
001216  华瓷股份      09:25  6连板   氧化锆粉体+MLCC验证+日用陶瓷+"华"字      2.59
```

注意「**华**字辈」出现在 5 只票的题材里——典型的 A 股字辈炒作联动，
申万行业分类永远看不出这种关系，题材字段直接捕捉到了。

### 两个硬约束与一个口径差异

1. **限流 15 次/分钟**（响应头 `X-RateLimit-Limit`）——采集器固定 4.2 秒间隔，
   绝不能用它拉全市场日线（5559 只 ≈ 6 小时，且已有更稳来源）
2. **连板天梯接口不采集**——它是 30 天滚动窗口且每板位截断 4 只，数据不完整；
   连板分布从 `dwd_limit_up.boards` 聚合即可
3. **口径差异**：官方涨停池 63 只 vs 我们自算 72 只（`pct_chg>=9.8` 近似会多算）。
   后续市场状态识别应改用官方口径（见 TODO）

---

## 八、评分与复盘

### 融合公式

```
最终分 = 0.45 × 策略强度 + 0.40 × 因子分 + 0.15 × 市场适配 − 风险扣分
```

- **策略强度**：条件满足程度（连续变量，不是布尔），0~100
- **因子分**：量能 30% + 相对强度 30% + 动量 25% + 位置 15%，全部做全市场分位数标准化
- **市场适配**：该档权重 / 最大权重 × 100
- **风险扣分**：ST、流动性不足、波动过大、60 日高位追高、次新股风险

每只候选都保存**评分拆解**，用于回答「为什么推荐它」。

### 复盘口径

- 推荐在数据日盘后生成，计划在**次一交易日开盘买入**（贴合人工交易场景）
- 记录：当日收益、最大涨幅、最大回撤、持有 3 日收益
- 结果标记 win / loss / flat，按档位与策略分别统计胜率

---

## 九、AI 与 Token 成本控制

参考 caveman 的压缩思路，落地为六条规则（见 `astock/llm/`）：

1. 只对当日 Top-N 候选调用 LLM，全市场扫描全在本地完成
2. 输入只传「已算好的指标」，不传原始 K 线序列（输入 token 降幅最大的一环）
3. 强制 JSON 结构化短输出，禁止自然语言段落
4. 每个字段长度硬限制，禁止铺垫与总结句
5. 每日 token 预算护栏，超限自动降级为本地模板报告
6. 消耗量落库 `sys_llm_usage`，可随时审计

**默认关闭 LLM**，纯本地模板也能生成完整报告。启用步骤：

1. 复制 `.env.example` 为 `.env`，填入 `DEEPSEEK_API_KEY`
2. `.env` 中设 `LLM_ENABLED=true`
3. 运行 `python -m astock.cli daily --llm`

预估成本：每日 15 只候选 × 约 1.8K token ≈ **0.05~0.1 元/天**。

---

## 十、定时任务与备份

以管理员身份运行：

```bat
scripts\register_task.bat
```

会注册两个任务：

| 任务名 | 时间 | 作用 |
|---|---|---|
| `AStockAI_Daily` | 每交易日 18:30 | 每日全流程（补数 → 因子 → 市场状态 → 选股 → AI 报告 → 复盘） |
| `AStockAI_Backup` | 每周日 20:00 | 数据备份 |

**为什么是 18:30**：配置项 `data.data_ready_time`（默认 `16:00`）规定了当日行情视为「完整可用」的时间点。
早于该时间运行 `daily` / `backfill` 时，系统**只处理到上一个交易日** —— 因为收盘前拉到的 K 线不是完整日线，
而一旦入库就会成为「本地最新日期」游标，后续补数会直接跳过，**错误数据会被永久保留**。

**为什么不用 `schtasks` 命令行**：`schtasks /Create` 无法设置 `StartWhenAvailable`（错过计划时间后补跑）。
本机不保证 24 小时开机，18:30 若关机或休眠，任务会被直接跳过、当天什么都没有。
`register_task.ps1` 用 `New-ScheduledTaskSettingsSet -StartWhenAvailable` 解决这个问题，
并同时设置「使用电池也运行」「不因切换电源状态而停止」「超时 3 小时」「不重复叠加实例」。

定时任务失败时，`run_daily.bat` 会：
1. 把 Python 的退出码**透传出去**（否则任务计划程序永远显示「上次结果 0」，失败被静默吞掉）；
2. 在 `data/logs/LAST_DAILY_FAILED.txt` 留下标记，`python scripts\monitor.py` 会把它顶到最上方显示。

### 数据备份

```bat
scripts\backup_data.bat                          :: 备份到 data\backups\
scripts\backup_data.bat --dest E:\astock_backup   :: 备份到另一个盘（推荐）
scripts\backup_data.bat --list                    :: 查看已有备份
```

备份内容：主库 `astock.duckdb`、`skills/`、`config/settings.yaml`、`data/reports/`、`data/backtest/`、`data/serving/`。
**刻意排除** `.env`（含 API Key，不随备份扩散）与 `data/logs/`（体积大、价值低）。

两个关键设计：

1. **主库被占用时拒绝备份**。DuckDB 是单文件存储，有写进程在跑时直接复制会得到
   「看似完整、实际损坏」的副本 —— 这种备份比没有备份更危险，因为它只会在真正需要恢复时才暴露问题。
2. **备份后校验副本**。会打开副本查一次日线行数并与源库比对，不一致就删除本次备份并报错。
   不做校验的话，备份可能长期是个坏文件而无人知晓。

> ⚠️ 默认备份到 `data\backups\`，与主库**同一块磁盘**，只能防误删、防不了磁盘损坏。
> 定期加 `--dest` 指到移动硬盘或网盘才是真的防灾。

---

## 十一、常见问题

**Q: 全市场回填太慢？**
加大并发：`python -m astock.cli backfill --workers 8`。或分批跑，中断可续传。

**Q: 提示某只股票无数据，但它是正常股票？**
用 `python -m astock.cli check` 查看。若属于「仍显示上市但无数据」，用 `backfill --retry-empty` 重试。

**Q: 为什么短线档经常没有候选？**
短线策略条件较严（需要昨日涨停 + 今日洗盘），在震荡/退潮环境下本就极少命中。若市场状态不是「情绪高潮」，系统也会主动降低短线档权重。

**Q: 能改成自动下单吗？**
本系统定位为「AI 研究员 + 多策略选股 + 复盘进化」，不含交易执行，避免误操作风险。

**Q: 想换 PostgreSQL？**
只需替换 `astock/storage/db.py` 中的 `Storage` 实现，业务层不接触数据库 API，无需改动其它代码。

---

## 十二、数据源限流与恢复（实测踩坑记录）

这几种情况在开发过程中都真实发生过，遇到时按此处理：

### 0. 数据源自动降级（已内置，通常无需人工干预）

`config/settings.yaml`：

```yaml
data:
  primary_source: baostock
  source_fallback: [baostock, akshare]   # 主源不可用时按顺序自动尝试
```

主源登录失败时会自动切到备用源并打日志：

```
WARNING | 数据源 baostock 不可用：baostock 登录失败，请检查网络
WARNING | 已自动切换到备用数据源：akshare
```

akshare 通道内部还有三层降级（`astock/data/sources/akshare_source.py`）：

| 顺序 | 站点 | 实测表现 |
|---|---|---|
| 1 | 新浪 `stock_zh_a_daily` | **最稳**，约 0.5 秒/只、可拉满 2 年 |
| 2 | 腾讯 `stock_zh_a_hist_tx` | 可用，约 1.3 秒/只 |
| 3 | 东财 `stock_zh_a_hist` | 字段最全，但限流时直接断连 |

> 注意：新浪/腾讯**不提供昨收**，代码会用前一行收盘价推算 `preclose` 与 `pct_chg`，
> 否则因子层的涨停判定与市场状态的涨跌家数都无法计算。
> 新浪的 `turnover` 是**小数比例**（0.00196），已统一乘以 100 转成百分数。

排查命令：

```bat
python scripts\probe_alt_source.py   :: 各替代通道当前是否可用
```

### 1. baostock 返回「黑名单用户」

```
login failed!
baostock 登录失败: 黑名单用户，请与管理员联系
```

**原因**：早期版本在并行采集时，每采一只股票就 `login` + `logout` 一次。
800 只股票 = 1600 次登录请求，服务端直接封禁。

**已修复**：每个 worker 进程复用同一条连接（每进程只登录一次），
并发从 6 降到 4，批次之间加 5 秒冷却。

**处理**：
- 黑名单通常是临时的，等待一段时间即可恢复
- 期间可用其他源继续：`python -m astock.cli backfill --source akshare`
- 恢复后正常重跑 `scripts\run_backfill.bat`（自动续传）

### 2. akshare / adata 报 ProxyError

```
ProxyError: Unable to connect to proxy
```

**原因**：本机启用了系统代理（如 `127.0.0.1:7897`），
`requests` 会自动走代理，而 baostock 使用原始 socket 不受影响。

**处理**：`config/settings.yaml` 中已默认开启直连：

```yaml
data:
  bypass_proxy: true
```

### 3. 采集报「整批 100% 失败」

说明数据源已限流。脚本会自动中止并打印提示，**不会**把失败的股票标记成「无数据」，
因此限流恢复后重跑即可补齐，不会漏数据。

排查工具：

```bat
python scripts\probe_network.py       # 各行情站点是否可达
python scripts\probe_source.py        # 各数据源能否正常取数
python scripts\monitor.py             # 后台任务进度与存活状态
python -m astock.cli check            # 区分「真退市」与「采集失败」
```

---

## 十三、样本外验证（回测）

用来回答一个关键问题：**这些策略到底赚不赚钱，还是只是看起来合理？**

```bat
scripts\run_backtest.bat                    :: 全区间回放
scripts\run_backtest.bat --months 6         :: 只回放最近 6 个月
scripts\run_backtest.bat --tiers swing      :: 只验证波段档
scripts\run_backtest.bat --list             :: 查看历史回测运行
```

等价命令：`python -m astock.cli backtest --months 6 --top-n 5`

### 回测口径（决定了结果是否可信）

| 环节 | 处理方式 | 为什么这样做 |
|---|---|---|
| 选股数据 | 只用当日及之前的数据 | 因子表按日存储，天然无未来函数 |
| 买入价 | **次一交易日开盘价** | 用当日收盘价买入是做不到的 |
| 股票池 | 按当日截面过滤 | 用今天的成交额门槛筛两年前的股票是作弊 |
| 退市股 | 按当时 `out_date` 判断 | 用「今天还活着」反推会产生**幸存者偏差** |
| 市场状态 | 取当日快照 | 权重也必须 as-of |
| 稳定性 | 切分观察期 / 验证期 | 识别「前半段灵、后半段废」的伪参数 |

### 输出内容

- **整体表现**：T+1 胜率、平均收益、盈亏比、持有 3/5/10 日收益、最大涨幅/回撤
- **分档位 / 分策略**：定位哪些策略真的有效、哪些该淘汰
- **分市场状态**：验证「趋势行情推波段、情绪高潮推短线」是否成立
- **多策略共振**：验证「多策略同时命中更好」这个直觉是不是伪信号
- **观察期 vs 验证期 / 按月**：判断参数是稳健还是偶然
- **自动结论**：直接给出「有效策略 / 无效策略 / 样本不足」清单和调参建议

产物落在 `data/backtest/`：

| 文件 | 内容 |
|---|---|
| `backtest_<run_id>.md` | 完整报告（含结论与建议） |
| `backtest_<run_id>.csv` | 每条信号的明细，可自行用 Excel 做二次分析 |

### 调参原则

1. 先看**分策略**的平均收益与盈亏比，不要只看总胜率
2. 只有当某条参数在**所有子区间**都为正时，才值得改 `config/settings.yaml`
3. 样本数 < 30 的策略不下结论，先补数据
4. 报告会自动标注「次日冲高回落」特征（T+1 为正、3 日为负），据此决定持有时长

---

## 十四、策略 Skill 库

把每个策略落成**独立目录**，形成可持续迭代的策略资产（目录规范参考 UZI-Skill）。

```bat
python -m astock.cli skills list              :: 列出全部策略与判定
python -m astock.cli skills show turtle_trade :: 查看单个策略完整档案
python -m astock.cli skills sync              :: 重新落盘（含参数变更记版本）
python -m astock.cli skills stats             :: 刷新历史成功率
```

### 目录结构

```
skills/
├── README.md                 索引（人读，含判定汇总表）
├── registry.json             索引（程序读：前端 / API / Agent 都能消费）
└── turtle_trade/
    ├── SKILL.md              策略档案（YAML frontmatter + 条件 + 表现 + 失效场景）
    ├── meta.yaml             机器可读元数据 + 参数快照（用于版本 diff）
    ├── performance.json      历史成功率（回测 + 实盘跟踪）
    └── references/
        ├── conditions.md     条件详解与设计依据
        └── changelog.md      参数版本演进 —— 改参数自动追加
```

### 每个 Skill 记录什么

| 内容 | 说明 |
|---|---|
| 一句话定位 + 设计逻辑 | **为什么这样设计**，而不只是条件罗列 |
| 判定条件 | 逐条列出，与代码一一对应 |
| 参数与来源 | 指向 `config/settings.yaml` 的具体位置 |
| 适用 / 规避的市场状态 | 与市场状态识别联动，回测会验证这个假设是否成立 |
| **已知失效场景** | 经验沉淀（如「退潮期的集体跌停不是错杀」），最有价值的部分 |
| 历史成功率 | 样本外回测 + 实盘纸面跟踪，**两套口径分开记录** |
| 移植来源 | 来自哪个开源项目的哪个文件 |

### 参数变更自动记版本（策略进化的审计链）

`meta.yaml` 保存参数快照，每次 `skills sync` 会做 diff：

```
## v1.0.0 · 2026-09-22 19:11
- 首次建档

## v1.1.0 · 2026-09-22 19:11
- 参数变更：`breakout_window`: 20 → 22
- 变更后表现：样本 1695，胜率 46.4%，T+1 平均 0.21%
```

### 调参流程（闭环）

1. 看 `skills list` 或 `performance.json`，找出判定为 ❌ 无效 / 🟡 观察 的策略
2. 改 `config/settings.yaml` 中对应参数
3. `python -m astock.cli skills sync` → 自动升版本并写入 changelog
4. `python -m astock.cli backtest` → 回测结果自动回填到 `performance.json`
5. 对比 changelog 里的「变更前 / 变更后表现」，决定保留还是回滚

> 判定门槛：样本 ≥ 30 条才算数；胜率 ≥ 50% 且平均收益 > 0 记 ✅ 有效；
> 平均收益 ≤ 0 记 ❌ 无效；收益为正但胜率偏低记 🟡 观察（低胜率高赔率型）。

### API

```text
GET /api/skills               → registry.json（策略索引与判定）
GET /api/skills/{name}        → SKILL.md + meta + performance + references
```

这两个接口**只读文件、不查库**，因此采集/回测期间也能正常访问（与展示层快照同一思路）。

---

## 十五、后续路线

| 阶段 | 内容 |
|---|---|
| **Phase 2** | adata 财务/资金/概念数据接入；价值档完整实现；Skill 库落盘与历史成功率统计；自动复盘归因；飞书推送 |
| **Phase 3** | LightGBM 排序模型替代规则打分；市场状态识别升级为模型；参数自动优化与低效策略淘汰；Vue3 完整界面；迁移 PostgreSQL + TimescaleDB |
