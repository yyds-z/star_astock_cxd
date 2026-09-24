# -*- coding: utf-8 -*-
"""离场规则的经验首达概率（Exit Policy）。

解决的问题：
系统目前只管「买什么」（选股 + 次日开盘买入），**完全不管什么时候走**。
但回测数据自己指出了缺口：T+1 +0.70% → T+3 0.00% → T+5 -0.33%，
而最大涨幅均值 14.4%、最大回撤 -11.0% —— 冲高空间很大却全部吐回。
这说明当前最大的价值不在选股，而在**离场规则**。

本脚本用真实推荐样本（ads_backtest）还原持有路径，统计
「先触止盈 / 先触止损 / 超时到期」三类互斥事件的**经验概率**，
并搜索最优 (止盈X, 止损Y, 持有N) 组合。

这相当于「隐马尔可夫条件交易」文档 §7/§8 的**可验证简化版**：
用经验频率代替 HMM，先回答"最简单的版本够不够用"，
不够再引入隐状态 —— 避免先搭脚手架再发现不需要。

------------------------------------------------------------------
三个必须处理的方法论细节（忽略任何一个结论都会失真）
------------------------------------------------------------------
1. **A 股 T+1 制度**：买入当日**不能卖出**。因此止盈/止损从**买入次日起**
   检查，N 是"可卖交易日数"。若按买入日当天检查，会得出无法执行的策略。

2. **日线数据看不出"同一天内先触哪个"**：某日 high 与 low 同时越过止盈与止损线时，
   真实先后顺序无法从日线得知。这里采用**保守假设：按先止损处理**
   （不夸大收益）。文档 §12 也强调这一点。该假设会让结果偏保守，是刻意的。

3. **基准口径**：必须与"什么都不做"对比才有意义。给出两个可执行基准：
   · T+1 开盘无脑卖（最保守、最简单）
   · T+1 收盘卖（对应系统复盘的口径）
   而系统回测里的 ret1（买入日收盘）**不能当天卖出**，只作纸面参考。

用法：
    python scripts\\exit_policy.py                 # 最新一轮回测
    python scripts\\exit_policy.py --run-id xxx    # 指定轮次
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from astock.storage.db import get_storage  # noqa: E402

# 搜索网格
X_GRID = [3.0, 5.0, 8.0, 10.0, 15.0]      # 止盈(%)
Y_GRID = [2.0, 3.0, 5.0, 8.0]             # 止损(%)
N_GRID = [1, 2, 3, 5, 10]                 # 最多持有几个可卖交易日


def load_samples(storage, run_id: str) -> pd.DataFrame:
    return storage.query_df(
        """
        SELECT code, data_date, trade_date, tier, strategy, market_state
        FROM ads_backtest
        WHERE run_id = ?
        ORDER BY trade_date
        """,
        [run_id],
    )


def load_bars(storage, codes: list[str], start, end) -> dict[str, dict]:
    """按股票加载日线，并预建日期 → 行号索引（避免样本内重复查找）。"""
    df = storage.query_df(
        """
        SELECT code, date, open, high, low, close
        FROM dwd_daily_bar
        WHERE code IN (SELECT UNNEST(?)) AND date BETWEEN ? AND ?
        ORDER BY code, date
        """,
        [codes, start, end],
    )
    if df.empty:
        return {}
    out: dict[str, dict] = {}
    for code, g in df.groupby("code", sort=False):
        g = g.reset_index(drop=True)
        # 必须统一成 datetime.date：DuckDB 的 DATE 列经 pandas 读出是 Timestamp，
        # 而调用方用的是 `.date()` 得到的 date，两者**不相等**，
        # 字典查找会全部落空 —— 表现为"样本全被跳过"且**不报任何错**。
        dlist = [pd.Timestamp(x).date() for x in g["date"].tolist()]
        out[str(code)] = {
            "date": dlist,
            "open": g["open"].to_numpy(dtype=float),
            "high": g["high"].to_numpy(dtype=float),
            "low": g["low"].to_numpy(dtype=float),
            "close": g["close"].to_numpy(dtype=float),
            "pos": {d: i for i, d in enumerate(dlist)},
        }
    return out


def simulate_one(bar: dict, day0, x: float, y: float, n: int) -> dict | None:
    """模拟单笔交易。返回 None 表示数据不足。

    day0 = 买入日（trade_date），买入价为当日开盘。
    止盈/止损从 day0 之后第 1 个交易日开始检查（T+1 制度）。
    """
    i0 = bar["pos"].get(day0)
    if i0 is None:
        return None
    entry = bar["open"][i0]
    if not entry or np.isnan(entry):
        return None

    length = len(bar["date"])
    out = {
        "entry": entry,
        "t1_open": (bar["open"][i0 + 1] / entry - 1) * 100 if i0 + 1 < length else None,
        "t1_close": (bar["close"][i0 + 1] / entry - 1) * 100 if i0 + 1 < length else None,
        "t0_close": (bar["close"][i0] / entry - 1) * 100,  # 纸面参考（当天卖不掉）
    }

    tp = entry * (1 + x / 100)
    sl = entry * (1 - y / 100)
    for k in range(1, n + 1):
        j = i0 + k
        if j >= length:
            break
        o = bar["open"][j]
        # ---- 跳空优先（**这一步决定结论是否成立**）----
        # 若开盘就低于止损线，不可能在止损价成交（开盘即已跌破），
        # 只能按开盘价卖出 —— 止损率越高，这条越关键。
        # 初版漏了这一步，导致"止损 2%"看起来白白多赚 1.5%，实为虚假。
        if o <= sl:
            return {**out, "exit": f"gap_sl{k}", "ret": (o / entry - 1) * 100, "days": k}
        if o >= tp:
            return {**out, "exit": f"gap_tp{k}", "ret": (o / entry - 1) * 100, "days": k}
        hit_tp = bar["high"][j] >= tp
        hit_sl = bar["low"][j] <= sl
        if hit_sl:  # 同日双触按先止损（保守）
            return {**out, "exit": f"sl{k}", "ret": -y, "days": k}
        if hit_tp:
            return {**out, "exit": f"tp{k}", "ret": x, "days": k}
    else:
        j = i0 + n
        if j < length:
            return {
                **out,
                "exit": "timeout",
                "ret": (bar["close"][j] / entry - 1) * 100,
                "days": n,
            }
    return None  # 后续数据不足


def main() -> int:
    parser = argparse.ArgumentParser(description="离场规则的经验首达概率")
    parser.add_argument("--run-id", type=str, default=None, help="回测轮次，默认最新")
    parser.add_argument("--top", type=int, default=20, help="展示前 N 个组合")
    args = parser.parse_args()

    storage = get_storage()
    run_id = args.run_id or storage.query_value("SELECT MAX(run_id) FROM ads_backtest")
    if not run_id:
        print("ads_backtest 为空，请先跑回测：python -m astock.cli backtest --top-n 5")
        return 1

    samples = load_samples(storage, run_id)
    if samples.empty:
        print(f"轮次 {run_id} 无样本")
        return 1

    trade_dates = pd.to_datetime(samples["trade_date"])
    start = trade_dates.min().date()
    end = (trade_dates.max() + pd.Timedelta(days=40)).date()
    codes = [str(c) for c in samples["code"].unique()]

    print(f"加载日线：{len(codes)} 只股票，区间 {start} ~ {end} …")
    bars = load_bars(storage, codes, start, end)
    print(f"已加载 {len(bars)} 只股票的日线\n")

    lines: list[str] = []

    def out(text: str = "") -> None:
        print(text)
        lines.append(text)

    # ---- 基准 ----
    recs = []
    skipped_no_bar = skipped_no_date = 0
    for row in samples.to_dict("records"):
        bar = bars.get(str(row["code"]))
        if bar is None:
            skipped_no_bar += 1
            continue
        d0 = pd.Timestamp(row["trade_date"]).date()
        i0 = bar["pos"].get(d0)
        if i0 is None or not bar["open"][i0]:
            skipped_no_date += 1
            continue
        entry = bar["open"][i0]
        n_len = len(bar["date"])
        recs.append(
            {
                "code": row["code"],
                "tier": row["tier"],
                "strategy": row["strategy"],
                "market_state": row["market_state"],
                "day0": d0,
                "i0": i0,
                "entry": entry,
                "have": n_len - i0 - 1,  # 之后还有几个交易日可用
                "t0_close": (bar["close"][i0] / entry - 1) * 100,
                "t1_open": (bar["open"][i0 + 1] / entry - 1) * 100 if i0 + 1 < n_len else np.nan,
                "t1_close": (bar["close"][i0 + 1] / entry - 1) * 100 if i0 + 1 < n_len else np.nan,
            }
        )
    base = pd.DataFrame(recs)
    if base.empty:
        # 静默失败最难查，因此把跳过原因打出来
        print(f"没有可用样本。诊断：无日线 {skipped_no_bar} 笔，"
              f"日线中找不到买入日 {skipped_no_date} 笔，"
              f"总样本 {len(samples)} 笔")
        return 1
    if skipped_no_bar or skipped_no_date:
        print(f"（已跳过：无日线 {skipped_no_bar} 笔，缺买入日 {skipped_no_date} 笔）")

    out("=" * 88)
    out("  离场规则 · 经验首达概率")
    out("=" * 88)
    out(f"  回测轮次：{run_id}")
    out(f"  样本：{len(base)} 笔　股票 {base['code'].nunique()} 只　"
        f"区间 {base['day0'].min()} ~ {base['day0'].max()}")
    out("")
    out("  基准（买入价 = 买入日开盘，均不可当日卖出）：")
    out(f"    · 买入日收盘（纸面，实际卖不掉）：{base['t0_close'].mean():+.2f}%　"
        f"胜率 {(base['t0_close'] > 0).mean() * 100:.1f}%")
    out(f"    · T+1 开盘无脑卖（最保守可执行）：{base['t1_open'].mean():+.2f}%　"
        f"胜率 {(base['t1_open'] > 0).mean() * 100:.1f}%")
    out(f"    · T+1 收盘卖　　　　　　　　　　 ：{base['t1_close'].mean():+.2f}%　"
        f"胜率 {(base['t1_close'] > 0).mean() * 100:.1f}%")

    # ---- 同 N 的「无脑持有」基准 ----
    # 必须逐 N 计算：拿"持有 10 天"的组合去比"持有 1 天的基准"是不公平的
    # （持有期不同，收益根本不可比）。公平对照应同为 N 天。
    hold_base: dict[int, float] = {}
    for n in N_GRID:
        rets = []
        for r in base[base["have"] >= n].to_dict("records"):
            bar = bars[str(r["code"])]
            j = r["i0"] + n
            if j < len(bar["date"]):
                rets.append((bar["close"][j] / r["entry"] - 1) * 100)
        if rets:
            hold_base[n] = float(np.mean(rets))

    out("")
    out("  同持仓期的「无脑持有到收盘」基准（网格的公平对照）：")
    for n in sorted(hold_base):
        out(f"    持有 {n:>2} 天：{hold_base[n]:+.2f}%")

    # ---- 网格搜索 ----
    rows = []
    for x in X_GRID:
        for y in Y_GRID:
            for n in N_GRID:
                use = base[base["have"] >= n]
                if len(use) < 50 or n not in hold_base:
                    continue
                got = []
                for _, r in use.iterrows():
                    bar = bars[str(r["code"])]
                    res = simulate_one(bar, r["day0"], x, y, n)
                    if res:
                        got.append(res)
                if len(got) < 50:
                    continue
                g = pd.DataFrame(got)
                tp = (g["exit"].str.contains("tp")).mean() * 100
                sl = (g["exit"].str.contains("sl")).mean() * 100
                to = (g["exit"] == "timeout").mean() * 100
                exp = float(g["ret"].mean())
                rows.append(
                    {
                        "X": x, "Y": y, "N": n, "样本": len(g),
                        "止盈率": tp, "止损率": sl, "超时率": to,
                        "期望收益": exp,
                        "基准": hold_base[n],
                        # 增量才是决策依据：相对"同样持有 N 天不动"是否真的更好
                        "增量": exp - hold_base[n],
                        "中位收益": g["ret"].median(),
                        "胜率": (g["ret"] > 0).mean() * 100,
                        "平均持有": g["days"].mean(),
                    }
                )
    grid = pd.DataFrame(rows)
    if grid.empty:
        print("网格搜索无有效结果")
        return 1

    out("")
    out("-" * 88)
    out(f"  网格搜索（{len(grid)} 个组合）· 按「相对同 N 无脑持有的增量」排序")
    out("-" * 88)
    out(f"  {'止盈':>5}{'止损':>5}{'持有':>5}{'样本':>7}{'止盈率':>8}{'止损率':>8}"
        f"{'超时率':>8}{'期望收益':>10}{'基准':>8}{'增量':>8}{'胜率':>8}")
    out("-" * 88)
    for _, r in grid.nlargest(args.top, "增量").iterrows():
        out(f"  {r['X']:>5.0f}{r['Y']:>5.0f}{r['N']:>5.0f}{int(r['样本']):>7}"
            f"{r['止盈率']:>7.1f}%{r['止损率']:>7.1f}%{r['超时率']:>7.1f}%"
            f"{r['期望收益']:>+9.2f}%{r['基准']:>+7.2f}%{r['增量']:>+7.2f}%"
            f"{r['胜率']:>7.1f}%")
    best_delta = float(grid["增量"].max())
    out("")
    out(f"  最大增量：{best_delta:+.2f}%（离场规则相对「同样持有期不动」的最好改善）")
    if best_delta < 0.3:
        out("  判定：改善幅度 < 0.3%，**在滑点与佣金面前基本归零** —— "
            "说明这批样本上离场规则几乎没有可提取的价值。")

    # ---- 最优组合的稳健性 ----
    top = grid.nlargest(1, "增量").iloc[0]
    out("")
    out("-" * 88)
    out(f"  最优组合的稳健性检查：止盈 {top['X']:.0f}% / 止损 {top['Y']:.0f}% / "
        f"持有 {top['N']:.0f} 天")
    out("-" * 88)
    near = grid[(grid["N"] == top["N"])].sort_values(["Y", "X"])
    out(f"  固定持有 {top['N']:.0f} 天，看止盈/止损的组合（若最优点是孤峰，多半是过拟合）：")
    # 表头标签单独拼：f-string 表达式内不能出现反斜杠（Python 3.12 以下语法错误）
    corner = "止盈/止损"
    out(f"  {corner:>10}" + "".join(f"{y:>9.0f}%" for y in Y_GRID))
    for x in X_GRID:
        cells = []
        for y in Y_GRID:
            v = near[(near["X"] == x) & (near["Y"] == y)]
            cells.append(f"{v['期望收益'].iloc[0]:>+9.2f}" if not v.empty else f"{'-':>10}")
        out(f"  {x:>9.0f}%" + "".join(cells))

    # ---- 按档位分解 ----
    out("")
    out("-" * 88)
    out(f"  最优组合按档位/状态分解（{top['X']:.0f}% / {top['Y']:.0f}% / {top['N']:.0f}天）")
    out("-" * 88)
    use = base[base["have"] >= int(top["N"])].copy()
    got = []
    for _, r in use.iterrows():
        res = simulate_one(bars[str(r["code"])], r["day0"], top["X"], top["Y"], int(top["N"]))
        if res:
            got.append({**res, "tier": r["tier"], "market_state": r["market_state"]})
    gg = pd.DataFrame(got)
    for key, label in (("tier", "档位"), ("market_state", "市场状态")):
        out(f"  【按{label}】")
        for name, sub in gg.groupby(key):
            out(f"    {str(name):<12} n={len(sub):>5}　期望 {sub['ret'].mean():>+7.2f}%　"
                f"胜率 {(sub['ret'] > 0).mean() * 100:>5.1f}%　"
                f"止盈率 {(sub['exit'].str.contains('tp')).mean() * 100:>5.1f}%　"
                f"止损率 {(sub['exit'].str.contains('sl')).mean() * 100:>5.1f}%")
        out("")

    # ---- 方法论提示 ----
    out("-" * 88)
    out("  读这份结果前必须知道的三件事：")
    out("    1. 止盈/止损从**买入次日**起检查（A 股 T+1 制度，买入当日卖不掉）。")
    out("    2. 某日 high 与 low 同时越过两条线时，日线无法判断先后，")
    out("       本脚本一律**按先止损**处理 —— 结果偏保守，是刻意的。")
    out("    2b. 跳空按开盘价成交（开盘已越过止损线时无法在止损价卖出），")
    out("        这是初版遗漏的关键修正；实盘还有滑点，结果应再打折扣。")
    out("    3. 网格里期望收益最高的组合不一定是真最优：要看上面那张")
    out("       稳健性表是否「连成一片」。孤峰 = 过拟合，平台 = 可信。")
    out("=" * 88)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = ROOT / "data" / "reports" / f"exit_policy_{stamp}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告已保存：{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
