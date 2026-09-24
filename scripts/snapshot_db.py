# -*- coding: utf-8 -*-
"""主库只读副本：让开发/分析/迭代不再被写任务的独占锁挡住。

------------------------------------------------------------------
为什么需要它
------------------------------------------------------------------
DuckDB 是**嵌入式单进程**列存库，跨进程锁是**文件级独占**：
一个写任务（回测 26 分钟、财务回填 11 小时）启动后，其他进程
**连"只读打开"都做不到** —— 于是"回测在跑"就等于"整个系统不能碰"，
迭代速度被严重拖慢。

解法不是改数据库（换 PostgreSQL 要改 SQL 方言、丢掉列存性能，
对单人本地系统不划算），而是**把"读"从主库上摘出去**：
分析一律读副本，主库只留给写任务。

------------------------------------------------------------------
纪律（很关键）
------------------------------------------------------------------
· **写**（daily / backfill / 建派生表）→ 主库
· **读**（IC 检验、因子研究、临时排查、写分析脚本时反复试错）→ 副本
副本是某一时刻的快照，可能滞后几分钟到几小时。对历史数据分析无影响；
若要基于"刚刚产生的新数据"下结论，先刷新副本。

用法：
    python scripts\\snapshot_db.py           # 刷新副本
    python scripts\\snapshot_db.py --check   # 只看副本状态，不刷新
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb  # noqa: E402

from astock.config import get_config  # noqa: E402

# 副本里必须存在的表：用来验证副本是"完整可用"而不是复制到一半
CHECK_TABLES = ["dim_stock", "dwd_daily_bar", "dws_feature", "ads_recommend"]


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _try_open_readonly(path: Path) -> tuple[bool, str]:
    """能否只读打开。打不开＝有写进程正持有独占锁。

    这是**唯一可靠的判断方式**：文件时间戳、进程名都不足以说明问题，
    而"复制一个正被写入的库"会得到一个内部不一致的文件，
    它还能正常打开、正常查询，只是数据错乱 —— 是最难排查的一类问题。
    """
    try:
        con = duckdb.connect(str(path), read_only=True)
        con.close()
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]


def _counts(path: Path, tables: list[str]) -> dict[str, int]:
    con = duckdb.connect(str(path), read_only=True)
    out: dict[str, int] = {}
    try:
        for t in tables:
            try:
                out[t] = int(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            except Exception:  # noqa: BLE001
                out[t] = -1
    finally:
        con.close()
    return out


def status() -> int:
    cfg = get_config()
    src = Path(cfg.duckdb_path)
    dst = src.with_name("astock_readonly.duckdb")
    if not dst.exists():
        print(f"副本尚未创建：{dst}")
        print("执行 `python scripts\\snapshot_db.py` 生成。")
        return 0
    age_min = (time.time() - dst.stat().st_mtime) / 60
    print(f"副本：{dst}")
    print(f"大小：{_human(dst.stat().st_size)}　"
          f"生成于 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(dst.stat().st_mtime))}"
          f"（{age_min:.0f} 分钟前）")
    ok, err = _try_open_readonly(dst)
    print(f"可只读打开：{'是' if ok else '否 — ' + err}")
    if ok:
        for t, n in _counts(dst, CHECK_TABLES).items():
            print(f"    {t:<20}{n:>12,}" + ("  ⚠ 表不存在" if n < 0 else ""))
    return 0


def main() -> int:
    if "--check" in sys.argv:
        return status()

    cfg = get_config()
    src = Path(cfg.duckdb_path)
    dst = src.with_name("astock_readonly.duckdb")
    if not src.exists():
        print(f"[!] 主库不存在：{src}")
        return 1

    ok, err = _try_open_readonly(src)
    if not ok:
        print("[!] 主库正被写任务占用，**现在不能复制**：")
        print(f"    {err}")
        print()
        print("    为什么不能直接复制：那样得到的是「写到一半」的库。")
        print("    它能正常打开、正常查询，但数据内部不一致 —— 是最难察觉的一类错误。")
        print("    处理：等该任务结束（python scripts\\monitor.py 看进度）后重跑本命令。")
        return 2

    # DuckDB 未正常关闭时会留下 .wal，此时主库文件本身并不完整，
    # 必须连 .wal 一起带走，否则副本会缺少最后一批已提交的数据。
    wal = Path(str(src) + ".wal")
    if wal.exists():
        print(f"[i] 检测到 {wal.name}（主库未正常关闭），将一并复制")

    t0 = time.monotonic()
    src_counts = _counts(src, CHECK_TABLES)
    shutil.copy2(src, dst)
    if wal.exists():
        shutil.copy2(wal, Path(str(dst) + ".wal"))

    # 复制后必须校验：如果副本与主库的行数不一致，说明复制过程中
    # 主库被动过（或磁盘写失败），这种副本必须丢弃而不是拿去分析。
    dst_counts = _counts(dst, CHECK_TABLES)
    mismatch = {t: (src_counts[t], dst_counts[t]) for t in CHECK_TABLES
                if src_counts.get(t) != dst_counts.get(t)}

    print(f"副本已生成：{dst}")
    print(f"  大小 {_human(dst.stat().st_size)}　耗时 {time.monotonic() - t0:.1f}s")
    for t in CHECK_TABLES:
        flag = "" if src_counts.get(t) == dst_counts.get(t) else "  ⚠ 行数不一致"
        print(f"    {t:<20}{dst_counts.get(t, -1):>12,}{flag}")
    if mismatch:
        print("\n[!] 校验失败，副本不可信，请删除后重试：", mismatch)
        return 3
    print("\n校验通过。此后分析类工作（IC 检验、因子研究、临时排查）请一律连副本：")
    print(f"    duckdb.connect(r'{dst}', read_only=True)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
