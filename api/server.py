# -*- coding: utf-8 -*-
"""FastAPI 本地服务（展示层）。

架构要点：服务端**只读 Parquet 快照**，不打开主数据库。
原因是 DuckDB 对主库文件采用独占锁，若服务长期持有连接，
`daily` / `backfill` 就无法运行。快照由 `python -m astock.cli daily` 结束时自动导出。

这些接口是纯数据接口（无模板渲染），Phase 3 迁移到 Vue3 时可直接复用。
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from astock.config import get_config, params_version
from astock.logger import get_logger, setup_logging
from astock.storage.serving import get_reader, serving_dir

cfg = get_config()
setup_logging(cfg.log_dir)
logger = get_logger("api")

app = FastAPI(title="A股 AI 选股系统", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

WEB_DIR = cfg.project_root / "web"


# ---------------- 工具 ----------------
def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    """清洗单行数据：NaN/NaT/pd.NA → None，日期 → 字符串。"""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if value is None:
            out[key] = None
            continue
        try:
            if pd.isna(value):
                out[key] = None
                continue
        except (TypeError, ValueError):
            pass
        out[key] = str(value) if hasattr(value, "isoformat") else value
    return out


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    return json.loads(df.to_json(orient="records", force_ascii=False, date_format="iso"))


def _need_snapshot() -> None:
    if not get_reader().available():
        raise HTTPException(
            503,
            "服务层快照尚未生成。请先执行：python -m astock.cli daily（或 astock.cli export）",
        )


# ---------------- 页面 ----------------
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    index_file = WEB_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(404, "前端页面缺失")
    return FileResponse(index_file)


# ---------------- 基础 ----------------
@app.get("/api/health")
def health() -> dict[str, Any]:
    meta = get_reader().meta()
    return {
        "ok": True,
        "mode": "serving-snapshot",
        "snapshot_exported_at": meta.get("exported_at"),
        "serving_dir": str(serving_dir()),
        "params_version": params_version(),
    }


@app.get("/api/status")
def status() -> dict[str, Any]:
    meta = get_reader().meta()
    return {
        "mode": "serving-snapshot",
        "exported_at": meta.get("exported_at"),
        "summary": meta.get("summary") or {},
        "tables": meta.get("tables") or {},
    }


@app.get("/api/config")
def api_config() -> dict[str, Any]:
    """前端需要的配置（不含任何密钥）。

    2026-10-08：`strategies` 字段随策略层删除 —— 前端不再有"策略/档位"概念。
    """
    return {
        "project": cfg.get("project.name"),
        "new_stock_days": cfg.get("universe.new_stock_days"),
        "llm_enabled": bool(cfg.get("llm.enabled", False)),
    }


# ---------------- 市场状态 ----------------
@app.get("/api/market/latest")
def market_latest() -> JSONResponse:
    _need_snapshot()
    rows = get_reader().records(
        "SELECT * FROM market_regime ORDER BY date DESC LIMIT 1", "market_regime"
    )
    if not rows:
        return JSONResponse({"available": False, "data": None})
    return JSONResponse({"available": True, "data": _jsonable(rows[0])})


@app.get("/api/market/history")
def market_history(days: int = Query(120, ge=5, le=2000)) -> dict[str, Any]:
    _need_snapshot()
    rows = get_reader().records(
        "SELECT * FROM market_regime ORDER BY date DESC LIMIT ?", "market_regime", [days]
    )
    return {"records": list(reversed(rows))}


# ---------------- 推荐 ----------------
@app.get("/api/recommend/latest")
def recommend_latest(offset: int = Query(0, ge=0, le=60)) -> dict[str, Any]:
    _need_snapshot()
    reader = get_reader()
    dates = reader.records(
        "SELECT DISTINCT rec_date FROM recommend ORDER BY rec_date DESC LIMIT ?",
        "recommend",
        [offset + 1],
    )
    if len(dates) <= offset:
        return {"available": False, "records": []}

    target = dates[offset]["rec_date"]
    records = reader.records(
        "SELECT * FROM recommend WHERE rec_date = ? ORDER BY tier, tier_rank",
        "recommend",
        [target],
    )
    for r in records:
        for key, default in (("reasons", "[]"), ("score_detail", "{}")):
            try:
                r[key] = json.loads(r.get(key) or default)
            except (TypeError, ValueError):
                r[key] = [] if key == "reasons" else {}

    review_map: dict[str, dict] = {}
    if records:
        reviews = reader.records(
            "SELECT rec_id, next_pct_chg, next_high_pct, next_low_pct, hold3_pct, result, next_date "
            "FROM review WHERE rec_date = ?",
            "review",
            [target],
        )
        review_map = {r["rec_id"]: r for r in reviews}
    for r in records:
        r["review"] = review_map.get(r["rec_id"])

    return {
        "available": True,
        "rec_date": target,
        "trade_date": records[0]["trade_date"] if records else None,
        "market_state": records[0]["market_label"] if records else None,
        "market_state_code": records[0]["market_state"] if records else None,
        "records": records,
    }


@app.get("/api/recommend/history")
def recommend_history(days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    _need_snapshot()
    # INTERVAL 不支持参数占位符，days 已由 FastAPI 限定为 1~365 的整数，可安全内联
    return {
        "records": get_reader().records(
            "SELECT * FROM recommend WHERE rec_date >= (SELECT MAX(rec_date) FROM recommend) "
            f"- INTERVAL {int(days)} DAY ORDER BY rec_date DESC, tier, tier_rank",
            "recommend",
        )
    }


# ---------------- 影子信号（唯一候选来源，处于纸面跟踪观察期）----------------
# 2026-10-08：主链路的复盘端点（/api/review/*）随主链路一并删除 ——
# 它们复盘的对象是 ads_recommend 里的 15 只候选，而那些策略经体检
# 全部无可实现 alpha。影子自身的成绩回顾由 /api/shadow/history 直接给出
# （结算在 ads_shadow_pick 内完成）。
_ZT_CAP = {"30": 19.5, "68": 19.5}  # 创业板/科创板 20cm


def _shadow_min_score() -> float:
    """影子模块的「信号分下限」（低于它不推荐）。

    直接调用引擎侧的实现（`astock.shadow.shadow_min_signal_score`）——
    展示层**绝不重新实现**口径判断：一旦这里写死 50，改配置就只会改到引擎，
    页面仍在展示已被淘汰的名单。
    """
    from astock.shadow import shadow_min_signal_score

    return shadow_min_signal_score()


def _shadow_min_amt() -> float:
    """流动性下限（前 20 日均成交额），0 = 未启用。同样是引擎侧的实现。"""
    from astock.shadow import shadow_min_amount_avg20

    return shadow_min_amount_avg20()


def _shadow_filters() -> tuple[str, list[Any]]:
    """影子名单的准入条件 → (SQL 片段, 参数)。

    为什么要集中拼一次：有四个端点要过滤影子名单（最新候选、历史成绩、
    净值曲线、校准曲线），散着写就一定会漏 —— 而漏掉的那个会静默展示
    **引擎已不再产出**的候选，比不显示更糟。

    两条过滤（都是**可买性条件**，可对历史回溯施加）：
      ① `NOT is_sealed` —— 信号日已封板的票收盘买不进（占历史候选 15.3%），
         其"收益"是幻影。这是 ex-post 过滤：封板与否在信号日收盘即已知，
         对历史施加它**不引入未来信息**，也不是参数拟合。
      ② 流动性下限。

    ⚠️ 为什么不按 `params`（参数指纹）过滤：历史 9415 行是原始参数时期写入的，
    按指纹过滤等于**每改一次参数就把历史成绩清零**（实测导致净值曲线与
    历史成绩直接消失）。指纹的用途是审计与分段归因，不是展示过滤。
    """
    from astock.shadow import shadow_min_amount_avg20

    sql = "(is_sealed IS NULL OR is_sealed = FALSE)"
    params: list[Any] = []
    amt = shadow_min_amount_avg20()
    if amt > 0:
        # amount_ma20 为 NULL 的行放行：无法判断时宁可展示也不静默隐藏。
        # 实际上不会有 NULL —— 列迁移时已对历史行做过一次性回填。
        sql += " AND (amount_ma20 IS NULL OR amount_ma20 >= ?)"
        params.append(amt)
    return sql, params


def _shadow_status(code: str, pct: float | None) -> str:
    """按快照涨幅判定可执行性（与每日报告使用**同一套**分类口径）。"""
    if pct is None:
        return "无快照"
    cap = _ZT_CAP.get(str(code)[:2], 9.7)
    if pct >= cap:
        return "已涨停·放弃"
    if pct >= 7.0:
        return "涨幅过大·回避"
    return "可买"


@app.get("/api/shadow/latest")
def shadow_latest(offset: int = Query(0, ge=0, le=60)) -> dict[str, Any]:
    """最新信号日的影子候选；若计划交易日已有盘中快照，附带"此刻能不能买"的实况。"""
    _need_snapshot()
    reader = get_reader()
    dates = reader.records(
        "SELECT DISTINCT CAST(date AS VARCHAR) AS date FROM shadow ORDER BY date DESC LIMIT ?",
        "shadow",
        [offset + 1],
    )
    if len(dates) <= offset:
        return {"available": False, "records": [], "hint": "尚无影子信号：需先跑 daily（含 shadow build）"}

    sig_date = str(dates[offset]["date"])[:10]
    vol_max = 1.0 - _shadow_min_score() / 100.0
    where, where_params = _shadow_filters()
    rows = reader.records(
        "SELECT code, name, close AS sig_close, zt20, days_since_zt, vol_ratio, "
        f"signal_score, amount_ma20 FROM shadow WHERE CAST(date AS VARCHAR) = ? AND {where} "
        "ORDER BY signal_score DESC",
        "shadow",
        [sig_date, *where_params],
    )
    # 被规则挡掉的数量也要说出来：否则用户看到"36 只变 5 只"会以为数据丢了。
    # 用"当日总数 − 通过数"计算，不重写一遍准入条件（重写就会与上面漂移）。
    total_on_date = int(reader.scalar(
        "SELECT COUNT(*) FROM shadow WHERE CAST(date AS VARCHAR) = ?",
        "shadow", [sig_date], default=0) or 0)
    dropped = max(0, total_on_date - len(rows))

    # 盘中实况：**只有快照日期晚于信号日**才关联。
    # 若不过滤，会把几天前那份快照的价格当成"现价"显示 —— 那是主动误导，
    # 比不给实况更糟（本项目的核心教训之一就是"口径不对等于没有数据"）。
    snap_date, slots = None, []
    snap = reader.records(
        "SELECT DISTINCT CAST(date AS VARCHAR) AS date, slot FROM intraday "
        "ORDER BY date DESC, slot DESC LIMIT 6",
        "intraday",
    )
    fresh = [s for s in snap if str(s["date"])[:10] > sig_date]
    if fresh:
        snap_date = str(fresh[0]["date"])[:10]
        slots = sorted({s["slot"] for s in fresh if str(s["date"])[:10] == snap_date}, reverse=True)
    live: dict[str, dict] = {}
    if snap_date and slots:
        for r in reader.records(
            "SELECT code, price, pct_chg, volume_ratio, CAST(captured_at AS VARCHAR) AS captured_at "
            "FROM intraday WHERE CAST(date AS VARCHAR) = ? AND slot = ?",
            "intraday",
            [snap_date, slots[0]],
        ):
            live[r["code"]] = r

    for r in rows:
        q = live.get(r["code"])
        r["snap_price"] = q.get("price") if q else None
        r["snap_pct"] = q.get("pct_chg") if q else None
        r["snap_volume_ratio"] = q.get("volume_ratio") if q else None
        r["status"] = _shadow_status(r["code"], r["snap_pct"] if q else None)

    order = {"可买": 0, "涨幅过大·回避": 1, "已涨停·放弃": 2, "无快照": 3}
    rows.sort(key=lambda r: (order.get(r["status"], 9), -(r.get("signal_score") or 0)))
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {
        "available": True,
        "signal_date": sig_date,
        "snapshot_date": snap_date,
        "snapshot_slot": slots[0] if slots else None,
        "count": len(rows),
        "status_counts": counts,
        "vol_ratio_max": round(vol_max, 2),
        "min_amount_avg20": round(_shadow_min_amt(), 0),
        "filtered_out": dropped,
        "total_on_date": total_on_date,
        "records": rows,
        "rules": [
            f"缩量门槛：量 < 前 5 日均量 × {vol_max:g}（抛压枯竭）",
            "**不买信号日已封板的票**（封板买不进；实测封板股次日高开低走）",
            (f"前 20 日均成交额 ≥ {_shadow_min_amt() / 1e8:g} 亿（买不进的票不算收益）"
             if _shadow_min_amt() > 0 else "未设流动性下限"),
            "执行口径：**次日开盘买入 → 再次日收盘卖出**（最早合法卖点，T+1）",
            "等权分散；收益来自约 1/4 命中涨停的尾部，靠分散才成为正期望",
        ],
    }


@app.get("/api/shadow/history")
def shadow_history(days: int = Query(60, ge=2, le=400)) -> dict[str, Any]:
    """按信号日聚合的影子成绩：笔数、次日收益、超额、命中涨停、结算情况。"""
    _need_snapshot()
    reader = get_reader()
    # INTERVAL 不接受绑定参数（实测报错），days 已由 FastAPI 限定为 2~400 的整数，可安全内联。
    # signal_score 过滤：成绩必须按**当前生效规则**统计。历史表里保留着阈值收紧前
    # 写入的低分候选（信号分是逐行属性，不会被改写），若不过滤，
    # 页面上显示的"日均超额/t"就是一条早已不再执行的规则的旧成绩。
    where, where_params = _shadow_filters()
    rows = reader.records(
        f"""
        SELECT CAST(date AS VARCHAR) AS date,
               COUNT(*) AS n,
               COUNT(exec_d1) AS settled,
               ROUND(AVG(exec_d1), 3) AS avg_ret1,
               ROUND(AVG(exec_d3), 3) AS avg_ret3,
               ROUND(AVG(exec_d1 - exec_bench), 3) AS avg_excess,
               SUM(CASE WHEN hit_limit_up THEN 1 ELSE 0 END) AS hit_zt
        FROM shadow
        WHERE {where} AND date >= (SELECT MAX(date) FROM shadow) - INTERVAL {int(days)} DAY
        GROUP BY date ORDER BY date DESC
        """,
        "shadow",
        where_params,
    )
    settled = [r for r in rows if r.get("settled")]
    if settled:
        import statistics

        ex = [r["avg_excess"] for r in settled if r.get("avg_excess") is not None]
        mean = statistics.fmean(ex) if ex else None
        t = None
        if len(ex) > 2:
            sd = statistics.stdev(ex)
            t = round(mean / (sd / len(ex) ** 0.5), 2) if sd else None
        agg = {
            "days": len(settled),
            "mean_excess": round(mean, 3) if mean is not None else None,
            "t": t,
            "hit_zt": sum(int(r.get("hit_zt") or 0) for r in settled),
        }
    else:
        agg = {"days": 0, "mean_excess": None, "t": None, "hit_zt": 0}
    return {"records": rows, "aggregate": agg}


@app.get("/api/shadow/review")
def shadow_review(days: int = Query(60, ge=1, le=400)) -> dict[str, Any]:
    """影子复盘（归因）：最近若干次复盘记录。

    一行 = 一次复盘（按复盘日主键）：**可实现口径**的聚合统计 + LLM 归因文字。
    旧口径 `avg_ret1_old` 一并返回，用途是让页面上能看见"买不进的幻影收益"有多大。
    """
    _need_snapshot()
    rows = get_reader().records(
        "SELECT * FROM shadow_review ORDER BY review_date DESC LIMIT ?",
        "shadow_review",
        [days],
    )
    if not rows:
        return {"available": False, "records": [],
                "hint": "尚无复盘：daily 会自动执行影子复盘（ShadowReviewer.run）"}
    latest = dict(rows[0])
    if latest.get("detail"):
        try:
            latest["detail"] = json.loads(latest["detail"])
        except (TypeError, ValueError):
            pass
    return {"available": True, "latest": latest, "records": rows}


# ---------------- 曲线（净值 / 校准）----------------
@app.get("/api/equity")
def equity(include_benchmark: bool = Query(True)) -> dict[str, Any]:
    """两条净值曲线（影子信号 + 同池等权基准）+ 绩效/显著性指标。

    **全部从 Parquet 快照计算**（不碰主库），且与报告复用同一套口径与统计
    （`astock.backtest.charts`），否则"页面上的结论"和"报告里的结论"会不一致。
    口径一律是**可实现**的：信号日次日开盘买 → 再次日收盘卖，双边扣 0.3%。
    主链路曲线已随主链路删除（2026-10-08）。
    """
    _need_snapshot()
    from astock.backtest import charts

    reader = get_reader()
    curves: list[charts.Curve] = []
    run_id = None

    # 主链路曲线（ads_backtest）已随主链路删除。现在只有两个对象：
    # 影子信号（唯一候选来源）与同池等权基准（诚实参照）。

    # 影子曲线同样按当前阈值过滤：否则图上画的是旧规则的净值，
    # 与页面顶部的"信号分 ≥ N"说明自相矛盾。
    where, where_params = _shadow_filters()
    df = reader.query(
        "SELECT CAST(date AS VARCHAR) AS d, AVG(exec_d1) AS r FROM shadow "
        f"WHERE exec_d1 IS NOT NULL AND {where} GROUP BY date ORDER BY date",
        "shadow", where_params)
    if not df.empty:
        s = pd.Series((df["r"] / 100.0 - charts.COST_ROUND_TRIP).to_numpy(),
                      index=df["d"].astype(str).tolist())
        curves.append(charts.curve_from_series(
            s, "影子信号", charts.C_SHADOW,
            "信号日次日开盘买→再次日收盘卖（可实现口径），扣0.3%"))

    if include_benchmark:
        # 基准 = 同池等权、与主链路同口径（用 stock_bars + dim_stock 两张快照算）。
        # ⚠️ 依赖 stock_bars 的快照天数（默认 250 个交易日）：若将来影子历史长于它，
        # 基准会先把窗口截短，导致「页面窗口 < 报告窗口」。届时把
        # `serving.export(bars_days=...)` 调大即可（当前 250 > 影子 239，安全）。
        bdf = reader.query_tables(
            """
            WITH pool AS (SELECT code, out_date, ipo_date, board, name FROM dim_stock),
            fwd AS (
                SELECT b.date,
                       (LEAD(b.close, 2) OVER (PARTITION BY b.code ORDER BY b.date)
                        / NULLIF(LEAD(b.open, 1) OVER (PARTITION BY b.code ORDER BY b.date), 0)
                        - 1) * 100 AS r
                FROM stock_bars b JOIN pool p ON p.code = b.code
                WHERE p.board IN ('main', 'gem') AND p.name NOT LIKE '%ST%'
                  AND (p.out_date IS NULL OR p.out_date > b.date)
                  AND p.ipo_date <= b.date - INTERVAL 120 DAY
            )
            SELECT CAST(date AS VARCHAR) AS d, AVG(r) AS r
            FROM fwd WHERE r IS NOT NULL AND r > -50 AND r < 50
            GROUP BY date ORDER BY date
            """,
            ("stock_bars", "dim_stock"),
        )
        if not bdf.empty:
            s = pd.Series((bdf["r"] / 100.0 - charts.COST_ROUND_TRIP).to_numpy(),
                          index=bdf["d"].astype(str).tolist())
            curves.append(charts.curve_from_series(
                s, "基准（同池等权）", charts.C_BENCH, "同一可交易池等权，同口径，扣0.3%",
                dashed=True))

    curves = list(charts.attach_excess(curves))
    if not curves:
        return {"available": False, "dates": [], "curves": []}
    dates = curves[0].dates
    return {
        "available": True,
        "run_id": run_id,
        "dates": dates,
        "span": [dates[0], dates[-1]] if dates else [None, None],
        "judgement": "判据看「日超额 / t」，不要只看净值高低：净值受区间选择与波动拖累影响。",
        # 影子曲线的阈值是在**同一段数据**上选出来的（2026-09-30 修订），
        # 全区间数字因此含样本内选择偏差。不标注的话，页面上会出现
        # "+3737%" 这种必然诱发过度自信的读数 —— 本项目的核心纪律就是
        # "宁可说清局限，也不给一个看起来很好的数"。
        "caveat": (
            "⚠️ 影子曲线使用了在**同期数据**上选定的信号分阈值（在样本内），"
            "全区间数字含选择偏差、不可直接外推。可信口径是验证期检验结果："
            "（2026-04-03 起 122 个交易日）日均超额 +1.258%、t=3.88。"
            "实际表现以 v1.0 上线后（2026-09-30 起）的纸面跟踪为准。"
        ),
        "curves": [
            {
                "name": c.name,
                "color": c.color,
                "dashed": c.dashed,
                "note": c.note,
                "nav": [round(v, 5) for v in c.nav],
                "stats": {k: (round(v, 6) if isinstance(v, float) else v)
                          for k, v in c.stats().items()},
                "excess": {k: round(v, 6) for k, v in (c.excess or {}).items()},
            }
            for c in curves
        ],
    }


@app.get("/api/calibration")
def calibration(bins: int = Query(10, ge=3, le=20)) -> dict[str, Any]:
    """校准曲线：评分分档 → 该档实际超额收益。回答"高分是否真的涨得多"。"""
    _need_snapshot()
    reader = get_reader()

    def calc(df: pd.DataFrame, label: str) -> dict[str, Any]:
        if df is None or df.empty or df["score"].notna().sum() < bins * 5:
            return {"title": label, "labels": [], "excess": [], "n": []}
        d = df.dropna(subset=["score", "ret"]).copy()
        try:
            d["bin"] = pd.qcut(d["score"], bins, labels=False, duplicates="drop")
        except ValueError:
            return {"title": label, "labels": [], "excess": [], "n": []}
        g = d.groupby("bin").agg(n=("ret", "size"), ret=("ret", "mean"))
        g["excess"] = g["ret"] - d["ret"].mean()
        return {
            "title": label,
            "labels": [f"Q{i + 1}" for i in range(len(g))],
            "excess": [round(float(v), 4) for v in g["excess"]],
            "n": [int(v) for v in g["n"]],
        }

    # 主链路分组（backtest_scores.final_score）已随主链路删除，此处只剩影子。
    where, where_params = _shadow_filters()
    shadow = reader.query(
        f"SELECT signal_score AS score, exec_d1 AS ret FROM shadow "
        f"WHERE exec_d1 IS NOT NULL AND {where}",
        "shadow", where_params)
    return {
        "groups": [
            calc(shadow, "影子信号 · signal_score（缩量程度）分位"),
        ],
        "hint": "纵轴为该分位相对全样本均值的超额收益（**可实现口径**）。"
                "若 signal_score 有排序能力，柱形应自左向右单调上升。"
                "⚠️ 它只是缩量程度的展示排序，**不是胜率排序** —— "
                "请勿据此挑前几名重仓（等权分散才是它的成立前提）。",
    }


# ---------------- 个股 ----------------
@app.get("/api/stock/{code}")
def stock_detail(code: str, days: int = Query(180, ge=20, le=1000)) -> dict[str, Any]:
    _need_snapshot()
    reader = get_reader()
    info = reader.records("SELECT * FROM dim_stock WHERE code = ?", "dim_stock", [code])
    bars = reader.records(
        "SELECT * FROM stock_bars WHERE code = ? ORDER BY date DESC LIMIT ?",
        "stock_bars",
        [code, days],
    )
    if not bars:
        raise HTTPException(404, f"快照中未找到 {code} 的行情数据（可能超出快照保留区间）")

    feature = reader.records(
        "SELECT * FROM feature_latest WHERE code = ?", "feature_latest", [code]
    )
    recs = reader.records(
        "SELECT rec_date, tier, strategy, final_score, reasons FROM recommend "
        "WHERE code = ? ORDER BY rec_date DESC LIMIT 10",
        "recommend",
        [code],
    )
    return {
        "code": code,
        "info": info[:1],
        "bars": list(reversed(bars)),
        "feature": feature[:1],
        "recommendations": recs,
    }


# ---------------- 板块 ----------------
@app.get("/api/sector/top")
def sector_top(
    n: int = Query(15, ge=1, le=100),
    source: str = Query("", description="分类体系 sw/sina，留空取主用来源"),
    days: int = Query(1, ge=1, le=60),
) -> dict[str, Any]:
    """板块强度排行。

    板块强度由本地日线自算（不依赖板块接口），因此**可回填历史**。
    """
    _need_snapshot()
    reader = get_reader()
    src = source.strip().lower()
    if not src:
        # 必须默认主用来源：申万与新浪是两套分类体系，混在一起排名会失去可比性
        # （19 只股票的新浪小板块与 479 只的申万大行业同台竞争）。
        src = str(get_config().get("sector.primary_source", "sw") or "sw")

    params: list[Any] = []
    where = ""
    if src:
        where = " AND source = ?"
        params.append(src)

    # 先定位最新可用日期（板块数据可能滞后于行情一天）
    latest = reader.records(
        f"SELECT MAX(date) AS d FROM sector_strength WHERE 1=1{where}", "sector_strength", params
    )
    if not latest or latest[0].get("d") is None:
        return {"available": False, "records": [], "sources": []}

    target = latest[0]["d"]
    rows = reader.records(
        "SELECT * FROM sector_strength WHERE date = ?"
        + (" AND source = ?" if src else "")
        + " ORDER BY strength_score DESC LIMIT ?",
        "sector_strength",
        ([target] + ([src] if src else []) + [int(n)]),
    )

    sources = reader.records(
        "SELECT DISTINCT source FROM sector_strength ORDER BY source", "sector_strength"
    )
    return {
        "available": True,
        "date": target,
        "source": src,
        "sources": [s["source"] for s in sources],
        "records": rows[:n],
    }


# ---------------- 策略 / Skill 库：已于 2026-10-08 随主链路删除 ----------------
# /api/strategies（各策略命中数）与 /api/skills（策略档案）描述的都是那 8 个策略，
# 它们在新版中已不存在，故三个端点全部移除。


@app.get("/api/llm/usage")
def llm_usage() -> dict[str, Any]:
    llm_cfg = cfg.section("llm")
    history = []
    if get_reader().available():
        history = get_reader().records(
            "SELECT * FROM llm_usage ORDER BY date DESC LIMIT 30", "llm_usage"
        )
    today_used = 0
    today = str(pd.Timestamp.today().date())
    for row in history:
        if str(row.get("date")) == today:
            today_used = int(row.get("prompt_tokens") or 0) + int(row.get("completion_tokens") or 0)
            break
    return {
        "enabled": bool(llm_cfg.get("enabled", False)),
        "model": llm_cfg.get("model"),
        "daily_budget": int(llm_cfg.get("daily_token_budget", 0) or 0),
        "today_used": today_used,
        "history": history,
    }
