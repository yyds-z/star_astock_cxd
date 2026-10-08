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
from astock.review.attribution import CATEGORIES, Attributor, build_prompt  # noqa: E402
from astock.storage.db import SCHEMA_FILE, Storage  # noqa: E402

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
        ("dws_finance_metrics",
         HithinkCollector._finance_rows("600519", {"income": income,
                                                   "balance": balance,
                                                   "cashflow": cashflow})),
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


# ---------------- 2. 前视偏差与单位 ----------------
def test_report_date_stored() -> None:
    """财务必须存披露日：报告期末 ≠ 财报公开日。"""
    income = [{"period_end_ms": ms(date(2026, 6, 30)),
               "report_date_ms": ms(date(2026, 8, 29)),
               "fiscal_year": 2026, "fiscal_period": "Q2",
               "operating_income": 1e9, "parent_holder_net_profit": 1.8e8}]
    row = HithinkCollector._finance_rows("600519", {"income": income})[0]
    check("period_end 正确", row["period_end"] == date(2026, 6, 30))
    check("report_date 正确（≠ 报告期末）", row["report_date"] == date(2026, 8, 29))


# test_auction_units 已随 dwd_auction 表与 _auction_rows 一并移除（2026-09-30）。
# 若将来恢复竞价链路，按 git 历史取回本用例即可（要点：竞价成交量单位是「手」、
# 涨跌幅已是百分数原值，不做二次换算）。


# ---------------- 3. 财务口径 ----------------
def test_finance_yoy() -> None:
    """同比只在同 fiscal_period 之间算。"""
    income = [
        {"period_end_ms": ms(date(2026, 6, 30)), "report_date_ms": ms(date(2026, 8, 1)),
         "fiscal_year": 2026, "fiscal_period": "Q2", "operating_income": 1000.0,
         "parent_holder_net_profit": 180.0},
        {"period_end_ms": ms(date(2026, 3, 31)), "report_date_ms": ms(date(2026, 4, 28)),
         "fiscal_year": 2026, "fiscal_period": "Q1", "operating_income": 500.0,
         "parent_holder_net_profit": 90.0},
        {"period_end_ms": ms(date(2025, 6, 30)), "report_date_ms": ms(date(2025, 8, 1)),
         "fiscal_year": 2025, "fiscal_period": "Q2", "operating_income": 800.0,
         "parent_holder_net_profit": 150.0},
    ]
    # 用 (财年, 报告期) 定位：同一个 fiscal_period 会跨年份出现多次，
    # 只按 fiscal_period 取会拿到上一年的那一行，断言就失去意义
    rows = {
        (r["fiscal_year"], r["fiscal_period"]): r
        for r in HithinkCollector._finance_rows("600000", {"income": income})
    }
    q2 = rows[(2026, "Q2")]
    check("Q2 营收同比 = 25%（对比去年同期 Q2，而非上一期 Q1）",
          abs(q2["revenue_yoy"] - 25.0) < 1e-6, str(q2["revenue_yoy"]))
    check("Q2 净利同比 = 20%", abs(q2["profit_yoy"] - 20.0) < 1e-6, str(q2["profit_yoy"]))
    check("Q1 无去年同期数据 → 同比为 None", rows[(2026, "Q1")]["revenue_yoy"] is None)


def test_finance_negative_base() -> None:
    """上期亏损时同比必须为 None —— 负基数硬算会得出「亏损扩大显示正增长」。"""
    income = [
        {"period_end_ms": ms(date(2026, 6, 30)), "fiscal_year": 2026,
         "fiscal_period": "Q2", "operating_income": 1000.0, "parent_holder_net_profit": 50.0},
        {"period_end_ms": ms(date(2025, 6, 30)), "fiscal_year": 2025,
         "fiscal_period": "Q2", "operating_income": 900.0, "parent_holder_net_profit": -100.0},
    ]
    row = HithinkCollector._finance_rows("600000", {"income": income})[0]
    check("上期净利为负 → 净利同比 None", row["profit_yoy"] is None)
    check("上期营收为正 → 营收同比正常计算", abs(row["revenue_yoy"] - 100.0 / 9) < 1e-6)


def test_finance_ratios() -> None:
    """ROE 与资产负债率由三表合并计算。"""
    income = [{"period_end_ms": ms(D), "fiscal_year": 2026, "fiscal_period": "Q2",
               "operating_income": 1000.0, "parent_holder_net_profit": 180.0}]
    balance = [{"period_end_ms": ms(D), "assets_total": 5000.0, "total_debt": 2000.0,
                "holder_equity_total": 2500.0}]
    row = HithinkCollector._finance_rows("600000", {"income": income, "balance": balance})[0]
    check("ROE = 归母净利/权益 = 7.2%", abs(row["roe"] - 7.2) < 1e-6, str(row["roe"]))
    check("资产负债率 = 40%", abs(row["debt_ratio"] - 40.0) < 1e-6, str(row["debt_ratio"]))


# ---------------- 4. 复盘归因 ----------------
def test_attribution_metrics() -> None:
    """开盘溢价需从「当日涨跌幅」反推前收 —— 直接用开/收盘价是错的。"""
    row = {"next_open": 12.10, "next_close": 11.80, "next_pct_chg": -1.5}
    premium = Attributor._open_premium(row)
    # preclose = 11.80 / (1 - 0.015) = 11.9797；溢价 = 12.10 / 11.9797 - 1 ≈ +1.00%
    check("次日开盘溢价 ≈ +1.00%（相对前收）", abs(premium - 1.004) < 0.01, str(premium))
    ret = Attributor._buy_return(row)
    check("买入后收益 ≈ -2.48%（相对买入价）", abs(ret + 2.479) < 0.01, str(ret))
    check("缺字段时返回 None",
          Attributor._open_premium({"next_open": None, "next_close": 1.0,
                                   "next_pct_chg": 0.0}) is None)


def test_attribution_prompt() -> None:
    """Prompt 必须列出全部固定类别，否则模型只能自创、无法聚合统计。"""
    prompt = build_prompt({"tier": "short", "primary_strategy": "s1",
                           "market_label": "情绪退潮", "open_premium": 1.0,
                           "ret1": -2.0, "next_pct_chg": -1.0})
    missing = [k for k in CATEGORIES if k not in prompt]
    check("Prompt 含全部类别枚举", not missing, f"缺少：{missing}")


# ---------------- 5.（已移除）因子表与统计工具 ----------------
# 原两项用例随研究产物于 2026-09-30 一并移除：
#   · test_factor_columns_match_schema —— 校验 dws_limit_factor 列序（表与
#     FACTOR_COLUMNS 均已删除）
#   · test_statistics —— 校验 scripts/factor_ic.py 的 Bonferroni 阈值与 IC 统计
#     （脚本已删除）。若将来恢复研究工具，按 git 历史取回即可。


def main() -> int:
    print("=" * 72)
    print("  数据解析回归测试（离线，不联网 / 不查库）")
    print("=" * 72)
    print("\n[1] 字段映射")
    test_row_keys_match_schema()
    test_field_mapping()
    print("\n[2] 前视偏差与单位")
    test_report_date_stored()
    # test_auction_units 已随 dwd_auction 表移除（2026-09-30）
    print("\n[3] 财务口径")
    test_finance_yoy()
    test_finance_negative_base()
    test_finance_ratios()
    print("\n[4] 复盘归因")
    test_attribution_metrics()
    test_attribution_prompt()
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
