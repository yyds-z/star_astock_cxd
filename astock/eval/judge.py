# -*- coding: utf-8 -*-
"""统一裁判：可实现口径的收益、超额与显著性检验。

**全系统唯一的评价口径来源。** 报告、回测、界面、参数网格都必须调用这里，
不允许任何地方再自己算一遍收益 —— 本项目的历史错误全部源于"另一处也算了一遍"。

★★★ 两条铁律（已因它们翻车 5 次，写死在代码里）★★★

  铁律 1（T+1）：**买入日 < 卖出日**。A 股当日买入当日不可卖出。
  铁律 2（可买性）：**买不到的价格不能当成交价**。
      · 买"信号日收盘" → 要求信号日**非封板**（封板时没有卖盘，买不进）
      · 买"次日开盘"   → 要求次日**非一字板**（high == low 时全天一个价，无成交）

历史错误清单（每一次都曾得出"业绩很好"的结论）：

  ① `ret1 = close/open − 1`（开盘买、当天收盘卖）
     → 违反铁律 1。曾据此得出"超额 +0.476pp、t=5.66"。
  ② 用**当日成交额**当流动性下限
     → 混淆了信号强度：影子信号本身就是"缩量"，当日额小是信号，不是流动性差。
  ③ 候选含**当日封板**股，却按收盘价计入收益
     → 违反铁律 2。实测"业绩"几乎全部来自这部分（占候选 15.3%，
       在分数≥50 档里高达 39.6%）；剔除后超额由 +0.65% 变成 −0.17%。
  ④ 「t+1 开盘买 → **t+1 收盘卖**」
     → 再次违反铁律 1（当天买当天卖）。曾据此得出"超额 +0.164%、t=2.18"。

口径定义（两个都合法，取 B 作对外主口径）：

  A（最早入场，需判断封板）  买 **t 收盘**（非封板）→ 卖 **t+1 收盘**
  B（统一口径，无需判断封板）买 **t+1 开盘**（非一字）→ 卖 **t+2 收盘**

  为什么把 B 作为对外主口径：它对所有候选一视同仁、不需要"今天是否封板"这一
  判断（那个判断本身就是最容易出错的地方），且天然满足 T+1。
"""

from __future__ import annotations

import math
import statistics
from typing import Any

import pandas as pd

from astock.storage.db import Storage  # noqa: F401  (类型标注)

# 单次往返成本（佣金+印花税+滑点的保守估计）。扣除方式：从策略日收益里减。
EXEC_COST = 0.003

# 可交易池（与选股池一致；基准必须与策略**同池同口径**）
POOL_SQL = """
    s.board IN ('main', 'gem')
    AND COALESCE(s.is_st, FALSE) = FALSE
    AND (s.out_date IS NULL OR s.out_date > {alias}.date)
    AND s.ipo_date <= {alias}.date - INTERVAL 120 DAY
"""

BARS_CTE = """
bars AS (
    SELECT b.code, b.date, b.open, b.high, b.low, b.close,
           LEAD(b.open, 1)  OVER w AS o1,
           LEAD(b.high, 1)  OVER w AS h1,
           LEAD(b.low, 1)   OVER w AS l1,
           LEAD(b.close, 1) OVER w AS c1,
           LEAD(b.close, 2) OVER w AS c2
    FROM dwd_daily_bar b
    WHERE b.open > 0 AND b.close > 0 AND b.volume > 0
    WINDOW w AS (PARTITION BY b.code ORDER BY b.date)
)
"""


def pool_benchmark(storage) -> pd.DataFrame:
    """同池等权基准：每个交易日、两个口径各一条。

    为什么基准必须是"同池等权"而不是指数：判断"选股是否有效"的对照是
    "同一天、同一可交易池、随便买"能拿到多少。指数的成分与交易限制都不同。
    """
    sql = f"""
    WITH {BARS_CTE}
    SELECT b.date,
           AVG((b.c1 / NULLIF(b.close, 0) - 1) * 100) AS bm_a,
           AVG((b.c2 / NULLIF(b.o1, 0) - 1) * 100)     AS bm_b
    FROM bars b JOIN dim_stock s ON s.code = b.code
    WHERE {POOL_SQL.format(alias='b')}
      AND b.c1 IS NOT NULL AND b.c2 IS NOT NULL
    GROUP BY b.date
    """
    return storage.query_df(sql)


def picks_with_returns(storage, source_sql: str,
                       params: list[Any] | None = None) -> pd.DataFrame:
    """给任意候选集附上**可执行口径**的收益与可买性标记。

    `source_sql` 必须返回 (date, code, ...) 两列（其余列原样带出）。
    """
    sql = f"""
    WITH {BARS_CTE}
    SELECT p.*,
           b.open, b.high, b.low, b.close, b.o1, b.h1, b.l1, b.c1, b.c2,
           (l.code IS NOT NULL)     AS zt_t,      -- 信号日已封板 → 铁律 2 命中
           (b.h1 <= b.l1 * 1.0001)  AS yizi_t1    -- 次日一字板   → 铁律 2 命中
    FROM ( {source_sql} ) p
    JOIN bars b ON b.code = p.code AND b.date = p.date
    LEFT JOIN dwd_limit_up l ON l.code = p.code AND l.date = p.date
    WHERE b.c1 IS NOT NULL AND b.c2 IS NOT NULL
    """
    df = storage.query_df(sql, params or [])
    if df.empty:
        return df
    for c in ("close", "o1", "c1", "c2"):
        df[c] = df[c].astype(float)
    for c in ("zt_t", "yizi_t1"):
        df[c] = df[c].fillna(False).astype(bool)
    # 口径 A：买 t 收盘 → 卖 t+1 收盘（可执行 ⇔ 信号日非封板）
    df["r_a"] = (df["c1"] / df["close"] - 1) * 100
    # 口径 B：买 t+1 开盘 → 卖 t+2 收盘（可执行 ⇔ 次日非一字板）
    df["r_b"] = (df["c2"] / df["o1"] - 1) * 100
    return df


def attach_benchmark(df: pd.DataFrame, bench: pd.DataFrame) -> pd.DataFrame:
    """贴基准并算超额（口径 A/B 各一组）。"""
    if df.empty:
        return df
    out = df.merge(bench, on="date", how="left")
    out["ex_a"] = out["r_a"] - out["bm_a"]
    out["ex_b"] = out["r_b"] - out["bm_b"]
    return out


def executable(df: pd.DataFrame, kou: str = "b") -> pd.DataFrame:
    """按铁律 2 过滤出**真正能成交**的候选。

    kou="a"：要求信号日非封板　kou="b"：要求次日非一字板
    """
    if df.empty:
        return df
    return df[~df["zt_t"]] if kou == "a" else df[~df["yizi_t1"]]


def summarise(df: pd.DataFrame, kou: str = "b", cost: float = EXEC_COST) -> dict[str, Any]:
    """按日聚类统计：日均超额、t、累计净值、样本量。

    为什么按**日**聚类算 t：同一天的多只候选高度相关，把每一笔当独立样本会
    把 t 值抬高数倍（这是本项目最早的一个统计错误）。
    """
    if df is None or df.empty or df["date"].nunique() < 3:
        return {"days": 0}
    r, ex = f"r_{kou}", f"ex_{kou}"
    g = df.groupby("date").agg(r=(r, "mean"), ex=(ex, "mean"), n=("code", "size"))
    n = len(g)
    sd = float(g["ex"].std(ddof=1))
    nav = 1.0
    for v in g["r"]:
        nav *= (1 + (float(v) - cost) / 100.0)
    return {
        "days": n,
        "picks": int(len(df)),
        "per_day": round(len(df) / n, 1),
        "excess": round(float(g["ex"].mean()), 4),
        "t": round(float(g["ex"].mean()) / (sd / math.sqrt(n)), 2) if sd else None,
        "nav": round(nav, 3),
        "win_rate": round(float((g["ex"] > 0).mean()) * 100, 1),
    }


def split_out_of_sample(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """按交易日对半拆：观察期（选参）/ 验证期（检验）。

    空集/单日必须能安全返回：体检会跑到"整段历史只有 1 笔信号"的策略
    （实测 new_stock_burst 250 日仅 1 笔），此时按索引取中点会
    `IndexError: list index out of range` 直接把整次体检打断。
    """
    if df is None or df.empty:
        return df, df
    days = sorted(set(df["date"].unique()))
    if len(days) < 2:
        return df.iloc[0:0], df
    mid = days[len(days) // 2]
    return df[df["date"] < mid], df[df["date"] >= mid]


def bonferroni_t(n_tests: int, n_days: int, alpha: float = 0.05) -> float:
    """多重检验下的 t 门槛（双侧 Bonferroni 校正）。

    为什么需要：在 N 个参数组合里挑最好的那个，即使全是噪声，也必然有一个
    "看起来显著" —— 实测本项目 7 轮回测的调参全部落在噪声范围内。
    扫 N 个组合就用 alpha/N 的临界值，把"试出来的最优"挡在门外。
    """
    if n_tests <= 1:
        return 1.96
    p = 1.0 - alpha / (2.0 * n_tests)
    # 用正态分位近似（样本 ≥60 日时足够；样本更小则更保守地抬高门槛）
    q = statistics.NormalDist().inv_cdf(p)
    if n_days < 60:
        q *= 1.15
    return round(q, 2)


def format_table(rows: list[dict[str, Any]], title: str = "") -> str:
    """把若干 summarise 结果打成一张表（报告/界面共用同一套呈现）。"""
    lines = [title] if title else []
    lines.append(f"{'口径/分组':<34}{'天数':>6}{'只/日':>8}{'日均超额':>10}{'t':>7}{'净值':>8}")
    for r in rows:
        if not r or not r.get("days"):
            continue
        lines.append(
            f"{r.get('label', ''):<34}{r['days']:>6}{r.get('per_day', 0):>8.1f}"
            f"{r['excess']:>+9.3f}%{(r['t'] if r['t'] is not None else float('nan')):>7.2f}"
            f"{r['nav']:>8.2f}"
        )
    return "\n".join(lines)
