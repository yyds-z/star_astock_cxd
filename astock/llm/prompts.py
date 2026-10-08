# -*- coding: utf-8 -*-
"""Prompt 模板（参考 caveman 的 token 压缩思想）。

caveman 的核心结论：只压缩「叙述性文字」，代码、路径、数字、字段名一律不改。
本模块把这个规则落到选股报告场景：

1. 强制结构化 JSON 输出，不做自然语言段落；
2. 只喂「已算好的指标值」，不喂原始 K 线序列（输入 token 降幅最大的一环）；
3. 限制每个字段字数，禁止铺垫与总结句；
4. 明确禁止编造数字，只能用输入中给出的值。
"""

from __future__ import annotations

import json
from typing import Any

# caveman 风格的输出约束。
#
# 关键教训：光说「≤40字、只输出JSON」是不够的 —— 必须**逐个字段说明要写什么**。
# 早期版本没有字段语义说明，模型看到 market/weights 这类 JSON 就直接复述字段名，
# 产出 `趋势行情，confidence 0.619，breadth_ma20 56.32…` 这种既不是分析、
# 又占字数的输出；同时 risk 字段没有对应输入数据，模型只能按规则写「数据不足」。
# 因此这里补上「字段含义」段，并把 risk 所需的输入一并喂进去。
SYSTEM_PROMPT = """你是A股盘前研究助手，为个人投资者撰写当日操作参考。

输出必须是 JSON，结构固定：
{"market_view":"...","picks":[{"id":"...","logic":"...","risk":"..."}],"ops":"..."}

各字段要写什么（按含义写，禁止照抄输入里的字段名）：
- market_view：当前环境下**该进攻还是防守**的判断（市场状态只作背景，不涉及档位/策略选择）。50字内。
- picks[].logic：这只股票**为什么入选**。引用输入 reasons 中的事实来说明，40字内。
- picks[].risk：这只股票**最需要提防什么**。依据 reasons 里含风险/偏高/追高/波动/流动性
  的条目，以及 risk_penalty 的大小；若确实没有任何风险信息，写"未见明显风险"。40字内。
- ops：今天**具体怎么操作**（仓位、买点或止损思路）。40字内。

硬性规则：
1. 只输出 JSON，不要解释、寒暄、markdown 代码块。
2. 只用输入中出现的事实与数字，禁止推测、编造、引入外部信息。
3. 每个输入 pick 必须对应一个输出 pick，id 原样回填，不得增删或合并。
4. 用简体中文短句；不要"综上所述""值得注意的是"这类铺垫，不要形容词堆砌。
5. 数字保留输入精度，不要改写成整数。
6. 输出正文里**不得出现输入字段名**（如 risk_penalty、breadth_ma20、limit_up）；
   要把字段名翻译成中文说法（如"风险扣分 30"→"风险偏高"，"limit_up 72"→"涨停 72 家"）。"""


def pick_key(pick: dict) -> str:
    """候选唯一标识：同一只股票可能同时出现在不同档位，因此必须带档位。"""
    tier = pick.get("tier")
    return f"{tier}:{pick.get('code')}"


# 判定「这条理由属于风险提示」的关键词。
# 用于把风险信息单独抽出来喂给 LLM —— 否则 risk 字段没有依据，
# 模型只能按规则写「数据不足」，风险提示就变成了一句废话。
RISK_HINTS = ("风险", "偏高", "追高", "波动", "流动性", "不足")


def as_list(value: Any) -> list[str]:
    """把 reasons 归一化成 list[str]。

    同一个字段在不同调用路径下形态不同：引擎内存帧里是 `list`，
    从 `ads_recommend` 读回来是 **JSON 字符串**。
    若不对字符串先解析就切片，`value[:4]` 取到的是前 4 个**字符**
    （形如 `'["长期'`），会把 Prompt 直接喂成乱码。
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return [str(v) for v in parsed]
            except (ValueError, TypeError):
                pass
        return [text] if text else []
    return [str(value)]


def risk_notes_of(pick: dict) -> list[str]:
    """从 reasons 中挑出风险类条目，作为 LLM 撰写 risk 字段的依据。"""
    return [r for r in as_list(pick.get("reasons")) if any(k in r for k in RISK_HINTS)]


def _num(value: Any, default: float = 0.0) -> float:
    """安全转 float（兼容 None / NaN / pd.NA）。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return default if f != f else f  # NaN 走默认值


def build_market_prompt(market: dict, stats: dict, picks: list[dict]) -> str:
    """构造市场分析 Prompt：只传聚合指标，不传原始行情。"""
    weights = market.get("weights") or {}
    main_tier = max(weights, key=weights.get) if weights else None
    payload = {
        "market": {
            "state": market.get("label"),
            "breadth_ma20_pct": market.get("breadth_ma20"),
            "breadth_ma60_pct": market.get("breadth_ma60"),
            "limit_up": market.get("limit_up"),
            "limit_down": market.get("limit_down"),
            "broken_rate_pct": market.get("broken_rate"),
            "current_weights": weights,
            "main_tier_hint": main_tier,
        },
        "strategy_hits": stats,
        "picks": [
            {
                "id": pick_key(p),
                "code": p.get("code"),
                "name": p.get("name"),
                "tier": p.get("tier_label") or p.get("tier"),
                "strategy": p.get("strategy_label") or p.get("strategy"),
                "score": p.get("final_score"),
                "reasons": as_list(p.get("reasons"))[:4],
                "risk_penalty": p.get("risk_penalty"),
                "risk_notes": risk_notes_of(p),
            }
            for p in picks
        ],
    }
    return "输入数据：\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def build_review_prompt(stats: dict, detail: list[dict]) -> str:
    """构造复盘归因 Prompt。"""
    payload = {"stats": stats, "records": detail[:20]}
    return (
        "以下是近期推荐结果统计与明细，请归因成功与失败原因。\n"
        "输出JSON：{\"win_factors\":[...],\"loss_factors\":[...],\"suggest\":[...]}，"
        "每项≤20字。\n数据：\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def template_market_view(market: dict) -> str:
    """无 LLM 时的本地模板：保证报告结构完整。

    2026-10-08：不再输出「主推 X 档」—— 档位配额随主链路删除，
    市场状态只作背景描述（它不再影响任何选股）。
    """
    label = market.get("label") or "未知"
    return (
        f"{label}；宽度MA20 {market.get('breadth_ma20', '-')}%、MA60 {market.get('breadth_ma60', '-')}%；"
        f"涨停 {market.get('limit_up', '-')} 家、炸板率 {market.get('broken_rate', '-')}%。"
    )


def template_logic(pick: dict) -> str:
    reasons = as_list(pick.get("reasons"))
    return "；".join(reasons[:3]) if reasons else "命中策略条件"


def template_risk(pick: dict) -> str:
    notes = risk_notes_of(pick)
    if notes:
        return "；".join(notes[:2])
    penalty = _num(pick.get("risk_penalty"))
    return "风险可控" if penalty < 10 else f"风险扣分 {penalty:.0f}"


# ---------------- 回顾板块（上一批信号的实际结果）----------------
# 与上面的盘前 Prompt 分开：那个是「选什么」，这个是「上次选得怎么样」，
# 两者的输出 schema 与事实依据完全不同，混在一个 Prompt 里会互相干扰。
#
# 2026-10-08：归因对象由主链路候选改为**影子信号**（主链路已删）。
# 同时新增第 6 条硬性规则：本系统处于**冻结期**，归因只用于理解与记录，
# 不得据以调参 —— 否则"复盘"会变成"事后拟合"的入口，这是本项目最大的教训。
RECAP_SYSTEM_PROMPT = """你在复盘A股影子信号（涨停基因+缩量不破位）最近一批候选的实际结果。

输出必须是 JSON，结构固定：
{"verdict":"...","wins":"...","losses":"...","lesson":"...","adjust":"..."}

各字段要写什么（按含义写，禁止照抄输入里的字段名）：
- verdict：这一批信号整体算成功还是失败，并给出**最主要的一条依据**（如胜率、超额收益）。40字内。
- wins：**上涨的那批**有什么共性（题材、板块、形态、量价特征）。必须点名具体股票或板块。40字内。
- losses：**下跌的那批**有什么共性，必须点名具体股票或板块。40字内。
- lesson：一条可执行的观察结论（下次遇到同类情况该怎么看）。40字内。
- adjust：是否需要改变做法；**契约冻结期内一律写"维持现状"**，把想法记进 lesson 即可。40字内。

硬性规则：
1. 只输出 JSON，不要解释、寒暄、markdown 代码块。
2. 只用输入中出现的事实与数字，禁止推测、编造、引入外部行情信息。
3. 用简体中文短句；不要"综上所述""值得注意的是"这类铺垫，不要形容词堆砌。
4. 不要复述输入里的字段名（如 win_rate_pct），要翻译成中文说法。
5. 若样本很少（少于 3 只），在 verdict 中明确说明"样本过少、结论参考价值有限"。
6. 收益一律以**可实现口径**（次日开盘买→再次日收盘卖）为准；旧口径数字若在输入中
   出现，必须明确说明它是"买不进去的幻影收益"，不得当作经验依据。"""


def build_recap_prompt(recap: dict) -> str:
    """构造「上一批推荐回顾」的总结 Prompt。

    只喂聚合指标 + 涨跌各前 5 只（code/名称/收益/策略/归因原因），
    不喂全部明细：明细越长，模型越容易只挑个别股票讲故事而忽略整体。
    """
    rows = recap.get("rows") or []

    def brief(r: dict) -> dict:
        return {
            "code": r.get("code"),
            "name": r.get("name"),
            "tier": r.get("tier"),
            "ret_pct": r.get("ret"),
            "day_chg_pct": r.get("day_chg"),
            "strategy": r.get("strategy_label"),
            "advice_score": r.get("final_score"),
            "reason": r.get("reason"),
        }

    payload = {
        "review_date": recap.get("review_date"),
        "rec_date": recap.get("rec_date"),
        "stats": {
            "count": recap.get("count"),
            "win_rate_pct": recap.get("win_rate"),
            "avg_return_pct": recap.get("avg_return"),
            "avg_day_chg_pct": recap.get("avg_day_chg"),
            "benchmark_return_pct": recap.get("benchmark_return"),
            "excess_pct": recap.get("excess"),
            "effect_score": (recap.get("effect_score") or {}).get("score"),
            "avg_max_gain_pct": recap.get("avg_max_gain"),
            "avg_max_dd_pct": recap.get("avg_max_dd"),
        },
        "by_tier": recap.get("by_tier"),
        "by_strategy": recap.get("by_strategy"),
        "gainers": [brief(r) for r in rows if (r.get("ret") or 0) > 0][:5],
        "losers": [brief(r) for r in rows if (r.get("ret") or 0) <= 0][:5],
    }
    return "输入数据：\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def template_recap_summary(recap: dict) -> dict:
    """无 LLM 时的本地模板：只陈述事实（涨跌名单），不假装做归因。"""
    rows = recap.get("rows") or []
    gainers = [r for r in rows if (r.get("ret") or 0) > 0]
    losers = [r for r in rows if (r.get("ret") or 0) <= 0]

    def names(items: list[dict]) -> str:
        return "、".join(f"{r.get('code')} {r.get('name')}" for r in items[:3]) or "无"

    score = (recap.get("effect_score") or {}).get("score")
    return {
        "verdict": (
            f"效果分 {score}；{len(gainers)}/{len(rows)} 只上涨，"
            f"买入后平均 {recap.get('avg_return')}%，超额 {recap.get('excess')}%。"
        ),
        "wins": f"上涨：{names(gainers)}",
        "losses": f"下跌/持平：{names(losers)}",
        "lesson": "未启用 LLM，仅列出结果；启用后可自动归因共性与教训。",
        "adjust": "维持现状",
    }
