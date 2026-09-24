# -*- coding: utf-8 -*-
"""全市场回填驱动脚本：分批执行直到全部完成。

为什么要分批：单次跑完 5500 只耗时约 1 小时，中途若关机或断网会丢失进度感；
分批执行每批落盘一次，进度可随时查看，中断后重跑自动续传。

用法：
    python scripts/run_full_backfill.py                 # 默认每批 800 只、6 进程
    python scripts/run_full_backfill.py --batch 500 --workers 8
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astock.config import get_config  # noqa: E402
from astock.data.collector import Collector  # noqa: E402
from astock.data.universe import Universe  # noqa: E402
from astock.logger import get_logger, setup_logging  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="全市场分批回填")
    parser.add_argument("--batch", type=int, default=800, help="每批股票数")
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="并行进程数。默认 4：实测 6 进程时 baostock 登录会被拒，导致整批失败",
    )
    parser.add_argument("--years", type=int, default=None, help="回填年限")
    parser.add_argument("--cooldown", type=int, default=5, help="批次之间的间隔秒数，降低数据源压力")
    args = parser.parse_args()

    cfg = get_config()
    setup_logging(cfg.log_dir)
    logger = get_logger("run_full_backfill")

    collector = Collector()
    years = args.years or int(cfg.get("data.backfill_years", 2))
    end = collector.calendar.latest_open_date() or date.today()
    start = date.today() - timedelta(days=int(365.25 * years))

    collector.ensure_base()
    all_codes = Universe(collector.storage).all_codes()
    logger.info("全市场 %d 只，回填区间 %s ~ %s，每批 %d 只 / %d 进程",
                len(all_codes), start, end, args.batch, args.workers)

    round_no = 0
    started = time.perf_counter()
    try:
        while True:
            pending = collector._pending_codes(all_codes, end)
            if not pending:
                logger.info("全部完成，无需继续回填")
                break

            round_no += 1
            logger.info("第 %d 批：剩余 %d 只待采集", round_no, len(pending))
            stats = collector.backfill(
                years=years, limit=args.batch, workers=args.workers, start=start
            )
            if stats["tasks"] == 0:
                logger.warning("本批未产生任务，提前结束以避免死循环")
                break

            # 整批全败通常是数据源限流 / 登录被拒，继续循环只会无效重试
            if stats["rows"] == 0 and stats["failed"] >= stats["tasks"]:
                logger.error(
                    "第 %d 批全部失败（%d 只），疑似数据源限流或登录被拒，已中止。",
                    round_no, stats["failed"],
                )
                logger.error(
                    "处理建议：等待 5~10 分钟后重跑本脚本；并降低并发，例如 --workers 2"
                )
                break

            logger.info("第 %d 批完成：入库 %d 行，失败 %d 只，耗时累计 %.1f 分钟",
                        round_no, stats["rows"], stats["failed"],
                        (time.perf_counter() - started) / 60)
            if args.cooldown:
                time.sleep(args.cooldown)
    except KeyboardInterrupt:
        logger.warning("已被手动中断。直接重跑本脚本即可从断点续传。")
        return 130
    finally:
        # 顺便补齐指数
        try:
            collector.sync_index(start=start)
        except Exception as exc:  # noqa: BLE001
            logger.warning("指数同步失败：%s", exc)
        status = collector.status()
        collector.close()

    print("\n=========== 回填结果 ===========")
    for k, v in status.items():
        print(f"  {k}: {v}")
    print(f"  总耗时: {(time.perf_counter() - started) / 60:.1f} 分钟")
    print("\n下一步：python -m astock.cli factor && python -m astock.cli daily")
    return 0


if __name__ == "__main__":
    sys.exit(main())
