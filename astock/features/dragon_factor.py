# -*- coding: utf-8 -*-
"""龙虎榜因子宽表（`dws_dragon_factor`）。

------------------------------------------------------------------
为什么建它
------------------------------------------------------------------
`dwd_dragon_tiger` 已采 243 天、约 1.9 万条，含**机构净买入、游资净买入、人气排名**——
这是现有 8 条策略完全没有的信息维度（它们全是量价形态：均线、突破、量比、换手）。
但这批数据此前**只用于报告展示，从未进过评分或回测**，等于白采。

本模块把它变成**可验证的因子**：把「当日可见的龙虎榜特征 + 板块特征」
与「信号日之后的收益」放进同一行，供 IC / 分组收益检验。

------------------------------------------------------------------
为什么只取「当日榜」
------------------------------------------------------------------
同一只股票可能同时出现在「当日榜」和「3 日榜」，两者是**不同统计口径**：
当日榜是该日席位买卖，3 日榜是连续 3 日累计。混在一行里会让
「当日净买入」的含义漂移，使因子失效且难以察觉。
因此以 `range_days = 1` 为基行，3 日榜净买入另存 `net_value_3d` 列。

------------------------------------------------------------------
口径（与 `dws_limit_factor` 保持一致）
------------------------------------------------------------------
信号日 T 收盘后数据可见 → T+1 **开盘买入**：
    open_premium = T+1 开盘 / T 收盘 − 1     （买入成本）
    ret1         = T+1 收盘 / T+1 开盘 − 1   （买入后到手收益）
    ret3 / ret5  = T+3 / T+5 收盘 / T+1 开盘 − 1
收益用 `LEAD()` 取「下一根 K 线」而不是「日期 +1」：停牌日没有 K 线，
按日期加 1 会取到错位的价格。
"""

from __future__ import annotations

from datetime import date

from astock.logger import get_logger
from astock.storage.db import Storage, get_storage

logger = get_logger("features.dragon_factor")

# 「近 N 个**交易日**内上榜次数」：按交易日序号做区间连接，而不是
# `ROWS BETWEEN 4 PRECEDING` —— 后者是「最近 5 次上榜」，两次上榜间隔
# 可能隔了几个月，那算出来的就不是「近期热度」了。
CORE_SQL = """
WITH seq AS (
    SELECT date, ROW_NUMBER() OVER (ORDER BY date) AS dn
    FROM trade_calendar WHERE is_open
),
base AS (
    SELECT t.date, t.code, s.dn,
           t.net_value, t.net_rate, t.org_net_value, t.hot_money_net_value,
           t.org_buy_num, t.org_sell_num, t.hot_rank, t.limit_reason
    FROM dwd_dragon_tiger t
    JOIN seq s ON s.date = t.date
    WHERE t.range_days = 1
),
three AS (
    SELECT date, code, net_value AS net_value_3d
    FROM dwd_dragon_tiger WHERE range_days = 3
),
recent5 AS (
    SELECT a.date, a.code, COUNT(*) AS list_cnt_5d, SUM(b.net_value) AS net_sum_5d
    FROM base a JOIN base b ON b.code = a.code AND b.dn BETWEEN a.dn - 4 AND a.dn
    GROUP BY 1, 2
),
recent20 AS (
    SELECT a.date, a.code, COUNT(*) AS list_cnt_20d
    FROM base a JOIN base b ON b.code = a.code AND b.dn BETWEEN a.dn - 19 AND a.dn
    GROUP BY 1, 2
),
ind AS (
    -- 列名是 industry_name（实测核对，不是 industry）；取 is_primary 的主用行业，
    -- 否则一股多行业会让同一条记录被复制成多行、板块统计翻倍。
    SELECT code, industry_name AS industry FROM dim_stock_industry WHERE is_primary
),
sec AS (
    SELECT b.date, i.industry,
           COUNT(*) AS sector_list_cnt, SUM(b.net_value) AS sector_net_sum
    FROM base b JOIN ind i ON i.code = b.code GROUP BY 1, 2
),
secrank AS (
    SELECT b.date, b.code,
           ROW_NUMBER() OVER (PARTITION BY b.date, i.industry ORDER BY b.net_value DESC)
               AS sector_rank
    FROM base b JOIN ind i ON i.code = b.code
),
lu AS (
    SELECT l.date, i.industry, COUNT(*) AS sector_limit_up
    FROM dwd_limit_up l JOIN ind i ON i.code = l.code GROUP BY 1, 2
),
ss AS (
    -- `dws_sector_strength` 带 `source` 列，同 (date, industry_name) 可能有**多来源**的行。
    -- 直接 JOIN 会让结果行静默翻倍（因子表看起来正常，统计全错）。
    -- 这里先按 key 聚合掉，保证"一天一行业一行"。
    SELECT date, industry_name, MAX(strength_score) AS strength
    FROM dws_sector_strength GROUP BY 1, 2
),
bars AS (
    SELECT code, date, close,
           LEAD(open)      OVER w AS n_open,
           LEAD(close)     OVER w AS n_close,
           LEAD(close, 3)  OVER w AS n3_close,
           LEAD(close, 5)  OVER w AS n5_close
    FROM dwd_daily_bar
    WINDOW w AS (PARTITION BY code ORDER BY date)
)
SELECT
    b.date, b.code, i.industry,
    b.net_value, b.net_rate, b.org_net_value, b.hot_money_net_value,
    -- 「谁主导」：机构占多空双方合计的比例。用绝对值做分母，
    -- 否则机构 +2 亿、游资 -1 亿会算出 >100% 的无意义比例。
    100.0 * b.org_net_value
        / NULLIF(ABS(b.org_net_value) + ABS(b.hot_money_net_value), 0) AS inst_share,
    CASE
        WHEN COALESCE(b.org_net_value, 0) > 0 AND COALESCE(b.hot_money_net_value, 0) > 0
            THEN 'both'      -- 机构与游资同向买入（最强共振）
        WHEN COALESCE(b.org_net_value, 0) < 0 AND COALESCE(b.hot_money_net_value, 0) < 0
            THEN 'diverge'   -- 双方都在卖
        WHEN ABS(COALESCE(b.org_net_value, 0)) >= ABS(COALESCE(b.hot_money_net_value, 0))
            THEN 'inst'
        WHEN COALESCE(b.hot_money_net_value, 0) <> 0 THEN 'youzi'
        ELSE 'none'
    END AS dominance,
    b.org_buy_num, b.org_sell_num, b.hot_rank,
    t3.net_value_3d, b.limit_reason,
    r5.list_cnt_5d, r20.list_cnt_20d, r5.net_sum_5d,
    sc.sector_list_cnt, sc.sector_net_sum, sr.sector_rank,
    ss.strength AS sector_strength, lu.sector_limit_up,
    -- 板块强度列名是 strength_score（实测核对）
    CASE WHEN bd.n_open > 0 THEN (bd.n_open / bd.close - 1) * 100 END AS open_premium,
    CASE WHEN bd.n_open > 0 THEN (bd.n_close / bd.n_open - 1) * 100 END AS ret1,
    CASE WHEN bd.n_open > 0 THEN (bd.n3_close / bd.n_open - 1) * 100 END AS ret3,
    CASE WHEN bd.n_open > 0 THEN (bd.n5_close / bd.n_open - 1) * 100 END AS ret5
FROM base b
LEFT JOIN three   t3 ON t3.date = b.date AND t3.code = b.code
LEFT JOIN recent5 r5 ON r5.date = b.date AND r5.code = b.code
LEFT JOIN recent20 r20 ON r20.date = b.date AND r20.code = b.code
LEFT JOIN ind      i ON i.code = b.code
LEFT JOIN sec     sc ON sc.date = b.date AND sc.industry = i.industry
LEFT JOIN secrank sr ON sr.date = b.date AND sr.code = b.code
LEFT JOIN ss        ON ss.date = b.date AND ss.industry_name = i.industry
LEFT JOIN lu       lu ON lu.date = b.date AND lu.industry = i.industry
LEFT JOIN bars     bd ON bd.code = b.code AND bd.date = b.date
"""


class DragonFactorBuilder:
    """龙虎榜因子宽表构建器（纯派生表，可全量重算）。"""

    def __init__(self, storage: Storage | None = None) -> None:
        self.storage = storage or get_storage()

    def rebuild_all(self) -> int:
        """全量重算。口径变更后必须走这条路，避免新旧口径混在一张表里。"""
        self.storage.execute("DELETE FROM dws_dragon_factor")
        n = self.build()
        logger.info("龙虎榜因子全量重建完成：%d 行", n)
        return n

    def build(self, start: date | None = None, end: date | None = None) -> int:
        """构建（可按区间增量）。区间内先删后插，保证幂等。"""
        conds, params = [], []
        if start is not None:
            conds.append("date >= ?")
            params.append(start)
        if end is not None:
            conds.append("date <= ?")
            params.append(end)
        where = f" WHERE {' AND '.join(conds)}" if conds else ""

        if conds:
            self.storage.execute(
                f"DELETE FROM dws_dragon_factor{where}", params
            )
        # 注意：约束只能加在**最外层** SELECT 上。放进 CTE 会改变
        # `recent5` 这类窗口统计的口径（它们需要看到区间之外的历史上榜记录，
        # 否则区间左边界附近的「近 5 日上榜次数」会被系统性低估）。
        sql = (
            f"INSERT INTO dws_dragon_factor "
            f"SELECT * FROM ({CORE_SQL}) t{where}"
        )
        self.storage.execute(sql, params)
        n = int(self.storage.query_value("SELECT COUNT(*) FROM dws_dragon_factor") or 0)
        logger.info("龙虎榜因子构建完成：全表 %d 行", n)
        return n

    def summary(self) -> dict:
        """给报告/命令行用的概览。"""
        return {
            "rows": int(self.storage.query_value("SELECT COUNT(*) FROM dws_dragon_factor") or 0),
            "dates": int(
                self.storage.query_value("SELECT COUNT(DISTINCT date) FROM dws_dragon_factor") or 0
            ),
            "with_ret": int(
                self.storage.query_value(
                    "SELECT COUNT(*) FROM dws_dragon_factor WHERE ret1 IS NOT NULL"
                )
                or 0
            ),
        }
