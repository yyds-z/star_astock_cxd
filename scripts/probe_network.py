# -*- coding: utf-8 -*-
"""网络连通性诊断：确认行情数据源在当前的代理/网络环境下是否可达。

用于排查这类问题：
- baostock 返回「黑名单用户」→ 需等待解封或换源
- akshare/adata 报 ProxyError → 系统代理拦截，需在 config 中开启 bypass_proxy
- ConnectionError / RemoteDisconnected → 目标站点不可达或被限流

用法：
    python scripts/probe_network.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from astock.config import get_config  # noqa: E402

TARGETS = {
    "东财行情(eastmoney)": "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    "?secid=1.600519&fields1=f1&fields2=f51,f52,f53,f54,f55&klt=101&fqt=1&end=20500101&lmt=5",
    "新浪行情(sina)": "https://hq.sinajs.cn/list=sh600519",
    "腾讯行情(qq)": "https://web.sqt.gtimg.cn/q=sh600519",
    "百度股市通": "https://finance.pae.baidu.com/selfselect/getstockquotation"
    "?all=1&isIndex=false&code=600519&isStock=true&newFormat=1&group=quotation_kline_ab&ktype=1",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://finance.sina.com.cn",
}


def main() -> int:
    cfg = get_config()
    print("代理设置:")
    print(f"  HTTP_PROXY  = {os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy') or '(未设置)'}")
    print(f"  HTTPS_PROXY = {os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy') or '(未设置)'}")
    print(f"  NO_PROXY    = {os.environ.get('NO_PROXY') or '(未设置)'}")
    print(f"  配置 bypass_proxy = {cfg.get('data.bypass_proxy')}")

    try:
        import urllib.request

        sys_proxy = urllib.request.getproxies()
    except Exception as exc:  # noqa: BLE001
        sys_proxy = f"读取失败: {exc}"
    print(f"  系统代理（注册表） = {sys_proxy}")

    print("\n可达性测试:")
    for name, url in TARGETS.items():
        try:
            resp = requests.get(url, headers=HEADERS, timeout=12, proxies={"http": None, "https": None})
            body = resp.text.strip()
            ok = resp.status_code == 200 and len(body) > 0
            sample = body[:70].replace("\n", " ")
            print(f"  [{'OK  ' if ok else 'FAIL'}] {name}: HTTP {resp.status_code}, {len(body)} bytes | {sample}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] {name}: {type(exc).__name__}: {str(exc)[:110]}")

    print("\n结论提示:")
    print("  · 若全部 FAIL 且报 ProxyError → 检查 config/settings.yaml 的 data.bypass_proxy 是否为 true")
    print("  · 若个别 FAIL 报 RemoteDisconnected → 该站点暂时限流，稍后重试或换源")
    return 0


if __name__ == "__main__":
    sys.exit(main())
