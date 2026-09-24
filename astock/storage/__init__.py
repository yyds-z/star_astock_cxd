# -*- coding: utf-8 -*-
"""存储层：DuckDB 热数据 + Parquet 冷归档 + 展示层快照。

- `db.Storage`：计算层，独占访问 DuckDB 主库（写入 + 分析）
- `serving.ServingReader`：展示层，只读 Parquet 快照，与计算层互不阻塞
"""

from astock.storage.db import Storage, get_storage, try_get_storage
from astock.storage.serving import ServingReader, export as export_serving, get_reader

__all__ = [
    "Storage",
    "get_storage",
    "try_get_storage",
    "ServingReader",
    "get_reader",
    "export_serving",
]
