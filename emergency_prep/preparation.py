"""筹备安排与演练登记：占用、排他、回填核验与重评估。

核心规则：
- 确认的筹备安排占用设备与演练时间；另一组演练不能重复确认同一终端或操作员，
  取消后才释放（SAT 按 capacity 计并发占用）；
- 已有演练只有在计划版本、设备配置和业务步骤相符时才支持能力判断；
- 回填后按原情景核对承诺是否被证据支持，区分流程执行失败、资源不够、配置不适用；
- 证据显示某共享设备能力不足时，所有引用它的未执行安排都要重新评估；
  已执行演练保留当时安排及差异，不能用修订后容量重写；
- 现场临时借调设备不证明原配置具备该能力；
- 补演只针对尚不能支撑的承诺与真实争议，并遵守已确认的其他筹备占用。
"""

from .evidence import (
    CONFIRMED,
    DISPUTED,
    FAILED,
    UNCONFIRMED,
    EvidenceLedger,
    StoredObservation,
)
from .inputs import Inputs
from .scheduler import Plan
from .timeutil import iso, now_utc, parse_iso

# 安排/演练状态
BOOKED = "booked"          # 已确认占用
CANCELLED = "cancelled"    # 已取消，资源释放
PENDING = "pending"        # 已登记待执行（同样占用资源）
EXECUTED = "executed"      # 已回填结果

# 回填核验结论
SUPPORTED = "supported"
PROCESS_FAILURE = "process_failure"          # 流程执行失败
RESOURCE_INSUFFICIENT = "resource_insufficient"  # 资源不够
CONFIG_INAPPLICABLE = "config_inapplicable"  # 配置不适用
MISSING_EVIDENCE = "missing_evidence"        # 无证据
DISPUTED_VERDICT = "disputed"                # 证据争议


class BookingError(ValueError):
    """筹备登记违反占用或状态规则。"""


def _overlaps(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    return parse_iso(a_start) < parse_iso(b_end) and parse_iso(b_start) < parse_iso(a_end)


def active_arrangements(state: dict) -> list[dict]:
    """仍占用资源的安排：pending/booked/executed。

    已执行演练保留当时安排，资源在其时间窗内仍视为被占用；
    只有 cancelled 明确释放。
    """
    return [
        a
        for a in state["arrangements"]
        if a["status"] in (PENDING, BOOKED, EXECUTED)
    ]


def active_drills(state: dict) -> list[dict]:
    return [
        d
        for d in state["drills"]
        if d["status"] in (PENDING, BOOKED, EXECUTED)
    ]


def check_resource_conflicts(
    inputs: Inputs,
    state: dict,
    resources: list[str],
    window_start: str,
    window_end: str,
    exclude_arrangement_id: str | None = None,
) -> list[dict]:
    """检查资源在时间窗内的占用冲突。

    终端/操作员容量为 1 => 同一时刻不可重复确认；
    通道按 capacity 计并发。冲突返回明细清单。
    """
    conflicts: list[dict] = []
    window_resources = set(resources)
    # 安排与演练都登记资源占用，统一检查；已执行记录同样保留占用
    holders = [
        ("arrangement", a) for a in active_arrangements(state)
    ] + [
        ("drill", d) for d in active_drills(state)
    ]
    for kind, arrangement in holders:
        if arrangement["id"] == exclude_arrangement_id:
            continue
        if not _overlaps(window_start, window_end, arrangement["window_start"], arrangement["window_end"]):
            continue
        for res_name in arrangement["resources"]:
            if res_name not in window_resources:
                continue
            res = inputs.resources[res_name]
            if res.capacity == 1:
                conflicts.append(
                    {
                        "resource": res_name,
                        "type": res.type,
                        "held_by": f"{kind}:{arrangement['id']}",
                        "reason": (
                            f"{res_name} 已被{('演练' if kind == 'drill' else '安排')} "
                            f"{arrangement['id']} 在重叠时间占用（容量 1，不可重复确认）"
                        ),
                    }
                )
            else:
                # 多容量资源：统计窗内并发
                concurrent = _concurrent_holds(
                    state, res_name, window_start, window_end, exclude_arrangement_id
                )
                if concurrent >= res.capacity:
                    conflicts.append(
                        {
                            "resource": res_name,
                            "type": res.type,
                            "held_by": f"{kind}:{arrangement['id']}",
                            "reason": (
                                f"{res_name} 并发占用 {concurrent} 已达容量 {res.capacity}"
                            ),
                        }
                    )
    return conflicts


def _concurrent_holds(
    state: dict,
    resource: str,
    window_start: str,
    window_end: str,
    exclude_id: str | None,
) -> int:
    count = 0
    holders = [
        a for a in active_arrangements(state)
    ] + [
        d for d in active_drills(state)
    ]
    for arrangement in holders:
        if arrangement["id"] == exclude_id:
            continue
        if resource not in arrangement["resources"]:
            continue
        if _overlaps(window_start, window_end, arrangement["window_start"], arrangement["window_end"]):
            count += 1
    return count


def create_arrangement(
    state: dict,
    arrangement_id: str,
    title: str,
    scenario: str,
    plan: Plan,
    resources: list[str],
    window_start: str,
    window_end: str,
    note: str = "",
) -> dict:
    """登记一条筹备安排（立即占用资源）。调用前须先做冲突检查。"""
    if parse_iso(window_end) <= parse_iso(window_start):
        raise BookingError("演练时间窗结束必须晚于开始")
    plan_dict = plan.to_dict()
    if not plan_dict["valid"]:
        raise BookingError("不能登记校验未通过的计划：" + "；".join(plan_dict["validation_errors"]))
    arrangement = {
        "id": arrangement_id,
        "title": title,
        "scenario": scenario,
        "status": BOOKED,
        "resources": sorted(set(resources)),
        "window_start": window_start,
        "window_end": window_end,
        "plan_snapshot": plan_dict,
        "note": note,
        "created_at": iso(now_utc()),
        "cancelled_at": None,
        "cancel_reason": None,
    }
    state["arrangements"].append(arrangement)
    return arrangement


def cancel_arrangement(state: dict, arrangement_id: str, reason: str) -> dict:
    """取消安排，释放其占用（已执行演练不可取消，只能保留差异）。"""
    arrangement = _find(state["arrangements"], arrangement_id, "安排")
    if arrangement["status"] == EXECUTED:
        raise BookingError(f"安排 {arrangement_id} 已执行，只能保留当时记录与差异，不能取消")
    arrangement["status"] = CANCELLED
    arrangement["cancelled_at"] = iso(now_utc())
    arrangement["cancel_reason"] = reason
    # 关联的未执行演练一并释放
    for drill in state["drills"]:
        if drill.get("arrangement_id") == arrangement_id and drill["status"] in (PENDING, BOOKED):
            drill["status"] = CANCELLED
            drill["cancelled_at"] = arrangement["cancelled_at"]
            drill["cancel_reason"] = f"随安排 {arrangement_id} 取消：{reason}"
    return arrangement


def register_drill(
    state: dict,
    drill_id: str,
    title: str,
    scenario: str,
    plan: Plan,
    resources: list[str],
    window_start: str,
    window_end: str,
    arrangement_id: str | None,
    expected_configs: dict[str, str],
    note: str = "",
) -> dict:
    """登记一组演练（占用资源）。expected_configs: business -> config。"""
    if parse_iso(window_end) <= parse_iso(window_start):
        raise BookingError("演练时间窗结束必须晚于开始")
    if not plan.to_dict()["valid"]:
        raise BookingError("不能基于校验未通过的计划登记演练")
    drill = {
        "id": drill_id,
        "title": title,
        "scenario": scenario,
        "status": BOOKED,
        "arrangement_id": arrangement_id,
        "resources": sorted(set(resources)),
        "window_start": window_start,
        "window_end": window_end,
        "plan_snapshot": plan.to_dict(),
        "expected_configs": dict(expected_configs),
        "actual_steps": [],
        "verification": None,
        "note": note,
        "created_at": iso(now_utc()),
        "cancelled_at": None,
        "cancel_reason": None,
    }
    state["drills"].append(drill)
    return drill


# ------------------------------------------------------------------ 回填核验

def backfill_drill(
    inputs: Inputs,
    state: dict,
    ledger: EvidenceLedger,
    drill_id: str,
    actual_steps: list[dict],
    imported_from: str,
) -> dict:
    """回填实际步骤与完成时刻，按原情景与原计划快照核验。

    actual_steps 每行：
    {business, config, resource, source, result, start, end, borrowed?}
    观察编号自动生成（D<drill>-<business>-<source>），重复 observation 仍会被台账拒绝。
    """
    drill = _find(state["drills"], drill_id, "演练")
    if drill["status"] not in (BOOKED, PENDING):
        raise BookingError(f"演练 {drill_id} 状态为 {drill['status']}，不可回填")

    stored_steps: list[dict] = []
    for index, step in enumerate(actual_steps):
        for field_name in ("business", "config", "resource", "source", "result", "start", "end"):
            if field_name not in step:
                raise BookingError(f"回填第 {index} 行缺少字段 {field_name}")
        if step["business"] not in inputs.businesses:
            raise BookingError(f"回填引用未知业务：{step['business']}")
        if step["resource"] not in inputs.resources:
            raise BookingError(f"回填引用未知资源：{step['resource']}")
        obs_id = step.get("observation") or f"{drill_id}-{step['business']}-{step['source']}"
        row = {
            "observation": obs_id,
            "drill": drill_id,
            "business": step["business"],
            "config": step["config"],
            "resource": step["resource"],
            "source": step["source"],
            "result": step["result"],
            "start": step["start"],
            "end": step["end"],
            "borrowed": bool(step.get("borrowed", False)),
        }
        stored = ledger.add_observation(row, imported_from=imported_from)
        stored_steps.append(stored.to_dict())

    drill["actual_steps"] = stored_steps
    drill["verification"] = verify_drill(inputs, drill, ledger)
    drill["status"] = EXECUTED
    # 关联的筹备安排同步进入已执行状态：保留当时安排与差异，此后不可取消重写
    if drill.get("arrangement_id"):
        for arrangement in state["arrangements"]:
            if arrangement["id"] == drill["arrangement_id"]:
                arrangement["status"] = EXECUTED
                arrangement["executed_drill"] = drill["id"]
    return drill


def verify_drill(inputs: Inputs, drill: dict, ledger: EvidenceLedger) -> dict:
    """按原情景、原计划快照核对每条承诺是否被证据支持。"""
    plan_snapshot = drill["plan_snapshot"]
    planned_businesses = [s["business"] for s in plan_snapshot["steps"]]
    drill_obs = [o for o in ledger.for_drill(drill["id"])]
    results: list[dict] = []

    for biz_name in planned_businesses:
        biz = inputs.businesses[biz_name]
        planned_step = next(s for s in plan_snapshot["steps"] if s["business"] == biz_name)
        expected_config = drill["expected_configs"].get(biz_name, biz.config)
        related = [o for o in drill_obs if o.business == biz_name]
        verdict, detail, capability = _classify_step(
            biz, planned_step, expected_config, related, ledger,
            drill["window_start"],
        )
        results.append(
            {
                "business": biz_name,
                "verdict": verdict,
                "detail": detail,
                "planned_resource": planned_step["terminal"],
                "planned_config": expected_config,
                "observed_resources": sorted({o.resource for o in related}),
                "borrowed_used": any(o.borrowed for o in related),
                "observations": [o.observation for o in related],
                "capability_status": capability.status,
            }
        )

    overall = _overall_verdict(results)
    return {
        "verified_at": iso(now_utc()),
        "overall": overall,
        "steps": results,
        "disputed": [r for r in results if r["verdict"] in (DISPUTED_VERDICT, CONFIG_INAPPLICABLE)],
        "failed": [r for r in results if r["verdict"] in (PROCESS_FAILURE, RESOURCE_INSUFFICIENT, MISSING_EVIDENCE)],
    }


def _classify_step(
    biz,
    planned_step: dict,
    expected_config: str,
    related: list[StoredObservation],
    ledger: EvidenceLedger,
    drill_window_start: str,
) -> tuple[str, str, object]:
    if not related:
        return (
            MISSING_EVIDENCE,
            f"业务 {biz.business} 在本次演练中没有任何观察记录",
            ledger.capability(biz.business, expected_config, planned_step["terminal"]),
        )

    # 计划版本/配置/终端相符的观察才进入能力判断
    config_match = [o for o in related if o.config == expected_config]
    config_mismatch = [o for o in related if o.config != expected_config]
    terminal_match = [o for o in config_match if o.resource == planned_step["terminal"]]
    borrowed = [o for o in terminal_match if o.borrowed]
    borrowed_foreign = [o for o in related if o.borrowed and o.resource != planned_step["terminal"]]

    if not config_match:
        return (
            CONFIG_INAPPLICABLE,
            (
                f"观察配置 {sorted({o.config for o in related})} 与计划要求 {expected_config} 全部不符，"
                "该演练不支持本配置的能力判断"
            ),
            ledger.capability(biz.business, expected_config, planned_step["terminal"]),
        )

    if not terminal_match and borrowed_foreign:
        return (
            CONFIG_INAPPLICABLE,
            (
                f"仅使用临时借调设备 {sorted({o.resource for o in borrowed_foreign})} 取得结果，"
                f"不能证明原配置在 {planned_step['terminal']} 上具备该能力"
            ),
            ledger.capability(biz.business, expected_config, planned_step["terminal"]),
        )

    # 同窗口 machine/observer 冲突
    conflict = EvidenceLedger._conflicts(terminal_match or config_match)
    if conflict:
        cap = ledger.capability(biz.business, expected_config, planned_step["terminal"])
        return (
            DISPUTED_VERDICT,
            "机器日志与观察员记录冲突：" + "；".join(
                f"{c['drill']} machine={c['machine_result']} observer={c['observer_result']}"
                for c in conflict
            ),
            cap,
        )

    evaluated = terminal_match or config_match
    results = {o.result for o in evaluated if not o.borrowed}
    if not results:
        return (
            CONFIG_INAPPLICABLE,
            "相符终端上的记录全部来自借调标注，不能证明原配置能力",
            ledger.capability(biz.business, expected_config, planned_step["terminal"]),
        )

    # 实测时间是否满足原情景承诺（deadline 是开始期限）
    timing_ok = all(
        _within_commitment(o, biz, drill_window_start) for o in evaluated if o.result == "success"
    )
    if results == {"fail"} or (results == {"success", "fail"}):
        # 区分资源不够与流程失败：失败观察集中在共享资源/终端容量问题时判资源不够，
        # 否则按流程执行失败处理；这里以失败行本身的资源与计划资源对比作为依据
        if any(o.resource == planned_step["terminal"] and o.result == "fail" for o in evaluated):
            verdict = RESOURCE_INSUFFICIENT
            detail = (
                f"计划资源 {planned_step['terminal']} 实测失败，"
                "属于资源/能力不足；引用该资源的未执行安排需重新评估"
            )
        else:
            verdict = PROCESS_FAILURE
            detail = "实测出现失败且资源与计划相符，按流程执行失败处理"
        return verdict, detail, ledger.capability(
            biz.business, expected_config, planned_step["terminal"]
        )

    if not timing_ok:
        return (
            PROCESS_FAILURE,
            "业务虽成功但实测开始晚于原情景最晚开始时间，属于流程执行失败",
            ledger.capability(biz.business, expected_config, planned_step["terminal"]),
        )

    return (
        SUPPORTED,
        f"计划版本、配置 {expected_config}、终端 {planned_step['terminal']} 与实测步骤相符且成功",
        ledger.capability(biz.business, expected_config, planned_step["terminal"]),
    )


def _within_commitment(obs: StoredObservation, biz, drill_window_start: str) -> bool:
    """回填实测时间与原情景分钟刻度核对：开始时刻不晚于 deadline。"""
    # 演练时间窗起点对应原情景 t=0
    window_start = parse_iso(drill_window_start)
    elapsed = (parse_iso(obs.start) - window_start).total_seconds() / 60
    return elapsed <= biz.deadline_minutes + 1e-9


def _overall_verdict(results: list[dict]) -> str:
    verdicts = {r["verdict"] for r in results}
    if not results:
        return MISSING_EVIDENCE
    if verdicts == {SUPPORTED}:
        return SUPPORTED
    if DISPUTED_VERDICT in verdicts:
        return DISPUTED_VERDICT
    if CONFIG_INAPPLICABLE in verdicts and not (
        verdicts & {PROCESS_FAILURE, RESOURCE_INSUFFICIENT}
    ):
        return CONFIG_INAPPLICABLE
    if RESOURCE_INSUFFICIENT in verdicts:
        return RESOURCE_INSUFFICIENT
    if MISSING_EVIDENCE in verdicts:
        return MISSING_EVIDENCE
    return PROCESS_FAILURE


# ------------------------------------------------------------------ 重评估

def reevaluate_pending_arrangements(
    inputs: Inputs, state: dict, ledger: EvidenceLedger
) -> list[dict]:
    """证据显示某共享设备能力不足（failed/disputed）时，重评引用它的未执行安排。

    已执行演练及其安排保留当时记录与差异，不用修订后的证据重写；
    因此只处理仍处于 pending/booked 的安排。返回受影响的重评记录。
    """
    affected_records: list[dict] = []
    for arrangement in state["arrangements"]:
        if arrangement["status"] not in (PENDING, BOOKED):
            continue
        flags = []
        for step in arrangement["plan_snapshot"]["steps"]:
            biz = inputs.businesses[step["business"]]
            # 以该步骤实际选用的终端（业务×配置×终端）的能力状态为准
            cap = ledger.capability(biz.business, biz.config, step["terminal"])
            if cap.status in (FAILED, DISPUTED):
                flags.append(
                    {
                        "business": step["business"],
                        "resource": step["terminal"],
                        "capability_status": cap.status,
                        "conflicts": cap.conflicts,
                    }
                )
        if flags:
            arrangement["reevaluation"] = {
                "at": iso(now_utc()),
                "status": "needs_replanning",
                "flags": flags,
            }
            affected_records.append(
                {"arrangement_id": arrangement["id"], "flags": flags}
            )
    return affected_records


def supplement_targets(
    inputs: Inputs,
    state: dict,
    ledger: EvidenceLedger,
    scenario_name: str,
    plan: Plan,
) -> dict:
    """确定补演目标：尚不能支撑的承诺与真实争议；同时给出现有占用约束。"""
    targets: list[dict] = []
    for step in plan.steps:
        biz = inputs.businesses[step.business]
        cap = ledger.capability(biz.business, biz.config, step.terminal)
        if cap.status != CONFIRMED:
            targets.append(
                {
                    "business": step.business,
                    "required_resource": step.terminal,
                    "required_config": biz.config,
                    "current_status": cap.status,
                    "reason": _supplement_reason(cap),
                    "observations": cap.observations,
                    "conflicts": cap.conflicts,
                }
            )
    unmet_targets = [
        {
            "business": u.business,
            "current_status": UNCONFIRMED,
            "reason": f"计划内无法满足（{u.reason}），需先解决资源/预置问题再安排补演",
        }
        for u in plan.unmet
    ]
    # 已确认的筹备占用（含已执行演练的保留占用）都必须遵守，只有 cancelled 释放
    holders = [
        ("arrangement", a) for a in active_arrangements(state)
    ] + [
        ("drill", d) for d in active_drills(state)
    ]
    blocking = [
        {
            "holder_type": kind,
            "holder_id": holder["id"],
            "resources": holder["resources"],
            "window_start": holder["window_start"],
            "window_end": holder["window_end"],
        }
        for kind, holder in holders
    ]
    return {
        "scenario": scenario_name,
        "supplement_targets": targets,
        "unresolvable_in_plan": unmet_targets,
        "existing_occupancy_to_respect": blocking,
        "rule": "补演不得重复确认已占用的终端/操作员，须另选时间窗或等待取消释放",
    }


def _supplement_reason(cap) -> str:
    if cap.status == DISPUTED:
        return "机器日志与观察员存在真实争议，需补演澄清"
    if cap.status == FAILED:
        return "现有证据为失败，需整改后补演"
    return "无相符证据，能力仅为待确认假设"


def _find(rows: list[dict], row_id: str, label: str) -> dict:
    for row in rows:
        if row["id"] == row_id:
            return row
    raise BookingError(f"{label}不存在：{row_id}")
