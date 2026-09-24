# -*- coding: utf-8 -*-
"""策略层：策略注册表与实例化。

每个策略是一个独立 Skill 单元，参数全部来自 config/settings.yaml，
便于 Phase 3 做参数优化与低效策略淘汰。
"""

from __future__ import annotations

from astock.config import Config, get_config
from astock.logger import get_logger
from astock.strategy.base import BaseStrategy, Candidate, StrategyContext, TIER_LABEL
from astock.strategy.short_term import SHORT_STRATEGIES
from astock.strategy.swing import SWING_STRATEGIES
from astock.strategy.value import VALUE_STRATEGIES

logger = get_logger("strategy")

STRATEGY_REGISTRY: dict[str, list[type[BaseStrategy]]] = {
    "short": SHORT_STRATEGIES,
    "swing": SWING_STRATEGIES,
    "value": VALUE_STRATEGIES,
}


def all_strategy_classes() -> list[type[BaseStrategy]]:
    return [cls for group in STRATEGY_REGISTRY.values() for cls in group]


def build_strategies(cfg: Config | None = None) -> dict[str, list[BaseStrategy]]:
    """按配置实例化启用的策略，并注入各自参数。"""
    cfg = cfg or get_config()
    result: dict[str, list[BaseStrategy]] = {}

    for tier, classes in STRATEGY_REGISTRY.items():
        instances: list[BaseStrategy] = []
        for cls in classes:
            params = cfg.section(f"strategy.{tier}.{cls.name}")
            if not params:
                # 配置缺失时按默认参数启用（便于新增策略直接跑通）
                params = {}
            if not bool(params.get("enabled", True)):
                logger.info("策略已禁用：%s", cls.name)
                continue
            instances.append(cls(params))
        result[tier] = instances

    return result


__all__ = [
    "BaseStrategy",
    "Candidate",
    "StrategyContext",
    "TIER_LABEL",
    "STRATEGY_REGISTRY",
    "build_strategies",
    "all_strategy_classes",
]
