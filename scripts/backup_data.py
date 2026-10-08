# -*- coding: utf-8 -*-
"""数据备份。

为什么必须有：
DuckDB 主库里是**唯一且不可再生**的资产 —— 历史日线、每日推荐、复盘结果、回测明细。
策略代码可以重写，但积累下来的行情数据与真实推荐记录买不回来。

备份内容与取舍：

| 内容 | 可再生 | 说明 |
|---|---|---|
| `data/astock.duckdb` | 否 | 主库：行情 / 因子 / 推荐 / 复盘 / 回测 |
| `skills/` | 否 | 策略档案与参数变更史（经验沉淀，最有价值） |
| `config/settings.yaml` | 否 | 调参结果 |
| `data/reports/` | 否 | 每日报告存档 |
| `data/backtest/` | 半可 | 回测报告与明细（重跑一次要 30 分钟） |
| `data/serving/` | 是 | 展示层快照，可由 export 重建，但很小 |
| `.env` | — | **故意排除**：含 API Key，不随备份扩散 |
| `data/logs/` | — | 排除：体积大、价值低 |

用法：
    python scripts/backup_data.py                       # 备份到 data/backups/
    python scripts/backup_data.py --dest E:\\astock_backup
    python scripts/backup_data.py --keep-months 12      # 月度归档保留 12 个月
    python scripts/backup_data.py --list                # 查看已有备份
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# (相对路径, 说明, 类型)
BACKUP_ITEMS: list[tuple[str, str, str]] = [
    ("data/astock.duckdb", "主库（行情/因子/推荐/复盘/回测）", "file"),
    ("skills", "策略 Skill 库（档案 + 参数变更史）", "dir"),
    ("config/settings.yaml", "策略参数配置", "file"),
    ("data/reports", "每日报告存档", "dir"),
    ("data/backtest", "回测报告与明细", "dir"),
    ("data/serving", "展示层快照", "dir"),
]


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def check_db_state(db_path: Path) -> tuple[bool, str]:
    """探测主库是否正被占用。

    为什么必须先探测：DuckDB 是单文件存储，若有写进程正在运行，
    直接复制文件会得到**看似完整、实际损坏**的副本 ——
    这种"备份"比没有备份更危险，因为它只会在真正需要恢复时才暴露问题。
    """
    if not db_path.exists():
        return False, "主库文件不存在"
    try:
        import duckdb

        con = duckdb.connect(str(db_path), read_only=True)
        try:
            n = con.execute("SELECT COUNT(*) FROM dwd_daily_bar").fetchone()[0]
        finally:
            con.close()
        return True, f"可读，日线 {n} 行"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:100]}"


def verify_backup(db_file: Path, expected: int | None) -> tuple[bool, str]:
    """校验备份副本真的能打开并查到数据。

    不做这一步的话，备份可能是个损坏文件而无人知晓 ——
    定期"成功"的备份只有在灾难恢复那天才会被验证，那就太晚了。
    """
    if not db_file.exists():
        return False, "副本文件缺失"
    try:
        import duckdb

        con = duckdb.connect(str(db_file), read_only=True)
        try:
            bars = con.execute("SELECT COUNT(*) FROM dwd_daily_bar").fetchone()[0]
            stocks = con.execute("SELECT COUNT(*) FROM dim_stock").fetchone()[0]
            # 2026-10-08：原先统计的 ads_recommend 已随主链路删除，新建库里根本不存在，
            # 会让这条校验**直接抛异常**（表现为"副本无法打开"）。改统计影子信号。
            recs = con.execute("SELECT COUNT(*) FROM ads_shadow_pick").fetchone()[0]
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        return False, f"副本无法打开：{type(exc).__name__}: {str(exc)[:100]}"

    if expected is not None and bars != expected:
        return False, f"行数不一致：源 {expected} / 副本 {bars}"
    return True, f"校验通过（日线 {bars} / 股票 {stocks} / 推荐 {recs}）"


def list_backups(dest_root: Path) -> int:
    if not dest_root.exists():
        print(f"备份目录不存在：{dest_root}")
        return 0
    items = sorted((d for d in dest_root.iterdir() if d.is_dir()), reverse=True)
    if not items:
        print(f"暂无备份：{dest_root}")
        return 0
    print("=" * 72)
    print(f"  已有备份（{dest_root}）")
    print("=" * 72)
    for d in items:
        manifest = d / "manifest.json"
        size = dir_size(d)
        note = ""
        if manifest.exists():
            try:
                m = json.loads(manifest.read_text(encoding="utf-8"))
                note = f"日线 {m.get('bars')} 行，{m.get('files')} 项"
            except (OSError, ValueError):
                note = "manifest 损坏"
        print(f"  {d.name}　{human(size):>9}　{note}")
    print("=" * 72)
    print(f"  共 {len(items)} 份")
    return 0


def prune(dest_root: Path, keep_newest: int = 1, keep_months: int = 6) -> list[str]:
    """按月轮转：保留**最新 N 份** + 最近 M 个月的**月度归档各 1 份**。

    为什么不用「保留最近 K 份」（原实现是 `items[keep:]`）：备份每周 1 份、
    每份约 1.4 GB，按份数保留会让占用线性增长（默认 5 份 ≈ 7 GB），
    而且**没有长期视角** —— 半年前的检查点留不下，上周的却囤了 5 份。
    按月归档的语义更贴合实际需求：**最近的随时能回滚，历史每月留一个检查点**。
    """
    items = sorted((d for d in dest_root.iterdir() if d.is_dir()), reverse=True)
    if not items:
        return []
    keep: set[Path] = set(items[: max(1, keep_newest)])
    months: set[str] = set()
    for d in items:                      # 名称倒序 = 时间倒序（YYYYMMDD_HHMMSS）
        month = d.name[:6]
        if not month.isdigit():          # 非日期命名的目录（如 pre_finance_xxx）
            keep.add(d)                  # 视为人工归档，不动它
            continue
        if month in months:
            continue                     # 该月已留过一份
        months.add(month)
        keep.add(d)
        if len(months) >= max(1, keep_months):
            break
    removed: list[str] = []
    for d in items:
        if d not in keep:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)
    return removed



def main() -> int:
    parser = argparse.ArgumentParser(description="astock_ai 数据备份")
    parser.add_argument("--dest", default=None, help="备份根目录（默认 <项目>/data/backups）")
    parser.add_argument("--keep-newest", type=int, default=1,
                        help="无条件保留最新的 N 份，默认 1")
    parser.add_argument("--keep-months", type=int, default=6,
                        help="每月各保留 1 份月度归档，默认保留最近 6 个月")
    parser.add_argument("--list", action="store_true", help="只看已有备份，不执行备份")
    parser.add_argument("--allow-locked-db", action="store_true",
                        help="主库被占用时仍强行复制（危险，不推荐）")
    args = parser.parse_args()

    dest_root = Path(args.dest) if args.dest else (ROOT / "data" / "backups")
    if not dest_root.is_absolute():
        dest_root = ROOT / dest_root

    if args.list:
        return list_backups(dest_root)

    print("=" * 72)
    print(f"  数据备份　{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  目标目录：{dest_root}")
    print("=" * 72)

    db_path = ROOT / "data" / "astock.duckdb"
    db_ok, db_msg = check_db_state(db_path)
    print(f"  主库状态：{db_msg}")

    if not db_ok and not args.allow_locked_db:
        print()
        print("  ✖ 主库当前不可读（很可能有采集/回测任务在运行）。")
        print("    此时复制会得到**看似完整、实际损坏**的副本，因此已中止。")
        print("    处理：等任务结束后重跑，或先查看状态：python -m astock.cli status --snapshot")
        print("    确实要继续可加 --allow-locked-db（不推荐）。")
        return 1

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = dest_root / stamp
    target.mkdir(parents=True, exist_ok=True)

    print()
    print(f"  {'内容':<12}{'大小':>10}   说明")
    print("-" * 72)
    copied = 0
    total_bytes = 0
    for rel, desc, kind in BACKUP_ITEMS:
        src = ROOT / rel
        if not src.exists():
            print(f"  {rel:<12}{'-':>10}   跳过（不存在）")
            continue
        size = dir_size(src)
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if kind == "dir":
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        copied += 1
        total_bytes += size
        print(f"  {rel:<12}{human(size):>10}   {desc}")

    print("-" * 72)
    print(f"  合计 {copied} 项，{human(total_bytes)}")

    # ---- 校验 ----
    print()
    print("  校验副本：")
    db_file = target / "data" / "astock.duckdb"
    ok = True
    bars = None
    if db_file.exists():
        import duckdb  # noqa: E402

        try:
            con = duckdb.connect(str(db_path), read_only=True)
            bars = con.execute("SELECT COUNT(*) FROM dwd_daily_bar").fetchone()[0]
            con.close()
        except Exception:  # noqa: BLE001
            bars = None
        good, msg = verify_backup(db_file, bars)
        ok = good
        print(f"    {'✔' if good else '✖'} 主库副本：{msg}")

    if not ok:
        print()
        print("  ✖ 校验未通过，已删除本次备份（避免留下不可用的备份冒充有效备份）。")
        shutil.rmtree(target, ignore_errors=True)
        return 1

    (target / "manifest.json").write_text(
        json.dumps(
            {
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "bars": bars,
                "files": copied,
                "bytes": total_bytes,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # ---- 轮转（按月归档：最新 N 份 + 每月 1 份）----
    removed = prune(dest_root, args.keep_newest, args.keep_months)
    if removed:
        print(f"  已清理旧备份 {len(removed)} 份（保留最新 {args.keep_newest} 份 + "
              f"最近 {args.keep_months} 个月的月度归档）：{'、'.join(removed)}")

    print()
    print(f"  ✔ 备份完成：{target}")
    if "backups" in str(dest_root):
        print()
        print("  ⚠ 提醒：备份与主库在**同一块磁盘**上，无法抵御磁盘损坏。")
        print("    建议定期把整个目录复制到移动硬盘或网盘：")
        print(r"      python scripts\backup_data.py --dest E:\astock_backup")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
