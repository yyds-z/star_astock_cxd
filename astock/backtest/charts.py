# -*- coding: utf-8 -*-
"""回测曲线：**策略历史回放的收益曲线**（净值）+ 因子/评分校准曲线。

设计要点（全部有依据，不是审美偏好）：

1. **零新依赖**：SVG 手绘 + ECharts HTML（沿用项目既有前端栈）。
   matplotlib 需要改 requirements.txt，而本项目刻意保持轻依赖。
2. **矢量**：SVG 可在浏览器无损放大；交互版用 ECharts 的 `renderer: 'svg'`。
3. **口径各自原生**（2026-09-24 P0 的教训）：两条链路必须用**各自可执行的**买卖点，
   不能为了"好看"统一口径 —— 那正是 `ret1` 时代的错误。
     · 主链路：买入日**开盘**买 → 次日收盘卖（`ads_backtest.ret_exit_d1c`）
     · 影子信号：信号日**收盘**买 → 次日收盘卖（`ads_shadow_pick.ret1`）
   两条都扣**双边 0.3%** 成本（印花税 0.05% + 佣金 + 滑点）。
4. **a11y**：曲线不靠颜色单独区分（同时有图例文字与线型/标注），
   这是 ui-ux-pro-max 图表域的硬性要求。
5. **不粉饰**：图上必须显示基准线与已知限制，避免"净值好看就下结论"。
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Sequence

import pandas as pd

from astock.logger import get_logger
from astock.storage.db import Storage, get_storage

logger = get_logger("backtest.charts")

# 双边成本：印花税 0.05%（卖出单边）+ 佣金 + 滑点，实测取 0.30%
COST_ROUND_TRIP = 0.003

# 配色（与 web/index.html 的既有 token 一致，红涨绿跌）
C_BG = "#0e1116"
C_PANEL = "#161b22"
C_LINE = "#242c3a"
C_TEXT = "#e6edf3"
C_MUTED = "#8b949e"
C_MAIN = "#58a6ff"     # 主链路（观察池）
C_SHADOW = "#f85149"   # 影子信号（决策依据，红=A股强势色）
C_BENCH = "#8b949e"    # 基准（灰，不抢眼）


# ============================================================
# 计算层
# ============================================================
@dataclass
class Curve:
    """一条净值曲线及其绩效指标。"""

    name: str
    dates: list[str] = field(default_factory=list)
    nav: list[float] = field(default_factory=list)      # 累计净值，起点 1.0
    color: str = C_MAIN
    dashed: bool = False
    note: str = ""                                       # 口径说明（显示在图例）
    daily: pd.Series | None = None                       # 日收益（用于统计）
    # 相对基准的日度超额与聚类 t。**必须与净值并排展示**：
    #   实测同一策略在「全区间」跑输基准 12.8pp、在「近一年」跑赢 24.6pp，
    #   而两个区间的日均超额 t 都不显著（0.09 / 1.04）——
    #   净值差距主要来自**波动拖累**（主链路日波动 3.4% vs 基准 1.7%），
    #   只看净值会得出相反结论。
    excess: dict[str, float] = field(default_factory=dict)

    def stats(self) -> dict[str, Any]:
        """常见绩效指标。样本不足时返回 None 而不是 0（0 会被误读成"真实测得的零"）。"""
        if self.daily is None or len(self.daily) < 2:
            return {}
        r = self.daily.dropna()
        if len(r) < 2:
            return {}
        n = len(r)
        total = float(self.nav[-1]) - 1.0
        years = n / 242.0                                # A 股约 242 个交易日/年
        ann = (1.0 + total) ** (1.0 / years) - 1.0 if years > 0 and total > -1 else None
        vol = float(r.std(ddof=1)) * math.sqrt(242)
        sharpe = float(r.mean()) / float(r.std(ddof=1)) * math.sqrt(242) if r.std(ddof=1) else None
        downside = r[r < 0]
        sortino = (float(r.mean()) / float(downside.std(ddof=1)) * math.sqrt(242)
                   if len(downside) > 1 and downside.std(ddof=1) else None)
        peak, mdd = -1e9, 0.0
        for v in self.nav:
            peak = max(peak, v)
            if peak > 0:
                mdd = min(mdd, v / peak - 1.0)
        return {
            "days": n,
            "total": total,
            "annual": ann,
            "vol": vol,
            "sharpe": sharpe,
            "sortino": sortino,
            "max_dd": mdd,
            "calmar": (ann / abs(mdd)) if (ann is not None and mdd < 0) else None,
            "win_rate": float((r > 0).mean()),
        }


def _compound(daily: pd.Series) -> Curve:
    nav, v = [], 1.0
    for x in daily:
        v *= (1.0 + float(x))
        nav.append(v)
    return Curve(name="", dates=[str(d) for d in daily.index], nav=nav, daily=daily)


def curve_from_series(daily: pd.Series, name: str, color: str,
                      note: str = "", dashed: bool = False) -> Curve:
    """从「日收益序列」构造曲线（**纯函数，不碰数据库**）。

    为什么单独抽出：展示层（FastAPI）只允许读 Parquet 快照、绝不能打开主库
    （DuckDB 独占锁会让 daily 无法运行），所以它需要一条不依赖 Storage 的构造路径，
    但它与报告必须用**完全相同的口径与统计**，否则页面上和报告里会给出两个答案。
    """
    c = _compound(daily)
    c.name, c.color, c.note, c.dashed = name, color, note, dashed
    return c


def attach_excess(curves: Sequence[Curve],
                  benchmark_keyword: str = "基准") -> Sequence[Curve]:
    """给各曲线附加相对基准的「日度超额 + 聚类 t」，并把所有曲线对齐到共同日期。

    两条用途合一：① 对齐（长度不齐的曲线画在一起会错位）；
    ② 显著性（净值高低会骗人，日超额/t 才是判据）。
    """
    curves = [c for c in curves if c.daily is not None and not c.daily.empty]
    if not curves:
        return []
    common = sorted(set.intersection(*[set(c.daily.index) for c in curves]))
    if not common:
        return list(curves)
    out: list[Curve] = []
    for c in curves:
        sub = c.daily.reindex(common).dropna()
        fixed = curve_from_series(sub, c.name, c.color, c.note, c.dashed)
        out.append(fixed)
    base = next((c for c in out if benchmark_keyword in c.name), None)
    if base is not None:
        for c in out:
            if c is base:
                continue
            d = (c.daily - base.daily).dropna()
            if len(d) > 2 and d.std(ddof=1):
                c.excess = {
                    "mean": float(d.mean()),
                    "t": float(d.mean() / (d.std(ddof=1) / math.sqrt(len(d)))),
                    "days": float(len(d)),
                }
    return out


def load_main_equity(storage: Storage, run_id: str) -> Curve:
    """主链路净值：按 `data_date` 等权合并当日 15 只，口径 = 次日开盘买→再次日收盘卖。

    ⚠️ 近似说明：该策略持仓跨 2 个交易日，逐日复利隐含"每日全额再平衡"假设。
    真实资金会在相邻两日之间重叠占用。图上必须标注这一点。
    """
    df = storage.query_df(
        "SELECT CAST(data_date AS VARCHAR) AS d, AVG(ret_exit_d1c) AS r "
        "FROM ads_backtest WHERE run_id = ? AND ret_exit_d1c IS NOT NULL "
        "GROUP BY data_date ORDER BY data_date",
        [run_id],
    )
    if df.empty:
        return Curve(name="主链路（观察池）", color=C_MAIN,
                     note="无数据：请先跑 backtest 且该轮次需含 ret_exit_d1c 列")
    s = pd.Series((df["r"] / 100.0 - COST_ROUND_TRIP).to_numpy(), index=df["d"].tolist())
    c = _compound(s)
    c.name, c.color, c.note = "主链路（观察池）", C_MAIN, "次日开盘买→再次日收盘卖，扣0.3%"
    return c


def load_shadow_equity(storage: Storage, start: str | None = None,
                       end: str | None = None) -> Curve:
    """影子信号净值：按 `date` 等权合并当日候选，口径 = 信号日收盘买→次日收盘卖。

    这条**不重叠**（收盘买、次日收盘卖，逐日衔接），所以逐日复利是精确的，不需要近似假设。
    """
    sql = ("SELECT CAST(date AS VARCHAR) AS d, AVG(ret1) AS r FROM ads_shadow_pick "
           "WHERE ret1 IS NOT NULL")
    params: list[Any] = []
    if start:
        sql += " AND date >= ?"
        params.append(start)
    if end:
        sql += " AND date <= ?"
        params.append(end)
    sql += " GROUP BY date ORDER BY date"
    df = storage.query_df(sql, params)
    if df.empty:
        return Curve(name="影子信号（决策依据）", color=C_SHADOW, note="无已结算样本")
    s = pd.Series((df["r"] / 100.0 - COST_ROUND_TRIP).to_numpy(), index=df["d"].tolist())
    c = _compound(s)
    c.name, c.color, c.note = "影子信号（决策依据）", C_SHADOW, "信号日收盘买→次日收盘卖，扣0.3%"
    return c


def load_benchmark(storage: Storage, start: str | None = None,
                   end: str | None = None) -> Curve:
    """基准：**同池等权**（可交易池），口径与主链路一致 —— 基准必须与所比较的策略同口径。

    为什么不用指数：指数含大量非候选标的、且成分会调整。判断"选股是否有效"的正确对照
    是"同一天、同一可交易池、随便买"能拿到多少。
    """
    sql = """
    WITH pool AS (
        SELECT code, out_date, ipo_date, board, name FROM dim_stock
    ),
    fwd AS (
        SELECT b.date,
               (LEAD(b.close, 2) OVER (PARTITION BY b.code ORDER BY b.date)
                / NULLIF(LEAD(b.open, 1) OVER (PARTITION BY b.code ORDER BY b.date), 0) - 1) AS r
        FROM dwd_daily_bar b
        JOIN pool p ON p.code = b.code
        WHERE b.date >= ?
          AND p.board IN ('main', 'gem')
          AND p.name NOT LIKE '%ST%'
          AND (p.out_date IS NULL OR p.out_date > b.date)
          AND p.ipo_date <= b.date - INTERVAL 120 DAY
    )
    SELECT CAST(date AS VARCHAR) AS d, AVG(r) AS r
    FROM fwd WHERE r IS NOT NULL AND r > -0.5 AND r < 0.5
    GROUP BY date ORDER BY date
    """
    params: list[Any] = [start or "2000-01-01"]
    if end:
        sql = sql.replace("GROUP BY date ORDER BY date",
                          "HAVING date <= ? GROUP BY date ORDER BY date")
        params.append(end)
    df = storage.query_df(sql, params)
    if df.empty:
        return Curve(name="基准（同池等权）", color=C_BENCH, dashed=True, note="无数据")
    s = pd.Series((df["r"] - COST_ROUND_TRIP).to_numpy(), index=df["d"].tolist())
    c = _compound(s)
    c.name, c.color, c.dashed = "基准（同池等权）", C_BENCH, True
    c.note = "同一可交易池等权，同口径，扣0.3%"
    return c


def build_curves(storage: Storage | None = None, run_id: str | None = None,
                 start: str | None = None, end: str | None = None) -> list[Curve]:
    """构建三条曲线并**对齐到共同区间**（否则长度不同的曲线无法对照）。"""
    st = storage or get_storage()
    if run_id is None:
        run_id = st.query_value("SELECT MAX(run_id) FROM ads_backtest") or ""
    main = load_main_equity(st, run_id)
    shadow = load_shadow_equity(st, start, end)
    bench = load_benchmark(st, start, end)

    # 对齐到共同日期 + 计算相对基准的日度超额与 t（与展示层共用同一实现）
    out = attach_excess([shadow, main, bench])
    if out and out[0].dates:
        logger.info("曲线区间对齐：%s ~ %s（共同 %d 个交易日，run_id=%s）",
                    out[0].dates[0], out[0].dates[-1], len(out[0].dates), run_id)
    return list(out)


# ============================================================
# 校准曲线（评分分档 → 实际收益）
# ============================================================
def calibration_main(storage: Storage, run_id: str, bins: int = 10) -> pd.DataFrame:
    """主链路：按 final_score 分位 → 该档平均可实现收益（%）。"""
    return _calibration(
        storage,
        f"""SELECT final_score AS score, ret_exit_d1c AS ret FROM ads_backtest
            WHERE run_id = '{run_id}' AND ret_exit_d1c IS NOT NULL""",
        bins,
    )


def calibration_shadow(storage: Storage, bins: int = 10) -> pd.DataFrame:
    """影子信号：按 signal_score（缩量程度）分位 → 该档平均收益（%）。"""
    return _calibration(
        storage,
        "SELECT signal_score AS score, ret1 AS ret FROM ads_shadow_pick WHERE ret1 IS NOT NULL",
        bins,
    )


def _calibration(storage: Storage, sql: str, bins: int) -> pd.DataFrame:
    df = storage.query_df(sql)
    if df.empty or df["score"].notna().sum() < bins * 5:
        return pd.DataFrame()
    df = df.dropna(subset=["score", "ret"]).copy()
    try:
        df["bin"] = pd.qcut(df["score"], bins, labels=False, duplicates="drop")
    except ValueError:
        return pd.DataFrame()
    g = df.groupby("bin").agg(n=("ret", "size"), ret=("ret", "mean"),
                              lo=("score", "min"), hi=("score", "max"))
    # 该档相对全样本均值的超额（去掉市场整体涨跌的影响）
    g["excess"] = g["ret"] - df["ret"].mean()
    return g.reset_index()


# ============================================================
# 渲染：SVG（矢量，可内嵌 Markdown）
# ============================================================
@dataclass
class SvgTheme:
    w: int = 960
    h: int = 420
    pad_l: int = 62
    pad_r: int = 96
    pad_t: int = 34
    pad_b: int = 46


def _nice_ticks(lo: float, hi: float, count: int = 5) -> list[float]:
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / max(1, count)
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if raw / step <= 1.5:
            break
    start = math.floor(lo / step) * step
    ticks, v = [], start
    while v <= hi + step * 0.5:
        if v >= lo - step * 0.5:
            ticks.append(round(v, 6))
        v += step
    return ticks


def render_equity_svg(curves: Sequence[Curve], title: str = "策略历史回放 · 累计净值",
                      subtitle: str = "") -> str:
    """把若干条净值曲线画成**手绘 SVG**（矢量、无外部依赖）。

    刻意不做外发光/霓虹（anti-slop 规则），只用细线与轻微描边区分。
    """
    curves = [c for c in curves if c.dates and c.nav]
    if not curves:
        return _empty_svg("暂无数据：需先跑回测（主链路）与影子结算")

    th = SvgTheme()
    xs = sorted({d for c in curves for d in c.dates})
    xi = {d: i for i, d in enumerate(xs)}
    lo = min(min(c.nav) for c in curves)
    hi = max(max(c.nav) for c in curves)
    pad = (hi - lo) * 0.08 or 0.02
    ylo, yhi = lo - pad, hi + pad

    def px(i: int) -> float:
        return th.pad_l + (th.w - th.pad_l - th.pad_r) * (i / max(1, len(xs) - 1))

    def py(v: float) -> float:
        return th.pad_t + (th.h - th.pad_t - th.pad_b) * (1 - (v - ylo) / (yhi - ylo))

    s: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {th.w} {th.h}" '
        f'width="100%" role="img" aria-label="{html.escape(title)}" '
        f'font-family="-apple-system,Segoe UI,Microsoft YaHei,sans-serif">',
        f'<rect width="{th.w}" height="{th.h}" fill="{C_PANEL}" stroke="{C_LINE}"/>',
        f'<text x="{th.pad_l}" y="21" fill="{C_TEXT}" font-size="14" font-weight="600">'
        f'{html.escape(title)}</text>',
    ]
    if subtitle:
        s.append(f'<text x="{th.pad_l}" y="34.5" fill="{C_MUTED}" font-size="10.5">'
                 f'{html.escape(subtitle)}</text>')

    # 水平网格与 Y 轴刻度（净值）
    for t in _nice_ticks(ylo, yhi, 5):
        y = py(t)
        s.append(f'<line x1="{th.pad_l}" y1="{y:.1f}" x2="{th.w - th.pad_r}" y2="{y:.1f}" '
                 f'stroke="{C_LINE}" stroke-width="1"/>')
        s.append(f'<text x="{th.pad_l - 7}" y="{y + 3.5:.1f}" fill="{C_MUTED}" font-size="10" '
                 f'text-anchor="end">{t:.2f}</text>')
    # 净值 1.00 基准线（更淡更细，避免与数据线混淆）
    if ylo < 1.0 < yhi:
        y = py(1.0)
        s.append(f'<line x1="{th.pad_l}" y1="{y:.1f}" x2="{th.w - th.pad_r}" y2="{y:.1f}" '
                 f'stroke="{C_MUTED}" stroke-dasharray="2 3" stroke-width="1"/>')

    # X 轴：均匀取 6 个日期标签
    for k in range(6):
        if len(xs) < 2:
            break
        i = round(k * (len(xs) - 1) / 5)
        s.append(f'<text x="{px(i):.1f}" y="{th.h - th.pad_b + 16}" fill="{C_MUTED}" '
                 f'font-size="10" text-anchor="middle">{xs[i][5:]}</text>')

    # 数据线
    for c in curves:
        pts = " ".join(f"{px(xi[d]):.1f},{py(v):.1f}" for d, v in zip(c.dates, c.nav)
                       if d in xi)
        dash = ' stroke-dasharray="5 3"' if c.dashed else ""
        s.append(f'<polyline points="{pts}" fill="none" stroke="{c.color}" '
                 f'stroke-width="{1.6 if not c.dashed else 1.4}"{dash} '
                 f'stroke-linejoin="round" stroke-linecap="round"/>')
        # 末端数值标签（直接给数，不要求读者去猜线条）
        if c.nav:
            d_last = c.dates[-1]
            if d_last in xi:
                s.append(f'<text x="{px(xi[d_last]) + 4:.1f}" y="{py(c.nav[-1]) + 3.5:.1f}" '
                         f'fill="{c.color}" font-size="10.5" font-weight="600">'
                         f'{c.nav[-1]:.2f}×</text>')

    # 图例（带口径说明 —— 两条链路口径不同，必须写清；并给出日度超额与 t）
    y = th.pad_t + 4
    for c in curves:
        st = c.stats()
        dash_attr = ' stroke-dasharray="5 3"' if c.dashed else ""
        s.append(f'<line x1="{th.w - th.pad_r + 8}" y1="{y}" x2="{th.w - th.pad_r + 26}" '
                 f'y2="{y}" stroke="{c.color}" stroke-width="2"{dash_attr}/>')
        s.append(f'<text x="{th.w - th.pad_r + 31}" y="{y + 3.5}" fill="{C_TEXT}" '
                 f'font-size="10">{html.escape(c.name)}</text>')
        line2 = ""
        if st:
            ann = st.get("annual")
            if ann is not None:
                line2 = f"年化 {ann * 100:+.1f}%　回撤 {st['max_dd'] * 100:.1f}%"
        if line2:
            s.append(f'<text x="{th.w - th.pad_r + 31}" y="{y + 15}" fill="{C_MUTED}" '
                     f'font-size="9">{html.escape(line2)}</text>')
        if c.excess:
            s.append(f'<text x="{th.w - th.pad_r + 31}" y="{y + 26}" fill="{c.color}" '
                     f'font-size="9">日超额 {c.excess["mean"] * 100:+.3f}%　t={c.excess["t"]:.2f}</text>')
        if c.note:
            s.append(f'<text x="{th.w - th.pad_r + 31}" y="{y + 37}" fill="{C_MUTED}" '
                     f'font-size="8.5">{html.escape(c.note)}</text>')
        y += 50
    # 判读提示（防止"净值好看就下结论"）
    s.append(f'<text x="{th.pad_l}" y="{th.h - 12}" fill="{C_MUTED}" font-size="9">'
             f'判据看「日超额 / t」而不是净值高低：净值受区间与波动拖累影响，'
             f'同一策略在不同区间可给出相反结论。</text>')
    s.append("</svg>")
    return "\n".join(s)


def render_calibration_svg(cal: pd.DataFrame, label: str = "评分分位",
                           title: str = "校准曲线：评分分档 → 实际收益") -> str:
    """校准曲线用**折线+点+零轴**而非柱状：单调性一眼可读。"""
    if cal is None or cal.empty:
        return _empty_svg("校准曲线：样本不足（需 ≥50 条且分档后每档有样本）")
    th = SvgTheme(h=300, pad_l=58, pad_r=70, pad_b=52)
    vals = list(cal["excess"].astype(float))
    n = len(vals)
    lo, hi = min(0.0, min(vals)), max(0.0, max(vals))
    pad = (hi - lo) * 0.15 or 0.1
    ylo, yhi = lo - pad, hi + pad

    def px(i: int) -> float:
        return th.pad_l + (th.w - th.pad_l - th.pad_r) * (i / max(1, n - 1))

    def py(v: float) -> float:
        return th.pad_t + (th.h - th.pad_t - th.pad_b) * (1 - (v - ylo) / (yhi - ylo))

    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {th.w} {th.h}" width="100%" '
         f'role="img" aria-label="{html.escape(title)}" '
         f'font-family="-apple-system,Segoe UI,Microsoft YaHei,sans-serif">',
         f'<rect width="{th.w}" height="{th.h}" fill="{C_PANEL}" stroke="{C_LINE}"/>',
         f'<text x="{th.pad_l}" y="21" fill="{C_TEXT}" font-size="14" font-weight="600">'
         f'{html.escape(title)}</text>',
         f'<text x="{th.pad_l}" y="34" fill="{C_MUTED}" font-size="10.5">'
         f'纵轴=该档相对全样本均值的超额收益(%)；若评分有效，应自左向右单调上升</text>']
    for t in _nice_ticks(ylo, yhi, 4):
        y = py(t)
        s.append(f'<line x1="{th.pad_l}" y1="{y:.1f}" x2="{th.w - th.pad_r}" y2="{y:.1f}" '
                 f'stroke="{C_LINE}"/>')
        s.append(f'<text x="{th.pad_l - 7}" y="{y + 3.5:.1f}" fill="{C_MUTED}" font-size="10" '
                 f'text-anchor="end">{t:+.2f}</text>')
    y0 = py(0.0)
    s.append(f'<line x1="{th.pad_l}" y1="{y0:.1f}" x2="{th.w - th.pad_r}" y2="{y0:.1f}" '
             f'stroke="{C_MUTED}" stroke-dasharray="3 3"/>')
    pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(vals))
    s.append(f'<polyline points="{pts}" fill="none" stroke="{C_MAIN}" stroke-width="1.8"/>')
    for i, v in enumerate(vals):
        cnt = int(cal["n"].iloc[i]) if "n" in cal.columns else 0
        s.append(f'<circle cx="{px(i):.1f}" cy="{py(v):.1f}" r="3.2" fill="{C_MAIN}"/>')
        s.append(f'<text x="{px(i):.1f}" y="{py(v) + (14 if v >= 0 else -7):.1f}" '
                 f'fill="{C_MUTED}" font-size="8.5" text-anchor="middle">{cnt}</text>')
        s.append(f'<text x="{px(i):.1f}" y="{th.h - th.pad_b + 16}" fill="{C_MUTED}" '
                 f'font-size="9.5" text-anchor="middle">Q{i + 1}</text>')
    s.append(f'<text x="{th.w - th.pad_r + 6}" y="{th.h / 2:.0f}" fill="{C_MUTED}" font-size="9.5" '
             f'transform="rotate(90 {th.w - th.pad_r + 6} {th.h / 2:.0f})">'
             f'{html.escape(label)}</text>')
    s.append("</svg>")
    return "\n".join(s)


def _empty_svg(msg: str) -> str:
    th = SvgTheme(h=160)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {th.w} {th.h}" width="100%">'
            f'<rect width="{th.w}" height="{th.h}" fill="{C_PANEL}" stroke="{C_LINE}"/>'
            f'<text x="{th.w / 2}" y="{th.h / 2}" fill="{C_MUTED}" font-size="12" '
            f'text-anchor="middle">{html.escape(msg)}</text></svg>')


# ============================================================
# 渲染：交互 HTML（ECharts，SVG 渲染器 → 保持矢量 + 可框选缩放）
# ============================================================
_HTML_TMPL = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
  :root{--bg:#0e1116;--panel:#161b22;--line:#242c3a;--text:#e6edf3;--muted:#8b949e}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;font-size:14px}
  header{padding:16px 20px 6px}
  h1{margin:0;font-size:17px;font-weight:600;letter-spacing:.3px}
  .sub{color:var(--muted);font-size:12px;margin-top:6px;line-height:1.7}
  .wrap{padding:0 20px 24px;display:grid;gap:16px}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
  .panel h2{margin:0 0 10px;font-size:13px;font-weight:600;color:var(--text)}
  #equity{width:100%;height:460px}
  #calibs .calib{width:100%;height:320px}
  .calib-title{font-size:12.5px;color:var(--muted);margin:12px 0 2px}
  table{border-collapse:collapse;width:100%;font-size:12.5px}
  th,td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:right}
  th:first-child,td:first-child{text-align:left}
  th{color:var(--muted);font-weight:500;background:#1c2230}
  .pos{color:#f85149}.neg{color:#3fb950}
  .muted{color:var(--muted)}
  .hint{color:var(--muted);font-size:11.5px;margin-top:8px;line-height:1.7}
  .legend-tip{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px}
</style></head><body>
<header>
  <h1>__TITLE__</h1>
  <div class="sub">__SUBTITLE__</div>
</header>
<div class="wrap">
  <section class="panel">
    <h2>累计净值（可拖拽框选区间放大；悬停查看当日数值）</h2>
    <div id="equity"></div>
    <div class="hint">口径：主链路=次日开盘买→再次日收盘卖；影子信号=信号日收盘买→次日收盘卖；
      基准=同一可交易池等权。三者均已扣双边 0.3% 成本。<b>主链路逐日复利为近似</b>（其持仓跨 2 日，存在重叠）。
      <br><b>判读提示</b>：请以「日超额 / t」为准，不要只看净值高低 —— 净值同时受<b>区间选择</b>与
      <b>波动拖累</b>影响（策略日波动越大，同样的日均收益复利越差）。实测同一策略在全区间跑输基准 12.8pp、
      在近一年跑赢 24.6pp，而两个区间的日度超额 t 都不显著。</div>
  </section>
  <section class="panel">
    <h2>绩效指标</h2>
    <div id="stats"></div>
  </section>
  <section class="panel">
    <h2>校准曲线：评分分档 → 实际超额收益</h2>
    <div id="calibs"></div>
    <div class="hint">纵轴为该分位相对全样本均值的超额收益；柱上数字为该档样本数。
      若评分具备排序能力，曲线应自左向右单调上升（不再要求读者去猜"分数越高是否真的越好"）。</div>
  </section>
</div>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<script>
const DATA = __PAYLOAD__;
if (typeof echarts === "undefined") {
  document.body.insertAdjacentHTML("beforeend",
    '<p class="hint" style="padding:0 20px">ECharts 未能加载（离线环境）：请下载 echarts.min.js 到本地并改 <code>script src</code>。</p>');
} else {
  const AX = {axisLine:{lineStyle:{color:"#30363d"}},
              axisLabel:{color:"#8b949e",fontSize:11},
              splitLine:{lineStyle:{color:"#1f242c"}}};
  const TIP = {trigger:"axis",backgroundColor:"#161b22",borderColor:"#30363d",
               textStyle:{color:"#e6edf3",fontSize:12}};
  const eq = echarts.init(document.getElementById("equity"), null, {renderer:"svg"});
  eq.setOption({
    backgroundColor:"transparent",
    tooltip:Object.assign({}, TIP, {valueFormatter:v => v==null?"-":(+v).toFixed(4)}),
    legend:{top:0,right:0,textStyle:{color:"#8b949e"},data:DATA.curves.map(c=>c.name)},
    grid:{left:64,right:96,top:48,bottom:52},
    xAxis:{type:"category",data:DATA.dates,boundaryGap:false,...AX},
    yAxis:{type:"value",scale:true,...AX,name:"净值",nameTextStyle:{color:"#8b949e"}},
    dataZoom:[{type:"inside"},{type:"slider",height:18,bottom:10,
               backgroundColor:"#1c2230",borderColor:"#30363d",
               textStyle:{color:"#8b949e"},fillerColor:"#58a6ff33"}],
    series:DATA.curves.map(c=>({name:c.name,type:"line",data:c.nav,showSymbol:false,
             lineStyle:{width:2,color:c.color,type:c.dashed?"dashed":"solid"},
             itemStyle:{color:c.color},emphasis:{focus:"series"},
             endLabel:{show:true,color:c.color,fontSize:11,formatter:"{a} {c}"}}))
  });
  window.addEventListener("resize", () => eq.resize());

  const calBox = document.getElementById("calibs");
  const groups = (DATA.calibs && DATA.calibs.length) ? DATA.calibs : [];
  if (!groups.length) {
    calBox.insertAdjacentHTML("beforeend",
      '<p class="hint">样本不足：校准曲线需要每档至少若干样本（当前不足）。</p>');
  }
  groups.forEach((g, i) => {
    calBox.insertAdjacentHTML("beforeend", `<div class="calib-title">${g.title}</div>`);
    const div = document.createElement("div");
    div.className = "calib";
    div.id = "calib" + i;
    calBox.appendChild(div);
    const ch = echarts.init(div, null, {renderer:"svg"});
    ch.setOption({
      backgroundColor:"transparent", tooltip:TIP,
      grid:{left:64,right:24,top:18,bottom:44},
      xAxis:{type:"category",data:g.labels,name:"评分分位",nameLocation:"middle",
             nameGap:26,nameTextStyle:{color:"#8b949e"},...AX},
      yAxis:{type:"value",...AX,name:"超额%",nameTextStyle:{color:"#8b949e"}},
      series:[
        {type:"bar",data:g.excess,barMaxWidth:24,
         itemStyle:{color:p => p.value >= 0 ? "#f85149" : "#3fb950"},
         label:{show:true,position:"top",color:"#8b949e",fontSize:9.5,
                formatter:p => g.n[p.dataIndex]}},
        {type:"line",data:g.excess,symbolSize:6,smooth:false,
         lineStyle:{color:"#58a6ff",width:1.8},itemStyle:{color:"#58a6ff"},
         markLine:{silent:true,symbol:"none",lineStyle:{color:"#8b949e",type:"dashed"},
                   data:[{yAxis:0}]}}
      ]
    });
    window.addEventListener("resize", () => ch.resize());
  });

  // 指标表（用真实数据，不写死）
  const rows = DATA.curves.map(c => {
    const s = c.stats || {}, e = c.excess || {};
    const fmt = (v, pct) => (v == null || !isFinite(v)) ? "-"
      : (pct ? (v*100).toFixed(2)+"%" : v.toFixed(2));
    const tCls = (e.t ?? 0) >= 2 ? "pos" : "muted";
    return `<tr><td><span class="legend-tip" style="background:${c.color}"></span>${c.name}</td>
      <td>${s.days ?? "-"}</td>
      <td class="${(s.total||0)>=0?"pos":"neg"}">${fmt(s.total,true)}</td>
      <td class="${(s.annual||0)>=0?"pos":"neg"}">${fmt(s.annual,true)}</td>
      <td class="neg">${fmt(s.max_dd,true)}</td>
      <td>${fmt(s.sharpe)}</td>
      <td>${fmt(s.calmar)}</td>
      <td>${fmt(s.win_rate,true)}</td>
      <td class="${(e.mean||0)>=0?"pos":"neg"}">${e.mean==null?"-":(e.mean*100).toFixed(3)+"%"}</td>
      <td class="${tCls}">${e.t==null?"-":e.t.toFixed(2)}</td></tr>`;
  }).join("");
  document.getElementById("stats").innerHTML =
    `<table><thead><tr><th>曲线</th><th>交易日</th><th>累计</th><th>年化</th>
     <th>最大回撤</th><th>Sharpe</th><th>Calmar</th><th>日胜率</th>
     <th>日超额</th><th>t</th></tr></thead>
     <tbody>${rows}</tbody></table>
     <div class="hint">日超额 = 相对基准的日均差值（同一交易日集合）；t = 日度聚类的显著性。
     判据：|t| ≥ 2 才算有统计依据 —— 这是唯一能抵抗"区间挑选"的口径。</div>`;
}
</script></body></html>
"""


def render_equity_html(curves: Sequence[Curve],
                       calibs: dict[str, pd.DataFrame] | None = None,
                       title: str = "策略历史回放 · 净值与校准",
                       subtitle: str = "") -> str:
    """自包含交互页（除 ECharts CDN 外无外部依赖）。可框选区间放大、悬停看每日数值。

    `calibs` 为「名称 → 分档表」的映射，每个条目渲染一张校准图（主链路与影子各自一张，
    便于并列对照"哪条链路的评分真的有排序能力"）。
    """
    curves = [c for c in curves if c.dates and c.nav]
    # 只保留所有曲线共有的日期（长度不齐会让 x 轴错位）
    common = sorted(set.intersection(*[set(c.dates) for c in curves])) if curves else []
    payload: dict[str, Any] = {
        "dates": common,
        "curves": [
            {
                "name": c.name,
                "color": c.color,
                "dashed": c.dashed,
                "nav": [round(c.nav[c.dates.index(d)], 5) if d in c.dates else None for d in common],
                "stats": {k: (round(v, 6) if isinstance(v, float) else v)
                          for k, v in c.stats().items()},
                "excess": {k: round(v, 6) for k, v in (c.excess or {}).items()},
            }
            for c in curves
        ],
        "calibs": [],
    }
    for name, cal in (calibs or {}).items():
        if cal is None or cal.empty:
            continue
        payload["calibs"].append({
            "title": name,
            "labels": [f"Q{i + 1}" for i in range(len(cal))],
            "excess": [round(float(v), 4) for v in cal["excess"]],
            "n": [int(v) for v in cal["n"]],
        })
    import json

    return (_HTML_TMPL
            .replace("__TITLE__", html.escape(title))
            .replace("__SUBTITLE__", html.escape(subtitle).replace("\n", "<br>"))
            .replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False)))
