# -*- coding: utf-8 -*-
"""影子模块：**独立于主评分体系**的实验性信号。

为什么单独成包而不是塞进 `scoring`/`recommend`：
主链路（8 策略 → 评分 → 配额）的评价指标 `ret1 = 买入日 close/open − 1` 在
A 股 T+1 下不可实现，实测可实现口径下超额归零、D+5 显著为负。在它修好之前，
把新信号混进同一条链路，会造成两个后果：① 新信号的成绩被旧口径污染；
② 一旦结果变差，无法判断是信号失效还是旧链路的问题。

所以这里的东西只做三件事：**落库、结算、统计**，不参与选股。
"""

from astock.shadow.limit_gene import (
    LimitGeneShadow,
    shadow_current_params,
    shadow_min_amount_avg20,
    shadow_min_signal_score,
    shadow_params_fingerprint,
)

__all__ = [
    "LimitGeneShadow",
    "shadow_current_params",
    "shadow_min_amount_avg20",
    "shadow_min_signal_score",
    "shadow_params_fingerprint",
]
