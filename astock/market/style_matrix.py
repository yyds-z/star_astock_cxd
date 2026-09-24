# -*- coding: utf-8 -*-
"""状态 × 规则 表现矩阵（`dws_style_matrix`）。

------------------------------------------------------------------
它回答什么
------------------------------------------------------------------
「这个市场状态下，哪条规则真的更好？」——为「状态 → 规则组合」的绑定
提供有统计依据的初始值。这是自适应风格机制的 **P0**：
**先验证思路是否成立，再决定要不要投后面的阶段。**

------------------------------------------------------------------
三个必须做的统计处理（缺一不可）
------------------------------------------------------------------
① **收缩估计**：6 状态 × N 规则 = 几十个格子，而短线档日均仅 2.1 只信号，
   很多格子只有几十个样本。直接按均值排序 = 把噪声当规律。
   格子分 = (n × 格子分 + k × 该规则全局分) / (n + k)，样本越少越往全局靠。

② **最小样本门槛**：n < min_samples 的格子标记为 thin，**不参与绑定**。

③ **Welch t 检验**：不只比均值大小，还检验「该规则在此状态 vs 在其它状态」
   是否真有差异。否则随机波动会被读成"这个状态适合这套打法"。

------------------------------------------------------------------
两个口径细节
------------------------------------------------------------------
· **按规则展开**：`ads_backtest.all_strategies` 是拼接串（"turtle_trade+rps_breakout"），
  按整串分组会把"组合"当成规则。这里按 `+` 拆分，信号与其命中的**每条规则**
  都算一次 —— 这才是"规则触发后的表现"。
· **收益同口径**：`ret1` 是 T+1 的「开盘→收盘」，所以基准也必须取
  **指数当日的开盘→收盘**，不能取收盘→收盘（那是另一种口径，两者相减无意义）。
"""

from __future__ import annotations

from datetime import date
from math import sqrt

import pandas as pd

from astock.config import get_config
from astock.logger import get_logger
from astock.storage.db import Storage, get_storage

logger = get_logger("market.style_matrix")

BENCH_PREFERENCE = ["sh.000300", "sh.000001"]


def _score(win_rate: float, excess: float | None) -> float:
    """综合分 = 0.4×胜率分 + 0.6×超额分（与「昨日回顾」板块同一口径）。"""
    win_part = max(0.0, min(100.0, float(win_rate)))
    if excess is None:
        return round(win_part, 2)
    excess_part = max(0.0, min(100.0, 50.0 + float(excess) * 15.0))
    return round(0.4 * win_part + 0.6 * excess_part, 2)


def _welch_t(a: pd.Series, b: pd.Series) -> float | None:
    """Welch t 值（不假设方差相等）。样本不足或方差为 0 时返回 None。"""
    if len(a) < 5 or len(b) < 5:
        return None
    v1, v2 = a.var(ddof=1), b.var(ddof=1)
    se = sqrt(v1 / len(a) + v2 / len(b))
    if not se or se != se:
        return None
    return round(float((a.mean() - b.mean()) / se), 2)


class StyleMatrixBuilder:
    """状态 × 规则表现矩阵构建器。"""

    def __init__(self, storage: Storage | None = None) -> None:
        self.cfg = get_config()
        self.storage = storage or get_storage()
        self.shrink_k = int(self.cfg.get("style_matrix.shrink_k", 40))
        self.min_samples = int(self.cfg.get("style_matrix.min_samples", 20))

    # ---------------- 数据 ----------------
    def _load(self, run_id: str | None) -> pd.DataFrame:
        run_id = run_id or self.storage.query_value(
            "SELECT MAX(run_id) FROM ads_backtest"
        )
        if not run_id:
            raise RuntimeError("ads_backtest 无数据，先跑一次 backtest")
        self.run_id = run_id
        df = self.storage.query_df(
            """
            SELECT data_date, trade_date, market_state, ret1,
                   UNNEST(STRING_SPLIT(all_strategies, '+')) AS rule
            FROM ads_backtest
            WHERE run_id = ? AND ret1 IS NOT NULL AND all_strategies IS NOT NULL
            """,
            [run_id],
        )
        # 基准：指数在**同一交易日**的开盘→收盘（与 ret1 同口径）
        codes = self.storage.query_df(
            "SELECT DISTINCT code FROM dwd_index_bar"
        )["code"].astype(str).tolist()
        bench_code = next((c for c in BENCH_PREFERENCE if c in codes), None)
        self.benchmark_code = bench_code
        if bench_code:
            bench = self.storage.query_df(
                "SELECT date, (close / NULLIF(open, 0) - 1) * 100 AS bench "
                "FROM dwd_index_bar WHERE code = ?",
                [bench_code],
            )
            df = df.merge(
                bench, left_on="trade_date", right_on="date", how="left"
            ).drop(columns=["date"])
        else:
            df["bench"] = None
        for c in ("market_state", "rule"):
            df[c] = df[c].astype(str)
        return df.dropna(subset=["ret1"])

    # ---------------- 主流程 ----------------
    def build(self, run_id: str | None = None) -> pd.DataFrame:
        df = self._load(run_id)
        logger.info("载入 %d 条信号（展开后按规则计），基准=%s", len(df), self.benchmark_code)

        rows = []
        for (state, rule), g in df.groupby(["market_state", "rule"]):
            others = df[(df["rule"] == rule) & (df["market_state"] != state)]
            win = float((g["ret1"] > 0).mean() * 100)
            avg = float(g["ret1"].mean())
            bench = float(g["bench"].mean()) if g["bench"].notna().any() else None
            excess = None if bench is None else round(avg - bench, 3)
            rows.append(
                {
                    "state": state,
                    "rule": rule,
                    "n": len(g),
                    "win_rate": round(win, 2),
                    "avg_ret1": round(avg, 3),
                    "benchmark": None if bench is None else round(bench, 3),
                    "excess": excess,
                    "score": _score(win, excess),
                    "t_stat": _welch_t(g["ret1"], others["ret1"]) if len(others) else None,
                    "_rets": g["ret1"],
                }
            )
        cell = pd.DataFrame(rows)

        # 该规则的全局分 = 收缩目标
        glob = df.groupby("rule").apply(
            lambda g: _score(
                (g["ret1"] > 0).mean() * 100,
                None if g["bench"].notna().sum() == 0
                else float(g["ret1"].mean()) - float(g["bench"].mean()),
            ),
            include_groups=False,
        ).to_dict()
        cell["score_global"] = cell["rule"].map(glob).astype(float)
        cell["score_shrunk"] = (
            (cell["n"] * cell["score"] + self.shrink_k * cell["score_global"])
            / (cell["n"] + self.shrink_k)
        ).round(2)

        # 判定：样本不足 / 显著更好 / 显著更差 / 不显著
        def verdict(r) -> str:
            if r["n"] < self.min_samples:
                return "thin"
            if r["t_stat"] is None or pd.isna(r["t_stat"]):
                return "weak"
            if r["t_stat"] >= 2.0:
                return "edge"
            # 负向显著**同样重要**：它说明「这条规则在这个状态下明显更差」，是可操作的
            # 信息（应避开）。原先只判 `t >= 2`，把 t ≤ -2 归进"不显著"——实测
            # range 状态的 ma_multi_trend 是 t = -3.13，却被标成"不显著"，
            # 真实信号被报告自己掩盖了。
            if r["t_stat"] <= -2.0:
                return "bad"
            return "weak"

        cell["verdict"] = cell.apply(verdict, axis=1)
        cell["run_id"] = self.run_id
        cell["window_end"] = df["data_date"].max()

        out = cell.drop(columns=["_rets"])
        self.storage.execute(
            "DELETE FROM dws_style_matrix WHERE run_id = ?", [self.run_id]
        )
        self.storage.upsert_df(
            out[
                ["run_id", "window_end", "state", "rule", "n", "win_rate",
                 "avg_ret1", "benchmark", "excess", "score", "score_global",
                 "score_shrunk", "t_stat", "verdict"]
            ],
            "dws_style_matrix",
        )
        logger.info("风格矩阵已落库：%d 个格子（轮次 %s）", len(out), self.run_id)
        return out

    # ---------------- 报告 ----------------
    def report(self, cell: pd.DataFrame) -> str:
        """P0 的验收物：既要给出绑定建议，也要**明确指出思路是否成立**。"""
        lines = [
            "=" * 92,
            "  状态 × 规则 表现矩阵（P0：先验证思路是否成立）",
            "=" * 92,
            f"  回测轮次：{self.run_id}　截止：{cell['window_end'].max()}",
            f"  基准：{self.benchmark_code}（开→收，与 ret1 同口径）",
            f"  收缩参数 k={self.shrink_k}（伪样本量）　最小样本门槛 {self.min_samples}",
            "",
        ]
        n_thin = int((cell["verdict"] == "thin").sum())
        n_edge = int((cell["verdict"] == "edge").sum())
        n_bad = int((cell["verdict"] == "bad").sum())
        lines += [
            f"  格子总数 {len(cell)}：样本不足 {n_thin}　显著更好 {n_edge}　"
            f"显著更差 {n_bad}　不显著 {len(cell) - n_thin - n_edge - n_bad}",
            "",
            f"  {'状态':<10}{'规则':<26}{'n':>6}{'胜率':>8}{'超额':>9}"
            f"{'格子分':>8}{'收缩分':>8}{'t':>7}  判定",
            "-" * 92,
        ]
        # 按「可操作程度」排序：显著更好 → 显著更差（要避开）→ 不显著 → 样本不足
        order = {"edge": 0, "bad": 1, "weak": 2, "thin": 3}
        for _, r in cell.sort_values(
            ["state", "verdict", "score_shrunk"],
            key=lambda s: s.map(order) if s.name == "verdict" else s,
            ascending=[True, True, False],
        ).iterrows():
            ex = "-" if r["excess"] is None or pd.isna(r["excess"]) else f"{r['excess']:+.2f}%"
            t = "-" if r["t_stat"] is None or pd.isna(r["t_stat"]) else f"{r['t_stat']:+.2f}"
            mark = {
                "edge": "★ 显著更好", "bad": "▼ 显著更差",
                "weak": "~ 不显著", "thin": "× 样本不足",
            }[r["verdict"]]
            lines.append(
                f"  {r['state']:<10}{r['rule']:<26}{int(r['n']):>6}{r['win_rate']:>7.1f}%"
                f"{ex:>9}{r['score']:>8.1f}{r['score_shrunk']:>8.1f}{t:>7}  {mark}"
            )

        lines += [
            "", "-" * 92,
            "  各状态下的绑定建议（仅采纳样本够、且**未显著更差**的格子，按收缩分排序）",
            "-" * 92,
        ]
        ok = cell[cell["verdict"] != "thin"]        # 样本够（结论段统计用）
        sug = cell[~cell["verdict"].isin(["thin", "bad"])]   # 可进入绑定建议的格子
        for state, g in sug.groupby("state"):
            top = g.sort_values("score_shrunk", ascending=False).head(3)
            parts = [
                f"{r['rule']}({r['score_shrunk']:.1f}{'★' if r['verdict'] == 'edge' else ''})"
                for _, r in top.iterrows()
            ]
            lines.append(f"  {state:<10}{'、'.join(parts) if parts else '无可用格子'}")

        lines += ["", "=" * 92, "  结论", "=" * 92]
        usable = len(ok)
        if usable == 0:
            lines.append("  ❌ 所有格子样本都不足 → **状态级绑定这条思路不可行**。")
            lines.append("     原因：信号总量不足以支撑「状态 × 规则」的切分。")
            lines.append("     可选出路：① 合并市场状态（6 → 3 档）② 扩规则库前先扩样本（延长时间窗口）")
        elif n_edge == 0:
            lines.append(
                f"  ⚠️ {usable} 个格子样本够，但**没有一个在特定状态下显著更好**（t ≥ +2）。"
            )
            if n_bad:
                lines.append(
                    f"     另有 {n_bad} 个格子**显著更差**（t ≤ -2）—— 负面信号是真实存在的："
                )
                lines.append("     应「在这些状态下避开该规则」，而不是指望状态切换带来增益。")
            lines.append("     也就是说：规则的表现更多取决于它自己，而不是市场状态。")
            lines.append("     → 状态级绑定的边际价值有限，建议改用「统一按近期表现排序」，")
            lines.append("       而不是先做状态切换。")
        else:
            lines.append(f"  ✅ {n_edge} 个格子在特定状态下显著更好 → 状态级绑定**有依据**。")
            lines.append(
                f"     （另有 {usable - n_edge - n_bad} 个格子样本够但差异不显著，绑定时应降权；"
            )
            lines.append(f"       {n_bad} 个格子显著更差，应避开。）")
        lines.append("")
        return "\n".join(lines)


if __name__ == "__main__":
    import sys

    b = StyleMatrixBuilder()
    try:
        cell = b.build()
    except Exception as exc:  # noqa: BLE001
        print(f"[!] 构建失败：{exc}")
        sys.exit(1)
    print(b.report(cell))
