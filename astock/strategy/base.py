# -*- coding: utf-8 -*-
"""策略基类与上下文。

与 Sequoia-X 的 `run() -> list[str]` 相比，本系统的策略返回的是
带「分值 + 理由」的 Candidate，这是后续评分融合与复盘归因的基础。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from astock.config import Config, get_config
from astock.features.indicators import to_float, to_list, to_str
from astock.logger import get_logger

logger = get_logger("strategy.base")

TIER_LABEL = {"short": "短线", "swing": "波段", "value": "价值"}

# 策略异常登记表。
# 单个策略异常会被吞掉以保证整体流程不中断，但这意味着**该策略当天的信号全部丢失**。
# 静默丢数据是最危险的一类缺陷，因此这里显式累计，供回测/日报在结尾提示。
STRATEGY_ERRORS: dict[str, dict[str, Any]] = {}


def record_strategy_error(name: str, exc: Exception, context: str = "") -> None:
    info = STRATEGY_ERRORS.setdefault(name, {"count": 0, "last": "", "last_context": ""})
    info["count"] += 1
    info["last"] = f"{type(exc).__name__}: {str(exc)[:180]}"
    info["last_context"] = context


def reset_strategy_errors() -> None:
    STRATEGY_ERRORS.clear()


def strategy_error_summary() -> dict[str, dict[str, Any]]:
    return STRATEGY_ERRORS


@dataclass
class Candidate:
    """策略产出的候选股。"""

    code: str
    name: str
    tier: str
    strategy: str
    strategy_label: str
    score: float
    reasons: list[str] = field(default_factory=list)
    snapshot: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """转为扁平字典。

        必须带上 snapshot 中的行情字段（收盘价/涨跌幅/换手/量比/RPS），
        否则下游评分与报告只能拿到 0 —— 这些字段不在策略输出列里。
        """
        data: dict[str, Any] = {
            "code": self.code,
            "name": self.name,
            "tier": self.tier,
            "strategy": self.strategy,
            "strategy_label": self.strategy_label,
            "strategy_score": round(float(self.score), 2),
            "reasons": self.reasons,
        }
        for key, value in (self.snapshot or {}).items():
            data.setdefault(key, value)
        return data


@dataclass
class StrategyContext:
    """策略运行上下文：一次加载，全策略共享，避免重复查库。"""

    trade_date: date
    features: pd.DataFrame
    universe: pd.DataFrame
    cfg: Config = field(default_factory=get_config)

    def __post_init__(self) -> None:
        self._merged: pd.DataFrame | None = None

    @property
    def merged(self) -> pd.DataFrame:
        """因子截面与股票池的合并结果（惰性计算，只做一次）。"""
        if self._merged is None:
            if self.features is None or self.features.empty:
                self._merged = pd.DataFrame()
            else:
                uni = self.universe
                if uni is None or uni.empty:
                    self._merged = self.features.copy()
                    self._merged["name"] = ""
                    self._merged["board"] = "main"
                    self._merged["enough_bars"] = True
                else:
                    keep = uni[["code", "name", "board", "enough_bars"]].copy()
                    self._merged = self.features.merge(keep, on="code", how="inner")
                self._merged = self._merged.reset_index(drop=True)
        return self._merged


def scale(series: pd.Series, low: float, high: float) -> pd.Series:
    """把序列线性映射到 0~1，用于把「条件强度」转成得分。"""
    s = pd.to_numeric(series, errors="coerce")
    if high == low:
        return pd.Series(0.5, index=s.index)
    return ((s - low) / (high - low)).clip(0, 1).fillna(0.0)


def pct(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}%"
    except (TypeError, ValueError):
        return "-"


def yi(value: Any) -> str:
    """把金额格式化为「亿」。"""
    try:
        return f"{float(value) / 1e8:.2f}亿"
    except (TypeError, ValueError):
        return "-"


def num(value: Any, digits: int = 2, suffix: str = "") -> str:
    """安全格式化数值，空值显示为 '-'。

    为什么必须用它而不是 f"{x:.2f}"：
    - 因子列里 NA 真实存在（次新股/停牌股的部分指标无值），
      `f"{pd.NA:.2f}"` 虽然不抛异常，但会输出 `<NA>` 这种字样直接进报告；
    - `float(pd.NA)` / `int(pd.NA)` 会直接抛 TypeError，把整个策略打断。
    统一走这里，报告里永远不会出现 `<NA>` / `nan`。
    """
    try:
        if value is None or pd.isna(value):
            return "-"
    except (TypeError, ValueError):
        return "-"
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "-"


class BaseStrategy(ABC):
    """策略抽象基类。

    子类需定义：
    - name / label / tier
    - require_col：依赖的因子列，用于次新股数据不足时的降级判断
    - evaluate(df)：向量化计算，返回含 code / strategy_score / reasons 的 DataFrame
    """

    name: str = "base"
    label: str = "基础策略"
    tier: str = "swing"
    require_col: str = "ma20"
    enabled_key: str = ""

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = params or {}

    # ---------------- 参数 ----------------
    def param(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)

    @property
    def enabled(self) -> bool:
        return bool(self.params.get("enabled", True))

    # ---------------- 主流程 ----------------
    def screen(self, ctx: StrategyContext) -> list[Candidate]:
        """执行选股。数据不足的标的（如次新股）在此被降级跳过，而不是剔除出池。"""
        df = ctx.merged
        if df is None or df.empty:
            return []

        if self.require_col in df.columns:
            df = df[df[self.require_col].notna()]
        if df.empty:
            return []
        # 重置索引，保证后续 mask / reasons 按「位置」对齐
        df = df.reset_index(drop=True)

        try:
            out = self.evaluate(df)
        except Exception as exc:  # noqa: BLE001 - 单策略异常不应中断整体流程
            record_strategy_error(self.name, exc, context=str(ctx.trade_date))
            logger.exception(
                "[%s] 策略计算异常，本次（%s）该策略信号全部丢失: %s",
                self.name, ctx.trade_date, exc,
            )
            return []

        if out is None or out.empty:
            return []

        out = out[out["strategy_score"] > 0]
        candidates: list[Candidate] = []
        for _, r in out.iterrows():
            candidates.append(
                Candidate(
                    code=to_str(r["code"]),
                    name=to_str(r.get("name")),
                    tier=self.tier,
                    strategy=self.name,
                    strategy_label=self.label,
                    score=to_float(r["strategy_score"]),
                    reasons=to_list(r.get("reasons")),
                    snapshot={k: r.get(k) for k in ("close", "pct_chg", "turn", "vol_ratio", "rps120")},
                )
            )
        logger.info("[%s] 命中 %d 只", self.name, len(candidates))
        return candidates

    @abstractmethod
    def evaluate(self, df: pd.DataFrame) -> pd.DataFrame:
        """向量化评估，返回列：code, name, strategy_score, reasons。"""

    # ---------------- 工具 ----------------
    @staticmethod
    def build_result(df: pd.DataFrame, mask: pd.Series, score: pd.Series, reasons: list[list[str]]) -> pd.DataFrame:
        """把掩码 + 分数 + 理由组装成标准结果。"""
        sel = df[mask].copy()
        if sel.empty:
            return pd.DataFrame(columns=["code", "name", "strategy_score", "reasons"])
        sel["strategy_score"] = pd.Series(score, index=df.index)[mask].values
        sel["reasons"] = [reasons[i] for i in np.where(mask.values)[0]]
        cols = ["code", "name", "strategy_score", "reasons"]
        for extra in ("close", "pct_chg", "turn", "vol_ratio", "rps120"):
            if extra in sel.columns:
                cols.append(extra)
        return sel[cols].reset_index(drop=True)
