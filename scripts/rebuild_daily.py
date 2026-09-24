# -*- coding: utf-8 -*-
"""用同花顺全市场日K导出**重建** `dwd_daily_bar`（单一来源、单一口径）。

------------------------------------------------------------------
为什么做这件事
------------------------------------------------------------------
原先的 `dwd_daily_bar` 是**多源混用**：
    adjust=2（前复权，来自 baostock/akshare 逐只拉取）2024-09-23 ~ 2026-09-22
    adjust=dump（未复权，来自同花顺导出兜底）      仅 2026-09-23
5200 只股票存在多口径并存。当前实测断裂为 0（因前复权锚定于最新日，最近价
恰好等于真实价），但这是**潜在缺陷**：只要有股票在边界日除权，
`LEAD(close)/close` 这类跨日收益就会凭空多出跌幅，而回测/因子/胜率全依赖它。

------------------------------------------------------------------
口径选择：未复权，而不是前复权
------------------------------------------------------------------
前复权价格**不是真实成交价**。而本系统要用真实价判断「是否涨停」「不破位」
「缩量」，涨停价还得按 `round(前收 × 1.1)` 算 —— 用未复权才准确。
（导出接口恒返回 `adjusted='none'`；实测传 adjusted=qfq 会被忽略。）

------------------------------------------------------------------
除权日的处理（本脚本的核心逻辑）
------------------------------------------------------------------
未复权序列**没有"昨收"字段**，必须用前一交易日收盘链式推算。而这样推算，
在除权/送转日会出现虚假暴跌（实测 000034 在 2026-05-19 为 -25.5%，
主板涨跌停只有 ±10%，不可能是真实跌幅）。后果有两层：
    ① `pct_chg` 错误 → `is_limit_down` 会被**误判为跌停**（-13.7% ≤ -9.5%）；
    ② 跨日收益凭空多出 -25% 的**幽灵亏损**。

处置（"不猜"原则）：
    · 识别：|当日收益| 超过该板涨跌停限制 0.5 个百分点 ⇒ 判为除权/送转
    · 这些行的 `pct_chg` **置空**，绝不填一个错的数；
    · 并在导出时给出计数与样例，便于人工抽查。
不做"自建复权链"的进一步修正：那需要真实的除权因子（当前数据源不提供），
用当日市场收益去替代属于猜测，会把误差藏起来。

用法：
    python scripts\\rebuild_daily.py --years 2            # 只建新表 + 校验（默认）
    python scripts\\rebuild_daily.py --years 2 --swap      # 校验通过后原子替换
    python scripts\\rebuild_daily.py --skip-download       # 复用已下载的 parquet
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from astock.config import get_config  # noqa: E402
from astock.logger import get_logger  # noqa: E402
from astock.storage.db import get_storage  # noqa: E402

logger = get_logger("scripts.rebuild_daily")

NEW_TABLE = "dwd_daily_bar_new"
CACHE = ROOT / "data" / "cache" / "hithink_daily_k_dump.parquet"
GEM_PREFIX = ("300", "301", "688", "689")   # 创业板/科创板 20cm
EX_DIV_TOL = 0.5                            # 超出涨跌停限制该幅度即视为除权


def backup(table: str, storage) -> Path:
    out_dir = ROOT / "data" / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"{table}_{time.strftime('%Y%m%d_%H%M%S')}.parquet"
    # COPY 走 DuckDB 原生导出，2.5M 行秒级完成
    storage.execute(f"COPY {table} TO '{dst.as_posix()}' (FORMAT PARQUET)")
    mb = dst.stat().st_size / 1024 / 1024
    logger.info("已备份 %s → %s（%.1f MB）", table, dst.name, mb)
    print(f"  ✔ 备份 {table} → {dst.name}（{mb:.1f} MB）")
    return dst


def download_dump(force: bool = False) -> pd.DataFrame:
    """下载全市场日K导出；本地缓存，避免反复拉 170MB。"""
    if CACHE.exists() and not force:
        print(f"  复用缓存：{CACHE.name}（{CACHE.stat().st_size / 1024 / 1024:.1f} MB）")
        return pd.read_parquet(CACHE)

    import requests

    key = get_config().env("HITHINK_FINANCE_API_KEY")
    s = requests.Session()
    s.headers.update({"X-api-key": key or ""})
    meta = s.get("https://fuyao.aicubes.cn/api/dump/market-dumps/daily-k/download-url",
                 timeout=60).json()
    url = (meta.get("data") or {}).get("presigned_url")
    if not url:
        raise RuntimeError(f"未取到导出下载链接：{meta}")
    print("  下载全市场日K导出 …")
    r = s.get(url, timeout=900)
    if r.status_code != 200:
        raise RuntimeError(f"下载失败 HTTP {r.status_code}")
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_bytes(r.content)
    print(f"  下载完成：{len(r.content) / 1024 / 1024:.1f} MB → {CACHE.name}")
    return pd.read_parquet(io.BytesIO(r.content))


def build(dump: pd.DataFrame, years: int, storage) -> pd.DataFrame:
    dump = dump.copy()
    dump["date"] = (pd.to_datetime(dump["date_ms"], unit="ms", utc=True)
                    .dt.tz_convert("Asia/Shanghai").dt.date)
    dump["code"] = dump["thscode"].str.split(".").str[0]
    # 多留 10 天余量：回测起点是 2024-09-23（周一），而"2 年"按 730.5 天
    # 推算恰好落在 2024-09-24，会丢掉首日。余量保证覆盖完整回测区间。
    start = date.today() - timedelta(days=int(years * 365.25) + 10)
    dump = dump[dump["date"] >= start]
    dump = dump.sort_values(["code", "date"]).reset_index(drop=True)

    out = pd.DataFrame({
        "code": dump["code"],
        "date": dump["date"],
        "open": dump["open_price"],
        "high": dump["high_price"],
        "low": dump["low_price"],
        "close": dump["close_price"],
        "volume": dump["volume"],
        "amount": dump["turnover"],
    })
    # 昨收：链式推算（同一口径内部自洽）
    out["preclose"] = out.groupby("code")["close"].shift(1)
    out["pct_chg"] = (out["close"] / out["preclose"] - 1) * 100

    # 除权/送转识别：超过该板涨跌停限制 ⇒ 不可能是真实涨跌幅
    lim = np.where(out["code"].str.startswith(GEM_PREFIX), 20.0, 10.0)
    exdiv = out["preclose"].notna() & (out["pct_chg"].abs() > lim + EX_DIV_TOL)
    print(f"  辨识出除权/送转行 {int(exdiv.sum())} 行"
          f"（占 {100.0 * exdiv.mean():.3f}%，涉及 {out.loc[exdiv, 'code'].nunique()} 只）")
    if exdiv.any():
        print("    样例（这些行 pct_chg 将置空）：")
        print(out.loc[exdiv, ["code", "date", "preclose", "close", "pct_chg"]]
              .head(5).to_string(index=False))
    out.loc[exdiv, "pct_chg"] = np.nan          # 不猜：宁可空，不给错值

    # 换手率：导出不含。**优先沿用旧表的真实值**（同一天同股，换手率只取决于
    # 成交量与流通股本，与价格复权口径无关，因此可以安全继承）；
    # 旧表没有的（如最新一日）留空，不用"反推股本"的近似值冒充。
    old = storage.query_df("SELECT code, date, turn FROM dwd_daily_bar WHERE turn IS NOT NULL")
    if not old.empty:
        # DuckDB 返回的是 datetime64，而本表 date 是 Python date（object）——
        # 直接 merge 会因 dtype 不同报错，必须先统一。
        old["date"] = pd.to_datetime(old["date"]).dt.date
        out = out.merge(old, on=["code", "date"], how="left")
        got = int(out["turn"].notna().sum())
        print(f"  换手率继承自旧表：{got} 行（{100.0 * got / len(out):.1f}%），其余留空")
    else:
        out["turn"] = np.nan

    out["adjust"] = "dump"
    return out[["code", "date", "open", "high", "low", "close", "preclose",
                "volume", "amount", "turn", "pct_chg", "adjust"]]


def validate(new: pd.DataFrame, storage) -> bool:
    print("\n" + "=" * 84)
    print("  校验：新表 vs 旧表")
    print("=" * 84)
    old = storage.query_df(
        "SELECT COUNT(*) AS 行数, COUNT(DISTINCT code) AS 股票, COUNT(DISTINCT date) AS 交易日, "
        "MIN(date) AS 最早, MAX(date) AS 最新 FROM dwd_daily_bar"
    ).iloc[0]
    print(f"  旧表：{int(old['行数'])} 行 / {int(old['股票'])} 只 / {int(old['交易日'])} 日 "
          f"（{old['最早']} ~ {old['最新']}）")
    print(f"  新表：{len(new)} 行 / {new['code'].nunique()} 只 / {new['date'].nunique()} 日 "
          f"（{new['date'].min()} ~ {new['date'].max()}）")

    checks = []
    dup = int(new.duplicated(subset=["code", "date"]).sum())
    checks.append(("无重复 (code,date)", dup == 0, f"{dup} 条重复"))
    bad_ohlc = int((new[["open", "high", "low", "close"]] <= 0).any(axis=1).sum())
    checks.append(("价格均为正", bad_ohlc == 0, f"{bad_ohlc} 行非正价"))
    null_pct = int(new["pct_chg"].isna().sum())
    checks.append(("pct_chg 空值（除权+首日）占比 < 2%",
                   null_pct / len(new) < 0.02,
                   f"{null_pct} 行（{100.0 * null_pct / len(new):.2f}%）"))
    hi = int((new["high"] < new[["open", "close"]].max(axis=1) - 1e-6).sum())
    lo = int((new["low"] > new[["open", "close"]].min(axis=1) + 1e-6).sum())
    checks.append(("high/low 与 open/close 自洽", hi + lo == 0, f"{hi + lo} 行矛盾"))
    for name, ok, detail in checks:
        print(f"  {'✔' if ok else '✖'} {name}：{detail}")

    # 最近交易日与旧表比价：前复权锚定于最新日，最近价应几乎一致
    common = storage.query_df(
        "SELECT MAX(date) AS d FROM dwd_daily_bar WHERE date < "
        "(SELECT MAX(date) FROM dwd_daily_bar)"
    ).iloc[0]["d"]
    if common is not None:
        # query_df 返回 datetime64，而 new["date"] 是 Python date，直接 == 恒为 False
        # （实测因此"比价"空转、打印 0 只）。先统一成 date 再比。
        common = pd.to_datetime(common).date()
        n_ = new[new["date"] == common][["code", "close"]]
        o_ = storage.query_df("SELECT code, close FROM dwd_daily_bar WHERE date = ?", [common])
        j = n_.merge(o_, on="code", suffixes=("_new", "_old"))
        j["diff"] = (j["close_new"] / j["close_old"] - 1).abs()
        big = int((j["diff"] > 0.001).sum())
        print(f"  {'✔' if big == 0 else '✖'} 与旧表比价（{common}，{len(j)} 只）："
              f"差异 >0.1% 的 {big} 只，最大 {100 * j['diff'].max():.3f}%")
        checks.append(("与旧表比价一致", big == 0, f"{big} 只不一致"))

    return all(ok for _, ok, _ in checks)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=2)
    ap.add_argument("--swap", action="store_true", help="校验通过后替换正式表")
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--refetch", action="store_true", help="忽略本地缓存重新下载")
    ap.add_argument("--no-backup", action="store_true", help="跳过备份（重跑时用）")
    args = ap.parse_args()

    storage = get_storage()
    print("=" * 84)
    print(f"  重建 dwd_daily_bar（同花顺官方导出，近 {args.years} 年，未复权单一口径）")
    print("=" * 84)

    print("\n[1/4] 备份现有表")
    if args.no_backup:
        print("  （--no-backup，跳过；重跑时用，避免堆出多个 100MB 副本）")
    else:
        backup("dwd_daily_bar", storage)

    print("\n[2/4] 获取导出数据")
    dump = download_dump(force=args.refetch and not args.skip_download)

    print("\n[3/4] 映射与除权处理")
    new = build(dump, args.years, storage)

    print("\n[4/4] 写入新表并校验")
    storage.execute(f"DROP TABLE IF EXISTS {NEW_TABLE}")
    # 必须复制原表 DDL（含 PRIMARY KEY），不能用 CREATE TABLE AS SELECT：
    # 后者不保留主键，而 `upsert_df` 依赖 ON CONFLICT 主键做幂等写入，会直接报
    # "no UNIQUE/PRIMARY KEY constraints"。实测踩过。
    ddl = storage.query_value(
        "SELECT sql FROM duckdb_tables() WHERE table_name = 'dwd_daily_bar'"
    )
    if not ddl:
        print("✖ 未能取到 dwd_daily_bar 的建表语句，无法安全复制结构")
        return 1
    storage.execute(ddl.replace("dwd_daily_bar", NEW_TABLE, 1))
    n = storage.upsert_df(new, NEW_TABLE)
    print(f"  已写入 {NEW_TABLE}：{n} 行")
    ok = validate(new, storage)

    if args.swap:
        if not ok:
            print("\n✖ 校验未通过，**不执行替换**")
            return 1
        storage.execute("DROP TABLE dwd_daily_bar")
        storage.execute(f"ALTER TABLE {NEW_TABLE} RENAME TO dwd_daily_bar")
        print("\n✔ 已替换 dwd_daily_bar。请接着重建派生表：")
        print("     python -m astock.cli daily --no-refresh     # 或单独跑因子重建")
        return 0

    print(f"\n（未替换）确认无误后加 --swap 重新执行即可。新表保留在 {NEW_TABLE}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
