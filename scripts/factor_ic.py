# -*- coding: utf-8 -*-
"""涨停因子有效性检验（IC + 分组收益）。

回答的问题：涨停池/板块数据里的字段，**哪些真的能预测次日收益**？
只有通过检验的因子才有资格进评分器；通不过的，就老实待在报告里当展示信息。

四个检验维度：
1. **IC（信息系数）**：每个交易日做一次横截面 Spearman 秩相关（因子 vs ret1），
   再对日期求均值。IR = 均值/标准差衡量稳定性，t 值衡量统计显著性。
2. **多重检验校正**：同时检验十几个因子时，|t| > 2 的阈值会产生假阳性
   （检验 11 次，至少一次误判的概率约 43%）。因此用 Bonferroni 校正后的
   阈值判定，**未过校正的因子一律按「不显著」对待**。
3. **分组收益**：按因子值分 5 组，看单调性与首尾组差。
   IC 可能被极端值影响，分组收益是更直观的佐证。
4. **样本稳定性**：把区间对半切开，分别算 IC。
   全区间显著但只有一段显著 ⇒ 该结论依赖特定市场环境，不可当作稳定规律。

⚠️ 已知局限：本脚本用同一份数据既做发现又做检验，**无法排除过拟合**。
真正的样本外验证需要另一段独立区间（目前库里只有约 1 年数据）。

用法：
    python scripts/factor_ic.py            # 重建因子表并检验
    python scripts/factor_ic.py --no-build # 直接用已有因子表
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from statistics import NormalDist
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from astock.storage.db import get_storage  # noqa: E402

# 个股级因子：同一天内不同股票取值不同，适合横截面 IC
FACTORS: list[tuple[str, str, int]] = [
    ("boards", "连板高度", 0),
    ("seal_ratio", "封单额/成交额", 0),
    ("seal_money", "封单额", 0),
    ("first_minutes", "首次涨停时间(越早越小)", -1),
    ("turn", "换手率", 0),
    ("float_mv", "流通市值", 0),
    ("theme_heat", "题材热度(最热概念涨停数)", 0),
    ("sector_strength", "板块强度", 0),
    ("pct_5d", "前5日涨幅", 0),
]

# 市场级因子：同一天内所有股票取值相同，**横截面 IC 在数学上无意义**
# （分母方差为 0）。它们只能做时间序列检验：
# 「今天的情绪指标能否预测明天的市场/打板收益」。
MARKET_FACTORS: list[tuple[str, str]] = [
    ("market_limit_up", "全市场涨停家数"),
    ("market_break", "全市场炸板家数"),
    ("break_rate", "炸板率"),
]

RET = "ret1"


def t_crit(n_tests: int, alpha: float = 0.05) -> float:
    """Bonferroni 校正后的 t 阈值。

    同时检验多个因子时，|t| > 2 会带来假阳性：检验 11 次时
    「至少一次误判为显著」的概率约 43%。校正后阈值提高到约 2.6，
    代价是更保守 —— 对「决定要不要进评分器」这种决策，保守是对的。
    """
    if n_tests <= 0:
        return 1.96
    return NormalDist().inv_cdf(1 - alpha / (2 * n_tests))


def ic_series(df: pd.DataFrame, factor: str, ret: str) -> pd.Series:
    """逐日横截面 Spearman IC 序列（索引为日期）。"""
    sub = df[["date", factor, ret]].dropna()
    if sub.empty:
        return pd.Series(dtype=float)
    out: dict[Any, float] = {}
    for d, g in sub.groupby("date"):
        if len(g) < 5 or g[factor].nunique() < 3:
            continue
        ic = g[factor].rank().corr(g[ret].rank())
        if pd.notna(ic):
            out[d] = float(ic)
    return pd.Series(out).sort_index()


def ic_stats(s: pd.Series) -> dict:
    """由 IC 序列得到均值/IR/t 值。"""
    if s is None or len(s) < 20:
        return {"days": 0 if s is None else len(s)}
    arr = s.to_numpy(dtype=float)
    mean = float(arr.mean())
    std = float(arr.std(ddof=1))
    ir = mean / std if std else 0.0
    return {
        "days": len(arr),
        "mean_ic": mean,
        "ir": ir,
        "t": float(ir * np.sqrt(len(arr))),
        "pos_ratio": float((arr > 0).mean() * 100),
    }


def group_returns(df: pd.DataFrame, factor: str, ret: str, q: int = 5) -> pd.DataFrame:
    """按因子分 q 组，返回每组收益均值、中位数、胜率、样本数。"""
    sub = df[["date", factor, ret]].dropna()
    if sub.empty:
        return pd.DataFrame()
    try:
        sub["grp"] = pd.qcut(sub[factor].rank(method="first"), q, labels=False)
    except ValueError:
        return pd.DataFrame()
    g = sub.groupby("grp")[ret].agg(["mean", "median", "count"])
    g["win"] = sub.groupby("grp")[ret].apply(lambda s: (s > 0).mean() * 100)
    return g


def main() -> int:
    parser = argparse.ArgumentParser(description="涨停因子有效性检验")
    parser.add_argument("--no-build", action="store_true", help="跳过因子表重建")
    args = parser.parse_args()

    storage = get_storage()

    if not args.no_build:
        from astock.features.limit_factor import LimitFactorBuilder

        print("重建涨停因子表 …")
        n = LimitFactorBuilder(storage).build()
        print(f"  完成：{n} 行\n")

    df = storage.query_df("SELECT * FROM dws_limit_factor")
    if df.empty:
        print("因子表为空，请先执行采集：python -m astock.cli limit-pool --years 1")
        return 1

    lines: list[str] = []

    def out(text: str = "") -> None:
        print(text)
        lines.append(text)

    out("=" * 78)
    out("  涨停因子有效性检验")
    out("=" * 78)
    valid = df.dropna(subset=[RET])
    out(f"  样本：{len(df)} 条涨停记录（{df['date'].nunique()} 个交易日）")
    out(f"        其中 {len(valid)} 条已有次日收益（末尾 T+1/T+3 自然缺失）")
    out(f"  区间：{df['date'].min()} ~ {df['date'].max()}")
    out("")
    out("  收益口径：T 日涨停收盘触发 → T+1 开盘买入。")
    out(f"  全体均值：次日开盘溢价 {valid['open_premium'].mean():+.2f}%"
        f"　买入后当日 {valid[RET].mean():+.2f}%"
        f"　胜率 {(valid[RET] > 0).mean() * 100:.1f}%")
    out(f"  持有 T+3 {valid['ret3'].mean():+.2f}%　T+5 {valid['ret5'].mean():+.2f}%")
    out("")

    # ---- 单因子 IC ----
    crit = t_crit(len(FACTORS))
    false_pos = (1 - 0.95 ** len(FACTORS)) * 100
    out("-" * 78)
    out("  一、单因子 IC（横截面 Spearman，按日计算后取均值）")
    out("-" * 78)
    out(f"  多重检验校正：共检验 {len(FACTORS)} 个因子，"
        f"Bonferroni 校正后 |t| ≥ {crit:.2f} 才算显著")
    out(f"  （若不校正，检验 {len(FACTORS)} 次至少出现一次假阳性的概率约 {false_pos:.0f}%）")
    out("")
    out(f"  {'因子':<24}{'有效日':>6}{'均值IC':>9}{'IR':>8}{'t值':>8}{'IC>0占比':>10}  判定")
    out("-" * 78)
    rows = []
    for col, label, direction in FACTORS:
        s = ic_series(valid, col, RET)
        st = ic_stats(s)
        if not st.get("mean_ic"):
            out(f"  {label:<24}{st.get('days', 0):>6}        样本不足")
            continue
        t = st["t"]
        if abs(t) >= crit:
            verdict = "★ 显著"
        elif abs(t) >= 2:
            verdict = "~ 仅未校正显著"
        elif abs(t) >= 1.3:
            verdict = "~ 边缘"
        else:
            verdict = "× 不显著"
        out(f"  {label:<24}{st['days']:>6}{st['mean_ic']:>+9.4f}"
            f"{st['ir']:>8.2f}{t:>+8.2f}{st['pos_ratio']:>9.1f}%  {verdict}")
        rows.append((col, label, st, direction, s))

    # ---- 分组收益（只对显著/边缘因子展开） ----
    out("")
    out("-" * 78)
    out("  二、分组收益（按因子分 5 组，看单调性与首尾差）")
    out("-" * 78)
    for col, label, st, direction, _s in rows:
        if abs(st["t"]) < 2:
            continue
        g = group_returns(valid, col, RET)
        if g.empty:
            continue
        out("")
        out(f"  【{label}】IC {st['mean_ic']:+.4f} (t={st['t']:+.2f})")
        for grp, r in g.iterrows():
            out(f"    第 {int(grp) + 1} 组　均值 {r['mean']:>+7.2f}%　"
                f"中位 {r['median']:>+7.2f}%　胜率 {r['win']:>5.1f}%　n={int(r['count'])}")
        spread = float(g["mean"].iloc[-1] - g["mean"].iloc[0])
        out(f"    首尾组差：{spread:+.2f}%")

    # ---- 样本稳定性 ----
    out("")
    out("-" * 78)
    out("  三、样本稳定性（区间对半切分，检验结论是否只依赖某一段行情）")
    out("-" * 78)
    out("  仅全区间显著、但只有一段显著 ⇒ 该结论不可当作稳定规律")
    out("")
    out(f"  {'因子':<24}{'前半段IC':>10}{'后半段IC':>10}{'同号':>6}  判定")
    out("-" * 78)
    for col, label, st, _direction, s in rows:
        half = len(s) // 2
        first = ic_stats(s.iloc[:half])
        second = ic_stats(s.iloc[half:])
        if not first.get("mean_ic") or not second.get("mean_ic"):
            continue
        same = (first["mean_ic"] > 0) == (second["mean_ic"] > 0)
        weak = min(abs(first["mean_ic"]), abs(second["mean_ic"]))
        if same and weak >= 0.01:
            verdict = "稳定"
        elif same:
            verdict = "同号但偏弱"
        else:
            verdict = "✖ 方向翻转"
        out(f"  {label:<24}{first['mean_ic']:>+10.4f}{second['mean_ic']:>+10.4f}"
            f"{('是' if same else '否'):>6}  {verdict}")

    # ---- 连板高度专项 ----
    out("")
    out("-" * 78)
    out("  四、连板高度专项（打板者的核心问题：追高还是打首板）")
    out("-" * 78)
    board = valid.dropna(subset=["boards"]).copy()
    board["档"] = board["boards"].clip(upper=4).map(
        {1: "首板", 2: "2连板", 3: "3连板", 4: "4连板及以上"}
    )
    agg = board.groupby("档").agg(
        样本=("code", "count"),
        次日溢价=("open_premium", "mean"),
        当日收益=(RET, "mean"),
        胜率=(RET, lambda s: (s > 0).mean() * 100),
        T3=("ret3", "mean"),
    )
    order = ["首板", "2连板", "3连板", "4连板及以上"]
    agg = agg.reindex([o for o in order if o in agg.index])
    out(f"  {'档位':<14}{'样本':>7}{'次日溢价':>10}{'当日收益':>10}{'胜率':>8}{'T+3':>9}")
    out("-" * 78)
    for idx, r in agg.iterrows():
        out(f"  {idx:<14}{int(r['样本']):>7}{r['次日溢价']:>+9.2f}%"
            f"{r['当日收益']:>+9.2f}%{r['胜率']:>7.1f}%{r['T3']:>+8.2f}%")

    # ---- 市场级因子（时间序列） ----
    out("")
    out("-" * 78)
    out("  五、市场级因子（情绪指标 → 次日市场/打板收益）")
    out("-" * 78)
    out("  说明：这类因子同日对所有股票取值相同，横截面 IC 无意义，只能在时间轴上检验。")

    mkt = df.groupby("date").agg(
        lu=("market_limit_up", "max"),
        br=("market_break", "max"),
        brr=("break_rate", "max"),
        prem=("open_premium", "mean"),
        r1=(RET, "mean"),
        n=("code", "count"),
    ).reset_index()
    first_only = (
        df[df["boards"] <= 1].groupby("date")[RET].mean().rename("r1_first").reset_index()
    )
    mkt = mkt.merge(first_only, on="date", how="left")

    idx = storage.query_df(
        "SELECT date, close, LEAD(close, 1) OVER (ORDER BY date) AS nc "
        "FROM dwd_index_bar WHERE code = 'sh.000001'"
    )
    idx["idx_ret"] = (idx["nc"] / idx["close"] - 1) * 100
    mkt = mkt.merge(idx[["date", "idx_ret"]], on="date", how="left")
    mkt = mkt.dropna(subset=["idx_ret"])

    out("")
    out(f"  {'情绪指标':<18}{'vs 次日指数':>12}{'vs 次日首板收益':>16}")
    out("-" * 78)
    for col, label in [("lu", "涨停家数"), ("brr", "炸板率"), ("prem", "次日开盘溢价")]:
        ic_idx = mkt[col].rank().corr(mkt["idx_ret"].rank())
        sub = mkt.dropna(subset=["r1_first"])
        ic_lu = sub[col].rank().corr(sub["r1_first"].rank())
        out(f"  {label:<18}{ic_idx:>+12.3f}{ic_lu:>+16.3f}")

    out("")
    out("  按「涨停家数」分档（次日指数收益 / 次日首板平均收益 / 首板胜率）")
    out("-" * 78)
    bins = [0, 40, 60, 80, 100, 10000]
    labels = ["<40（冰点）", "40~60（低迷）", "60~80（正常）", "80~100（活跃）", ">100（高潮）"]
    mkt["情绪档"] = pd.cut(mkt["lu"], bins=bins, labels=labels, right=False)
    for lab, g in mkt.groupby("情绪档", observed=True):
        out(f"    {lab:<16} 天数 {len(g):>3}　次日指数 {g['idx_ret'].mean():>+6.2f}%　"
            f"次日首板 {g['r1_first'].mean():>+6.2f}%　"
            f"首板胜率 {(g['r1_first'] > 0).mean() * 100:>5.1f}%")

    # ---- 保存 ----
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = ROOT / "data" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"factor_ic_{stamp}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print()
    print(f"报告已保存：{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
