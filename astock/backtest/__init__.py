# -*- coding: utf-8 -*-
"""回测展示层：净值曲线与校准曲线。

2026-10-08：回测引擎（`engine` / `metrics` / `report`）已随主链路删除 ——
它评估的对象正是那 8 个策略，而统一体检证明它们全部无可实现 alpha。
这里只保留**曲线的构造与统计**：影子信号的净值曲线、校准曲线仍由它渲染
（`api/server.py` 的 `/api/equity`、`/api/calibration`），
且与页面复用同一套口径与显著性统计。
"""

from astock.backtest import charts

__all__ = ["charts"]
