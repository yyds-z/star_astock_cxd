# -*- coding: utf-8 -*-
"""一键跑全部测试套件（含接口冒烟，需要 Web 服务在跑）。

为什么要有这个：之前每个测试都要手敲命令，PowerShell / cmd 下的 `&` 链式执行
行为不一致（实测有些套件根本没被执行，却看起来"跑过了"），
而"看起来跑过了"是最危险的状态。这里统一用 subprocess 串行执行并汇总。
用法：
    python scripts\\run_tests.py            # 全部
    python scripts\\run_tests.py --no-smoke # 跳过需要服务的冒烟测试
"""

from __future__ import annotations

import argparse
import io
import subprocess
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent

SUITES = [
    ("报告结构回归", "tests/test_report.py", False),
    ("上游解析回归", "tests/test_hithink_parsers.py", False),
    ("快照并发读取", "tests/test_serving_concurrency.py", False),
    ("接口冒烟（需服务）", "tests/smoke_test.py", True),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-smoke", action="store_true", help="跳过需要 Web 服务的冒烟测试")
    args = ap.parse_args()

    failed: list[str] = []
    for label, rel, needs_server in SUITES:
        if needs_server and args.no_smoke:
            print(f"跳过　{label}（--no-smoke）")
            continue
        proc = subprocess.run(
            [sys.executable, str(ROOT / rel)],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        tail = [l for l in (proc.stdout or "").strip().splitlines() if l.strip()]
        status = "通过" if proc.returncode == 0 else "失败"
        print(f"{status}　{label}")
        for line in tail[-2:]:
            print(f"        {line.strip()}")
        if proc.returncode != 0:
            failed.append(label)
            err = (proc.stdout or "") + (proc.stderr or "")
            for line in [l for l in err.splitlines() if "Error" in l or "错误" in l][-3:]:
                print(f"        {line.strip()}")

    print()
    if failed:
        print(f"✖ 共 {len(SUITES)} 个套件，失败 {len(failed)}：{', '.join(failed)}")
        return 1
    print(f"✔ 全部 {len(SUITES)} 个套件通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
