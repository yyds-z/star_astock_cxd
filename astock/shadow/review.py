# -*- coding: utf-8 -*-
"""影子信号复盘（归因）：把「涨了/跌了」变成「为什么」，供人工理解与校正。

为什么单独写，而不恢复已删的 `astock/review`：
  旧复盘的对象是 `ads_recommend`（主链路 15 只候选），那套候选已随主链路删除。
  影子的结算本来就在 `ads_shadow_pick` 内完成（`ret1/3/5` 与可实现口径 `exec_*`），
  所以复盘只需在这张表上做聚合 + 归因，**不引入任何新的数据依赖**。

三条设计纪律：
1. **一律按可实现口径（exec_d1 / exec_bench）复盘。** 旧口径 ret1（信号日收盘买）
   的收益 100% 来自当日已封板、收盘买不进的票（历史占 15.3%）。用 ret1 复盘
   等于把幻影收益当成经验写进教训 —— 旧口径只作为**对照**同时给出，
   用途恰恰是提醒两者差多少。
2. 归因只喂「聚合指标 + 涨跌各前 5 只」（复用 `llm.prompts.build_recap_prompt`）：
   明细越长，模型越容易挑个别股票讲故事而忽略整体。
3. LLM 不可用 / 超预算 → 退回本地模板（只陈述事实，不假装做归因）。
4. **冻结期纪律**：本模块只产出「观察与记录」，不产出可执行参数。
   `adjust` 字段在提示词里被要求一律写"维持现状"。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pandas as pd

from astock.llm.client import LLMClient
from astock.llm.prompts import (
    RECAP_SYSTEM_PROMPT,
    build_recap_prompt,
    template_recap_summary,
)
from astock.logger import get_logger
from astock.storage.db import Storage, get_storage

logger = get_logger("shadow.review")


class ShadowReviewer:
    """影子信号复盘器：聚合 → 归因 → 落库。"""

    def __init__(self, storage: Storage | None = None,
                 llm: LLMClient | None = None) -> None:
        self.storage = storage or get_storage()
        self.llm = llm or LLMClient(storage=self.storage)

    # ---------------- 聚合 ----------------
    def collect(self, days: int = 20) -> dict[str, Any]:
        """聚合最近 `days` 个自然日内已结算的影子候选（**排除当日封板的不可买样本**）。

        排除封板是与引擎一致的"可买性"口径：那些票在信号日收盘买不进，
        把它们算进复盘会让结论建立在买不到的收益上。
        """
        rows = self.storage.query_df(
            f"""
            SELECT CAST(date AS VARCHAR) AS date, code, name,
                   signal_score, vol_ratio, amount_ma20,
                   ret1, exec_d1, exec_bench, hit_limit_up
            FROM ads_shadow_pick
            WHERE exec_d1 IS NOT NULL
              AND (is_sealed IS NULL OR is_sealed = FALSE)
              AND date >= (SELECT MAX(date) FROM ads_shadow_pick)
                          - INTERVAL {int(days)} DAY
            ORDER BY exec_d1 DESC
            """
        )
        if rows.empty:
            return {"available": False, "reason": "窗口内没有已结算的影子候选"}

        n = len(rows)
        win = int((rows["exec_d1"] > 0).sum())
        avg_exec = float(rows["exec_d1"].mean())
        bench = rows["exec_bench"].dropna()
        avg_bench = float(bench.mean()) if not bench.empty else None
        excess = (avg_exec - avg_bench) if avg_bench is not None else None
        old = rows["ret1"].dropna()
        avg_old = float(old.mean()) if not old.empty else None
        hit_zt = int(rows["hit_limit_up"].fillna(False).sum())

        def reason(r: Any) -> str:
            sc = r.get("signal_score")
            vr = r.get("vol_ratio")
            bits = []
            if pd.notna(sc):
                bits.append(f"信号分 {float(sc):.0f}")
            if pd.notna(vr):
                bits.append(f"量比 {float(vr):.2f}")
            amt = r.get("amount_ma20")
            if pd.notna(amt):
                bits.append(f"前20日均额 {float(amt) / 1e8:.2f} 亿")
            return "；".join(bits) or "—"

        detail = [
            {
                "code": str(r["code"]),
                "name": str(r.get("name") or ""),
                "date": str(r["date"]),
                "ret": None if pd.isna(r["exec_d1"]) else round(float(r["exec_d1"]), 3),
                "ret_old": None if pd.isna(r["ret1"]) else round(float(r["ret1"]), 3),
                "hit_limit_up": bool(r["hit_limit_up"]) if pd.notna(r["hit_limit_up"]) else False,
                "signal_score": None if pd.isna(r["signal_score"]) else round(float(r["signal_score"]), 1),
                "reason": reason(r),
            }
            for _, r in rows.iterrows()
        ]

        recap: dict[str, Any] = {
            "review_date": str(self.storage.latest_trade_date()),
            "rec_date": f"{rows['date'].min()} ~ {rows['date'].max()}",
            "signal_days": int(rows["date"].nunique()),
            "count": n,
            "win_rate": round(win / n * 100, 1),
            # 可实现口径（唯一判据）
            "avg_return": round(avg_exec, 3),
            "benchmark_return": None if avg_bench is None else round(avg_bench, 3),
            "excess": None if excess is None else round(excess, 3),
            # 旧口径仅作对照（差额即"买不进的幻影"）
            "avg_return_old": None if avg_old is None else round(avg_old, 3),
            "hit_zt": hit_zt,
            "avg_day_chg": None,
            "effect_score": None,
            "avg_max_gain": None,
            "avg_max_dd": None,
            "by_tier": None,
            "by_strategy": None,
            "rows": detail,
        }
        return {"available": True, "recap": recap}

    # ---------------- 归因 ----------------
    def run(self, days: int = 20, use_llm: bool | None = None) -> dict[str, Any]:
        """聚合 → 归因（LLM 优先，失败退回模板）→ 落库 `ads_shadow_review`。"""
        got = self.collect(days=days)
        if not got.get("available"):
            logger.info("影子复盘跳过：%s", got.get("reason"))
            return {"available": False, "reason": got.get("reason")}

        recap = got["recap"]
        want_llm = self.llm.enabled if use_llm is None else bool(use_llm)
        comment = None
        used_llm = False
        if want_llm:
            comment = self.llm.chat_json(RECAP_SYSTEM_PROMPT, build_recap_prompt(recap))
            used_llm = comment is not None
        if not used_llm:
            comment = template_recap_summary(recap)
        comment = comment or {}

        top = recap["rows"]
        gainers = [r for r in top if (r["ret"] or 0) > 0][:5]
        losers = [r for r in top if (r["ret"] or 0) <= 0][:5]
        row = {
            "review_date": self.storage.latest_trade_date(),
            "window_from": recap["rec_date"].split(" ~ ")[0],
            "window_to": recap["rec_date"].split(" ~ ")[-1],
            "signal_days": recap["signal_days"],
            "picks": recap["count"],
            "win_rate": recap["win_rate"],
            "avg_exec_d1": recap["avg_return"],
            "avg_bench": recap["benchmark_return"],
            "excess": recap["excess"],
            "avg_ret1_old": recap["avg_return_old"],
            "hit_zt": recap["hit_zt"],
            "llm_used": used_llm,
            "verdict": str(comment.get("verdict") or ""),
            "wins": str(comment.get("wins") or ""),
            "losses": str(comment.get("losses") or ""),
            "lesson": str(comment.get("lesson") or ""),
            "adjust": str(comment.get("adjust") or ""),
            "detail": json.dumps({"gainers": gainers, "losers": losers},
                                 ensure_ascii=False),
            "created_at": datetime.now(),
        }
        n = self.storage.upsert_df(pd.DataFrame([row]), "ads_shadow_review")
        logger.info(
            "影子复盘完成：%d 只（%d 个信号日）｜可实现超额 %s%%｜%s",
            recap["count"], recap["signal_days"], recap["excess"],
            "LLM" if used_llm else "本地模板",
        )
        return {"available": True, "rows": n, **row}

    # ---------------- 查询 ----------------
    def latest(self) -> dict[str, Any] | None:
        df = self.storage.query_df(
            "SELECT * FROM ads_shadow_review ORDER BY review_date DESC LIMIT 1"
        )
        if df.empty:
            return None
        out = df.iloc[0].to_dict()
        for k in ("review_date", "window_from", "window_to"):
            if out.get(k) is not None:
                out[k] = str(out[k])[:10]
        if out.get("created_at") is not None:
            out["created_at"] = str(out["created_at"])[:19]
        return out
