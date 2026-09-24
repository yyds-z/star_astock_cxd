# -*- coding: utf-8 -*-
"""涨停因子宽表（dws_limit_factor）。

解决的问题：
`dwd_limit_up`（连板数/封单额/题材）和 `dwd_daily_bar`（未来收益）分处两张表，
无法回答最关键的问题 ——「连板高度、封单额、题材热度这些字段，
**到底能不能预测次日收益**」。不回答这个问题，新采集的数据就只能当报告装饰，
无法理直气壮地接进评分器。

因此这里把「信号日可见的因子」与「信号日之后的收益」放进同一行，
供 `scripts/factor_ic.py` 做 IC 与分组检验。

收益口径（与全系统一致，务必不要改）：
    信号日 T 收盘触发 → T+1 **开盘价买入** → 因此
    - `open_premium`：次日开盘溢价 = T+1 开盘 / T 收盘 - 1（打板者要付的溢价）
    - `ret1`：买入后当日收益   = T+1 收盘 / T+1 开盘 - 1
    - `ret3` / `ret5`：持有到 T+3 / T+5 收盘
    用「T+1 开盘」而非「T 收盘」作成本，是因为 T 日已涨停，实际上买不到。

纯派生表：全部内容可由 dwd_limit_up + dwd_daily_bar + dws_sector_strength 重算。
"""

from __future__ import annotations

from datetime import date

from astock.config import get_config
from astock.logger import get_logger
from astock.storage.db import Storage, get_storage

logger = get_logger("features.limit_factor")

# 列顺序必须与 schema.sql 中 dws_limit_factor 的定义一致
FACTOR_COLUMNS = [
    "date", "code", "boards", "board_text", "reason", "first_minutes",
    "is_new", "is_st", "seal_money", "seal_ratio", "turn", "amount", "float_mv",
    "pct_5d", "market_limit_up", "market_break", "break_rate",
    "theme_heat", "sector_strength",
    "open_premium", "ret1", "ret3", "ret5", "high1", "low1",
]

# 09:30 距零点的分钟数。首次涨停时间转为「距开盘分钟数」后，
# 09:25 的一字板得到 -5（最小 = 最强），与「越小越强」的直觉一致。
OPEN_MINUTES = 9 * 60 + 30

FACTOR_SQL = """
WITH bars AS (
    SELECT
        code, date, open, high, low, close, amount, turn,
        LEAD(open, 1)  OVER w AS n_open,
        LEAD(close, 1) OVER w AS n_close,
        LEAD(high, 1)  OVER w AS n_high,
        LEAD(low, 1)   OVER w AS n_low,
        LEAD(close, 3) OVER w AS c3,
        LEAD(close, 5) OVER w AS c5
    FROM dwd_daily_bar
    WINDOW w AS (PARTITION BY code ORDER BY date)
),
mkt AS (
    SELECT date, COUNT(*) AS cnt FROM dwd_limit_up GROUP BY date
),
brk AS (
    SELECT date, COUNT(*) AS cnt FROM dwd_limit_break GROUP BY date
),
-- 题材热度必须按**单个概念**统计，不能整串精确匹配。
-- reason 是完整题材链（如「资产重组+商业地产+央企+ST板块」），
-- 整串几乎每只都不同，匹配结果恒为 1，因子直接失效（真的踩过）。
concepts AS (
    SELECT
        date,
        code,
        TRIM(UNNEST(STRING_SPLIT(reason, '+'))) AS concept
    FROM dwd_limit_up
    WHERE reason IS NOT NULL AND reason <> ''
),
concept_cnt AS (
    SELECT date, concept, COUNT(*) AS n FROM concepts GROUP BY date, concept
),
theme AS (
    -- 取该股所属概念中**最热**的那个（只要有任一概念被资金追捧即算热点）
    SELECT c.date, c.code, MAX(cc.n) AS n
    FROM concepts c
    JOIN concept_cnt cc ON cc.date = c.date AND cc.concept = c.concept
    GROUP BY c.date, c.code
),
ind AS (
    SELECT code, ANY_VALUE(industry_name) AS industry_name
    FROM dim_stock_industry
    WHERE is_primary
    GROUP BY code
),
feat AS (
    SELECT code, date, float_mv, pct_5d FROM dws_feature
)
SELECT
    lu.date,
    lu.code,
    lu.boards,
    lu.board_text,
    lu.reason,
    CASE
        WHEN lu.first_time IS NULL OR lu.first_time = '' THEN NULL
        ELSE TRY_CAST(SPLIT_PART(lu.first_time, ':', 1) AS INTEGER) * 60
             + TRY_CAST(SPLIT_PART(lu.first_time, ':', 2) AS INTEGER)
             - {open_minutes}
    END AS first_minutes,
    lu.is_new,
    lu.is_st,
    lu.seal_money,
    lu.seal_money / NULLIF(b.amount, 0) AS seal_ratio,
    b.turn,
    b.amount,
    f.float_mv,
    f.pct_5d,
    mkt.cnt AS market_limit_up,
    brk.cnt AS market_break,
    100.0 * brk.cnt / NULLIF(mkt.cnt + COALESCE(brk.cnt, 0), 0) AS break_rate,
    th.n AS theme_heat,
    ss.strength_score AS sector_strength,
    (b.n_open / NULLIF(b.close, 0) - 1) * 100        AS open_premium,
    (b.n_close / NULLIF(b.n_open, 0) - 1) * 100      AS ret1,
    (b.c3 / NULLIF(b.n_open, 0) - 1) * 100           AS ret3,
    (b.c5 / NULLIF(b.n_open, 0) - 1) * 100           AS ret5,
    (b.n_high / NULLIF(b.n_open, 0) - 1) * 100       AS high1,
    (b.n_low / NULLIF(b.n_open, 0) - 1) * 100        AS low1
FROM dwd_limit_up lu
JOIN bars b ON b.code = lu.code AND b.date = lu.date
LEFT JOIN mkt   ON mkt.date = lu.date
LEFT JOIN brk   ON brk.date = lu.date
LEFT JOIN theme th ON th.date = lu.date AND th.code = lu.code
LEFT JOIN ind   ON ind.code = lu.code
LEFT JOIN dws_sector_strength ss
       ON ss.date = lu.date
      AND ss.industry_name = ind.industry_name
      AND ss.source = ?
LEFT JOIN feat f ON f.code = lu.code AND f.date = lu.date
"""


class LimitFactorBuilder:
    """构建涨停因子宽表（纯派生，可全量重算）。"""

    def __init__(self, storage: Storage | None = None) -> None:
        self.storage = storage or get_storage()
        self.cfg = get_config()

    @property
    def source(self) -> str:
        """行业分类体系，须与 dim_stock_industry 的 is_primary 标签一致。"""
        return str(self.cfg.get("sector.primary_source", "sw") or "sw")

    def build(self) -> int:
        """全量重算。

        选全量而非增量：涨停池只有 1.8 万行，重算耗时以秒计，
        而增量需要处理「未来收益随新交易日变化」的回填（T+1/T+3/T+5 会陆续到齐），
        复杂度远高于收益。全量重算天然没有这类一致性问题。
        """
        sql = FACTOR_SQL.format(open_minutes=OPEN_MINUTES)
        cols = ", ".join(FACTOR_COLUMNS)
        self.storage.execute("DELETE FROM dws_limit_factor")
        self.storage.execute(
            f"INSERT INTO dws_limit_factor ({cols}) " + sql,
            [self.source],
        )
        row = self.storage.query_one(
            "SELECT COUNT(*), COUNT(ret1), MIN(date), MAX(date) FROM dws_limit_factor"
        )
        total, with_ret, lo, hi = row
        logger.info(
            "涨停因子表重建完成：%d 行（%s ~ %s），其中 %d 行已具备次日收益",
            total, lo, hi, with_ret,
        )
        return int(total or 0)

    # ---------------- 查询 ----------------
    def latest_date(self) -> date | None:
        return self.storage.latest_trade_date("dws_limit_factor")
