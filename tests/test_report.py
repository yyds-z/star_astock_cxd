# -*- coding: utf-8 -*-
"""报告生成回归测试（不需要数据库）。

覆盖两个曾真实出现过的缺陷：
1. 行情展示字段全为 0.00 —— 策略输出里不带 close/pct_chg/turn/vol_ratio，
   必须在 Candidate 序列化时带上 snapshot；
2. 推荐逻辑「串档」—— 同一只股票同时出现在多个档位时，
   若按 code 索引 AI 解读，会取到另一档的理由，必须按 (档位, 代码) 索引。

用法：
    python tests/test_report.py      # 或 scripts\run_tests.bat 一键全跑
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from astock.report.builder import ReportBuilder  # noqa: E402
from astock.strategy.base import Candidate  # noqa: E402


class _StubStorage:
    """占位存储：只用于构造 ReportBuilder，不触碰真实数据库。"""


class _StubLLM:
    """假 LLM：返回按 id 索引的解读，用于验证对齐逻辑。"""

    enabled = True

    def __init__(self) -> None:
        self.captured_prompt = ""

    def chat_json(self, system: str, user: str) -> dict:
        self.captured_prompt = user
        payload = json.loads(user.split("\n", 1)[1])
        return {
            "market_view": "测试视角",
            "picks": [
                {"id": p["id"], "logic": f"LLM逻辑-{p['tier']}", "risk": "测试风险"}
                for p in payload["picks"]
            ],
            "ops": "测试操作",
        }


def make_scored() -> pd.DataFrame:
    """构造一只股票同时命中波段与价值两档的评分结果。"""
    rows = []
    for tier, reasons in (
        ("swing", ["突破20日新高 4.38%", "成交额 30.38亿"]),
        ("value", ["长期均线多头（MA20>MA60>MA120）"]),
    ):
        rows.append(
            {
                "code": "000676",
                "name": "智度股份",
                "tier": tier,
                "tier_label": {"swing": "波段", "value": "价值"}[tier],
                "strategy": "turtle_trade" if tier == "swing" else "ma_multi_trend",
                "strategy_label": "海龟突破" if tier == "swing" else "长期均线多头",
                "final_score": 89.5 if tier == "swing" else 71.1,
                "tier_rank": 1,
                "close": 12.34,
                "pct_chg": 4.38,
                "turn": 5.67,
                "vol_ratio": 2.31,
                "rps120": 93.2,
                "reasons": reasons,
                "score_detail": json.dumps({"策略强度": 88.2}, ensure_ascii=False),
                "rec_id": f"2026-09-22_{tier}_000676",
            }
        )
    return pd.DataFrame(rows)


def check_candidate_serialization() -> list[str]:
    """验证 Candidate.as_dict 会带上行情字段。"""
    issues = []
    c = Candidate(
        code="000002",
        name="万科A",
        tier="swing",
        strategy="turtle_trade",
        strategy_label="海龟突破",
        score=92.0,
        reasons=["突破20日新高"],
        snapshot={"close": 9.87, "pct_chg": 4.38, "turn": 3.21, "vol_ratio": 1.9, "rps120": 88.0},
    )
    d = c.as_dict()
    for key, expect in (("close", 9.87), ("pct_chg", 4.38), ("turn", 3.21), ("vol_ratio", 1.9)):
        if d.get(key) != expect:
            issues.append(f"Candidate.as_dict 缺少字段 {key}（期望 {expect}，实际 {d.get(key)}）")
    return issues


def check_render() -> list[str]:
    issues = []
    recs = make_scored()
    builder = ReportBuilder(storage=_StubStorage(), llm=_StubLLM())

    result = {
        "data_date": "2026-09-22",
        "plan_date": "2026-09-23",
        "params_version": "test0001",
        "pool_size": 455,
        "strategy_stats": {"turtle_trade": 13},
        "recommendations": recs,
    }
    market = {
        "label": "趋势行情",
        "confidence": 0.6,
        "breadth_ma20": 46.29,
        "breadth_ma60": 61.48,
        "limit_up": 15,
        "limit_down": 3,
        "broken_rate": 21.05,
        "weights": {"short": 0.1, "swing": 0.7, "value": 0.2},
    }

    ai = builder._ai_section(market, result["strategy_stats"], recs, use_llm=True)
    md = builder._render_markdown(result, market, recs, ai)

    if __import__("os").environ.get("DEBUG"):
        print("--- ai.picks ---")
        print(json.dumps(ai.get("picks"), ensure_ascii=False, indent=2))
        print("--- markdown ---")
        print(md)

    # 1) 行情字段不能是 0.00
    if "| 12.34 |" not in md:
        issues.append("报告表格未渲染出正确收盘价（出现 0.00？）")
    if "4.38%" not in md:
        issues.append("报告表格未渲染出正确涨跌幅")

    # 2) 波段档必须用波段自己的理由，不能串到价值档
    swing_block = md.split("### 价值档")[0]
    if "长期均线多头（MA20>MA60>MA120）" in swing_block:
        issues.append("波段档推荐逻辑串到了价值档的理由")

    # 3) AI 解读按 (档位, 代码) 对齐
    #    假 LLM 回填的是中文档位名（prompt 里 tier_label 传的就是中文）
    if "LLM逻辑-波段" not in swing_block:
        issues.append("波段档未取到对应的 LLM 解读（档位对齐失败）")

    value_block = md.split("### 价值档")[-1]
    if "LLM逻辑-价值" not in value_block:
        issues.append("价值档未取到对应的 LLM 解读（档位对齐失败）")

    # 4) LLM 输入里必须带 id 字段，否则无法对齐
    prompt = json.loads(builder.llm.captured_prompt.split("\n", 1)[1])
    if not all("id" in p for p in prompt["picks"]):
        issues.append("LLM 输入缺少 id 字段，无法按档位对齐")
    ids = {p["id"] for p in prompt["picks"]}
    if ids != {"swing:000676", "value:000676"}:
        issues.append(f"LLM 输入 id 集合不正确：{ids}")

    return issues


def main() -> int:
    issues = check_candidate_serialization() + check_render()
    if issues:
        print("测试失败：")
        for item in issues:
            print("  -", item)
        return 1
    print("报告生成回归测试全部通过")
    print("  · Candidate 序列化包含行情字段")
    print("  · 同代码跨档位时推荐逻辑不串档")
    print("  · AI 解读按 (档位, 代码) 正确对齐")
    return 0


if __name__ == "__main__":
    sys.exit(main())
