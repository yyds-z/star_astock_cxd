# -*- coding: utf-8 -*-
"""命令行入口。

用法（在项目根目录执行）：
    python -m astock.cli initdb                        # 初始化数据库
    python -m astock.cli backfill --years 2            # 回填近 2 年日线（支持中断续传）
    python -m astock.cli backfill --limit 300          # 先小样本验证
    python -m astock.cli daily                         # 每日全流程：补数→因子→状态→选股→报告
    python -m astock.cli daily --no-llm                # 强制使用本地模板报告
    python -m astock.cli review                        # T+1 复盘
    python -m astock.cli status                        # 数据状态
    python -m astock.cli serve --port 8000             # 启动本地 Web 服务
"""

from __future__ import annotations

import argparse
import sys

from astock.config import get_config, params_version
from astock.logger import get_logger, setup_logging


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="astock", description="A股 AI 智能选股系统（Phase 1）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("initdb", help="初始化数据库表结构")

    p_backfill = sub.add_parser("backfill", help="回填历史日线")
    p_backfill.add_argument("--years", type=int, default=None, help="回填年限，默认取配置")
    p_backfill.add_argument("--limit", type=int, default=None, help="单次最多处理 N 只（分批跑用）")
    p_backfill.add_argument("--workers", type=int, default=4,
                            help="并行进程数，默认 4（实测 5500 只约 1 小时；1 最稳但需 3.7 小时）")
    p_backfill.add_argument("--codes", type=str, default=None, help="指定股票代码，逗号分隔")
    p_backfill.add_argument("--retry-empty", action="store_true",
                            help="重新尝试此前被标记为「无数据」的股票")
    p_backfill.add_argument(
        "--source",
        type=str,
        default=None,
        choices=["akshare"],
        help="数据源。**只剩 akshare**（baostock/adata 适配器已于 2026-09-30 删除）",
    )

    p_daily = sub.add_parser("daily", help="执行每日全流程")
    p_daily.add_argument("--no-refresh", action="store_true", help="跳过数据补齐（用本地已有数据）")
    p_daily.add_argument("--no-factor", action="store_true", help="跳过因子重算")
    p_daily.add_argument("--llm", action="store_true", help="启用 LLM 生成报告解读")
    p_daily.add_argument("--no-llm", action="store_true", help="强制使用本地模板报告")
    p_daily.add_argument("--workers", type=int, default=4, help="增量采集并行进程数")


    p_regime = sub.add_parser("regime", help="重新计算市场状态")
    p_regime.add_argument("--stats", action="store_true", help="同时输出各状态的天数分布")


    p_serve = sub.add_parser("serve", help="启动本地 Web 服务")
    p_serve.add_argument("--host", type=str, default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--reload", action="store_true")

    p_status = sub.add_parser("status", help="查看数据与采集状态")
    p_status.add_argument("--snapshot", action="store_true",
                          help="只读展示层快照，不接触主库（任务运行期间也能查）")

    p_check = sub.add_parser("check", help="数据体检：检查无数据标记与数据滞后情况")
    p_check.add_argument("--limit", type=int, default=30, help="每类最多显示多少条")

    # 策略体检：与"数据体检"（check）不同，这个跑的是**评价**——
    # 对每个策略生成全历史候选，用可实现口径给成绩并做样本外判定。

    p_export = sub.add_parser("export", help="导出展示层快照（Web 端只读这份 Parquet）")
    p_export.add_argument("--bars-days", type=int, default=250, help="个股 K 线快照保留的交易日数")

    p_lp = sub.add_parser("limit-pool", help="采集涨停/炸板池（同花顺源，限流 15 次/分钟）")
    p_lp.add_argument("--date", type=str, default=None, help="只采集指定日期 YYYY-MM-DD")
    p_lp.add_argument("--years", type=int, default=None, help="回填最近 N 年（首次建库用）")
    p_lp.add_argument("--show", type=int, default=0, help="顺便打印最新一天涨停板前 N（按封单额）")

    p_dump = sub.add_parser(
        "dump-daily",
        help="用全市场导出补齐最新交易日（免费源未发布时的补数通道，1 秒完成）",
    )
    p_dump.add_argument("--days", type=int, default=10,
                        help="回看天数，默认 10（>10 会改用 10 年全量导出）")



    p_sector = sub.add_parser("sector", help="同步行业/板块映射（板块强度由本地日线自算）")
    p_sector.add_argument("--coverage", action="store_true", help="只查看映射覆盖率，不采集")
    p_sector.add_argument("--top", type=int, default=0, help="顺便打印当日强度前 N 的板块")
    p_sector.add_argument("--refresh-primary", action="store_true",
                          help="仅按配置重算主用标签（换主源后无需重新拉取）")

    p_ss = sub.add_parser("sector-strength", help="计算板块日度强度（可回填历史）")
    p_ss.add_argument("--all", action="store_true", help="全量重建（口径变更后使用）")

    p_index = sub.add_parser("index", help="同步指数日线（市场状态识别需要）")
    p_index.add_argument("--years", type=int, default=None, help="同步年限，默认取配置")
    p_index.add_argument(
        "--source", type=str, default="akshare", choices=["akshare"],
        help="指数走 akshare（新浪源）。其它源适配器已删除",
    )

    p_snap = sub.add_parser(
        "snapshot", help="采集盘中快照（盘中决策的唯一数据来源，越早开始越有价值）"
    )
    p_snap.add_argument(
        "--slot", type=str, default=None,
        help="时段标识，默认按当前时刻 HH:MM；同一时段重跑为覆盖",
    )
    p_snap.add_argument(
        "--date", type=str, default=None, help="指定快照所属交易日 YYYY-MM-DD（默认今天）",
    )

    p_sh = sub.add_parser(
        "shadow", help="影子模块：涨停基因+缩量不破位（独立于主链路，不参与选股）"
    )
    p_sh.add_argument(
        "action", nargs="?", default="status",
        choices=["build", "settle", "status", "backfill"],
        help="build=生成信号；settle=结算收益；backfill=按区间回填历史；status=看成绩（默认）",
    )
    p_sh.add_argument(
        "--source", type=str, default="daily", choices=["daily", "snapshot"],
        help="daily=用日线收盘（盘后/回填历史）；snapshot=用盘中快照（14:00 决策）",
    )
    p_sh.add_argument("--date", type=str, default=None, help="信号日 YYYY-MM-DD")
    p_sh.add_argument("--from", dest="date_from", type=str, default=None,
                      help="backfill 起始日 YYYY-MM-DD")
    p_sh.add_argument("--to", dest="date_to", type=str, default=None,
                      help="backfill 结束日 YYYY-MM-DD")
    p_sh.add_argument("--days", type=int, default=60, help="status 统计的回看天数")

    return parser


# ---------------- 各命令实现 ----------------
def cmd_initdb(_args) -> int:
    from astock.storage.db import get_storage

    storage = get_storage()
    storage.init_schema()
    print(f"数据库初始化完成：{storage.db_path}")
    print("表：", ", ".join(sorted(storage.list_tables())))
    return 0


def cmd_backfill(args) -> int:
    from astock.data.collector import Collector

    codes = None
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]

    collector = Collector(source_name=args.source)
    try:
        stats = collector.backfill(
            years=args.years,
            limit=args.limit,
            codes=codes,
            workers=args.workers,
            retry_empty=args.retry_empty,
        )
    finally:
        collector.close()

    print("\n回填结果：", stats)
    print("提示：中断后直接重跑本命令即可续传（已入库的数据会自动跳过）")
    print("      全市场进度可用 python -m astock.cli status 查看")
    return 0
def cmd_regime(args) -> int:
    import pandas as pd

    from astock.market.regime import MarketRegime

    regime = MarketRegime()
    n = regime.compute()
    latest = regime.latest() or {}
    print(f"市场状态计算完成：{n} 个交易日")
    print(f"最新：{latest.get('date')} → {latest.get('state_label')}"
          f"（置信度 {latest.get('confidence')}）")

    if getattr(args, "stats", False):
        df = regime.history(days=100000)
        if df.empty:
            return 0
        print()
        print("=" * 72)
        print("  市场状态分布（用于判断某状态是否有足够样本支撑结论）")
        print("=" * 72)
        print(f"  {'状态':<10}{'天数':>7}{'占比':>9}{'平均涨停':>10}{'平均炸板率':>12}")
        print("-" * 72)
        total = len(df)
        counts = df.groupby("state_label").size().sort_values(ascending=False)
        for label, cnt in counts.items():
            sub = df[df["state_label"] == label]
            up = pd.to_numeric(sub["limit_up_count"], errors="coerce").mean()
            broken = pd.to_numeric(sub["broken_rate"], errors="coerce").mean()
            print(f"  {label:<10}{cnt:>7}{cnt / total * 100:>8.1f}%"
                  f"{up:>10.1f}{broken:>11.1f}%")
        print("=" * 72)
        print("  提示：占比过低的状态（如 < 5%）意味着该环境下的策略表现样本不足，")
        print("        回测结论不可用于调参。")
    return 0


def cmd_daily(args) -> int:
    """每日流程：采集日线 → 涨停池 → 市场状态 → 影子信号 → 报告 → 快照导出。

    2026-10-08：**「选股」环节已删除**。原主链路（8 个策略 → 评分融合 → 15 只配额）
    经 250 个交易日、可实现口径的样本外体检，8 个策略**全部无可实现 alpha**，
    故连同评分器、配额、因子宽表、财务采集一并移除。

    影子信号是**唯一的候选来源**，但它自身在可实现口径下也**未通过检验**
    （见 ads_shadow_pick 的口径说明 / 报告影子板块的裁判结论），
    因此系统当前处于**纸面跟踪观察期** —— 报告与页面都按这个定性呈现，
    不给出"可执行名单"的说法。
    """
    from datetime import datetime, timedelta

    from astock.data.collector import Collector
    from astock.report.builder import ReportBuilder
    from astock.storage.db import get_storage

    # ---- 1) 数据就绪预检（1 次请求，秒级）----
    # 为什么不靠 data_ready_time：那只是**时钟**，无法知道行情源是否真的发布了。
    # 实测 16:08 时东财仍无当日日线，若不预检就会对全市场发出数千次注定失败的
    # 请求（既慢又可能触发封禁），最后还带着**上一个交易日**的数据走完流程 ——
    # 生成口径错位的"今日信号"并写进库，事后才发现。
    if not args.no_refresh:
        try:
            from datetime import date as _date

            collector = Collector()
            try:
                today = _date.today()
                # 只有「本地也缺当天数据」时才需要问外部源。
                # 反例（真实踩过）：用 dump-daily 把当日数据补进库后，
                # 若仍按"源未发布"拦截，会把已经具备数据的运行误杀掉。
                local_latest = collector.storage.latest_trade_date()
                need_fetch = local_latest is None or local_latest < today
                if (
                    need_fetch
                    and collector.calendar.is_open(today)
                    and collector.calendar.is_data_ready(today)
                ):
                    probe, diag = collector.probe_source_date(today)
                    if probe is None:
                        print()
                        print(f"✖ 数据源预检失败，已停止本次运行：{diag}")
                        print("  原因：行情源可能正在维护或限流中。")
                        print("  处理：稍后重跑；也可先跑 python scripts\\probe_hithink.py 看接口连通性。")
                        return 1
                    if probe < today:
                        print()
                        print(f"✖ 行情源尚未发布 {today} 的数据（实测最新只到 {probe}）。")
                        print("  原因：源在收盘后需要时间更新日线，实测通常 17:30 前后才可用。")
                        print("  处理：稍后重跑本命令即可，已入库数据不会重复采集。")
                        print("  若确认要用上一交易日数据，可加 --no-refresh 强制继续。")
                        return 1
            finally:
                collector.close()
        except Exception as exc:  # noqa: BLE001 - 预检失败不应阻断主流程
            print(f"提示：数据就绪预检未能完成，继续执行：{str(exc)[:120]}")

    storage = get_storage()

    # ---- 2) 同步行情到最新交易日（同花顺全市场导出，秒级）----
    if not args.no_refresh:
        collector = Collector(storage=storage)
        try:
            stats = collector.sync_daily(workers=args.workers)
            print(f"行情同步：{stats}")
        except Exception as exc:  # noqa: BLE001
            print(f"提示：行情同步失败，改用库内最新数据继续：{str(exc)[:160]}")
        finally:
            collector.close()

    data_date = storage.latest_trade_date()
    if data_date is None:
        print("✖ 库内没有任何日线数据，无法继续（请先跑 backfill）")
        return 1
    print(f"数据日：{data_date}")

    use_llm = None
    if args.no_llm:
        use_llm = False
    elif args.llm:
        use_llm = True

    # ---- 3) 涨停/炸板池（影子基因依赖 dwd_limit_up）----
    try:
        from astock.data.calendar import TradeCalendar
        from astock.data.hithink import HithinkCollector

        TradeCalendar(storage).sync()
        pool_stats = HithinkCollector(storage).sync(dates=[data_date])
        # 不能用 logger：它是 main() 的局部变量，模块级不存在（真的踩过）
        print(
            "涨停池采集完成：%s（涨停 %d / 炸板 %d）"
            % (data_date, pool_stats["limit_up"], pool_stats["limit_break"])
        )
    except Exception as exc:  # noqa: BLE001
        print(f"提示：涨停池采集失败（影子基因会因缺少涨停数据而退化）：{str(exc)[:140]}")

    # ---- 4) 市场状态（**只用于展示**：市场宽度/涨停家数/炸板率）----
    market: dict = {}
    try:
        from astock.market.regime import MarketRegime

        regime = MarketRegime(storage)
        regime.compute()
        row = regime.latest() or {}
        market = {
            "state": row.get("state"),
            "label": row.get("state_label"),
            "confidence": row.get("confidence"),
            "breadth_ma20": row.get("breadth_ma20"),
            "breadth_ma60": row.get("breadth_ma60"),
            "limit_up": row.get("limit_up_count"),
            "limit_down": row.get("limit_down_count"),
            "broken_rate": row.get("broken_rate"),
            "top_sector": row.get("top_sector"),
            "top_sector_share": row.get("top_sector_share"),
        }
        print("市场状态：%s（宽度 MA20 %s%%）"
              % (row.get("state_label"), row.get("breadth_ma20")))
    except Exception as exc:  # noqa: BLE001
        print(f"提示：市场状态计算失败（页面宽度区将缺数据）：{str(exc)[:140]}")

    # ---- 5) 影子信号：先结算历史，再用数据日生成次日候选 ----
    # settle —— 结算此前信号的实际收益（含可实现口径 exec_*，判据见 settle 内注释）
    # build  —— 用当日收盘数据生成次日候选
    # 必须排在报告之前：报告的影子板块读的就是刚落库的数据。
    try:
        from astock.shadow import LimitGeneShadow

        sh = LimitGeneShadow(storage=storage)
        settled = sh.settle()
        # 必须用**数据日**而不是今天：daily 的数据只到 data_date，
        # 用今天 build 会因"今日无 K 线"恒为空 —— 实测踩过。
        picks = sh.build(data_date)
        print(f"影子模块完成：结算 {settled} 行 | {data_date} 生成 {len(picks)} 只候选")
    except Exception as exc:  # noqa: BLE001
        print(f"提示：影子模块失败（它是唯一候选来源，必须排查）：{str(exc)[:200]}")

    # ---- 5.5) 影子复盘（归因）：把「涨了/跌了」变成「为什么」 ----
    # 必须排在报告之前：报告的「二、影子复盘」板块读的就是刚落库的这条记录。
    # 归因只用于理解与记录，冻结期内不据此调参。
    try:
        from astock.shadow import ShadowReviewer

        rev = ShadowReviewer(storage=storage).run(days=20, use_llm=use_llm)
        if rev.get("available"):
            print(
                "影子复盘：%d 只 / %d 个信号日｜可实现超额 %s%%"
                % (rev["picks"], rev["signal_days"], rev["excess"])
            )
        else:
            print(f"影子复盘跳过：{rev.get('reason')}")
    except Exception as exc:  # noqa: BLE001
        print(f"提示：影子复盘失败（报告复盘板块会缺内容）：{str(exc)[:160]}")

    # ---- 6) 报告 ----
    try:
        from astock.data.calendar import TradeCalendar

        nxt = TradeCalendar(storage).open_dates(
            data_date + timedelta(days=1), data_date + timedelta(days=30))
        plan_date = nxt[0] if nxt else None
    except Exception:  # noqa: BLE001 - 报告不应因日历缺失而失败
        plan_date = None

    result = {
        "data_date": str(data_date),
        "plan_date": str(plan_date) if plan_date else None,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "market": market,
        "params_version": params_version(),
    }
    report = ReportBuilder(storage).build(result, use_llm=use_llm)
    print(ReportBuilder.console_summary(result, {"market_view": report.get("market_view")}))

    # ---- 7) 导出展示层快照：Web 服务只读这份 Parquet，从而不会占用主库锁 ----
    from astock.storage.serving import export as export_serving

    meta = export_serving(storage)

    print(f"\n报告文件：{report['markdown_path']}")
    print(f"结构化结果：{report['json_path']}")
    print(f"展示层快照：{meta['exported_at']}（Web 端读取）")
    return 0


def cmd_export(args) -> int:
    from astock.storage.serving import export as export_serving

    meta = export_serving(bars_days=getattr(args, "bars_days", 250))
    print(f"展示层快照导出完成：{meta['exported_at']}")
    for name, rows in (meta.get("tables") or {}).items():
        print(f"  {name}: {rows} 行")
    return 0
def cmd_serve(args) -> int:
    import uvicorn

    print(f"启动本地服务：http://{args.host}:{args.port}")
    uvicorn.run("api.server:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def _status_from_snapshot(note: str = "") -> int:
    """从 Parquet 快照汇报状态。

    刻意不碰主库：这样在任何任务（回填/回测）运行期间都能查状态，
    否则用户想看看跑到哪了，反而会撞上「数据库被占用」而什么都看不到。
    """
    from astock.storage.serving import get_reader, serving_dir

    out_dir = serving_dir()
    reader = get_reader()
    if not reader.available():
        print("展示层快照尚未生成，无法读取。请先执行：python -m astock.cli export")
        return 1

    meta = reader.meta()
    print(f"  数据来源：展示层快照（导出时点 {meta.get('exported_at') or '未知'}）")
    if note:
        print(f"  说明：{note}")
    print(f"  快照目录：{out_dir}")
    print("-" * 64)

    # 只列展示层快照契约内的表（trade_calendar 等内部表不参与导出）
    for name in (
        "dim_stock", "stock_bars", "feature_latest",
        "market_regime", "recommend", "review", "strategy_stats", "llm_usage",
    ):
        if not (out_dir / f"{name}.parquet").exists():
            print(f"  {name:<16} 缺失")
            continue
        n = reader.query(f"SELECT COUNT(*) AS n FROM {name}", name)
        rows = int(n.iloc[0, 0]) if not n.empty else 0
        extra = ""
        if name == "stock_bars":
            d = reader.query(
                "SELECT MIN(date) AS a, MAX(date) AS b FROM stock_bars", "stock_bars"
            )
            if not d.empty:
                extra = f"（{d.iloc[0, 0]} ~ {d.iloc[0, 1]}）"
        elif name == "recommend":
            d = reader.query("SELECT MAX(rec_date) AS d FROM recommend", "recommend")
            if not d.empty and d.iloc[0, 0] is not None:
                extra = f"（最近数据日 {d.iloc[0, 0]}）"
        print(f"  {name:<16} {rows:>10} 行 {extra}")

    regime = reader.query(
        "SELECT date, state_label, confidence FROM market_regime "
        "ORDER BY date DESC LIMIT 1",
        "market_regime",
    )
    if not regime.empty:
        r = regime.iloc[0]
        print("-" * 64)
        print(f"  最新市场状态：{r['date']} {r['state_label']}（置信度 {r['confidence']}）")
    return 0


def cmd_status(args) -> int:
    from astock.config import get_config as _gc

    cfg = _gc()
    print(f"数据库：{cfg.duckdb_path}")
    print(f"数据目录：{cfg.data_dir}")

    if getattr(args, "snapshot", False):
        return _status_from_snapshot("已指定 --snapshot，强制不接触主库")

    from astock.data.collector import Collector

    try:
        collector = Collector()
    except RuntimeError as exc:
        # 被占用时不让用户卡死：自动降级到快照，并说明是谁占着
        lines = str(exc).splitlines()
        print("\n[!] 主库正被其它进程占用，自动改用展示层快照汇报")
        if len(lines) > 1:
            print(f"    {lines[1]}")
        print()
        return _status_from_snapshot("主库被占用，数据为上次导出时点")

    try:
        for k, v in collector.status().items():
            print(f"  {k}: {v}")
    finally:
        collector.close()
    return 0


def cmd_limit_pool(args) -> int:
    """涨停/炸板池采集（同花顺源，限流 15 次/分钟）。"""
    from astock.data.hithink import HithinkCollector

    try:
        collector = HithinkCollector()
    except RuntimeError as exc:
        print(f"[!] {exc}")
        return 1

    dates = None
    if args.date:
        import pandas as pd

        dates = [pd.to_datetime(args.date).date()]
    stats = collector.sync(dates=dates, years=args.years)

    print()
    print("=" * 72)
    if stats["days"]:
        print(f"  采集完成：{stats['days']} 个交易日（{stats['start']} ~ {stats['end']}）")
        print(f"  涨停记录 {stats['limit_up']} 条　炸板记录 {stats['limit_break']} 条")
        print(f"  耗时 {stats['elapsed_sec']}s（速度受 15 次/分钟限流约束）")
    else:
        print("  无需采集（已是最新）")
    print("=" * 72)

    if args.show:
        _print_limit_pool(args.show)
    return 0


def cmd_dump_daily(args) -> int:
    """用全市场导出补齐最新交易日（免费源尚未发布时的补数通道）。

    存在意义：免费源（东财/baostock）收盘后要等 1~2 小时才发布，且会限流/封禁。
    同花顺的导出实测 16:2x 就已含当日全市场数据，1 次请求 1 秒拿完。
    """
    from astock.data.hithink import HithinkCollector
    from astock.storage.db import get_storage

    try:
        collector = HithinkCollector(get_storage())
    except RuntimeError as exc:
        print(f"[!] {exc}")
        return 1

    print("正在下载全市场日K导出 …")
    try:
        stats = collector.sync_daily_from_dump(days=args.days)
    except Exception as exc:  # noqa: BLE001
        print(f"补数失败：{str(exc)[:200]}")
        return 1

    print()
    print("=" * 76)
    if stats.get("rows"):
        print(f"  补数完成：{stats['days']} 个交易日（{', '.join(stats['dates'])}），"
              f"写入 {stats['rows']} 行")
        print("  注意：导出为**未复权**，因此只补库里缺失的日期、不覆盖已有数据；")
        print("        换手率是按上一日流通市值反推股本的**近似值**（adjust 标记为 dump）。")
    else:
        print(f"  无需补数（库内已是最新 {stats.get('latest_before')}）")
    print("=" * 76)
    return 0
def _print_finance(n: int) -> None:
    """展示最新一期财务指标（价值档的核心输入）。"""
    import pandas as pd  # cli.py 模块级没有导入 pandas，必须局部导入

    from astock.storage.db import get_storage

    df = get_storage().query_df(
        """
        SELECT f.code, s.name, f.period_end, f.roe, f.debt_ratio,
               f.revenue_yoy, f.profit_yoy, f.eps
        FROM dws_finance_metrics f
        JOIN (
            SELECT code, MAX(period_end) AS pe FROM dws_finance_metrics GROUP BY code
        ) m ON m.code = f.code AND m.pe = f.period_end
        LEFT JOIN dim_stock s ON s.code = f.code
        ORDER BY f.roe DESC NULLS LAST
        LIMIT ?
        """,
        [n],
    )
    if df.empty:
        print("\n  财务表为空，先执行：python -m astock.cli finance --all")
        return
    print()
    print("=" * 88)
    print("  最新一期财务指标（按 ROE 排序）")
    print("=" * 88)
    print(f"  {'代码':<8}{'名称':<10}{'报告期':<12}{'ROE':>8}{'资产负债率':>11}"
          f"{'营收同比':>10}{'净利同比':>10}{'EPS':>8}")
    print("-" * 88)
    for _, r in df.iterrows():
        def fmt(key: str, digits: int = 2, suffix: str = "%") -> str:
            v = r[key]
            if v is None or pd.isna(v):
                return "-"
            return f"{float(v):.{digits}f}{suffix}"

        print(f"  {r['code']:<8}{str(r['name'] or ''):<10}{str(r['period_end']):<12}"
              f"{fmt('roe', 1):>8}{fmt('debt_ratio', 1):>11}"
              f"{fmt('revenue_yoy', 1):>10}{fmt('profit_yoy', 1):>10}{fmt('eps', 2, ''):>8}")
    print("-" * 88)
    print("  说明：这些是价值档取代「纯技术面代理」的基本面输入。")
    print("=" * 88)
def _print_limit_pool(n: int) -> None:
    """打印最新一天的涨停板（按封单额排序），并给出连板分布。"""
    from astock.storage.db import get_storage

    storage = get_storage()
    latest = storage.query_value("SELECT MAX(date) FROM dwd_limit_up")
    if latest is None:
        print("涨停池无数据")
        return

    df = storage.query_df(
        "SELECT code, name, first_time, reason, board_text, seal_money "
        "FROM dwd_limit_up WHERE date = ? ORDER BY seal_money DESC LIMIT ?",
        [latest, n],
    )
    if df.empty:
        print("涨停池无数据")
        return

    dist = storage.query_df(
        "SELECT boards, COUNT(*) AS n FROM dwd_limit_up WHERE date = ? "
        "GROUP BY boards ORDER BY boards DESC",
        [latest],
    )
    ladder = "、".join(
        f"{int(r.boards)}板×{int(r.n)}" for r in dist.itertuples() if r.boards
    )
    print()
    print("=" * 96)
    print(f"  涨停板 {latest}（按封单额前 {n}）　连板分布：{ladder or '-'}")
    print("=" * 96)
    print(f"  {'代码':<8}{'名称':<10}{'时间':<7}{'连板':<6}{'题材':<24}{'封单(亿)':>8}")
    print("-" * 96)
    for r in df.itertuples():
        seal = (r.seal_money or 0) / 1e8
        reason = (r.reason or "-")[:22]
        print(
            f"  {r.code:<8}{str(r.name):<10}{str(r.first_time or '-'):<7}"
            f"{str(r.board_text or '-'):<6}{reason:<24}{seal:>8.2f}"
        )
    print("=" * 96)


def cmd_sector(args) -> int:
    """行业/板块映射同步与覆盖率检查。"""
    from astock.data.sectors import SectorCollector

    collector = SectorCollector()

    if getattr(args, "refresh_primary", False):
        n = collector.refresh_primary_flags()
        print(f"主用标签已按配置（{collector.primary_source}）重算：{n} 条")
        return 0

    if args.coverage:
        cov = collector.coverage()
        print(f"  股票总数：{cov['stocks_total']}")
        print(f"  已映射：  {cov['stocks_mapped']}（{cov['ratio']}）")
        print(f"  分来源：  {cov['by_source']}")
        print()
        print("  说明：未映射的股票只是没有板块标签，不会从选股池消失。")
        print("        新浪源稳定但不含新股，申万源权威但接口不稳定，两者互补。")
        return 0

    stats = collector.sync()
    print("行业映射同步完成：")
    for name, info in stats["sources"].items():
        if not info:
            continue
        state = "成功" if info.get("ok") else "失败"
        print(
            f"  {name:<6}{state}　行业 {info.get('industries', 0)} 个　"
            f"映射 {info.get('mapped', 0)} 条　"
            f"失败 {len(info.get('failed') or [])} 个"
        )
        if info.get("finance"):
            print(f"         附带回填基本面 {info['finance']} 条（ROE/增速/PE/PB）")
        if info.get("error"):
            print(f"         原因：{info['error']}")

    cov = stats["coverage"]
    print(f"  覆盖率：{cov['stocks_mapped']} / {cov['stocks_total']} = {cov['ratio']}")
    print(f"  耗时：  {stats['elapsed_sec']}s")

    if getattr(args, "top", 0):
        _print_top_sectors(args.top)
    else:
        print()
        print("  下一步：python -m astock.cli sector-strength   （由本地日线算板块强度）")
    return 0


def _print_top_sectors(n: int) -> None:
    from astock.features.sector_strength import SectorStrengthBuilder

    builder = SectorStrengthBuilder()
    target = builder.latest_date()
    if target is None:
        print("\n  板块强度尚未计算，请先执行：python -m astock.cli sector-strength")
        return
    print()
    print("=" * 72)
    print(f"  当日板块强度 Top{n}（{target}）")
    print("=" * 72)
    print(f"  {'板块':<12}{'强度':>7}{'涨幅':>9}{'涨停':>6}{'家数':>6}{'量能':>8}")
    print("-" * 72)
    for row in builder.top(target, n):
        ratio = row.get("amount_ratio")
        print(
            f"  {row['industry_name']:<12}{row['strength_score']:>7.1f}"
            f"{row['avg_pct_chg']:>8.2f}%{int(row['limit_up_count'] or 0):>6}"
            f"{int(row['member_count'] or 0):>6}"
            f"{(f'{ratio:.2f}x' if ratio else '-'):>8}"
        )
    name, share = builder.top_sector(target)
    if name:
        print("-" * 72)
        print(f"  最强板块：{name}　其涨停家数占全市场 {share}%")
        print(f"  事件驱动阈值：{get_config().get('market_regime.event.sector_limit_up_share', 30.0)}%")
    print("=" * 72)


def cmd_sector_strength(args) -> int:
    """计算板块日度强度。"""
    from astock.features.sector_strength import SectorStrengthBuilder

    builder = SectorStrengthBuilder()
    if args.all:
        print("全量重建板块强度...")
        n = builder.rebuild_all()
    else:
        n = builder.build()

    print(f"板块强度计算完成：{n} 行")
    if n:
        print()
        _print_top_sectors(10)
    return 0


def cmd_shadow(args) -> int:
    """影子模块：涨停基因 + 缩量不破位。

    **独立于主评分体系**：只落库、结算、统计，不参与选股。
    原因见 `astock/shadow/__init__.py` —— 主链路的评价指标（买入日 close/open−1）
    在 T+1 下不可实现，混在一起会导致两边都无法归因。
    """
    from datetime import date as _date

    from astock.shadow import LimitGeneShadow

    action = getattr(args, "action", "status") or "status"
    trade_date = None
    if getattr(args, "date", None):
        try:
            trade_date = _date.fromisoformat(args.date)
        except ValueError:
            print(f"日期格式错误：{args.date}（应为 YYYY-MM-DD）")
            return 2

    model = LimitGeneShadow()
    if action == "build":
        df = model.build(trade_date, source=getattr(args, "source", "daily") or "daily")
        if df.empty:
            print("影子信号：无候选（可能是非交易日 / 历史不足 / 条件未命中）")
        else:
            print(f"影子信号已落库：{len(df)} 只")
            print(df[["code", "name", "close", "zt20", "days_since_zt",
                      "vol_ratio", "vs_ma5"]].to_string(index=False))
    elif action == "backfill":
        def _p(i, n, total):
            print(f"  已处理 {i}/{n} 天，累计 {total} 只", flush=True)

        start = _date.fromisoformat(args.date_from) if getattr(args, "date_from", None) else None
        end = _date.fromisoformat(args.date_to) if getattr(args, "date_to", None) else None
        total = model.backfill(start, end, progress=_p)
        print(f"历史回填完成：共 {total} 只信号（未结算，收益需再跑 settle）")
    elif action == "settle":
        print(f"影子信号结算：{model.settle()} 行")
    else:
        print(model.report(days=int(getattr(args, "days", 60) or 60)))
    return 0


def cmd_snapshot(args) -> int:
    """采集盘中快照（`dwd_intraday_snapshot`）。

    为什么独立成命令、不并入 daily：daily 有 `data_ready_time = 16:00` 守卫
    （`calendar.resolve_data_date`），14:00 运行会被**主动回退到上一交易日**，
    产出「昨天的数据日 + 今天的计划日」的错位报告，拿不到"当天此刻"的截面。
    盘中决策只能用这条链路。

    另外这也是唯一**有时效性**的一步：免费源没有历史盘中数据，
    晚一天开始记录，就永远少一天可回测的样本。
    """
    from datetime import date as _date

    from astock.data.intraday import IntradaySnapshotCollector

    trade_date = None
    if getattr(args, "date", None):
        try:
            trade_date = _date.fromisoformat(args.date)
        except ValueError:
            print(f"日期格式错误：{args.date}（应为 YYYY-MM-DD）")
            return 2

    collector = IntradaySnapshotCollector(source=getattr(args, "source", None) or "akshare")
    try:
        n = collector.collect(slot=getattr(args, "slot", None), trade_date=trade_date)
    finally:
        collector.close()

    if n:
        print(f"盘中快照已落库：{n} 只")
    else:
        print("未写入任何快照（非交易日 / 数据源不可用 / 无有效报价）")
    return 0


def cmd_index(args) -> int:
    """同步指数日线。市场状态识别依赖指数动量，缺少时该维度会为空。"""
    from astock.data.collector import Collector

    collector = Collector(source_name=getattr(args, "source", None) or "akshare")
    try:
        n = collector.sync_index()
    finally:
        collector.close()
    print(f"指数同步完成：入库 {n} 行")
    if n == 0:
        print("提示：指数未入库时，市场状态中的指数动量维度为空，不影响其余判定。")
    return 0


def _sync_skills(storage, label: str = "Skill 库") -> None:
    """刷新 Skill 库，失败不影响主流程。

    主流程（回测/日报）的核心产物已经落盘，Skill 库属于附加统计，
    不能因为它的任何问题让用户觉得整条命令失败了。
    """
    try:
        from astock.skills import SkillsStore

        SkillsStore(storage).sync()
    except Exception as exc:  # noqa: BLE001
        print(f"提示：{label}同步失败（不影响本次结果）：{str(exc)[:160]}")
        print("      可稍后单独执行：python -m astock.cli skills sync")
def _print_skill_table(store) -> int:
    skills = store.list_skills()
    if not skills:
        print("Skill 库为空，请先执行：python -m astock.cli skills sync")
        return 1

    verdict_text = {
        "effective": "有效", "watching": "观察", "ineffective": "无效",
        "insufficient": "样本不足", "no_data": "未回测",
    }
    print("=" * 96)
    print(f"  策略 Skill 库（{len(skills)} 个）")
    print("=" * 96)
    print(f"  {'档位':<6}{'策略':<24}{'版本':<9}{'状态':<9}{'样本':>7}{'胜率':>8}{'T+1平均':>9}  {'判定'}")
    print("-" * 96)
    for s in sorted(skills, key=lambda x: (x["tier"], x["name"])):
        tier = {"short": "短线", "swing": "波段", "value": "价值"}.get(s["tier"], s["tier"])
        print(
            f"  {tier:<6}{s['label']:<24}{str(s.get('version', '-')):<9}"
            f"{str(s.get('status', '-')):<9}"
            f"{str(s.get('sample_size') or '-'):>7}{str(s.get('win_rate') or '-'):>8}"
            f"{str(s.get('avg_ret1') or '-'):>9}  {verdict_text.get(s.get('verdict'), '-')}"
        )
    print("=" * 96)
    print("  查看单个策略：python -m astock.cli skills show <name>")
    print("  改动参数后：  python -m astock.cli skills sync   （自动升版本并记 changelog）")
    return 0


def _list_backtest_runs() -> int:
    from astock.storage.db import get_storage

    storage = get_storage()
    df = storage.query_df(
        """
        SELECT run_id,
               COUNT(*)                AS signals,
               COUNT(DISTINCT data_date) AS days,
               MIN(data_date)          AS start_date,
               MAX(data_date)          AS end_date
        FROM ads_backtest GROUP BY run_id ORDER BY run_id DESC
        """
    )
    if df.empty:
        print("暂无回测记录。运行：python -m astock.cli backtest --months 6")
        return 0
    print("历史回测运行：")
    print(df.to_string(index=False))
    return 0


def _report_from_stored(storage_provider) -> int:
    """用最近一次回测的落库结果重算统计并重出报告（不重新回放）。

    报告口径调整后无需再等几分钟重跑回放，直接重算即可。
    """
    import pandas as pd

    from astock.backtest import report as bt_report
    from astock.backtest.engine import BacktestEngine
    from astock.backtest.metrics import summarise

    storage = storage_provider()
    engine = BacktestEngine(storage)
    detail = engine.load_run()
    if detail.empty:
        print("暂无回测记录，请先运行：python -m astock.cli backtest")
        return 1

    detail["data_date"] = pd.to_datetime(detail["data_date"])
    stats = summarise(detail)
    stats.update(
        {
            "run_id": str(detail["run_id"].iloc[0]),
            "top_n": int(detail.groupby(["data_date", "tier"]).size().groupby("tier").max().max()),
            "tiers": sorted(detail["tier"].unique().tolist()),
            "elapsed_sec": 0,
            "detail": detail,
        }
    )
    paths = bt_report.save(stats)
    print(f"已基于已落库的回测结果重出报告：{paths['markdown']}")
    for note in bt_report.conclusions(stats):
        print("  ·", note)
    return 0
def _parse_date(value: str | None):
    from datetime import date as _d

    if not value:
        return None
    return _d.fromisoformat(value)
def cmd_check(args) -> int:
    """数据体检：区分「真退市 / 长期停牌」与「采集失败」，并列出滞后股票。"""
    from astock.storage.db import get_storage

    storage = get_storage()

    empty = storage.query_df(
        """
        SELECT s.key AS code, d.name, d.status, d.ipo_date, d.out_date
        FROM sys_collect_state s
        LEFT JOIN dim_stock d ON d.code = s.key
        WHERE s.task = 'collect' AND s.value = 'EMPTY'
        ORDER BY s.key
        """
    )
    print(f"\n【被标记为无数据】共 {len(empty)} 只（采集失败与真无数据需区分）")
    if not empty.empty:
        listed = empty[empty["status"].fillna(1).astype(int) == 1]
        delisted = len(empty) - len(listed)
        print(f"  其中已退市/退市整理：{delisted} 只（属正常，无需处理）")
        if not listed.empty:
            print(f"  仍显示上市但无数据：{len(listed)} 只（可能为长期停牌，也可能是采集失败）")
            print(listed.head(args.limit).to_string(index=False))
            print("  → 如需重试：python -m astock.cli backfill --retry-empty --limit 500")

    latest = storage.latest_trade_date()
    if latest is not None:
        stale = storage.query_df(
            """
            SELECT b.code, d.name, MAX(b.date) AS last_date
            FROM dwd_daily_bar b
            LEFT JOIN dim_stock d ON d.code = b.code
            GROUP BY b.code, d.name
            HAVING MAX(b.date) < (SELECT MAX(date) FROM dwd_daily_bar)
            ORDER BY last_date
            LIMIT ?
            """,
            [args.limit],
        )
        print(f"\n【数据滞后于最新交易日 {latest}】显示前 {len(stale)} 只")
        print("  （停牌股属正常；若大量出现则说明需要重跑 backfill 补数）")
        if not stale.empty:
            print(stale.to_string(index=False))

    return 0


COMMANDS = {
    "initdb": cmd_initdb,
    "backfill": cmd_backfill,
    "regime": cmd_regime,
    "daily": cmd_daily,
    "serve": cmd_serve,
    "status": cmd_status,
    "check": cmd_check,
    "export": cmd_export,
    "sector": cmd_sector,
    "limit-pool": cmd_limit_pool,
    "dump-daily": cmd_dump_daily,
    "sector-strength": cmd_sector_strength,
    "index": cmd_index,
    "snapshot": cmd_snapshot,
    "shadow": cmd_shadow,
}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    cfg = get_config()
    setup_logging(cfg.log_dir)
    logger = get_logger("cli")

    handler = COMMANDS.get(args.command)
    if handler is None:
        parser.print_help()
        return 2

    logger.info("执行命令：%s", args.command)
    try:
        return handler(args)
    except KeyboardInterrupt:
        logger.warning("任务被中断。已入库的数据不会丢失，直接重跑本命令即可续传。")
        return 130
    except Exception as exc:  # noqa: BLE001
        logger.exception("命令执行失败：%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
