# -*- coding: utf-8 -*-
"""因子层：向量化指标。

2026-10-08：因子宽表（`dws_feature`，545 万行）**已随主链路删除** ——
它唯一的用途是给那 8 个策略提供面板，而策略体检证明它们全部无可实现 alpha。
保留的 `indicators` 是纯函数（被市场状态计算依赖），`sector_strength` 供板块展示。
"""

from astock.features.indicators import clip_score, limit_ratio, percentile_score, safe_div

__all__ = ["limit_ratio", "percentile_score", "clip_score", "safe_div"]
