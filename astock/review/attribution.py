# -*- coding: utf-8 -*-
"""复盘归因（LLM）：把「涨了/跌了」变成「为什么」。

为什么需要它：
`Reviewer` 只算出结果（T+1 收益、胜率），但**不解释原因**。
没有原因，策略迭代就只能靠猜：胜率下降时无法判断是
「市场环境变了」还是「选股逻辑失效」——这两者的应对完全相反
（前者调权重，后者改策略）。

设计取舍：
1. **类别固定成枚举**，不让模型自由发挥。自由文本无法聚合，
   而聚合统计（哪类失败最多）才是迭代的依据。
2. 只对**已有实际结果**的记录归因，不做预测。
3. 每条只喂压缩后的指标，不喂原始 K 线 —— 与全系统的 token 控制策略一致。
4. 归因失败（超时/预算耗尽/JSON 解析失败）只跳过该条，不阻断流程。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pandas as pd

from astock.logger import get_logger
from astock.storage.db import Storage, get_storage

logger = get_logger("review.attribution")

# 固定类别枚举。key 存入库、中文用于展示。
CATEGORIES: dict[str, str] = {
    "theme": "题材兑现（热点延续）",
    "emotion": "情绪退潮（整体走弱）",
    "gap_down": "高开回落（买点被套）",
    "follow": "跟风失败（板块未接力）",
    "stock": "个股独立行情（与策略无关）",
    "market": "大盘拖累",
    "entry": "买点不利（位置偏高）",
    "other": "其它",
}

OUTCOMES = {"success": "盈利", "failure": "亏损", "flat": "基本走平"}

SYSTEM_PROMPT = (
    "你是 A 股短线复盘的归因助手。只输出 JSON，不要任何解释文字。\n"
    "你需要根据一条推荐记录的实际结果，判断其成败并归类原因。\n"
    "类别必须从给定枚举中选择，不要自创类别。"
)


def _pick(value: Any, default: str = "-") -> str:
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        return default
    text = str(value).strip()
    return text or default


def build_prompt(row: dict[str, Any]) -> str:
    """构造压缩后的归因请求（只给已算好的指标）。"""
    cats = " / ".join(f"{k}={v}" for k, v in CATEGORIES.items())

    def pct(key: str) -> str:
        v = row.get(key)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return "-"
        try:
            return f"{float(v):+.2f}%"
        except (TypeError, ValueError):
            return "-"

    reasons = _pick(row.get("reasons"))
    detail = _pick(row.get("score_detail"))
    if len(reasons) > 160:
        reasons = reasons[:160] + "…"
    if len(detail) > 200:
        detail = detail[:200] + "…"

    return f"""推荐记录：
档位 {_pick(row.get('tier'))}　主策略 {_pick(row.get('primary_strategy') or row.get('strategy'))}
市场状态 {_pick(row.get('market_label'))}
买入价（次日开盘）相对前收盘 {pct('open_premium')}
买入后当日收益 {pct('ret1')}（相对买入价）　当日涨跌幅 {pct('next_pct_chg')}（相对前收）
最大浮盈 {pct('next_high_pct')}　最大浮亏 {pct('next_low_pct')}
推荐理由 {reasons}
评分拆解 {detail}

可选类别：{cats}

请输出 JSON：
{{"outcome":"success|failure|flat","category":"上面某个英文 key","reason":"20字以内的原因","lesson":"20字以内、可执行的改进"}}"""


class Attributor:
    """复盘归因器。"""

    def __init__(self, storage: Storage | None = None, llm=None) -> None:
        self.storage = storage or get_storage()
        self._llm = llm

    @property
    def llm(self):
        if self._llm is None:
            from astock.llm.client import LLMClient

            self._llm = LLMClient(storage=self.storage)
        return self._llm

    # ---------------- 待归因 ----------------
    def pending(self, limit: int = 50) -> pd.DataFrame:
        """已复盘但尚未归因的记录。"""
        return self.storage.query_df(
            """
            SELECT
                v.rec_id, v.code, v.next_date, v.result,
                v.next_open, v.next_close, v.next_pct_chg,
                v.next_high_pct, v.next_low_pct,
                r.tier, r.primary_strategy, r.strategy, r.market_label,
                r.reasons, r.score_detail
            FROM ads_review v
            JOIN ads_recommend r ON r.rec_id = v.rec_id
            LEFT JOIN ads_review_attribution a ON a.rec_id = v.rec_id
            WHERE a.rec_id IS NULL
            ORDER BY v.next_date DESC
            LIMIT ?
            """,
            [limit],
        )

    @staticmethod
    def _buy_return(row: dict[str, Any]) -> float | None:
        """买入后当日收益 = 买入日收盘 / 买入价 - 1。

        这个值**没有落库**（ads_review 只存了 next_pct_chg，那是相对前收的涨跌幅），
        但它是判断「买点好不好」最直接的指标，必须现算。
        """
        buy = row.get("next_open")
        close = row.get("next_close")
        try:
            if buy is None or close is None or pd.isna(buy) or pd.isna(close):
                return None
            if not float(buy):
                return None
            return (float(close) / float(buy) - 1) * 100
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    @staticmethod
    def _open_premium(row: dict[str, Any]) -> float | None:
        """次日开盘溢价 = 买入价 / 前收 - 1（追高的核心成本项）。

        ads_review 里没有存「前收」，只存了 `next_pct_chg`（买入日收盘 / 前收 - 1），
        因此用 `next_close / (1 + next_pct_chg/100)` 反推前收。
        直接拿 next_open / next_close 是错的 —— 那是「当天开盘到收盘」的日内涨跌，
        与「相对前收的高开幅度」是两个概念。
        """
        buy = row.get("next_open")
        close = row.get("next_close")
        chg = row.get("next_pct_chg")
        for v in (buy, close, chg):
            if v is None:
                return None
            try:
                if pd.isna(v):
                    return None
            except (TypeError, ValueError):
                return None
        try:
            preclose = float(close) / (1 + float(chg) / 100)
            if not preclose:
                return None
            return (float(buy) / preclose - 1) * 100
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    # ---------------- 主流程 ----------------
    def run(self, limit: int = 50) -> dict[str, Any]:
        """对尚未归因的复盘记录逐条归因。幂等：已归因的不再处理。"""
        df = self.pending(limit=limit)
        if df.empty:
            return {"pending": 0, "attributed": 0, "failed": 0}

        if not self.llm.enabled:
            return {
                "pending": len(df),
                "attributed": 0,
                "failed": 0,
                "skipped_reason": "LLM 未启用（.env 中设置 LLM_ENABLED=true 与 DEEPSEEK_API_KEY）",
            }

        attributed = 0
        failed = 0
        for rec in df.to_dict(orient="records"):
            rec["open_premium"] = self._open_premium(rec)
            rec["ret1"] = self._buy_return(rec)
            data = self.llm.chat_json(SYSTEM_PROMPT, build_prompt(rec))
            if not data:
                failed += 1
                continue
            category = str(data.get("category") or "").strip()
            if category not in CATEGORIES:
                # 模型偶尔会自创类别（如 "gap_down_risk"），
                # 归到 other 并留原值，既不污染统计也不丢信息
                raw = category or "空"
                category = "other"
                logger.info("[%s] 类别不在枚举内（%s），归入 other", rec["rec_id"], raw)
            outcome = str(data.get("outcome") or "").strip().lower()
            if outcome not in OUTCOMES:
                outcome = "flat"

            self.storage.execute(
                "INSERT OR REPLACE INTO ads_review_attribution "
                "(rec_id, outcome, category, reason, lesson, model, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    rec["rec_id"],
                    outcome,
                    category,
                    str(data.get("reason") or "")[:120],
                    str(data.get("lesson") or "")[:120],
                    getattr(self.llm, "model", ""),
                    pd.Timestamp.now().to_pydatetime(),
                ],
            )
            attributed += 1

        logger.info("复盘归因完成：%d 条成功，%d 条失败", attributed, failed)
        return {"pending": len(df), "attributed": attributed, "failed": failed}

    # ---------------- 统计 ----------------
    def summary(self, days: int = 90) -> dict[str, Any]:
        """按失败/成功原因聚合，回答「哪类问题最值得修」。"""
        since = date.today() - pd.Timedelta(days=days)
        df = self.storage.query_df(
            """
            SELECT a.category, a.outcome, v.next_pct_chg, v.result
            FROM ads_review_attribution a
            JOIN ads_review v ON v.rec_id = a.rec_id
            WHERE v.next_date >= ?
            """,
            [since.date() if hasattr(since, "date") else since],
        )
        if df.empty:
            return {"count": 0}
        grouped = df.groupby("category").agg(
            count=("next_pct_chg", "size"),
            avg_ret=("next_pct_chg", "mean"),
            win=("next_pct_chg", lambda s: (s > 0).mean() * 100),
        )
        grouped = grouped.sort_values("count", ascending=False)
        return {
            "count": int(len(df)),
            "by_category": {
                CATEGORIES.get(idx, idx): {
                    "count": int(r["count"]),
                    "avg_return": round(float(r["avg_ret"]), 2) if pd.notna(r["avg_ret"]) else None,
                    "win_rate": round(float(r["win"]), 1),
                }
                for idx, r in grouped.iterrows()
            },
            "outcomes": df["outcome"].value_counts().to_dict(),
        }
