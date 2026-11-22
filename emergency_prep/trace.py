"""从一条应急承诺反向追溯：情景、资源占用、支撑证据、现场结果、待办动作。"""

from .evidence import CONFIRMED, EvidenceLedger
from .inputs import Inputs
from .preparation import active_arrangements, active_drills
from .timeutil import iso, now_utc


def trace_commitment(
    inputs: Inputs,
    state: dict,
    ledger: EvidenceLedger,
    business_name: str,
) -> dict:
    biz = inputs.businesses.get(business_name)
    if biz is None:
        raise KeyError(f"未知业务承诺：{business_name}")

    # 该业务出现在哪些情景
    scenarios = [
        s.scenario
        for s in inputs.scenarios.values()
        if biz.site in s.outage_sites
    ]

    # 资源占用：所有引用该业务步骤的安排与演练
    occupancies = []
    for arrangement in active_arrangements(state):
        for step in arrangement["plan_snapshot"].get("steps", []):
            if step["business"] == business_name:
                occupancies.append(
                    {
                        "holder_type": "arrangement",
                        "holder_id": arrangement["id"],
                        "title": arrangement["title"],
                        "status": arrangement["status"],
                        "scenario": arrangement["scenario"],
                        "window_start": arrangement["window_start"],
                        "window_end": arrangement["window_end"],
                        "resources": arrangement["resources"],
                        "planned_resources": {
                            "terminal": step["terminal"],
                            "operator": step["operator"],
                            "channel": step["channel"],
                        },
                        "planned_start_minute": step["start_minute"],
                    }
                )
    for drill in active_drills(state) + [
        d for d in state["drills"] if d["status"] not in ("booked", "pending", "executed")
    ]:
        for step in drill["plan_snapshot"].get("steps", []):
            if step["business"] == business_name:
                occupancies.append(
                    {
                        "holder_type": "drill",
                        "holder_id": drill["id"],
                        "title": drill["title"],
                        "status": drill["status"],
                        "scenario": drill["scenario"],
                        "window_start": drill["window_start"],
                        "window_end": drill["window_end"],
                        "resources": drill["resources"],
                        "planned_resources": {
                            "terminal": step["terminal"],
                            "operator": step["operator"],
                            "channel": step["channel"],
                        },
                        "planned_start_minute": step["start_minute"],
                    }
                )

    # 支撑证据：所有与该业务相关的观察，按（配置, 终端）归总能力状态
    observations = [o.to_dict() for o in ledger.all() if o.business == business_name]
    capability_rows = [
        cap.to_dict()
        for cap in ledger.capability_matrix()
        if cap.business == business_name
    ]
    # 现场结果：已执行演练中该业务的核验结论
    field_results = []
    for drill in state["drills"]:
        if drill.get("verification") is None:
            continue
        for step in drill["verification"].get("steps", []):
            if step["business"] == business_name:
                field_results.append(
                    {
                        "drill": drill["id"],
                        "verdict": step["verdict"],
                        "detail": step["detail"],
                        "planned_resource": step["planned_resource"],
                        "observed_resources": step["observed_resources"],
                        "borrowed_used": step["borrowed_used"],
                        "observations": step["observations"],
                    }
                )

    # 待办动作
    actions = [
        a.to_dict() if hasattr(a, "to_dict") else a
        for a in state.get("action_items", [])
        if business_name in a.get("businesses", [])
    ]

    has_confirmed = any(cap["status"] == CONFIRMED for cap in capability_rows)
    return {
        "business": business_name,
        "site": biz.site,
        "kind": biz.kind,
        "priority": biz.priority,
        "config": biz.config,
        "deadline_minutes": biz.deadline_minutes,
        "duration_minutes": biz.duration_minutes,
        "depends": list(biz.depends),
        "scenarios": scenarios,
        "resource_occupancy": occupancies,
        "supporting_evidence": {
            "observations": observations,
            "capabilities": capability_rows,
            "any_confirmed": has_confirmed,
        },
        "field_results": field_results,
        "pending_actions": actions,
    }


def add_action_item(
    state: dict,
    action_id: str,
    title: str,
    businesses: list[str],
    reason: str,
    due: str | None = None,
) -> dict:
    item = {
        "id": action_id,
        "title": title,
        "businesses": list(businesses),
        "reason": reason,
        "due": due,
        "status": "open",
        "created_at": iso(now_utc()),
        "closed_at": None,
    }
    state["action_items"].append(item)
    return item


def close_action_item(state: dict, action_id: str) -> dict:
    from .timeutil import iso, now_utc

    for item in state["action_items"]:
        if item["id"] == action_id:
            item["status"] = "closed"
            item["closed_at"] = iso(now_utc())
            return item
    raise KeyError(f"待办动作不存在：{action_id}")
