-- ============================================================
-- astock_ai DuckDB 表结构
-- 分层：dim(维度) / dwd(明细) / dws(汇总) / ads(应用) / sys(系统)
-- 所有表带主键，保证 INSERT OR REPLACE 幂等写入
-- ============================================================

-- ---------- 维度层 ----------
CREATE TABLE IF NOT EXISTS dim_stock (
    code        VARCHAR PRIMARY KEY,
    name        VARCHAR,
    exchange    VARCHAR,        -- sh / sz / bj
    board       VARCHAR,        -- main / gem(创业板) / star(科创板) / bj(北交所)
    ipo_date    DATE,
    out_date    DATE,
    status      INTEGER,        -- 1=上市 0=退市
    is_st       BOOLEAN DEFAULT FALSE,
    updated_at  TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trade_calendar (
    date    DATE PRIMARY KEY,
    is_open BOOLEAN
);

-- ---------- 维度层：行业/板块 ----------
-- 为什么需要：A股的收益很大程度由板块效应驱动，但免费源的板块接口都不稳定
-- （东财被拦、申万时好时坏、新浪只覆盖约一半股票）。
-- 因此这里只把「个股 → 行业」这个**相对静态的映射**落成维度表，
-- 板块强度则由本地日线自行计算（见 dws_sector_strength），
-- 好处是：可回填历史、可回测「板块效应是否真的有效」、不依赖接口可用性。
CREATE TABLE IF NOT EXISTS dim_industry (
    industry_id   VARCHAR PRIMARY KEY,   -- 稳定 ID，如 sina:new_blhy / sw:801010
    name          VARCHAR,
    source        VARCHAR,              -- sina / sw
    level         INTEGER DEFAULT 1,    -- 1=一级 2=二级 3=三级
    parent        VARCHAR,              -- 上级行业名
    stock_count   INTEGER,
    updated_at    TIMESTAMP
);

-- 一个代码可以有多个来源的标签（source 区分），is_primary 标记主用标签。
-- PRIMARY KEY (code, source) 保证同一来源下幂等。
CREATE TABLE IF NOT EXISTS dim_stock_industry (
    code          VARCHAR,
    industry_name VARCHAR,
    source        VARCHAR,
    is_primary    BOOLEAN DEFAULT TRUE,
    updated_at    TIMESTAMP,
    PRIMARY KEY (code, source)
);

-- 【已删除】dim_stock_finance（2026-09-30）
-- 原为「申万成分接口顺手带的快照类基本面」（roe/pe/pb/增速），早已被
-- 同花顺财务三表 → dws_finance_metrics 取代，纯冗余，因此连同表一并移除。

-- ---------- 明细层：涨停/炸板池（来源：同花顺 Financial-API）----------
-- 这是短线情绪档的核心数据，免费源（akshare）完全没有：
-- 涨停题材、封单金额、连板高度、首次涨停时间都是打板策略的关键输入。
-- 接口按交易日查询（date_ms），支持历史回填；限流 15 次/分钟，
-- 因此只做每日一次的低频采集，绝不用于全市场行情。
-- 连板天梯接口不采集：它是 30 天滚动窗口且每板位截断 4 只，数据不完整，
-- 连板分布可直接从 dwd_limit_up 的 boards 字段聚合得到。
CREATE TABLE IF NOT EXISTS dwd_limit_up (
    date            DATE,
    code            VARCHAR,
    name            VARCHAR,
    first_time      VARCHAR,     -- 首次涨停时间 HH:MM（越早越强）
    reason          VARCHAR,     -- 涨停题材（上游原文，可能为空）
    boards          INTEGER,     -- 连板数（1=首板）
    board_text      VARCHAR,     -- 连板文本（首板 / 5天4板）
    seal_money      DOUBLE,      -- 收盘封单额(元)
    max_seal_money  DOUBLE,      -- 盘中峰值封单额(元)
    price           DOUBLE,
    pct_chg         DOUBLE,
    is_st           BOOLEAN,
    is_new          BOOLEAN,     -- 未开板新股
    updated_at      TIMESTAMP,
    PRIMARY KEY (date, code)
);

CREATE TABLE IF NOT EXISTS dwd_limit_break (
    date            DATE,
    code            VARCHAR,
    name            VARCHAR,
    open_times      INTEGER,     -- 盘中开板次数（越多说明分歧越大）
    price           DOUBLE,
    pct_chg         DOUBLE,
    turnover_ratio  DOUBLE,      -- 换手率(%)
    turnover        DOUBLE,      -- 成交额(元)
    updated_at      TIMESTAMP,
    PRIMARY KEY (date, code)
);

-- ---------- 明细层：龙虎榜（来源：同花顺 Financial-API）----------
-- ⚠️ **已停用采集（2026-10-08）**：全代码库零消费者（原消费方已在 2026-09-30
--    清理时删除），每天仍消耗 1 次 API 请求写一张无人读的表。
--    表定义与历史数据保留为归档；确认不再需要时可 DROP TABLE。
--    详见 config/data_registry.yaml 的 deprecated_note。
--
-- 短线资金面的关键补充：游资/机构席位的净买入方向，是免费源完全没有的信息。
-- 接口固定返回全量、不分页，每天 1 次请求即可（比涨停池更便宜）。
--
-- range_days 必须进主键：同一只股票可能同时出现在「当日榜」和「3 日榜」，
-- 这是两条口径不同的记录，若只用 (date, code) 会互相覆盖、丢失信息。
CREATE TABLE IF NOT EXISTS dwd_dragon_tiger (
    date                 DATE,
    code                 VARCHAR,
    name                 VARCHAR,
    range_days           INTEGER,   -- 1=当日榜 3=3 日榜
    net_value            DOUBLE,    -- 龙虎榜净买入额（元）
    net_rate             DOUBLE,    -- 净买入占比（小数）
    buy_value            DOUBLE,    -- 买方金额（元）
    sell_value           DOUBLE,    -- 卖方金额（元）
    amount               DOUBLE,    -- 成交额（元）
    hot_rank             INTEGER,   -- 同花顺人气排名（越小越靠前）
    org_net_value        DOUBLE,    -- 机构净买入（元）
    org_net_rate         DOUBLE,
    org_buy_num          INTEGER,   -- 买入机构数
    org_sell_num         INTEGER,   -- 卖出机构数
    hot_money_net_value  DOUBLE,    -- 游资合计净买入（元）
    limit_reason         VARCHAR,   -- 涨跌停原因
    updated_at           TIMESTAMP,
    PRIMARY KEY (date, code, range_days)
);

-- 【已删除】dwd_auction（2026-09-30）
-- 集合竞价快照表自建库以来**从未写入过任何一行**（0 行），对应的 `auction` 命令
-- 与同花顺 `auction/snapshot` 接口保留但表已移除 —— 决策链路从未读取它，
-- 属零使用数据。若将来要做「竞价类影子信号」，按 git 历史还原本表定义即可。

-- ---------- 汇总层：财务指标（来源：同花顺 Financial-API）----------
-- 解决价值档「只有技术面、没有基本面」的问题：原价值档仅用 MA20>MA60>MA120
-- 做趋势代理，实测在所有市场状态下都是最弱档。
--
-- 存**比率**而非原始金额：ROE / 资产负债率 / 同比增速 才是可跨公司比较的量，
-- 原始金额受规模影响无法直接用于选股。同比在此处一次算完（需要用到上一期数据）。
-- 注意：本表是**多期历史**（按 period_end 区分），因此天然支持按报告期做
-- 时点回测（避免用未来财报选股的前视偏差）。
--
-- ⚠️ 口径陷阱（下游务必遵守）：A 股季报是**累计口径** ——
-- Q1=3个月、Q2=6个月、Q3=9个月、Q4=12个月。
-- 因此 roe / revenue / net_profit 这类**流量指标跨报告期不可直接比较**
-- （拿 Q1 的 9.7% ROE 去比 Q4 的 32.4% 是错的）。
-- 可比的只有：
--   1. 同比列（revenue_yoy / profit_yoy）—— 已在同 fiscal_period 内计算，可直接用；
--   2. 同 fiscal_period 的不同年份之间比较。
-- 若要做跨公司横向对比，请统一取 `fiscal_period = 'FY'`（年报）。
--
-- ⚠️ 回测必须用 `report_date`（披露日）过滤，不能用 `period_end`：
-- 报告期末只是会计区间终点，财报实际公开要晚 1~4 个月。
-- 按 period_end 取数等于用了当时还不存在的信息，会把历史业绩虚高到无法复现。
-- 例：2026 中报 period_end=2026-06-30，但 report_date 可能是 2026-08-29。
CREATE TABLE IF NOT EXISTS dws_finance_metrics (
    code                VARCHAR,
    period_end          DATE,      -- 报告期末（会计区间终点）
    report_date         DATE,      -- **披露日**：财报实际公开的日期
    fiscal_year         INTEGER,
    fiscal_period       VARCHAR,   -- FY / Q1 / Q2 / Q3 / Q4
    revenue             DOUBLE,    -- 营业收入（元）
    net_profit          DOUBLE,    -- 净利润（元）
    parent_net_profit   DOUBLE,    -- 归母净利润（元）
    eps                 DOUBLE,    -- 基本每股收益（元/股）
    total_assets        DOUBLE,
    total_debt          DOUBLE,
    equity              DOUBLE,
    cash_flow_net       DOUBLE,    -- 经营活动现金流净额（元）
    debt_ratio          DOUBLE,    -- 资产负债率(%)
    roe                 DOUBLE,    -- ROE(%)：归母净利 / 股东权益
    revenue_yoy         DOUBLE,    -- 营收同比(%)，负基数时为 NULL
    profit_yoy          DOUBLE,    -- 归母净利同比(%)，负基数时为 NULL
    updated_at          TIMESTAMP,
    PRIMARY KEY (code, period_end)
);

-- ---------- 明细层 ----------
CREATE TABLE IF NOT EXISTS dwd_daily_bar (
    code      VARCHAR,
    date      DATE,
    open      DOUBLE,
    high      DOUBLE,
    low       DOUBLE,
    close     DOUBLE,
    preclose  DOUBLE,
    volume    DOUBLE,          -- 成交量(股)
    amount    DOUBLE,          -- 成交额(元)
    turn      DOUBLE,          -- 换手率(%)
    pct_chg   DOUBLE,          -- 涨跌幅(%)
    adjust    VARCHAR,         -- 复权方式
    PRIMARY KEY (code, date)
);

CREATE TABLE IF NOT EXISTS dwd_index_bar (
    code    VARCHAR,
    date    DATE,
    open    DOUBLE,
    high    DOUBLE,
    low     DOUBLE,
    close   DOUBLE,
    volume  DOUBLE,
    amount  DOUBLE,
    pct_chg DOUBLE,
    PRIMARY KEY (code, date)
);

-- ---------- 汇总层：因子宽表（核心，全部由 SQL 窗口函数计算）----------
CREATE TABLE IF NOT EXISTS dws_feature (
    code            VARCHAR,
    date            DATE,
    open            DOUBLE,
    high            DOUBLE,
    low             DOUBLE,
    close           DOUBLE,
    preclose        DOUBLE,
    volume          DOUBLE,
    amount          DOUBLE,
    turn            DOUBLE,
    pct_chg         DOUBLE,
    -- 均线
    ma5             DOUBLE,
    ma10            DOUBLE,
    ma20            DOUBLE,
    ma60            DOUBLE,
    ma120           DOUBLE,
    prev_ma5        DOUBLE,
    prev_ma20       DOUBLE,
    prev_ma60       DOUBLE,
    -- 量能
    vol_ma5         DOUBLE,
    vol_ma20        DOUBLE,
    vol_ma20_prev   DOUBLE,
    vol_ratio       DOUBLE,
    amount_ma20     DOUBLE,
    -- 前值
    prev_close      DOUBLE,
    prev2_close     DOUBLE,
    prev_volume     DOUBLE,
    prev_high       DOUBLE,
    -- 区间极值
    hh20_prev       DOUBLE,     -- 前 20 日最高价（不含当日），海龟突破用
    hh10            DOUBLE,
    ll10            DOUBLE,
    hh40            DOUBLE,
    ll40            DOUBLE,
    hh120           DOUBLE,
    ll120           DOUBLE,
    -- 动量与形态
    pct_5d          DOUBLE,
    pct_20d         DOUBLE,
    pct_60d         DOUBLE,
    pos_60          DOUBLE,     -- 60 日区间位置 0~1
    amplitude_20    DOUBLE,     -- 20 日平均振幅(%)
    rps120          DOUBLE,     -- 120 日相对强度百分位（横截面）
    float_mv        DOUBLE,     -- 流通市值(元)
    -- 结构标记
    listed_days     INTEGER,
    is_st           BOOLEAN,
    is_new_stock    BOOLEAN,
    is_limit_up     BOOLEAN,
    is_limit_down   BOOLEAN,
    is_yang         BOOLEAN,
    PRIMARY KEY (code, date)
);

-- ---------- 汇总层：市场状态 ----------
CREATE TABLE IF NOT EXISTS dws_market_regime (
    date                DATE PRIMARY KEY,
    up_count            INTEGER,
    down_count          INTEGER,
    limit_up_count      INTEGER,
    limit_down_count    INTEGER,
    broken_rate         DOUBLE,   -- 炸板率(%)
    total_amount        DOUBLE,   -- 全市场成交额
    amount_ratio        DOUBLE,   -- 相对 20 日均值倍数
    breadth_ma20        DOUBLE,   -- MA20 上方占比(%)
    breadth_ma60        DOUBLE,   -- MA60 上方占比(%)
    index_code          VARCHAR,
    index_pct_5d        DOUBLE,
    index_pct_20d       DOUBLE,
    top_sector          VARCHAR,
    top_sector_share    DOUBLE,
    state               VARCHAR,  -- trend/euphoria/recession/range/event
    state_label         VARCHAR,  -- 中文标签
    confidence          DOUBLE,
    -- w_short / w_swing / w_value 三档权重已于 2026-10-08 随主链路配额制移除。
    -- 市场状态现在只用于**展示**（宽度/涨停家数/炸板率），不再影响任何选股。
    -- 涨停家数/炸板率的来源口径：selfcalc（自算，可覆盖全部历史）或 upstream（同花顺涨停池）。
    -- 存下来是为了**可追溯**：日后回看某天的状态判定时，能立刻知道用的哪个口径，
    -- 否则口径切换造成的历史断层将无法解释。
    limit_up_source     VARCHAR,
    detail              VARCHAR
);

-- ---------- 汇总层：板块强度 ----------
-- 由本地 dwd_daily_bar + dim_stock_industry **自行计算**，不依赖任何板块接口。
-- 这样做的两个理由：
--   1. 可回填历史 —— 接口只给当日快照，没有历史就无法回测「板块效应是否有效」；
--   2. 不受接口稳定性影响 —— 免费板块接口（东财/申万）实测经常不可用。
CREATE TABLE IF NOT EXISTS dws_sector_strength (
    date            DATE,
    source          VARCHAR,     -- sw / sina：分类体系
    industry_name   VARCHAR,
    member_count    INTEGER,     -- 参与计算的成分股数（当日有行情的）
    up_count        INTEGER,
    down_count      INTEGER,
    limit_up_count  INTEGER,     -- 板块内涨停家数
    avg_pct_chg     DOUBLE,      -- 板块平均涨跌幅(%)
    median_pct_chg  DOUBLE,      -- 中位涨跌幅(%)，比均值更抗极端值
    total_amount    DOUBLE,      -- 板块成交额
    amount_ratio    DOUBLE,      -- 相对自身 20 日均值倍数，衡量资金关注度
    strength_score  DOUBLE,      -- 综合强度 0~100（涨幅+涨停+量能合成）
    -- source 必须进主键：申万与新浪是**两套分类体系**，若混在一起做横截面
    -- 百分位排名，会出现「19 只股票的新浪小板块」和「479 只的申万大行业」
    -- 同台竞争的情况，强度分失去可比性。因此强度只在**同一体系内**排名。
    PRIMARY KEY (date, source, industry_name)
);

-- 【已删除】研究产物三表（2026-09-30）
--   dws_limit_factor   涨停因子宽表（18,701 行）
--   dws_dragon_factor  龙虎榜因子宽表（13,189 行）
--   dws_style_matrix   状态 × 规则表现矩阵（42 行）
-- 依据：三者的研究结论已固化进 config/settings.yaml 与 config/data_registry.yaml
-- 的注释，且**没有任何生产代码读取**（style_matrix 的 42 个格子中"显著更好"为 0，
-- 状态级绑定无正向依据）。构建器模块（features/limit_factor.py、dragon_factor.py、
-- market/style_matrix.py）与依赖它们的 entry_cost 过滤同批移除。
-- 若将来解冻后要做新方向研究，按 git 历史还原本段即可。
-- ---------- 应用层：复盘归因（LLM）----------
-- 存「为什么赚/为什么亏」。没有它，策略迭代只能靠猜：
-- 胜率下降时无法区分「市场环境变了」（调权重）与「选股逻辑失效」（改策略），
-- 而这两者的应对完全相反。
-- category 是**固定枚举**（见 astock/review/attribution.CATEGORIES），
-- 不让模型自由发挥：自由文本无法聚合，而聚合统计才是迭代的依据。
-- 独立成表而非加列到 ads_review：归因会随模型升级重算，主表只存客观结果。
CREATE TABLE IF NOT EXISTS ads_review_attribution (
    rec_id      VARCHAR PRIMARY KEY,
    outcome     VARCHAR,     -- success / failure / flat
    category    VARCHAR,     -- 固定枚举 key
    reason      VARCHAR,     -- 模型给出的原因（20 字以内）
    lesson      VARCHAR,     -- 可执行的改进（20 字以内）
    model       VARCHAR,     -- 产生该归因的模型，便于换模型后对比
    created_at  TIMESTAMP
);

-- ---------- 应用层：推荐记录 ----------
CREATE TABLE IF NOT EXISTS ads_recommend (
    rec_id            VARCHAR PRIMARY KEY,
    rec_date          DATE,        -- 生成日期
    trade_date        DATE,        -- 计划交易日
    market_state      VARCHAR,
    market_label      VARCHAR,
    tier              VARCHAR,     -- short / swing / value
    tier_rank         INTEGER,     -- 档内排名
    code              VARCHAR,
    name              VARCHAR,
    strategy          VARCHAR,     -- 全部命中策略拼接（展示用）
    primary_strategy  VARCHAR,     -- 得分最高的主策略（按策略归因统计用）
    final_score       DOUBLE,
    strategy_score    DOUBLE,
    factor_score      DOUBLE,
    market_fit        DOUBLE,    -- 【已废弃】原「档位权重归一化」，档内常数、不影响排序，新记录写 NULL
    risk_penalty      DOUBLE,
    reasons           VARCHAR,     -- JSON 数组
    score_detail      VARCHAR,     -- JSON：评分拆解
    params_version    VARCHAR,
    created_at        TIMESTAMP
);

-- ---------- 应用层：T+1 复盘 ----------
CREATE TABLE IF NOT EXISTS ads_review (
    rec_id          VARCHAR PRIMARY KEY,
    rec_date        DATE,
    code            VARCHAR,
    tier            VARCHAR,
    strategy        VARCHAR,
    next_date       DATE,
    next_open       DOUBLE,
    next_close      DOUBLE,
    next_pct_chg    DOUBLE,     -- 次日涨跌幅(%)
    next_high_pct   DOUBLE,     -- 次日最大涨幅(%)
    next_low_pct    DOUBLE,     -- 次日最大回撤(%)
    hold3_pct       DOUBLE,     -- 持有 3 日收益(%)
    result          VARCHAR,    -- win / loss / flat / pending
    updated_at      TIMESTAMP
);

-- ---------- 明细层：盘中快照 ----------
-- 为什么需要它：现有 daily 依赖 `calendar.is_data_ready(今天)`，而
-- `data_ready_time = 16:00` —— 14:00 运行时守卫会**主动回退到上一交易日**。
-- 因此盘前/盘中的决策拿不到"当天此刻"的截面，必须另建一条链路。
--
-- 为什么必须落库而不能事后补：免费源（baostock/akshare 日线）不提供历史盘中数据。
-- **不从现在开始记录，就永远无法回测盘中决策。** 这是唯一有时效性的一步。
--
-- why `volume_ratio`（量比）是核心字段：14:00 时成交量只累积了约 3.5 小时，
-- 直接与前 5 日**全日**均量比较会系统性偏小（看着像"缩量"）。
-- 量比 = 当日累计量 / (过去 5 日每分钟均量 × 已开盘分钟数)，源已按时间归一化，
-- 盘中直接可比，无需再按时间折算。
--
-- 主键含 slot 而非 captured_at：同一时段重跑同一任务时覆盖而非追加，
-- 否则重试/补跑会留下重复行，且"几点几分抓的"这种抖动不该产生新样本。
CREATE TABLE IF NOT EXISTS dwd_intraday_snapshot (
    date            DATE,        -- 快照所属交易日
    slot            VARCHAR,     -- 时段标识，如 14:00（同一时段重跑即覆盖）
    code            VARCHAR,
    name            VARCHAR,
    price           DOUBLE,      -- 最新价
    preclose        DOUBLE,
    open            DOUBLE,
    high            DOUBLE,
    low             DOUBLE,
    pct_chg         DOUBLE,      -- 涨跌幅(%)
    volume          DOUBLE,      -- 成交量（手）
    amount          DOUBLE,      -- 成交额（元）
    turnover_rate   DOUBLE,      -- 换手率(%)
    volume_ratio    DOUBLE,      -- 量比（源已按已开盘时长归一化）
    float_mv        DOUBLE,      -- 流通市值
    total_mv        DOUBLE,      -- 总市值
    speed           DOUBLE,      -- 涨速
    pct_5min        DOUBLE,      -- 近 5 分钟涨跌(%)
    captured_at     TIMESTAMP,   -- 实际采集时刻
    source          VARCHAR,
    PRIMARY KEY (date, slot, code)
);

-- ---------- 应用层：影子模块候选（涨停基因 + 缩量不破位）----------
-- 定位：**独立于现有评分体系**。现有 8 策略 + 评分 + 配额那条链路的评价指标
-- （ret1 = 买入日 close/open − 1）在 T+1 下不可实现（实测可实现口径下超额归零、
-- D+5 显著为负 −0.605pp，t=−3.28）。因此在旧链路修好之前，新信号不能混进去，
-- 否则两边都无法归因。
--
-- 样本期警告：dwd_limit_up 仅覆盖 2025-09-23 起（243 天），"涨停基因"在此之前
-- 不可见 —— 现有回测只有约 1 年样本，**没有跨周期检验**。影子运行的目的正是积累。
--
-- 评价口径（必须是这个）：整理期以**市价**买入，持有到 D+1/D+3/D+5 收盘。
-- 不涉及涨停排队（买得到）、不涉及封板卖不出（涨停价有买盘队列，卖出可达）。
CREATE TABLE IF NOT EXISTS ads_shadow_pick (
    date          DATE,        -- 信号日
    code          VARCHAR,
    name          VARCHAR,
    close         DOUBLE,      -- 信号日收盘（买入价基准）
    zt20          INTEGER,     -- 过去 28 个自然日内涨停次数
    days_since_zt INTEGER,     -- 距上次涨停天数
    vol_ratio     DOUBLE,      -- 当日量 / 前 5 日均量（< 阈值 = 缩量）
    amount_ma20   DOUBLE,      -- 前 20 个交易日均成交额（流动性下限用；不含当日
                               -- 是为了让 18:30 日线路径与 14:00 快照路径口径一致）
    is_sealed     BOOLEAN,     -- 信号日是否**已封板**（铁律 2：封板则收盘买不进）。
                               -- 新行恒为 FALSE（候选已排除封板）；该列主要为**历史行**
                               -- 而设：历史是在"排除封板"之前采的，需要按此列回溯过滤，
                               -- 否则展示的成绩仍会包含买不进的票
    vs_ma5        DOUBLE,      -- 收盘 / 前 5 日均价 − 1（≥−0.02 = 不破位）
    signal_score  DOUBLE,      -- 按缩量程度排序的参考分（仅展示，不用于选股）
    next_date     DATE,        -- 下一交易日
    -- ⚠️ 以下三列（ret1/ret3/ret5）口径为「信号日收盘买」——**仅作诊断保留**：
    -- 信号日已封板的股票收盘买不进（实测占候选 15.3%，在分数≥50 档里高达 39.6%），
    -- 该口径的"收益"几乎全部来自这些买不进的票。**不得用于任何决策或展示**。
    ret1          DOUBLE,      -- [诊断·不可执行] 信号日收盘买 → 次日收盘卖(%)
    ret3          DOUBLE,      -- [诊断·不可执行] 同上，持有 3 日(%)
    ret5          DOUBLE,      -- [诊断·不可执行] 同上，持有 5 日(%)
    -- ✅ 可实现口径（裁判口径，见 astock/eval/judge.py 铁律 1/2）：
    --    买入 = 信号日次日**开盘**（一字板买不进，候选已排除）
    --    卖出 = 买入后第 N 个交易日**收盘**
    exec_d1       DOUBLE,      -- 可实现 D+1（最早合法卖点，**主口径**）
    exec_d3       DOUBLE,      -- 可实现 D+3
    exec_d5       DOUBLE,      -- 可实现 D+5
    exec_bench    DOUBLE,      -- 同期同池等权、同口径基准(%)，用于算超额
    hit_limit_up  BOOLEAN,     -- D+1 是否涨停（收益来源，用于归因）
    benchmark     DOUBLE,      -- 同期全池等权 D+1 收益(%)
    excess        DOUBLE,      -- ret1 − benchmark
    params        VARCHAR,     -- 参数快照，便于事后分辨"参数变了"与"信号失效"
    created_at    TIMESTAMP,
    PRIMARY KEY (date, code)
);

-- ---------- 系统层：采集进度与游标 ----------
CREATE TABLE IF NOT EXISTS sys_collect_state (
    task        VARCHAR,
    key         VARCHAR,
    value       VARCHAR,
    updated_at  TIMESTAMP,
    PRIMARY KEY (task, key)
);

CREATE TABLE IF NOT EXISTS sys_llm_usage (
    date        DATE PRIMARY KEY,
    calls       INTEGER,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    updated_at  TIMESTAMP
);

-- 每轮选股的策略命中统计，用于观察各策略在不同市场环境下的产出能力
CREATE TABLE IF NOT EXISTS sys_strategy_stats (
    data_date   DATE,
    tier        VARCHAR,
    strategy    VARCHAR,
    label       VARCHAR,
    hits        INTEGER,
    updated_at  TIMESTAMP,
    PRIMARY KEY (data_date, strategy)
);

-- ---------- 应用层：影子信号复盘（归因）----------
-- 2026-10-08 新增：主链路删除后，**复盘对象改为影子信号**。
-- 一行 = 一次复盘（按复盘日主键），内容 = 可实现口径的聚合统计 + LLM 归因文字。
--
-- ⚠️ 判据一律是**可实现口径**（exec_*：信号日次日开盘买 → 再次日收盘卖）。
--    旧口径 avg_ret1_old 只作对照 —— 它的收益 100% 来自当日已封板、收盘买不进的票，
--    单列出来正是为了看得见"幻影收益"有多大（历史占候选 15.3%）。
CREATE TABLE IF NOT EXISTS ads_shadow_review (
    review_date    DATE PRIMARY KEY,   -- 复盘日（= 数据日）
    window_from    DATE,               -- 覆盖的信号日区间
    window_to      DATE,
    signal_days    INTEGER,            -- 覆盖几个信号日
    picks          INTEGER,            -- 样本数（已排除当日封板的不可买样本）
    win_rate       DOUBLE,             -- 上涨占比(%)
    avg_exec_d1    DOUBLE,             -- 可实现日均收益(%)  ← 唯一判据
    avg_bench      DOUBLE,             -- 同池等权基准(%)
    excess         DOUBLE,             -- 超额(%)
    avg_ret1_old   DOUBLE,             -- 旧口径（仅对照）
    hit_zt         INTEGER,            -- 命中涨停数
    llm_used       BOOLEAN,            -- 归因是否来自 LLM（false = 本地模板）
    verdict        VARCHAR,
    wins           VARCHAR,
    losses         VARCHAR,
    lesson         VARCHAR,
    adjust         VARCHAR,
    detail         VARCHAR,            -- 涨跌各前 5 只（JSON）
    created_at     TIMESTAMP
);

-- 已删除（2026-10-08）：ads_backtest —— 主链路（8 策略 → 评分 → 配额）的回测明细。
-- 该链条经 250 个交易日、可实现口径的样本外体检**全部无 alpha**，已连代码一并删除。
-- 影子的净值/校准改由 ads_shadow_pick 现算（api /api/equity、/api/calibration）。
