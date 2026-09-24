# -*- coding: utf-8 -*-
"""数据资产体检：核对「台账 / 实际库 / 代码引用」三者是否一致。

解决的问题：
数据采集最危险的模式不是「采不到」，而是「采了一堆但不知道有没有用」。
每多一份数据 = 多一份噪声 + 一份维护成本 + 一份出错面，
而判断有用与否必须有统计证据。本工具把这件事变成可执行的例行检查。

它做四件事：
1. **台账核对**：库里有哪些表、台账登记了哪些，找出「绕过台账的采集」；
2. **死数据检测**：扫描代码，找出**只写不读**的表（采了从没人用）；
3. **新鲜度检查**：核心表的最新日期是否落后于最近交易日；
4. **欠验证债**：列出 status=pending 的数据 —— 它们禁止进评分，
   必须给出验证结论（有效 → validated；无效 → deprecated）。

用法：
    python scripts\data_audit.py
    python scripts\data_audit.py --verbose    # 显示读/写代码位置
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import yaml  # noqa: E402

from astock.storage.db import get_storage  # noqa: E402

REGISTRY = ROOT / "config" / "data_registry.yaml"

# 扫描范围：只有这些目录的代码才算"使用数据"
SCAN_DIRS = ("astock", "api", "scripts")

# 应用层产出表（不是采集来的输入，不参与准入检查）
OUTPUT_PREFIXES = ("ads_", "sys_")


def scan_usage(table: str) -> tuple[list[str], list[str]]:
    """扫描代码，返回 (写入位置, 读取位置)。

    判定依据：
      写入 = upsert_df(..., "表名") 或 INSERT [OR REPLACE] INTO 表名
      读取 = FROM 表名 / JOIN 表名
    "只写不读"意味着这份数据采了但从未参与任何计算 —— 纯噪声。
    """
    write_re = re.compile(
        rf'(upsert_df\([^)]*["\']{re.escape(table)}["\']|'
        rf"INSERT\s+(OR\s+REPLACE\s+)?INTO\s+{re.escape(table)}\b)",
        re.IGNORECASE | re.DOTALL,
    )
    read_re = re.compile(rf"\b(FROM|JOIN)\s+{re.escape(table)}\b", re.IGNORECASE)

    writes: list[str] = []
    reads: list[str] = []
    for sub in SCAN_DIRS:
        base = ROOT / sub
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            rel = str(path.relative_to(ROOT)).replace("\\", "/")
            if write_re.search(text):
                writes.append(rel)
            if read_re.search(text):
                reads.append(rel)
    return writes, reads


def main() -> int:
    parser = argparse.ArgumentParser(description="数据资产体检")
    parser.add_argument("--verbose", action="store_true", help="显示读/写代码位置")
    args = parser.parse_args()

    if not REGISTRY.exists():
        print(f"台账不存在：{REGISTRY}")
        return 1
    registry = yaml.safe_load(REGISTRY.read_text(encoding="utf-8")) or {}
    datasets = registry.get("datasets") or []
    by_table = {d["table"]: d for d in datasets if d.get("table")}

    storage = get_storage()
    db_tables = [
        r[0]
        for r in storage.query_df(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' ORDER BY 1"
        ).itertuples(index=False)
    ]
    last_trade_day = storage.query_value(
        "SELECT MAX(date) FROM trade_calendar WHERE is_open = TRUE AND date <= ?",
        [date.today()],
    )

    print("=" * 96)
    print("  数据资产体检")
    print("=" * 96)
    print(f"  台账：{REGISTRY.relative_to(ROOT)}　已登记 {len(datasets)} 项")
    print(f"  库内表：{len(db_tables)} 张　最近交易日：{last_trade_day}")
    print()

    dead: list[str] = []
    pending: list[str] = []
    stale: list[str] = []
    unregistered: list[str] = []

    print(f"  {'表名':<24}{'用途':<10}{'状态':<12}{'行数':>10}{'最新':>12}  判定")
    print("-" * 96)
    for d in datasets:
        table = d.get("table", "")
        if table.startswith("（"):
            continue  # 未采集的数据源
        purpose = str(d.get("purpose", "-"))
        status = str(d.get("status", "-"))
        writes, reads = scan_usage(table)

        rows = "-"
        latest = "-"
        if table in db_tables:
            n = storage.query_value(f"SELECT COUNT(*) FROM {table}", default=0)
            rows = f"{int(n):,}"
            cols = [c[0] for c in storage.query_df(f"DESCRIBE {table}").itertuples(index=False)]
            dcol = next((c for c in ("date", "period_end") if c in cols), None)
            if dcol:
                v = storage.query_value(f"SELECT MAX({dcol}) FROM {table}")
                latest = str(v)[:10] if v else "-"

        # ---- 判定（多条判定可叠加，不互相覆盖）----
        # 「只写不读」是独立事实，即使该表已被标记废弃也要显示出来，
        # 否则无法解释「为什么判定它废弃」。
        notes: list[str] = []
        write_only = bool(table in db_tables and not reads
                          and purpose not in ("core", "derived"))
        if table not in db_tables:
            notes.append("尚未建表")
        else:
            if status == "deprecated":
                notes.append("建议停止采集" + ("（依据：只写不读）" if write_only else ""))
            elif status == "pending":
                notes.append("⏳ 欠验证（禁止进评分）")
                pending.append(table)
            elif status == "validated":
                notes.append("✔ 已验证")
            elif status == "display":
                notes.append("— 展示/辅助")
            else:
                notes.append("✔ 基础数据")
            if write_only:
                # 汇总口径与表格文案必须一致：否则出现
                # 「表里写着只写不读、汇总却说 0 个」的自相矛盾
                dead.append(table)
                if status != "deprecated":
                    notes.append("⚠ 死数据（只写不读）")
            if d.get("exception"):
                notes.append("⚠ 规范例外")

        if purpose in ("core", "regime") and latest != "-" and last_trade_day:
            try:
                lag = (last_trade_day - date.fromisoformat(latest)).days
                if lag > 7:
                    notes.append(f"⚠ 落后 {lag} 天")
                    stale.append(table)
            except ValueError:
                pass

        print(f"  {table:<24}{purpose:<10}{status:<12}{rows:>10}{latest:>12}  "
              + "；".join(notes))
        if args.verbose:
            print(f"      写入：{', '.join(writes) or '无'}")
            print(f"      读取：{', '.join(reads) or '无'}")

    # ---- 绕过台账的采集 ----
    for t in db_tables:
        if t in by_table or t.startswith(OUTPUT_PREFIXES):
            continue
        unregistered.append(t)

    print("-" * 96)
    print("  汇总")
    print("-" * 96)
    print(f"  只写不读（采了但没有任何代码读取）：{len(dead)}　"
          f"{'、'.join(dead) if dead else '无'}")
    print(f"  欠验证（禁止进评分）：{len(pending)}　{'、'.join(pending) if pending else '无'}")
    print(f"  数据滞后：{len(stale)}　{'、'.join(stale) if stale else '无'}")
    print(f"  台账外新增表：{len(unregistered)}　{'、'.join(unregistered) if unregistered else '无'}")

    print()
    print("  规范提醒：")
    print("    1. 新增任何数据源，必须先在 config/data_registry.yaml 登记，")
    print("       并写出**可执行的验证方法**（写不出来的不许进评分）。")
    print("    2. status=pending 的数据只能展示，不得参与 Scoring/Regime/Filter。")
    print("    3. 每次回测后重跑本工具，把 pending 逐个给出结论：")
    print("       有效 → 改 validated（并在台账写明证据）；无效 →改 deprecated 并停止采集。")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    sys.exit(main())
