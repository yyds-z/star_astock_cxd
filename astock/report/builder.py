# -*- coding: utf-8 -*-
"""报告生成（模块 6）。

输出三种形态：
1. Markdown 报告文件：便于阅读与归档；
2. JSON 结构化结果：供 FastAPI 与前端消费；
3. 控制台摘要：命令行直接查看。

LLM 不可用或超预算时自动降级为本地模板，保证每天的报告结构始终完整。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pandas as pd

from astock.config import get_config
from astock.llm.client import LLMClient
from astock.llm.prompts import (
    RECAP_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_market_prompt,
    build_recap_prompt,
    pick_key,
    template_logic,
    template_market_view,
    template_recap_summary,
    template_risk,
)
from astock.logger import get_logger
from astock.recommend.engine import RecommendEngine
from astock.storage.db import Storage, get_storage

logger = get_logger("report.builder")

TIER_ORDER = ["short", "swing", "value"]


def _format_fundamentals(fund: dict[str, Any] | None) -> str:
    """把一行财务指标格式化成报告里的一行文本。

    注意只展示**比率型**指标（ROE / 资产负债率 / 同比）：
    A 股季报是累计口径，Q2 是半年、Q4 是全年，金额类指标跨期不可比，
    直接列表会把「半年赚了 44 亿」和「全年赚了 82 亿」并列成误导。
    """
    if not fund:
        return ""

    def val(key: str, digits: int = 1) -> str | None:
        v = fund.get(key)
        if v is None:
            return None
        try:
            if pd.isna(v):
                return None
        except (TypeError, ValueError):
            return None
        return f"{float(v):.{digits}f}"

    parts: list[str] = []
    roe = val("roe")
    if roe is not None:
        parts.append(f"ROE {roe}%")
    debt = val("debt_ratio")
    if debt is not None:
        parts.append(f"资产负债率 {debt}%")
    rev = val("revenue_yoy")
    if rev is not None:
        parts.append(f"营收同比 {float(rev):+.1f}%")
    profit = val("profit_yoy")
    if profit is not None:
        parts.append(f"净利同比 {float(profit):+.1f}%")
    if not parts:
        return ""
    period = _text(fund.get("period_end"))[:10]
    return f"{' ｜ '.join(parts)}（{period}）"


def _pct(value: Any, digits: int = 2) -> str:
    """百分比格式化：None/NaN 一律显示 '-'。

    报告里出现 `nan%` 会让人以为数据坏了，而缺失在回顾板块是常态
    （如 hold3 需要第 3 个交易日、旧记录没有对应字段）。
    """
    try:
        if value is None or pd.isna(value):
            return "-"
        return f"{float(value):+.{digits}f}%"
    except (TypeError, ValueError):
        return "-"


def _num(value: Any, digits: int = 2) -> str:
    """数值格式化（价格等），同样把缺失显示成 '-'。"""
    try:
        if value is None or pd.isna(value):
            return "-"
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def _text(value: Any, default: str = "") -> str:
    """把可能为 NaN/None 的列值安全转成字符串。

    报告里出现 'nan' 字样会让人以为数据坏了；行业映射本身也只覆盖 99.4%，
    未映射的股票必须有干净的占位符。
    """
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        return default
    text = str(value).strip()
    return default if text in ("", "nan", "None", "NaT") else text


class ReportBuilder:
    """盘前报告生成器。"""

    def __init__(self, storage: Storage | None = None, llm: LLMClient | None = None) -> None:
        self.cfg = get_config()
        self.storage = storage or get_storage()
        self.llm = llm or LLMClient(self.cfg, self.storage)

    def _load_fundamentals(self, data_date: str, codes: list[str]) -> dict[str, dict[str, Any]]:
        """取各候选股最新一期财务指标（按**披露日**过滤）。

        必须用 `report_date <= 决策日`，不能用 `period_end`：
        报告期末只是会计区间终点，财报实际公开要晚 1~4 个月。
        按 period_end 取数等于用了当时还不存在的财报 —— 这是价值类策略
        最常见的前视偏差，会让历史业绩虚高到无法复现。

        任何异常都只记日志并返回空：财务是增强项，不应阻断报告生成。
        """
        if not codes:
            return {}
        placeholders = ", ".join("?" for _ in codes)
        try:
            df = self.storage.query_df(
                f"""
                WITH ranked AS (
                    SELECT code, roe, debt_ratio, revenue_yoy, profit_yoy,
                           period_end, report_date,
                           ROW_NUMBER() OVER (
                               PARTITION BY code
                               -- 必须加 period_end 作次级排序：上游存在
                               -- 披露日错填的情况（实测 2025 中报的 report_date
                               -- 被写成 2026-08-15，与 2026 中报同值），
                               -- 此时只按 report_date 排序会退化成「任意取一条」，
                               -- 可能取到一年前的旧数据。
                               ORDER BY report_date DESC, period_end DESC
                           ) AS rn
                    FROM dws_finance_metrics
                    WHERE report_date IS NOT NULL AND report_date <= ?
                      AND code IN ({placeholders})
                )
                SELECT code, roe, debt_ratio, revenue_yoy, profit_yoy, period_end
                FROM ranked WHERE rn = 1
                """,
                [data_date, *codes],
            )
        except Exception as exc:  # noqa: BLE001 - 表不存在等情况一律降级
            logger.warning("财务指标读取失败（本次报告不含基本面）：%s", str(exc)[:120])
            return {}
        if df.empty:
            return {}
        return {str(r["code"]): r.to_dict() for _, r in df.iterrows()}

    # ---------------- 主流程 ----------------
    def build(self, result: dict[str, Any], use_llm: bool | None = None) -> dict[str, Any]:
        """生成报告。返回 {'markdown_path', 'json_path', 'summary', 'ai'}"""
        data_date = str(result.get("data_date"))
        recs: pd.DataFrame = result.get("recommendations")
        market = result.get("market") or {}

        ai = self._ai_section(market, result.get("strategy_stats") or {}, recs, use_llm)

        # 回顾板块（报告的第一板块）。数据来自 `ads_review`，
        # 因此 daily 流程里「复盘」必须排在「生成报告」之前，
        # 否则这里读到的还是 pending、整个板块会空掉。
        recap = self._load_recap()
        recap_ai = self._review_section(recap, use_llm)

        md = self._render_markdown(result, market, recs, ai, recap, recap_ai)
        report_dir = RecommendEngine.report_dir()
        md_path = report_dir / f"report_{data_date}.md"
        json_path = report_dir / f"report_{data_date}.json"
        md_path.write_text(md, encoding="utf-8")

        payload = {
            "data_date": data_date,
            "plan_date": result.get("plan_date"),
            "generated_at": result.get("generated_at"),
            "market": market,
            "strategy_stats": result.get("strategy_stats"),
            "pool_size": result.get("pool_size"),
            "params_version": result.get("params_version"),
            "ai": ai,
            "review": {
                "stats": {k: v for k, v in recap.items() if k != "rows"},
                "rows": recap.get("rows") or [],
                "comment": recap_ai,
            },
            "recommendations": []
            if recs is None or recs.empty
            else json.loads(recs.to_json(orient="records", force_ascii=False)),
        }
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        logger.info("报告已生成：%s", md_path)
        return {
            "markdown_path": str(md_path),
            "json_path": str(json_path),
            "markdown": md,
            "market_view": ai.get("market_view"),
            "ai_used": ai.get("used_llm", False),
        }

    # ---------------- AI 段 ----------------
    def _ai_section(
        self,
        market: dict,
        stats: dict,
        recs: pd.DataFrame | None,
        use_llm: bool | None,
    ) -> dict[str, Any]:
        picks = [] if recs is None or recs.empty else json.loads(
            recs.to_json(orient="records", force_ascii=False)
        )

        # 本地模板兜底（永远可用）
        fallback = {
            "market_view": template_market_view(market),
            "picks": [
                {
                    "key": pick_key(p),
                    "code": p.get("code"),
                    "tier": p.get("tier"),
                    "name": p.get("name"),
                    "logic": template_logic(p),
                    "risk": template_risk(p),
                }
                for p in picks
            ],
            "ops": "",
        }

        want_llm = self.llm.enabled if use_llm is None else bool(use_llm)
        if not want_llm:
            return {**fallback, "used_llm": False}

        data = self.llm.chat_json(SYSTEM_PROMPT, build_market_prompt(market, stats, picks))
        if not data:
            return {**fallback, "used_llm": False}

        # LLM 结果与模板合并：按 (档位, 代码) 对齐，LLM 缺失的字段用模板补齐
        merged_picks = []
        llm_picks = {str(p.get("id") or p.get("key")): p for p in (data.get("picks") or [])}
        for p in fallback["picks"]:
            lp = llm_picks.get(p["key"], {})
            merged_picks.append(
                {
                    "key": p["key"],
                    "code": p["code"],
                    "tier": p["tier"],
                    "name": p["name"],
                    "logic": (lp.get("logic") or p["logic"])[:60],
                    "risk": (lp.get("risk") or p["risk"])[:60],
                }
            )

        return {
            "market_view": (data.get("market_view") or fallback["market_view"])[:120],
            "picks": merged_picks,
            "ops": (data.get("ops") or "")[:120],
            "used_llm": True,
        }

    # ---------------- 回顾板块 ----------------
    def _load_recap(self) -> dict:
        """取上一批已结算推荐的结果。失败一律降级为「无回顾」，不影响今日报告。"""
        try:
            from astock.review.recap import build as build_recap

            return build_recap(self.storage)
        except Exception as exc:  # noqa: BLE001
            logger.warning("昨日回顾生成失败（本次报告不含回顾板块）：%s", str(exc)[:120])
            return {"available": False, "reason": f"回顾生成失败：{str(exc)[:60]}"}

    def _review_section(self, recap: dict, use_llm: bool | None) -> dict[str, Any]:
        """回顾板块的文字总结：LLM 优先，未启用或失败则退回本地模板。"""
        fallback = template_recap_summary(recap)
        want_llm = self.llm.enabled if use_llm is None else bool(use_llm)
        if not recap.get("available") or not want_llm:
            return {**fallback, "used_llm": False}

        data = self.llm.chat_json(RECAP_SYSTEM_PROMPT, build_recap_prompt(recap))
        if not data:
            return {**fallback, "used_llm": False}

        out: dict[str, Any] = {}
        for key in ("verdict", "wins", "losses", "lesson", "adjust"):
            value = data.get(key)
            text = str(value).strip() if value else ""
            out[key] = text[:80] or fallback.get(key, "")
        return {**out, "used_llm": True}

    @staticmethod
    def _render_review(recap: dict, recap_ai: dict) -> list[str]:
        """把回顾板块渲染成 markdown 行。"""
        lines: list[str] = ["## 一、昨日推荐回顾", ""]
        if not recap.get("available"):
            lines.append(f"（{recap.get('reason') or '暂无数据'}）")
            lines.append("")
            return lines

        score = recap.get("effect_score") or {}
        tier_label = {"short": "短线", "swing": "波段", "value": "价值"}
        result_label = {"win": "盈", "loss": "亏", "flat": "平", "pending": "待"}

        lines.append(
            f"推荐日 **{recap.get('rec_date')}** → 表现日 **{recap.get('review_date')}**"
            f"　共 {recap.get('count')} 只"
        )
        lines.append("")
        lines.append(
            f"- **选股效果分：{_num(score.get('score'), 1)} / 100**"
            f"（胜率分 {_num(score.get('win_part'), 1)} × 0.4 + "
            f"超额分 {_num(score.get('excess_part'), 1)} × 0.6）"
        )
        lines.append(
            f"- 命中：{recap.get('wins')} 涨 / {recap.get('losses')} 跌 / "
            f"{recap.get('flats')} 平，胜率 **{recap.get('win_rate')}%**"
        )
        lines.append(
            f"- 收益：**买入后平均 {_pct(recap.get('avg_return'))}**"
            f"（当日涨跌平均 {_pct(recap.get('avg_day_chg'))}）"
        )
        if recap.get("benchmark_return") is not None:
            lines.append(
                f"- 基准：{recap.get('benchmark_code')} 当日 "
                f"{_pct(recap.get('benchmark_return'))} → "
                f"**超额 {_pct(recap.get('excess'))}**"
            )
        lines.append(
            f"- 波动：平均最大涨幅 {_pct(recap.get('avg_max_gain'))}，"
            f"平均最大回撤 {_pct(recap.get('avg_max_dd'))}"
        )
        if recap.get("avg_hold3") is not None:
            lines.append(f"- 持有 3 日：{_pct(recap.get('avg_hold3'))}")
        lines.append("")

        by_tier = recap.get("by_tier") or {}
        if by_tier:
            lines.append("### 分档表现")
            lines.append("")
            lines.append("| 档位 | 数量 | 胜率 | 平均买入后收益 |")
            lines.append("|---|---|---|---|")
            for t in TIER_ORDER:
                s = by_tier.get(t)
                if s:
                    lines.append(
                        f"| {tier_label.get(t, t)} | {s['count']} | {s['win_rate']}% "
                        f"| {_pct(s['avg_return'])} |"
                    )
            lines.append("")

        rows = recap.get("rows") or []
        if rows:
            lines.append("### 个股表现（由涨到跌）")
            lines.append("")
            lines.append(
                "| # | 代码 | 名称 | 档位 | 推荐分 | 买入价 | 收盘 "
                "| 买入后收益 | 当日涨跌 | 最大涨幅 | 最大回撤 | 结果 |"
            )
            lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
            for i, r in enumerate(rows, 1):
                lines.append(
                    f"| {i} | {r.get('code')} | {r.get('name')} "
                    f"| {tier_label.get(r.get('tier'), r.get('tier'))} "
                    f"| {_num(r.get('final_score'), 1)} | {_num(r.get('buy_price'))} "
                    f"| {_num(r.get('close'))} | **{_pct(r.get('ret'))}** "
                    f"| {_pct(r.get('day_chg'))} | {_pct(r.get('max_gain'))} "
                    f"| {_pct(r.get('max_dd'))} "
                    f"| {result_label.get(r.get('result'), r.get('result'))} |"
                )
            lines.append("")
            lines.append("> 「买入后收益」= 买入日收盘/开盘 − 1（系统按 T+1 开盘买入，这是到手收益）；")
            lines.append("> 「当日涨跌」= 买入日收盘/前收 − 1（与行情软件一致，超额收益按此口径比较）。")
            lines.append("")

            lines.append("### 逐只归因（涨的为什么涨、跌的为什么跌）")
            lines.append("")
            for r in rows:
                head = (
                    f"- **{_pct(r.get('ret'))} {r.get('code')} {r.get('name')}"
                    f"（{tier_label.get(r.get('tier'), r.get('tier'))}）**："
                )
                if r.get("reason"):
                    tail = str(r["reason"])
                    if r.get("lesson"):
                        tail += f" ｜ 教训：{r['lesson']}"
                    if r.get("category"):
                        tail += f"（{r['category']}）"
                else:
                    tail = "尚无归因（运行 `python -m astock.cli attribution` 可生成）"
                lines.append(head + tail)
            lines.append("")

        lines.append("### 经验教训")
        lines.append("")
        lines.append(f"- 整体判断：{recap_ai.get('verdict') or '-'}")
        lines.append(f"- 上涨共性：{recap_ai.get('wins') or '-'}")
        lines.append(f"- 下跌共性：{recap_ai.get('losses') or '-'}")
        lines.append(f"- 经验：{recap_ai.get('lesson') or '-'}")
        lines.append(f"- 调整：{recap_ai.get('adjust') or '-'}")
        lines.append("")
        if not recap_ai.get("used_llm"):
            lines.append("> 本节由本地模板生成（未启用 LLM），只陈述结果、不做归因。")
            lines.append("")
        return lines

    # ---------------- Markdown ----------------
    def _render_markdown(
        self,
        result: dict,
        market: dict,
        recs: pd.DataFrame | None,
        ai: dict,
        recap: dict | None = None,
        recap_ai: dict | None = None,
    ) -> str:
        lines: list[str] = []
        data_date = result.get("data_date")
        lines.append(f"# A股 AI 选股报告 · 数据日 {data_date}")
        lines.append("")
        lines.append(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"- 计划交易日：{result.get('plan_date')}")
        lines.append(f"- 参数版本：{result.get('params_version')}")
        lines.append(f"- 股票池：{result.get('pool_size')} 只")
        lines.append(f"- AI 解读：{'LLM' if ai.get('used_llm') else '本地模板（未启用或超预算）'}")
        lines.append("")

        # ---- 第一板块：昨日推荐回顾（先看上次选得怎么样，再看今天选什么）----
        lines.extend(self._render_review(recap or {}, recap_ai or {}))

        # ---- 第二板块：影子信号（v1.0 唯一决策依据；在 recs 空判断之前，
        #      保证主链路无候选时影子板块仍然出现）----
        lines.extend(self._render_shadow(result))

        # ---- 第三板块：主链路候选（已降级为信号观察池）----
        lines.append("## 三、主链路候选（信号观察池）")
        lines.append("")
        lines.append("### 市场环境")
        lines.append("")
        lines.append(f"- 市场状态：**{market.get('label')}**（置信度 {market.get('confidence')}）")
        lines.append(f"- 市场宽度：MA20 上方 {market.get('breadth_ma20')}% / MA60 上方 {market.get('breadth_ma60')}%")
        lines.append(f"- 涨跌停：涨停 {market.get('limit_up')} 家 / 跌停 {market.get('limit_down')} 家，炸板率 {market.get('broken_rate')}%")
        w = market.get("weights") or {}
        lines.append(f"- 档位权重：短线 {w.get('short')} / 波段 {w.get('swing')} / 价值 {w.get('value')}")
        if market.get("top_sector"):
            share = market.get("top_sector_share")
            extra = f"，占全市场涨停 {share}%" if share is not None else ""
            lines.append(f"- 最强板块：**{market.get('top_sector')}**{extra}")
        lines.append("")
        lines.append(f"> {ai.get('market_view') or '-'}")
        lines.append("")

        if recs is None or recs.empty:
            lines.append("今日无符合条件的候选股票。")
            return "\n".join(lines)

        # 基本面（价值档的核心输入）。只对候选股查询，且用**披露日**过滤。
        data_date = str(result.get("data_date"))
        fundamentals = self._load_fundamentals(data_date, [str(c) for c in recs["code"]])

        for tier in TIER_ORDER:
            sub = recs[recs["tier"] == tier].sort_values("final_score", ascending=False)
            if sub.empty:
                continue
            label = str(sub.iloc[0]["tier_label"])
            lines.append(f"### {label}档（{len(sub)} 只）")
            lines.append("")
            lines.append("| 排名 | 代码 | 名称 | 板块 | 策略 | 综合分 | 收盘 | 当日涨跌 | 换手 | 量比 |")
            lines.append("|---|---|---|---|---|---|---|---|---|---|")
            for _, r in sub.iterrows():
                # 板块：优先显示「行业名（板块强度）」，未映射的显示 '-'
                ind = _text(r.get("industry"), "-")
                strength = r.get("sector_strength")
                sector_txt = ind
                if ind != "-" and strength is not None and not pd.isna(strength):
                    sector_txt = f"{ind}({float(strength):.0f})"
                lines.append(
                    f"| {int(r['tier_rank'])} | {r['code']} | {r['name']} | {sector_txt} "
                    f"| {r['strategy_label']} "
                    f"| **{r['final_score']:.1f}** | {r['close']:.2f} | {r['pct_chg']:.2f}% "
                    f"| {r['turn']:.2f}% | {r['vol_ratio']:.2f} |"
                )
            lines.append("")

            # 必须按 (档位, 代码) 取 AI 解读：同一只股票可能同时出现在多个档位
            ai_picks = {str(p.get("key")): p for p in (ai.get("picks") or [])}
            for _, r in sub.iterrows():
                code = str(r["code"])
                lines.append(f"#### {r['tier_rank']}. {code} {r['name']}（综合分 {r['final_score']:.1f}）")
                lines.append("")
                lines.append(f"- 命中策略：{r['strategy_label']}")
                # 涨停题材归因（近期进过涨停池的股票才有）
                reason = _text(r.get("limit_reason"))
                if reason:
                    boards = r.get("limit_boards")
                    board_txt = _text(r.get("limit_board_text")) or (
                        f"{int(boards)}连板" if boards else ""
                    )
                    seal = r.get("limit_seal_money")
                    seal_txt = f"，封单 {float(seal) / 1e8:.2f} 亿" if seal else ""
                    pool_date = _text(r.get("pool_date"))[:10]
                    lines.append(
                        f"- 涨停题材：{reason}（{board_txt}{seal_txt}，{pool_date}）"
                    )
                ind = _text(r.get("industry"))
                if ind:
                    strength = r.get("sector_strength")
                    if strength is not None and not pd.isna(strength):
                        lines.append(f"- 所属板块：{ind}（当日强度 {float(strength):.1f}/100）")
                    else:
                        lines.append(f"- 所属板块：{ind}")
                fund_txt = _format_fundamentals(fundamentals.get(code))
                if fund_txt:
                    lines.append(f"- 基本面：{fund_txt}")
                ap = ai_picks.get(f"{r['tier']}:{code}", {})
                lines.append(f"- 推荐逻辑：{ap.get('logic') or template_logic(r.to_dict())}")
                try:
                    detail = json.loads(r["score_detail"])
                    lines.append(
                        "- 评分拆解："
                        + "，".join(f"{k} {v}" for k, v in detail.items() if k != "权重")
                    )
                except Exception:  # noqa: BLE001
                    pass
                lines.append(f"- 风险提示：{ap.get('risk') or template_risk(r.to_dict())}")
                lines.append("")

        if ai.get("ops"):
            lines.append("## 四、操作提示")
            lines.append("")
            lines.append(f"> {ai['ops']}")
            lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("## 策略命中统计")
        lines.append("")
        for name, cnt in (result.get("strategy_stats") or {}).items():
            lines.append(f"- {name}: {cnt} 只")
        lines.append("")
        lines.append("> 本报告由系统自动生成，仅供研究参考，不构成投资建议。")
        return "\n".join(lines)

    # ---------------- 影子信号板块 ----------------
    def _render_shadow(self, result: dict) -> list[str]:
        """影子信号板块：v1.0 契约下的**唯一决策依据**，固定出现在每日报告。

        为什么排在主链路候选之前：可实现口径回测（2026-09-24 P0）证明主链路
        超额 t=0.09（无 alpha），影子信号 t=5.62（通过样本外）——报告的呈现
        顺序应反映决策权重，而不是代码演进历史。

        自动关联盘中快照：若计划交易日已有快照（如 14:00 采集任务），
        每只候选会标注**此刻实况与可执行性**——这是「14:00 决策」的落地形式；
        18:30 生成报告时快照通常尚不存在，则只呈现信号本身。
        """
        lines: list[str] = [
            "## 二、影子信号候选（v1.0 决策依据）",
            "",
            "> 逻辑：涨停基因（近 28 日有涨停、距上次 ≤10 天）+ 缩量（量 < 前 5 日均量 70%）"
            "+ 不破位（收 ≥ 前 5 日均价 98%）。检验结论：次日涨停率 9.2%（全市场 2.05%）、"
            "可实现超额 +0.667%/日（t=5.62，观察期选参→验证期 t=3.56）。",
            "> 执行规则：**等权分散**（收益来自约 9% 命中涨停的尾部，靠分散变正期望）、"
            "市价买入不追板、持有 D+1/D+3 收盘。",
            "",
        ]
        data_date = str(result.get("data_date"))
        plan_date = str(result.get("plan_date") or "")
        try:
            picks = self.storage.query_df(
                "SELECT code, name, close AS sig_close, zt20, days_since_zt, "
                "vol_ratio, signal_score FROM ads_shadow_pick "
                "WHERE date = ? ORDER BY signal_score DESC",
                [data_date],
            )
        except Exception:  # noqa: BLE001 - 表不存在等，报告不因影子失败而中断
            picks = pd.DataFrame()
        if picks.empty:
            lines.append(f"数据日 {data_date} 无影子候选（条件未命中或数据不足）。")
            lines.append("")
            return lines

        # 盘中实况（计划交易日有快照才关联；无则仅呈现信号）
        snap = pd.DataFrame()
        if plan_date and plan_date != "None":
            try:
                snap = self.storage.query_df(
                    "SELECT code, price, pct_chg, volume_ratio "
                    "FROM dwd_intraday_snapshot "
                    "WHERE date = ? AND slot = "
                    "  (SELECT MAX(slot) FROM dwd_intraday_snapshot WHERE date = ?)",
                    [plan_date, plan_date],
                )
            except Exception:  # noqa: BLE001
                snap = pd.DataFrame()
        if not snap.empty:
            picks = picks.merge(snap, on="code", how="left")

        def _cat(r) -> str:
            if pd.isna(r.get("price")):
                return "无快照"
            cap = 19.5 if str(r["code"]).startswith(("30", "68")) else 9.7
            p = float(r["pct_chg"]) if pd.notna(r.get("pct_chg")) else 0.0
            if p >= cap:
                return "已涨停·放弃"
            if p >= 7.0:
                return "涨幅过大·回避"
            return "可买"

        if not snap.empty:
            picks["状态"] = picks.apply(_cat, axis=1)
            order = {"可买": 0, "涨幅过大·回避": 1, "已涨停·放弃": 2, "无快照": 3}
            picks = picks.sort_values(
                by=["状态", "signal_score"], key=lambda s: s.map(order) if s.name == "状态" else -s
            ).reset_index(drop=True)
            n_buy = int((picks["状态"] == "可买").sum())
            n_zt = int(picks["状态"].str.startswith("已涨停").sum())
            lines.append(
                f"信号日 {data_date} 共 **{len(picks)}** 只候选（计划 {plan_date} 买入）。"
                f"按计划日快照核对：可买 **{n_buy}** 只、已涨停 {n_zt} 只（放弃）。"
            )
            lines.append("")
            lines.append("| 代码 | 名称 | 信号分 | 信号日收盘 | 快照现价 | 快照涨幅 | 快照量比 | 信号量比 | 连板 | 距涨停(日) | 状态 |")
            lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
            for _, r in picks.iterrows():
                price = "-" if pd.isna(r.get("price")) else f"{float(r['price']):.2f}"
                pct = "-" if pd.isna(r.get("pct_chg")) else f"{float(r['pct_chg']):+.2f}%"
                vr_now = "-" if pd.isna(r.get("volume_ratio")) else f"{float(r['volume_ratio']):.2f}"
                vr_sig = "-" if pd.isna(r.get("vol_ratio")) else f"{float(r['vol_ratio']):.2f}"
                lines.append(
                    f"| {r['code']} | {r['name']} | {r['signal_score']:.1f} "
                    f"| {float(r['sig_close']):.2f} | {price} | {pct} | {vr_now} | {vr_sig} "
                    f"| {int(r['zt20'])} | {int(r['days_since_zt'])} | {r['状态']} |"
                )
        else:
            lines.append(f"信号日 {data_date} 共 **{len(picks)}** 只候选（计划 {plan_date} 买入）。")
            lines.append("")
            lines.append("| 代码 | 名称 | 信号分 | 信号日收盘 | 信号量比 | 连板 | 距涨停(日) |")
            lines.append("|---|---|---|---|---|---|---|")
            for _, r in picks.iterrows():
                lines.append(
                    f"| {r['code']} | {r['name']} | {r['signal_score']:.1f} "
                    f"| {float(r['sig_close']):.2f} | {float(r['vol_ratio']):.2f} "
                    f"| {int(r['zt20'])} | {int(r['days_since_zt'])} |"
                )
        lines.append("")
        return lines

    # ---------------- 控制台摘要 ----------------
    @staticmethod
    def console_summary(result: dict, ai: dict) -> str:
        market = result.get("market") or {}
        recs: pd.DataFrame = result.get("recommendations")
        lines = [
            "",
            "=" * 68,
            f"  A股AI选股 · 数据日 {result.get('data_date')} → 计划交易日 {result.get('plan_date')}",
            "=" * 68,
            f"  市场状态：{market.get('label')}（置信度 {market.get('confidence')}）",
            f"  市场宽度：MA20 {market.get('breadth_ma20')}% / MA60 {market.get('breadth_ma60')}%"
            f" | 涨停 {market.get('limit_up')} 家 | 炸板率 {market.get('broken_rate')}%",
            f"  档位权重：短线 {market.get('weights', {}).get('short')}"
            f" / 波段 {market.get('weights', {}).get('swing')}"
            f" / 价值 {market.get('weights', {}).get('value')}",
            "-" * 68,
            f"  {ai.get('market_view') or ''}",
            "-" * 68,
        ]
        if recs is None or recs.empty:
            lines.append("  今日无候选股票")
        else:
            for tier in TIER_ORDER:
                sub = recs[recs["tier"] == tier].sort_values("tier_rank")
                if sub.empty:
                    continue
                lines.append(f"  【{sub.iloc[0]['tier_label']}档】")
                for _, r in sub.iterrows():
                    lines.append(
                        f"    {int(r['tier_rank'])}. {r['code']} {r['name'][:6]:<6} "
                        f"分数 {r['final_score']:>5.1f} | {r['strategy_label']}"
                    )
        lines.append("=" * 68)
        return "\n".join(lines)
