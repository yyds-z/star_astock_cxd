# -*- coding: utf-8 -*-
"""配置模块：加载 config/settings.yaml，并支持 .env 环境变量覆盖。

设计要点：
- 通过 `cfg.get("strategy.swing.rps_breakout.enabled", True)` 点号路径取值，缺失时返回默认值。
- 路径统一解析为绝对路径，避免因工作目录不同导致数据写错位置。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "settings.yaml"

# 指纹只覆盖「会影响信号本身」的配置。
# 2026-10-08：主链路（strategy/scoring）删除后，含义从「策略/评分参数」变为
# **影子信号参数** —— 它仍是 G3 验收时区分「修订前/修订后」样本的唯一依据
# （例如 2026-09-30 把信号分下限从 30 改到 50，必须能分段统计）。
PARAM_KEYS = ("shadow_gene", "universe", "market_regime")


def params_version() -> str:
    """当前生效参数的指纹，逐条落库，用于追踪「哪套参数产生了哪条记录」。"""
    cfg = get_config()
    payload = {k: cfg.get(k, {}) for k in PARAM_KEYS}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:10]


def report_dir() -> Path:
    """每日报告的落盘目录（自动创建）。"""
    p = get_config().data_dir / "reports"
    p.mkdir(parents=True, exist_ok=True)
    return p


class Config:
    """点号路径访问的配置容器。"""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data or {}

    # ---------- 读取 ----------
    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, path: str) -> dict[str, Any]:
        value = self.get(path, {})
        return value if isinstance(value, dict) else {}

    def raw(self) -> dict[str, Any]:
        return self._data

    # ---------- 路径 ----------
    @property
    def project_root(self) -> Path:
        return PROJECT_ROOT

    @property
    def data_dir(self) -> Path:
        raw = os.environ.get("ASTOCK_DATA_DIR") or self.get("project.data_dir", "data")
        p = Path(raw)
        return p if p.is_absolute() else (PROJECT_ROOT / p)

    @property
    def duckdb_path(self) -> Path:
        p = Path(self.get("storage.duckdb_path", "data/astock.duckdb"))
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def parquet_dir(self) -> Path:
        p = Path(self.get("storage.parquet_dir", "data/parquet"))
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def log_dir(self) -> Path:
        p = self.data_dir / "logs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ---------- 环境变量 ----------
    def env(self, key: str, default: str | None = None) -> str | None:
        return os.environ.get(key, default)

    def env_bool(self, key: str, default: bool = False) -> bool:
        raw = os.environ.get(key)
        if raw is None:
            return default
        return raw.strip().lower() in ("1", "true", "yes", "on")


_config: Config | None = None


def load_config(path: str | Path | None = None, reload: bool = False) -> Config:
    """加载配置（单例）。"""
    global _config
    if _config is not None and not reload:
        return _config

    load_dotenv(PROJECT_ROOT / ".env", override=False)

    cfg_path = Path(path) if path else DEFAULT_CONFIG
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    else:
        data = {}

    _config = Config(data)
    _apply_network_policy(_config)
    return _config


def _apply_network_policy(cfg: Config) -> None:
    """数据源直连策略。

    本机若启用了系统代理（Windows 注册表 ProxyEnable=1），
    `requests` 会自动走该代理，导致 akshare/adata 请求被拦截而失败；
    baostock 使用原始 socket，不受影响。国内行情数据源直连更稳定，
    因此在配置开启时统一绕过代理。
    """
    if not cfg.get("data.bypass_proxy", True):
        return
    # 直接赋值（而非 setdefault）：配置已明确要求绕过代理，不应被已有值覆盖
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    # akshare 内部会输出进度条，批量采集时会把日志刷满，这里关闭
    os.environ.setdefault("TQDM_DISABLE", "1")


def get_config() -> Config:
    return load_config()
