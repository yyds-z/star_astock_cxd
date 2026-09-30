# -*- coding: utf-8 -*-
"""数据源适配器集合：多源融合 + 兜底切换。"""

from astock.data.sources.base import (
    BaseSource,
    board_of,
    clean_daily,
    exchange_of,
    is_st_name,
    normalize_code,
    to_source_code,
)

__all__ = [
    "BaseSource",
    "normalize_code",
    "exchange_of",
    "board_of",
    "is_st_name",
    "to_source_code",
    "clean_daily",
    "get_source",
    "available_sources",
    "SOURCE_NAMES",
]

# 支持的数据源（顺序即建议优先级：主源在前、兜底在后）
SOURCE_NAMES: tuple[str, ...] = ("baostock", "akshare", "adata")


def available_sources() -> list[str]:
    """返回已支持的数据源名列表。

    供诊断脚本遍历探测，避免诊断脚本自己硬编码源名 ——
    硬编码是造成「诊断结论与实际行为脱节」的根源之一。
    """
    return list(SOURCE_NAMES)


def get_source(name: str = "baostock") -> BaseSource:
    """按名称获取数据源实例（延迟导入，避免不必要的依赖加载）。"""
    key = (name or "baostock").lower()
    if key == "baostock":
        from astock.data.sources.baostock_source import BaostockSource

        return BaostockSource()
    if key == "akshare":
        from astock.data.sources.akshare_source import AkshareSource

        return AkshareSource()
    if key == "adata":
        from astock.data.sources.adata_source import AdataSource

        return AdataSource()
    raise ValueError(f"未知数据源: {name}")
