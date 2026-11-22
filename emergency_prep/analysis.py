"""多站点中断情景的业务覆盖分析。

回答应急通信负责人的核心问题：选择一个多站点中断情景后，
- 哪些业务承诺能满足、哪些不能；
- 最先失守的依赖与共享瓶颈是什么；
- 不同设备预置、业务次序、操作员安排之间如何取舍；
- 关键业务达不到时，放弃哪些次要业务仍然不够（不制造表面全覆盖）；
- 每条承诺当前有什么证据支撑，哪些只能列为待确认假设。
"""

from dataclasses import dataclass

from .evidence import (
    CONFIRMED,
    DISPUTED,
    FAILED,
    UNCONFIRMED,
    CapabilityStatus,
    EvidenceLedger,
)
from .inputs import Business, Inputs, Scenario
from .scheduler import (
    Plan,
    enumerate_plans,
    plan_metrics,
    rank_plans,
    shared_bottlenecks,
)


@dataclass
class CommitmentAssessment:
    business: str
    site: str
    priority: int
    kind: str
    config: str
    deadline_minutes: int
    duration_minutes: int
    depends: list[str]
    scheduled: bool
    planned_resources: dict | None
    planned_start_minute: int | None
    deadline_met: bool
    unmet_reason: str | None
    unmet_detail: str | None
    capability: CapabilityStatus
    assumption: bool  # 证据不足 => 仅为待确认假设

    def to_dict(self) -> dict:
        return {
            "business": self.business,
            "site": self.site,
            "priority": self.priority,
            "kind": self.kind,
            "config": self.config,
            "deadline_minutes": self.deadline_minutes,
            "duration_minutes": self.duration_minutes,
            "depends": self.depends,
            "scheduled": self.scheduled,
            "planned_resources": self.planned_resources,
            "planned_start_minute": self.planned_start_minute,
            "deadline_met": self.deadline_met,
            "unmet_reason": self.unmet_reason,
            "unmet_detail": self.unmet_detail,
            "capability": self.capability.to_dict(),
            "assumption": self.assumption,
        }


@dataclass
class ScenarioReport:
    scenario: str
    outage_sites: list[str]
    duration_minutes: int
    best_plan: Plan
    commitments: list[CommitmentAssessment]
    critical_gaps: list[dict]
    bottlenecks: list[dict]
    comparisons: list[dict]
    variant_count: int
    valid_count: int

    def to_dict(self) -> dict:
        return {
            "scenario": self.scenario,
            "outage_sites": self.outage_sites,
            "duration_minutes": self.duration_minutes,
            "variant_count": self.variant_count,
            "valid_count": self.valid_count,
            "best_plan": self.best_plan.to_dict(),
            "commitments": [c.to_dict() for c in self.commitments],
            "critical_gaps": self.critical_gaps,
            "bottlenecks": self.bottlenecks,
            "comparisons": self.comparisons,
        }


def analyze_scenario(
    inputs: Inputs,
    ledger: EvidenceLedger,
    scenario_name: str,
) -> ScenarioReport:
    scenario, plans = enumerate_plans(inputs, scenario_name)
    affected = inputs.businesses_for_scenario(scenario)
    ranked = rank_plans(inputs, plans)
    best = ranked[0]

    commitments = _assess_commitments(inputs, ledger, scenario, best)
    critical_gaps = _critical_gaps(inputs, plans, ranked, affected)
    bottlenecks = shared_bottlenecks(inputs, best)
    comparisons = _comparisons(inputs, ranked)

    return ScenarioReport(
        scenario=scenario.scenario,
        outage_sites=list(scenario.outage_sites),
        duration_minutes=scenario.duration_minutes,
        best_plan=best,
        commitments=commitments,
        critical_gaps=critical_gaps,
        bottlenecks=bottlenecks,
        comparisons=comparisons,
        variant_count=len(plans),
        valid_count=sum(1 for p in plans if p.valid),
    )


def _assess_commitments(
    inputs: Inputs,
    ledger: EvidenceLedger,
    scenario: Scenario,
    plan: Plan,
) -> list[CommitmentAssessment]:
    steps_by_biz = {s.business: s for s in plan.steps}
    unmet_by_biz = {u.business: u for u in plan.unmet}
    assessments: list[CommitmentAssessment] = []
    for name in sorted(
        affected_names(inputs, scenario),
        key=lambda biz_name: (inputs.businesses[biz_name].priority, biz_name),
    ):
        biz = inputs.businesses[name]
        step = steps_by_biz.get(name)
        unmet = unmet_by_biz.get(name)
        capability = _capability_for_step(ledger, biz, step)
        assessments.append(
            CommitmentAssessment(
                business=name,
                site=biz.site,
                priority=biz.priority,
                kind=biz.kind,
                config=biz.config,
                deadline_minutes=biz.deadline_minutes,
                duration_minutes=biz.duration_minutes,
                depends=list(biz.depends),
                scheduled=step is not None,
                planned_resources=(
                    {
                        "terminal": step.terminal,
                        "operator": step.operator,
                        "channel": step.channel,
                    }
                    if step
                    else None
                ),
                planned_start_minute=step.start_minute if step else None,
                deadline_met=step.deadline_met if step else False,
                unmet_reason=unmet.reason if unmet else None,
                unmet_detail=unmet.detail if unmet else None,
                capability=capability,
                assumption=capability.status != CONFIRMED,
            )
        )
    return assessments


def _capability_for_step(
    ledger: EvidenceLedger, biz: Business, step
) -> CapabilityStatus:
    if step is None:
        # 排不进计划：仍给出该业务在任意已观察终端上的最佳证据状态，供筹备参考
        matrix = [
            cap
            for cap in ledger.capability_matrix()
            if cap.business == biz.business and cap.config == biz.config
        ]
        order = {CONFIRMED: 0, DISPUTED: 1, FAILED: 2, UNCONFIRMED: 3}
        matrix.sort(key=lambda c: order[c.status])
        if matrix:
            cap = matrix[0]
            cap.resource = cap.resource + "（计划未选用）"
            return cap
        return CapabilityStatus(biz.business, biz.config, "（无）", UNCONFIRMED)
    return ledger.capability(biz.business, biz.config, step.terminal)


def affected_names(inputs: Inputs, scenario: Scenario) -> list[str]:
    return list(inputs.businesses_for_scenario(scenario).keys())


def _critical_gaps(
    inputs: Inputs,
    plans: list[Plan],
    ranked: list[Plan],
    affected: dict[str, Business],
) -> list[dict]:
    """关键业务（priority=1）缺口分析。

    若在所有可行方案族中某关键业务都无法满足，检查放弃全部次要业务后
    是否仍无法解决；明确给出"放弃 X 仍不够"的结论，而非表面全覆盖。
    """
    gaps: list[dict] = []
    priority1 = sorted(n for n, b in affected.items() if b.priority == 1)
    secondary = sorted(n for n, b in affected.items() if b.priority > 1)
    for name in priority1:
        plans_meeting = [p for p in plans if name in p.met_businesses and p.valid]
        if plans_meeting:
            continue
        # 所有方案都满足不了：找代表性失守原因
        reasons = sorted(
            {
                next(u.reason for u in p.unmet if u.business == name)
                for p in plans
                if any(u.business == name for u in p.unmet)
            }
        )
        details = [
            next(u.detail for u in p.unmet if u.business == name)
            for p in ranked
            if any(u.business == name for u in p.unmet)
        ]
        # 放弃次要业务能否腾出资源？枚举只保留关键业务的方案
        critical_only_plans = [
            p
            for p in plans
            if all(s.business in priority1 for s in p.steps)
        ]
        still_failing = all(name not in p.met_businesses for p in critical_only_plans)
        gaps.append(
            {
                "business": name,
                "reasons_across_variants": reasons,
                "representative_detail": details[0] if details else None,
                "secondary_businesses_sacrificed": secondary,
                "sacrifice_sufficient": not still_failing,
                "conclusion": (
                    f"即使放弃全部次要业务（{', '.join(secondary) or '无'}），"
                    f"{name} 仍无法在截止时间内满足"
                    if still_failing
                    else f"放弃次要业务（{', '.join(secondary)}）后 {name} 可满足"
                ),
            }
        )
    return gaps


def _comparisons(inputs: Inputs, ranked: list[Plan]) -> list[dict]:
    """方案族对比：预置 × 次序 × 操作员裁决，按优劣排序后输出。"""
    out = []
    for plan in ranked:
        metrics = plan_metrics(plan)
        p1_met = sum(
            1
            for s in plan.steps
            if inputs.businesses[s.business].priority == 1
        )
        out.append(
            {
                "preposition": dict(plan.preposition),
                "ordering": plan.ordering,
                "tie_policy": plan.tie_policy,
                "valid": plan.valid,
                "validation_errors": plan.validation_errors,
                "priority1_met": p1_met,
                "met": plan.met_businesses,
                "unmet": [u.business for u in plan.unmet],
                "total_move_minutes": metrics["total_move_minutes"],
                "total_moves": metrics["total_moves"],
                "max_finish_minute": metrics["max_finish_minute"],
            }
        )
    return out
