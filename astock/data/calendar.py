# -*- coding: utf-8 -*-
"""交易日历：判断交易日、定位最新交易日、生成缺失区间。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pandas as pd

from astock.config import get_config
from astock.logger import get_logger
from astock.storage.db import Storage, get_storage

logger = get_logger("data.calendar")

# 当日数据未完整时的提示只打印一次，避免每个调用点都刷一行
_READY_HINT_PRINTED = False


class TradeCalendar:
    """交易日历服务，数据来自 baostock 并落库缓存。"""

    def __init__(self, storage: Storage | None = None) -> None:
        self.storage = storage or get_storage()

    # ---------------- 同步 ----------------
    def sync(self, force: bool = False, source=None) -> int:
        """同步交易日历（默认只补齐缺失部分）。

        source：可复用调用方已登录的数据源实例。
        注意 baostock 使用模块级全局 socket，多个实例各自 login/logout 会互相破坏连接，
        因此这里优先复用外部传入的同一实例。
        """
        today = date.today()
        max_cached = self.storage.query_value("SELECT MAX(date) FROM trade_calendar")

        if not force and max_cached is not None and max_cached >= today:
            return 0

        start = date(2015, 1, 1) if (force or max_cached is None) else max_cached + timedelta(days=1)
        end = date(today.year + 1, 12, 31)

        if source is not None:
            df = source.list_trade_dates(start, end)
        else:
            from astock.data.sources import get_source

            src = get_source(get_config().get("data.primary_source", "baostock"))
            with src:
                df = src.list_trade_dates(start, end)

        if df.empty:
            logger.warning("交易日历同步为空")
            return 0

        self.storage.upsert_df(df, "trade_calendar")
        logger.info("交易日历同步完成：%s ~ %s，共 %d 行", start, end, len(df))
        return len(df)

    # ---------------- 查询 ----------------
    def is_open(self, d: date | str) -> bool:
        ts = pd.to_datetime(d)
        val = self.storage.query_value(
            "SELECT is_open FROM trade_calendar WHERE date = ?", [ts.date()]
        )
        if val is None:
            # 日历未覆盖时按工作日兜底
            return ts.weekday() < 5
        return bool(val)

    # ---------------- 数据完整性守卫 ----------------
    def data_ready_time(self) -> time:
        """当日行情视为「完整可用」的时间点。

        收盘 15:00 之后数据源仍需时间更新，因此默认留 1 小时缓冲。
        """
        raw = str(get_config().get("data.data_ready_time", "16:00") or "16:00")
        try:
            hh, mm = raw.split(":")[:2]
            return time(int(hh), int(mm))
        except (ValueError, AttributeError):
            return time(16, 0)

    def is_data_ready(self, d: date, now: datetime | None = None) -> bool:
        """判断某交易日的数据是否已完整。"""
        now = now or datetime.now()
        if d < now.date():
            return True
        if d > now.date():
            return False
        return now.time() >= self.data_ready_time()

    def resolve_data_date(self, requested: date | None = None) -> tuple[date | None, date | None]:
        """返回 (可用于计算的数据日, 被跳过的当日)。

        为什么需要这个守卫：
        盘中运行 `daily` / `backfill` 会把**当天尚未收盘的 K 线**当成完整日线写入
        `dwd_daily_bar`。一旦写入，该行就成为「本地最新日期」游标，
        后续补数会因为 `latest >= target` 直接跳过，**错误数据被永久保留**，
        并且因子、市场状态、选股结果全部建立在半根 K 线上。

        因此这里统一把「当天数据未就绪」的情况回退到上一个交易日，
        并明确告诉用户跳过了哪一天、为什么。
        """
        global _READY_HINT_PRINTED
        today = date.today()
        if requested is not None:
            return self.latest_open_date(not_after=requested, require_closed=False), None

        if not self.is_data_ready(today):
            skipped = today if self.is_open(today) else None
            if skipped is not None and not _READY_HINT_PRINTED:
                _READY_HINT_PRINTED = True
                logger.warning(
                    "当日（%s）行情尚未完整，本次只处理到上一个交易日。"
                    "原因：当前 %s 早于数据就绪时间 %s。"
                    "若需要当日数据，请在 %s 之后重跑。",
                    skipped,
                    datetime.now().strftime("%H:%M"),
                    self.data_ready_time().strftime("%H:%M"),
                    self.data_ready_time().strftime("%H:%M"),
                )
            return self.latest_open_date(require_closed=True), skipped
        return self.latest_open_date(require_closed=True), None

    def latest_open_date(
        self, not_after: date | None = None, require_closed: bool = True
    ) -> date | None:
        """不晚于指定日期的最近一个交易日（默认今天）。

        require_closed=True（默认）时，若当天数据尚未完整则回退到上一交易日 ——
        这是**数据安全的默认值**，理由见 `resolve_data_date` 的说明。
        只有确实需要「今天」这个日期本身（而不是它的数据）时才传 False。
        """
        bound = not_after or date.today()
        if require_closed and not_after is None and not self.is_data_ready(bound):
            bound = bound - timedelta(days=1)
        return self.storage.query_value(
            "SELECT MAX(date) FROM trade_calendar WHERE is_open = TRUE AND date <= ?", [bound]
        )

    def open_dates(self, start: date, end: date) -> list[date]:
        df = self.storage.query_df(
            "SELECT date FROM trade_calendar WHERE is_open = TRUE AND date BETWEEN ? AND ? ORDER BY date",
            [start, end],
        )
        return [d.date() if hasattr(d, "date") else d for d in df["date"].tolist()]

    def missing_open_dates(self, table: str = "dwd_daily_bar") -> list[date]:
        """返回本地数据缺失的交易日列表（用于「不保证开机」场景自动补数）。"""
        latest = self.storage.latest_trade_date(table)
        target = self.latest_open_date()
        if target is None:
            return []
        if latest is None:
            return []
        if latest >= target:
            return []
        return self.open_dates(latest + timedelta(days=1), target)
