# -*- coding: utf-8 -*-
"""数据源抽象层：定义统一接口与代码规范化工具。

任何数据源（baostock / adata / akshare）实现这一接口后即可被采集器无差别调用。
这是「多源融合 + 兜底切换」的实现基础。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

import pandas as pd

# ---------- 代码与板块工具 ----------


def normalize_code(raw: str) -> str:
    """把各种格式的代码统一为 6 位数字字符串。

    支持 'sh.600000' / '600000.SH' / '600000' / 600000。
    """
    s = str(raw).strip().upper()
    for sep in (".", "_"):
        if sep in s:
            a, b = s.split(sep, 1)
            s = b if a in ("SH", "SZ", "BJ") else a
    s = "".join(ch for ch in s if ch.isdigit())
    return s.zfill(6) if len(s) < 6 else s


def exchange_of(code: str) -> str:
    """判断交易所：sh / sz / bj。"""
    code = normalize_code(code)
    if code.startswith(("600", "601", "603", "605", "688", "689", "900")):
        return "sh"
    if code.startswith(("000", "001", "002", "003", "300", "301", "200")):
        return "sz"
    if code.startswith(("4", "8", "920")):
        return "bj"
    # 兜底：6/9 开头归上交所，其余归深交所
    return "sh" if code.startswith(("6", "9")) else "sz"


def board_of(code: str) -> str:
    """判断板块：main(主板) / gem(创业板) / star(科创板) / bj(北交所)。"""
    code = normalize_code(code)
    if code.startswith(("688", "689")):
        return "star"
    if code.startswith(("300", "301")):
        return "gem"
    if code.startswith(("4", "8", "920")):
        return "bj"
    return "main"


def is_st_name(name: str | None) -> bool:
    """根据名称判断是否 ST / *ST / 退市整理。"""
    if not name:
        return False
    upper = str(name).upper().replace(" ", "")
    return "ST" in upper or "退" in upper


def to_source_code(code: str, source: str = "baostock") -> str:
    """转换为数据源专用代码格式。"""
    code = normalize_code(code)
    if source == "baostock":
        return f"{exchange_of(code)}.{code}"
    if source == "tushare":
        return f"{code}.{exchange_of(code).upper()}"
    return code


class BaseSource(ABC):
    """数据源抽象基类。"""

    name: str = "base"

    # ---------------- 基础信息 ----------------
    @abstractmethod
    def list_stocks(self) -> pd.DataFrame:
        """返回股票列表。

        列：code, name, exchange, board, ipo_date, out_date, status
        """

    @abstractmethod
    def list_trade_dates(self, start: date | str, end: date | str) -> pd.DataFrame:
        """返回交易日历。列：date, is_open"""

    # ---------------- 行情 ----------------
    @abstractmethod
    def fetch_daily(self, code: str, start: date | str, end: date | str) -> pd.DataFrame:
        """返回单只股票日 K。

        列：code, date, open, high, low, close, preclose, volume, amount, turn, pct_chg
        """

    def fetch_index_daily(self, index_code: str, start: date | str, end: date | str) -> pd.DataFrame:
        """返回指数日线。默认不支持，子类可覆盖。"""
        raise NotImplementedError(f"{self.name} 不支持指数行情")

    # ---------------- 生命周期 ----------------
    def open(self) -> None:
        """建立连接（如需要登录）。"""

    def close(self) -> None:
        """释放连接。"""

    def __enter__(self) -> "BaseSource":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ---------- 通用清洗工具 ----------

DAILY_COLUMNS = [
    "code",
    "date",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "amount",
    "turn",
    "pct_chg",
]


def clean_daily(df: pd.DataFrame) -> pd.DataFrame:
    """统一清洗日 K：类型转换、去空、按日期排序。"""
    if df is None or df.empty:
        return pd.DataFrame(columns=DAILY_COLUMNS)

    out = df.copy()
    for col in DAILY_COLUMNS:
        if col not in out.columns:
            out[col] = pd.NA

    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    for col in ("open", "high", "low", "close", "preclose", "volume", "amount", "turn", "pct_chg"):
        out[col] = pd.to_numeric(out[col], errors="coerce")

    out = out.dropna(subset=["date", "close"])
    out = out[out["close"] > 0]
    out = out.sort_values("date").drop_duplicates(subset=["code", "date"], keep="last")
    return out[DAILY_COLUMNS].reset_index(drop=True)
