# -*- coding: utf-8 -*-
"""涨停板（按行业分组 + 连板梯队）的口径 —— **报告与网页的唯一来源**。

为什么单独成模块：同一份"涨停板"要同时出现在两个地方 ——
  ① 每日报告的「四、涨停板」板块（`astock/report/builder.py`，读主库）
  ② 网页（`api/server.py` 的 `/api/limitup`，读 Parquet 快照）
两处若各写一份分组规则，必然出现"报告里的行业和页面上的不一样"。这已经是本项目
最贵的失效模式（同一口径两个来源），2026-10-08 刚因为同类问题修过一批展示不一致。

因此这里**只放纯函数**：输入明细 DataFrame，输出分组结构；不读数据库、不读配置。
（展示层绝不能打开主库 —— DuckDB 是独占文件锁，API 一旦持有连接，18:30 的 daily
就会直接失败，这个坑本项目实测踩过。）

2026-10-08 上线当天的口径对照验证抓到两处，都已在本文件内解决：
  · 行业列在主库是 NULL（None），经 Parquet 读出会变成 float('nan')，
    `str()` 之后成了叫「nan」的行业 → 统一走 `_s()`；
  · 报告与页面的 ORDER BY 不同（一个 `first_time, code`，另一个只有 `first_time`），
    同封板时间的先后就分叉 → **顺序由本函数自己决定**，不依赖调用方排序。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

#: 没有行业归属时的分组名（申万与新浪两套映射都没有该代码）
UNCLASSIFIED = "未分类"

#: 家数为 1 的行业 + 无归属的，统一并入这个标签
OTHER_LABEL = "其他"


def _s(value: Any) -> str:
    """安全转字符串：None / NaN / 数字都能处理，避免 `str(nan)` 变成 `'nan'`。"""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _boards(row: Any) -> int:
    """连板数：缺失或非法时按 1（首板）处理。"""
    try:
        value = row.get("boards")
        return 1 if value is None or pd.isna(value) else int(value)
    except (TypeError, ValueError):
        return 1


def board_badge(row: Any) -> str:
    """连板徽标：优先上游原文（`首板` / `3连板` / `3天2板`），缺失时按 boards 拼。"""
    txt = _s(row.get("board_text"))
    if txt:
        return txt
    boards = _boards(row)
    return "首板" if boards <= 1 else f"{boards}连板"


def theme_tag(row: Any) -> str:
    """涨停题材只取第一段：上游原文形如 `固态电池+钠离子电池+AI PC键盘`。"""
    txt = _s(row.get("reason"))
    return txt.split("+")[0].strip() if txt else ""


def _item(row: Any) -> dict[str, Any]:
    boards = _boards(row)
    return {
        "code": _s(row.get("code")),
        "name": _s(row.get("name")),
        "first_time": _s(row.get("first_time")),
        "badge": board_badge(row),
        "boards": boards,
        "theme": theme_tag(row),
        "industry": _s(row.get("industry")) or UNCLASSIFIED,
        "is_multi_board": boards > 1,
    }


def group_limit_up(rows: pd.DataFrame) -> dict[str, Any]:
    """把涨停明细按行业分组，并抽出**连板梯队**。

    入参需含列：code / name / first_time / boards / board_text / reason / industry
    （行业列缺失、为 NULL 或 NaN 时一律按「未分类」处理）。

    返回：
      total         涨停总数
      multi_board   连板 ≥2 的只数
      ladder        连板梯队：连板降序 → 同板位按封板时间、代码升序
      groups        行业分组（家数 ≥2 且非「未分类」），家数降序 → 行业名升序
      other         家数为 1 的行业 + 无归属，合并为「其他」（无则为 None）
    """
    if rows is None or len(rows) == 0:
        return {"total": 0, "multi_board": 0, "ladder": [], "groups": [], "other": None}

    items = [_item(r) for _, r in rows.iterrows()]
    # **顺序在这里定型**，不依赖调用方的 SQL ORDER BY：报告读主库、页面读 Parquet，
    # 两条 SQL 的排序一旦不同（实测就是如此），同封板时间的先后就会分叉，
    # 于是"同一份数据两个顺序"。以 (封板时间, 代码) 为准，两处必然一致。
    items.sort(key=lambda x: (x["first_time"], x["code"]))

    # 连板梯队：高标是这个板块最该被一眼看到的信息；同板位早封板的更强
    ladder = sorted(
        (x for x in items if x["is_multi_board"]),
        key=lambda x: (-x["boards"], x["first_time"], x["code"]),
    )

    by_industry: dict[str, list[dict[str, Any]]] = {}
    for x in items:
        by_industry.setdefault(x["industry"], []).append(x)

    # 家数为 1 的行业、以及完全没有行业归属的，一并进「其他」：
    # 十几个单只行业各占一行会把有效信息淹掉（与行情软件的呈现一致）。
    groups = [
        {"industry": ind, "count": len(v), "items": v}
        for ind, v in by_industry.items()
        if len(v) > 1 and ind != UNCLASSIFIED
    ]
    groups.sort(key=lambda g: (-g["count"], g["industry"]))

    solo = [x for ind, v in by_industry.items() if len(v) == 1 or ind == UNCLASSIFIED for x in v]
    solo.sort(key=lambda x: (x["first_time"], x["code"]))
    other = {"label": OTHER_LABEL, "count": len(solo), "items": solo} if solo else None

    return {
        "total": len(items),
        "multi_board": len(ladder),
        "ladder": ladder,
        "groups": groups,
        "other": other,
    }


def ladder_text(ladder: list[dict[str, Any]], sep: str = " ｜ ") -> str:
    """把连板梯队压成一行文本（报告用；页面用自己的 HTML 渲染，但同一份 ladder 数据）。

    按板位分桶、板位从高到低：`5连板 某某（行业） ｜ 3连板 A（行业）、B（行业）`
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for x in ladder:
        buckets.setdefault(x["badge"], []).append(x)
    order = sorted(buckets, key=lambda b: (-max(x["boards"] for x in buckets[b]), b))
    return sep.join(
        f"**{badge}** " + "、".join(f"{x['name']}（{x['industry']}）" for x in buckets[badge])
        for badge in order
    )
