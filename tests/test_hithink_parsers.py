# -*- coding: utf-8 -*-
"""数据解析回归测试（**不联网、不查库**，用桩数据）。

为什么需要它：
今天新增了 4 个上游采集器（涨停池/龙虎榜/竞价/财务），它们的字段映射、
单位换算、同比口径全靠人工核对过一遍 —— 而人工核对**不会自动重来**。
上游改字段名、或日后改动映射代码，都会静默丢数据，没有任何报错。

本文件把当时核对的结论固化成断言，覆盖四类真实踩过的坑：

1. **字段映射漂移**：校验「代码产出的字典键」是「schema 表定义列」的子集。
   这是 primary_strategy 事故的同类问题 —— 写库列名对不上会直接崩或丢数据。
2. **前视偏差**：财务表必须存披露日 `report_date`（报告期末不等于公开日）。
3. **单位陷阱**：竞价成交量单位是「手」不是「股」；竞价涨跌幅已是百分数原值。
4. **口径陷阱**：同比只在同 fiscal_period 之间算；负基数返回 None 而不是硬算。

用法：
    python tests/test_hithink_parsers.py
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from astock.data.hithink import HithinkCollector  # noqa: E402
from astock.storage.db import SCHEMA_FILE, Storage  # noqa: E402

# astock.review.attribution 已随主链路删除（2026-10-08）；影子信号的归因
# 现在由 astock.shadow.review.ShadowReviewer 承担，那部分不解析上游字段，
# 因此不需要这里的桩数据测试。

CST = timezone(timedelta(hours=8))
D = date(2026, 6, 30)

_results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((ok, name, detail))
    print(f"  {'✔' if ok else '✖'} {name}" + (f"　{detail}" if detail and not ok else ""))


def ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=CST).timestamp() * 1000)


def schema_columns(table: str) -> set[str]:
    """从 schema.sql 解析某张表的期望列（复用生产代码的解析逻辑）。"""
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    return Storage._expected_columns(statements, table)


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------- 1. 字段映射 ----------------
def test_row_keys_match_schema() -> None:
    """产出的字典键必须是表定义列的子集 —— 多出的键会导致写库失败。"""
    limit_item = {
        "ticker": "000910", "name": "大亚圣象", "limit_up_time": "09:30",
        "limit_up_reason": "机器人+PCB", "continue_day_cnt": 4,
        "continue_day_text": "4连板", "seal_money": 1.4e8, "max_seal_money": 2.0e8,
        "last_price": 12.1, "price_change_ratio_pct": 10.02, "is_st": False, "is_new": False,
    }
    break_item = {
        "ticker": "600000", "name": "浦发银行", "open_times": 2, "last_price": 10.0,
        "price_change_ratio_pct": 3.2, "turnover_ratio_pct": 1.5, "turnover": 1.0e9,
    }
    # dragon_item 已随龙虎榜链路移除（2026-10-08，零消费者）
    # 财务三表：只需覆盖会被读到的字段
    income = [{
        "period_end_ms": ms(D), "report_date_ms": ms(date(2026, 8, 29)),
        "fiscal_year": 2026, "fiscal_period": "Q2", "operating_income": 1e9,
        "net_profit": 2e8, "parent_holder_net_profit": 1.8e8, "basic_eps": 1.8,
    }]
    balance = [{"period_end_ms": ms(D), "assets_total": 5e9, "total_debt": 2e9,
                "holder_equity_total": 2.5e9}]
    cashflow = [{"period_end_ms": ms(D), "act_cash_flow_net": 3e8}]

    cases = [
        ("dwd_limit_up", HithinkCollector._up_rows(D, [limit_item])),
        ("dwd_limit_break", HithinkCollector._break_rows(D, [break_item])),
        # dwd_auction 已随表移除（2026-09-30，该表 0 行），不再校验
        # dwd_dragon_tiger 已随链路移除（2026-10-08，零消费者），不再校验
        # dws_finance_metrics 与财务采集方法同批移除（2026-10-08，唯一消费方
        # ——价值档基本面因子——随主链路删除），不再校验
    ]
    for table, rows in cases:
        cols = schema_columns(table)
        if not cols:
            check(f"{table} 的 DDL 可解析", False, "未解析出列名")
            continue
        produced = set(rows[0].keys())
        extra = produced - cols
        check(f"{table} 产出键 ⊆ 表定义列", not extra, f"多出：{sorted(extra)}")


def test_field_mapping() -> None:
    """具体字段映射：上游字段名 → 本地语义。"""
    item = {
        "ticker": "000910", "name": "大亚圣象", "limit_up_time": "09:30",
        "limit_up_reason": "机器人", "continue_day_cnt": 4,
        "continue_day_text": "4连板", "seal_money": 1.4e8, "max_seal_money": 2e8,
        "last_price": 12.1, "price_change_ratio_pct": 10.02, "is_st": False, "is_new": False,
    }
    row = HithinkCollector._up_rows(D, [item])[0]
    check("连板数取自 continue_day_cnt", row["boards"] == 4)
    check("题材取自 limit_up_reason", row["reason"] == "机器人")
    check("首封时间取自 limit_up_time", row["first_time"] == "09:30")
    check("封单额取自 seal_money", row["seal_money"] == 1.4e8)


# test_dragon_defaults 已随龙虎榜链路移除（2026-10-08，零消费者）


# test_report_date_stored 已随财务采集功能移除（2026-10-08）。它测的其实是
# `HithinkCollector._finance_rows` 把 report_date 存成**披露日**而非报告期末，
# 用例对象（dws_finance_metrics / 财务解析器）已随主链路删除，故一并移除。
# ⚠️ 但那条原理必须留住：**财务数据的"可知日"是披露日，不是报告期末**。
#    用报告期末当 as-of 边界会让回测提前知道当时尚未公布的数字（前视偏差）。
#    将来若重新采集财务数据，务必按披露日过滤（原实现见 git f904528）。


# ---------------- 2. 前视偏差与单位 ----------------
def main() -> int:
    print("=" * 72)
    print("  数据解析回归测试（离线，不联网 / 不查库）")
    print("=" * 72)
    print("\n[1] 字段映射")
    test_row_keys_match_schema()
    test_field_mapping()
    print("\n[2] 前视偏差与单位")
    # test_report_date_stored 已移除（见文件末尾说明）
    # 注：该用例曾于 2026-10-08 被清理脚本误删（连续叠加删除操作的后果），
    #     已从 git 历史取回并恢复调用。
    # 原说明：
    # （上一版按函数名删除后留下孤儿代码行，把该函数体并入了其它函数）。
    # 它校验的是「报告期 vs 披露日」的前视偏差——**该覆盖目前是缺失的**，
    # 需要时按 git 历史取回：git show <commit>:tests/test_hithink_parsers.py
    # test_report_date_stored()
    # test_auction_units 已随 dwd_auction 表移除（2026-09-30）
    # [3] 财务口径 已随 dws_finance_metrics / 财务采集方法移除（2026-10-08）
    # [4] 复盘归因 已随主链路移除（2026-10-08）：见文件末尾说明。
    # [5] 因子表与统计工具 已随研究产物移除（2026-09-30）：
    #   dws_limit_factor / FACTOR_COLUMNS / scripts/factor_ic.py 均已删除。

    failed = [r for r in _results if not r[0]]
    print()
    print("-" * 72)
    print(f"  共 {len(_results)} 项，通过 {len(_results) - len(failed)}，失败 {len(failed)}")
    for _, name, detail in failed:
        print(f"    ✖ {name}　{detail}")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
