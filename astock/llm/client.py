# -*- coding: utf-8 -*-
"""DeepSeek 客户端（OpenAI 兼容协议）+ Token 预算护栏。

成本控制措施（对应方案第 8 章）：
1. 只对当日 Top-N 候选调用，全市场扫描全在本地完成；
2. 输入只传「已算好的指标」，不传原始 K 线；
3. 结构化短输出（caveman 风格），输出 token 大幅下降；
4. 每日 token 预算硬上限，超限自动降级为本地模板报告；
5. 调用量落库 sys_llm_usage，可随时审计成本。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from astock.config import get_config
from astock.logger import get_logger
from astock.storage.db import Storage, get_storage

logger = get_logger("llm.client")


class LLMClient:
    """DeepSeek 客户端封装。"""

    def __init__(self, cfg=None, storage: Storage | None = None) -> None:
        self.cfg = cfg or get_config()
        self.storage = storage or get_storage()
        llm = self.cfg.section("llm")
        self.model = self.cfg.env("DEEPSEEK_MODEL") or llm.get("model", "deepseek-chat")
        self.base_url = self.cfg.env("DEEPSEEK_BASE_URL") or llm.get("base_url", "https://api.deepseek.com")
        self.temperature = float(llm.get("temperature", 0.3))
        self.max_tokens = int(llm.get("max_tokens", 1200))
        self.budget = int(
            self.cfg.env("LLM_DAILY_TOKEN_BUDGET") or llm.get("daily_token_budget", 200000)
        )
        self.api_key = self.cfg.env("DEEPSEEK_API_KEY")
        self._enabled = bool(llm.get("enabled", False)) or self.cfg.env_bool("LLM_ENABLED", False)
        self._client = None

    # ---------------- 可用性 ----------------
    @property
    def enabled(self) -> bool:
        if not self._enabled:
            return False
        if not self.api_key:
            logger.info("未配置 DEEPSEEK_API_KEY，LLM 功能降级为本地模板")
            return False
        return True

    def _client_or_none(self):
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI

            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=60)
            return self._client
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM 客户端初始化失败: %s", exc)
            return None

    # ---------------- 预算 ----------------
    def today_usage(self) -> int:
        row = self.storage.query_one(
            "SELECT prompt_tokens, completion_tokens FROM sys_llm_usage WHERE date = ?",
            [date.today()],
        )
        if not row:
            return 0
        return int((row[0] or 0) + (row[1] or 0))

    def budget_ok(self) -> bool:
        used = self.today_usage()
        if used >= self.budget:
            logger.warning("LLM 今日 token 已用 %d / %d，降级为本地模板", used, self.budget)
            return False
        return True

    def _record_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        row = self.storage.query_one(
            "SELECT calls, prompt_tokens, completion_tokens FROM sys_llm_usage WHERE date = ?",
            [date.today()],
        )
        calls = int(row[0] or 0) + 1 if row else 1
        p = int(row[1] or 0) + prompt_tokens if row else prompt_tokens
        c = int(row[2] or 0) + completion_tokens if row else completion_tokens
        self.storage.execute(
            "INSERT OR REPLACE INTO sys_llm_usage VALUES (?, ?, ?, ?, ?)",
            [date.today(), calls, p, c, datetime.now()],
        )

    # ---------------- 调用 ----------------
    def chat_json(self, system: str, user: str) -> dict[str, Any] | None:
        """返回解析后的 JSON 对象；任何失败都返回 None 由调用方降级。"""
        if not self.enabled or not self.budget_ok():
            return None
        client = self._client_or_none()
        if client is None:
            return None

        try:
            resp = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                response_format={"type": "json_object"},
            )
            usage = getattr(resp, "usage", None)
            if usage is not None:
                self._record_usage(
                    int(getattr(usage, "prompt_tokens", 0) or 0),
                    int(getattr(usage, "completion_tokens", 0) or 0),
                )
            content = resp.choices[0].message.content or ""
            return json.loads(content)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM 调用失败，降级为本地模板: %s", exc)
            return None

    def usage_report(self) -> dict[str, Any]:
        df = self.storage.query_df(
            "SELECT * FROM sys_llm_usage ORDER BY date DESC LIMIT 30"
        )
        return {
            "enabled": self.enabled,
            "model": self.model,
            "daily_budget": self.budget,
            "today_used": self.today_usage(),
            "history": [] if df.empty else df.to_dict(orient="records"),
        }
