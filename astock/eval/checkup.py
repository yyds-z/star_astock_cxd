# -*- coding: utf-8 -*-
"""策略体检：对任意策略跑一遍历史，用统一裁判给出可实现口径成绩与样本外判定。

**为什么必须有它**：`ads_backtest`（487 天回放）只记录**最终入选的 15 只**，
无法回答"某个策略本身准不准"—— 比如 turtle_trade 每天命中 76 只，
但回测表里只有被评分/配额选中的那几只。策略的去留必须基于它**自身**的成绩。

口径（全部来自 `astock/eval/judge.py`，此处不重算任何收益）：
  · 主口径 B：信号日次日**开盘**买 → 再次日**收盘**卖（T+1 下最早合法）
  · 次口径 A：信号日**收盘**买（须非封板）→ 次日收盘卖
  · 超额 = 同池等权、同口径基准；t 按**日**聚类
  · 观察期/验证期各半；判定门槛用 Bonferroni 校正（扫 8 个策略 → 门槛抬高）

判定规则（三者同时满足才算"通过"）：
  ① 验证期 t ≥ 门槛且为正　② 观察期与验证期同号　③ 样本 ≥ 60 个交易日
"""

from __future__ import annotations

from datetime import date as date_cls
from typing import Any

import pandas as pd

from astock.eval.judge import (
    attach_benchmark,
    bonferroni_t,
    executable,
    picks_with_returns,
    split_out_of_sample,
    summarise,
)
from astock.logger import get_logger

logger = get_logger("eval.checkup")

# 与 universe 默认口径一致（体检必须与生产同池，否则成绩不可比）
PANEL_SQL = """
SELECT f.*, s.name, s.board
FROM dws_feature f
JOIN dim_stock s ON s.code = f.code
WHERE f.date = ?
  AND s.board IN ('main', 'gem')
  AND COALESCE(f.is_st, FALSE) = FALSE
  AND (s.out_date IS NULL OR s.out_date > f.date)
  AND f.listed_days >= ?
  AND COALESCE(f.amount_ma20, 0) >= ?
  AND f.close >= ?
  AND f.volume > 0
"""


def load_day_panel(storage, d: date_cls, *, new_stock_days: int = 120,
                   min_amount: float = 5e7, min_price: float = 2.0) -> pd.DataFrame:
    """取某日的因子截面（已施加股票池过滤）。

    逐日取而不是一次性取全历史：全历史 250 万行 × 50 列进 pandas 要数百 MB，
    而逐日 ≈3500 行（单日查询约 50ms）。总耗时同量级，内存却恒定。
    """
    return storage.query_df(PANEL_SQL, [d, new_stock_days, min_amount, min_price])


def _eval_one(strategy, panel: pd.DataFrame, d: date_cls) -> pd.DataFrame | None:
    """在某日截面上跑一个策略，返回 (date, code, score) 或 None。

    镜像 `BaseStrategy.screen()` 的两处处理（否则体检口径会与生产不一致）：
    ① `require_col` 缺失的行先剔除；② 只保留 `strategy_score > 0`。
    """
    cur = panel
    if strategy.require_col in cur.columns:
        cur = cur[cur[strategy.require_col].notna()]
    if cur.empty:
        return None
    try:
        out = strategy.evaluate(cur.reset_index(drop=True))
    except Exception as exc:  # noqa: BLE001 - 单日单策略异常跳过，不中断体检
        logger.warning("[%s] %s 评估异常，跳过该日：%s", strategy.name, d, str(exc)[:100])
        return None
    if out is None or out.empty:
        return None
    out = out[out["strategy_score"] > 0]
    if out.empty:
        return None
    return pd.DataFrame({
        "date": d,
        "code": out["code"].astype(str),
        "score": out["strategy_score"].astype(float).values,
    })


def collect_hits(storage, strategies: list[Any], days: list[date_cls],
                 **panel_kw: Any) -> dict[str, pd.DataFrame]:
    """逐日加载**一次**截面，喂给全部策略 → {策略名: 命中表}。

    为什么要"每天只加载一次"：按策略分别加载会让同一天的截面被查 8 遍
    （实测 250 日 × 8 策略要多花 6 分钟），而生产流程本来就是
    "一天一个上下文、所有策略共享"（`StrategyContext` 的设计意图）。
    """
    frames: dict[str, list[pd.DataFrame]] = {s.name: [] for s in strategies}
    for i, d in enumerate(days, 1):
        panel = load_day_panel(storage, d, **panel_kw)
        if panel.empty:
            continue
        for s in strategies:
            got = _eval_one(s, panel, d)
            if got is not None:
                frames[s.name].append(got)
        if i % 60 == 0:
            logger.info("体检进度 %d/%d（%s）", i, len(days), d)
    empty = pd.DataFrame(columns=["date", "code", "score"])
    out: dict[str, pd.DataFrame] = {}
    for s in strategies:
        hits = pd.concat(frames[s.name], ignore_index=True) if frames[s.name] else empty
        out[s.name] = hits
        logger.info("[%s] 命中 %d 笔 / %d 日", s.name, len(hits),
                    hits["date"].nunique() if not hits.empty else 0)
    return out


def strategy_hits(storage, strategy, days: list[date_cls], **panel_kw: Any) -> pd.DataFrame:
    """单策略命中表（内部复用 `collect_hits`，保证两条路径同口径）。"""
    return collect_hits(storage, [strategy], days, **panel_kw)[strategy.name]


def score_strategy(hits: pd.DataFrame, bench: pd.DataFrame, label: str,
                   n_tests: int = 1) -> dict[str, Any]:
    """给一个命中表打分：两口径 + 样本外 + 判定。"""
    from astock.storage.db import get_storage

    st = get_storage()
    st.conn.register("_checkup_hits", hits[["date", "code"]])
    df = picks_with_returns(st, "SELECT date, code FROM _checkup_hits")
    df = attach_benchmark(df, bench)
    if df.empty:
        return {"label": label, "days": 0, "verdict": "无样本"}

    out: dict[str, Any] = {"label": label, "picks": int(len(df))}
    for kou in ("a", "b"):
        exe = executable(df, kou)
        s = summarise(exe, kou)
        obs, val = split_out_of_sample(exe)
        s_obs, s_val = summarise(obs, kou), summarise(val, kou)
        out[kou] = {**s, "obs_t": s_obs.get("t"), "val_t": s_val.get("t"),
                    "obs_ex": s_obs.get("excess"), "val_ex": s_val.get("excess")}

    b = out["b"]
    th = bonferroni_t(n_tests, b.get("days", 0))
    ok_stat = (b.get("val_t") is not None and b["val_t"] >= th
               and (b.get("val_ex") or 0) > 0)
    same_sign = (b.get("obs_t") is not None and b.get("val_t") is not None
                 and (b["obs_t"] > 0) == (b["val_t"] > 0))
    enough = b.get("days", 0) >= 60
    out["threshold"] = th
    out["pass"] = bool(ok_stat and same_sign and enough)
    out["verdict"] = ("通过" if out["pass"] else
                      "样本不足" if not enough else
                      "不通过（样本外不显著）" if not ok_stat else
                      "不通过（两期不同号）")
    return out


def format_report(results: list[dict[str, Any]], n_tests: int) -> str:
    """体检报告（等宽表，报告与命令行共用）。"""
    lines = [
        "策略体检（可实现口径；主口径 = 信号日次日开盘买 → 再次日收盘卖）",
        f"多重检验门槛：扫 {n_tests} 个策略 ⇒ 验证期 t ≥ {bonferroni_t(n_tests, 240)}",
        "",
        f"{'策略':<26}{'笔数':>7}{'只/日':>7}{'超额':>9}{'t':>7}"
        f"{'观察t':>7}{'验证t':>7}{'净值':>8}  判定",
        "-" * 96,
    ]
    for r in results:
        b = r.get("b") or {}
        if not b.get("days"):
            lines.append(f"{r['label']:<26}{'—':>7}{'—':>7}{'—':>9}{'—':>7}"
                         f"{'—':>7}{'—':>7}{'—':>8}  {r.get('verdict', '')}")
            continue
        lines.append(
            f"{r['label']:<26}{b.get('picks', 0):>7}{b.get('per_day', 0):>7.1f}"
            f"{(b.get('excess') or 0):>+8.3f}%{(b.get('t') or 0):>7.2f}"
            f"{(b.get('obs_t') or 0):>7.2f}{(b.get('val_t') or 0):>7.2f}"
            f"{b.get('nav', 0):>8.2f}  {r.get('verdict', '')}"
        )
    lines.append("-" * 96)
    lines.append("注：超额为正才可能是真信号；t 用**日度**序列算（按笔算会把 t 抬高数倍）。")
    return "\n".join(lines)
