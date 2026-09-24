# -*- coding: utf-8 -*-
"""昨日推荐回顾：把「上一批推荐的实际结果」整理成报告的第一个板块。

------------------------------------------------------------------
为什么需要它
------------------------------------------------------------------
原报告只讲「今天买什么」，不讲「上次买的东西怎么样了」——
这样的报告只能看、不能学：策略错了没有任何反馈闭环。
本模块把已结算的 `ads_review`（复盘的客观结果）与
`ads_review_attribution`（LLM 的原因归因）合成一份可读的回顾。

------------------------------------------------------------------
两个口径必须说清楚（容易混淆）
------------------------------------------------------------------
`ads_review` 里有两个容易混的收益：
  · `next_pct_chg` = 买入日收盘 / **前收** − 1  → 当日涨跌幅（与行情软件一致）
  · `ret`          = 买入日收盘 / **开盘** − 1  → **买入后实际到手收益**
系统口径是「T+1 开盘买入」，所以**实际收益是后者**；`result`(win/loss)
也按后者判定（见 reviewer.py 的 `day_return`）。
而「跑赢市场」必须用**同口径**比较（都是收盘对收盘），所以超额用 `next_pct_chg`。
两个都给出，避免用错。

------------------------------------------------------------------
效果分是什么，不是什么
------------------------------------------------------------------
它是**相对指标**：用于横向比较不同日期的选股质量（今天比昨天好还是差），
不是绝对评价标准。公式刻意做成可拆解的：
    效果分 = 0.4 × 胜率分 + 0.6 × 超额分
    胜率分 = 胜率(%)                                    (0~100)
    超额分 = clip(50 + 超额收益% × 15, 0, 100)          (±3.33% 触及上下限)
超额权重更高：胜率高但「赚小亏大」的选股没有意义。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from astock.logger import get_logger

logger = get_logger("review.recap")

# 基准指数偏好顺序（沪深300 更贴近主板+创业板的可交易样本；缺失时退回上证指数）
BENCH_PREFERENCE = ["sh.000300", "sh.000001"]


def _benchmark_return(storage, day) -> tuple[float | None, str | None]:
    """基准指数在 `day` 当天的涨跌幅(%)（收盘对收盘，与 next_pct_chg 同口径）。

    用 `LAG(close)` 现算而不是依赖 `preclose` 列：索引表未必有该列，
    缺列会让整个回顾板块失效，不值得为省一次窗口函数冒这个风险。
    任何异常都返回 (None, None)，调用方降级为「不显示超额」。
    """
    try:
        codes = storage.query_df(
            "SELECT DISTINCT code FROM dwd_index_bar WHERE date = ?", [day]
        )["code"].astype(str).tolist()
        if not codes:
            return None, None
        chosen = next((c for c in BENCH_PREFERENCE if c in codes), codes[0])
        df = storage.query_df(
            """
            SELECT date, close,
                   LAG(close) OVER (ORDER BY date) AS prev_close
            FROM dwd_index_bar WHERE code = ? AND date <= ?
            """,
            [chosen, day],
        )
        if df.empty:
            return None, None
        row = df[df["date"] == day]
        if row.empty:
            return None, None
        close, prev = float(row["close"].iloc[0]), row["prev_close"].iloc[0]
        if prev is None or pd.isna(prev) or float(prev) == 0:
            return None, chosen
        return round((close / float(prev) - 1) * 100, 2), chosen
    except Exception as exc:  # noqa: BLE001
        logger.warning("基准指数读取失败（本次不显示超额收益）：%s", str(exc)[:120])
        return None, None


def effect_score(win_rate: float | None, excess: float | None) -> dict[str, Any]:
    """选股效果分（0~100）及其拆解。见模块注释的公式说明。"""
    if win_rate is None:
        return {"score": None, "win_part": None, "excess_part": None}
    win_part = max(0.0, min(100.0, float(win_rate)))
    if excess is None:
        return {
            "score": round(win_part, 1),
            "win_part": round(win_part, 1),
            "excess_part": None,
            "note": "无基准数据，效果分仅由胜率构成",
        }
    excess_part = max(0.0, min(100.0, 50.0 + float(excess) * 15.0))
    return {
        "score": round(0.4 * win_part + 0.6 * excess_part, 1),
        "win_part": round(win_part, 1),
        "excess_part": round(excess_part, 1),
    }


def build(storage, on_date=None) -> dict[str, Any]:
    """构建「最近一批已结算推荐」的回顾数据。

    刻意用「最近一批已结算」而不是字面上的「昨天」：
    周末/节假日没有行情，硬取 T-1 会得到空结果并让整个板块消失。
    """
    try:
        day = on_date or storage.query_value(
            "SELECT MAX(next_date) FROM ads_review "
            "WHERE result IS NOT NULL AND result <> 'pending'"
        )
    except Exception as exc:  # noqa: BLE001
        # 截断要留足长度：DuckDB 的 Binder Error 关键信息（缺哪个列）在后面，
        # 只留 80 字会把真正有用的部分切掉、只剩一句"表没有列"。
        logger.warning("回顾板块读取失败：%s", str(exc)[:400])
        return {"available": False, "reason": f"复盘表读取失败：{str(exc)[:300]}"}
    if day is None:
        return {"available": False, "reason": "还没有已结算的推荐记录（首次运行属正常）"}

    # ⚠️ `ads_recommend` **没有** strategy_label 列（只有 strategy 与 primary_strategy）：
    # strategy 是"全部命中策略拼接"（展示用），primary_strategy 是得分最高的那个
    # （按策略统计必须用它，否则组合名会把统计口径搅乱）。
    df = storage.query_df(
        """
        SELECT v.rec_id, v.code, v.tier, v.rec_date, v.next_date,
               v.next_open, v.next_close, v.next_pct_chg,
               v.next_high_pct, v.next_low_pct, v.hold3_pct, v.result,
               r.name, r.final_score, r.strategy, r.primary_strategy,
               a.outcome, a.category, a.reason, a.lesson
        FROM ads_review v
        JOIN ads_recommend r ON r.rec_id = v.rec_id
        LEFT JOIN ads_review_attribution a ON a.rec_id = v.rec_id
        WHERE v.next_date = ?
        """,
        [day],
    )
    if df.empty:
        return {"available": False, "reason": f"{day} 没有已结算的推荐"}

    # 买入后实际收益（收盘/开盘 − 1）：系统按「T+1 开盘买入」计价，这是到手收益
    ret = (
        pd.to_numeric(df["next_close"], errors="coerce")
        / pd.to_numeric(df["next_open"], errors="coerce")
        - 1
    ) * 100
    df["ret"] = ret.round(2)
    df = df.sort_values("ret", ascending=False, na_position="last")

    total = len(df)
    wins = int((df["result"] == "win").sum())
    win_rate = round(wins / total * 100, 1) if total else None
    avg_ret = round(float(pd.to_numeric(df["ret"], errors="coerce").mean()), 2)
    avg_chg = round(float(pd.to_numeric(df["next_pct_chg"], errors="coerce").mean()), 2)

    bench_ret, bench_code = _benchmark_return(storage, day)
    excess = None if bench_ret is None else round(avg_chg - bench_ret, 2)
    score = effect_score(win_rate, excess)

    def _avg(series) -> float | None:
        s = pd.to_numeric(series, errors="coerce").dropna()
        return round(float(s.mean()), 2) if len(s) else None

    by_tier: dict[str, dict] = {}
    for tier, g in df.groupby("tier"):
        by_tier[str(tier)] = {
            "count": len(g),
            "win_rate": round(float((g["result"] == "win").mean() * 100), 1),
            "avg_return": _avg(g["ret"]),
        }
    by_strategy: dict[str, dict] = {}
    for strat, g in df.groupby("primary_strategy"):
        by_strategy[str(strat)] = {
            "count": len(g),
            "win_rate": round(float((g["result"] == "win").mean() * 100), 1),
            "avg_return": _avg(g["ret"]),
        }

    rows = []
    for _, r in df.iterrows():
        rows.append(
            {
                "code": str(r["code"]),
                "name": "" if r["name"] is None else str(r["name"]),
                "tier": str(r["tier"]),
                "strategy_label": "" if r["strategy"] is None else str(r["strategy"]),
                "primary_strategy": "" if r["primary_strategy"] is None else str(r["primary_strategy"]),
                "final_score": None if r["final_score"] is None else round(float(r["final_score"]), 1),
                "rec_date": str(r["rec_date"]),
                "next_date": str(r["next_date"]),
                "buy_price": None if r["next_open"] is None else round(float(r["next_open"]), 2),
                "close": None if r["next_close"] is None else round(float(r["next_close"]), 2),
                "ret": None if r["ret"] is None or pd.isna(r["ret"]) else float(r["ret"]),
                "day_chg": None if r["next_pct_chg"] is None else float(r["next_pct_chg"]),
                "max_gain": None if r["next_high_pct"] is None else float(r["next_high_pct"]),
                "max_dd": None if r["next_low_pct"] is None else float(r["next_low_pct"]),
                "hold3": None if r["hold3_pct"] is None else float(r["hold3_pct"]),
                "result": str(r["result"]),
                "category": None if r["category"] is None or pd.isna(r["category"]) else str(r["category"]),
                "reason": None if r["reason"] is None or pd.isna(r["reason"]) else str(r["reason"]),
                "lesson": None if r["lesson"] is None or pd.isna(r["lesson"]) else str(r["lesson"]),
            }
        )

    return {
        "available": True,
        "review_date": str(day),
        "rec_date": str(df["rec_date"].iloc[0]),
        "count": total,
        "wins": wins,
        "losses": int((df["result"] == "loss").sum()),
        "flats": int((df["result"] == "flat").sum()),
        "win_rate": win_rate,
        "avg_return": avg_ret,
        "avg_day_chg": avg_chg,
        "avg_max_gain": _avg(df["next_high_pct"]),
        "avg_max_dd": _avg(df["next_low_pct"]),
        "avg_hold3": _avg(df["hold3_pct"]),
        "benchmark_code": bench_code,
        "benchmark_return": bench_ret,
        "excess": excess,
        "effect_score": score,
        "by_tier": by_tier,
        "by_strategy": by_strategy,
        "attributed": int(df["category"].notna().sum()),
        "rows": rows,
    }
