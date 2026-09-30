# -*- coding: utf-8 -*-
"""Skill 库：把每个策略落成独立目录（档案 + 参数快照 + 历史成功率）。

参考 UZI-Skill 的 skill 规范：
- SKILL.md 带 YAML frontmatter，描述触发场景与能力
- meta.yaml 为机器可读元数据
- references/ 存细节文档
- 通过 registry.json 提供程序可读索引
"""

from astock.skills import catalog, performance
from astock.skills.store import SkillsStore

__all__ = ["SkillsStore", "catalog", "performance"]
