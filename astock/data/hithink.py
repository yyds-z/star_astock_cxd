# -*- coding: utf-8 -*-
"""同花顺特色数据采集：涨停池 / 炸板池。

数据价值：涨停题材（limit_up_reason）、封单金额（seal_money）、
连板高度（continue_day_cnt）、首次涨停时间——这些是短线情绪档的核心输入，
免费源（akshare/adata/baostock）完全没有。

**关键约束：限流 15 次/分钟**（实测响应头 X-RateLimit-Limit: 15）。
因此请求间隔固定 4.2 秒，且只做「每日一次」的低频采集；
绝不能用它拉全市场日线（5559 只 × 15/分钟 ≈ 6 小时，且已有更稳的来源）。

历史深度：接口支持 date_ms 按交易日查询，实测上游至少保留 1 年
（更早的日期返回 0 条，且那几个日期经交易日历核对均为非交易日），
因此可回填整个回测区间。

连板天梯接口**不采集**：它是 30 天滚动窗口且每板位截断 4 只，数据不完整；
连板分布直接从 dwd_limit_up 的 boards 字段聚合即可。
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd

from astock.config import get_config
from astock.logger import get_logger
from astock.storage.db import Storage, get_storage

logger = get_logger("data.hithink")

BASE_URL = "https://fuyao.aicubes.cn"
PATH_LIMIT_UP = "/api/a-share/special-data/limit-up-pool"
PATH_LIMIT_BREAK = "/api/a-share/special-data/limit-break-pool"
# 龙虎榜：固定返回全量、**不分页**，因此每天只需 1 次请求（比涨停池还便宜）。
# 显式传 date 时必须是交易日，传非交易日返回 code=1002（不会自动回退）。
PATH_DRAGON_TIGER = "/api/a-share/special-data/dragon-tiger-list"
# 集合竞价快照：单次最多 100 个 thscode，只用于「当日候选股」这种小批量场景。
PATH_AUCTION = "/api/a-share/auction/snapshot"
# 全市场行情快照（盘中实时价/量/额）。实测单次 **500 只可用**（1000 只失败），
# 因此全市场约 11 次请求、40 余秒即可覆盖 —— 远优于竞价的 100 只上限。
#
# ⚠️ 两个实测坑，务必遵守：
#   1. **必须只传存续股票**。混入已退市代码会让整批失败，返回
#      code=1002 / `Unknown thscode: xxx`。这个报错看起来像"数量超限"，
#      实测正是它造成过一次错误结论（"批量 5 只就失败"）——
#      真实原因是请求里混进了已退市的 000003.SZ。
#   2. 本接口**不含** name / 量比 / 换手率 / 流通市值，需本地补齐或自算
#      （量比见 `astock/data/intraday.py`）。
PATH_SNAPSHOT = "/api/a-share/prices/snapshot"
SNAPSHOT_BATCH = 500
# 上游对"无法识别的代码"的报错会点名具体代码，形如：
#   [path] 业务错误 code=1002：Unknown A-share thscode: 301139.SZ
# 解析出它就能把这一只剔掉、保住同批其余 499 只（见 `_snapshot_batch`）。
_BAD_THSCODE_RE = re.compile(r"Unknown A-share thscode:\s*([0-9]{6})", re.I)
# 财务报表三件套：入参契约一致，**单只查询、不接受逗号**，因此每只股票 3 次请求。
# 全市场（5200 只 × 3）在 15 次/分钟限流下约需 17 小时 —— 只适合夜间跑，
# 日常只对候选股（约 15 只）采集。
PATH_INCOME = "/api/a-share/financials/income-statements"
PATH_BALANCE = "/api/a-share/financials/balance-sheets"
PATH_CASHFLOW = "/api/a-share/financials/cash-flow-statements"
# 全市场数据导出（Parquet 整库）：1 次请求下载全市场日 K，不占用 15 次/分钟限流
PATH_DUMP_DAILY_10D = "/api/dump/market-dumps/daily-k-10d/download-url"
PATH_DUMP_DAILY_10Y = "/api/dump/market-dumps/daily-k/download-url"
CST = timezone(timedelta(hours=8))


def date_to_ms(d: date) -> int:
    """交易日转上海时区零点毫秒戳（接口要求 Asia/Shanghai 00:00:00）。

    用本地时间构造会差 8 小时，导致查到的是前一天/后一天的数据。
    """
    return int(datetime(d.year, d.month, d.day, tzinfo=CST).timestamp() * 1000)


def _ms_to_date(value: Any):
    """毫秒时间戳 → 上海时区日期。

    必须显式做时区转换：`pd.to_datetime(ms, unit="ms")` 得到的是 **UTC** 时刻，
    而上游给的是 Asia/Shanghai 零点戳，直接取 .date() 会**整体偏移一天**
    （2026-06-30 变成 2026-06-29）。财报报告期差一天看似无关紧要，
    但会破坏「同一报告期」的匹配与同比计算，是隐蔽且影响正确性的错误。
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    return pd.to_datetime(value, unit="ms", utc=True).tz_convert(CST).date()


def _yoy(cur: Any, prev: Any) -> float | None:
    """同比增速(%)。

    上期为负或缺失时返回 None 而不是硬算：负基数下的同比会给出
    「亏损扩大反而显示正增长」这类误导性数字，宁可不给。
    """
    try:
        if cur is None or prev is None or float(prev) <= 0:
            return None
        return (float(cur) / float(prev) - 1) * 100
    except (TypeError, ValueError):
        return None


class HithinkCollector:
    """涨停/炸板池、龙虎榜、集合竞价、财务报表采集器（限流安全）。"""

    PAGE_SIZE = 200          # 接口上限 200；涨停一天最多 200+ 只，通常 1 页
    DEFAULT_GAP = 4.2        # 15 次/分钟 => 间隔至少 4 秒

    def __init__(self, storage: Storage | None = None) -> None:
        self.cfg = get_config()
        self.storage = storage or get_storage()
        self.gap = float(self.cfg.get("hithink.request_gap", self.DEFAULT_GAP) or self.DEFAULT_GAP)
        self.retry = int(self.cfg.get("hithink.retry", 2) or 2)
        self._last_request_at = 0.0

        key = self.cfg.env("HITHINK_FINANCE_API_KEY")
        if not key:
            raise RuntimeError(
                "未配置 HITHINK_FINANCE_API_KEY，无法采集涨停/炸板池。"
                "请在 .env 中填入同花顺金融数据服务的 API Key。"
            )
        import requests

        self.sess = requests.Session()
        self.sess.headers.update({"X-api-key": key})

    # ---------------- HTTP ----------------
    def _throttle(self) -> None:
        """按 15 次/分钟限流。超限会被服务端拒绝，宁可慢不能被封。"""
        wait = self.gap - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """带重试的 GET。业务错误（code != 0）不重试，网络/5xx 类错误重试。"""
        last_exc: Exception | None = None
        for attempt in range(self.retry + 1):
            self._throttle()
            self._last_request_at = time.monotonic()
            try:
                r = self.sess.get(BASE_URL + path, params=params, timeout=30)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning("[%s] 网络异常（第 %d 次）：%s", path, attempt + 1, str(exc)[:80])
                time.sleep(1.0 * (attempt + 1))
                continue
            if r.status_code >= 500:
                last_exc = RuntimeError("HTTP %s" % r.status_code)
                time.sleep(1.0 * (attempt + 1))
                continue
            # 限流必须**退避重试**，不能当业务错误直接抛出。
            # 长任务（财务全市场回填约 17 小时）中途必然撞上限流，
            # 若直接失败会导致整轮采集中断。官方口径：HTTP 429 或 code=4001 即限流。
            if r.status_code == 429:
                wait = self.gap * (attempt + 2)
                logger.warning("[%s] 触发限流（HTTP 429），%.0f 秒后重试", path, wait)
                time.sleep(wait)
                last_exc = RuntimeError("HTTP 429")
                continue
            try:
                data = r.json()
            except ValueError as exc:
                raise RuntimeError("[%s] 响应非 JSON：%s" % (path, r.text[:120])) from exc
            code = data.get("code")
            if code == 4001:
                wait = self.gap * (attempt + 2)
                logger.warning("[%s] 触发限流（code=4001），%.0f 秒后重试", path, wait)
                time.sleep(wait)
                last_exc = RuntimeError("rate limited (4001)")
                continue
            if code != 0:
                # 其它业务错误（参数/权限/无数据），重试无意义
                raise RuntimeError(
                    "[%s] 业务错误 code=%s：%s" % (path, code, str(data.get("message"))[:120])
                )
            return data.get("data") or {}
        raise RuntimeError("[%s] 请求失败（已重试 %d 次）：%s" % (path, self.retry, last_exc))

    def _fetch_pool(self, path: str, d: date) -> list[dict[str, Any]]:
        """拉取某日整个池（自动翻页）。"""
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self._get(path, {"date_ms": date_to_ms(d), "page": page, "size": self.PAGE_SIZE})
            items.extend(data.get("item") or [])
            pages = int((data.get("pagination") or {}).get("pages") or 1)
            if page >= pages:
                break
            page += 1
        return items

    # ---------------- 行映射 ----------------
    @staticmethod
    def _up_rows(d: date, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now = datetime.now()
        rows = []
        for it in items:
            code = str(it.get("ticker") or "").strip()
            if not code:
                continue
            rows.append(
                {
                    "date": d,
                    "code": code,
                    "name": it.get("name"),
                    "first_time": it.get("limit_up_time"),
                    "reason": it.get("limit_up_reason"),
                    "boards": it.get("continue_day_cnt"),
                    "board_text": it.get("continue_day_text"),
                    "seal_money": it.get("seal_money"),
                    "max_seal_money": it.get("max_seal_money"),
                    "price": it.get("last_price"),
                    "pct_chg": it.get("price_change_ratio_pct"),
                    "is_st": bool(it.get("is_st") or False),
                    "is_new": bool(it.get("is_new") or False),
                    "updated_at": now,
                }
            )
        return rows

    @staticmethod
    def _break_rows(d: date, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now = datetime.now()
        rows = []
        for it in items:
            code = str(it.get("ticker") or "").strip()
            if not code:
                continue
            rows.append(
                {
                    "date": d,
                    "code": code,
                    "name": it.get("name"),
                    "open_times": it.get("open_times"),
                    "price": it.get("last_price"),
                    "pct_chg": it.get("price_change_ratio_pct"),
                    "turnover_ratio": it.get("turnover_ratio_pct"),
                    "turnover": it.get("turnover"),
                    "updated_at": now,
                }
            )
        return rows

    # ---------------- 龙虎榜 ----------------
    @staticmethod
    def _dragon_rows(d: date, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """龙虎榜行映射。

        必须保留 range_days：同一只股票可能同时出现在「当日榜」和「3 日榜」，
        这是**两条不同的记录**（榜单口径不同），用 (date, code, range_days) 才不会互相覆盖。
        """
        now = datetime.now()
        rows = []
        for it in items:
            code = str(it.get("ticker") or "").strip()
            if not code:
                continue
            rows.append(
                {
                    "date": d,
                    "code": code,
                    "name": it.get("name"),
                    "range_days": it.get("range_days") or 1,
                    "net_value": it.get("net_value"),
                    "net_rate": it.get("net_rate"),
                    "buy_value": it.get("buy_value"),
                    "sell_value": it.get("sell_value"),
                    "amount": it.get("amount"),
                    "hot_rank": it.get("hot_rank"),
                    "org_net_value": it.get("org_net_value"),
                    "org_net_rate": it.get("org_net_rate"),
                    "org_buy_num": it.get("org_buy_num"),
                    "org_sell_num": it.get("org_sell_num"),
                    "hot_money_net_value": it.get("hot_money_net_value"),
                    "limit_reason": it.get("limit_reason"),
                    "updated_at": now,
                }
            )
        return rows

    def collect_dragon_day(self, d: date) -> int:
        """采集某天龙虎榜（1 次请求）。"""
        data = self._get(PATH_DRAGON_TIGER, {"board_type": "all", "date": str(d)})
        items = data.get("stock_items") or []
        rows = self._dragon_rows(d, items)
        if rows:
            self.storage.upsert_df(pd.DataFrame(rows), "dwd_dragon_tiger")
        logger.info("[%s] 龙虎榜 %d 条（上游 %d 条）", d, len(rows), len(items))
        return len(rows)

    def pending_dragon_dates(self, years: int | None = None, end: date | None = None) -> list[date]:
        """尚未采集龙虎榜的交易日。"""
        from astock.data.calendar import TradeCalendar

        cal = TradeCalendar(self.storage)
        end = end or date.today()
        last = self.storage.query_value("SELECT MAX(date) FROM dwd_dragon_tiger")
        if years is not None:
            start = end - timedelta(days=int(365.25 * years))
        elif last is not None:
            start = last + timedelta(days=1)
        else:
            start = end - timedelta(days=int(365.25 * 1))
        if start > end:
            return []
        return cal.open_dates(start, end)

    def sync_dragon_tiger(
        self,
        dates: list[date] | None = None,
        years: int | None = None,
        progress_every: int = 20,
    ) -> dict[str, Any]:
        """采集龙虎榜（每天 1 次请求，限流下约 4.2 秒/天）。"""
        from astock.data.calendar import TradeCalendar

        cal = TradeCalendar(self.storage)
        cal.sync()
        if dates is None:
            dates = self.pending_dragon_dates(years=years)
        dates = sorted(set(dates))
        if not dates:
            logger.info("龙虎榜已是最新，无需采集")
            return {"days": 0, "rows": 0, "failed": 0, "elapsed_sec": 0.0}

        started = time.monotonic()
        total = 0
        failed = 0
        for i, d in enumerate(dates, 1):
            try:
                total += self.collect_dragon_day(d)
            except Exception as exc:  # noqa: BLE001
                # 单日失败（如上游对该日无数据）不应中断整轮回填：
                # 否则一个异常日期会让后面几百天全部采不到。
                failed += 1
                logger.warning("[%s] 龙虎榜采集失败，跳过：%s", d, str(exc)[:120])
            if i % progress_every == 0 or i == len(dates):
                rate = i / max(time.monotonic() - started, 1e-6)
                remain = (len(dates) - i) / max(rate, 1e-6) / 60
                logger.info(
                    "龙虎榜采集进度 %d/%d（%s）| 预计剩余 %.1f 分钟",
                    i, len(dates), d, remain,
                )
        return {
            "days": len(dates) - failed,
            "rows": total,
            "failed": failed,
            "elapsed_sec": round(time.monotonic() - started, 1),
            "start": str(dates[0]),
            "end": str(dates[-1]),
        }

    # ---------------- 全市场日 K 补数 ----------------
    def fetch_dump_daily(self, days: int = 10) -> pd.DataFrame:
        """下载全市场最近 N 个交易日的日 K（Parquet 整库）。

        与逐只请求的区别（实测对比）：
          免费源（东财/baostock）：5263 次请求、10~26 分钟，且会被限流/封禁，
                                   收盘后要等 1~2 小时才发布；
          本接口：1 次请求 + 1 次下载，**1 秒**，实测 16:2x 就已包含当日全市场数据。
        """
        path = PATH_DUMP_DAILY_10D if days <= 10 else PATH_DUMP_DAILY_10Y
        meta = self._get(path, {})
        url = (meta or {}).get("presigned_url")
        if not url:
            raise RuntimeError("未取到导出下载链接（响应缺少 presigned_url）")
        import io

        r = self.sess.get(url, timeout=180)  # 预签名链接自带鉴权，无需 X-api-key
        if r.status_code != 200:
            raise RuntimeError(f"导出下载失败：HTTP {r.status_code}")
        df = pd.read_parquet(io.BytesIO(r.content))
        logger.info(
            "已下载全市场日K导出：%d 行，%d 只股票，%.2f MB",
            len(df), df["thscode"].nunique(), len(r.content) / 1024 / 1024,
        )
        return df

    def sync_daily_from_dump(self, days: int = 10) -> dict[str, Any]:
        """用全市场导出**补齐库内缺失的最新交易日**。

        两条硬约束（均由实测得出，务必遵守）：

        1. **只补不覆盖**：导出是未复权（adjusted=none），而 `dwd_daily_bar`
           存的是前复权。实测最近一日两者完全一致（5199/5199 只误差 <0.01%），
           但更早的日期在除权日必然不一致 —— 覆盖会在复权口径上引入断层。
           因此这里只写 `date > 库内最新日期` 的行。
        2. **换手率是近似值**：导出不含换手率，用库内流通市值反推流通股本后计算：
           `股本 = float_mv / close`（上一交易日），`换手 = volume / 股本 * 100`。
           日常中流通股本不变，误差只在增发/解禁当日出现。
        """
        latest = self.storage.query_value("SELECT MAX(date) FROM dwd_daily_bar")
        dump = self.fetch_dump_daily(days=days)
        dump = dump.copy()
        dump["date"] = (
            pd.to_datetime(dump["date_ms"], unit="ms", utc=True)
            .dt.tz_convert(CST).dt.date
        )
        dump["code"] = dump["thscode"].str.split(".").str[0]

        if latest is not None:
            dump = dump[dump["date"] > latest]
        if dump.empty:
            logger.info("导出中没有比库内更新的日期（库内最新 %s），无需补数", latest)
            return {"days": 0, "rows": 0, "latest_before": str(latest)}

        dates = sorted(dump["date"].unique())

        # 上一交易日收盘价：**优先取库内值**，保证 preclose 与前复权序列一致
        prev_close: dict[str, float] = {}
        if latest is not None:
            pdf = self.storage.query_df(
                "SELECT code, close FROM dwd_daily_bar WHERE date = ?", [latest]
            )
            prev_close = {str(r["code"]): float(r["close"]) for _, r in pdf.iterrows()}

        # 流通股本（用于近似换手率），取库内最新一期
        shares: dict[str, float] = {}
        try:
            fdf = self.storage.query_df(
                "SELECT code, close, float_mv FROM dws_feature WHERE date = ?", [latest]
            )
            for _, r in fdf.iterrows():
                fm, cl = r["float_mv"], r["close"]
                if fm is not None and cl and not pd.isna(fm) and float(cl) > 0:
                    shares[str(r["code"])] = float(fm) / float(cl)
        except Exception as exc:  # noqa: BLE001
            logger.warning("流通股本反推失败，本次换手率留空：%s", str(exc)[:100])

        rows = []
        for d in dates:
            day = dump[dump["date"] == d]
            for _, r in day.iterrows():
                code = str(r["code"])
                pre = prev_close.get(code)
                close = r["close_price"]
                pct = ((close / pre - 1) * 100) if (pre and close) else None
                vol = r["volume"]
                sh = shares.get(code)
                rows.append(
                    {
                        "code": code,
                        "date": d,
                        "open": r["open_price"],
                        "high": r["high_price"],
                        "low": r["low_price"],
                        "close": close,
                        "preclose": pre,
                        "volume": vol,
                        "amount": r["turnover"],
                        "turn": (vol / sh * 100) if (sh and vol is not None) else None,
                        "pct_chg": pct,
                        # 标记来源，便于日后追溯/必要时重建复权
                        "adjust": "dump",
                    }
                )
            # 相邻日之间用当日收盘价续接（当日内多日时保持链路一致）
            for _, r in day.iterrows():
                prev_close[str(r["code"])] = float(r["close_price"])

        out = pd.DataFrame(rows)
        n = self.storage.upsert_df(out, "dwd_daily_bar")
        logger.info("全市场导出补数完成：%d 个交易日，共 %d 行", len(dates), n)
        return {
            "days": len(dates),
            "rows": n,
            "dates": [str(x) for x in dates],
            "latest_before": str(latest),
        }

    # ---------------- 财务报表 ----------------
    def _statements(
        self, code: str, period: str, limit: int, include_cashflow: bool = False
    ) -> dict[str, list[dict[str, Any]]]:
        """拉取财报（默认 **2 次**请求：利润表 + 资产负债表）。

        **默认不取现金流量表**：现有基本面分只用
        ROE（利润表+资产负债表）、资产负债率（资产负债表）、
        营收/净利同比（利润表），`cash_flow_net` 只是存下来、没参与任何计算。
        而每少一张表就少 1/3 请求 —— 在 15 次/分钟的硬限流下，
        全市场回填从约 19 小时降到约 13 小时。等真的要上现金流因子再打开。

        另：`limit`（期数）**不影响耗时** —— 一次请求就能返回最多 limit 期，
        所以取 12 期（3 年）和取 8 期（2 年）的请求数完全相同。
        """
        ths = self._thscode(code)
        paths = [("income", PATH_INCOME), ("balance", PATH_BALANCE)]
        if include_cashflow:
            paths.append(("cashflow", PATH_CASHFLOW))
        out: dict[str, list[dict[str, Any]]] = {}
        for key, path in paths:
            data = self._get(path, {"thscode": ths, "period": period, "limit": limit})
            out[key] = data.get("item") or []
        return out

    @staticmethod
    def _finance_rows(code: str, stmts: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        """三表按报告期合并，并算出价值档需要的**比率型**指标。

        为什么在这里算比率而不是留给下游：
        比率（ROE、资产负债率、同比增速）才是可跨公司比较的量，
        原始金额受规模影响、无法直接用于选股；且同比需要用到上一期数据，
        在采集处一次算完最省事。

        ⚠️ 同比只在**同 fiscal_period** 之间计算（Q2 比 Q2）：
        A 股季报是累计口径（Q2=6个月、Q4=12个月），跨报告期比较会得出
        荒谬结论。同理，roe / revenue 等流量指标的**水平值**也不能跨报告期比，
        横向对比请统一取 annual（FY）。
        """
        now = datetime.now()
        income = {i.get("period_end_ms"): i for i in stmts.get("income") or []}
        balance = {i.get("period_end_ms"): i for i in stmts.get("balance") or []}
        cashflow = {i.get("period_end_ms"): i for i in stmts.get("cashflow") or []}

        rows = []
        for pe, inc in income.items():
            bal = balance.get(pe) or {}
            cf = cashflow.get(pe) or {}
            equity = bal.get("holder_equity_total")
            assets = bal.get("assets_total")
            parent_np = inc.get("parent_holder_net_profit")
            rows.append(
                {
                    "code": code,
                    "period_end": _ms_to_date(pe),
                    # 必须存**披露日**：报告期末（period_end）只是会计区间终点，
                    # 财报实际公开要晚 1~4 个月。回测时若按 period_end 取数，
                    # 等于用了当时还不存在的信息（前视偏差），会把价值策略的
                    # 历史业绩虚高到无法复现。选股必须用 report_date <= 当日 过滤。
                    "report_date": _ms_to_date(inc.get("report_date_ms")),
                    "fiscal_year": inc.get("fiscal_year"),
                    "fiscal_period": inc.get("fiscal_period"),
                    "revenue": inc.get("operating_income"),
                    "net_profit": inc.get("net_profit"),
                    "parent_net_profit": parent_np,
                    "eps": inc.get("basic_eps"),
                    "total_assets": assets,
                    "total_debt": bal.get("total_debt"),
                    "equity": equity,
                    "cash_flow_net": cf.get("act_cash_flow_net"),
                    "debt_ratio": (
                        100.0 * bal["total_debt"] / assets
                        if bal.get("total_debt") is not None and assets
                        else None
                    ),
                    "roe": (
                        100.0 * parent_np / equity
                        if parent_np is not None and equity
                        else None
                    ),
                    "updated_at": now,
                }
            )

        # 同比：同 fiscal_period 的上一财年数据（口径必须一致，否则比错）
        rows.sort(key=lambda r: (r["period_end"] or date.min), reverse=True)
        by_key = {(r["fiscal_year"], r["fiscal_period"]): r for r in rows}
        for r in rows:
            prev = by_key.get((r["fiscal_year"] - 1, r["fiscal_period"])) if r["fiscal_year"] else None
            r["revenue_yoy"] = _yoy(r["revenue"], prev["revenue"] if prev else None)
            r["profit_yoy"] = _yoy(r["parent_net_profit"], prev["parent_net_profit"] if prev else None)
        return rows

    def collect_financials(
        self,
        codes: list[str],
        period: str = "quarterly",
        limit: int = 12,
        progress_every: int = 50,
        include_cashflow: bool = False,
    ) -> int:
        """采集指定股票的财报（每只默认 **2 次**请求，见 `_statements`）。

        limit 默认 12 期（约 3 年）：**期数不影响请求数**（一次请求返回最多 limit 期），
        所以多取历史是免费的，还能让 2 年历史回测窗口全段都有财报可用。

        必须打进度日志：全市场回填是 10 小时级的任务，
        没有进度输出就等于黑盒，用户无法判断是正常在跑还是已经卡死。
        """
        started = time.monotonic()
        total = 0
        failed = 0
        for i, code in enumerate(codes, 1):
            try:
                stmts = self._statements(code, period, limit)
            except Exception as exc:  # noqa: BLE001
                # 单只失败不中断：全市场回填中个别股票无财报是常态
                failed += 1
                logger.warning("[%s] 财务采集失败，跳过：%s", code, str(exc)[:120])
            else:
                rows = self._finance_rows(code, stmts)
                if rows:
                    self.storage.upsert_df(pd.DataFrame(rows), "dws_finance_metrics")
                    total += len(rows)
            if i % progress_every == 0 or i == len(codes):
                rate = i / max(time.monotonic() - started, 1e-6)
                remain = (len(codes) - i) / max(rate, 1e-6) / 60
                logger.info(
                    "财务采集进度 %d/%d（%s）| 预计剩余 %.0f 分钟",
                    i, len(codes), code, remain,
                )
        logger.info(
            "财务采集完成：%d 只股票，共 %d 期报表，失败 %d 只",
            len(codes), total, failed,
        )
        return total

    def pending_finance_codes(self, boards: list[str] | None = None) -> list[str]:
        """尚未采集财务的股票（回填用，按 dim_stock 顺序）。

        默认只取**可交易板块**（与 `universe.boards` 一致）：科创板/北交所没有交易权限，
        采了也永远不会被选中，而它们占全市场约 11% ——
        在 15 次/分钟的硬限流下，那就是一小时以上的差别。
        """
        from astock.config import get_config

        if boards is None:
            boards = list(get_config().get("universe.boards", ["main", "gem"]) or [])

        sql = (
            "SELECT s.code FROM dim_stock s "
            "LEFT JOIN (SELECT DISTINCT code FROM dws_finance_metrics) f ON f.code = s.code "
            "WHERE f.code IS NULL"
        )
        # 排除**已退市**股票：实测 000003/000004/000005 这类老壳代码会让接口
        # 返回 code=1002 Unknown thscode，而每个失败请求照样要等 4.2 秒限流 ——
        # 在 11.5 小时的回填里那是几十分钟的纯浪费。
        # 退市股本来也永远不会被选中（universe 已排除），采了没有任何用途。
        sql += " AND s.out_date IS NULL"
        params: list = []
        if boards:
            placeholders = ", ".join("?" for _ in boards)
            sql += f" AND s.board IN ({placeholders})"
            params.extend(boards)
        df = self.storage.query_df(sql + " ORDER BY s.code", params)
        return df["code"].tolist() if not df.empty else []

    # ---------------- 集合竞价 ----------------
    @staticmethod
    def _thscode(code: str) -> str:
        """转为接口要求的带后缀格式（600519.SH）。"""
        from astock.data.sources.base import to_source_code

        return to_source_code(code, "tushare")

    # ---------------- 盘中行情快照 ----------------
    def _snapshot_batch(self, batch: list[str], depth: int = 0
                        ) -> tuple[list[dict[str, Any]], list[str], int]:
        """取一批快照，失败时**只丢弃真正无效的代码**，保住同批其余。

        为什么必须这么做：上游把一批 500 只当**整体**校验，一个它不认识的代码
        就让整批 500 只全部失败。实测 2026-10-08：因 *ST元道（301139）一只
        无法识别，一次丢掉 515 只 = 全市场 9.6%，且丢的是代码顺序相邻的一整批
        —— 这种缺失不报错、不中断任务，只会让当天数据**静默变少**。

        处置策略（按代价从低到高）：
          1. 报错点名了代码（`Unknown A-share thscode: 301139.SZ`）→ 剔除它重试，
             代价仅 +1 个请求；
          2. 报错没点名、但确属**业务错误**（上游拒绝这批）→ 二分拆半递归定位；
          3. 深度超限（防无限递归）或已到单只 → 丢弃它并记日志。

        ⚠️ **只有业务错误才做逐级定位**。网络/5xx/限流类异常必须原样放弃该批：
        否则一次网络抖动会触发二分递归，把 500 只拆成 ~500 次单只请求 ——
        既耗时数分钟，又必然撞上 15 次/分钟的限流，把一个瞬时故障放大成
        持续数分钟的自我封禁。

        返回 (items, 上游不认的代码, 因非业务错误放弃的只数)。
        """
        if not batch:
            return [], [], 0
        try:
            data = self._get(
                PATH_SNAPSHOT,
                {"thscodes": ",".join(self._thscode(c) for c in batch)},
            )
        except Exception as exc:  # noqa: BLE001 - 单批任何异常都不应中断整次采集
            msg = str(exc)
            bad_err = bool(_BAD_THSCODE_RE.search(msg))
            biz_err = bad_err or "业务错误" in msg
            if not biz_err:
                logger.warning(
                    "行情快照批次失败（网络/服务异常，放弃 %d 只，不拆批重试）：%s",
                    len(batch), msg[:140],
                )
                return [], [], len(batch)
            hit = _BAD_THSCODE_RE.search(msg)
            if hit and len(batch) > 1:
                bad = hit.group(1)
                rest = [c for c in batch if c != bad]
                if len(rest) < len(batch):
                    items, dropped, failed = self._snapshot_batch(rest, depth + 1)
                    return items, [bad, *dropped], failed
                # 点名的代码不在本批（理论不该发生）——退回二分，别误删好代码
                logger.warning("快照报错点名的 %s 不在本批，改用二分定位", bad)
            if len(batch) > 1 and depth < 12:
                mid = len(batch) // 2
                left, drop_l, fail_l = self._snapshot_batch(batch[:mid], depth + 1)
                right, drop_r, fail_r = self._snapshot_batch(batch[mid:], depth + 1)
                return left + right, drop_l + drop_r, fail_l + fail_r
            logger.warning("行情快照丢弃单只 %s：%s", batch[0], msg[:120])
            return [], [batch[0]], 0
        return list(data.get("item") or []), [], 0

    def snapshot(self, codes: list[str]) -> pd.DataFrame:
        """批量拉取实时行情快照（分批，每批 `SNAPSHOT_BATCH` 只）。

        返回上游原始字段：`ticker / last_price / prev_price / open_price /
        high_price / low_price / price_change_ratio_pct / volume / turnover`。
        字段归一化与派生（量比等）由调用方负责 —— 本层只保证"取到"。

        **单批失败不再整批丢弃**：改为逐级定位到真正无效的代码（见 `_snapshot_batch`）。
        整批丢弃的代价太大 —— 一个无效代码 = 500 只全丢（实测丢过 9.6% 的全市场），
        而快照是**每天一次性、不可回补**的数据。
        """
        uniq = [c for c in dict.fromkeys(codes) if c]
        if not uniq:
            return pd.DataFrame()

        frames: list[pd.DataFrame] = []
        dropped: list[str] = []
        failed = 0
        for i in range(0, len(uniq), SNAPSHOT_BATCH):
            batch = uniq[i : i + SNAPSHOT_BATCH]
            items, gone, fail = self._snapshot_batch(batch)
            dropped.extend(gone)
            failed += fail
            if items:
                frames.append(pd.DataFrame(items))

        if dropped:
            # 必须把**具体代码**打出来：只说"跳过 500 只"无法排查，
            # 而"哪些代码上游不认"本身是有价值的信息（可据此判断是退市、
            # 改名还是上游数据缺口）。
            logger.warning(
                "行情快照丢弃 %d 只上游不认的代码（占 %d 只的 %.2f%%）：%s",
                len(dropped), len(uniq), 100.0 * len(dropped) / len(uniq),
                "、".join(dropped[:20]) + ("…" if len(dropped) > 20 else ""),
            )
        if failed:
            logger.warning("行情快照因网络/服务异常放弃 %d 只（本次不重试，等下次采集）", failed)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def auction_snapshot(self, codes: list[str], stage: str = "final") -> list[dict[str, Any]]:
        """批量拉取集合竞价快照（单次上限 100 只，自动分批）。

        **为什么只有这一层**：落库链路（`dwd_auction` 表 + `_auction_rows` +
        `collect_auction` + `auction` 命令）已于 2026-09-30 作为零使用数据移除
        （该表自建库以来 0 行）。本方法作为**薄 API 包装**保留：将来若要做
        「竞价类影子信号」（解冻后路线里点名的方向），只需重写落库与展示两段，
        不必再对接接口。表定义见 git 历史中的 schema.sql。
        """
        uniq = [c for c in dict.fromkeys(codes) if c]
        if not uniq:
            return []
        items: list[dict[str, Any]] = []
        for i in range(0, len(uniq), 100):
            batch = uniq[i : i + 100]
            data = self._get(
                PATH_AUCTION,
                {"thscodes": ",".join(self._thscode(c) for c in batch), "stage": stage},
            )
            items.extend(data.get("item") or [])
        return items

    def collect_day(self, d: date) -> dict[str, int]:
        """采集某一天（2 次请求）。返回各表写入行数。"""
        up_items = self._fetch_pool(PATH_LIMIT_UP, d)
        up_rows = self._up_rows(d, up_items)
        if up_rows:
            self.storage.upsert_df(pd.DataFrame(up_rows), "dwd_limit_up")

        br_items = self._fetch_pool(PATH_LIMIT_BREAK, d)
        br_rows = self._break_rows(d, br_items)
        if br_rows:
            self.storage.upsert_df(pd.DataFrame(br_rows), "dwd_limit_break")

        logger.info(
            "[%s] 涨停 %d 只（已入库 %d），炸板 %d 只（已入库 %d）",
            d, len(up_items), len(up_rows), len(br_items), len(br_rows),
        )
        return {"limit_up": len(up_rows), "limit_break": len(br_rows)}

    def pending_dates(self, years: int | None = None, end: date | None = None) -> list[date]:
        """返回尚未采集的交易日（按交易日历，跳过周末/节假日）。"""
        from astock.data.calendar import TradeCalendar

        cal = TradeCalendar(self.storage)
        end = end or date.today()
        last = self.storage.query_value(
            "SELECT MAX(date) FROM dwd_limit_up"
        )
        if years is not None:
            start = end - timedelta(days=int(365.25 * years))
        elif last is not None:
            start = last + timedelta(days=1)
        else:
            start = end - timedelta(days=int(365.25 * 1))
        if start > end:
            return []
        return cal.open_dates(start, end)

    def sync(
        self,
        dates: list[date] | None = None,
        years: int | None = None,
        progress_every: int = 20,
    ) -> dict[str, Any]:
        """采集一个或多个交易日。

        dates 为空时：从「上次采集的最新日期」起补齐到今天（增量）；
        years 不为空时：回填最近 N 年（首次回填用，耗时约 2 请求/天 × 4.2 秒）。
        """
        from astock.data.calendar import TradeCalendar

        cal = TradeCalendar(self.storage)
        cal.sync()
        if dates is None:
            dates = self.pending_dates(years=years)
        dates = sorted(set(dates))
        if not dates:
            logger.info("涨停/炸板池已是最新，无需采集")
            return {"days": 0, "limit_up": 0, "limit_break": 0, "elapsed_sec": 0.0}

        started = time.monotonic()
        total = {"limit_up": 0, "limit_break": 0}
        for i, d in enumerate(dates, 1):
            stats = self.collect_day(d)
            total["limit_up"] += stats["limit_up"]
            total["limit_break"] += stats["limit_break"]
            if i % progress_every == 0 or i == len(dates):
                rate = i / max(time.monotonic() - started, 1e-6)
                remain = (len(dates) - i) / max(rate, 1e-6) / 60
                logger.info(
                    "涨停池采集进度 %d/%d（%s）| 预计剩余 %.1f 分钟",
                    i, len(dates), d, remain,
                )
        elapsed = round(time.monotonic() - started, 1)
        return {
            "days": len(dates),
            **total,
            "elapsed_sec": elapsed,
            "start": str(dates[0]),
            "end": str(dates[-1]),
        } 
