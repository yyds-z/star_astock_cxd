# -*- coding: utf-8 -*-
"""评价层：全系统唯一的可实现口径裁判。

独立成包（而不是塞进 backtest/ 或 shadow/）的原因：
过去每个模块都自己算一遍收益，于是同一个策略在不同地方有两种成绩，
而**只有其中一种是对的** —— 这正是本项目反复踩的坑。
这里集中定义"什么算收益"，并要求所有消费方（报告/回测/界面/参数网格）调用它。
"""

from astock.eval.checkup import score_strategy, strategy_hits
from astock.eval.judge import (
    EXEC_COST,
    attach_benchmark,
    bonferroni_t,
    executable,
    format_table,
    picks_with_returns,
    pool_benchmark,
    split_out_of_sample,
    summarise,
)

__all__ = [
    "EXEC_COST",
    "attach_benchmark",
    "bonferroni_t",
    "executable",
    "format_table",
    "picks_with_returns",
    "pool_benchmark",
    "score_strategy",
    "split_out_of_sample",
    "strategy_hits",
    "summarise",
]
