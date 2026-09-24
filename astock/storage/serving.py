# -*- coding: utf-8 -*-
"""服务层快照（Serving Snapshot）。

为什么需要这一层：
DuckDB 对数据库文件采用**独占文件锁**，同一时间只能有一个进程打开它。
如果 Web 服务长期持有连接，`daily` / `backfill` 就无法运行；反之亦然。

解决方案是把「计算」与「展示」彻底解耦：
- 计算层（CLI）在每次 daily 结束时，把展示所需数据导出为 Parquet 快照；
- 展示层（FastAPI）只读这些 Parquet，完全不接触主库，因此互不阻塞。

Parquet 没有独占锁，任何进程都可随时读取，也便于备份与拷贝。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from astock.config import get_config
from astock.logger import get_logger
from astock.storage.db import Storage, get_storage

logger = get_logger("storage.serving")

# 快照包含的表及各自的数据范围
SNAPSHOT_QUERIES: dict[str, str] = {
    "dim_stock": "SELECT * FROM dim_stock",
    "market_regime": "SELECT * FROM dws_market_regime ORDER BY date",
    "recommend": """
        SELECT * FROM ads_recommend
        WHERE rec_date >= (SELECT MAX(rec_date) FROM ads_recommend) - INTERVAL 180 DAY
        ORDER BY rec_date DESC, tier, tier_rank
    """,
    "review": """
        SELECT * FROM ads_review
        WHERE rec_date >= (SELECT MAX(rec_date) FROM ads_review) - INTERVAL 180 DAY
        ORDER BY rec_date DESC
    """,
    "feature_latest": """
        SELECT f.*, s.name, s.board, i.industry_name AS industry
        FROM dws_feature f
        LEFT JOIN dim_stock s ON s.code = f.code
        LEFT JOIN (
            SELECT code, industry_name FROM (
                SELECT code, industry_name,
                       ROW_NUMBER() OVER (PARTITION BY code
                                          ORDER BY is_primary DESC, source) AS rn
                FROM dim_stock_industry
            ) t WHERE rn = 1
        ) i ON i.code = f.code
        WHERE f.date = (SELECT MAX(date) FROM dws_feature)
    """,
    # 板块强度保留两个分类体系（sw / sina），由前端按需筛选；
    # 只留最近 120 个交易日，避免快照体积过大。
    "sector_strength": """
        SELECT * FROM dws_sector_strength
        WHERE date >= (SELECT MAX(date) FROM dws_sector_strength) - INTERVAL 120 DAY
        ORDER BY date DESC, source, strength_score DESC
    """,
    "llm_usage": "SELECT * FROM sys_llm_usage ORDER BY date DESC LIMIT 30",
}


def serving_dir() -> Path:
    path = get_config().data_dir / "serving"
    path.mkdir(parents=True, exist_ok=True)
    return path


def export(storage: Storage | None = None, bars_days: int = 250) -> dict[str, Any]:
    """导出服务层快照。返回 {table: rows} 与导出时间。"""
    storage = storage or get_storage()
    out_dir = serving_dir()
    stats: dict[str, Any] = {}

    def dump(name: str, sql: str) -> None:
        file_path = out_dir / f"{name}.parquet"
        try:
            storage.conn.execute(
                f"COPY ( {sql} ) TO '{file_path.as_posix()}' "
                f"(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            n = int(
                storage.query_value(f"SELECT COUNT(*) FROM ( {sql} ) t", default=0) or 0
            )
            stats[name] = n
        except Exception as exc:  # noqa: BLE001 - 单表失败不应影响其余快照
            logger.warning("导出 %s 失败：%s", name, exc)

    for name, sql in SNAPSHOT_QUERIES.items():
        dump(name, sql)

    # 个股 K 线（用于详情页画图），只保留最近 N 个交易日
    dump(
        "stock_bars",
        f"""
        SELECT code, date, open, high, low, close, volume, amount, pct_chg, turn
        FROM dwd_daily_bar
        WHERE date >= (
            SELECT MIN(date) FROM (
                SELECT DISTINCT date FROM dwd_daily_bar ORDER BY date DESC LIMIT {int(bars_days)}
            ) t
        )
        """,
    )

    # 策略命中统计（由 daily 流程写入 dws_market_regime.detail 之外，这里单独落一张表）
    dump("strategy_stats", "SELECT * FROM sys_strategy_stats")

    # 概览信息（供前端顶部状态栏使用，避免每次都查库）
    summary = {
        "stocks_total": int(storage.table_count("dim_stock")),
        "bars": int(storage.table_count("dwd_daily_bar")),
        "latest_bar_date": _as_str(storage.latest_trade_date()),
        "latest_trade_date": _as_str(storage.latest_open_trade_date()),
        "feature_date": _as_str(storage.latest_trade_date("dws_feature")),
        "rec_days": int(
            storage.query_value("SELECT COUNT(DISTINCT rec_date) FROM ads_recommend", default=0) or 0
        ),
        "review_rows": int(storage.table_count("ads_review")),
    }

    meta = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "tables": stats,
        "summary": summary,
    }
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("服务层快照导出完成：%s", ", ".join(f"{k}({v})" for k, v in stats.items()))
    return meta


def _as_str(value: Any) -> str | None:
    return None if value is None else str(value)


class ServingReader:
    """只读快照访问器：使用内存连接直接查询 Parquet，不触碰主库。

    **线程安全设计**（FastAPI 会把同步接口丢进线程池，多个请求天然并发）：

    - 只创建一个「基连接」，其内存库承载全部视图；创建过程用锁保护，
      避免两个线程各自建库、后建的覆盖前一个；
    - 每个线程通过 `base.cursor()` 拿到自己的游标。游标**共享同一个内存库的 catalog**，
      因此基连接上注册的视图对所有线程可见，而各线程的查询互不干扰；
    - 视图注册集合与创建动作都在锁内完成，杜绝「集合说有、连接说没有」的不一致 ——
      早期版本正是栽在这里：`_views` 是两个连接共享的，导致查询落到没有视图的连接上，
      报 `Catalog Error: Table with name xxx does not exist`。
    """

    def __init__(self, directory: Path | None = None) -> None:
        self.dir = directory or serving_dir()
        # RLock：_ensure_view 持锁期间还会调用 _base_conn()，普通 Lock 会自锁死
        self._lock = threading.RLock()
        self._base: duckdb.DuckDBPyConnection | None = None
        self._registered: set[str] = set()
        # 每个线程一个游标（thread-local），避免所有请求排队等同一把连接锁
        self._local = threading.local()

    # ---------------- 连接 ----------------
    def _base_conn(self) -> duckdb.DuckDBPyConnection:
        """惰性创建唯一的基连接（双重检查加锁，保证只建一次）。"""
        base = self._base
        if base is not None:
            return base
        with self._lock:
            if self._base is None:
                con = duckdb.connect()  # 内存库
                con.execute("SET preserve_insertion_order=false")
                self._base = con
            return self._base

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        """当前线程的游标，共享基连接的内存库 catalog。"""
        cur = getattr(self._local, "con", None)
        if cur is None:
            cur = self._base_conn().cursor()
            self._local.con = cur
        return cur

    def close(self) -> None:
        with self._lock:
            if self._base is not None:
                try:
                    self._base.close()
                except Exception:  # noqa: BLE001
                    pass
            self._base = None
            self._registered.clear()
            self._local = threading.local()

    # ---------------- 元信息 ----------------
    def meta(self) -> dict[str, Any]:
        path = self.dir / "meta.json"
        if not path.exists():
            return {"exported_at": None, "tables": {}}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"exported_at": None, "tables": {}}

    def available(self) -> bool:
        return (self.dir / "market_regime.parquet").exists()

    def _ensure_view(self, name: str) -> bool:
        """确保视图已注册，且注册在**基连接**上。

        两个关键点：
        1. 必须注册到基连接 —— 只有基连接 catalog 里的视图才被所有线程的游标共享。
           若顺手注册到当前线程的游标上，其他线程依旧查不到。
        2. 注册动作与标记「已注册」必须在同一把锁内完成。
           早期版本先标记后创建、且标记是跨连接共享的，
           于是出现「集合说有视图、实际连接的 catalog 里没有」的错乱。
        """
        if name in self._registered:
            return True
        file_path = self.dir / f"{name}.parquet"
        if not file_path.exists():
            return False
        with self._lock:
            if name in self._registered:
                return True
            self._base_conn().execute(
                f"CREATE OR REPLACE VIEW {name} AS "
                f"SELECT * FROM read_parquet('{file_path.as_posix()}')"
            )
            self._registered.add(name)
        return True

    # ---------------- 查询 ----------------
    def query(self, sql: str, table: str, params: list[Any] | None = None) -> pd.DataFrame:
        """执行查询；若所需快照表不存在则返回空 DataFrame。"""
        if not self._ensure_view(table):
            return pd.DataFrame()
        return self.con.execute(sql, params or []).df()

    def records(self, sql: str, table: str, params: list[Any] | None = None) -> list[dict]:
        df = self.query(sql, table, params)
        if df.empty:
            return []
        # date_format="iso" 必须显式指定：默认会把日期序列化成毫秒时间戳（整数），
        # 导致这些值再作为 DATE 参数回查时报 "Unimplemented type for cast (BIGINT -> DATE)"。
        return json.loads(
            df.to_json(orient="records", force_ascii=False, date_format="iso")
        )

    def scalar(self, sql: str, table: str, params: list[Any] | None = None, default: Any = None) -> Any:
        df = self.query(sql, table, params)
        if df.empty or df.iloc[0, 0] is None:
            return default
        return df.iloc[0, 0]


_reader: ServingReader | None = None


def get_reader() -> ServingReader:
    global _reader
    if _reader is None:
        _reader = ServingReader()
    return _reader
