# -*- coding: utf-8 -*-
"""样本外验证引擎：历史逐日回放。

设计原则（避免自欺欺人的回测结果）：

1. **严格 as-of**：每个交易日只用该日及之前的数据 —— 因子表按日存储，
   股票池按当日截面过滤，市场状态取当日快照，均无未来信息。
2. **可执行的价格**：以「信号日次一交易日开盘」为买入价，而不是当日收盘价。
3. **无幸存者偏差**：股票池按当时的 out_date 判断是否仍在上市，
   而不是用「今天还活着」反推。
4. **可检查稳定性**：按时间切分观察期/验证期，识别「前半段灵、后半段废」的参数。
"""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any

import pandas as pd

from astock.backtest.metrics import phase_split, summarise
from astock.config import get_config
from astock.data.universe import Universe
from astock.features.builder import FeatureBuilder
from astock.features.indicators import to_float, to_int, to_str
from astock.logger import get_logger
from astock.market.regime import MarketRegime
from astock.scoring.scorer import Scorer
from astock.storage.db import Storage, get_storage
from astock.strategy import build_strategies, StrategyContext
from astock.strategy.base import reset_strategy_errors, strategy_error_summary

logger = get_logger("backtest.engine")

# 远期收益视图：以每一行自身为「买入日」，open 为买入价
#
# ⚠️ 口径警告（2026-09-24 P0 修正，此前所有 A/B 都栽在这里）：
#   `ret1 = close/open − 1` 是「开盘买、当天收盘卖」—— **A 股 T+1 下不可实现**。
#   它系统性高估收益：候选股呈「次日低开→日内回升→再低开」形态，
#   该口径恰好吃到日内回升段，却把制度强制承担的隔夜（实测 −0.86%）漏掉。
#   实测用它算出的"显著超额 +0.476pp（t=5.66）"在可实现口径下全部消失。
#   ⇒ ret1/ret3/5/10 保留仅作诊断；**裁判是 ret_exit_d1o / ret_exit_d1c**。
#   注意 ret3/5/10 本身是合法的（LEAD(close,2/4/9) 均在买入日之后卖出）。
FORWARD_VIEW_SQL = """
CREATE OR REPLACE TEMP VIEW _fwd_ret AS
SELECT
    code,
    date,
    (close / NULLIF(open, 0) - 1) * 100                                AS ret1,
    (LEAD(open, 1)  OVER w / NULLIF(open, 0) - 1) * 100                AS ret_exit_d1o,
    (LEAD(close, 1) OVER w / NULLIF(open, 0) - 1) * 100                AS ret_exit_d1c,
    (LEAD(close, 2) OVER w / NULLIF(open, 0) - 1) * 100                AS ret3,
    (LEAD(close, 4) OVER w / NULLIF(open, 0) - 1) * 100                AS ret5,
    (LEAD(close, 9) OVER w / NULLIF(open, 0) - 1) * 100                AS ret10,
    (MAX(high) OVER w10 / NULLIF(open, 0) - 1) * 100                   AS max_gain,
    (MIN(low)  OVER w10 / NULLIF(open, 0) - 1) * 100                   AS max_dd
FROM dwd_daily_bar
WINDOW
    w   AS (PARTITION BY code ORDER BY date),
    w10 AS (PARTITION BY code ORDER BY date ROWS BETWEEN CURRENT ROW AND 9 FOLLOWING)
"""


class BacktestEngine:
    """历史回放引擎。"""

    def __init__(self, storage: Storage | None = None, boards: list[str] | None = None) -> None:
        """boards 用于覆盖股票池口径。

        默认 None ⇒ 取配置 `universe.boards`（可交易子集），
        因为回测的用途是给**推荐逻辑**校准权重，必须在"你真能买的标的"上校准。
        显式传入全部板块（main/gem/star/bj）可跑一遍全市场口径做对比 ——
        注意两者结果不能混用，口径不同。
        """
        self.cfg = get_config()
        self.storage = storage or get_storage()
        self.universe = Universe(self.storage)
        self.boards = boards
        self.builder = FeatureBuilder(self.storage)
        self.regime = MarketRegime(self.storage)
        self.scorer = Scorer(self.cfg, self.storage)
        self.strategies = build_strategies(self.cfg)

    # ---------------- 主流程 ----------------
    def run(
        self,
        start: date | None = None,
        end: date | None = None,
        tiers: list[str] | None = None,
        top_n: int | None = None,
        split: float = 0.6,
        persist: bool = True,
        progress_every: int = 20,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        top_n = top_n or int(self.cfg.get("scoring.total_picks", 15))
        # 命令行的 --top-n 必须真正生效：原先它只写进统计，选股用的是配置默认值
        # （Scorer 从 cfg 读名额），等于该参数形同虚设。
        self.scorer.total_picks = int(top_n)

        # 1) 准备表与远期收益视图
        self.storage.init_schema()
        self._ensure_table()
        self.storage.conn.execute(FORWARD_VIEW_SQL)

        # 2) 交易日与映射表
        dates = self._trading_dates(start, end)
        if not dates:
            raise RuntimeError("指定区间内没有因子数据，请先执行 factor 构建")
        next_map = self._next_trade_map()
        regime_map = self._regime_map()

        logger.info(
            "开始回放：%s ~ %s，共 %d 个交易日 | 候选总名额 %d 只 | 档位=%s",
            dates[0], dates[-1], len(dates), top_n, tiers or "全部",
        )

        active_tiers = set(tiers) if tiers else set(self.strategies.keys())
        records: list[dict[str, Any]] = []
        skipped = 0
        self._quiet_strategy_logs(True)
        reset_strategy_errors()

        try:
            records, skipped = self._replay(
                dates, next_map, regime_map, active_tiers, run_id, progress_every, started
            )
        finally:
            self._quiet_strategy_logs(False)

        # 策略异常会导致「该策略当天的信号全部丢失」，必须在结果里显式提示，
        # 否则用户会基于不完整的数据下结论。
        errors = strategy_error_summary()
        if errors:
            logger.warning(
                "回放期间有 %d 个策略发生异常，对应信号已丢失：%s",
                len(errors),
                "；".join(f"{k}×{v['count']}（{v['last']}）" for k, v in errors.items()),
            )

        if not records:
            raise RuntimeError("回放未产生任何信号，请检查股票池与策略配置")

        # 3) 关联远期收益
        detail = self._attach_returns(pd.DataFrame(records))
        detail = phase_split(detail, ratio=split)

        # 4) 落库
        if persist:
            self.storage.conn.execute("DELETE FROM ads_backtest WHERE run_id = ?", [run_id])
            self.storage.upsert_df(
                detail[
                    [
                        "run_id", "data_date", "trade_date", "market_state", "tier",
                        "tier_rank", "code", "name", "strategy", "all_strategies",
                        "hit_count", "final_score", "strategy_score", "factor_score",
                        "ret1", "ret_exit_d1o", "ret_exit_d1c",
                        "ret3", "ret5", "ret10", "max_gain", "max_dd", "phase",
                    ]
                ],
                "ads_backtest",
            )

        stats = summarise(detail)
        stats.update(
            {
                "run_id": run_id,
                "top_n": top_n,
                "tiers": sorted(active_tiers),
                "skipped_days": skipped,
                "elapsed_sec": round(time.perf_counter() - started, 1),
                "strategy_errors": errors,
                "detail": detail,
            }
        )
        logger.info(
            "回放完成：信号 %d 条，覆盖 %d 个交易日，耗时 %.1f 分钟",
            len(detail), stats["dates"]["days"], stats["elapsed_sec"] / 60,
        )
        return stats

    # ---------------- 逐日回放 ----------------
    def _replay(
        self,
        dates: list[date],
        next_map: dict[date, date],
        regime_map: dict[date, dict[str, Any]],
        active_tiers: set[str],
        run_id: str,
        progress_every: int,
        started: float,
    ) -> tuple[list[dict[str, Any]], int]:
        records: list[dict[str, Any]] = []
        skipped = 0

        for i, day in enumerate(dates, 1):
            plan_date = next_map.get(day)
            if plan_date is None:
                skipped += 1
                continue

            pool = self.universe.filtered_codes(day, verbose=False, boards=self.boards)
            if pool.empty:
                skipped += 1
                continue

            features = self.builder.load_features(day)
            if features.empty:
                skipped += 1
                continue

            ctx = StrategyContext(
                trade_date=day, features=features, universe=pool, cfg=self.cfg
            )

            raw: list[dict[str, Any]] = []
            for tier, instances in self.strategies.items():
                if tier not in active_tiers:
                    continue
                for strategy in instances:
                    raw.extend(h.as_dict() for h in strategy.screen(ctx))

            if raw:
                regime_row = regime_map.get(day, {})
                # 入场成本过滤：与 RecommendEngine 共用同一实现。
                # 只改推荐侧会让 A/B 回测对比失去意义（回测跑的还是旧逻辑）。
                from astock.features.entry_cost import apply_entry_cost_filter

                cand_df, _dropped = apply_entry_cost_filter(
                    pd.DataFrame(raw), self.storage, day, self.cfg
                )
                scored = self.scorer.score(cand_df, features, regime_row)
                for _, r in scored.iterrows():
                    records.append(
                        {
                            "run_id": run_id,
                            "data_date": day,
                            "trade_date": plan_date,
                            "market_state": regime_row.get("state"),
                            "tier": r["tier"],
                            "tier_rank": int(r["tier_rank"]),
                            "code": r["code"],
                            "name": r.get("name") or "",
                            # 按「主策略」归因，避免多策略共振被拼成组合名称导致统计碎片化
                            "strategy": to_str(r.get("primary_strategy")) or to_str(r.get("strategy")),
                            "all_strategies": to_str(r.get("strategy")),
                            "hit_count": to_int(r.get("hit_count"), 1),
                            "final_score": to_float(r.get("final_score")),
                            "strategy_score": to_float(r.get("strategy_score")),
                            "factor_score": to_float(r.get("factor_score")),
                        }
                    )

            if i % progress_every == 0 or i == len(dates):
                elapsed = time.perf_counter() - started
                logger.info(
                    "回放进度 %d/%d（%.0f%%）| 累计信号 %d 条 | 已用 %.1f 分钟 | 预计剩余 %.1f 分钟",
                    i, len(dates), i / len(dates) * 100, len(records),
                    elapsed / 60, (elapsed / i) * (len(dates) - i) / 60,
                )

        return records, skipped

    # ---------------- 辅助 ----------------
    @staticmethod
    def _quiet_strategy_logs(enable: bool) -> None:
        """回放会逐日调用策略，命中日志量巨大（数百天 × 7 策略），运行期间降噪。"""
        import logging

        level = logging.WARNING if enable else logging.INFO
        for name in ("astock.strategy.base", "astock.data.universe"):
            logging.getLogger(name).setLevel(level)

    def _ensure_table(self) -> None:
        """表结构升级检测。

        `CREATE TABLE IF NOT EXISTS` 不会修改已存在的表，因此当列发生变化时
        需要显式重建（回测结果可随时重跑产生，丢弃代价很低）。
        """
        try:
            cols = self.storage.query_df("PRAGMA table_info('ads_backtest')")["name"].tolist()
        except Exception:  # noqa: BLE001
            return
        required = {"hit_count", "all_strategies", "phase", "ret10", "ret_exit_d1o"}
        if not required.issubset(set(cols)):
            logger.warning("ads_backtest 表结构已升级，正在重建（旧回测记录将丢弃）")
            self.storage.execute("DROP TABLE IF EXISTS ads_backtest")
            self.storage.init_schema()

    def _trading_dates(self, start: date | None, end: date | None) -> list[date]:
        sql = "SELECT DISTINCT date FROM dws_feature"
        params: list[Any] = []
        conds = []
        if start is not None:
            conds.append("date >= ?")
            params.append(start)
        if end is not None:
            conds.append("date <= ?")
            params.append(end)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY date"
        df = self.storage.query_df(sql, params)
        return [d.date() if hasattr(d, "date") else d for d in df["date"].tolist()]

    def _next_trade_map(self) -> dict[date, date]:
        """每个交易日 → 其后的下一个交易日（用于确定计划买入日）。"""
        df = self.storage.query_df(
            "SELECT date FROM trade_calendar WHERE is_open = TRUE ORDER BY date"
        )
        days = [d.date() if hasattr(d, "date") else d for d in df["date"].tolist()]
        return {days[i]: days[i + 1] for i in range(len(days) - 1)}

    def _regime_map(self) -> dict[date, dict[str, Any]]:
        df = self.storage.query_df("SELECT * FROM dws_market_regime")
        if df.empty:
            logger.warning("市场状态表为空，回测将使用均衡权重（请先执行 astock.cli regime）")
            return {}
        out = {}
        for _, r in df.iterrows():
            key = r["date"].date() if hasattr(r["date"], "date") else r["date"]
            out[key] = r.to_dict()
        return out

    def _attach_returns(self, signals: pd.DataFrame) -> pd.DataFrame:
        """把远期收益视图关联到信号上（按 计划买入日 对齐）。"""
        self.storage.conn.register("_signals", signals)
        try:
            df = self.storage.query_df(
                """
                SELECT
                    s.run_id, s.data_date, s.trade_date, s.market_state, s.tier,
                    s.tier_rank, s.code, s.name, s.strategy, s.all_strategies,
                    s.hit_count, s.final_score, s.strategy_score, s.factor_score,
                    f.ret1, f.ret_exit_d1o, f.ret_exit_d1c,
                    f.ret3, f.ret5, f.ret10, f.max_gain, f.max_dd
                FROM _signals s
                LEFT JOIN _fwd_ret f
                       ON f.code = s.code AND f.date = s.trade_date
                """
            )
        finally:
            self.storage.conn.unregister("_signals")
        return df

    # ---------------- 查询 ----------------
    def load_run(self, run_id: str | None = None) -> pd.DataFrame:
        if run_id:
            return self.storage.query_df(
                "SELECT * FROM ads_backtest WHERE run_id = ? ORDER BY data_date, tier, tier_rank",
                [run_id],
            )
        latest = self.storage.query_value("SELECT MAX(run_id) FROM ads_backtest")
        if latest is None:
            return pd.DataFrame()
        return self.load_run(str(latest))
