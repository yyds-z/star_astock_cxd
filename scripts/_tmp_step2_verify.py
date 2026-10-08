# -*- coding: utf-8 -*-
"""第2步收尾验证（用后即删）：
   ① is_sealed 回填情况
   ② 展示层过滤后的真实成绩
   ③ 服务进程是否占用主库（必须不占）
"""
import io
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from astock.eval import attach_benchmark, executable, picks_with_returns, pool_benchmark, summarise, format_table  # noqa: E402
from astock.storage.db import get_storage  # noqa: E402

st = get_storage()
c = st.conn
print("【1】is_sealed 回填")
print(c.execute("""
    SELECT COUNT(*) AS 总行, SUM(CASE WHEN is_sealed THEN 1 ELSE 0 END) AS 已封板,
           SUM(CASE WHEN exec_d1 IS NOT NULL THEN 1 ELSE 0 END) AS exec已结算
    FROM ads_shadow_pick
""").df().to_string(index=False))

print()
print("【2】展示层口径（过滤封板 + 流动性）下的真实成绩")
df = attach_benchmark(picks_with_returns(
    st, "SELECT date, code, name, signal_score, amount_ma20, is_sealed "
        "FROM ads_shadow_pick WHERE (is_sealed IS NULL OR is_sealed = FALSE)"),
    pool_benchmark(st))
rows = []
for k in ("a", "b"):
    rows.append({**summarise(executable(df, k), k), "label": f"口径{k.upper()}·可执行"})
print(format_table(rows))
print()
print("与库里已结算列是否一致（抽查 exec_d1 vs 现场重算）：")
import pandas as pd  # noqa: E402
chk = c.execute("SELECT date, code, exec_d1, exec_bench FROM ads_shadow_pick "
                "WHERE exec_d1 IS NOT NULL AND is_sealed = FALSE").df()
m = chk.merge(df[["date", "code", "r_b", "bm_b"]], on=["date", "code"], how="inner")
print(f"    行数 {len(m)}　exec_d1 最大差 {(m['exec_d1'] - m['r_b']).abs().max():.6f}"
      f"　exec_bench 最大差 {(m['exec_bench'] - m['bm_b']).abs().max():.6f}")

print()
print("【3】服务进程是否占用主库（API 绝不能碰主库）")
import duckdb  # noqa: E402
try:
    _t = duckdb.connect("data/astock.duckdb")
    _t.close()
    print("    主库空闲 ✅（serve 进程没有打开它）")
except Exception as e:  # noqa: BLE001
    print(f"    ✖ 主库被占用：{str(e)[:160]}")

print()
print("【4】接口")
for p in ("/api/shadow/latest", "/api/shadow/history?days=90"):
    try:
        d = json.loads(urllib.request.urlopen("http://127.0.0.1:8000" + p, timeout=60).read().decode())
        if "count" in d:
            print(f"    {p} → {d['count']} 只　缩量门槛 {d.get('vol_ratio_max')}"
                  f"　当日共 {d.get('total_on_date')} 行")
        else:
            print(f"    {p} → 聚合 {d.get('aggregate')}")
    except Exception as e:  # noqa: BLE001
        print(f"    {p} → ✖ {type(e).__name__}: {str(e)[:100]}")
