# -*- coding: utf-8 -*-
"""报告生成回归测试（不需要数据库）。

2026-10-08 重写：原测试覆盖的是主链路的两个缺陷（Candidate 序列化带行情字段、
同代码跨档位不串档）—— 那套候选已随主链路删除，测试对象随之失效。

现在覆盖**新报告结构**（影子信号 + 影子复盘 + 市场环境）的两条不变量：
1. 三个板块标题必须存在且顺序正确；
2. **不得再出现主链路字样**（候选股票/三档/档位权重/推荐回顾）——
   防止将来有人"顺手"把它们加回来，也防止残留代码把已删概念写进报告。

用法：python tests/test_report.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from astock.report.builder import ReportBuilder  # noqa: E402


class _StubStorage:
    """占位存储：一律返回空结果，从而走"无候选/无复盘"的降级分支。"""

    def query_df(self, sql: str, params=None) -> pd.DataFrame:  # noqa: ANN001
        return pd.DataFrame()

    def query_value(self, sql: str, params=None, default=None):  # noqa: ANN001
        return default

    def table_count(self, table: str) -> int:
        return 0

    def latest_trade_date(self, table: str = "dwd_daily_bar"):
        return None


MARKET = {
    "label": "趋势行情",
    "confidence": 0.6,
    "breadth_ma20": 56.3,
    "breadth_ma60": 48.1,
    "limit_up": 72,
    "limit_down": 3,
    "broken_rate": 21.05,
    "top_sector": "半导体",
    "top_sector_share": 18.2,
}

RESULT = {
    "data_date": "2026-09-30",
    "plan_date": "2026-10-08",
    "params_version": "test0001",
    "pool_size": 455,
    "market": MARKET,
}

FORBIDDEN = ("候选股票", "档位权重", "短线档", "波段档", "价值档", "昨日推荐回顾", "final_score")


def check_structure() -> list[str]:
    """板块标题存在且顺序正确。"""
    issues: list[str] = []
    builder = ReportBuilder(storage=_StubStorage())
    md = builder._render_markdown(RESULT, MARKET, None,
                                  {"market_view": "测试", "used_llm": False})
    order = []
    for title in ("## 一、影子信号候选", "## 二、影子复盘（归因）", "## 三、市场环境"):
        if title not in md:
            issues.append(f"报告缺少板块：{title}")
        else:
            order.append(md.index(title))
    if order != sorted(order):
        issues.append("板块顺序错误（应为 影子信号 → 影子复盘 → 市场环境）")
    # 降级路径也要有内容，不能整段空白
    if "无影子候选" not in md:
        issues.append("无候选时应给出明确说明，实际未出现「无影子候选」")
    if "尚无复盘记录" not in md:
        issues.append("无复盘时应给出明确说明，实际未出现「尚无复盘记录」")
    return issues


def check_no_legacy() -> list[str]:
    """不得再出现主链路概念（防止已删的东西被"顺手"写回报告）。"""
    builder = ReportBuilder(storage=_StubStorage())
    md = builder._render_markdown(RESULT, MARKET, None,
                                  {"market_view": "测试", "used_llm": False})
    return [f"报告出现已删除的主链路字样：{w}" for w in FORBIDDEN if w in md]


def main() -> int:
    issues = check_structure() + check_no_legacy()
    if issues:
        print("测试失败：")
        for item in issues:
            print("  -", item)
        return 1
    print("报告结构回归测试通过")
    print("  · 三个板块（影子信号 / 影子复盘 / 市场环境）齐全且顺序正确")
    print("  · 无候选、无复盘时降级说明齐全")
    print("  · 未出现任何已删除的主链路字样")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
