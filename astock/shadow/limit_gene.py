# -*- coding: utf-8 -*-
"""影子模块：涨停基因 + 缩量不破位（`ads_shadow_pick`）。

------------------------------------------------------------------
信号定义（已通过样本外检验，见 settings.yaml 的 shadow_gene 注释）
------------------------------------------------------------------
候选 = 同时满足：
  · 涨停基因：过去 28 个自然日内有涨停（`zt20 ≥ 1`）
  · 近期活跃：距上次涨停 ≤ 10 天
  · 缩量    ：当日量 / 前 5 日均量 < 0.7
  · 不破位  ：收盘 / 前 5 日均价 − 1 ≥ −0.02

**这四个条件里，"回踩"是方向性错误**：实测把「不破位」换成「回踩破位」
（vs_ma5 < −0.02）后，次日涨停率从 8.29% 掉到 3.58%、超额由 +0.668% 转为
−0.249%（t=−2.94，显著为负）。所以缩量**整理**有效，缩量**回踩**无效。

------------------------------------------------------------------
buy/sell 口径（必须与检验一致，否则数字不可比）
------------------------------------------------------------------
  买入 = 信号日**收盘市价**（整理期，不涉及涨停排队 → 买得到）
  卖出 = D+1 / D+3 / D+5 收盘（涨停价上有买盘队列 → 卖得出）
这与主链路的「次日开盘买入」不同，**两者的成绩不可混用**。

------------------------------------------------------------------
结构性设计：build 与 settle 分离
------------------------------------------------------------------
`build` 只写信号、不写收益（此刻未来还没有发生）；`settle` 事后补收益。
这样**前视在结构上就不可能发生**——不是靠"记得不要用未来数据"的纪律，
而是 build 时刻根本拿不到未来数据。主链路曾因 `plan_date or data_date` 的兜底
写过 12 条被污染的记录，那类问题在这里不可能出现。
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime, timedelta

import pandas as pd

from astock.config import get_config
from astock.logger import get_logger
from astock.storage.db import Storage, get_storage

logger = get_logger("shadow.limit_gene")

# 取前 5 个交易日所需的自然日回溯（含长假也够）
_LOOKBACK_CALENDAR_DAYS = 20


def shadow_params_fingerprint() -> str:
    """当前策略的**参数指纹**（`ads_shadow_pick.params` 列存的就是它）。

    ⚠️ **必须只读配置、不碰数据库**：展示层（FastAPI）会调用它，而那个进程
    绝不能打开主库 —— DuckDB 是独占文件锁，一旦 API 持有连接，18:30 的 daily
    就会因"文件被占用"直接失败。实测踩过：让 API 调一个构造了
    `LimitGeneShadow()`（含 storage）的函数，服务进程立刻锁住了主库。
    """
    s = get_config().section("shadow_gene")
    amt = shadow_min_amount_avg20()
    amt_txt = f"/amt20>={amt / 1e8:g}亿" if amt else ""
    sealed = "/noSealed" if bool(s.get("exclude_sealed_today", True)) else ""
    score = shadow_min_signal_score()
    return (f"zt{int(s.get('zt_window_days', 28))}"
            f"/recent{int(s.get('recent_days', 10))}"
            f"/score>={score:g}(vol<={1.0 - score / 100.0:g}){amt_txt}{sealed}"
            f"/ma5>={float(s.get('vs_ma5_min', -0.02))}")


def shadow_current_params() -> str:
    """（兼容保留）当前策略的参数指纹。"""
    return shadow_params_fingerprint()


def shadow_min_signal_score() -> float:
    """当前生效的「信号分下限」：低于它不推荐。

    **配置的唯一读取点** —— 引擎（build）、报告（builder）、网页接口（api）全部
    调用它，而不是各自读一次配置或各自写死一个数。本项目反复出现的失效模式就是
    "同一口径存在两个来源"，最后两边给出两个答案且无人发现。

    依据 `signal_score = (1 − vol_ratio) × 100`，故此值等价于缩量阈值
    `vol_ratio_max = 1 − 分数/100`。
    """
    s = get_config().section("shadow_gene")
    m = s.get("min_signal_score")
    if m is not None:
        return float(m)
    return (1.0 - float(s.get("vol_ratio_max", 0.7))) * 100.0


def shadow_min_amount_avg20() -> float:
    """流动性硬约束：前 20 个交易日均成交额下限（0 = 不启用）。

    与 `shadow_min_signal_score` 同理 —— **配置的唯一读取点**：引擎、报告、
    网页接口都调它，而不是各自读配置或各自写一个数。
    展示层必须按同一阈值过滤，否则页面上会出现引擎已不再产出的"幽灵候选"。
    """
    s = get_config().section("shadow_gene")
    return float(s.get("min_amount_avg20", 0) or 0)


class LimitGeneShadow:
    """涨停基因影子模块。"""

    def __init__(self, storage: Storage | None = None) -> None:
        self.cfg = get_config()
        self.storage = storage or get_storage()
        s = self.cfg.section("shadow_gene")
        self.enabled = bool(s.get("enabled", True))
        self.zt_window = int(s.get("zt_window_days", 28))
        self.recent_days = int(s.get("recent_days", 10))
        self.ma5_min = float(s.get("vs_ma5_min", -0.02))
        self.max_picks = int(s.get("max_picks", 60))
        # 缩量门槛：以「信号分下限」为准，**反推** vol_ratio_max（见模块级函数）。
        self.min_signal_score = shadow_min_signal_score()
        self.vol_max = 1.0 - self.min_signal_score / 100.0
        # 流动性硬约束（见模块级函数）
        self.min_amount_avg20 = shadow_min_amount_avg20()
        # 排除「信号日当天已封板」的候选：封板时收盘买不进（铁律 2）。
        # 这是策略定义的一部分，不是可选优化 —— 实测口径 B（次日开盘买）下
        # 排除后超额由 −0.016% 变为 +0.137%（t 由 −0.12 升到 1.23），
        # 机制清楚：封板股次日普遍高开低走。
        self.exclude_sealed = bool(s.get("exclude_sealed_today", True))

    @property
    def _params(self) -> str:
        """参数指纹，逐行落库。**必须随阈值变化而变** —— 它是审计与分段归因的
        唯一依据（否则不同参数时期的样本会混在一起统计）。

        委托给模块级函数，保证"落库的指纹"与"展示层算的指纹"**同源**：
        两处各写一份 format 字符串迟早会分叉。
        """
        return shadow_params_fingerprint()

    # ---------------- 信号 ----------------
    def build(self, trade_date: date_cls | None = None, source: str = "daily") -> pd.DataFrame:
        """生成信号并落库（**不含收益**）。返回写入的候选。

        source="daily"    ：用日线收盘价/全日量（盘后可用，也能回填历史）
        source="snapshot" ：用盘中快照（14:00 决策用）。此时"缩量"用数据源的
                            **量比**代替 —— 量比 = 当日累计量 / (过去 5 日每分钟
                            均量 × 已开盘分钟数)，等价于「按当前节奏外推的全日
                            量与过去 5 日全日均量之比」，即与日线口径同量纲。
                            偏差在于 A 股成交呈 U 形（开盘/收盘重），14:00 时
                            量比会略微低估全日比例。
        """
        if not self.enabled:
            logger.info("影子模块未启用，跳过")
            return pd.DataFrame()

        trade_date = trade_date or self._latest_date()
        if trade_date is None:
            logger.warning("日线表为空，无法生成影子信号")
            return pd.DataFrame()

        df = (self._query_snapshot(trade_date) if source == "snapshot"
              else self._query_daily(trade_date))
        if df.empty:
            logger.info("影子信号：%s 无候选（source=%s）", trade_date, source)
            return df

        if len(df) > self.max_picks:
            df = df.sort_values("vol_ratio").head(self.max_picks).reset_index(drop=True)
            logger.warning("影子候选超过上限，按缩量程度截取前 %d 只", self.max_picks)

        out = pd.DataFrame({
            "date": trade_date,
            "code": df["code"].astype(str),
            "name": df["name"],
            "close": df["close"].astype(float),
            "zt20": df["zt20"].astype(int),
            "days_since_zt": df["days_since_zt"].astype(int),
            "vol_ratio": df["vol_ratio"].astype(float),
            "vs_ma5": df["vs_ma5"].astype(float),
            # 前 20 个交易日均成交额：流动性下限的判据，**逐行落库**，
            # 让展示层/报告能按同一列过滤（不必各自重算，避免口径漂移）
            "amount_ma20": df["amount_ma20"].astype(float),
            # 新行恒非封板（候选已排除封板）——显式写入，便于展示层统一过滤
            "is_sealed": False,
            # 参考分：越缩量越高。仅用于展示排序，**不参与选股**
            # （截断只按 vol_ratio，避免引入未经检验的权重）
            "signal_score": (1.0 - df["vol_ratio"].astype(float)).clip(lower=0) * 100,
            "params": self._params,
            "created_at": datetime.now(),
        })
        n = self.storage.upsert_df(out, "ads_shadow_pick")
        logger.info("影子信号已落库：%s 共 %d 只（source=%s）", trade_date, n, source)
        return out

    # ---------------- 结算 ----------------
    def settle(self) -> int:
        """为尚未结算完整的信号补上 D+1/D+3/D+5 收益与基准。返回更新行数。

        判据是 `ret1 IS NULL OR ret5 IS NULL`，而不是只看 `ret1`：
        信号日当天只能拿到 D+1 收益（更远的还没发生）。若只判 `ret1`，
        当天结算一次后该行就再也不会被写入，`ret3`/`ret5` 会**永久留空**，
        而"持 3 日/5 日"恰恰是我们最关心的口径。
        重复结算同一行是安全的：收益由行情重算，值不变。
        """
        # 判据含 exec_d1 IS NULL：历史行是在"可执行口径"列存在之前结算的，
        # 若只判 ret1/ret5，它们的 exec_* 会**永久为空**（审计口径一旦缺列，
        # 所有依赖它的统计都会静默漏掉这批样本）。
        pending = self.storage.query_df(
            "SELECT date, code FROM ads_shadow_pick "
            "WHERE ret1 IS NULL OR ret5 IS NULL OR exec_d1 IS NULL "
            "   OR exec_bench IS NULL"
        )
        if pending.empty:
            return 0

        fwd = self._forward_returns()
        if fwd.empty:
            return 0

        merged = pending.merge(fwd, on=["date", "code"], how="inner")
        merged = merged[merged["c1"].notna()]
        if merged.empty:
            return 0

        # 基准：同期**可交易池**等权收益。两个口径各一套 —— 超额必须与自身口径
        # 的基准相减，否则等于拿"收盘买"的策略去比"开盘买"的市场（口径错配）。
        #
        # ⚠️ 必须施加**股票池过滤**（与 eval/judge.py 的 POOL_SQL 一致）。
        # 曾经这里直接对全量日线求均值，于是基准里混进了 ST、北交所、次新股，
        # 与裁判模块的基准差出 0.68pp —— 基准口径不一致，超额就是错的。
        bench = self.storage.query_df("""
            SELECT f.date,
                   AVG(f.ret1)     AS benchmark,
                   AVG(f.exec_d1)  AS exec_bench
            FROM (
                SELECT code, date,
                       (LEAD(close, 1) OVER w / NULLIF(close, 0) - 1) * 100 AS ret1,
                       (LEAD(close, 2) OVER w / NULLIF(LEAD(open, 1) OVER w, 0) - 1) * 100
                           AS exec_d1
                FROM dwd_daily_bar
                WHERE open > 0 AND close > 0 AND volume > 0
                WINDOW w AS (PARTITION BY code ORDER BY date)
            ) f
            JOIN dim_stock s ON s.code = f.code
            WHERE s.board IN ('main', 'gem')
              AND COALESCE(s.is_st, FALSE) = FALSE
              AND (s.out_date IS NULL OR s.out_date > f.date)
              AND s.ipo_date <= f.date - INTERVAL 120 DAY
            GROUP BY f.date
        """)
        # 是否命中涨停：看该股在**下一个交易日**是否在涨停池里
        hit = self.storage.query_df("SELECT DISTINCT code, date FROM dwd_limit_up")
        hit = hit.rename(columns={"date": "next_date"})
        hit["is_zt"] = True

        merged = merged.merge(bench, on="date", how="left")
        merged = merged.merge(hit, on=["code", "next_date"], how="left")

        upd = pd.DataFrame({
            "date": merged["date"],
            "code": merged["code"],
            "next_date": merged["next_date"],
            "ret1": merged["ret1"],
            "ret3": merged["ret3"],
            "ret5": merged["ret5"],
            "hit_limit_up": merged["is_zt"].fillna(False).astype(bool),
            "benchmark": merged["benchmark"],
            "excess": merged["ret1"] - merged["benchmark"],
            "exec_d1": merged["exec_d1"],
            "exec_d3": merged["exec_d3"],
            "exec_d5": merged["exec_d5"],
            "exec_bench": merged["exec_bench"],
        })
        # 只更新结算列：先取回全行再合并，避免 upsert 整行覆盖丢掉信号字段
        full = self.storage.query_df("SELECT * FROM ads_shadow_pick")
        full = full.drop(columns=[c for c in
                                  ["next_date", "ret1", "ret3", "ret5",
                                   "hit_limit_up", "benchmark", "excess",
                                   "exec_d1", "exec_d3", "exec_d5", "exec_bench"]
                                  if c in full.columns])
        out = full.merge(upd, on=["date", "code"], how="inner")
        n = self.storage.upsert_df(out, "ads_shadow_pick")
        logger.info("影子信号结算完成：%d 行", n)
        return n

    # ---------------- 回填 ----------------
    def trading_days(self, start: date_cls | None = None,
                     end: date_cls | None = None) -> list[date_cls]:
        sql = "SELECT DISTINCT date FROM dwd_daily_bar WHERE open > 0 AND close > 0"
        params: list = []
        if start is not None:
            sql += " AND date >= ?"
            params.append(start)
        if end is not None:
            sql += " AND date <= ?"
            params.append(end)
        sql += " ORDER BY date"
        return self.storage.query_df(sql, params)["date"].tolist()

    def backfill(self, start: date_cls | None = None, end: date_cls | None = None,
                 progress=None) -> int:
        """逐日生成历史信号（**不含收益**）。

        为什么要能回填：检验结论（240 天 / 9186 笔）是在临时脚本里算的。
        把它固化成可复现的命令，才能随时用同一口径重算 —— 否则"结论"只存在于
        一次性脚本的输出里，无法随数据更新而复核。
        注意：`dwd_limit_up` 只覆盖 2025-09-23 起，早于此日期"涨停基因"恒不可见。
        """
        days = self.trading_days(start, end)
        total = 0
        for i, d in enumerate(days, 1):
            total += len(self.build(d, source="daily"))
            if progress is not None and i % 20 == 0:
                progress(i, len(days), total)
        return total

    # ---------------- 统计 ----------------
    def report(self, days: int = 60) -> str:
        """影子运行摘要：命中率、**可实现收益**、超额与显著性。

        主口径 = 可实现口径（`exec_*`：次日开盘买 → 再次日收盘卖）。
        旧的 `ret1/3/5`（信号日收盘买）只在末尾作为"诊断"列出 —— 它的收益
        来自买不进的封板股，不能用来判断策略好坏。
        """
        # 必须按**日期**截窗口，不能用 `LIMIT days × max_picks`：
        # max_picks=200 时它等于 12000 行，会把全部历史都算进来，
        # 而输出却写着"近 N 日"（实测踩过：显示 243 日却自称 60 日）。
        df = self.storage.query_df(
            f"SELECT * FROM ads_shadow_pick WHERE exec_d1 IS NOT NULL "
            f"AND date >= (SELECT MAX(date) FROM ads_shadow_pick) - INTERVAL {int(days)} DAY "
            f"ORDER BY date DESC"
        )
        if df.empty:
            return "影子模块：暂无已结算样本。"

        daily = df.groupby("date", as_index=False).agg(
            r=("exec_d1", "mean"), bench=("exec_bench", "mean"),
            r3=("exec_d3", "mean"), r5=("exec_d5", "mean"))
        daily["ex"] = daily["r"] - daily["bench"]
        t = 0.0
        if len(daily) > 2:
            sd = daily["ex"].std(ddof=1)
            t = float(daily["ex"].mean() / (sd / len(daily) ** 0.5)) if sd else 0.0

        lines = [
            f"影子模块（涨停基因+缩量+不破位+不买封板）　参数 {self._params}",
            f"  样本 {len(df)} 笔 / {df['date'].nunique()} 个交易日"
            f"（近 {int(days)} 日窗口）",
            f"  次日涨停率 {100.0 * df['hit_limit_up'].fillna(False).mean():.2f}%"
            f"（全市场约 2.05%）",
            "  ── 可实现口径（次日开盘买 → 次日收盘卖，T+1 下最早合法）──",
            f"  收益：1日 {daily['r'].mean():+.3f}%　"
            f"3日 {daily['r3'].mean():+.3f}%　5日 {daily['r5'].mean():+.3f}%",
            f"  日均超额 {daily['ex'].mean():+.3f}%　t = {t:+.2f}"
            f"{'（显著）' if abs(t) >= 2 else '（不显著）'}",
        ]
        # 与检验结论一致的提醒：收益集中在尾部
        tail = df[df["hit_limit_up"].fillna(False)]
        rest = df[~df["hit_limit_up"].fillna(False)]
        if not rest.empty:
            lines.append(
                f"  ⚠ 收益来源：命中涨停 {len(tail)} 笔均值 {tail['exec_d1'].mean():+.2f}%，"
                f"其余 {len(rest)} 笔均值 {rest['exec_d1'].mean():+.2f}%"
            )
        old = df["ret1"].dropna()
        if not old.empty:
            lines.append(
                f"  （诊断·不可执行：旧口径『信号日收盘买』为 {old.mean():+.3f}%，"
                f"该收益来自买不进的封板股，勿用于决策）"
            )
        return "\n".join(lines)

    # ---------------- 内部 ----------------
    def _latest_date(self) -> date_cls | None:
        v = self.storage.query_value(
            "SELECT MAX(date) FROM dwd_intraday_snapshot"
        )
        if v is not None:
            return v.date() if hasattr(v, "date") else v
        v = self.storage.query_value("SELECT MAX(date) FROM dwd_daily_bar")
        if v is None:
            return None
        return v.date() if hasattr(v, "date") else v

    def _gene_cte(self, target: date_cls, lower: date_cls) -> tuple[str, list]:
        """涨停基因 CTE + 参数（供 daily / snapshot 两种模式复用）。"""
        sql = """
        gene AS (
            SELECT b.code, COUNT(l.date) AS zt20,
                   DATE_DIFF('day', MAX(l.date), b.date) AS days_since_zt
            FROM (SELECT DISTINCT code, date FROM dwd_daily_bar WHERE date = ?) b
            LEFT JOIN dwd_limit_up l
                   ON l.code = b.code AND l.date < b.date AND l.date >= ?
            GROUP BY b.code, b.date
        )"""
        return sql, [target, lower]

    def _query_daily(self, target: date_cls) -> pd.DataFrame:
        lower = target - timedelta(days=self.zt_window)
        gene_sql, gene_params = self._gene_cte(target, lower)
        win_lower = target - timedelta(days=60)
        # 流动性：前 20 个交易日（不含当日）的日均成交额。
        # 不含当日的原因见 settings.yaml shadow_gene.min_amount_avg20 的注释 ——
        # 简言之：14:00 快照路径拿不到当日全日成交额，两条路径必须同口径。
        amt_col = ", amount" if self.min_amount_avg20 > 0 else ""
        amt_win = (""", AVG(amount) OVER (PARTITION BY code ORDER BY date
                       ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS amount_ma20"""
                   if self.min_amount_avg20 > 0 else ", NULL AS amount_ma20")
        amt_where = " AND COALESCE(r.amount_ma20, 0) >= ?" if self.min_amount_avg20 > 0 else ""
        amt_param = [self.min_amount_avg20] if self.min_amount_avg20 > 0 else []
        # 铁律 2：信号日已封板 → 收盘买不进（`dwd_limit_up` 标记的是当日收盘涨停）
        sealed_where = (" AND NOT EXISTS (SELECT 1 FROM dwd_limit_up z "
                        "WHERE z.code = r.code AND z.date = r.date)"
                        if self.exclude_sealed else "")
        sql = f"""
        WITH recent AS (
            SELECT code, date, close{amt_col},
                   volume / NULLIF(AVG(volume) OVER (PARTITION BY code ORDER BY date
                       ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING), 0) AS vol_ratio,
                   close / NULLIF(AVG(close) OVER (PARTITION BY code ORDER BY date
                       ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING), 0) - 1 AS vs_ma5
                   {amt_win}
            FROM dwd_daily_bar
            WHERE open > 0 AND close > 0 AND volume > 0 AND date >= ? AND date <= ?
        ),
        {gene_sql}
        SELECT r.code, s.name, r.close, r.vol_ratio, r.vs_ma5, r.amount_ma20,
               g.zt20, g.days_since_zt
        FROM recent r
        JOIN gene g ON g.code = r.code
        JOIN dim_stock s ON s.code = r.code
        WHERE r.date = ?
          AND s.board IN ('main', 'gem')
          AND COALESCE(s.is_st, FALSE) = FALSE
          AND (s.out_date IS NULL OR s.out_date > r.date)
          AND DATE_DIFF('day', s.ipo_date, r.date) >= 120
          AND g.zt20 >= 1
          AND g.days_since_zt <= ?
          AND r.vol_ratio < ?
          AND r.vs_ma5 >= ?{amt_where}{sealed_where}
        ORDER BY r.vol_ratio
        """
        params = [win_lower, target, *gene_params, target,
                  self.recent_days, self.vol_max, self.ma5_min, *amt_param]
        return self.storage.query_df(sql, params)

    def _query_snapshot(self, target: date_cls) -> pd.DataFrame:
        """用当日快照 + 前 5 日收盘均线。缩量用源提供的**量比**代替。"""
        lower = target - timedelta(days=self.zt_window)
        gene_sql, gene_params = self._gene_cte(target, lower)
        prev = self.storage.query_df(
            "SELECT DISTINCT date FROM dwd_daily_bar WHERE date < ? "
            "ORDER BY date DESC LIMIT 5",
            [target],
        )
        days = [d for d in prev["date"].tolist()] if not prev.empty else []
        if len(days) < 5:
            logger.warning("历史不足 5 个交易日，无法计算均线（snapshot 模式）")
            return pd.DataFrame()
        marks = ", ".join("?" for _ in days)
        # 流动性 CTE：与日线路径**同一指标**（前 20 个交易日日均成交额，不含当日）。
        # 这里用 ROW_NUMBER 取最近 20 行再平均 —— 因为快照路径没有"当日"这一行
        # （当日只有半天数据），语义上正好就是"前 20 个已完成交易日"。
        amt_cte = amt_join = amt_where = ""
        amt_param: list = []
        if self.min_amount_avg20 > 0:
            amt_cte = f""",
        amt AS (
            SELECT code, AVG(amount) AS amount_ma20 FROM (
                SELECT code, amount,
                       ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn
                FROM dwd_daily_bar
                WHERE date < ? AND date >= ? AND amount > 0
            ) t WHERE rn <= 20 GROUP BY code
        )"""
            amt_join = " JOIN amt a ON a.code = k.code"
            amt_where = " AND COALESCE(a.amount_ma20, 0) >= ?"
        sealed_where = (" AND NOT EXISTS (SELECT 1 FROM dwd_limit_up z "
                        "WHERE z.code = k.code AND z.date = k.date)"
                        if self.exclude_sealed else "")
        sql = f"""
        WITH ma AS (
            SELECT code, AVG(close) AS ma5, AVG(volume) AS avg_vol
            FROM dwd_daily_bar
            WHERE date IN ({marks}) AND close > 0 AND volume > 0
            GROUP BY code
        ),
        {gene_sql}{amt_cte}
        SELECT k.code, s.name, k.price AS close, k.volume_ratio AS vol_ratio,
               k.price / NULLIF(m.ma5, 0) - 1 AS vs_ma5,
               {'a.amount_ma20' if amt_cte else 'NULL'} AS amount_ma20,
               g.zt20, g.days_since_zt
        FROM dwd_intraday_snapshot k
        JOIN ma m ON m.code = k.code
        JOIN gene g ON g.code = k.code{amt_join}
        JOIN dim_stock s ON s.code = k.code
        WHERE k.date = ?
          AND s.board IN ('main', 'gem')
          AND COALESCE(s.is_st, FALSE) = FALSE
          AND (s.out_date IS NULL OR s.out_date > k.date)
          AND DATE_DIFF('day', s.ipo_date, k.date) >= 120
          AND g.zt20 >= 1
          AND g.days_since_zt <= ?
          AND k.volume_ratio IS NOT NULL
          AND k.volume_ratio < ?
          AND k.price / NULLIF(m.ma5, 0) - 1 >= ?{amt_where}{sealed_where}
        ORDER BY k.volume_ratio
        """
        if self.min_amount_avg20 > 0:
            amt_param = [target, target - timedelta(days=60)]
        # 参数顺序必须与 SQL 中 ? 出现的顺序**严格一致**。SQL 里的出现次序是：
        #   ① ma CTE 的 date IN (marks…) ② gene CTE (target, lower)
        #   ③ amt CTE (date < target, date >= target-60) ④ 各阈值
        # 所以 amt 必须排在 gene **之后**（amt_cte 在 SQL 文本里跟在 gene_sql 后面）。
        params = [*days, *gene_params, *amt_param, target,
                  self.recent_days, self.vol_max, self.ma5_min,
                  *([self.min_amount_avg20] if amt_where else [])]
        return self.storage.query_df(sql, params)

    def _forward_returns(self) -> pd.DataFrame:
        """每行自身为买入日的前瞻收益（窗口建在**全量**日线上，再 join 候选）。"""
        return self.storage.query_df("""
        WITH fwd AS (
            SELECT b.code, b.date, b.close,
                   LEAD(b.close, 1) OVER w AS c1,
                   LEAD(b.close, 3) OVER w AS c3,
                   LEAD(b.close, 5) OVER w AS c5,
                   LEAD(b.date, 1)  OVER w AS nd,
                   -- 可实现口径：买次日开盘，卖持有 N 日后的收盘
                   -- （T+1：买入日=t+1，最早卖出=t+2 收盘 ⇒ N 从 1 起）
                   LEAD(b.open, 1)  OVER w AS o1,
                   LEAD(b.close, 2) OVER w AS c2,
                   LEAD(b.close, 4) OVER w AS c4,
                   LEAD(b.close, 6) OVER w AS c6
            FROM dwd_daily_bar b
            WHERE b.open > 0 AND b.close > 0 AND b.volume > 0
            WINDOW w AS (PARTITION BY b.code ORDER BY b.date)
        )
        SELECT code, date, nd AS next_date, c1,
               (c1 / NULLIF(close, 0) - 1) * 100 AS ret1,
               (c3 / NULLIF(close, 0) - 1) * 100 AS ret3,
               (c5 / NULLIF(close, 0) - 1) * 100 AS ret5,
               (c2 / NULLIF(o1, 0) - 1) * 100 AS exec_d1,
               (c4 / NULLIF(o1, 0) - 1) * 100 AS exec_d3,
               (c6 / NULLIF(o1, 0) - 1) * 100 AS exec_d5
        FROM fwd
        """)
