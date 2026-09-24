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

from astock.config import get_config
from astock.logger import get_logger, setup_logging
from astock.recommend.engine import params_version
from astock.review.reviewer import Reviewer
from astock.storage.serving import get_reader, serving_dir
from astock.strategy import build_strategies

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
    """前端需要的配置（不含任何密钥）。"""
    strategies = build_strategies(cfg)
    return {
        "project": cfg.get("project.name"),
        "tier_top_n": cfg.get("scoring.tier_top_n"),
        "new_stock_days": cfg.get("universe.new_stock_days"),
        "strategies": {
            tier: [{"name": s.name, "label": s.label} for s in items]
            for tier, items in strategies.items()
        },
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


# ---------------- 复盘 ----------------
@app.get("/api/review/summary")
def review_summary(days: int = Query(60, ge=1, le=1000)) -> dict[str, Any]:
    _need_snapshot()
    df = get_reader().query(
        "SELECT * FROM review WHERE next_date >= (SELECT MAX(next_date) FROM review) "
        f"- INTERVAL {int(days)} DAY",
        "review",
    )
    return Reviewer.summary_from_frame(df)


@app.get("/api/review/history")
def review_history(days: int = Query(60, ge=1, le=1000)) -> dict[str, Any]:
    _need_snapshot()
    return {
        "records": get_reader().records(
            "SELECT * FROM review ORDER BY next_date DESC LIMIT ?", "review", [days]
        )
    }


@app.post("/api/review/run")
def review_run() -> dict[str, Any]:
    """手动触发复盘回填。

    **必须在子进程里执行，绝不在本服务进程内打开主库。**
    本服务是长驻进程，一旦持有 DuckDB 连接就会把主库锁到进程退出为止，
    直接后果是：用户只要开着网页，`daily` / `backfill` / `backtest` 全都会因
    「文件被占用」而失败。子进程跑完即释放锁，本服务继续只读 Parquet 快照。
    """
    import subprocess
    import sys

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "astock.cli", "review"],
            cwd=str(cfg.project_root),
            capture_output=True,
            text=True,
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "复盘执行超时（超过 15 分钟），请改用命令行查看进度。") from None

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise HTTPException(500, f"复盘执行失败：{detail[-600:] or '未知错误'}")

    return {
        "ok": True,
        "message": "复盘完成，展示快照已更新，可刷新页面查看。",
        "output": (proc.stdout or "").strip()[-2000:],
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


# ---------------- 策略 ----------------
@app.get("/api/strategies")
def strategies() -> dict[str, Any]:
    _need_snapshot()
    reader = get_reader()
    rows = reader.records(
        "SELECT * FROM strategy_stats ORDER BY data_date DESC, tier, strategy", "strategy_stats"
    )
    if not rows:
        return {"available": False, "tiers": {}, "data_date": None, "pool_size": 0}

    latest = max(r["data_date"] for r in rows)
    rows = [r for r in rows if r["data_date"] == latest]

    tiers: dict[str, list[dict[str, Any]]] = {"short": [], "swing": [], "value": []}
    for r in rows:
        tiers.setdefault(str(r["tier"]), []).append(
            {"name": r["strategy"], "label": r["label"], "hits": r["hits"]}
        )

    pool_size = get_reader().scalar("SELECT COUNT(*) FROM feature_latest", "feature_latest", default=0)
    return {
        "available": True,
        "data_date": latest,
        "pool_size": int(pool_size or 0),
        "tiers": tiers,
    }


# ---------------- Skill 库 ----------------
@app.get("/api/skills")
def skills_index() -> dict[str, Any]:
    """策略 Skill 库索引。

    直接读 `skills/registry.json`（文件），不查库 —— 与展示层快照同一思路，
    因此采集/回测期间也能正常访问。
    """
    from astock.skills.store import SkillsStore

    store = SkillsStore()
    registry_path = store.root / "registry.json"
    if not registry_path.exists():
        return {"available": False, "skills": [], "hint": "运行 python -m astock.cli skills sync 生成"}

    data = json.loads(registry_path.read_text(encoding="utf-8"))
    return {"available": True, **data}


@app.get("/api/skills/{name}")
def skill_detail(name: str) -> dict[str, Any]:
    from astock.skills.store import SkillsStore

    skill = SkillsStore().get_skill(name)
    if skill is None:
        raise HTTPException(404, f"未找到策略：{name}")
    return {
        "name": name,
        "skill_md": skill.get("skill_md", ""),
        "meta": skill.get("meta", {}),
        "performance": skill.get("performance", {}),
        "references": list((skill.get("references") or {}).keys()),
    }


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
