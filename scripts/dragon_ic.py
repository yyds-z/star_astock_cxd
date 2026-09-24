# -*- coding: utf-8 -*-
"""龙虎榜因子有效性检验（IC + 分组收益）。

只读 `data/astock_readonly.duckdb`（主库副本）——**刻意不碰主库**：
IC 检验会反复试错、反复重跑，如果连主库就会被回测/回填挡在外面。

判定标准与 `factor_ic.py` 一致：
· 横截面 Spearman IC，按日计算再取均值；
· Bonferroni 校正：同时检验 N 个因子，阈值 |t| ≥ 临界值才算显著
  （不校正的话，检验 14 次出现假阳性的概率接近 50%）。
"""

from __future__ import annotations

import sys
from math import sqrt
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "astock_readonly.duckdb"
OUT_DIR = ROOT / "data" / "reports"

# (列名, 中文名, 预期方向)  方向仅用于解读：+1 越大越好，-1 越小越好，0 不预设
FACTORS = [
    ("net_value", "龙虎榜净买入额", 0),
    ("net_rate", "净买入占比", 0),
    ("org_net_value", "机构净买入额", 0),
    ("inst_share", "机构主导度(%)", 0),
    ("hot_net_value", "游资净买入额", 0),
    ("hot_rank", "人气排名(越小越热)", -1),
    ("list_cnt_5d", "近5日上榜次数", 0),
    ("list_cnt_20d", "近20日上榜次数", 0),
    ("net_sum_5d", "近5日累计净买入", 0),
    ("sector_list_cnt", "板块上榜家数", 0),
    ("sector_net_sum", "板块净买入合计", 0),
    ("sector_rank", "板块内排名(1=最强)", -1),
    ("sector_strength", "板块强度", 0),
    ("sector_limit_up", "板块涨停家数", 0),
]
RET = "ret1"
MIN_N = 5  # 一天至少几只才有横截面意义


def main() -> int:
    if not DB.exists():
        print(f"[!] 副本不存在：{DB}\n    先执行：python scripts\\snapshot_db.py")
        return 1
    con = duckdb.connect(str(DB), read_only=True)
    df = con.execute(
        f"SELECT date, code, {', '.join(f[0] for f in FACTORS)}, "
        f"{RET}, open_premium FROM dws_dragon_factor WHERE {RET} IS NOT NULL"
    ).df()
    con.close()

    print("=" * 88)
    print("  龙虎榜因子有效性检验")
    print("=" * 88)
    print(f"  样本 {len(df)} 条（{df['date'].nunique()} 个交易日）")
    print(f"  全体均值：次日开盘溢价 {df['open_premium'].mean():+.2f}%　"
          f"买入后 {df[RET].mean():+.2f}%　胜率 {(df[RET] > 0).mean() * 100:.1f}%")

    n_tests = len(FACTORS)
    # Bonferroni：双侧 5% 显著性下的 t 临界值
    from statistics import NormalDist

    crit = NormalDist().inv_cdf(1 - 0.05 / (2 * n_tests))
    print(f"  多重检验校正：检验 {n_tests} 个因子，|t| ≥ {crit:.2f} 才算显著\n")

    lines = []
    print(f"  {'因子':<22}{'有效日':>7}{'均值IC':>9}{'IR':>7}{'t值':>8}{'IC>0占比':>9}  判定")
    print("-" * 88)
    rows = []
    for col, label, _ in FACTORS:
        ics = []
        for _, g in df.groupby("date"):
            s = g[[col, RET]].dropna()
            if len(s) < MIN_N or s[col].nunique() < 2:
                continue
            ic = s[col].rank().corr(s[RET].rank())
            if ic == ic:
                ics.append(ic)
        if len(ics) < 20:
            print(f"  {label:<22}{len(ics):>7}{'-':>9}{'-':>7}{'-':>8}{'-':>9}  × 有效日不足")
            continue
        arr = pd.Series(ics)
        mean_ic, sd = float(arr.mean()), float(arr.std(ddof=1))
        ir = mean_ic / sd if sd else 0.0
        t = mean_ic / (sd / sqrt(len(arr))) if sd else 0.0
        verdict = ("★ 显著" if abs(t) >= crit
                   else ("~ 未校正显著" if abs(t) >= 1.96 else "× 不显著"))
        print(f"  {label:<22}{len(arr):>7}{mean_ic:>+9.4f}{ir:>7.2f}{t:>+8.2f}"
              f"{(arr > 0).mean() * 100:>8.1f}%  {verdict}")
        lines.append((label, len(arr), mean_ic, ir, t, verdict))
        if abs(t) >= 1.96:
            rows.append((col, label, mean_ic, t))

    print()
    print("  分组收益（按因子分 5 组，看单调性与首尾差）")
    print("-" * 88)
    for col, label, mean_ic, t in rows:
        sub = df[[col, RET, "open_premium"]].dropna()
        if len(sub) < 500:
            continue
        sub = sub.copy()
        try:
            sub["g"] = pd.qcut(sub[col].rank(method="first"), 5, labels=False)
        except ValueError:
            continue
        print(f"\n  【{label}】IC {mean_ic:+.4f} (t={t:+.2f})")
        stats = sub.groupby("g").agg(
            n=(RET, "size"), prem=("open_premium", "mean"),
            ret=(RET, "mean"), win=(RET, lambda s: (s > 0).mean() * 100),
        )
        for g, r in stats.iterrows():
            print(f"    第 {int(g) + 1} 组　溢价 {r['prem']:>+6.2f}%　"
                  f"买入后 {r['ret']:>+6.2f}%　胜率 {r['win']:>5.1f}%　n={int(r['n'])}")
        print(f"    首尾组差：{stats['ret'].iloc[-1] - stats['ret'].iloc[0]:+.2f}%")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    path = OUT_DIR / f"dragon_ic_{datetime.now():%Y%m%d_%H%M%S}.md"
    body = ["=" * 78, "  龙虎榜因子有效性检验", "=" * 78, "",
            f"样本 {len(df)} 条 / {df['date'].nunique()} 天", "",
            f"{'因子':<22}{'均值IC':>9}{'t值':>8}  判定", "-" * 78]
    body += [f"{a:<22}{c:>+9.4f}{e:>+8.2f}  {f}" for a, b, c, d, e, f in lines]
    path.write_text("\n".join(body), encoding="utf-8")
    print(f"\n报告：{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
