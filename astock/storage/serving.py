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
from typing import Any, Sequence

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
    # "recommend"（观察池 ads_recommend）、"feature_latest"（因子宽表 dws_feature）、
    # "review"（ads_review）三条快照已于 2026-10-08 随主链路删除 ——
    # 它们导出的都是那 8 个策略的产物（候选、因子面板、候选复盘）。
    # 板块强度保留两个分类体系（sw / sina），由前端按需筛选；
    # 只留最近 120 个交易日，避免快照体积过大。
    "sector_strength": """
        SELECT * FROM dws_sector_strength
        WHERE date >= (SELECT MAX(date) FROM dws_sector_strength) - INTERVAL 120 DAY
        ORDER BY date DESC, source, strength_score DESC
    """,
    "llm_usage": "SELECT * FROM sys_llm_usage ORDER BY date DESC LIMIT 30",
    # ---- v1.0 决策依据：影子信号（此前**完全没有**导出，导致网页上看不到真正的名单）----
    # **全量导出**（约 9.4 千行，不足 1 MB）。为什么不截断：
    # 净值曲线的窗口长度直接决定结论 —— 实测截到 180 个日历日后只剩 110 个交易日，
    # 该窗口内"主链路 +24.6% > 影子 +12.4%"；而完整 239 个交易日里
    # "影子 +146% ≫ 主链路 -17.6%"。截断会把页面变成一个误导工具。
    "shadow": """
        SELECT * FROM ads_shadow_pick ORDER BY date DESC, signal_score DESC
    """,
    # 盘中快照：只导最新一天（用于给影子候选标注"此刻能不能买"），体积可控
    "intraday": """
        SELECT * FROM dwd_intraday_snapshot
        WHERE date = (SELECT MAX(date) FROM dwd_intraday_snapshot)
    """,
    # "backtest_daily"（主链路净值）与 "backtest_scores"（主链路校准曲线原始点）
    # 已于 2026-10-08 随 ads_backtest（主链路回测）一并删除。
    # 影子的净值/校准不需要预导快照：它们由 shadow 快照现算（api/server.py）。
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
        """导出单表。**先写临时文件再原子替换**。

        为什么必须原子：展示层（FastAPI）是长驻进程、随时可能在读这些 parquet，
        而 daily 会在 18:30 重写它们。若直接覆盖写，读者有机会读到写了一半的文件
        （表现为网页偶发 500 / "Invalid parquet file"）。临时文件以 `.` 开头，
        也不会被 `_ensure_view` 的 `{name}.parquet` 规则误注册。
        """
        file_path = out_dir / f"{name}.parquet"
        tmp_path = out_dir / f".{name}.parquet.tmp"
        try:
            storage.conn.execute(
                f"COPY ( {sql} ) TO '{tmp_path.as_posix()}' "
                f"(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            n = int(
                storage.query_value(f"SELECT COUNT(*) FROM ( {sql} ) t", default=0) or 0
            )
            tmp_path.replace(file_path)  # 原子替换：读者要么看到旧版、要么看到新版
            stats[name] = n
        except Exception as exc:  # noqa: BLE001 - 单表失败不应影响其余快照
            logger.warning("导出 %s 失败：%s", name, exc)
            tmp_path.unlink(missing_ok=True)

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

    # strategy_stats（各策略命中数）已随策略层删除

    # 概览信息（供前端顶部状态栏使用，避免每次都查库）
    summary = {
        "stocks_total": int(storage.table_count("dim_stock")),
        "bars": int(storage.table_count("dwd_daily_bar")),
        "latest_bar_date": _as_str(storage.latest_trade_date()),
        "latest_trade_date": _as_str(storage.latest_open_trade_date()),
        # feature_date / rec_days / review_rows 已随主链路（因子宽表、观察池、候选复盘）删除
        # ---- v1.0 契约的三项验收指标（G1/G2/G3），前端顶部看板直接读这里 ----
        # G1：影子信号每日产出且被结算 —— 信号日数与最新信号日
        "shadow_rows": int(storage.table_count("ads_shadow_pick")),
        "shadow_days": int(
            storage.query_value("SELECT COUNT(DISTINCT date) FROM ads_shadow_pick", default=0) or 0
        ),
        "shadow_latest_date": _as_str(
            storage.query_value("SELECT MAX(date) FROM ads_shadow_pick", default=None)
        ),
        # G2：盘中快照积累（目标 ≥60 个交易日）
        "snapshot_days": int(
            storage.query_value(
                "SELECT COUNT(DISTINCT date) FROM dwd_intraday_snapshot", default=0
            )
            or 0
        ),
        "snapshot_latest_date": _as_str(
            storage.query_value("SELECT MAX(date) FROM dwd_intraday_snapshot", default=None)
        ),
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

    def ensure(self, *tables: str) -> bool:
        """确保若干张快照视图都已注册（多表 JOIN 的 SQL 需要）。"""
        return all(self._ensure_view(t) for t in tables)

    def query_tables(self, sql: str, tables: Sequence[str],
                     params: list[Any] | None = None) -> pd.DataFrame:
        """多表查询：任一快照缺失即返回空表（而不是抛错，展示层应"缺数据不报错"）。"""
        if not self.ensure(*tables):
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
