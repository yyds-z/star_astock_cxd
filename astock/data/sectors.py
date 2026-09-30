# -*- coding: utf-8 -*-
"""行业/板块维度采集。

设计取舍（实测得出的结论，不是拍脑袋）：

**只采「个股 → 行业」映射，板块强度由本地日线自算。**
免费板块接口的实测情况：
- 东财系（`stock_board_industry_name_em` / `_cons_em`）：被拦截，直接断连；
- 申万（`sw_index_third_cons`）：时好时坏 —— 首次跑通 13/31 个行业，几分钟后
  连 `sw_index_first_info` 都抛 AttributeError；
- 新浪（`stock_sector_spot` + `stock_sector_detail`）：稳定且快（全量约 15 秒），
  但只覆盖约 48.9% 的股票（**不含近年的新股**）；
- 同花顺：只有行业列表，本机 akshare 版本没有成分股接口。

接口快照还有两个致命局限：**只有当日、没有历史**，因此无法回测
「板块效应是否真的有效」。所以：

| 做什么 | 谁来做 | 为什么 |
|---|---|---|
| 个股 → 行业 映射 | 接口（多源互补） | 相对静态的维度数据，接口足够 |
| 板块日度强度 | 本地日线自算 | 可回填历史、可回测、不受接口稳定性影响 |

容错原则：**任一行业、任一来源失败都只记警告，绝不让整体失败**。
覆盖率不足时，未映射的股票只是没有板块标签，不会从选股池消失。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

import pandas as pd

from astock.config import get_config
from astock.data.sources.base import normalize_code
from astock.logger import get_logger
from astock.storage.db import Storage, get_storage

logger = get_logger("data.sectors")


class SectorCollector:
    """行业映射采集器。"""

    def __init__(self, storage: Storage | None = None) -> None:
        self.cfg = get_config()
        self.storage = storage or get_storage()
        self.sources: list[str] = list(
            self.cfg.get("sector.sources", ["sina", "sw"]) or ["sina"]
        )
        self.sleep = float(self.cfg.get("sector.request_sleep", 0.1) or 0.1)
        self.retry = int(self.cfg.get("sector.retry", 2) or 2)
        # 主用标签来源：板块强度与报告展示都用它，另一来源仅作后备
        self.primary_source = str(self.cfg.get("sector.primary_source", "sw") or "sw")

    def _is_primary(self, source: str) -> bool:
        return source == self.primary_source

    def refresh_primary_flags(self) -> int:
        """按当前配置重算 is_primary。

        `is_primary` 是由 `sector.primary_source` 派生的。换了主用来源时
        不该重新拉取全部数据（要 3 分钟），一条 UPDATE 就够。
        """
        self.storage.execute(
            "UPDATE dim_stock_industry SET is_primary = (source = ?)",
            [self.primary_source],
        )
        n = int(
            self.storage.query_value(
                "SELECT COUNT(*) FROM dim_stock_industry WHERE is_primary", default=0
            )
            or 0
        )
        logger.info("主用标签已切换为 %s：%d 条", self.primary_source, n)
        return n

    # ---------------- 通用重试 ----------------
    def _call(self, fn: Callable[[], Any], desc: str) -> Any:
        """带重试调用；全部失败返回 None（由调用方决定是否跳过）。"""
        last: Exception | None = None
        for attempt in range(self.retry + 1):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < self.retry:
                    time.sleep(0.6 * (attempt + 1))
        logger.warning("[%s] 拉取失败（已重试 %d 次）：%s", desc, self.retry, str(last)[:90])
        return None

    # ---------------- 主流程 ----------------
    def sync(self) -> dict[str, Any]:
        """同步行业映射。返回各来源的统计，供调用方判断覆盖率。"""
        stats: dict[str, Any] = {"sources": {}, "total": 0}
        since = datetime.now()
        # 先按配置对齐主用标签，避免配置改了但历史行没跟上
        self.refresh_primary_flags()

        for name in self.sources:
            try:
                if name == "sina":
                    result = self._sync_sina()
                elif name == "sw":
                    result = self._sync_sw()
                else:
                    logger.warning("未知板块来源：%s", name)
                    continue
            except Exception as exc:  # noqa: BLE001
                logger.warning("板块来源 %s 整体失败：%s", name, str(exc)[:120])
                stats["sources"][name] = {"ok": False, "mapped": 0, "error": str(exc)[:120]}
                continue
            stats["sources"][name] = result

        stats["total"] = int(
            self.storage.query_value("SELECT COUNT(*) FROM dim_stock_industry", default=0) or 0
        )
        stats["coverage"] = self.coverage()
        stats["elapsed_sec"] = round((datetime.now() - since).total_seconds(), 1)
        return stats

    # ---------------- 新浪 ----------------
    def _sync_sina(self) -> dict[str, Any]:
        """新浪行业：稳定且快，但覆盖约一半股票（不含新股）。"""
        import akshare as ak

        spot = self._call(
            lambda: ak.stock_sector_spot(indicator="新浪行业"), "新浪行业列表"
        )
        if spot is None or spot.empty:
            return {"ok": False, "mapped": 0, "error": "行业列表为空"}

        industries: list[dict] = []
        mapping: list[dict] = []
        failed: list[str] = []

        for _, row in spot.iterrows():
            label = str(row.get("label") or "").strip()
            name = str(row.get("板块") or "").strip()
            if not label or not name:
                continue
            cons = self._call(lambda l=label: ak.stock_sector_detail(sector=l), f"新浪[{name}]")
            if cons is None or cons.empty:
                failed.append(name)
                continue

            industries.append(
                {
                    "industry_id": f"sina:{label}",
                    "name": name,
                    "source": "sina",
                    "level": 1,
                    "parent": None,
                    "stock_count": len(cons),
                    "updated_at": datetime.now(),
                }
            )
            for code in cons["code"].astype(str).tolist():
                code = normalize_code(code)
                if code:
                    mapping.append(
                        {
                            "code": code,
                            "industry_name": name,
                            "source": "sina",
                            "is_primary": self._is_primary("sina"),
                            "updated_at": datetime.now(),
                        }
                    )
            if self.sleep:
                time.sleep(self.sleep)

        self._write(industries, mapping)
        logger.info(
            "新浪行业同步完成：%d 个行业，%d 条映射（失败 %d 个行业）",
            len(industries), len(mapping), len(failed),
        )
        return {
            "ok": bool(mapping),
            "industries": len(industries),
            "mapped": len(mapping),
            "failed": failed,
        }

    # ---------------- 申万（补充源）----------------
    def _sync_sw(self) -> dict[str, Any]:
        """申万行业：更权威，且成分接口附带财务指标，但接口**时好时坏**。

        因此这里只做「尽力而为」：能拿到多少算多少，失败一个行业跳过即可，
        绝不因为申万不可用而影响新浪已经建立好的映射。
        """
        import akshare as ak

        first = self._call(lambda: ak.sw_index_first_info(), "申万一级行业")
        if first is None or first.empty:
            return {"ok": False, "mapped": 0, "error": "一级行业列表不可用（接口不稳定）"}

        industries: list[dict] = []
        mapping: list[dict] = []
        finance: list[dict] = []
        failed: list[str] = []

        for _, row in first.iterrows():
            code = str(row.get("行业代码") or "").strip()
            name = str(row.get("行业名称") or "").strip()
            if not code or not name:
                continue
            cons = self._call(lambda c=code: ak.sw_index_third_cons(symbol=c), f"申万[{name}]")
            if cons is None or cons.empty:
                failed.append(name)
                continue

            industries.append(
                {
                    "industry_id": f"sw:{code}",
                    "name": name,
                    "source": "sw",
                    "level": 1,
                    "parent": None,
                    "stock_count": len(cons),
                    "updated_at": datetime.now(),
                }
            )
            for _, c in cons.iterrows():
                raw = str(c.get("股票代码") or "")
                norm = normalize_code(raw.split(".")[0]) if raw else ""
                if not norm:
                    continue
                mapping.append(
                    {
                        "code": norm,
                        "industry_name": name,
                        "source": "sw",
                        "is_primary": self._is_primary("sw"),
                        "updated_at": datetime.now(),
                    }
                )
                finance.append(self._finance_row(norm, c))
            if self.sleep:
                time.sleep(self.sleep)

        self._write(industries, mapping)
        if finance:
            self._write_finance(finance)
        logger.info(
            "申万行业同步完成：%d 个行业，%d 条映射，%d 条财务补充（失败 %d 个行业）",
            len(industries), len(mapping), len(finance), len(failed),
        )
        return {
            "ok": bool(mapping),
            "industries": len(industries),
            "mapped": len(mapping),
            "finance": len(finance),
            "failed": failed,
        }

    @staticmethod
    def _finance_row(code: str, row: pd.Series) -> dict[str, Any]:
        """申万成分附带的基本面字段。

        这些字段是意外收获：本来只为拿行业映射，但接口同时返回了
        ROE / 净利润增速 / 营收增速 / PE / PB，正好补上价值档缺失的基本面维度。
        字段可能缺失（新股、亏损股），统一留空由下游处理。
        """
        def num(key: str) -> float | None:
            val = row.get(key)
            try:
                f = float(val)
            except (TypeError, ValueError):
                return None
            return None if f != f else f  # NaN → None

        return {
            "code": code,
            "source": "sw",
            "roe": num("ROE(%)"),
            "pe_ttm": num("市盈率ttm"),
            "pb": num("市净率"),
            "dividend_yield": num("股息率"),
            "profit_growth": num("净利润增速(%)(2026-06-30)"),
            "revenue_growth": num("营收增速(%)(2026-06-30)"),
            "updated_at": datetime.now(),
        }

    # ---------------- 落库 ----------------
    def _write(self, industries: list[dict], mapping: list[dict]) -> None:
        if industries:
            self.storage.upsert_df(pd.DataFrame(industries), "dim_industry")
        if mapping:
            df = pd.DataFrame(mapping).drop_duplicates(subset=["code", "source"], keep="last")
            self.storage.upsert_df(df, "dim_stock_industry")

    def _write_finance(self, rows: list[dict]) -> None:
        df = pd.DataFrame(rows).drop_duplicates(subset=["code", "source"], keep="last")
        self.storage.upsert_df(df, "dim_stock_finance")

    # ---------------- 查询 ----------------
    def coverage(self) -> dict[str, Any]:
        """映射覆盖率。**这是判断板块功能可用性的关键指标**，务必关注。"""
        total = int(self.storage.query_value("SELECT COUNT(*) FROM dim_stock", default=0) or 0)
        mapped = int(
            self.storage.query_value(
                "SELECT COUNT(DISTINCT code) FROM dim_stock_industry", default=0
            )
            or 0
        )
        by_source = self.storage.query_df(
            "SELECT source, COUNT(DISTINCT code) AS n FROM dim_stock_industry GROUP BY source"
        )
        return {
            "stocks_total": total,
            "stocks_mapped": mapped,
            "ratio": f"{mapped / total * 100:.1f}%" if total else "0%",
            "by_source": {} if by_source.empty else dict(
                zip(by_source["source"], by_source["n"].astype(int))
            ),
        }

    def industry_of(self, code: str) -> str | None:
        """取某只股票的行业名（优先新浪，其次申万）。"""
        val = self.storage.query_value(
            "SELECT industry_name FROM dim_stock_industry WHERE code = ? "
            "ORDER BY is_primary DESC LIMIT 1",
            [normalize_code(code)],
        )
        return str(val) if val else None
