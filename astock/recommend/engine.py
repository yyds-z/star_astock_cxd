# -*- coding: utf-8 -*-
"""候选生成流水线（模块 4 + 5 + 6 的编排）。

流程：数据补齐 → 因子更新 → 市场状态 → 股票池过滤 → 策略筛选 → 评分融合 → 落库
相比原方案文档的串行设计，这里把「市场状态」前置，用状态权重直接调制策略输出，
实现「先判断环境、再选择策略」的核心诉求。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from astock.config import get_config
from astock.data.collector import Collector
from astock.data.universe import Universe
from astock.features.builder import FeatureBuilder
from astock.features.sector_strength import SectorStrengthBuilder
from astock.features.indicators import to_float, to_list, to_str
from astock.logger import get_logger
from astock.market.regime import MarketRegime
from astock.scoring.scorer import Scorer
from astock.storage.db import Storage, get_storage
from astock.strategy import build_strategies, StrategyContext

logger = get_logger("recommend.engine")

PARAM_KEYS = ["strategy", "scoring", "universe", "market_regime"]


def params_version() -> str:
    """根据策略相关配置生成版本指纹，用于追踪「哪套参数产生了哪条推荐」。"""
    cfg = get_config()
    payload = {k: cfg.get(k, {}) for k in PARAM_KEYS}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:10]


class RecommendEngine:
    """推荐流水线引擎。"""

    def __init__(self, storage: Storage | None = None) -> None:
        self.cfg = get_config()
        self.storage = storage or get_storage()
        self.collector = Collector(self.storage)
        self.universe = Universe(self.storage)
        self.builder = FeatureBuilder(self.storage)
        self.regime = MarketRegime(self.storage)
        self.scorer = Scorer(self.cfg, self.storage)

    # ---------------- 主流程 ----------------
    def run(
        self,
        trade_date: date | None = None,
        refresh_data: bool = True,
        recompute_factor: bool = True,
        persist: bool = True,
        workers: int = 4,
    ) -> dict[str, Any]:
        """执行一次完整的推荐流程。"""
        started = datetime.now()
        data_date = trade_date

        # 1) 数据补齐（幂等，可重复执行）
        if refresh_data:
            stats = self.collector.ensure_fresh(workers=workers)
            logger.info("数据补齐完成：%s", stats)

        # 2) 因子表更新
        if recompute_factor:
            self.builder.build()

        # 2.5) 板块强度（市场状态的 event 判定与报告的行业展示都依赖它）。
        # 任何失败都不阻塞主流程：它是增强项，缺了只是 top_sector 留空。
        if recompute_factor:
            try:
                built = SectorStrengthBuilder(self.storage).build()
                if built:
                    logger.info("板块强度已更新 %d 行", built)
            except Exception as exc:  # noqa: BLE001
                logger.warning("板块强度计算失败（不阻塞主流程）：%s", str(exc)[:120])

        if data_date is None:
            data_date = self.builder.latest_date()
        if data_date is None:
            raise RuntimeError("无可用数据，请先执行 backfill")

        # 3) 市场状态
        self.regime.compute()
        regime_row = self.regime.get(data_date) or self.regime.latest() or {}
        weights = {
            "short": to_float(regime_row.get("w_short"), 0.3),
            "swing": to_float(regime_row.get("w_swing"), 0.4),
            "value": to_float(regime_row.get("w_value"), 0.3),
        }
        logger.info(
            "市场状态：%s（置信度 %.2f）| 权重 短线%.2f 波段%.2f 价值%.2f",
            regime_row.get("state_label") or "未知",
            to_float(regime_row.get("confidence")),
            weights["short"], weights["swing"], weights["value"],
        )

        # 4) 股票池
        pool = self.universe.filtered_codes(data_date)
        if pool.empty:
            raise RuntimeError("股票池为空，请检查数据是否已回填")

        # 5) 因子截面 + 策略
        features = self.builder.load_features(data_date)
        ctx = StrategyContext(
            trade_date=data_date, features=features, universe=pool, cfg=self.cfg
        )

        strategies = build_strategies(self.cfg)
        raw_candidates: list[dict[str, Any]] = []
        strategy_stats: dict[str, int] = {}
        stat_rows: list[dict[str, Any]] = []
        for tier, instances in strategies.items():
            for strategy in instances:
                hits = strategy.screen(ctx)
                strategy_stats[strategy.name] = len(hits)
                stat_rows.append(
                    {
                        "data_date": data_date,
                        "tier": tier,
                        "strategy": strategy.name,
                        "label": strategy.label,
                        "hits": len(hits),
                        "updated_at": datetime.now(),
                    }
                )
                raw_candidates.extend(h.as_dict() for h in hits)

        if persist and stat_rows:
            self.storage.upsert_df(pd.DataFrame(stat_rows), "sys_strategy_stats")

        cand_df = pd.DataFrame(raw_candidates)
        logger.info("策略命中合计 %d 条（去重前）", len(cand_df))

        # 5.5) 入场成本过滤：只针对**当日涨停**的候选（它们必然高开、成本高）。
        # 非涨停候选实测是系统性**低开**（折价 -1.20%），对它们做成本惩罚是错的。
        # 详见 astock/features/entry_cost.py 的模块注释。
        from astock.features.entry_cost import apply_entry_cost_filter

        cand_df, dropped = apply_entry_cost_filter(
            cand_df, self.storage, data_date, self.cfg
        )
        if dropped is not None and not dropped.empty:
            logger.info("入场成本过滤：剔除 %d 只当日涨停候选", len(dropped))

        # 6) 评分
        scored = self.scorer.score(cand_df, features, regime_row)

        # 6.5) 附加行业标签与所属板块强度
        scored = self._attach_sector(scored, data_date)

        # 7) 落库
        plan_date = self._next_trade_date(data_date)
        if persist and not scored.empty:
            self._persist(scored, data_date, plan_date, regime_row)

        result = {
            "data_date": str(data_date),
            "plan_date": str(plan_date) if plan_date else None,
            "generated_at": started.isoformat(timespec="seconds"),
            "elapsed_sec": round((datetime.now() - started).total_seconds(), 1),
            "market": {
                "state": regime_row.get("state"),
                "label": regime_row.get("state_label"),
                "confidence": regime_row.get("confidence"),
                "breadth_ma20": regime_row.get("breadth_ma20"),
                "breadth_ma60": regime_row.get("breadth_ma60"),
                "limit_up": regime_row.get("limit_up_count"),
                "limit_down": regime_row.get("limit_down_count"),
                "broken_rate": regime_row.get("broken_rate"),
                "top_sector": regime_row.get("top_sector"),
                "top_sector_share": regime_row.get("top_sector_share"),
                "weights": weights,
            },
            "strategy_stats": strategy_stats,
            "recommendations": scored,
            "params_version": params_version(),
            "pool_size": len(pool),
        }
        logger.info(
            "推荐完成：%d 条候选，耗时 %.1fs",
            len(scored), result["elapsed_sec"],
        )
        return result

    # ---------------- 行业附加 ----------------
    def _attach_sector(self, scored: pd.DataFrame, data_date: date) -> pd.DataFrame:
        """给候选股附加「所属行业」与「该行业当日强度」。

        只用于**展示与归因**，不参与打分。理由是实测结论：
        板块强度最高的组次日平均 +0.218%，全市场基准 +0.152%，
        超额仅约 0.07 个百分点，且体现在均值而非胜率上（各分位胜率几乎相同、
        中位收益全为 0），属于赔率型弱信号 —— 不足以作为评分因子，
        但作为「这只票在什么板块、板块强不强」的上下文很有价值。

        任何失败都不影响选股主流程（行业数据是增强项，不是必需项）。
        """
        if scored is None or scored.empty:
            return scored
        try:
            ind = self.storage.query_df(
                """
                SELECT code, industry_name AS industry FROM (
                    SELECT code, industry_name,
                           ROW_NUMBER() OVER (PARTITION BY code
                                              ORDER BY is_primary DESC, source) AS rn
                    FROM dim_stock_industry
                ) t WHERE rn = 1
                """
            )
            if ind.empty:
                logger.info("行业映射为空，本次不附加行业信息（可执行 astock.cli sector 建立）")
                return scored
            scored = scored.merge(ind, on="code", how="left")
        except Exception as exc:  # noqa: BLE001
            logger.warning("行业标签附加失败（不影响选股）：%s", str(exc)[:100])
            return scored

        # 所属板块的当日强度，作为报告的上下文
        try:
            source = str(self.cfg.get("sector.primary_source", "sw") or "sw")
            st = self.storage.query_df(
                "SELECT industry_name AS industry, strength_score AS sector_strength "
                "FROM dws_sector_strength WHERE date = ? AND source = ?",
                [data_date, source],
            )
            if not st.empty:
                scored = scored.merge(st, on="industry", how="left")
        except Exception as exc:  # noqa: BLE001
            logger.warning("板块强度附加失败（不影响选股）：%s", str(exc)[:100])

        # 近期涨停题材：候选股（尤其短线档）的上涨逻辑往往由题材驱动，
        # 申万行业分类看不出「华字辈」「资产重组」这类联动，涨停池的题材字段可以。
        # 查「最近一次进涨停池」的记录：涨停洗盘类策略选的是昨日涨停股，
        # 它们的涨停发生在 data_date 之前，不能用当天的池。
        try:
            pool = self.storage.query_df(
                """
                SELECT code, reason AS limit_reason, boards AS limit_boards,
                       board_text AS limit_board_text,
                       seal_money AS limit_seal_money, pool_date
                FROM (
                    SELECT code, reason, boards, board_text, seal_money,
                           date AS pool_date,
                           ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn
                    FROM dwd_limit_up
                    WHERE date < ? AND date >= ? - INTERVAL 7 DAY
                ) t WHERE rn = 1
                """,
                [data_date, data_date],
            )
            if not pool.empty:
                scored = scored.merge(pool, on="code", how="left")
        except Exception as exc:  # noqa: BLE001
            logger.warning("涨停题材附加失败（不影响选股）：%s", str(exc)[:100])
        return scored

    # ---------------- 落库 ----------------
    def _persist(
        self,
        scored: pd.DataFrame,
        data_date: date,
        plan_date: date | None,
        regime_row: dict[str, Any],
    ) -> None:
        now = datetime.now()
        rows = []
        for _, r in scored.iterrows():
            rec_id = f"{data_date}_{r['tier']}_{r['code']}"
            rows.append(
                {
                    "rec_id": rec_id,
                    # 用 data_date 而不是 now().date()：推荐是对**数据日**的评价，
                    # 凌晨补跑时 now() 会是第二天，与 rec_id 的日期也不一致。
                    "rec_date": data_date,
                    # ⚠️ 绝不能写成 `plan_date or data_date`：交易日历缺少未来日期时
                    # `_next_trade_date` 返回 None，兜底会让「计划买入日 = 推荐日」，
                    # 复盘随后用**同一天**的 K 线结算 —— 用当天收盘信息回测当天开盘买入，
                    # 是典型前视偏差，会系统性高估策略表现（实测已污染 12 条记录）。
                    # 宁可存 NULL 让复盘跳过，也不能编一个假的买入日。
                    "trade_date": plan_date,
                    "market_state": regime_row.get("state"),
                    "market_label": regime_row.get("state_label"),
                    "tier": r["tier"],
                    "tier_rank": int(r["tier_rank"]),
                    "code": to_str(r["code"]),
                    "name": to_str(r.get("name")),
                    "strategy": to_str(r.get("strategy")),
                    "primary_strategy": to_str(r.get("primary_strategy") or r.get("strategy")),
                    "final_score": to_float(r.get("final_score")),
                    "strategy_score": to_float(r.get("strategy_score")),
                    "factor_score": to_float(r.get("factor_score")),
                    # 已废弃字段（保留列以兼容历史数据）。它曾是「档位权重归一化」，
                    # 在档内是常数、从未影响排序，且让分数跨档不可比，故不再计算。
                    "market_fit": None,
                    "risk_penalty": to_float(r.get("risk_penalty")),
                    "reasons": json.dumps(to_list(r.get("reasons")), ensure_ascii=False),
                    "score_detail": r["score_detail"],
                    "params_version": params_version(),
                    "created_at": now,
                }
            )
        n = self.storage.upsert_df(pd.DataFrame(rows), "ads_recommend")
        logger.info("推荐记录落库：%d 条（数据日 %s → 计划交易日 %s）", n, data_date, plan_date)

    # ---------------- 工具 ----------------
    def _next_trade_date(self, after: date) -> date | None:
        return self.storage.query_value(
            "SELECT MIN(date) FROM trade_calendar WHERE is_open = TRUE AND date > ?", [after]
        )

    # ---------------- 查询 ----------------
    def latest_recommendations(self, limit_date: date | None = None) -> pd.DataFrame:
        target = limit_date or self.storage.query_value("SELECT MAX(rec_date) FROM ads_recommend")
        if target is None:
            return pd.DataFrame()
        return self.storage.query_df(
            "SELECT * FROM ads_recommend WHERE rec_date = ? ORDER BY tier, tier_rank", [target]
        )

    def history(self, days: int = 30) -> pd.DataFrame:
        since = date.today() - timedelta(days=days)
        return self.storage.query_df(
            "SELECT * FROM ads_recommend WHERE rec_date >= ? ORDER BY rec_date DESC, tier, tier_rank",
            [since],
        )

    @staticmethod
    def report_dir() -> Path:
        cfg = get_config()
        p = cfg.data_dir / "reports"
        p.mkdir(parents=True, exist_ok=True)
        return p
