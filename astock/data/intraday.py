# -*- coding: utf-8 -*-
"""盘中快照采集（`dwd_intraday_snapshot`）。

------------------------------------------------------------------
数据来源：同花顺 `/api/a-share/prices/snapshot`
------------------------------------------------------------------
**为什么从 akshare 换成同花顺**：原实现走 akshare 的 `stock_zh_a_spot_em()`
（东方财富接口），实测在当前网络下稳定报 `RemoteDisconnected`，拿不到数据。
同花顺接口实测连通，且单批 500 只可用（全市场约 11 批、40 余秒）。

⚠️ 换源时踩过并必须避开的坑：**只能传存续股票**。混入已退市代码会让整批失败，
返回 `code=1002 / Unknown thscode: xxx` —— 这个报错看起来像"批量超限"，
实测正是它造成过一次错误结论（"批量 5 只就失败"），真实原因是请求里混进了
已退市的 000003.SZ。所以这里统一从 `dim_stock` 取 `out_date IS NULL` 的代码。

------------------------------------------------------------------
为什么必须另建一条链路，而不是改 daily 的执行时间
------------------------------------------------------------------
`daily` 依赖 `calendar.is_data_ready(今天)`，而 `data_ready_time = 16:00`。
14:00 运行时该守卫会**主动回退到上一交易日**，产出「昨天的数据日 + 今天的
计划买入日」——而今天早已开盘，报告里的计划既无数据也不可执行。

------------------------------------------------------------------
为什么必须落库
------------------------------------------------------------------
免费源不提供历史盘中数据。不从现在开始记录，就永远无法回测盘中决策 ——
这是本模块唯一有时效性的部分：晚一天开始，就少一天样本。

------------------------------------------------------------------
为什么量比要自己算（本接口不提供）
------------------------------------------------------------------
`prices/snapshot` 只给「当日累计成交量」，没有量比。14:00 时它只累积了约
3.5 小时，**直接与前 5 日全日均量比较会系统性偏小**（看着像缩量，其实只是
时间没走完）。因此按已开盘分钟数折算：

    量比 = 当日累计量 / (前 5 日全日均量 × 已开盘分钟数 / 240)

A 股全天 240 分钟（09:30–11:30 + 13:00–15:00），**必须处理午休**，
否则 12:00 会算出 150/240 的虚高比例。

⚠️ **已知偏差（实测，不要当成精确量比）**：上式假设成交量在时间上均匀分布，
但 A 股实际呈 **U 形**——早盘与尾盘重、盘中轻。实测 10:50 采样的全市场量比
**均值为 1.61**（若折算正确应接近 1.0），说明线性折算在早盘**高估约 60%**。
偏差随时间推移收敛：14:00 已走完 87.5% 的时间、而累计成交量约占全天的
93~95%，高估仅约 6%。

⇒ 处置：
  ① 阈值（`shadow_gene.vol_ratio_max`）**必须按 14:00 这个时点校准**，
     不能拿收盘口径的 0.7 直接套用；
  ② 待积累足够快照后，用「同一只股票在 14:00 的累计量 / 当日全日量」的
     实际分布替换线性折算（数据落库后即可标定，无需再猜）。
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime

import pandas as pd

from astock.config import get_config
from astock.logger import get_logger
from astock.storage.db import Storage, get_storage

logger = get_logger("data.intraday")

# 同花顺 prices/snapshot → 本表字段
SNAPSHOT_FIELD_MAP = {
    "ticker": "code",
    "last_price": "price",
    "prev_price": "preclose",
    "open_price": "open",
    "high_price": "high",
    "low_price": "low",
    "price_change_ratio_pct": "pct_chg",
    "volume": "volume",
    "turnover": "amount",
}

TABLE_COLUMNS = [
    "date", "slot", "code", "name", "price", "preclose", "open", "high", "low",
    "pct_chg", "volume", "amount", "turnover_rate", "volume_ratio",
    "float_mv", "total_mv", "speed", "pct_5min", "captured_at", "source",
]

TOTAL_MINUTES = 240          # 09:30–11:30 + 13:00–15:00
AM_OPEN, AM_CLOSE = 9 * 60 + 30, 11 * 60 + 30
PM_OPEN, PM_CLOSE = 13 * 60, 15 * 60


def elapsed_minutes(t: datetime) -> int:
    """当日已开盘分钟数（0~240）。**必须扣掉午休**，否则 11:30–13:00 之间
    会算出比实际大的比例，把"缩量"判成"放量"。"""
    m = t.hour * 60 + t.minute
    if m <= AM_OPEN:
        return 0
    if m <= AM_CLOSE:
        return m - AM_OPEN
    if m <= PM_OPEN:
        return AM_CLOSE - AM_OPEN
    if m <= PM_CLOSE:
        return (AM_CLOSE - AM_OPEN) + (m - PM_OPEN)
    return TOTAL_MINUTES


class IntradaySnapshotCollector:
    """采集全市场盘中快照并落库。"""

    def __init__(self, storage: Storage | None = None, source: str = "hithink") -> None:
        self.cfg = get_config()
        self.storage = storage or get_storage()
        self.source_name = source
        self._hk = None

    def close(self) -> None:
        if self._hk is not None:
            close = getattr(self._hk, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
            self._hk = None

    def _collector(self):
        if self._hk is None:
            from astock.data.hithink import HithinkCollector

            self._hk = HithinkCollector(storage=self.storage)
        return self._hk

    # ---------------- 主流程 ----------------
    def collect(self, slot: str | None = None, trade_date: date_cls | None = None,
                codes: list[str] | None = None) -> int:
        """抓一次快照并写入 `dwd_intraday_snapshot`，返回写入行数。

        同一 (date, slot) 重跑会**覆盖**而非追加，补跑/重试不会留下重复样本。
        """
        now = datetime.now()
        slot = slot or now.strftime("%H:%M")
        # slot 是人工/脚本给的标签，可能与实际采集时刻不符（实测发生过：
        # 传 --slot 14:00 却在 10:50 采集，数据被标成 14:00 的横截面，
        # 而盘中量比是随时间衰减的，标错时点会让后续校准全盘失真）。
        try:
            hh, mm = (int(x) for x in slot.split(":")[:2])
            if abs((hh * 60 + mm) - (now.hour * 60 + now.minute)) > 30:
                logger.warning(
                    "slot 标签(%s)与实际采集时刻(%s)相差超过 30 分钟 —— "
                    "盘中量比随时间变化，标错时点会使该批数据不可用",
                    slot, now.strftime("%H:%M"),
                )
        except (ValueError, TypeError):
            logger.warning("slot 标签格式异常：%s（应为 HH:MM）", slot)

        trade_date = trade_date or self._today_if_trading(now)
        if trade_date is None:
            logger.info("今天不是交易日，跳过快照采集")
            return 0

        codes = codes or self._active_codes()
        if not codes:
            logger.warning("股票池为空，跳过快照采集")
            return 0

        try:
            raw = self._collector().snapshot(codes)
        except Exception as exc:  # noqa: BLE001
            logger.warning("行情快照获取失败（本次跳过，不影响其它步骤）：%s", str(exc)[:150])
            return 0
        if raw is None or raw.empty:
            logger.warning("行情快照为空（可能是非交易时段或上游无数据）")
            return 0

        df = self._normalize(raw, trade_date, slot, now)
        if df.empty:
            return 0

        n = self.storage.upsert_df(df, "dwd_intraday_snapshot")
        logger.info("盘中快照已落库：%s slot=%s 共 %d 只（时刻 %s，已开盘 %d 分钟）",
                    trade_date, slot, n, now.strftime("%H:%M:%S"),
                    elapsed_minutes(now))
        return n

    # ---------------- 内部 ----------------
    @staticmethod
    def _today_if_trading(now: datetime) -> date_cls | None:
        """今天若是交易日则返回今天，否则 None。

        **不能用 `calendar.resolve_data_date`**：它有 `data_ready_time=16:00`
        守卫，14:00 时会回退到上一交易日 —— 那正是本模块要绕开的行为。
        这里只问"今天开不开市"。
        """
        from astock.data.calendar import TradeCalendar

        today = now.date()
        return today if TradeCalendar().is_open(today) else None

    def _active_codes(self) -> list[str]:
        """存续股票代码。**已退市代码必须排除**，否则整批请求会被上游拒绝
        （见模块注释里的 1002 坑）。"""
        df = self.storage.query_df(
            "SELECT code FROM dim_stock WHERE out_date IS NULL ORDER BY code"
        )
        return df["code"].astype(str).tolist() if not df.empty else []

    def _normalize(self, raw: pd.DataFrame, trade_date: date_cls,
                   slot: str, now: datetime) -> pd.DataFrame:
        renamed = raw.rename(columns=SNAPSHOT_FIELD_MAP)
        keep = [c for c in SNAPSHOT_FIELD_MAP.values() if c in renamed.columns]
        if "code" not in keep:
            logger.warning("行情快照缺少代码列，无法落库（上游字段可能变更）：%s",
                           list(raw.columns)[:12])
            return pd.DataFrame()
        out = renamed[keep].copy()
        out["code"] = out["code"].astype(str).str.strip().str.zfill(6)

        for col in SNAPSHOT_FIELD_MAP.values():
            if col != "code" and col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce")
        # 停牌股无有效报价，留着会污染"全市场等权"基准
        out = out[out["price"].notna() & (out["price"] > 0)]

        mins = elapsed_minutes(now)
        avg_vol = self._avg_volume_5d(trade_date)
        if avg_vol.empty:
            logger.warning("缺少前 5 日均量，本次量比留空（不猜）")
            out["volume_ratio"] = pd.NA
        elif mins <= 0:
            logger.info("尚未开盘（已开盘 %d 分钟），量比留空", mins)
            out["volume_ratio"] = pd.NA
        else:
            expected = out["code"].map(avg_vol) * (mins / TOTAL_MINUTES)
            out["volume_ratio"] = (out["volume"] / expected).replace([float("inf")], pd.NA)

        # 本地补 name / float_mv（上游快照不含）
        meta = self._meta(trade_date)
        if not meta.empty:
            out = out.merge(meta, on="code", how="left")
        else:
            out["name"] = None
            out["float_mv"] = None

        out["date"] = trade_date
        out["slot"] = slot
        out["captured_at"] = now
        out["source"] = self.source_name
        # 上游不提供：换手率 / 总市值 / 涨速 / 5分钟涨跌。留 NULL 而不是填 0 ——
        # 0 会被下游当成"真实测得的零"，造成误判。
        for col in ("turnover_rate", "total_mv", "speed", "pct_5min"):
            out[col] = pd.NA
        return out[TABLE_COLUMNS].reset_index(drop=True)

    def _avg_volume_5d(self, trade_date: date_cls) -> pd.Series:
        """前 5 个交易日的全日均量，索引为 code。"""
        df = self.storage.query_df(
            """
            WITH d AS (
                SELECT DISTINCT date FROM dwd_daily_bar
                WHERE date < ? ORDER BY date DESC LIMIT 5
            )
            SELECT code, AVG(volume) AS avg_vol FROM dwd_daily_bar
            WHERE date IN (SELECT date FROM d) AND volume > 0
            GROUP BY code
            """,
            [trade_date],
        )
        if df.empty:
            return pd.Series(dtype="float64")
        return df.set_index("code")["avg_vol"]

    def _meta(self, trade_date: date_cls) -> pd.DataFrame:
        """name（dim_stock）+ float_mv（**由日线现算**）。

        2026-10-08：不再读因子宽表 dws_feature（该表随主链路删除）。
        流通市值 = 流通股本 × 收盘价，其中流通股本由换手率反推：
            float_mv = close × (volume×100 / turn)
        与 FeatureBuilder 原来用的公式**逐字相同**，故数值不变。
        """
        prev = self.storage.query_value(
            "SELECT MAX(date) FROM dwd_daily_bar WHERE date < ?", [trade_date]
        )
        if prev is None:
            return self.storage.query_df("SELECT code, name, NULL AS float_mv FROM dim_stock")
        return self.storage.query_df(
            "SELECT s.code, s.name, "
            "       b.close * (b.volume * 100 / NULLIF(b.turn, 0)) AS float_mv "
            "FROM dim_stock s "
            "LEFT JOIN dwd_daily_bar b ON b.code = s.code AND b.date = ?",
            [prev],
        )
