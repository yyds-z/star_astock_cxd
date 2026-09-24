# -*- coding: utf-8 -*-
"""市场状态 × 档位 交叉表现矩阵。

用途：`config/settings.yaml` 里的 `market_regime.tier_weights` 决定了
「什么市况主推哪一档」。这个权重**必须用实测数据校准**，不能凭直觉写 ——
直觉认为「趋势行情该加码波段」，但实测往往相反。

本脚本从最新一次回测（ads_backtest）计算交叉表现，并给出校准建议。

用法：
    python scripts/regime_matrix.py
    python scripts/regime_matrix.py --run-id 20260922_210552
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import duckdb  # noqa: E402

from astock.config import get_config  # noqa: E402

MIN_SAMPLE = 60  # 低于此样本量的格子不参与权重建议（避免过拟合到噪声）

TIER_LABEL = {"short": "短线", "swing": "波段", "value": "价值"}
STATE_LABEL = {
    "trend": "趋势行情",
    "recession": "情绪退潮",
    "euphoria": "情绪高潮",
    "range": "震荡行情",
    "event": "事件驱动",
    "unknown": "状态不明",
}


def latest_run(con) -> str | None:
    """取最近一次回测。

    run_id 形如 20260922_210552（时间戳），因此必须按 run_id 排序。
    按 trade_date 排序是错的 —— 多个 run_id 会覆盖同一天，
    取到的可能是历史遗留的旧批次，导致结论基于过时样本。
    """
    row = con.execute("SELECT run_id FROM ads_backtest ORDER BY run_id DESC LIMIT 1").fetchone()
    return row[0] if row else None


def load(con, run_id: str):
    """返回 {state: {tier: metrics}} 与各状态的总表现。"""
    df = con.execute(
        """
        SELECT market_state, tier,
               COUNT(*)                                              AS n,
               AVG(CASE WHEN ret1 > 0 THEN 1.0 ELSE 0.0 END) * 100   AS win1,
               AVG(ret1)                                             AS ret1,
               AVG(ret3)                                             AS ret3,
               AVG(CASE WHEN ret1 > 0 THEN ret1 END)                 AS avg_win,
               AVG(CASE WHEN ret1 < 0 THEN ret1 END)                 AS avg_loss
        FROM ads_backtest
        WHERE run_id = ?
        GROUP BY 1, 2
        """,
        [run_id],
    ).df()
    matrix: dict[str, dict[str, dict]] = {}
    for _, r in df.iterrows():
        matrix.setdefault(r["market_state"], {})[r["tier"]] = {
            "n": int(r["n"]),
            "win1": float(r["win1"] or 0),
            "ret1": float(r["ret1"] or 0),
            "ret3": float(r["ret3"] or 0),
            "pf": _pf(r["avg_win"], r["avg_loss"]),
        }
    return matrix


def _pf(avg_win, avg_loss) -> float:
    """简单盈亏比（非严格 Profit Factor，用作强度参考）。"""
    try:
        aw, al = float(avg_win), float(avg_loss)
    except (TypeError, ValueError):
        return 0.0
    if al == 0:
        return 0.0
    return round(abs(aw / al), 2)


def suggest(matrix: dict[str, dict[str, dict]]) -> dict[str, dict[str, float]]:
    """按实测 T+1 平均收益给三档分配权重。

    规则：
    - 只保留 T+1 平均收益为正的档位；
    - 权重与「相对超额收益」成正比，让实测最好的档位获得最大配额；
    - 样本不足（< MIN_SAMPLE）的格子按 0 处理，不参与决策。
    """
    out: dict[str, dict[str, float]] = {}
    for state, tiers in matrix.items():
        usable = {
            t: m for t, m in tiers.items()
            if m["n"] >= MIN_SAMPLE and m["ret1"] > 0
        }
        all_tiers = ("short", "swing", "value")
        if not usable:
            out[state] = {t: 0.0 for t in all_tiers}
            continue
        # 以「最高收益」为基准做相对加权，避免绝对值差异被抹平
        best = max(m["ret1"] for m in usable.values())
        raw = {t: (m["ret1"] / best) ** 2 for t, m in usable.items()}
        total = sum(raw.values())
        out[state] = {
            t: round(raw.get(t, 0.0) / total * 100, 0) / 100 for t in all_tiers
        }
    return out


def main() -> int:
    only = None
    if "--run-id" in sys.argv:
        only = sys.argv[sys.argv.index("--run-id") + 1]

    cfg = get_config()
    con = duckdb.connect(str(cfg.duckdb_path), read_only=True)
    try:
        run_id = only or latest_run(con)
        if not run_id:
            print("ads_backtest 为空，请先执行：python -m astock.cli backtest")
            return 1
        matrix = load(con, run_id)
    finally:
        con.close()

    if not matrix:
        print(f"run_id={run_id} 无数据。")
        return 1

    print("=" * 78)
    print(f"  市场状态 × 档位 交叉表现　run_id={run_id}")
    print("=" * 78)
    header = f"  {'市场状态':<10}{'档位':<6}{'信号':>6}{'T+1胜率':>9}{'T+1平均':>9}{'3日平均':>9}{'盈亏比':>8}"
    print(header)
    print("-" * 78)

    # 按「该状态下收益最高的档位」排序，让结论一眼可见
    order = sorted(
        matrix.items(),
        key=lambda kv: -max((m["ret1"] for m in kv[1].values()), default=-99),
    )
    for state, tiers in order:
        label = STATE_LABEL.get(state, state)
        for tier in ("short", "swing", "value"):
            m = tiers.get(tier)
            if m is None:
                continue
            flag = "" if m["n"] >= MIN_SAMPLE else "  ←样本不足"
            print(
                f"  {label:<10}{TIER_LABEL[tier]:<6}{m['n']:>6}"
                f"{m['win1']:>8.1f}%{m['ret1']:>9.2f}{m['ret3']:>9.2f}{m['pf']:>8.2f}{flag}"
            )
        print("-" * 78)

    # ---- 权重校准建议 ----
    print()
    print("=" * 78)
    print("  权重校准建议（对照 config/settings.yaml → market_regime.tier_weights）")
    print("=" * 78)

    current = get_config().get("market_regime.tier_weights", {}) or {}
    suggested = suggest(matrix)

    print(f"  {'市场状态':<10}{'当前 short/swing/value':<28}{'建议 short/swing/value':<28}{'差异'}")
    print("-" * 78)
    for state in STATE_LABEL:
        if state not in matrix:
            continue
        cur = current.get(state)
        sug = suggested.get(state)
        if not sug:
            continue
        cur_txt = "未配置" if not cur else (
            f"{cur.get('short', 0):.2f} / {cur.get('swing', 0):.2f} / {cur.get('value', 0):.2f}"
        )
        sug_txt = f"{sug['short']:.2f} / {sug['swing']:.2f} / {sug['value']:.2f}"
        if cur:
            delta = max(
                abs(cur.get("short", 0) - sug["short"]),
                abs(cur.get("swing", 0) - sug["swing"]),
                abs(cur.get("value", 0) - sug["value"]),
            )
            mark = "需调整" if delta >= 0.20 else "基本一致"
        else:
            mark = "未配置"
        print(f"  {STATE_LABEL.get(state, state):<10}{cur_txt:<28}{sug_txt:<28}{mark}")

    print()
    print("  说明：")
    print(f"   1. 只采用样本 >= {MIN_SAMPLE} 且 T+1 平均收益为正的格子，其余按 0 处理。")
    print("   2. 权重与「相对超额收益」的平方成正比，让实测最好的档位获得最大配额。")
    print("   3. 样本不足的格子不参与决策 —— 例如情绪高潮期交易日极少，结论不可靠。")
    print("   4. 改动前请确认该结论在「观察期 / 验证期」都成立（见回测报告第六节）。")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
