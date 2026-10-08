# -*- coding: utf-8 -*-
"""接口冒烟测试：不启动服务，直接用 TestClient 验证所有 API 与前端页面是否正常。

用法（在项目根目录执行）：
    python tests/smoke_test.py          # 或 scripts\\run_tests.bat 一键全跑
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from api.server import app  # noqa: E402

client = TestClient(app)

ENDPOINTS = [
    ("GET", "/", None),
    ("GET", "/api/health", None),
    ("GET", "/api/status", None),
    ("GET", "/api/config", None),
    ("GET", "/api/market/latest", None),
    ("GET", "/api/market/history?days=30", None),
    # /api/recommend/*、/api/review/*、/api/strategies、/api/skills* 已随主链路删除
    ("GET", "/api/shadow/latest", None),
    ("GET", "/api/shadow/history?days=30", None),
    ("GET", "/api/shadow/review?days=30", None),
    ("GET", "/api/equity", None),
    ("GET", "/api/calibration", None),
    ("GET", "/api/sector/top?n=10", None),
    ("GET", "/api/llm/usage", None),
    ("GET", "/api/stock/000002", None),
]


def main() -> int:
    failed = 0
    for method, url, _payload in ENDPOINTS:
        try:
            resp = client.request(method, url)
            ok = resp.status_code == 200
            size = len(resp.content)
            flag = "OK  " if ok else "FAIL"
            print(f"[{flag}] {method} {url} → {resp.status_code} ({size} bytes)")
            if not ok:
                failed += 1
                print("       ", resp.text[:200])
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[FAIL] {method} {url} → 异常 {type(exc).__name__}: {exc}")

    if failed:
        print()
        print(f"存在 {failed} 个失败接口")
        return 1

    # ---- 并发阶段 ----
    # 必须单独测：上面是串行请求，掩盖了「多线程同时懒加载视图」的竞态。
    # 浏览器一次刷新就会并发打到这些接口，真实故障正是这样暴露的。
    print()
    print("-" * 66)
    print("并发阶段：模拟浏览器同时刷新多个接口（每轮 10 个并发请求）")
    print("-" * 66)

    concurrent_urls = [
        "/api/market/latest",
        "/api/market/history?days=120",
        "/api/shadow/latest",
        "/api/shadow/history?days=30",
        "/api/shadow/review?days=30",
        "/api/equity",
        "/api/status",
        "/api/stock/000002",
        "/api/sector/top?n=10",
        "/api/calibration",
    ]
    from concurrent.futures import ThreadPoolExecutor

    race_failed: list[str] = []
    for rnd in range(1, 4):
        with ThreadPoolExecutor(max_workers=len(concurrent_urls)) as pool:
            responses = list(pool.map(lambda u: (u, client.get(u)), concurrent_urls))
        bad = [(u, resp.status_code) for u, resp in responses if resp.status_code != 200]
        print(f"  第 {rnd} 轮：{len(concurrent_urls) - len(bad)}/{len(concurrent_urls)} 成功")
        for url, code in bad:
            race_failed.append(f"{url} → {code}")

    print()
    if race_failed:
        print("并发下存在失败接口（读取器可能有竞态）：")
        for item in race_failed[:10]:
            print(f"  {item}")
        return 1

    print("全部接口正常（串行 + 并发）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
