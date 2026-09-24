# -*- coding: utf-8 -*-
"""口径交叉校验：上游涨停家数 vs 自算涨停家数。

为什么必须有这个工具：
系统里「涨停家数」存在**两个独立口径**，且都参与决策 ——

1. 上游口径：`dwd_limit_up`（同花顺涨停池），权威但只有一年；
2. 自算口径：`dws_market_regime.limit_up_count`（由 dws_feature.is_limit_up 判涨跌幅），
   可回填任意历史、无外部依赖。

而市场状态的阈值（`euphoria.min_limit_up = 80`）是用**自算口径**校准的。
两者若不一致，同一天会被判成不同状态，进而切换到不同的档位权重 ——
表现为「选股结果与直觉不符」，且不报任何错，极难排查。

实测（2025-09 ~ 2026-09，242 个交易日）：仅 15 天完全一致，
平均绝对差 5.8 家（7.5%），32 天差异超过 10 家，最大差 46 家。
因此差异是**真实存在**的，需要用本工具定期复核。

用法：
    python scripts/check_caliber.py            # 全区间校验
    python scripts/check_caliber.py --top 15   # 多列几天
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from astock.storage.db import get_storage  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="涨停家数口径交叉校验")
    parser.add_argument("--top", type=int, default=10, help="列出差异最大的 N 天")
    args = parser.parse_args()

    df = get_storage().query_df(
        """
        WITH up AS (SELECT date, COUNT(*) AS upstream FROM dwd_limit_up GROUP BY date)
        SELECT r.date, up.upstream, r.limit_up_count AS selfcalc,
               ABS(up.upstream - r.limit_up_count) AS diff
        FROM dws_market_regime r
        JOIN up ON up.date = r.date
        ORDER BY r.date
        """
    )

    print("=" * 72)
    print("  涨停家数口径交叉校验")
    print(f"  运行时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 72)
    if df.empty:
        print("  无可比对数据（需要 dwd_limit_up 与 dws_market_regime 都有数据）。")
        return 1

    n = len(df)
    mean_diff = df["diff"].mean()
    base = df["selfcalc"].mean()
    same = int((df["diff"] == 0).sum())
    big = int((df["diff"] > 10).sum())
    ratio = mean_diff / base * 100 if base else 0

    print(f"  可比对交易日：{n}")
    print(f"  上游均值 {df['upstream'].mean():.1f} 家　自算均值 {base:.1f} 家")
    print(f"  平均绝对差 {mean_diff:.1f} 家（占自算 {ratio:.1f}%）")
    print(f"  完全一致：{same}/{n} 天　差异 >10 家：{big} 天")
    print()

    if ratio >= 10:
        print("  ✖ 口径差异过大（≥10%）：市场状态阈值可能被口径问题带偏。")
        print("    处理：确认以哪个口径为准，并重算 dws_market_regime 后再校准阈值。")
    elif ratio >= 5:
        print("  ⚠ 口径差异中等（5%~10%）：阈值附近的日子可能状态判定不一致。")
        print("    建议：重要决策前用本工具复核，或为状态判定加「口径一致」的约束。")
    else:
        print("  ✔ 口径差异较小，暂不影响状态判定。")

    print()
    print(f"  差异最大的 {args.top} 天：")
    print(f"  {'日期':<12}{'上游':>8}{'自算':>8}{'差值':>8}")
    print("-" * 72)
    for _, r in df.nlargest(args.top, "diff").iterrows():
        print(f"  {str(r['date']):<12}{int(r['upstream']):>8}{int(r['selfcalc']):>8}"
              f"{int(r['diff']):>8}")
    print("-" * 72)
    print("  差异来源提示：ST 股(5%)、创业板/科创板(20%)、北交所(30%)、")
    print("                新股上市首日、以及「盘中触板但收盘未封」的处理差异。")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
