# 数据来源与复现指南

> **本仓库只含代码与配置。** 所有数据（`data/` 实测约 4.6 GB：主库 1.4 GB、只读副本、备份、
> 缓存、Parquet 快照、报告、日志）均不入版本库，由本文件说明**数据从哪来、如何从零重建、
> 以及哪些数据无法事后补**。
>
> 复现所需的一切都在仓库内：`requirements.txt`（依赖）、`config/*.yaml`（口径与阈值）、
> `astock/storage/schema.sql`（22 张表结构）、`.env.example`（密钥模板）。
>
> **2026-09-30 清理**：删除了 5 张表（3 张研究产物 + 空表 `dwd_auction` + 已弃用的
> `dim_stock_finance`）与对应的脚本/模块；被删表的定义与结论可按 git 历史取回。

---

## 一、外部数据源（只有两个）

### 1. 同花顺金融数据服务（主源，需 API Key）

| 项 | 说明 |
|---|---|
| 入口 | `https://fuyao.aicubes.cn` |
| 认证 | HTTP 头 `X-api-key` |
| 环境变量 | `HITHINK_FINANCE_API_KEY` |
| 限流 | **15 次/分钟**（客户端固定间隔 4.2 秒，见 `astock/data/hithink.py`） |
| 特性 | 支持按 `date_ms` 查历史；池子类接口实测上游保留约 **1 年** |

| 接口 | 落表 | 说明 |
|---|---|---|
| `/api/a-share/export/daily-k-10d`（10 年全量）/ `daily-k` | `dwd_daily_bar` | **全市场日 K 整库导出**，1 次请求（不占 15/分钟限流），未复权。这是日线的唯一来源 |
| `/api/a-share/special-data/limit-up-pool` | `dwd_limit_up` | 涨停池：封单额、首封时间、连板高度、涨停原因 |
| `/api/a-share/special-data/limit-break-pool` | `dwd_limit_break` | 炸板池：开板次数等 |
| `/api/a-share/special-data/dragon-tiger-list` | `dwd_dragon_tiger` | 龙虎榜（固定全量、不分页，1 次请求/天） |
| `/api/a-share/auction/snapshot` | （原 `dwd_auction`，2026-09-30 移除） | 集合竞价（单次上限 100 只）。接口包装仍在 `HithinkCollector.auction_snapshot()` |
| `/api/a-share/prices/snapshot` | `dwd_intraday_snapshot` | **盘中快照**：现价/累计量额（单批 500 只可用） |
| `/api/a-share/financials/{income,balance,cash-flow}-statements` | `dws_finance_metrics` | 财务三表与指标（**单只查询**，每只 3 次请求） |

### 2. akshare（免费，只负责「基础三件套」）

| 接口 | 落表 | 为什么不用同花顺 |
|---|---|---|
| `list_stocks` | `dim_stock` | 同花顺无 `/stock/list`（探测 404） |
| 交易日历 | `trade_calendar` | 同花顺无 `/calendar/trade-dates`（404） |
| `fetch_index_daily` | `dwd_index_bar` | 同花顺无指数端点（404） |

三者都是**低频**（每天各 1 次），不构成依赖负担。配置见 `settings.yaml` 的 `data.primary_source`。

> **已下线的源**：`baostock`（socket 连接脆弱、易被限流拉黑）、`adata`（几乎未使用）。
> `data.source_fallback` 为空 —— 日线唯一来源是同花顺导出。

### 3. 本地派生（不需要任何外部数据）

`dws_feature`、`dws_sector_strength`、`dws_market_regime`、`dws_finance_metrics`
（由财务三表算）、以及全部 `ads_*`（决策与回测输出）。
`dim_industry` / `dim_stock_industry`（行业映射）由 `sector` 命令从免费源同步
（覆盖率约 91.5%）。

> 2026-09-30 移除的派生表：`dws_limit_factor`、`dws_dragon_factor`、`dws_style_matrix`
> —— 它们是研究产物（结论已固化进配置注释），无生产代码读取。

---

## 二、从零复现（首次建库）

```bat
:: 0) 环境
conda create -n astock python=3.11 -y
conda activate astock
pip install -r requirements.txt

:: 1) 密钥：复制模板并填写（.env 已被 .gitignore 排除，不会入库）
copy .env.example .env
::    DEEPSEEK_API_KEY=<你的 DeepSeek Key>        （AI 报告与复盘归因；不填则用本地模板）
::    HITHINK_FINANCE_API_KEY=<你的同花顺 Key>    （必需 —— 日线/池子/财务/快照全靠它）

:: 2) 建表（22 张表，来自 astock/storage/schema.sql）
python -m astock.cli initdb

:: 3) 全历史日线（★★ 推荐路径：同花顺 10 年整库导出，分钟级）
python -m astock.cli dump-daily --days 3650
::    说明：--days <=10 走最近 10 日导出；>10 自动改用 10 年全量导出。
::    替代路径 `backfill` 是「逐只拉取」（现走 akshare，5000+ 只、小时级），
::    仅在需要按代码补特定区间时使用：python -m astock.cli backfill --years 2 --codes 600519,000001

:: 4) 指数日线（akshare；市场状态识别的动量维度需要）
python -m astock.cli index --years 2

:: 5) 涨停池 / 炸板池（同花顺，限流 15 次/分钟，1 年约 17 分钟）
python -m astock.cli limit-pool --years 1

:: 6) 龙虎榜（同花顺，1 次请求/天）
python -m astock.cli dragon-tiger --years 1

:: 7) 行业映射 + 板块强度（板块强度由本地日线自算）
python -m astock.cli sector
python -m astock.cli sector-strength --all

:: 8) 财务（价值档输入；全市场约 17 小时，可随时中断续传，建议夜间跑）
python -m astock.cli finance --all

:: 9) 派生表
python -m astock.cli factor          :: 因子表 dws_feature
python -m astock.cli regime          :: 市场状态 dws_market_regime

:: 10) 体检
python -m astock.cli check
```

### 自检：影子信号应复现出检验结论

```bat
python -m astock.cli shadow backfill --from 2025-09-24 --to 2026-09-22
python -m astock.cli shadow settle
python -m astock.cli shadow status
```

预期（`dwd_limit_up` 覆盖期内约 240 个交易日）：

```
样本              ≈ 9187 笔
次日涨停率        ≈ 9.2%（全市场基准 2.05%，约 4.5 倍）
次日 / 3日收益    ≈ +0.55% / +0.90%（可实现口径）
日度聚类超额      ≈ +0.667%（t ≈ 5.6）
```

---

## 三、日常运行（每交易日）

| 时间 | 命令 | 作用 |
|---|---|---|
| 14:00 | `python -m astock.cli snapshot --slot 14:00` | 采集盘中快照 → 关联进报告实况 |
| 18:30 | `python -m astock.cli daily` | 全流程：补数 → 因子 → 板块 → 状态 → 选股 → **影子信号** → 报告 → 复盘 |

注册 Windows 计划任务（**需管理员权限**）：`scripts\register_task.bat`
（注册 `AStockAI_Snapshot` 14:00 与 `AStockAI_Daily` 18:30 两个任务）

---

## 四、⚠️ 无法事后补的数据（复现时必须知道）

| 数据 | 原因 | 后果 |
|---|---|---|
| `dwd_intraday_snapshot` | 免费源与同花顺都**不提供历史盘中截面** | 只能从开始记录那天起积累。在上述积累足够之前，任何「盘中决策」的回测都不可信 |
| 集合竞价（原 `dwd_auction`） | 同上（竞价快照只在盘前采集） | 历史竞价数据不可回补。表已于 2026-09-30 移除（当时 0 行） |

**这是唯一有时效性的约束**：晚一天开始采集，就永远少一天可回测的样本。

---

## 五、历史深度限制

| 数据 | 深度 | 影响 |
|---|---|---|
| 日线（dump） | 支持 10 年（`--days > 10` 切换全量） | 回测区间基本不受限 |
| 涨停池 / 炸板池 / 龙虎榜 | 上游约 **1 年** | 依赖「涨停基因」的信号样本上限约 1 年 → **无跨周期检验**，这也是当前冻结期要积累它的原因 |

---

## 六、口径与已知陷阱（都是实际踩过的）

1. **日线未复权**：dump 不支持复权参数，`dwd_daily_bar` 全为未复权价。
   - 用 `close / preclose` 判断涨停是**正确**的（交易所发布的前收已除权）；
   - 但 `LEAD(close)/open` 这类跨除权日的收益会**低估**（把除权缺口当亏损），量级约 0.01~0.02pp。
2. **回测裁判口径**：唯一可用的裁判是 `ret_exit_d1c`（买入次日收盘卖）。
   `ret1`（买入当天 `close/open - 1`）在 A 股 **T+1 下不可实现**，仅作诊断字段保留
   —— 用它得出的任何结论都会系统性偏乐观。
3. **盘中量比必须折算**：14:00 时成交量只累积约 3.5 小时，直接与前 5 日**全日**均量比会偏小。
   折算公式 `量比 = 累计量 / (前5日全日均量 × 已开盘分钟/240)`，**必须扣掉午休 11:30–13:00**。
   A 股成交呈 U 形 → 早盘折算高估约 60%，14:00 约 6%。
4. **同花顺快照/竞价只接受存续股票**：混入已退市代码会让**整批**失败，报错是
   `code=1002 / Unknown thscode: xxx`（看起来像"数量超限"）。代码里统一从
   `dim_stock WHERE out_date IS NULL` 取代码。
5. **`dim_stock.out_date` 不可被备用源覆盖**：备用源把退市日写成空，一旦覆盖会让
   **幸存者偏差防护静默失效**（已在 `collector.sync_stock_list` 修为「源空则沿用库值」）。

---

## 七、表清单速查（28 张）

| 层 | 表 |
|---|---|
| dim | `dim_stock` `trade_calendar` `dim_industry` `dim_stock_industry` |
| dwd | `dwd_daily_bar` `dwd_index_bar` `dwd_limit_up` `dwd_limit_break` `dwd_dragon_tiger` `dwd_intraday_snapshot` |
| dws | `dws_feature` `dws_finance_metrics` `dws_market_regime` `dws_sector_strength` |
| ads | `ads_recommend` `ads_review` `ads_review_attribution` `ads_shadow_pick` `ads_backtest` |
| sys | `sys_collect_state` `sys_llm_usage` `sys_strategy_stats` |
