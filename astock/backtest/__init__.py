# -*- coding: utf-8 -*-
"""样本外验证层：历史逐日回放 + 远期收益统计。

与「真实每日运行」的区别：回放时严格按照 as-of 语义取数据，
不使用任何未来信息（股票池、因子、市场状态全部按当日截面）。
"""

from astock.backtest.engine import BacktestEngine
from astock.backtest.metrics import summarise

__all__ = ["BacktestEngine", "summarise"]
