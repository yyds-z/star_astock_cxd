# -*- coding: utf-8 -*-
"""入场成本过滤：剔除「次日大概率大幅高开」的当日涨停候选。

------------------------------------------------------------------
为什么只针对「当日涨停候选」，而不是全部候选
------------------------------------------------------------------
动机来自一条被数据推翻的假设。原本的理由是「系统在 T+1 开盘买入，
高开就是成本」，但实测（5571 笔候选，2026-09-23 轮次）：

    候选股次日开盘跳空：均值 -1.203%，中位 -0.991%
    高开占比 29.2%，低开(<-2%) 占比 35.2%

**候选股是系统性低开的（折价），不是高开追高。** 真实资金曲线是：

    T+1 开盘买入（折价 -1.20%，有利）
      → 当日日内 +0.686%
      → 隔夜 -0.86%   ← 损失全在这里
      → T+2 开盘 -0.17% / T+2 收盘 +0.24%

也就是说：**问题不是入场价，而是 T+1 制度强制持有过夜**。
因此对全部候选做「入场成本惩罚」是错的 —— 那会把统计上便宜的入场当成昂贵。

但有一小类候选确实会高开：**当日涨停的股票**（占候选 4.52%，实测 974/21561）。
它们在涨停池的统计规律下必然高开，且高开幅度随封板强度单调上升：

    封单额/成交额      首次涨停时间        连板高度
    <2%    溢价 +0.00%   一字板      +5.28%   1板 +1.66%
    5~10%  溢价 +1.51%   开盘10分内  +2.53%   3板 +3.59%
    >20%   溢价 +3.49%   60分后      +1.07%   7板 +4.54%

在**候选股子集**上同样成立（这是本模块直接作用的样本）：

    封单占比 >20% 的当日涨停候选：溢价 +4.17%，买入后 -0.01%（成本 4% 却只打平）
    封单占比 <2%  的当日涨停候选：溢价 -0.70%，买入后 +2.18%（折价入场，反而最好）

所以只有「强封板 + 当日涨停」这一小类需要处理。

------------------------------------------------------------------
为什么用「三档取最大」而不是线性回归
------------------------------------------------------------------
回归溢价 ~ 封板特征 的 R² 仅 **0.054**，且系数与分档结论矛盾
（封单占比系数几乎为 0，因为它与流通市值共线）。
三者本质是同一件事（封板强度）的三个侧面，线性叠加会引入虚假精度。
改用**分档查表 + 取三者最大值**：保守、可解释、不假装精确。
"""

from __future__ import annotations

from typing import Any

# 次日开盘溢价的**分档经验值**（来源：dws_limit_factor 18636 条，2026-09-23 测算）
# 键为分档上界，值为该档平均溢价(%)。
PREMIUM_BY_SEAL_RATIO: list[tuple[float, float]] = [
    (2.0, 0.00),      # 封单额/成交额 < 2%
    (5.0, 0.95),
    (10.0, 1.51),
    (20.0, 1.90),
    (float("inf"), 3.49),  # > 20%
]

# 首次涨停时间：越早封板越强（minutes 为距 09:30 的分钟数，一字板为负）
PREMIUM_BY_FIRST_MINUTES: list[tuple[float, float]] = [
    (-4.0, 5.28),     # 一字板（09:25 或更早）
    (10.0, 2.53),     # 开盘 10 分钟内
    (30.0, 1.67),
    (60.0, 1.51),
    (float("inf"), 1.07),  # 60 分钟后
]

# 连板高度
PREMIUM_BY_BOARDS: list[tuple[int, float]] = [
    (1, 1.66),
    (2, 2.79),
    (3, 3.59),
    (4, 3.74),
    (5, 3.91),
    (6, 3.91),
    (7, 4.54),
]
PREMIUM_BOARDS_MAX = 4.8  # 7 板以上，外推封顶（样本仅 52 条，不宜继续放大）


def _bucket_lookup(value: float, table: list[tuple[float, float]]) -> float:
    for upper, premium in table:
        if value < upper:
            return premium
    return table[-1][1]


def _first_minutes(first_time: Any) -> float | None:
    """首次涨停时间 → 距 09:30 的分钟数（一字板 09:25 即 -5，越小越强）。"""
    if not first_time:
        return None
    try:
        hh, mm = str(first_time).strip().split(":")[:2]
        return float(int(hh) * 60 + int(mm) - (9 * 60 + 30))
    except (ValueError, IndexError, TypeError):
        return None


def estimate_open_premium(
    boards: Any = None,
    seal_ratio_pct: Any = None,
    first_minutes: Any = None,
) -> float | None:
    """估计当日涨停股在次日的**开盘溢价（%）**，即买入成本。

    三者取**最大值**：它们高度相关，线性叠加会重复计算同一信息；
    取最悲观的那个既保守又不引入虚假精度。

    返回 None 表示没有任何可用特征（此时调用方应跳过过滤，不做猜测）。
    """
    cands: list[float] = []
    try:
        if boards is not None and float(boards) >= 1:
            b = int(float(boards))
            table = dict(PREMIUM_BY_BOARDS)
            cands.append(table.get(b, PREMIUM_BOARDS_MAX if b > 7 else 1.66))
    except (TypeError, ValueError):
        pass
    try:
        if seal_ratio_pct is not None:
            cands.append(_bucket_lookup(float(seal_ratio_pct), PREMIUM_BY_SEAL_RATIO))
    except (TypeError, ValueError):
        pass
    try:
        if first_minutes is not None:
            cands.append(_bucket_lookup(float(first_minutes), PREMIUM_BY_FIRST_MINUTES))
    except (TypeError, ValueError):
        pass
    return max(cands) if cands else None


def apply_entry_cost_filter(cand_df, storage, data_date, cfg):
    """**统一入口**：推荐引擎与回测引擎都必须走这里。

    为什么必须共用一份实现：若只在 `RecommendEngine` 接入，回测就不会应用同一过滤，
    A/B 对比会得出「改动无效」的错误结论 —— 因为回测跑的其实是旧逻辑。
    项目里已经踩过同类坑（同一字段在两条路径口径不一致），因此这里强制收敛。

    返回 (保留的候选, 被剔除的候选)；未启用或无候选时原样返回。
    """
    if cand_df is None or len(cand_df) == 0:
        return cand_df, None
    if not bool(cfg.get("entry_cost.enabled", True)):
        return cand_df, None
    max_prem = float(cfg.get("entry_cost.max_premium", 3.0) or 3.0)
    return annotate_candidates(cand_df, storage, data_date, max_premium=max_prem)


def annotate_candidates(
    cand_df,
    storage,
    data_date,
    max_premium: float = 3.0,
):
    """给候选股标注预估入场成本，并返回 (保留的, 被剔除的)。

    只有**当日涨停**的候选才有成本风险（非涨停股实测系统性低开，是折价）。
    任何异常都只记日志并原样返回：入场过滤是增强项，
    绝不能因为一次查询失败而让当日选股整体失败。
    """
    import pandas as pd

    from astock.logger import get_logger

    logger = get_logger("features.entry_cost")
    if cand_df is None or cand_df.empty or "code" not in cand_df.columns:
        return cand_df, cand_df

    codes = [str(c) for c in cand_df["code"].unique()]
    placeholders = ", ".join("?" for _ in codes)
    try:
        # `dwd_limit_up` 只有封单额、没有成交额（实测列名已核对），
        # 而封单占比需要成交额，因此 join `dws_feature`。
        # 不用 `dws_limit_factor`：它是研究表（由 factor_ic.py 重建），
        # 实测停留在 9-22，不随 daily 更新，直接拿来做实时过滤会用到隔日数据。
        lu = storage.query_df(
            f"""SELECT l.code, l.boards, l.seal_money, l.first_time, f.amount
                FROM dwd_limit_up l
                LEFT JOIN dws_feature f ON f.code = l.code AND f.date = l.date
                WHERE l.date = ? AND l.code IN ({placeholders})""",
            [data_date, *codes],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("入场成本过滤跳过（涨停池查询失败）：%s", str(exc)[:120])
        return cand_df, cand_df.iloc[0:0]

    if lu.empty:
        return cand_df, cand_df.iloc[0:0]

    premiums: dict[str, float] = {}
    detail: dict[str, str] = {}
    for _, r in lu.iterrows():
        seal, amt = r["seal_money"], r["amount"]
        ratio = (float(seal) / float(amt) * 100) if (seal and amt) else None
        fmin = _first_minutes(r["first_time"])
        prem = estimate_open_premium(r["boards"], ratio, fmin)
        if prem is not None:
            premiums[str(r["code"])] = prem
            detail[str(r["code"])] = (
                f"{int(r['boards'] or 1)}板"
                + (f"／封单占比{ratio:.1f}%" if ratio is not None else "")
                + (f"／首封{fmin:+.0f}分" if fmin is not None else "")
            )

    out = cand_df.copy()
    out["entry_premium"] = out["code"].astype(str).map(premiums)
    keep = out["entry_premium"].isna() | (out["entry_premium"] <= max_premium)
    dropped = out[~keep].copy()
    if not dropped.empty:
        for code in dropped["code"].astype(str):
            logger.info(
                "剔除高成本候选 %s：预估次日开盘溢价 %.2f%%（%s）",
                code, premiums.get(code, 0.0), detail.get(code, ""),
            )
    return out[keep].reset_index(drop=True), dropped.reset_index(drop=True)
