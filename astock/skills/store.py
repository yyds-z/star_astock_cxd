# -*- coding: utf-8 -*-
"""Skill 库落盘（参考 UZI-Skill 的目录规范）。

目录结构：

```
skills/
├── README.md                 索引（人读）
├── registry.json             索引（程序读：前端/API/Agent 都能消费）
└── <strategy_name>/
    ├── SKILL.md              策略完整档案（YAML frontmatter + 判定条件 + 表现）
    ├── meta.yaml             机器可读元数据（含参数快照，用于版本 diff）
    ├── performance.json      历史成功率（回测 + 实盘跟踪，自动更新）
    └── references/
        ├── conditions.md     条件详解与设计依据
        └── changelog.md      参数版本演进记录
```

设计要点：
1. **策略代码与档案分离**：实现留在 `astock/strategy/*.py`，档案落在 `skills/`。
   这样策略进化时，「改了什么参数、为什么改、改完表现如何」有据可查。
2. **参数变更自动记版本**：`meta.yaml` 保存参数快照，下次 sync 时 diff，
   有变化就升版本并追加 changelog —— 这是策略迭代的审计链。
3. **表现数据不覆盖**：回测与实盘跟踪分开记录（见 performance.py）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from astock.config import get_config
from astock.logger import get_logger
from astock.skills import catalog, performance
from astock.storage.db import Storage
from astock.strategy import STRATEGY_REGISTRY
from astock.strategy.base import TIER_LABEL

__all__ = ["SkillsStore"]

logger = get_logger("skills.store")

REGISTRY_VERSION = "1"


class SkillsStore:
    """Skill 库读写。"""

    def __init__(self, storage: Storage | None = None, root: Path | None = None) -> None:
        self.cfg = get_config()
        self.storage = storage
        if root is not None:
            self.root = root
        else:
            raw = self.cfg.get("skills.dir", "skills")
            p = Path(raw)
            self.root = p if p.is_absolute() else self.cfg.project_root / p
        self.root.mkdir(parents=True, exist_ok=True)
        # 表现数据缓存：sync() 与 render 阶段共用，避免重复查库
        self._perf_cache: dict[str, dict[str, Any]] = {}

    # ---------------- 扫描策略 ----------------
    def _strategies(self) -> list[dict[str, Any]]:
        """收集所有策略（含被禁用的，禁用状态记录在 status 里）。"""
        items: list[dict[str, Any]] = []
        for tier, classes in STRATEGY_REGISTRY.items():
            for cls in classes:
                params = self.cfg.section(f"strategy.{tier}.{cls.name}") or {}
                items.append(
                    {
                        "name": cls.name,
                        "label": cls.label,
                        "tier": tier,
                        "tier_label": TIER_LABEL.get(tier, tier),
                        "require_col": cls.require_col,
                        "params": {k: v for k, v in params.items() if k != "enabled"},
                        "enabled": bool(params.get("enabled", True)),
                        "module": f"{cls.__module__}.{cls.__name__}",
                    }
                )
        return sorted(items, key=lambda x: (x["tier"], x["name"]))

    # ---------------- 单文件渲染 ----------------
    def _meta_path(self, name: str) -> Path:
        return self.root / name / "meta.yaml"

    def _read_meta(self, name: str) -> dict[str, Any]:
        path = self._meta_path(name)
        if not path.exists():
            return {}
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return {}

    @staticmethod
    def _bump(version: str, level: str = "minor") -> str:
        try:
            major, minor, patch = (int(x) for x in str(version).split("."))
        except (ValueError, AttributeError):
            major, minor, patch = 1, 0, 0
        if level == "major":
            return f"{major + 1}.0.0"
        if level == "minor":
            return f"{major}.{minor + 1}.0"
        return f"{major}.{minor}.{patch + 1}"

    def _diff_params(
        self, old: dict[str, Any], new: dict[str, Any]
    ) -> list[str]:
        """比较参数快照，返回人类可读的变更列表。"""
        changes: list[str] = []
        for key in sorted(set(old) | set(new)):
            before, after = old.get(key), new.get(key)
            if before != after:
                changes.append(f"`{key}`: {before} → {after}")
        return changes

    def render_meta(
        self,
        item: dict[str, Any],
        profile: dict[str, Any],
        perf: dict[str, Any],
        previous: dict[str, Any],
    ) -> dict[str, Any]:
        """生成 meta.yaml 的内容（含版本演进处理）。"""
        now = datetime.now().isoformat(timespec="seconds")
        old_params = (previous.get("params") or {}) if previous else {}
        changes = self._diff_params(old_params, item["params"]) if previous else []

        if not previous:
            version = "1.0.0"
            created_at = now
        elif changes:
            version = self._bump(str(previous.get("version", "1.0.0")), "minor")
            created_at = previous.get("created_at", now)
        else:
            version = str(previous.get("version", "1.0.0"))
            created_at = previous.get("created_at", now)

        backtest = (perf or {}).get("backtest") or {}
        return {
            "name": item["name"],
            "label": item["label"],
            "description": profile.get("summary", ""),
            "version": version,
            "tier": item["tier"],
            "tier_label": item["tier_label"],
            "status": "active" if item["enabled"] else "disabled",
            "author": "astock_ai",
            "license": "MIT",
            "created_at": created_at,
            "updated_at": now,
            "metadata": {
                "astock": {
                    "tags": [item["tier"], item["name"]],
                    "related_skills": [
                        k for k in catalog.all_names()
                        if k != item["name"] and catalog.get(k).get("source", {}).get("project")
                        == profile.get("source", {}).get("project")
                    ][:5],
                    "require_col": item["require_col"],
                    "implementation": item["module"],
                    "params_source": f"config/settings.yaml#strategy.{item['tier']}.{item['name']}",
                }
            },
            "conditions": profile.get("conditions", []),
            "apply_when": profile.get("apply_when", []),
            "avoid_when": profile.get("avoid_when", []),
            "params": item["params"],
            "source": profile.get("source", {}),
            "performance": {
                "verdict": backtest.get("verdict", "no_data"),
                "sample_size": backtest.get("sample_size"),
                "win_rate": backtest.get("win_rate"),
                "avg_ret1": backtest.get("avg_ret1"),
                "backtest_run_id": backtest.get("run_id"),
            },
            "param_changes": changes,
        }

    def render_skill_md(self, meta: dict[str, Any], profile: dict[str, Any]) -> str:
        """生成 SKILL.md（YAML frontmatter + 正文），格式对齐 UZI-Skill。"""
        perf_bt = ((self._perf_cache.get(meta["name"]) or {}).get("backtest")) or {}
        perf_live = ((self._perf_cache.get(meta["name"]) or {}).get("live")) or {}

        front = yaml.safe_dump(
            {
                "name": meta["name"],
                "description": (
                    f"{meta['label']}策略（{meta['tier_label']}档）。"
                    f"{profile.get('summary', '')}"
                    f"触发场景：{'、'.join(profile.get('apply_when', [])) or '不限'}。"
                    f"关键词：{meta['name']}, {meta['label']}, {meta['tier']}。"
                ),
                "version": meta["version"],
                "tier": meta["tier"],
                "status": meta["status"],
                "license": "MIT",
                "metadata": meta.get("metadata", {}),
            },
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ).strip()

        lines = [f"---\n{front}\n---", ""]
        lines.append(f"# {meta['label']} · `{meta['name']}`")
        lines.append("")
        lines.append(f"> {profile.get('summary', '')}")
        lines.append("")

        lines.append("## 🎯 策略定位")
        lines.append("")
        lines.append(f"- 档位：**{meta['tier_label']}**（`{meta['tier']}`）")
        lines.append(f"- 状态：**{meta['status']}**　|　版本：**{meta['version']}**")
        lines.append(f"- 实现：`{meta['metadata']['astock']['implementation']}`")
        lines.append(f"- 数据依赖：`{meta['metadata']['astock']['require_col']}`（不足时该股自动跳过，不剔除出池）")
        lines.append("")
        if profile.get("rationale"):
            lines.append(f"**设计逻辑**：{profile['rationale']}")
            lines.append("")

        lines.append("## 📐 判定条件（全部满足才命中）")
        lines.append("")
        for i, cond in enumerate(profile.get("conditions", []), 1):
            lines.append(f"{i}. {cond}")
        if not profile.get("conditions"):
            lines.append("_（待补充）_")
        lines.append("")

        lines.append("## ⚙️ 参数")
        lines.append("")
        lines.append(f"来源：`{meta['metadata']['astock']['params_source']}`")
        lines.append("")
        lines.append("| 参数 | 当前值 |")
        lines.append("|---|---|")
        for key, value in (meta.get("params") or {}).items():
            lines.append(f"| `{key}` | `{value}` |")
        if not meta.get("params"):
            lines.append("| - | - |")
        lines.append("")

        lines.append("## 🌦️ 适用环境")
        lines.append("")
        lines.append(f"- 适用市场状态：{_join(profile.get('apply_when'))}")
        lines.append(f"- 规避市场状态：{_join(profile.get('avoid_when'))}")
        lines.append("")

        lines.append("## 📊 历史表现")
        lines.append("")
        lines.append("### 样本外回测")
        lines.append("")
        if perf_bt:
            lines.append(f"- 判定：**{_verdict_text(perf_bt.get('verdict'))}** —— {perf_bt.get('verdict_reason', '')}")
            lines.append(f"- 区间：{perf_bt.get('period', {}).get('start')} ~ "
                         f"{perf_bt.get('period', {}).get('end')}"
                         f"（{perf_bt.get('period', {}).get('trading_days')} 个交易日）")
            lines.append(f"- 样本：{perf_bt.get('sample_size')} 条")
            lines.append(f"- T+1 胜率 / 平均收益：{perf_bt.get('win_rate')}% / {perf_bt.get('avg_ret1')}%")
            lines.append(f"- 持有 3/5/10 日平均：{perf_bt.get('avg_ret3')}% / "
                         f"{perf_bt.get('avg_ret5')}% / {perf_bt.get('avg_ret10')}%")
            lines.append(f"- 盈亏比：{perf_bt.get('profit_loss_ratio')}　|　"
                         f"平均最大涨/撤：{perf_bt.get('avg_max_gain')}% / {perf_bt.get('avg_max_dd')}%")
            by_state = perf_bt.get("by_market_state") or {}
            if by_state:
                lines.append("")
                lines.append("| 市场状态 | 样本 | 胜率 | T+1 平均 |")
                lines.append("|---|---|---|---|")
                for state, s in sorted(by_state.items(), key=lambda x: -(x[1].get("count") or 0)):
                    lines.append(f"| {state} | {s.get('count')} | {s.get('win_rate')}% | {s.get('avg_ret1')}% |")
        else:
            lines.append("_尚无回测数据。运行 `python -m astock.cli backtest` 后自动填充。_")
        lines.append("")
        lines.append("### 实盘纸面跟踪")
        lines.append("")
        if perf_live:
            lines.append(f"- 样本：{perf_live.get('count')} 条（{perf_live.get('first_date')} ~ {perf_live.get('last_date')}）")
            lines.append(f"- 胜率：{perf_live.get('win_rate')}%　|　平均次日收益：{perf_live.get('avg_ret1')}%")
        else:
            lines.append("_尚无实际推荐记录。_")
        lines.append("")

        lines.append("## ⛔ 已知失效场景")
        lines.append("")
        for p in profile.get("pitfalls", []):
            lines.append(f"- {p}")
        if not profile.get("pitfalls"):
            lines.append("- _（待补充）_")
        lines.append("")

        lines.append("## 📁 数据契约")
        lines.append("")
        lines.append("| 文件 | 谁写 | 谁读 | 说明 |")
        lines.append("|---|---|---|---|")
        lines.append("| `SKILL.md` | `skills sync` | 人 / Agent | 本档案 |")
        lines.append("| `meta.yaml` | `skills sync` | 程序 | 元数据与参数快照，用于版本 diff |")
        lines.append("| `performance.json` | `skills stats` | 程序 / 前端 | 历史成功率统计 |")
        lines.append("| `references/conditions.md` | `skills sync` | 人 | 条件详解与设计依据 |")
        lines.append("| `references/changelog.md` | `skills sync` | 人 | 参数版本演进 |")
        lines.append("")

        lines.append("## 📚 移植来源")
        lines.append("")
        src = profile.get("source", {})
        if src.get("project"):
            lines.append(f"- 项目：{src.get('project')}")
            lines.append(f"- 文件：`{src.get('file')}`")
            if src.get("note"):
                lines.append(f"- 说明：{src['note']}")
        else:
            lines.append("_（无外部来源）_")
        lines.append("")

        if meta.get("param_changes"):
            lines.append("## 🔄 本次同步的参数变更")
            lines.append("")
            for change in meta["param_changes"]:
                lines.append(f"- {change}")
            lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("> 本档案由 `python -m astock.cli skills sync` 自动生成，"
                     "策略知识部分维护在 `astock/skills/catalog.py`。")
        return "\n".join(lines)

    @staticmethod
    def render_conditions(meta: dict[str, Any], profile: dict[str, Any]) -> str:
        lines = [f"# {meta['label']} · 条件详解", ""]
        lines.append(f"> {profile.get('summary', '')}")
        lines.append("")
        lines.append("## 为什么这样设计")
        lines.append("")
        lines.append(profile.get("rationale") or "_（待补充）_")
        lines.append("")
        lines.append("## 条件清单")
        lines.append("")
        for i, cond in enumerate(profile.get("conditions", []), 1):
            lines.append(f"### {i}. {cond.split('：')[0]}")
            lines.append("")
            lines.append(cond)
            lines.append("")
        lines.append("## 与参数的对应关系")
        lines.append("")
        lines.append("| 参数 | 当前值 | 影响 |")
        lines.append("|---|---|---|")
        for key, value in (meta.get("params") or {}).items():
            lines.append(f"| `{key}` | `{value}` | 调整该值会直接改变命中数量与信号质量 |")
        lines.append("")
        return "\n".join(lines)

    # ---------------- 同步 ----------------
    def sync(self) -> dict[str, Any]:
        """把策略代码 + 配置参数 + 表现数据 渲染成 Skill 目录。"""
        self._perf_cache = performance.build(self.storage) if self.storage else {}
        items = self._strategies()
        written: list[str] = []

        for item in items:
            name = item["name"]
            skill_dir = self.root / name
            (skill_dir / "references").mkdir(parents=True, exist_ok=True)

            profile = catalog.get(name)
            previous = self._read_meta(name)
            perf = self._perf_cache.get(name) or {}

            meta = self.render_meta(item, profile, perf, previous)

            (skill_dir / "meta.yaml").write_text(
                yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=False),
                encoding="utf-8",
            )
            (skill_dir / "SKILL.md").write_text(
                self.render_skill_md(meta, profile), encoding="utf-8"
            )
            (skill_dir / "performance.json").write_text(
                json.dumps(perf, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (skill_dir / "references" / "conditions.md").write_text(
                self.render_conditions(meta, profile), encoding="utf-8"
            )
            self._append_changelog(skill_dir, meta, previous)
            written.append(name)

        self._write_registry(items, written)
        self._write_readme(items)
        logger.info("Skill 库同步完成：%d 个策略 → %s", len(written), self.root)
        return {"root": str(self.root), "skills": written}

    def _append_changelog(
        self, skill_dir: Path, meta: dict[str, Any], previous: dict[str, Any]
    ) -> None:
        """参数变化时追加一条版本演进记录。"""
        path = skill_dir / "references" / "changelog.md"
        if not path.exists():
            path.write_text(
                f"# {meta['label']} · 版本演进\n\n"
                "> 参数一旦变化，`skills sync` 会自动升版本并在此追加记录，"
                "形成策略迭代的审计链。\n\n",
                encoding="utf-8",
            )

        if not previous:
            entry = (
                f"## v{meta['version']} · {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
                "- 首次建档\n"
            )
        elif meta.get("param_changes"):
            changes = "\n".join(f"- 参数变更：{c}" for c in meta["param_changes"])
            perf = meta.get("performance") or {}
            entry = (
                f"## v{meta['version']} · {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
                f"{changes}\n"
                f"- 变更后表现：样本 {perf.get('sample_size')}，"
                f"胜率 {perf.get('win_rate')}%，T+1 平均 {perf.get('avg_ret1')}%\n"
            )
        else:
            return

        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n" + entry)

    def _write_registry(self, items: list[dict[str, Any]], written: list[str]) -> None:
        """registry.json：程序可读索引（前端 / API / Agent 都能消费）。"""
        entries = []
        for item in items:
            meta = self._read_meta(item["name"]) or {}
            perf = self._perf_cache.get(item["name"]) or {}
            bt = perf.get("backtest") or {}
            entries.append(
                {
                    "name": item["name"],
                    "label": item["label"],
                    "tier": item["tier"],
                    "status": meta.get("status", "active"),
                    "version": meta.get("version", "1.0.0"),
                    "path": f"skills/{item['name']}/SKILL.md",
                    "summary": catalog.get(item["name"]).get("summary", ""),
                    "verdict": bt.get("verdict", "no_data"),
                    "sample_size": bt.get("sample_size"),
                    "win_rate": bt.get("win_rate"),
                    "avg_ret1": bt.get("avg_ret1"),
                    "apply_when": catalog.get(item["name"]).get("apply_when", []),
                }
            )

        registry = {
            "registry_version": REGISTRY_VERSION,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "root": str(self.root),
            "count": len(entries),
            "by_tier": {
                tier: [e["name"] for e in entries if e["tier"] == tier]
                for tier in ("short", "swing", "value")
            },
            "skills": entries,
        }
        (self.root / "registry.json").write_text(
            json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("registry.json 已更新（%d 个 skill）", len(written))

    def _write_readme(self, items: list[dict[str, Any]]) -> None:
        lines = [
            "# 策略 Skill 库",
            "",
            "> 每个策略一个独立目录，包含完整档案、参数快照与历史成功率。",
            "> 由 `python -m astock.cli skills sync` 自动生成，"
            "策略知识维护在 `astock/skills/catalog.py`。",
            "",
            "## 索引",
            "",
            "| 档位 | 策略 | 版本 | 状态 | 样本 | 胜率 | T+1 平均 | 判定 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        verdict_text = {
            "effective": "✅ 有效",
            "watching": "🟡 观察",
            "ineffective": "❌ 无效",
            "insufficient": "❓ 样本不足",
            "no_data": "— 无数据",
        }
        for item in items:
            meta = self._read_meta(item["name"]) or {}
            perf = (self._perf_cache.get(item["name"]) or {}).get("backtest") or {}
            lines.append(
                f"| {item['tier_label']} | [{item['label']}]({item['name']}/SKILL.md) "
                f"| {meta.get('version', '-')} | {meta.get('status', '-')} "
                f"| {perf.get('sample_size', '-')} | {perf.get('win_rate', '-')}% "
                f"| {perf.get('avg_ret1', '-')}% "
                f"| {verdict_text.get(perf.get('verdict', 'no_data'), '-')} |"
            )

        lines += [
            "",
            "## 目录约定（参考 UZI-Skill 规范）",
            "",
            "```",
            "skills/<strategy_name>/",
            "├── SKILL.md              策略档案（frontmatter + 判定条件 + 表现）",
            "├── meta.yaml             元数据与参数快照（用于版本 diff）",
            "├── performance.json      历史成功率（回测 + 实盘跟踪）",
            "└── references/",
            "    ├── conditions.md     条件详解与设计依据",
            "    └── changelog.md      参数版本演进，改参数自动追加",
            "```",
            "",
            "## 怎么用",
            "",
            "```bat",
            "python -m astock.cli skills list            :: 列出所有策略与判定",
            "python -m astock.cli skills show turtle_trade  :: 查看单个策略档案",
            "python -m astock.cli skills sync            :: 重新同步（含参数变更记版本）",
            "python -m astock.cli skills stats           :: 只刷新历史成功率",
            "```",
            "",
            "## 调参流程（靠它做策略进化）",
            "",
            "1. 看 `performance.json` 或本页表格，找出判定为 ❌ 无效 / 🟡 观察 的策略",
            "2. 改 `config/settings.yaml` 中对应的参数",
            "3. 跑 `python -m astock.cli skills sync` → 自动升版本并写入 changelog",
            "4. 重跑 `python -m astock.cli backtest` → 观察表现是否改善",
            "5. 对比 changelog 里的「变更前 / 变更后表现」，决定保留还是回滚",
            "",
            "> 判定门槛：样本 ≥ 30 条才算数；胜率 ≥ 50% 且平均收益 > 0 记 ✅ 有效；",
            "> 平均收益 ≤ 0 记 ❌ 无效（建议排查或淘汰）。",
        ]
        (self.root / "README.md").write_text("\n".join(lines), encoding="utf-8")

    # ---------------- 查询 ----------------
    def list_skills(self) -> list[dict[str, Any]]:
        path = self.root / "registry.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data.get("skills", [])

    def get_skill(self, name: str) -> dict[str, Any] | None:
        skill_dir = self.root / name
        if not skill_dir.exists():
            return None
        result: dict[str, Any] = {"name": name, "dir": str(skill_dir)}
        for filename, key, kind in (
            ("SKILL.md", "skill_md", "text"),
            ("meta.yaml", "meta", "yaml"),
            ("performance.json", "performance", "json"),
        ):
            path = skill_dir / filename
            if not path.exists():
                continue
            raw = path.read_text(encoding="utf-8")
            if kind == "yaml":
                result[key] = yaml.safe_load(raw) or {}
            elif kind == "json":
                result[key] = json.loads(raw)
            else:
                result[key] = raw
        for ref in sorted((skill_dir / "references").glob("*.md")):
            result.setdefault("references", {})[ref.stem] = ref.read_text(encoding="utf-8")
        return result


def _join(values: Any) -> str:
    if not values:
        return "不限"
    return "、".join(str(v) for v in values)


def _verdict_text(verdict: str | None) -> str:
    return {
        "effective": "✅ 有效",
        "watching": "🟡 观察（低胜率高赔率）",
        "ineffective": "❌ 无效",
        "insufficient": "❓ 样本不足",
        "no_data": "— 尚未回测",
    }.get(str(verdict), "—")
