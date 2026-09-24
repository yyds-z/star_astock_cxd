# -*- coding: utf-8 -*-
"""评分融合模块（模块 5 的 Phase 1 实现）。

Phase 1 使用可解释的加权规则打分，而不是机器学习模型，原因：
1. 样本量不足时 ML 会过拟合；
2. 需要先产出可解释、可复盘的 baseline，才能判断后续模型是否真的更优；
3. 每个得分都能拆解到具体因子，便于自动复盘归因。

融合公式：
    最终分 = w1×策略强度 + w2×因子分 + w3×市场适配 − 风险扣分
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd

from astock.config import get_config
from astock.features.indicators import (
    clip_score,
    percentile_score,
    to_bool,
    to_float,
    to_list,
)
from astock.logger import get_logger
from astock.strategy.base import TIER_LABEL

logger = get_logger("scoring.scorer")


class Scorer:
    """多因子评分融合器。"""

    def __init__(self, cfg=None, storage=None) -> None:
        self.cfg = cfg or get_config()
        self.storage = storage  # 价值档基本面因子需要读库；缺省时自动跳过
        fcfg = self.cfg.section("scoring.fundamental")
        self.fundamental_enabled = bool(fcfg.get("enabled", True)) and storage is not None
        self.fundamental_weight = float(fcfg.get("value_weight", 0.40))
        self.fundamental_min_coverage = float(fcfg.get("min_coverage", 0.8))
        w = self.cfg.section("scoring.weights")
        raw_s = float(w.get("strategy_score", 0.53))
        raw_f = float(w.get("factor_score", 0.47))
        # 归一化：即使配置里手写了非归一的值（如旧的 0.45/0.40），
        # 也不会让 final_score 溢出 100 的上限。
        total_w = (raw_s + raw_f) or 1.0
        self.w_strategy = raw_s / total_w
        self.w_factor = raw_f / total_w
        self.risk = self.cfg.section("scoring.risk")
        # 每日**总**候选名额（按市场状态权重分配到各档），不再是「每档固定 N 只」。
        # 原因见 `_select_by_quota` 的注释：每档独立截断会让 tier_weights 完全失效。
        self.total_picks = int(self.cfg.get("scoring.total_picks", 15))
        # 单档名额上限；未配置的档位不设限。
        caps = self.cfg.section("scoring.tier_caps") or {}
        self.tier_caps: dict[str, int] = {
            str(k): int(v) for k, v in caps.items() if v is not None
        }

    # ---------------- 因子分 ----------------
    @staticmethod
    def build_factor_frame(features: pd.DataFrame) -> pd.DataFrame:
        """基于全市场截面计算各因子的分位数得分（0~100）。"""
        if features is None or features.empty:
            return pd.DataFrame()

        keep = [
            "code", "vol_ratio", "pct_20d", "rps120", "pos_60",
            "amount_ma20", "amplitude_20", "is_st", "is_new_stock",
            "close", "turn", "pct_chg",
        ]
        f = features[[c for c in keep if c in features.columns]].copy()

        p_vol = percentile_score(f["vol_ratio"])
        p_mom = percentile_score(f["pct_20d"])
        p_rps = percentile_score(f["rps120"])
        # 位置：偏好中高位（0.75 附近），过高（追高）与过低（弱势）都不理想
        p_pos = percentile_score(-(f["pos_60"] - 0.75).abs())

        f["factor_score"] = clip_score(0.30 * p_vol + 0.25 * p_mom + 0.30 * p_rps + 0.15 * p_pos)
        f["f_volume"] = p_vol.round(2)
        f["f_momentum"] = p_mom.round(2)
        f["f_rps"] = p_rps.round(2)
        f["f_position"] = p_pos.round(2)

        # 只保留「候选表里没有」的列，避免 merge 时产生 _x/_y 后缀
        overlap = {"close", "turn", "pct_chg", "vol_ratio", "rps120"}
        return f[[c for c in f.columns if c not in overlap]]

    # ---------------- 风险扣分 ----------------
    def risk_penalty(self, row: pd.Series) -> tuple[float, list[str]]:
        penalty = 0.0
        notes: list[str] = []

        if to_bool(row.get("is_st")):
            penalty += to_float(self.risk.get("st_penalty"), 100)
            notes.append("ST 风险")
        if to_float(row.get("amount_ma20")) < 100_000_000:
            penalty += to_float(self.risk.get("low_liquidity_penalty"), 30)
            notes.append("流动性偏低")
        if to_float(row.get("amplitude_20")) > 8:
            penalty += to_float(self.risk.get("high_volatility_penalty"), 15)
            notes.append("波动偏大")
        if to_float(row.get("pos_60")) > 0.95:
            penalty += to_float(self.risk.get("overbought_penalty"), 20)
            notes.append("处于60日高位，追高风险")
        if to_bool(row.get("is_new_stock")):
            penalty += to_float(self.risk.get("new_stock_penalty"), 10)
            notes.append("次新股波动风险")

        return penalty, notes

    # ---------------- 主流程 ----------------
    def score(
        self,
        candidates: pd.DataFrame,
        features: pd.DataFrame,
        regime: dict[str, Any] | None = None,
    ) -> pd.DataFrame:
        """对候选股打分，返回按档位排名并截断后的结果。"""
        if candidates is None or candidates.empty:
            return pd.DataFrame()

        factor_frame = self.build_factor_frame(features)
        if factor_frame.empty:
            return pd.DataFrame()

        df = candidates.merge(factor_frame, on="code", how="left")

        # ---- 价值档：接入基本面分（只作用于**有财务数据**的候选）----
        # 未采集到的候选保持纯技术面分不变：不做惩罚 ——
        # 「数据没采到」不等于「基本面差」。全市场回填需约 17 小时，
        # 所以这个降级路径在回填完成前是常态。
        if self.fundamental_enabled and "tier" in df.columns:
            try:
                as_of = None
                if "date" in getattr(features, "columns", []):
                    as_of = pd.to_datetime(features["date"].max()).date()
                if as_of is not None:
                    from astock.features.fundamentals import score_map

                    fund = score_map(
                        self.storage,
                        as_of,
                        df["code"].astype(str).tolist(),
                    )
                    if fund:
                        s = df["code"].astype(str).map(fund)
                        is_value = df["tier"] == "value"
                        value_n = int(is_value.sum())
                        mask = is_value & s.notna()
                        cover = (int(mask.sum()) / value_n) if value_n else 0.0
                        # 覆盖率门槛：财务是**分批采集**的（全市场回填约 17 小时）。
                        # 若只给「碰巧采到」的候选混入基本面分，同一天里
                        # 有数据/无数据的候选就是**两套评分口径**，排序不可比 ——
                        # 而这种偏差还会随采集批次变化，无法通过复盘识别。
                        # 因此覆盖率不足时**整批都不加**，宁可不用也不能不公平。
                        if value_n and cover >= self.fundamental_min_coverage:
                            df["fund_score"] = s
                            vw = self.fundamental_weight
                            df.loc[mask, "factor_score"] = (
                                (1 - vw)
                                * pd.to_numeric(df.loc[mask, "factor_score"], errors="coerce")
                                + vw * pd.to_numeric(s[mask], errors="coerce")
                            )
                            logger.info(
                                "价值档接入基本面：%d/%d 只（覆盖率 %.0f%%）",
                                int(mask.sum()), value_n, cover * 100,
                            )
                        elif value_n:
                            logger.warning(
                                "价值档财务覆盖率仅 %.0f%%（低于 %.0f%% 门槛）："
                                "本次不加基本面分，以免有数据与无数据的候选被两套口径评分。"
                                "补齐：python -m astock.cli finance --all",
                                cover * 100, self.fundamental_min_coverage * 100,
                            )
            except Exception as exc:  # noqa: BLE001
                # 基本面是增强项：任何失败都回退纯技术面，绝不影响选股主流程
                logger.warning("基本面因子加载失败，价值档回退纯技术面：%s", str(exc)[:120])

        # 档位权重**只用于名额分配**（`_select_by_quota`），不参与打分：
        # 它描述的是「资源投给哪个档位」，而个股质量应由策略强度与因子分决定。
        weights = {
            "short": float((regime or {}).get("w_short", 0.3)),
            "swing": float((regime or {}).get("w_swing", 0.4)),
            "value": float((regime or {}).get("w_value", 0.3)),
        }

        rows: list[dict[str, Any]] = []
        for _, r in df.iterrows():
            tier = str(r.get("tier"))
            penalty, risk_notes = self.risk_penalty(r)

            strategy_score = to_float(r.get("strategy_score"))
            factor_score = to_float(r.get("factor_score"))
            # 不再计入 market_fit：它在档内是常数，从未影响过排序；
            # 档位权重的作用由 `_select_by_quota` 的名额分配承担。
            final = (
                self.w_strategy * strategy_score
                + self.w_factor * factor_score
                - penalty
            )

            detail = {
                "策略强度": round(strategy_score, 2),
                "因子分": round(factor_score, 2),
                "量能分": round(to_float(r.get("f_volume")), 1),
                "动量分": round(to_float(r.get("f_momentum")), 1),
                "相对强度分": round(to_float(r.get("f_rps")), 1),
                "位置分": round(to_float(r.get("f_position")), 1),
                "风险扣分": round(penalty, 2),
                "权重": f"策略{self.w_strategy:.3f}/因子{self.w_factor:.3f}",
            }
            # 有财务数据时把基本面分一并展示，便于复盘时判断它是否真的有用
            fv = r.get("fund_score")
            if fv is not None and not pd.isna(fv):
                detail["基本面分"] = round(to_float(fv), 1)

            rows.append(
                {
                    "code": r["code"],
                    "name": str(r.get("name") or ""),
                    "tier": tier,
                    "tier_label": TIER_LABEL.get(tier, tier),
                    "strategy": r.get("strategy"),
                    "strategy_label": r.get("strategy_label"),
                    "strategy_score": round(strategy_score, 2),
                    "factor_score": round(factor_score, 2),
                    "risk_penalty": round(penalty, 2),
                    "final_score": round(float(min(max(final, 0), 100)), 2),
                    "close": to_float(r.get("close")),
                    "pct_chg": to_float(r.get("pct_chg")),
                    "turn": to_float(r.get("turn")),
                    "vol_ratio": to_float(r.get("vol_ratio")),
                    "rps120": to_float(r.get("rps120")),
                    "reasons": to_list(r.get("reasons")) + risk_notes,
                    "score_detail": json.dumps(detail, ensure_ascii=False),
                }
            )

        out = pd.DataFrame(rows)
        if out.empty:
            return out

        # 同一只股票可能被多个策略命中：保留最高分，理由合并
        out = self._dedupe(out)

        # 按市场状态权重分配名额（不再是「每档固定 N 只」）
        return self._select_by_quota(out, weights)

    # ---------------- 名额分配 ----------------
    @staticmethod
    def allocate_quota(total: int, weights: dict[str, float]) -> dict[str, int]:
        """最大余数法：按权重把 total 个名额分给各档，总和恰为 total。

        用最大余数法而不是简单四舍五入：直接 round 会让各档名额之和
        不等于 total（总名额漂移），而「恰好 N 个候选」是用户可预期的契约。
        """
        if total <= 0 or not weights:
            return dict.fromkeys(weights, 0)
        pos = {t: max(float(w), 0.0) for t, w in weights.items()}
        s = sum(pos.values()) or 1.0
        raw = {t: total * w / s for t, w in pos.items()}
        quota = {t: int(v // 1) for t, v in raw.items()}
        rest = total - sum(quota.values())
        for t in sorted(pos, key=lambda x: (-(raw[x] - quota[x]), -pos[x])):
            if rest <= 0:
                break
            quota[t] += 1
            rest -= 1
        return quota

    def _select_by_quota(self, out: pd.DataFrame, weights: dict[str, float]) -> pd.DataFrame:
        """按市场状态权重分配名额，而不是「每档各取前 N 只」。

        **为什么要改（实测发现的失效）**：原先每档独立排序、独立截断，
        而 `market_fit = w_tier / max_w × 100` 在**同一档内是常数**
        （只取决于候选所属档位）。档内排序时它只是一个常数偏移，
        **完全不改变档内名次**；截断又只看档内名次。
        ⇒ tier_weights 无论怎么调，选出的股票一模一样，所有权重校准都是零效果。

        配额制让权重真正决定「资源投给哪个档位」：
        · 总名额按权重分配到各档（最大余数法，总和恰为 total_picks）；
        · 某档候选不足时（短线档日均仅 2.2 只信号），空余名额**按权重**
          转给仍有候选的档；
        · 只有在所有档都填满后仍有余量时才停止（不会硬凑）。
        """
        tiers = list(out["tier"].unique())
        w = {t: float(weights.get(t, 0.0)) for t in tiers}
        quota = self.allocate_quota(self.total_picks, w)

        groups = {
            t: out[out["tier"] == t].sort_values("final_score", ascending=False)
            for t in tiers
        }
        taken = {t: min(quota.get(t, 0), len(groups[t])) for t in tiers}

        # 单档上限：某些档位"选得越多越差"，必须单独封顶。
        # 封顶必须发生在**补位轮之前**，否则空出的名额不会转给其它档
        # （那样等于白白少选几只，而不是把名额挪到更该用的地方）。
        for t, cap in self.tier_caps.items():
            if t in taken and taken[t] > cap:
                logger.info("档位 %s 触发上限：拟选 %d 只 → 封顶 %d 只", t, taken[t], cap)
                taken[t] = cap

        # 补位轮：把用不完的名额按权重转给仍有候选的档。
        # 每轮至少吸收 1 个名额（最大余数法保证），故 total_picks+2 轮内必收敛。
        for _ in range(self.total_picks + 2):
            gap = self.total_picks - sum(taken.values())
            if gap <= 0:
                break
            room = {
                t: max(w.get(t, 0.0), 1e-9) if len(groups[t]) > taken[t] else 0.0
                for t in tiers
            }
            if sum(room.values()) <= 0:
                break
            extra = self.allocate_quota(gap, room)
            if not any(extra.values()):
                break
            for t in tiers:
                taken[t] = min(taken[t] + extra.get(t, 0), len(groups[t]))

        picks = [groups[t].head(taken[t]) for t in tiers if taken[t] > 0]
        if not picks:
            return out.iloc[0:0]
        sel = pd.concat(picks)
        sel = sel.sort_values(["tier", "final_score"], ascending=[True, False])
        sel["tier_rank"] = sel.groupby("tier").cumcount() + 1
        return sel.reset_index(drop=True)

    # ---------------- 去重 ----------------
    @staticmethod
    def _dedupe(df: pd.DataFrame) -> pd.DataFrame:
        """同一代码同一档位去重：保留最高分。

        同时保留两个口径，避免后续统计被「组合名称」污染：
        - `primary_strategy`：得分最高的那条策略（用于按策略归因统计）
        - `strategy` / `strategy_label`：拼接后的全部命中策略（用于展示）
        - `hit_count`：命中的策略数量（用于验证「多策略共振」是否真的更好）
        """
        merged: list[dict[str, Any]] = []
        for (_code, _tier), group in df.groupby(["code", "tier"], sort=False):
            best = group.sort_values("final_score", ascending=False).iloc[0].to_dict()
            best["primary_strategy"] = best.get("strategy")
            best["primary_strategy_label"] = best.get("strategy_label")

            labels = list(dict.fromkeys(group["strategy_label"].dropna().tolist()))
            names = list(dict.fromkeys(group["strategy"].dropna().tolist()))
            reasons: list[str] = []
            for r in group["reasons"]:
                for item in r:
                    if item not in reasons:
                        reasons.append(item)

            best["strategy_label"] = " + ".join(labels) if labels else best.get("strategy_label")
            best["strategy"] = "+".join(names)
            best["reasons"] = reasons
            best["hit_count"] = len(group)
            merged.append(best)
        return pd.DataFrame(merged)
