# -*- coding: utf-8 -*-
"""AI 层：LLM 客户端与 Prompt 模板。"""

from astock.llm.client import LLMClient
from astock.llm.prompts import (
    SYSTEM_PROMPT,
    build_market_prompt,
    template_logic,
    template_market_view,
    template_risk,
)

__all__ = [
    "LLMClient",
    "SYSTEM_PROMPT",
    "build_market_prompt",
    "template_market_view",
    "template_logic",
    "template_risk",
]
