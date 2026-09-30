# -*- coding: utf-8 -*-
"""策略表现统计（Skill 库的「历史成功率」数据源）。

数据有两个来源，分开记录、互不覆盖：

| 来源 | 表 | 含义 |
|---|---|---|
| `backtest` | `ads_backtest` | 历史逐日回放的**样本外**表现，用于判断策略是否有效 |
| `live` | `ads_review` | 系统实际跑出来的推荐在次日的真实表现（纸面跟踪） |

为什么必须分开：回测口径是「全市场逐日取 Top-N」，而 live 口径是
「市场状态加权后的最终推荐」。两者样本分布不同，混在一起看会互相污染。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from astock.backtest.metrics import stats_of
from astock.logger import get_logger
from astock.storage.db import Storage, get_storage

logger = get_logger("skills.performance")

# 判定门槛（与 backtest/report.py 保持一致）
MIN_SAMPLE = 30
GOOD_WIN_RATE = 50.0
GOOD_AVG_RET = 0.0


def _verdict(stats: dict[str, Any], source: str = "backtest") -> tuple[str, str]:
    """给出策略判定：effective / ineffective / insufficient。"""
    n = int(stats.get("count") or 0)
    if n < MIN_SAMPLE:
        return "insufficient", f"样本 {n} 条 < {MIN_SAMPLE} 条，不足以判定"

    avg = stats.get("avg_ret1")
    win = stats.get("win_rate")
    if avg is None:
        return "insufficient", "无有效收益数据"

    if avg <= GOOD_AVG_RET:
        return "ineffective", f"平均收益 {avg}% ≤ 0，建议排查条件或淘汰"
    if (win or 0) < GOOD_WIN_RATE:
        return "watching", f"平均收益 {avg}% 为正，但胜率仅 {win}% —— 低胜率高赔率型，需控仓位"
    return "effective", f"样本 {n} 条，胜率 {win}%、平均 {avg}%，表现达标"


def from_backtest(storage: Storage, run_id: str | None = None) -> dict[str, dict[str, Any]]:
    """从最近一次回测结果统计每个策略的表现。"""
    latest = run_id or storage.query_value("SELECT MAX(run_id) FROM ads_backtest")
    if latest is None:
        return {}

    df = storage.query_df("SELECT * FROM ads_backtest WHERE run_id = ?", [latest])
    if df.empty:
        return {}

    out: dict[str, dict[str, Any]] = {}
    for strategy, group in df.groupby("strategy", dropna=True):
        stats = stats_of(group)
        verdict, reason = _verdict(stats)

        # 分市场状态表现：用于验证 apply_when / avoid_when 是否成立
        by_state: dict[str, Any] = {}
        for state, sub in group.groupby("market_state", dropna=True):
            s = stats_of(sub)
            by_state[str(state)] = {
                "count": s.get("count"),
                "win_rate": s.get("win_rate"),
                "avg_ret1": s.get("avg_ret1"),
            }

        out[str(strategy)] = {
            "source": "backtest",
            "run_id": str(latest),
            "period": {
                "start": str(pd.to_datetime(df["data_date"]).min().date()),
                "end": str(pd.to_datetime(df["data_date"]).max().date()),
                "trading_days": int(df["data_date"].nunique()),
            },
            "sample_size": stats.get("count"),
            "win_rate": stats.get("win_rate"),
            "avg_ret1": stats.get("avg_ret1"),
            "median_ret1": stats.get("median_ret1"),
            "avg_ret3": stats.get("avg_ret3"),
            "avg_ret5": stats.get("avg_ret5"),
            "avg_ret10": stats.get("avg_ret10"),
            "win_rate_ret3": stats.get("win_rate_ret3"),
            "profit_loss_ratio": stats.get("profit_loss_ratio"),
            "avg_max_gain": stats.get("avg_max_gain"),
            "avg_max_dd": stats.get("avg_max_dd"),
            "by_market_state": by_state,
            "verdict": verdict,
            "verdict_reason": reason,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
    return out


def from_review(storage: Storage) -> dict[str, dict[str, Any]]:
    """从实际推荐复盘结果统计（纸面跟踪）。

    容错：若 ads_recommend 还是旧表结构（缺 primary_strategy 列），
    退回用拼接后的 strategy 归因，而不是让整个 Skill 库同步失败。
    """
    try:
        df = storage.query_df(
            """
            SELECT COALESCE(NULLIF(r.primary_strategy, ''), r.strategy) AS strategy, v.*
            FROM ads_review v
            JOIN ads_recommend r ON r.rec_id = v.rec_id
            """
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("实盘跟踪统计失败（回退到旧口径）：%s", str(exc)[:120])
        df = storage.query_df(
            """
            SELECT r.strategy AS strategy, v.*
            FROM ads_review v
            JOIN ads_recommend r ON r.rec_id = v.rec_id
            """
        )
    if df.empty:
        return {}

    out: dict[str, dict[str, Any]] = {}
    for strategy, group in df.groupby("strategy", dropna=True):
        wins = int((group["result"] == "win").sum())
        losses = int((group["result"] == "loss").sum())
        ret = pd.to_numeric(group["next_pct_chg"], errors="coerce").dropna()
        out[str(strategy)] = {
            "count": len(group),
            "wins": wins,
            "losses": losses,
            "flats": int((group["result"] == "flat").sum()),
            "win_rate": round(wins / len(group) * 100, 1) if len(group) else None,
            "avg_ret1": round(float(ret.mean()), 2) if len(ret) else None,
            "median_ret1": round(float(ret.median()), 2) if len(ret) else None,
            "first_date": str(pd.to_datetime(group["rec_date"]).min().date()),
            "last_date": str(pd.to_datetime(group["rec_date"]).max().date()),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
    return out


def build(storage: Storage | None = None) -> dict[str, dict[str, Any]]:
    """合并两种来源，返回 {strategy: performance}。

    两个来源各自独立容错：任何一个统计失败都不应阻断 Skill 库落盘，
    否则用户会因为一个统计问题拿不到全部档案。
    """
    storage = storage or get_storage()

    try:
        backtest = from_backtest(storage)
    except Exception as exc:  # noqa: BLE001
        logger.warning("回测统计失败，本次跳过：%s", str(exc)[:120])
        backtest = {}

    try:
        live = from_review(storage)
    except Exception as exc:  # noqa: BLE001
        logger.warning("实盘统计失败，本次跳过：%s", str(exc)[:120])
        live = {}

    names = set(backtest) | set(live)
    out: dict[str, dict[str, Any]] = {}
    for name in names:
        out[name] = {
            "backtest": backtest.get(name),
            "live": live.get(name),
        }
    return out
