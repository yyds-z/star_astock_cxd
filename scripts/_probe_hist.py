# -*- coding: utf-8 -*-
"""临时：探测涨停池接口的历史数据深度（date_ms 能查到多早）。"""
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astock.config import get_config  # noqa: E402

get_config()
import requests  # noqa: E402

KEY = get_config().env("HITHINK_FINANCE_API_KEY")
sess = requests.Session()
sess.headers.update({"X-api-key": KEY or ""})
CST = timezone(timedelta(hours=8))


def to_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=CST).timestamp() * 1000)


def probe(d: date) -> str:
    try:
        r = sess.get(
            "https://fuyao.aicubes.cn/api/a-share/special-data/limit-up-pool",
            params={"date_ms": to_ms(d), "page": 1, "size": 5},
            timeout=30,
        )
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        return "异常 %s" % str(exc)[:60]
    if data.get("code") != 0:
        return "code=%s %s" % (data.get("code"), str(data.get("message"))[:50])
    pg = (data.get("data") or {}).get("pagination") or {}
    return "total=%s" % pg.get("total")


print("=" * 70)
print("  涨停池历史数据深度探测（间隔 4.3 秒防限流）")
print("=" * 70)
today = date.today()
for label, d in [
    ("昨天", today - timedelta(days=1)),
    ("1 周前", today - timedelta(days=7)),
    ("1 月前", today - timedelta(days=31)),
    ("3 月前", today - timedelta(days=92)),
    ("半年前", today - timedelta(days=183)),
    ("1 年前", today - timedelta(days=366)),
    ("2 年前", today - timedelta(days=731)),
]:
    print("  %s（%s）: %s" % (label, d, probe(d)))
    time.sleep(4.3)
print("=" * 70) 
