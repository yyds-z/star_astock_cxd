# -*- coding: utf-8 -*-
"""策略档案目录（Skill Catalog）。

这是「人工维护的策略知识库」：记录每个策略的定位、判定条件的设计依据、
适用市场环境、已知失效场景、移植来源。

为什么这部分不自动生成：
- 判定条件可以从代码里读出来，但「为什么这样设计」「什么情况下会失效」
  属于经验知识，只能沉淀，不能推导。
- 把它落成文件，就形成了可持续迭代的 Skill 资产（Phase 2/3 的策略进化就靠它）。

代码里的策略实现（astock/strategy/*.py）与这里的档案一一对应，
`python -m astock.cli skills sync` 会把两者合并渲染成 skills/<name>/ 目录。
"""

from __future__ import annotations

from typing import Any

# 每个策略的完整档案。key 必须与策略类的 name 属性一致。
CATALOG: dict[str, dict[str, Any]] = {
    # ==================== 短线档 ====================
    "limit_up_shakeout": {
        "summary": "昨日涨停后今日放量收阴但不破昨收，捕捉主力洗盘而非出货的次日回踩机会。",
        "rationale": (
            "涨停代表有资金主动做多；次日放量收阴且不破昨收，说明抛压被承接、"
            "筹码在换手而不是崩塌。这类形态的次日常有反包动能。"
        ),
        "conditions": [
            "昨日涨停：prev_close ≥ prev2_close × limit_ratio（默认 1.095）",
            "今日收阴：close < open",
            "今日放量：volume > prev_volume × vol_ratio（默认 2.0）",
            "支撑不破：low ≥ prev_close",
        ],
        "apply_when": ["euphoria", "event", "range"],
        "avoid_when": ["recession"],
        "pitfalls": [
            "退潮期（涨停家数骤降、炸板率 > 45%）失效明显：昨日的涨停本身就是情绪高点",
            "一字板涨停后打开再收阴，往往是真出货而非洗盘（本策略无法区分封单质量）",
            "次新股的涨停/跌停幅度不同（20%/30%），limit_ratio 需按板块区分",
        ],
        "source": {
            "project": "Sequoia-X",
            "file": "sequoia_x/strategy/limit_up_shakeout.py",
            "note": "判定条件直接移植，评估方式由逐股循环改为全市场向量化",
        },
    },
    "new_stock_burst": {
        "summary": "上市未满 120 日、高换手放量上涨的次新股，捕捉情绪博弈阶段的资金异动。",
        "rationale": (
            "次新股没有历史套牢盘、股本小、题材想象空间大，是情绪周期里弹性最大的一类。"
            "本策略是「保留次新股」这一需求的落地实现。"
        ),
        "conditions": [
            "上市天数 ≤ max_listed_days（默认 120 日）",
            "换手率 ≥ min_turn（默认 15%）",
            "放量：vol_ratio > vol_ratio_th（默认 2.0）",
            "当日涨幅 ≥ min_pct_chg（默认 3%）",
        ],
        "apply_when": ["euphoria", "event"],
        "avoid_when": ["recession", "trend"],
        "pitfalls": [
            "次新股上市初期 K 线不足，均线/量比类指标会失真 —— 因子层需按 require_col 降级跳过",
            "无涨跌停历史约束，单日振幅极大，风险扣分必须生效",
            "解禁预期（首发原股东限售股）会形成额外抛压，当前版本尚未纳入该因子",
        ],
        "source": {
            "project": "本系统新增",
            "file": "astock/strategy/short_term.py",
            "note": "为满足「保留次新用于情绪博弈」的需求而新增",
        },
    },
    # ==================== 波段档 ====================
    "turtle_trade": {
        "summary": "突破 20 日新高 + 成交额过亿 + 实体阳线，捕捉趋势启动的第一根放量突破。",
        "rationale": (
            "海龟法则的 A 股改良版。原版只要求突破新高，但在 A 股会被高开低走的诱多"
            "大阴线大量误伤（例如历史上的郑州煤电式形态），因此加入「实体阳线 + 真涨」"
            "两个防守条件。"
        ),
        "conditions": [
            "突破：close > 前 20 日最高价（不含当日，hh20_prev）",
            "流动性：amount > min_amount（默认 1 亿元）",
            "实体阳线：close > open",
            "真涨：close > prev_close（排除假阳线）",
        ],
        "apply_when": ["trend", "range"],
        "avoid_when": ["recession"],
        "pitfalls": [
            "震荡市里 20 日新高频繁出现假突破，需要流动性与阳线条件共同过滤",
            "高位放量突破后次日高开低走概率不低 —— 回测显示平均持有到 3 日转负，宜 T+1 了结",
            "复权方式变化会改变 hh20_prev，跨数据源切换后需重建因子表",
        ],
        "source": {
            "project": "Sequoia-X",
            "file": "sequoia_x/strategy/turtle_trade.py",
            "note": "条件直接移植（含防诱多改良），评估改为全市场向量化",
        },
    },
    "ma_volume": {
        "summary": "MA5 上穿 MA20 且成交量超过 20 日均量 1.5 倍，捕捉均线金叉的放量确认。",
        "rationale": (
            "均线金叉本身噪音很大，但叠加「放量确认」后信号质量显著提升 —— "
            "量能是资金真实参与的证明，能过滤掉大部分无量假金叉。"
        ),
        "conditions": [
            "金叉：prev_ma5 < prev_ma20 且 ma5 > ma20",
            "放量：vol_ratio > vol_ratio_th（默认 1.5）",
            "ma20 > 0（数据有效性检查）",
        ],
        "apply_when": ["trend", "range"],
        "avoid_when": ["recession"],
        "pitfalls": [
            "长期下跌趋势中的第一次金叉往往失败，需结合 MA60 方向过滤（当前版本未加）",
            "一字板或极端行情下 ma5/ma20 会在同一天完成穿越，信号滞后",
        ],
        "source": {
            "project": "Sequoia-X",
            "file": "sequoia_x/strategy/ma_volume.py",
            "note": "条件直接移植，评估改为全市场向量化",
        },
    },
    "rps_breakout": {
        "summary": "120 日相对强度位于全市场前 10%，且价格接近 120 日高点，捕捉强势股的突破。",
        "rationale": (
            "欧奈尔 RPS（相对价格强度）思想：强势股倾向于继续强势。"
            "RPS 是横截面排名，天然剔除大盘涨跌影响。"
        ),
        "conditions": [
            "RPS：120 日涨幅的全市场百分位 ≥ rps_threshold（默认 90）",
            "接近高点：close ≥ hh120 × near_high_ratio（默认 0.90）",
        ],
        "apply_when": ["trend", "euphoria"],
        "avoid_when": ["recession", "range"],
        "pitfalls": [
            "RPS 高只说明过去强势，在风格切换时容易买在顶部 —— 回测平均收益为负",
            "需要至少 120 个交易日的历史，次新股会被 require_col 过滤掉",
            "横截面排名随样本变化：股票池越小时 RPS 越不稳定（1000 只 vs 5000 只结论会不同）",
        ],
        "source": {
            "project": "Sequoia-X",
            "file": "sequoia_x/strategy/rps_breakout.py",
            "note": "条件直接移植，RPS 由 Python 排名改为 DuckDB PERCENT_RANK",
        },
    },
    "high_tight_flag": {
        "summary": "40 日强动量后进入 10 日极度收敛且缩量整理，捕捉旗形突破前的蓄势状态。",
        "rationale": (
            "「高而窄」的旗形是强势股中继整理的经典形态：前期涨幅大说明有资金，"
            "整理时振幅小且缩量说明没有出货，一旦突破往往延续原趋势。"
        ),
        "conditions": [
            "强动量：hh40 / ll40 > 1.6",
            "极度收敛：hh10 / ll10 < 1.15",
            "高位抗跌：ll10 ≥ hh40 × 0.8",
            "缩量：volume < 前 20 日均量 × 0.6",
        ],
        "apply_when": ["trend"],
        "avoid_when": ["recession"],
        "pitfalls": [
            "条件严格导致命中极少（2 年回测仅个位数），样本不足难以评估有效性",
            "收敛形态也可能是下跌中继而非上涨中继，需要板块强度辅助判断",
        ],
        "source": {
            "project": "Sequoia-X",
            "file": "sequoia_x/strategy/high_tight_flag.py",
            "note": "条件直接移植，评估改为全市场向量化",
        },
    },
    "uptrend_limit_down": {
        "summary": "中期上升趋势中突然放量跌停，捕捉错杀带来的反包机会。",
        "rationale": (
            "MA20 > MA60 说明中期趋势向好；在这种背景下出现放量跌停，"
            "更可能是恐慌错杀或事件性冲击，而非趋势反转。"
        ),
        "conditions": [
            "上升趋势：prev_ma20 > prev_ma60",
            "放量跌停：close ≤ prev_close × limit_down_ratio（默认 0.905）",
            "放量：vol_ratio > vol_ratio_th（默认 2.0）",
        ],
        "apply_when": ["trend", "range"],
        "avoid_when": ["recession"],
        "pitfalls": [
            "「错杀」与「基本面爆雷」在盘后数据上无法区分 —— 这是本策略最大的风险来源",
            "退潮期的集体跌停不是错杀，必须结合市场状态过滤",
            "回测显示胜率低于 50% 但平均收益为正，属于「低胜率高赔率」类型，需控制单笔仓位",
        ],
        "source": {
            "project": "Sequoia-X",
            "file": "sequoia_x/strategy/uptrend_limit_down.py",
            "note": "条件直接移植，评估改为全市场向量化",
        },
    },
    # ==================== 价值档 ====================
    "ma_multi_trend": {
        "summary": "MA20 > MA60 > MA120 的长期多头排列，且未有效跌破 MA20，作为价值档的趋势代理。",
        "rationale": (
            "Phase 1 尚未接入财务数据，因此用「长期均线多头」作为质量与趋势的代理指标。"
            "Phase 2 接入 adata 财务数据后，将补充 ROE / 营收利润增速 / 估值分位等真基本面因子。"
        ),
        "conditions": [
            "长期多头：ma20 > ma60 > ma120",
            "回踩不破：close ≥ ma20 × max_pullback（默认 0.92）",
        ],
        "apply_when": ["trend", "range"],
        "avoid_when": ["recession"],
        "pitfalls": [
            "MA120 需要 120 个交易日历史，次新股会被过滤",
            "趋势代理无法识别基本面恶化 —— 这是 Phase 1 的已知局限，Phase 2 必须补上财务维度",
            "命中数量最多（占总信号近一半），但平均收益接近 0，说明当前形态筛选的区分度不足",
        ],
        "source": {
            "project": "本系统新增（趋势代理）",
            "file": "astock/strategy/value.py",
            "note": "Phase 2 接入财务数据后升级为真正的价值策略",
        },
    },
}


def get(strategy_name: str) -> dict[str, Any]:
    """取某个策略的档案；不存在时返回空档案（保证新增策略也能落盘）。"""
    return CATALOG.get(
        strategy_name,
        {
            "summary": "（待补充：该策略尚未编写档案）",
            "rationale": "",
            "conditions": [],
            "apply_when": [],
            "avoid_when": [],
            "pitfalls": [],
            "source": {"project": "", "file": "", "note": ""},
        },
    )


def all_names() -> list[str]:
    return list(CATALOG.keys())
