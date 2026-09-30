# -*- coding: utf-8 -*-
"""akshare 数据源（兜底源）。

当 baostock 某只股票或某个交易日数据缺失时使用，也用于交易日历。
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from astock.data.sources.base import (
    BaseSource,
    board_of,
    clean_daily,
    exchange_of,
    is_st_name,
    normalize_code,
    to_source_code,
)
from astock.logger import get_logger

logger = get_logger("data.akshare")


def _compact(d: date | str | None) -> str:
    if d is None:
        return ""
    if isinstance(d, str):
        return d[:10].replace("-", "")
    return d.strftime("%Y%m%d")


class AkshareSource(BaseSource):
    """akshare 数据源实现（兜底）。"""

    name = "akshare"

    def __init__(self) -> None:
        self._ak = None

    def open(self) -> None:
        try:
            import akshare as ak
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("未安装 akshare，请执行 scripts/setup_env.bat") from exc
        self._ak = ak

    # ---------------- 基础信息 ----------------
    def list_stocks(self) -> pd.DataFrame:
        df = self._ak.stock_info_a_code_name()
        if df is None or df.empty:
            return pd.DataFrame()

        rows = []
        for _, r in df.iterrows():
            code = normalize_code(r.get("code", ""))
            if not code:
                continue
            name = r.get("name") or ""
            rows.append(
                {
                    "code": code,
                    "name": name,
                    "exchange": exchange_of(code),
                    "board": board_of(code),
                    "ipo_date": pd.NaT,
                    "out_date": pd.NaT,
                    "status": 1,
                    "is_st": is_st_name(name),
                }
            )
        out = pd.DataFrame(rows)
        if not out.empty:
            out["updated_at"] = pd.Timestamp.now()
        logger.info("akshare 股票列表获取完成，共 %d 只", len(out))
        return out

    def list_trade_dates(self, start: date | str, end: date | str) -> pd.DataFrame:
        df = self._ak.tool_trade_date_hist_sina()
        if df is None or df.empty:
            return pd.DataFrame(columns=["date", "is_open"])

        col = "trade_date" if "trade_date" in df.columns else df.columns[0]
        out = pd.DataFrame({"date": pd.to_datetime(df[col], errors="coerce"), "is_open": True})
        out = out.dropna(subset=["date"])
        if start is not None:
            out = out[out["date"] >= pd.to_datetime(start)]
        if end is not None:
            out = out[out["date"] <= pd.to_datetime(end)]
        return out.reset_index(drop=True)

    # ---------------- 行情 ----------------
    def fetch_daily(self, code: str, start: date | str, end: date | str) -> pd.DataFrame:
        """多站点自动降级获取日线。

        依次尝试新浪 → 腾讯 → 东财。东财数据最全但最容易限流（实测会被
        直接断连），新浪最稳且快（实测约 0.5 秒/只、可拉满 2 年）。
        """
        attempts = (
            ("新浪", self._daily_sina),
            ("腾讯", self._daily_tx),
            ("东财", self._daily_em),
        )
        last_error: Exception | None = None
        for name, fetcher in attempts:
            try:
                out = fetcher(code, start, end)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("[%s] %s 源失败：%s", code, name, str(exc)[:80])
                continue
            if out is not None and not out.empty:
                return out

        if last_error is not None:
            logger.warning("[%s] 全部 akshare 通道均失败", code)
        return clean_daily(pd.DataFrame())

    def _daily_sina(self, code: str, start: date | str, end: date | str) -> pd.DataFrame:
        """新浪源：stock_zh_a_daily。turnover 为「小数比例」而非百分数。"""
        code = normalize_code(code)
        symbol = f"{exchange_of(code)}{code}"
        df = self._ak.stock_zh_a_daily(
            symbol=symbol, start_date=_compact(start), end_date=_compact(end), adjust="qfq"
        )
        if df is None or df.empty:
            return clean_daily(pd.DataFrame())
        out = df.copy()
        # 新浪 turnover 为比例（0.00196 = 0.196%），统一乘以 100
        if "turnover" in out.columns:
            out["turn"] = pd.to_numeric(out["turnover"], errors="coerce") * 100
        return self._finalize(out, code)

    def _daily_tx(self, code: str, start: date | str, end: date | str) -> pd.DataFrame:
        """腾讯源：stock_zh_a_hist_tx。"""
        code = normalize_code(code)
        symbol = f"{exchange_of(code)}{code}"
        df = self._ak.stock_zh_a_hist_tx(
            symbol=symbol, start_date=_compact(start), end_date=_compact(end), adjust="qfq"
        )
        if df is None or df.empty:
            return clean_daily(pd.DataFrame())
        out = df.copy()
        if "turnover" in out.columns:
            out["turn"] = pd.to_numeric(out["turnover"], errors="coerce") * 100
        return self._finalize(out, code)

    def _daily_em(self, code: str, start: date | str, end: date | str) -> pd.DataFrame:
        """东财源：stock_zh_a_hist（字段最全，但限流时不可用）。"""
        code = normalize_code(code)
        df = self._ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=_compact(start),
            end_date=_compact(end),
            adjust="qfq",
        )
        if df is None or df.empty:
            return clean_daily(pd.DataFrame())

        out = df.rename(
            columns={
                "日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
                "最低": "low", "成交量": "volume", "成交额": "amount",
                "涨跌幅": "pct_chg", "换手率": "turn",
            }
        )
        if "volume" in out.columns:
            out["volume"] = pd.to_numeric(out["volume"], errors="coerce") * 100  # 手 → 股
        return self._finalize(out, code)

    def _finalize(self, df: pd.DataFrame, code: str) -> pd.DataFrame:
        """统一收尾：补 code、补 preclose/pct_chg、清洗。

        新浪与腾讯都不返回昨收，必须由前一行收盘价推算 —— 否则因子层的
        涨停判定与市场状态的涨跌家数都无法计算。
        """
        out = df.copy()
        out["code"] = normalize_code(code)
        if "date" in out.columns:
            out["date"] = pd.to_datetime(out["date"], errors="coerce")
            out = out.sort_values("date")

        if "preclose" not in out.columns or out["preclose"].isna().all():
            out["preclose"] = pd.to_numeric(out["close"], errors="coerce").shift(1)
        if "pct_chg" not in out.columns or out["pct_chg"].isna().all():
            pre = pd.to_numeric(out["preclose"], errors="coerce")
            close = pd.to_numeric(out["close"], errors="coerce")
            out["pct_chg"] = (close / pre.replace(0, pd.NA) - 1) * 100
        return clean_daily(out)

    def fetch_index_daily(self, index_code: str, start: date | str, end: date | str) -> pd.DataFrame:
        # akshare 指数代码格式如 sh000001
        symbol = index_code.replace(".", "")
        df = None
        # 优先新浪源：东财接口在限流时不可用，新浪更稳
        for fetcher in (
            lambda: self._ak.stock_zh_index_daily(symbol=symbol),
            lambda: self._ak.stock_zh_index_daily_em(symbol=symbol),
        ):
            try:
                candidate = fetcher()
                if candidate is not None and not candidate.empty:
                    df = candidate
                    break
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] 指数源不可用: %s", index_code, str(exc)[:80])
        if df is None or df.empty:
            return pd.DataFrame()

        out = df.rename(columns={"date": "date", "open": "open", "close": "close",
                                 "high": "high", "low": "low", "volume": "volume",
                                 "amount": "amount"})
        out["date"] = pd.to_datetime(out["date"], errors="coerce")
        out["pct_chg"] = out["close"].pct_change() * 100
        out["code"] = index_code
        mask = pd.Series(True, index=out.index)
        if start is not None:
            mask &= out["date"] >= pd.to_datetime(start)
        if end is not None:
            mask &= out["date"] <= pd.to_datetime(end)
        return out[mask].reset_index(drop=True)

    # ---------------- 快照 ----------------
    def fetch_spot(self) -> pd.DataFrame:
        """全市场实时快照（用于当日数据兜底）。"""
        df = self._ak.stock_zh_a_spot_em()
        return df if df is not None else pd.DataFrame()


_ = to_source_code  # 保持导入一致性，供未来扩展使用
