# -*- coding: utf-8 -*-
"""回测统计指标。

收益口径（2026-09-24 P0 修正）：
- 买入价 = 推荐日次一交易日开盘。
- **裁判口径 = `ret_exit_d1c`**（买入次日收盘卖）与 `ret_exit_d1o`（次日开盘卖）
  —— 这是 T+1 制度下**最早合法**的两个卖出点，是唯一能指导实盘的口径。
- `ret1`（买入日当天收盘卖）在 A 股**不可实现**，只作诊断字段保留：
  它系统性高估收益（吃到日内回升、漏掉强制隔夜），此前所有 A/B 结论
  建立在它之上，全部作废重看。
- `ret3/5/10` 本身合法（买入日之后卖出），保留。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

RET_COLS = ["ret1", "ret_exit_d1o", "ret_exit_d1c", "ret3", "ret5", "ret10"]
# 裁判口径：headline 统计（胜率/盈亏比等）用它算，而不是 ret1
EXIT_COL = "ret_exit_d1c"


def _num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


def _r(value: Any, digits: int = 2) -> float | None:
    """安全取整：NaN / inf / 空值统一返回 None，避免报告里出现 nan。

    （样本量小时盈亏比很容易算出 nan —— 例如某一组从来没有亏损样本。）
    """
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not pd.notna(f) or f in (float("inf"), float("-inf")):
        return None
    return round(f, digits)


def _stats(g: pd.DataFrame) -> dict[str, Any]:
    """计算一组推荐的统计指标。"""
    if g is None or g.empty:
        return {"count": 0}

    n = len(g)
    # headline 统计用可实现口径（EXIT_COL）：这是"下次再跑还能不能赚钱"的问题，
    # 用 ret1 回答它 = 用一个不可实现的卖点点差回答，此前所有"胜率 49%"均由此而来。
    r = _num(g[EXIT_COL]).dropna() if EXIT_COL in g.columns else pd.Series(dtype="float64")
    wins = r[r > 0]
    losses = r[r < 0]
    r1 = _num(g["ret1"]).dropna() if "ret1" in g.columns else pd.Series(dtype="float64")

    out: dict[str, Any] = {
        "count": n,
        "win_rate": _r(len(wins) / len(r) * 100, 1) if len(r) else None,
        f"avg_{EXIT_COL}": _r(r.mean()) if len(r) else None,
        "median_ret1": _r(r1.median()) if len(r1) else None,
        "std_ret1": _r(r1.std()) if len(r1) > 1 else None,
        "avg_win": _r(wins.mean()) if len(wins) else None,
        "avg_loss": _r(losses.mean()) if len(losses) else None,
        "profit_loss_ratio": (
            _r(abs(float(wins.mean()) / float(losses.mean())))
            if len(wins) and len(losses) and float(losses.mean()) != 0
            else None
        ),
    }

    for col in RET_COLS:  # 含 ret1（诊断口径）—— 保留它的均值/胜率以便对照
        s = _num(g[col]).dropna() if col in g.columns else pd.Series(dtype="float64")
        out[f"avg_{col}"] = _r(s.mean()) if len(s) else None
        out[f"win_rate_{col}"] = _r(float((s > 0).mean() * 100), 1) if len(s) else None

    gain = _num(g["max_gain"]).dropna()
    dd = _num(g["max_dd"]).dropna()
    out["avg_max_gain"] = _r(gain.mean()) if len(gain) else None
    out["avg_max_dd"] = _r(dd.mean()) if len(dd) else None
    return out


def stats_of(group: pd.DataFrame) -> dict[str, Any]:
    """公开入口：计算一组信号的统计指标（供 Skill 库等复用同一口径）。"""
    return _stats(group)


def group_table(df: pd.DataFrame, by: str, label_map: dict[str, str] | None = None) -> pd.DataFrame:
    """按某列分组统计，返回可直接渲染成表格的 DataFrame。"""
    if df is None or df.empty or by not in df.columns:
        return pd.DataFrame()

    rows = []
    for key, g in df.groupby(by, dropna=False):
        stats = _stats(g)
        stats[by] = label_map.get(str(key), str(key)) if label_map else str(key)
        rows.append(stats)

    out = pd.DataFrame(rows)
    if out.empty:
        return out
    cols = [by, "count", "win_rate", "avg_ret_exit_d1c", "avg_ret_exit_d1o",
            "avg_ret1", "avg_ret3", "avg_ret5", "avg_ret10",
            "profit_loss_ratio", "avg_max_gain", "avg_max_dd"]
    cols = [c for c in cols if c in out.columns]
    # 排序键也是裁判口径：谁该被推到台前，由可实现收益决定
    return out[cols].sort_values("avg_ret_exit_d1c", ascending=False).reset_index(drop=True)


def summarise(df: pd.DataFrame) -> dict[str, Any]:
    """整体统计 + 多维度分组统计。"""
    if df is None or df.empty:
        return {"count": 0}

    tier_label = {"short": "短线", "swing": "波段", "value": "价值"}
    result: dict[str, Any] = {
        "overall": _stats(df),
        "by_tier": group_table(df, "tier", tier_label),
        "by_strategy": group_table(df, "strategy"),
        "by_market_state": group_table(df, "market_state"),
        "by_phase": group_table(df, "phase", {"observe": "观察期", "validate": "验证期"}),
        "dates": {
            "start": str(pd.to_datetime(df["data_date"]).min().date()),
            "end": str(pd.to_datetime(df["data_date"]).max().date()),
            "days": int(df["data_date"].nunique()),
        },
    }

    # 按月看稳定性
    month = pd.to_datetime(df["data_date"]).dt.strftime("%Y-%m")
    tmp = df.assign(month=month)
    result["by_month"] = group_table(tmp, "month")

    # 多策略共振是否真的更好（验证「共振加分」这个直觉）
    if "hit_count" in df.columns:
        bucket = df["hit_count"].fillna(1).astype(int).map(
            lambda n: "单策略" if n <= 1 else ("2策略共振" if n == 2 else "3策略及以上")
        )
        result["by_multi_hit"] = group_table(df.assign(hit_bucket=bucket), "hit_bucket")

    return result


def phase_split(df: pd.DataFrame, ratio: float = 0.6) -> pd.DataFrame:
    """按时间切分观察期 / 验证期。

    规则型策略本身没有拟合过程，但仍需检查「前半段有效、后半段失效」的情况 ——
    这说明参数是在特定市场环境下偶然奏效，而不是真的稳健。
    """
    if df is None or df.empty:
        return df
    out = df.copy()
    out["data_date"] = pd.to_datetime(out["data_date"])
    dates = sorted(out["data_date"].unique())
    if len(dates) < 2:
        out["phase"] = "observe"
        return out
    cut_index = max(1, min(len(dates) - 1, int(len(dates) * ratio)))
    cutoff = dates[cut_index]
    out["phase"] = (out["data_date"] >= cutoff).map({True: "validate", False: "observe"})
    return out
