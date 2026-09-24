# -*- coding: utf-8 -*-
"""DuckDB 存储访问层。

对外只暴露 Storage 接口，业务层不直接接触 DuckDB API，
因此未来迁移到 PostgreSQL + TimescaleDB 时业务代码无需改动。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import duckdb
import pandas as pd

from astock.config import get_config
from astock.logger import get_logger

logger = get_logger("storage.db")

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"


class Storage:
    """DuckDB 连接与通用读写封装。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        cfg = get_config()
        self.db_path = Path(db_path) if db_path else cfg.duckdb_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: duckdb.DuckDBPyConnection | None = None

    # ---------------- 连接 ----------------
    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            try:
                self._conn = duckdb.connect(str(self.db_path))
            except duckdb.IOException as exc:
                raise RuntimeError(self._lock_message(str(exc))) from exc
            self._tune()
        return self._conn

    def _lock_message(self, raw: str) -> str:
        """构造带「谁占用了库」的诊断信息。

        DuckDB 的原始报错里已经包含占用进程的 PID，直接把它解析出来，
        用户就不用自己去猜是哪个任务卡住了。
        """
        import re

        holder = re.search(r"\(PID (\d+)\)", raw)
        hint = f"占用进程 PID = {holder.group(1)}" if holder else "占用进程未知"
        return (
            f"无法打开数据库（DuckDB 为独占文件锁）：{self.db_path}\n"
            f"{hint}\n"
            "处理方式（三选一）：\n"
            "  1) 等占用它的任务跑完 —— 常见于 backfill / backtest\n"
            "  2) 读取实时状态而不抢锁：python -m astock.cli status --snapshot\n"
            "  3) 确认可以中断时，结束该 PID 后再执行写入类命令\n"
            "说明：只读展示请用 `python -m astock.cli serve`，它只读 Parquet 快照，不占用主库。"
        )

    def _tune(self) -> None:
        """按机器情况设置资源参数，避免默认值在大表扫描时过慢。"""
        try:
            self._conn.execute("SET preserve_insertion_order=false")
            self._conn.execute("SET enable_progress_bar=false")
        except Exception:  # pragma: no cover - 老版本兼容
            pass

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------- 初始化 ----------------
    # 表结构增量迁移。
    # `CREATE TABLE IF NOT EXISTS` 不会修改**已存在**的表，因此后续新增的列
    # 必须显式 ALTER，否则写入时报 "Table xxx does not have a column named yyy"。
    # 这里集中声明「代码期望但 schema.sql 无法自动补齐」的列。
    COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
        "ads_recommend": {"primary_strategy": "VARCHAR"},
        "ads_backtest": {"hit_count": "INTEGER", "all_strategies": "VARCHAR"},
        # 财务报表必需披露日：缺它则无法避免前视偏差
        "dws_finance_metrics": {"report_date": "DATE"},
        # 记录涨停家数的来源口径，便于追溯历史状态判定
        "dws_market_regime": {"limit_up_source": "VARCHAR"},
    }

    # 纯派生表：内容完全可由上游数据重算，没有任何不可再生的信息。
    # 这类表一旦结构变更（尤其是**主键变更**，ALTER TABLE 无法表达），
    # 直接 DROP 重建是安全且省事的做法 —— 重建成本只是重跑一次计算，
    # 而放过结构不一致会留下难以排查的诡异错误。
    RECREATABLE_TABLES: set[str] = {
        "dws_sector_strength", "dws_limit_factor", "dws_dragon_factor",
        "dws_style_matrix",
    }

    def init_schema(self) -> None:
        """执行建表脚本（幂等），处理列补齐与派生表重建。"""
        sql = SCHEMA_FILE.read_text(encoding="utf-8")
        statements = [s.strip() for s in sql.split(";") if s.strip()]

        recreated = self._recreate_changed_tables(statements)
        for stmt in statements:
            self.conn.execute(stmt)
        added = self._migrate_columns()

        notes: list[str] = []
        if recreated:
            notes.append(f"已重建派生表：{'、'.join(recreated)}")
        if added:
            notes.append(f"已补齐列：{'、'.join(added)}")
        suffix = f"（{'；'.join(notes)}）" if notes else ""
        logger.info("表结构初始化完成：%s%s", self.db_path, suffix)

    def _recreate_changed_tables(self, statements: list[str]) -> list[str]:
        """对比派生表的实际列与 DDL 期望列，不一致则先 DROP。

        必须在执行 CREATE 之前调用，否则 IF NOT EXISTS 会直接跳过。
        """
        recreated: list[str] = []
        for table in self.RECREATABLE_TABLES:
            expected = self._expected_columns(statements, table)
            if not expected:
                continue
            try:
                info = self.query_df(f"PRAGMA table_info('{table}')")
            except Exception:  # noqa: BLE001
                continue
            if info.empty:
                continue  # 表还不存在，正常创建即可
            actual = set(info["name"].tolist())
            if actual != expected:
                logger.warning(
                    "派生表 %s 结构与定义不一致（多出 %s，缺少 %s），将重建",
                    table,
                    sorted(actual - expected) or "无",
                    sorted(expected - actual) or "无",
                )
                self.conn.execute(f"DROP TABLE {table}")
                recreated.append(table)
        return recreated

    @staticmethod
    def _expected_columns(statements: list[str], table: str) -> set[str]:
        """从建表语句中解析出期望的列名。

        两处必须小心，否则会解析出根本不存在的列名：
        1. **不能用贪婪正则取括号内容** —— `(.*\\))` 会一路吃到
           `PRIMARY KEY (a, b)` 的右括号，把 `b)` 当成列名（真的踩过），
           因此这里用括号配对扫描；
        2. **必须先剥注释** —— schema.sql 的整行/行尾注释都可能含逗号，
           直接按逗号切分会把注释文字当成列名。
        逗号也只在**括号深度为 0** 时才切，避免 `DECIMAL(10,2)` 被切开。
        """
        import re

        for stmt in statements:
            m = re.search(
                rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{re.escape(table)}\s*\(",
                stmt,
                re.IGNORECASE,
            )
            if not m:
                continue

            # ---- 括号配对，取出表定义主体 ----
            start = m.end()
            depth = 1
            i = start
            while i < len(stmt) and depth > 0:
                if stmt[i] == "(":
                    depth += 1
                elif stmt[i] == ")":
                    depth -= 1
                i += 1
            body = stmt[start : i - 1]

            # ---- 剥注释 ----
            body = "\n".join(
                line for line in body.splitlines() if not line.strip().startswith("--")
            )
            body = re.sub(r"--[^\n]*", "", body)

            # ---- 深度为 0 时切分逗号 ----
            parts: list[str] = []
            depth = 0
            buf: list[str] = []
            for ch in body:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                if ch == "," and depth == 0:
                    parts.append("".join(buf))
                    buf = []
                else:
                    buf.append(ch)
            parts.append("".join(buf))

            cols: set[str] = set()
            for raw in parts:
                token = raw.strip().split()[0] if raw.strip() else ""
                # 跳过表级约束（PRIMARY KEY (...) 等）
                if not token or token.upper() in {
                    "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"
                }:
                    continue
                cols.add(token)
            return cols
        return set()

    def _migrate_columns(self) -> list[str]:
        """为已存在的旧表补齐缺失列，返回新增列清单。"""
        added: list[str] = []
        for table, columns in self.COLUMN_MIGRATIONS.items():
            try:
                info = self.query_df(f"PRAGMA table_info('{table}')")
            except Exception:  # noqa: BLE001 - 表不存在时跳过
                continue
            if info.empty:
                continue
            existing = set(info["name"].tolist())
            for column, dtype in columns.items():
                if column in existing:
                    continue
                try:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {dtype}")
                    added.append(f"{table}.{column}")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("为 %s 补齐列 %s 失败：%s", table, column, exc)
        if added:
            self._backfill_migrated_columns(added)
        return added

    def _backfill_migrated_columns(self, added: list[str]) -> None:
        """给新增列填充合理值，避免历史行留 NULL 导致后续统计落空。"""
        if "ads_recommend.primary_strategy" in added:
            # 历史推荐未记录主策略，用当时的拼接策略名兜底（同名单策略时即为正确值）
            self.conn.execute(
                "UPDATE ads_recommend SET primary_strategy = strategy "
                "WHERE primary_strategy IS NULL"
            )
            logger.info("已回填 ads_recommend.primary_strategy 历史数据")

    # ---------------- 查询 ----------------
    def query_df(self, sql: str, params: Sequence[Any] | None = None) -> pd.DataFrame:
        return self.conn.execute(sql, list(params) if params else []).df()

    def query_one(self, sql: str, params: Sequence[Any] | None = None) -> tuple | None:
        row = self.conn.execute(sql, list(params) if params else []).fetchone()
        return row

    def query_value(self, sql: str, params: Sequence[Any] | None = None, default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if row is None or row[0] is None:
            return default
        return row[0]

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> None:
        self.conn.execute(sql, list(params) if params else [])

    # ---------------- 写入 ----------------
    def upsert_df(self, df: pd.DataFrame, table: str) -> int:
        """按主键幂等写入 DataFrame。重复数据自动覆盖，不会产生脏数据。

        **只写目标表真实存在的列**：数据源可能多返回列，直接拼进 INSERT 会让
        DuckDB 抛 `INTERNAL Error: Column with name "x" does not exist`。
        实测踩过：指数的 `preclose` 就是这样炸的 —— 而且它是"有数据要写时"才炸，
        上一轮恰好 `入库 0 行`（df 为空提前返回）把它藏了过去，
        表现为「同样的命令有时成功有时失败」，极难排查。
        这里统一按表结构裁剪，并把多出来的列打日志（不静默丢弃，否则字段写不进去
        也无人知晓）。
        """
        if df is None or df.empty:
            return 0
        table_cols = {
            r[0] for r in self.conn.execute(f"DESCRIBE {table}").fetchall()
        }
        keep = [c for c in df.columns if c in table_cols]
        extra = [c for c in df.columns if c not in table_cols]
        if extra:
            logger.warning(
                "写入 %s：忽略表中不存在的列 %s（数据源与表结构不一致）", table, extra
            )
        if not keep:
            return 0
        cols = ", ".join(keep)
        self.conn.register("_upsert_tmp", df[keep])
        try:
            self.conn.execute(
                f"INSERT OR REPLACE INTO {table} ({cols}) SELECT {cols} FROM _upsert_tmp"
            )
        finally:
            self.conn.unregister("_upsert_tmp")
        return len(df)

    def replace_partition(self, df: pd.DataFrame, table: str, keys: Iterable[str]) -> int:
        """按分区键先删后插（用于按日期整体重算的场景，如因子表）。"""
        if df is None or df.empty:
            return 0
        key_list = list(keys)
        if key_list:
            conds = " AND ".join(f"{k} = ?" for k in key_list)
            values = [df.iloc[0][k] for k in key_list]
            self.conn.execute(f"DELETE FROM {table} WHERE {conds}", values)
        return self.upsert_df(df, table)

    # ---------------- 游标状态（断点续传核心）----------------
    def set_state(self, task: str, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO sys_collect_state VALUES (?, ?, ?, ?)",
            [task, key, str(value), datetime.now()],
        )

    def get_state(self, task: str, key: str, default: str | None = None) -> str | None:
        return self.query_value(
            "SELECT value FROM sys_collect_state WHERE task = ? AND key = ?",
            [task, key],
            default=default,
        )

    # ---------------- 常用查询 ----------------
    def latest_trade_date(self, table: str = "dwd_daily_bar") -> date | None:
        return self.query_value(f"SELECT MAX(date) FROM {table}")

    def latest_open_trade_date(self, not_after: date | None = None) -> date | None:
        """不晚于指定日期（默认今天）的最近一个交易日。

        必须加上时间上界：交易日历会同步到次年年底，若不设上界会取到未来日期。
        """
        bound = not_after or date.today()
        return self.query_value(
            "SELECT MAX(date) FROM trade_calendar WHERE is_open = TRUE AND date <= ?", [bound]
        )

    def table_count(self, table: str) -> int:
        return int(self.query_value(f"SELECT COUNT(*) FROM {table}", default=0) or 0)

    def list_tables(self) -> list[str]:
        df = self.query_df("SHOW TABLES")
        return df.iloc[:, 0].tolist() if not df.empty else []

    # ---------------- 冷热分离 ----------------
    def archive_parquet(self, hot_days: int | None = None) -> dict[str, int]:
        """把超过 hot_days 的明细数据导出为 Parquet 并从热表删除。

        返回 {parquet_file: rows}。归档后热表只保留近期数据，控制单文件体积。
        """
        cfg = get_config()
        hot = hot_days or int(cfg.get("storage.hot_days", 400))
        cutoff = self.query_value(
            "SELECT MAX(date) - INTERVAL (?) DAY FROM dwd_daily_bar", [hot]
        )
        if cutoff is None:
            return {}

        result: dict[str, int] = {}
        for table, key_col in (("dwd_daily_bar", "date"), ("dwd_index_bar", "date")):
            rows = int(
                self.query_value(
                    f"SELECT COUNT(*) FROM {table} WHERE {key_col} < ?", [cutoff], default=0
                )
                or 0
            )
            if rows == 0:
                continue
            out = cfg.parquet_dir / table
            out.mkdir(parents=True, exist_ok=True)
            file_path = out / f"{table}_{cutoff}.parquet"
            self.conn.execute(
                f"COPY (SELECT * FROM {table} WHERE {key_col} < ?) TO '{file_path.as_posix()}' "
                f"(FORMAT PARQUET, COMPRESSION ZSTD)",
                [cutoff],
            )
            self.conn.execute(f"DELETE FROM {table} WHERE {key_col} < ?", [cutoff])
            result[str(file_path)] = rows
            logger.info("归档 %s → %s（%d 行）", table, file_path.name, rows)

        return result


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = Storage()
        _storage.init_schema()
    return _storage


def try_get_storage() -> Storage | None:
    """尝试获取可写存储；若数据库被其它进程占用则返回 None。"""
    try:
        return get_storage()
    except (RuntimeError, duckdb.IOException):
        return None


def release_storage() -> None:
    """关闭并丢弃进程级单例连接。

    长驻进程（如 Web 服务）**必须**在完成需要写库的操作后调用它，
    否则连接会一直挂在进程上，把主库锁到进程退出为止 ——
    直接后果是用户开着网页时 `daily` / `backfill` 全都跑不了。
    """
    global _storage
    if _storage is not None:
        try:
            _storage.close()
        except Exception:  # noqa: BLE001
            pass
        _storage = None
