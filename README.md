# A股 AI 选股系统

**一个 A 股短线候选的「纸面跟踪观察系统」**：每日自动产出影子信号候选 → 结算历史成绩 → LLM 归因复盘 → 只读网页呈现全部证据。

> ### ⚠️ 先看当前状态
>
> - **唯一候选来源**：影子信号（涨停基因 + 缩量不破位 + 排除当日封板 + 流动性下限）
> - **它在可实现口径下通过检验了吗：没有。** 240 个交易日累计 **−24.85%**（同池等权基准 −40.72%），日超额 **+0.114%、t=0.96**（不显著）
> - 原来的主链路（8 个策略 + 评分 + 配额）经 250 个交易日样本外体检**全部无可实现 alpha**，已于 2026-10-08 **整体删除**
> - 因此本系统的准确定性是 **纸面跟踪观察期**，不是 alpha 引擎。所有输出均为纸面记录，**不构成投资建议**
> - 契约：v1.0 冻结期（至 2026-12-31）—— 除修 bug / 数据源应急 / 风险类硬约束外，**不改任何东西**

---

## 三分钟跑起来（不需要任何 API key）

```bat
git clone https://github.com/yyds-z/star_astock_cxd.git
cd star_astock_cxd
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt

:: 从 Releases 下载 serving-v1.0.tar.gz，把解压出的 8 个 parquet + meta.json
:: 放进 data\serving\（仓库本身不含任何数据）

python -m astock.cli serve
```

打开 <http://127.0.0.1:8000> —— 应看到操作台 **29 只候选**、影子复盘（超额 **−0.514%**）、净值曲线（影子 **−24.85%** vs 基准 **−40.72%**）。

## 完整文档

📘 **[`A股AI选股系统_系统全案与复现手册.md`](A股AI选股系统_系统全案与复现手册.md)** —— **唯一完整源**，包含：

| 章节 | 内容 |
|---|---|
| 第 0 章 | 当前状态（含"未通过检验"的完整结论）与系统边界 |
| 第 1.2 章 | 复现三档：最小验证（47 MB）/ 零数据重建（自己的 key）/ 完整状态（988 MB 库） |
| 第 3 章 | 架构：目录、数据流、16 张表、15 个命令、三层隔离设计 |
| 第 4 章 | 当前设计：采集 / 信号 / 复盘 / 展示的取舍 |
| 第 5 章 | 成果与教训：**已证伪清单**（哪些做法被证明无效、为什么删）+ 11 条工程坑 |
| 附录 A | 给 AI 续做：红线约束与已知失效项 |

## 项目结构

```
astock/shadow/    信号层（唯一决策来源）：信号引擎 + 复盘归因
astock/data/      采集层：同花顺（日线/涨停池/快照）+ akshare（历史回填）
astock/market/    环境层：市场宽度/涨停家数（仅展示，不参与选股）
astock/storage/   存储：DuckDB 写入 + Parquet 快照（展示层隔离）
astock/report/    报告：三段式 Markdown
api/ + web/       展示：FastAPI（只读快照）+ 单文件前端
```

**关键架构决策**：写入层（DuckDB 独占锁）→ 快照层（`data/serving/*.parquet`，原子替换）→ 展示层（只读 Parquet）。
展示层**绝不打开主库** —— 否则网页一开就把主库锁死，`daily` 直接失败。

## 日常命令

```bat
python -m astock.cli daily              :: 每日全流程（18:30 计划任务）
python -m astock.cli status             :: 数据状态（--snapshot 不抢锁）
python -m astock.cli shadow status      :: 影子信号成绩
python scripts\run_tests.py             :: 全部测试（4 个套件）
scripts\run_serve.bat                   :: 启动网页
```

## License

见 [LICENSE](LICENSE)。
