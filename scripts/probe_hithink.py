# -*- coding: utf-8 -*-
"""临时：实测同花顺 Financial-API Key。Key 从 .env 读取，不打印。"""
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astock.config import get_config  # noqa: E402

KEY = get_config().env("HITHINK_FINANCE_API_KEY")
print("Key 已读取:", bool(KEY), "(长度 %s)" % len(KEY or ""))

import requests  # noqa: E402

BASE = "https://fuyao.aicubes.cn"
sess = requests.Session()
sess.headers.update({"X-api-key": KEY or ""})


def probe(desc, path, params=None):
    try:
        r = sess.get(BASE + path, params=params or {}, timeout=30)
    except Exception as exc:  # noqa: BLE001
        print("[%s] 网络失败: %s" % (desc, str(exc)[:100]))
        return None
    if r.status_code != 200:
        print("[%s] HTTP %s: %s" % (desc, r.status_code, r.text[:160].replace("\n", " ")))
        return None
    try:
        data = r.json()
    except ValueError:
        print("[%s] 非 JSON: %s" % (desc, r.text[:160]))
        return None
    payload = data.get("data")
    n = len(payload) if isinstance(payload, (list, dict)) else None
    print("[%s] HTTP %s  code=%s  msg=%s  条数=%s"
          % (desc, r.status_code, data.get("code"),
             str(data.get("message") or data.get("msg"))[:50], n))
    return payload


print("=" * 70)
print("  Financial-API 连通性实测")
print("=" * 70)

CASES = [
    ("行情快照", "/api/a-share/prices/snapshot", {"thscodes": "600519.SH"}),
    ("估值快照", "/api/a-share/valuations/snapshot", {"thscodes": "600519.SH"}),
    ("涨停池", "/api/a-share/special-data/limit-up-pool", {}),
    ("集合竞价", "/api/a-share/auction/snapshot", {"thscodes": "600519.SH"}),
]

ok = 0
for desc, path, params in CASES:
    try:
        r = sess.get(BASE + path, params=params, timeout=30)
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        print("[%s] 失败: %s" % (desc, str(exc)[:100]))
        continue
    print("=" * 70)
    print("[%s] HTTP %s  code=%s" % (desc, r.status_code, data.get("code")))
    print("  响应顶层键:", list(data.keys()))
    # 限流/配额相关响应头
    for h, v in r.headers.items():
        if any(k in h.lower() for k in ("limit", "quota", "remain", "reset", "x-ratelimit")):
            print("  响应头 %s: %s" % (h, v))
    payload = data.get("data")
    print("  data 类型:", type(payload).__name__)
    if isinstance(payload, dict):
        print("  data 键:", list(payload.keys()))
        for k, v in payload.items():
            if isinstance(v, list) and v:
                print("  %s: %d 条" % (k, len(v)))
                print("    首条:", {kk: vv for kk, vv in list(v[0].items())[:14]})
                break
    elif isinstance(payload, list) and payload:
        print("  首条:", {k: v for k, v in list(payload[0].items())[:14]})
    ok += 1

print()
print("=" * 70)
print("  完成 %d / %d" % (ok, len(CASES)))
print("=" * 70)
