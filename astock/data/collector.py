# -*- coding: utf-8 -*-
"""数据采集器：回填 / 每日增量 / 断点续传 / 缺口自动补数。

针对「Windows 电脑、不保证 24 小时开机」的环境特点，本模块的所有写入都是幂等的，
任务可以随时中断后重跑，不会产生重复或脏数据，也不会漏数据。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from multiprocessing import Pool
from typing import Iterable, Sequence

import pandas as pd

from astock.config import get_config
from astock.data.calendar import TradeCalendar
from astock.data.sources.base import normalize_code
from astock.data.universe import Universe
from astock.logger import get_logger
from astock.storage.db import Storage, get_storage

logger = get_logger("data.collector")

BATCH_FLUSH = 300  # 每采集 N 只股票落盘一次，降低中断损失

# 「数据源确认无数据」的标记有效期（天）。
# 退市股每次重试都要走完所有通道，非常浪费；但停牌股可能恢复交易，
# 因此标记到期后会再试一次，而不是永久跳过。
EMPTY_RETRY_DAYS = 7


# ---------------------------------------------------------------
# 进程池 worker（必须是模块级函数，Windows 下才能被 pickle）
# ---------------------------------------------------------------
# 每个 worker 进程复用同一个数据源连接。
# 关键教训：若每采一只股票就 login + logout 一次，800 只股票会产生 1600 次登录请求，
# baostock 服务端会直接拒绝后续登录，表现为「整批 100% 失败」。
_WORKER_SOURCE = None
_WORKER_SOURCE_NAME: str | None = None


def _build_source(source_name: str, adjust: str, retry: int, sleep: float):
    """按名称创建数据源实例。

    **只剩 akshare**：baostock / adata 适配器已于 2026-09-30 随数据源收敛删除
    （baostock 连接脆弱且会被拉黑；adata 几乎未使用）。日线走同花顺整库导出，
    此处只服务「股票列表 / 交易日历 / 指数日线」三个低频接口。
    保留传参形式是为了不改动调用方与 multiprocessing 的 pickle 契约。
    """
    from astock.data.sources.akshare_source import AkshareSource

    return AkshareSource()


def _get_worker_source(source_name: str, adjust: str, retry: int, sleep: float):
    """worker 进程内的数据源单例（每个进程只建立一次连接）。"""
    global _WORKER_SOURCE, _WORKER_SOURCE_NAME
    if _WORKER_SOURCE is None or _WORKER_SOURCE_NAME != source_name:
        if _WORKER_SOURCE is not None:
            try:
                _WORKER_SOURCE.close()
            except Exception:
                pass
        source = _build_source(source_name, adjust, retry, sleep)
        source.open()
        _WORKER_SOURCE = source
        _WORKER_SOURCE_NAME = source_name
    return _WORKER_SOURCE


def _fetch_worker(payload: tuple) -> tuple[str, pd.DataFrame, bool]:
    """进程池 worker。

    返回 (code, df, ok)：ok=False 表示「拉取失败」而非「该股确实无数据」，
    调用方必须区分这两种情况，否则会把临时失败误判成永久空数据。
    """
    global _WORKER_SOURCE, _WORKER_SOURCE_NAME
    code, start, end, adjust, retry, sleep, source_name = payload
    try:
        source = _get_worker_source(source_name, adjust, retry, sleep)
        df = source.fetch_daily(code, start, end)
        return code, df, True
    except Exception:  # noqa: BLE001 - worker 内异常不应中断整体任务
        # 连接可能已失效，丢弃单例，下一次调用重新建立
        if _WORKER_SOURCE is not None:
            try:
                _WORKER_SOURCE.close()
            except Exception:
                pass
        _WORKER_SOURCE = None
        _WORKER_SOURCE_NAME = None
        return code, pd.DataFrame(), False


class Collector:
    """数据采集调度器。"""

    def __init__(self, storage: Storage | None = None, source_name: str | None = None) -> None:
        self.cfg = get_config()
        self.storage = storage or get_storage()
        self.calendar = TradeCalendar(self.storage)
        self.source_name = source_name or self.cfg.get("data.primary_source", "baostock")
        self.adjust = str(self.cfg.get("data.adjust_flag", "2"))
        self.retry = int(self.cfg.get("data.retry", 3))
        self.reconnect_every = int(self.cfg.get("data.reconnect_every", 200))
        self.sleep = float(self.cfg.get("data.request_sleep", 0.01))
        self._source = None

    # ---------------- 数据源生命周期 ----------------
    def _open_source(self):
        """打开数据源，主源不可用时按 data.source_fallback 顺序自动降级。

        实测 baostock 会因登录过频被拉黑（「黑名单用户」），此时若不做降级，
        整个采集流程会直接失败。降级链保证「至少有一个源能用」。
        """
        if self._source is not None:
            return self._source

        fallback = [str(s) for s in (self.cfg.get("data.source_fallback", []) or [])]
        candidates: list[str] = [self.source_name]
        for name in fallback:
            if name not in candidates:
                candidates.append(name)

        errors: list[str] = []
        for name in candidates:
            try:
                source = _build_source(name, self.adjust, self.retry, self.sleep)
                source.open()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {str(exc)[:70]}")
                logger.warning("数据源 %s 不可用：%s", name, str(exc)[:90])
                continue

            if name != self.source_name:
                logger.warning("已自动切换到备用数据源：%s", name)
                self.source_name = name
            self._source = source
            return self._source

        raise RuntimeError(
            "所有数据源均不可用，请检查网络或稍后重试：\n  " + "\n  ".join(errors)
        )

    def close(self) -> None:
        if self._source is not None:
            self._source.close()
            self._source = None

    # ---------------- 基础信息同步 ----------------
    def sync_calendar(self, force: bool = False) -> int:
        return self.calendar.sync(force=force, source=self._open_source())

    def sync_stock_list(self) -> int:
        """同步股票列表到 dim_stock。

        ⚠️ 必须保留库里已有的 `out_date` / `ipo_date`。
        备用数据源（akshare / adata）不提供退市日，会把 `out_date` 写成 NaT
        （akshare_source.py / adata_source.py 的列表实现里写死），
        而这里是 INSERT OR REPLACE —— **整行覆盖**。所以一旦改用备用源同步股票列表，
        `dim_stock.out_date` 会被静默抹空；而 `Universe.filtered_codes` 正是靠
        `out_date.isna() | (out_date > as_of)` 判断「当时是否已退市」。

        后果是**幸存者偏差的防护无声失效**：回测会把当时已退市（现实中归零）的
        股票当成可交易标的，且不报错、无告警、结果上看不出异常 —— 这正是它最难
        被发现的原因。修法是「源给空值时沿用库里既有值」，只补不覆盖。
        """
        source = self._open_source()
        df = source.list_stocks()
        if df.empty:
            logger.warning("股票列表为空，跳过同步")
            return 0
        df = df.drop_duplicates(subset=["code"], keep="first")

        preserved = 0
        try:
            existing = self.storage.query_df("SELECT code, ipo_date, out_date FROM dim_stock")
        except Exception:  # noqa: BLE001
            existing = pd.DataFrame()
        if not existing.empty:
            ref = existing.drop_duplicates(subset=["code"]).set_index("code")
            for col in ("ipo_date", "out_date"):
                if col not in df.columns:
                    df[col] = pd.NaT
                from_db = df["code"].map(ref[col])
                need = df[col].isna() & from_db.notna()
                if need.any():
                    preserved += int(need.sum())
                    df.loc[need, col] = from_db[need]
        if preserved:
            logger.info("已保留 %d 个源缺失的上市/退市日期（避免防幸存者偏差失效）", preserved)

        df["updated_at"] = datetime.now()
        n = self.storage.upsert_df(df, "dim_stock")
        self.storage.set_state("collect", "stock_list_at", datetime.now().isoformat())
        logger.info("股票列表同步完成：%d 只", n)
        return n

    def ensure_base(self, force: bool = False) -> None:
        """确保股票列表与交易日历可用。

        统一复用同一个数据源实例：baostock 的 socket 是模块级全局的，
        多实例各自 login/logout 会互相破坏连接，导致 WinError 10038。
        """
        self._open_source()
        if force or self.storage.table_count("dim_stock") == 0:
            self.sync_stock_list()
        if force or self.storage.table_count("trade_calendar") == 0:
            self.calendar.sync(force=force, source=self._source)

    # ---------------- 增量状态 ----------------
    def _last_dates(self) -> dict[str, date]:
        df = self.storage.query_df("SELECT code, MAX(date) AS last_date FROM dwd_daily_bar GROUP BY code")
        if df.empty:
            return {}
        return {
            str(r["code"]): (r["last_date"].date() if hasattr(r["last_date"], "date") else r["last_date"])
            for _, r in df.iterrows()
        }

    def _empty_marked(self) -> dict[str, str]:
        """返回 {code: 标记时间}。

        记录时间而不是简单布尔标记，是为了避免「停牌股被永久跳过」：
        标记超过 EMPTY_RETRY_DAYS 天后会重新尝试一次。
        """
        df = self.storage.query_df(
            "SELECT key, value FROM sys_collect_state "
            "WHERE task = 'collect' AND value LIKE 'EMPTY%'"
        )
        if df.empty:
            return {}
        out: dict[str, str] = {}
        for _, r in df.iterrows():
            raw = str(r["value"])
            out[str(r["key"])] = raw.split(":", 1)[1] if ":" in raw else ""
        return out

    def _is_recently_empty(self, code: str, markers: dict[str, str]) -> bool:
        """该股票是否在近期被判定为「数据源无数据」。"""
        if code not in markers:
            return False
        stamp = markers[code]
        if not stamp:
            return True
        try:
            marked = datetime.fromisoformat(stamp)
        except ValueError:
            return True
        return (datetime.now() - marked).days < EMPTY_RETRY_DAYS

    def reset_empty_markers(self) -> int:
        """清除「无数据」标记。

        用于两种情况：一是此前因网络波动被误判为无数据的股票需要重试，
        二是退市/长期停牌股票在数据源补齐后需要重新拉取。
        """
        n = self.storage.query_value(
            "SELECT COUNT(*) FROM sys_collect_state WHERE task = 'collect' AND value LIKE 'EMPTY%'",
            default=0,
        )
        self.storage.execute(
            "DELETE FROM sys_collect_state WHERE task = 'collect' AND value LIKE 'EMPTY%'"
        )
        logger.info("已重置 %s 条无数据标记", n)
        return int(n or 0)

    # ---------------- 任务构建 ----------------
    def _pending_codes(self, codes: Sequence[str], end: date) -> list[str]:
        """返回仍需采集的股票。

        跳过两类：
        1. 已采到最新交易日；
        2. 近期（EMPTY_RETRY_DAYS 天内）被数据源确认无数据 —— 主要是退市股，
           否则它们会在每次日常增量里被反复重试，白白浪费请求额度。
        """
        last_dates = self._last_dates()
        empty = self._empty_marked()
        pending = []
        for code in codes:
            code = normalize_code(code)
            if self._is_recently_empty(code, empty):
                continue
            last = last_dates.get(code)
            if last is not None and last >= end:
                continue
            pending.append(code)
        return pending

    def _build_tasks(
        self,
        codes: Sequence[str],
        start_default: date,
        end: date,
        skip_empty: bool = True,
    ) -> list[tuple]:
        last_dates = self._last_dates()
        empty = self._empty_marked() if skip_empty else set()
        tasks: list[tuple] = []
        for code in codes:
            code = normalize_code(code)
            if code in empty and code not in last_dates:
                continue
            last = last_dates.get(code)
            start = (last + timedelta(days=1)) if last else start_default
            if start > end:
                continue
            tasks.append(
                (code, start, end, self.adjust, self.retry, self.sleep, self.source_name)
            )
        return tasks

    # ---------------- 执行 ----------------
    def _run_tasks(self, tasks: list[tuple], workers: int = 1) -> dict:
        """执行采集任务并落盘。返回统计信息。"""
        if not tasks:
            logger.info("没有需要采集的任务（数据已是最新）")
            return {"tasks": 0, "rows": 0, "empty": 0, "failed": 0}

        logger.info("待采集 %d 只股票，模式：%s", len(tasks), f"多进程({workers})" if workers > 1 else "单进程")

        stats = {"tasks": len(tasks), "rows": 0, "empty": 0, "failed": 0}
        buffer: list[pd.DataFrame] = []
        buffer_rows = 0

        def handle(code: str, df: pd.DataFrame, ok: bool = True) -> None:
            """ok=False 表示拉取失败，绝不能标记为 EMPTY（否则该股会被永久跳过）。"""
            nonlocal buffer_rows
            if not ok:
                stats["failed"] += 1
                return
            if df is None or df.empty:
                stats["empty"] += 1
                # 带上标记时间，便于 EMPTY_RETRY_DAYS 天后自动重试一次
                self.storage.set_state(
                    "collect", code, f"EMPTY:{datetime.now().isoformat(timespec='seconds')}"
                )
                return
            df = df.copy()
            df["adjust"] = self.adjust
            buffer.append(df)
            buffer_rows += len(df)
            stats["rows"] += len(df)
            self.storage.set_state("collect", code, str(df["date"].max().date()))

        def flush() -> None:
            nonlocal buffer, buffer_rows
            if not buffer:
                return
            merged = pd.concat(buffer, ignore_index=True)
            self.storage.upsert_df(merged, "dwd_daily_bar")
            buffer = []
            buffer_rows = 0

        if workers and workers > 1:
            with Pool(workers) as pool:
                for i, (code, df, ok) in enumerate(
                    pool.imap_unordered(_fetch_worker, tasks, chunksize=8), 1
                ):
                    handle(code, df, ok)
                    if buffer_rows >= 20000:
                        flush()
                    if i % 200 == 0:
                        logger.info(
                            "进度 %d/%d | 入库 %d 行 | 失败 %d",
                            i, len(tasks), stats["rows"], stats["failed"],
                        )
        else:
            source = self._open_source()
            for i, (code, start, end, *_rest) in enumerate(tasks, 1):
                ok = True
                try:
                    df = source.fetch_daily(code, start, end)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[%s] 采集失败: %s", code, exc)
                    df = pd.DataFrame()
                    ok = False
                handle(code, df, ok)
                if buffer_rows >= 20000:
                    flush()
                if i % 200 == 0:
                    logger.info(
                        "进度 %d/%d | 入库 %d 行 | 失败 %d",
                        i, len(tasks), stats["rows"], stats["failed"],
                    )

        flush()
        logger.info(
            "采集完成：任务 %d | 入库 %d 行 | 无数据 %d | 失败 %d",
            stats["tasks"], stats["rows"], stats["empty"], stats["failed"],
        )
        return stats

    # ---------------- 回填 ----------------
    def backfill(
        self,
        years: int | None = None,
        limit: int | None = None,
        codes: Iterable[str] | None = None,
        start: date | None = None,
        workers: int = 4,
        retry_empty: bool = False,
    ) -> dict:
        """回填历史日线。中断后重跑自动续传（依据本地已有的最新日期）。"""
        self.ensure_base()
        if retry_empty:
            self.reset_empty_markers()
        years = years or int(self.cfg.get("data.backfill_years", 2))
        start_default = start or (date.today() - timedelta(days=int(365.25 * years)))
        end = self._safe_end_date()

        if codes is None:
            codes = Universe(self.storage).all_codes()
        codes = list(codes)
        if limit:
            # 取「尚未采集」的前 limit 只，使分批执行天然衔接（已入库的自动跳过）
            codes = self._pending_codes(codes, end)[:limit]

        self.storage.set_state("collect", "backfill_started_at", datetime.now().isoformat())
        stats = self._run_tasks(self._build_tasks(codes, start_default, end), workers=workers)
        self.storage.set_state("collect", "backfill_finished_at", datetime.now().isoformat())
        return stats

    # ---------------- 数据完整性守卫 ----------------
    def _safe_end_date(self) -> date:
        """本次采集应处理到的最后日期。

        统一走这里，避免散落的 `latest_open_date() or date.today()` ——
        那个 `or date.today()` 兜底会在日历为空时**绕过完整性守卫**，
        把当天未收盘的 K 线拉进库。日历为空时回退到昨天即可。
        """
        resolved, _skipped = self.calendar.resolve_data_date()
        return resolved if resolved is not None else (date.today() - timedelta(days=1))

    # 预检用的股票：多只备选，避免单只停牌/退市导致误判"源不可用"
    PROBE_CODES = ("600519", "000001", "600036")

    def probe_source_date(self, target: date) -> tuple[date | None, str]:
        """探测数据源**实际**能提供到哪一天。

        为什么必须在全量采集前做这一步：
        `data_ready_time` 只是一个**时钟**（默认 16:00），它无法知道行情源
        是否真的发布了当日数据。实测东财在 16:18 时仍拿不到当日日线 ——
        若不预检，就会对全市场数千只股票发出注定失败的请求。

        返回 `(源最新可用日期, 诊断信息)`：
          - 日期非空且 < target ⇒ 源"尚未发布"，应中止（等发布后再跑）；
          - 日期为 None        ⇒ 源"不可达/异常"，**同样应中止**。
            这一点是踩坑后改的：最初设计成"探测失败就继续"，结果在一次
            源故障中对 5263 只股票空转，跑了 800 只只入库 5 行。
            源不健康时继续采集，只会浪费时间并加重限流。
        """
        # 用**区间**而不是单日查询：只查 target 那一天时，"源坏了"和
        # "源正常但该日数据还没发布"都会返回空，无法区分 ——
        # 而这两种情况对用户的含义完全不同（一个是等，一个是排查）。
        # 查一个回看窗口就能拿到"源实际最新到哪一天"。
        start = target - timedelta(days=10)
        errors: list[str] = []
        for code in self.PROBE_CODES:
            for attempt in (1, 2):
                try:
                    df = self._open_source().fetch_daily(code, start, target)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{code}#{attempt}:{type(exc).__name__}")
                    continue
                if df is None or df.empty:
                    continue
                try:
                    dates = pd.to_datetime(df["date"]).dt.date
                except Exception:  # noqa: BLE001
                    continue
                if len(dates):
                    return max(dates), ""
        return None, "；".join(errors[:5]) or "所有探测股票均无返回"

    def sync_from_dump_fallback(self, target: date) -> dict | None:
        """免费源拿不到目标日时，用同花顺全市场导出补数。

        成功返回统计，失败（无 key / 网络 / 导出不含该日）返回 None ——
        调用方据此决定是否中止，绝不让异常穿透到主流程。
        """
        try:
            from astock.data.hithink import HithinkCollector

            stats = HithinkCollector(self.storage).sync_daily_from_dump(days=10)
        except Exception as exc:  # noqa: BLE001
            logger.warning("同花顺导出补数失败：%s", str(exc)[:150])
            return None
        if stats.get("rows"):
            logger.warning(
                "已用同花顺全市场导出补齐至 %s（%d 行）。注意：导出为未复权、"
                "换手率是流通股本反推的近似值（已标记 adjust=dump）。",
                target, stats["rows"],
            )
            return stats
        return None

    # ---------------- 每日增量 / 补数 ----------------
    def sync_daily(self, target: date | None = None, workers: int = 4) -> dict:
        """同步到目标交易日。**统一走同花顺全市场导出**（daily-k-10d）。

        历史背景：早先走 baostock/akshare 逐只拉取（workers=4 多进程），
        实测 5263 只 × 4 进程 ≈ 10~26 分钟、且经常被限流/封禁。现在改为同花顺
        1 次请求下载整库 Parquet，秒级完成。dump 为**未复权**，因此表里所有行
        都是 `adjust='dump'` —— 跨期拼接处理见 `rebuild_daily.py` 的注释。
        """
        self.ensure_base()
        target = target or self._safe_end_date()

        latest = self.storage.latest_trade_date()
        if latest is not None and latest >= target:
            logger.info("行情数据已是最新（%s），无需增量", latest)
            return {"tasks": 0, "rows": 0, "empty": 0, "failed": 0}

        dump = self.sync_from_dump_fallback(target)
        if dump:
            return {
                "tasks": 0,
                "rows": int(dump.get("rows", 0)),
                "empty": 0,
                "failed": 0,
                "via": "hithink_dump",
                "dates": dump.get("dates", []),
            }
        # dump 失败：立即报错退出，**不再降级到 baostock/akshare**（已下线）。
        # 那条降级路径不仅慢，而且历史教训是它的"5005 错误"能把几天的数据全部
        # 静默抹成空。这里宁愿重试也不要静默走空数据。
        logger.error(
            "同花顺日K导出获取失败，本次采集已取消。请稍后重跑：\n"
            "  排查：检查 HITHINK_FINANCE_API_KEY / 网络 / 同花顺配额；\n"
            "        跑 python scripts\\probe_hithink.py 看 4 个接口连通性。"
        )
        return {
            "tasks": 0, "rows": 0, "empty": 0, "failed": 0,
            "aborted": True, "reason": "hithink_dump_failed",
            "target": str(target),
        }

    # ---------------- 指数 ----------------
    def sync_index(self, start: date | None = None) -> int:
        """同步指数日线，用于市场状态识别。"""
        self.ensure_base()
        index_codes = self.cfg.get("data.index_codes", []) or []
        if not index_codes:
            return 0

        end = self._safe_end_date()
        source = self._open_source()
        total = 0
        for idx_code in index_codes:
            last = self.storage.query_value(
                "SELECT MAX(date) FROM dwd_index_bar WHERE code = ?", [idx_code]
            )
            s = (last + timedelta(days=1)) if last else (
                start or (date.today() - timedelta(days=int(365.25 * int(self.cfg.get("data.backfill_years", 2)))))
            )
            if s > end:
                continue
            try:
                df = source.fetch_index_daily(idx_code, s, end)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] 指数同步失败: %s", idx_code, exc)
                continue
            if df is None or df.empty:
                continue
            # 强制回填索引代码：不同数据源/回退分支返回的 code 格式并不一致。
            # 实测踩过：库里出现了裸数字 "399001"（各 1 行）与项目约定的
            # "sz.399001"（486 行）并存 —— 同一指数变成两套互不覆盖的记录，
            # 而市场状态与「相对指数超额收益」都可能取到只有一天数据的那一套。
            # 在这里统一收口，比逐个数据源修更可靠（数据源会换、回退分支会增加）。
            df = df.copy()
            df["code"] = idx_code
            total += self.storage.upsert_df(df, "dwd_index_bar")
        logger.info("指数同步完成，入库 %d 行", total)
        return total

    # ---------------- 统一入口 ----------------
    def ensure_fresh(self, workers: int = 4) -> dict:
        """每日任务启动时调用：检测并补齐所有缺口。"""
        self.ensure_base()
        missing = self.calendar.missing_open_dates()
        if missing:
            logger.info("检测到 %d 个交易日数据缺失：%s ~ %s", len(missing), missing[0], missing[-1])

        stats_daily = self.sync_daily(workers=workers)
        stats_index = {"rows": self.sync_index()}
        return {"missing_days": len(missing), **stats_daily, "index_rows": stats_index["rows"]}

    # ---------------- 状态 ----------------
    def status(self) -> dict:
        latest = self.storage.latest_trade_date()
        # 用守卫后的日期：盘中查询时不应显示「今天还没数据」这种误导性结论
        target, skipped_today = self.calendar.resolve_data_date()
        total = int(self.storage.table_count("dim_stock"))
        covered = int(
            self.storage.query_value(
                "SELECT COUNT(DISTINCT code) FROM dwd_daily_bar", default=0
            )
            or 0
        )
        empty = len(self._empty_marked())
        return {
            "stocks_total": total,
            "stocks_covered": covered,
            "stocks_empty": empty,
            "coverage": f"{covered / total * 100:.1f}%" if total else "0%",
            "bars": self.storage.table_count("dwd_daily_bar"),
            "index_bars": self.storage.table_count("dwd_index_bar"),
            "calendar_days": self.storage.table_count("trade_calendar"),
            "latest_bar_date": str(latest) if latest else None,
            "latest_trade_date": str(target) if target else None,
            "skipped_today": str(skipped_today) if skipped_today else None,
            "data_ready_time": self.calendar.data_ready_time().strftime("%H:%M"),
            "missing_days": len(self.calendar.missing_open_dates()),
            "feature_rows": self.storage.table_count("dws_feature"),
            "regime_rows": self.storage.table_count("dws_market_regime"),
            "recommend_rows": self.storage.table_count("ads_recommend"),
            "review_rows": self.storage.table_count("ads_review"),
            "last_backfill_at": self.storage.get_state("collect", "backfill_finished_at"),
        }
