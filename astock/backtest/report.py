# -*- coding: utf-8 -*-
"""回测报告渲染。

输出一份可直接判断「哪些策略真的赚钱、哪些该淘汰」的 Markdown 报告，
并在末尾给出自动生成的结论与调参建议。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from astock.config import get_config
from astock.logger import get_logger

logger = get_logger("backtest.report")

# 判定阈值：样本太少不下结论
MIN_SAMPLE = 30
GOOD_WIN_RATE = 50.0
GOOD_AVG_RET = 0.0


def conclusions(stats: dict[str, Any]) -> list[str]:
    """公开入口：供 CLI 在不生成完整报告时直接打印结论。"""
    return _conclusions(stats)


def _table(df: pd.DataFrame, rename: dict[str, str] | None = None) -> str:
    if df is None or df.empty:
        return "_无数据_"
    d = df.rename(columns=rename) if rename else df
    header = "| " + " | ".join(str(c) for c in d.columns) + " |"
    sep = "|" + "|".join("---" for _ in d.columns) + "|"
    rows = []
    for _, r in d.iterrows():
        cells = []
        for v in r.tolist():
            # NaN 必须显式处理，否则表格里会出现 "nan"（小样本组很容易出现）
            if isinstance(v, float):
                cells.append("-" if pd.isna(v) else f"{v:.2f}")
            elif v is None or (isinstance(v, str) and not v):
                cells.append("-")
            else:
                cells.append(str(v))
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join([header, sep] + rows)


def _fmt(v: Any, digits: int = 2, suffix: str = "") -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{digits}f}{suffix}"
    return f"{v}{suffix}"


def _conclusions(stats: dict[str, Any]) -> list[str]:
    """基于统计给出口径明确的结论。

    所有判定都必须先过样本量门槛，避免「4 条样本平均收益很高」被当成有效证据 ——
    这是回测报告最容易骗人的地方。
    """
    notes: list[str] = []
    overall = stats.get("overall") or {}
    n = overall.get("count", 0)

    if n < MIN_SAMPLE:
        notes.append(
            f"[!] 样本仅 {n} 条，远低于 {MIN_SAMPLE} 条的可用下限，以下结论仅供参考。"
            "建议先补全历史数据（回填更多股票/更长区间）再评估。"
        )
        return notes

    # ---- 收益分布形态：均值与中位数背离说明靠少数大赢家驱动 ----
    mean, median = overall.get("avg_ret1"), overall.get("median_ret1")
    if mean is not None and median is not None:
        if mean > 0 > median:
            notes.append(
                f"[!] 收益分布右偏：平均 {mean}% 但中位数 {median}% —— "
                "多数信号其实是亏的，正收益靠少数大赢家贡献。"
                "实盘需严格控制单笔仓位，否则容易连续回撤后错过那次大赚。"
            )
        elif median > 0:
            notes.append(f"[OK] 中位收益 {median}% 为正，赚钱不依赖个别极端样本。")

    # ---- 分策略判定（带样本量门槛）----
    by_strategy = stats.get("by_strategy")
    if isinstance(by_strategy, pd.DataFrame) and not by_strategy.empty:
        good, bad, weak = [], [], []
        for _, r in by_strategy.iterrows():
            if r["count"] < MIN_SAMPLE:
                weak.append(f"{r['strategy']}（{int(r['count'])} 条）")
                continue
            if r["avg_ret1"] is None:
                weak.append(f"{r['strategy']}（无有效收益数据）")
                continue
            if r["avg_ret1"] > GOOD_AVG_RET and (r["win_rate"] or 0) >= GOOD_WIN_RATE:
                good.append(f"**{r['strategy']}**（{int(r['count'])} 条，胜率 {r['win_rate']}%，平均 {r['avg_ret1']}%）")
            elif r["avg_ret1"] <= 0:
                bad.append(f"**{r['strategy']}**（{int(r['count'])} 条，平均 {r['avg_ret1']}%）")
            else:
                weak.append(f"{r['strategy']}（{int(r['count'])} 条，平均 {r['avg_ret1']}% 但胜率仅 {r['win_rate']}%）")

        if good:
            notes.append("[OK] 有效策略（样本达标且胜率>50%、收益为正）：" + "；".join(good))
        if bad:
            notes.append("[X] 无效策略（平均收益为负，建议排查条件或淘汰）：" + "；".join(bad))
        if weak:
            notes.append(
                f"[?] 不足以判定（样本 < {MIN_SAMPLE} 条，或收益为正但胜率偏低）：" + "；".join(weak)
            )

    # ---- 持有周期选择 ----
    r1, r3 = overall.get("avg_ret1"), overall.get("avg_ret3")
    if r1 is not None and r3 is not None:
        if r1 > 0 > r3:
            notes.append(
                f"[!] 次日冲高回落：T+1 平均 {r1}%，持有到 3 日转为 {r3}%。"
                "建议**以 T+1 了结**为主，不要延长持有（信号偏短线博弈）。"
            )
        elif r3 > r1 > 0:
            notes.append(
                f"[OK] 收益随持有期延长而增加（T+1 {r1}% → 3 日 {r3}%），"
                "信号偏向趋势启动，可适当延长持有。"
            )

    # ---- 多策略共振（必须先过滤样本量）----
    by_hit = stats.get("by_multi_hit")
    if isinstance(by_hit, pd.DataFrame) and len(by_hit) >= 2:
        valid = by_hit[by_hit["count"] >= MIN_SAMPLE]
        single = valid[valid["hit_bucket"] == "单策略"]
        multi = valid[valid["hit_bucket"] != "单策略"]
        if valid.empty:
            notes.append(
                f"[?] 多策略共振样本不足（各组均 < {MIN_SAMPLE} 条），无法判断共振是否有效。"
            )
        elif single.empty or multi.empty:
            thin = by_hit[by_hit["count"] < MIN_SAMPLE]["hit_bucket"].tolist()
            notes.append(
                f"[?] 共振组样本不足（{'、'.join(map(str, thin))}），暂不判定；"
                f"当前达标的只有「{'、'.join(valid['hit_bucket'].tolist())}」。"
            )
        else:
            s_ret = single.iloc[0]["avg_ret1"]
            best = multi.sort_values("avg_ret1", ascending=False).iloc[0]
            if best["avg_ret1"] and s_ret is not None and best["avg_ret1"] > s_ret + 0.3:
                notes.append(
                    f"[OK] 多策略共振有效：「{best['hit_bucket']}」平均 {best['avg_ret1']}%"
                    f" 优于单策略 {s_ret}%，可考虑对共振标的加权。"
                )
            else:
                notes.append(
                    f"[X] 多策略共振无增益：单策略平均 {s_ret}%，"
                    f"共振组最高仅 {best['avg_ret1']}%。"
                    "结论：共振只能作为「展示信息」，不可作为加分依据 —— "
                    "当前评分实现已符合该结论（hit_count 仅用于统计归因，未参与打分），无需改动。"
                )

    # ---- 观察期 vs 验证期 ----
    by_phase = stats.get("by_phase")
    if isinstance(by_phase, pd.DataFrame) and len(by_phase) == 2:
        observe = by_phase[by_phase["phase"] == "观察期"]
        validate = by_phase[by_phase["phase"] == "验证期"]
        if not observe.empty and not validate.empty:
            o = observe.iloc[0]["avg_ret1"]
            v = validate.iloc[0]["avg_ret1"]
            if o is not None and v is not None:
                if v <= 0 < o:
                    notes.append(
                        f"[!] 稳定性存疑：观察期平均 {o}%，验证期 {v}%（转负）。"
                        "当前参数可能只适应特定市场环境，不宜放大仓位。"
                    )
                elif o > 0 and v > 0:
                    if o and abs(v - o) / abs(o) > 0.5:
                        notes.append(
                            f"[!] 两段均为正但幅度差异较大（观察期 {o}% vs 验证期 {v}%），"
                            "衰减明显，建议继续观察或降低仓位。"
                        )
                    else:
                        notes.append(f"[OK] 稳定性良好：观察期 {o}% vs 验证期 {v}%，表现一致。")
                elif o <= 0 and v <= 0:
                    notes.append(
                        f"[X] 两段均为负（观察期 {o}%、验证期 {v}%），当前参数不应实盘使用。"
                    )

    return notes


def render(stats: dict[str, Any]) -> str:
    overall = stats.get("overall") or {}
    dates = stats.get("dates") or {}
    lines: list[str] = []

    lines.append("# 策略样本外验证报告")
    lines.append("")
    lines.append(f"- 运行编号：{stats.get('run_id')}")
    lines.append(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 回测区间：{dates.get('start')} ~ {dates.get('end')}（{dates.get('days')} 个交易日）")
    lines.append(f"- 信号总数：{overall.get('count')} 条　|　每档取前 {stats.get('top_n')} 只"
                 f"　|　档位：{'、'.join(stats.get('tiers') or [])}")
    lines.append(f"- 回放耗时：{stats.get('elapsed_sec', 0) / 60:.1f} 分钟")
    lines.append("")
    lines.append("> 收益口径：信号日**次日开盘买入**，`ret1` 为买入日收盘收益，"
                 "`ret3/5/10` 为持有对应交易日数收益，均为百分比。")
    lines.append("")

    # 策略异常会造成信号静默丢失，必须写在报告最前面
    errors = stats.get("strategy_errors") or {}
    if errors:
        lines.append("## ⚠️ 数据质量提示（影响结论可信度）")
        lines.append("")
        lines.append("回放期间以下策略发生异常，**当天信号全部丢失**，统计结果偏低于真实水平：")
        lines.append("")
        lines.append("| 策略 | 异常次数 | 最后错误 |")
        lines.append("|---|---|---|")
        for name, info in errors.items():
            lines.append(f"| `{name}` | {info.get('count')} | {str(info.get('last'))[:90]} |")
        lines.append("")
        lines.append("> 修复后需重跑回测，否则这些策略的胜率不可用。")
        lines.append("")

    lines.append("## 一、整体表现")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|---|---|")
    lines.append(f"| 信号数 | {overall.get('count')} |")
    lines.append(f"| T+1 胜率 | {_fmt(overall.get('win_rate'), 1, '%')} |")
    lines.append(f"| T+1 平均收益 | {_fmt(overall.get('avg_ret1'), 2, '%')} |")
    lines.append(f"| T+1 中位收益 | {_fmt(overall.get('median_ret1'), 2, '%')} |")
    lines.append(f"| 平均盈利 / 平均亏损 | {_fmt(overall.get('avg_win'), 2, '%')} / "
                 f"{_fmt(overall.get('avg_loss'), 2, '%')} |")
    lines.append(f"| 盈亏比 | {_fmt(overall.get('profit_loss_ratio'))} |")
    lines.append(f"| 持有 3 日平均 / 胜率 | {_fmt(overall.get('avg_ret3'), 2, '%')} / "
                 f"{_fmt(overall.get('win_rate_ret3'), 1, '%')} |")
    lines.append(f"| 持有 5 日平均 / 胜率 | {_fmt(overall.get('avg_ret5'), 2, '%')} / "
                 f"{_fmt(overall.get('win_rate_ret5'), 1, '%')} |")
    lines.append(f"| 持有 10 日平均 / 胜率 | {_fmt(overall.get('avg_ret10'), 2, '%')} / "
                 f"{_fmt(overall.get('win_rate_ret10'), 1, '%')} |")
    lines.append(f"| 10 日内平均最大涨幅 | {_fmt(overall.get('avg_max_gain'), 2, '%')} |")
    lines.append(f"| 10 日内平均最大回撤 | {_fmt(overall.get('avg_max_dd'), 2, '%')} |")
    lines.append("")

    rename = {
        "count": "信号数", "win_rate": "T+1胜率", "avg_ret1": "T+1平均",
        "avg_ret3": "3日平均", "avg_ret5": "5日平均", "avg_ret10": "10日平均",
        "profit_loss_ratio": "盈亏比", "avg_max_gain": "平均最大涨",
        "avg_max_dd": "平均最大撤", "tier": "档位", "strategy": "策略",
        "market_state": "市场状态", "phase": "阶段", "month": "月份",
        "hit_bucket": "共振情况",
    }

    lines.append("## 二、分档位表现")
    lines.append("")
    lines.append(_table(stats.get("by_tier"), rename))
    lines.append("")
    lines.append("## 三、分策略表现")
    lines.append("")
    lines.append(_table(stats.get("by_strategy"), rename))
    lines.append("")
    lines.append("## 四、分市场状态表现")
    lines.append("")
    lines.append(_table(stats.get("by_market_state"), rename))
    lines.append("")
    lines.append("## 五、多策略共振效果")
    lines.append("")
    lines.append("> 用于验证「多个策略同时命中是否更好」这一直觉。"
                 "注意：当前评分实现中 `hit_count` 只用于统计归因，**不参与打分**，"
                 "因此本节结论的用途是判断「是否值得引入共振加分」，而非校验已存在的加分。")
    lines.append("")
    lines.append(_table(stats.get("by_multi_hit"), rename))
    lines.append("")
    lines.append("## 六、稳定性：观察期 vs 验证期")
    lines.append("")
    lines.append(_table(stats.get("by_phase"), rename))
    lines.append("")
    lines.append("## 七、稳定性：按月表现")
    lines.append("")
    lines.append(_table(stats.get("by_month"), rename))
    lines.append("")

    notes = _conclusions(stats)
    lines.append("## 八、结论与建议")
    lines.append("")
    if notes:
        for note in notes:
            lines.append(f"- {note}")
    else:
        lines.append("- 样本不足，暂无结论。")
    lines.append("")
    lines.append("> 调参原则：先看**分策略**的平均收益与盈亏比，再看稳定性（观察期/验证期）。")
    lines.append("> 只有当某条参数在所有子区间都为正时，才值得调整 `config/settings.yaml`。")
    return "\n".join(lines)


def save(stats: dict[str, Any]) -> dict[str, str]:
    """保存 Markdown 报告与明细 CSV。"""
    cfg = get_config()
    out_dir = cfg.data_dir / "backtest"
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = stats.get("run_id", datetime.now().strftime("%Y%m%d_%H%M%S"))

    md_path = out_dir / f"backtest_{run_id}.md"
    md_path.write_text(render(stats), encoding="utf-8")

    paths = {"markdown": str(md_path)}
    detail: pd.DataFrame = stats.get("detail")
    if detail is not None and not detail.empty:
        csv_path = out_dir / f"backtest_{run_id}.csv"
        detail.to_csv(csv_path, index=False, encoding="utf-8-sig")
        paths["csv"] = str(csv_path)
    logger.info("回测报告已保存：%s", md_path)
    return paths
