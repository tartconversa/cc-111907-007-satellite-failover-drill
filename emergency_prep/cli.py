"""命令行入口：离线业务分析与计划登记。

工作目录（--workdir / 环境变量 EMERGENCY_PREP_WORKDIR）保存全部状态，
重启后继续筹备；输入目录只读。
"""

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .analysis import analyze_scenario
from .evidence import (
    CONFIRMED,
    DISPUTED,
    FAILED,
    UNCONFIRMED,
    EvidenceLedger,
)
from .inputs import InputError, load_inputs
from .preparation import (
    BookingError,
    backfill_drill,
    cancel_arrangement,
    check_resource_conflicts,
    create_arrangement,
    register_drill,
    reevaluate_pending_arrangements,
    supplement_targets,
)
from .scheduler import SchedulingError, rank_plans, enumerate_plans
from .store import (
    StoreError,
    init_workspace,
    next_id,
    read_state,
    write_state,
)
from .timeutil import iso, now_utc
from .trace import add_action_item, close_action_item, trace_commitment

STATUS_LABELS = {
    CONFIRMED: "已确认",
    DISPUTED: "有争议",
    FAILED: "已证伪",
    UNCONFIRMED: "待确认假设",
}

REASON_LABELS = {
    "dependency_unmet": "前置依赖失守",
    "deadline_miss": "超过最晚开始时间",
    "outage_window_exceeded": "超出中断窗口",
    "no_compatible_resource": "无兼容资源",
    "unreachable": "站点不可达",
    "channel_contention": "通道容量争用",
    "capacity_contention": "资源容量争用",
}

VERDICT_LABELS = {
    "supported": "证据支持",
    "process_failure": "流程执行失败",
    "resource_insufficient": "资源不够",
    "config_inapplicable": "配置不适用",
    "missing_evidence": "缺少证据",
    "disputed": "证据争议",
}


class CliError(RuntimeError):
    pass


# --------------------------------------------------------------------- 辅助

def _workdir(args: argparse.Namespace) -> Path:
    workdir = args.workdir or Path.cwd() / "work"
    return Path(workdir)


def _load(workdir: Path):
    state = read_state(workdir)
    inputs = load_inputs(state["input_dir"])
    ledger = EvidenceLedger.from_dict(state["observations"])
    return state, inputs, ledger


def _save(workdir: Path, state: dict, ledger: EvidenceLedger) -> None:
    state["observations"] = ledger.to_dict()
    write_state(workdir, state)


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _ok(message: str) -> None:
    print(f"✓ {message}")


# --------------------------------------------------------------------- 命令

def cmd_init(args: argparse.Namespace) -> int:
    workdir = init_workspace(_workdir(args), args.inputs, force=args.force)
    _ok(f"工作目录已初始化：{workdir}")
    print("  下一步：python -m emergency_prep.cli import-evidence")
    return 0


def cmd_scenarios(args: argparse.Namespace) -> int:
    state, inputs, _ = _load(_workdir(args))
    if args.json:
        _print_json(
            [
                {
                    "scenario": s.scenario,
                    "start": s.start,
                    "outage_sites": list(s.outage_sites),
                    "duration_minutes": s.duration_minutes,
                    "affected_businesses": sorted(
                        b.business for b in inputs.businesses.values()
                        if b.site in s.outage_sites
                    ),
                }
                for s in inputs.scenarios.values()
            ]
        )
        return 0
    print("可用多站点中断情景：")
    for s in inputs.scenarios.values():
        biz = sorted(
            b.business for b in inputs.businesses.values() if b.site in s.outage_sites
        )
        print(
            f"  - {s.scenario}：站点 {','.join(s.outage_sites)} 同时中断，"
            f"窗口 {s.duration_minutes} 分钟，涉及业务 {', '.join(biz)}"
        )
    return 0


def cmd_import_evidence(args: argparse.Namespace) -> int:
    workdir = _workdir(args)
    state, inputs, ledger = _load(workdir)
    source = Path(args.file).resolve() if args.file else Path(state["input_dir"]) / "evidence.json"
    if args.file:
        # 导入同契约的新证据文件（独立目录，路径参数传入，不依赖工作目录）
        rows = json.loads(source.read_text(encoding="utf-8"))
        added = skipped = 0
        for row in rows:
            try:
                ledger.add_observation(row, imported_from=str(source), received=args.received)
                added += 1
            except ValueError as exc:
                if "已存在" in str(exc):
                    skipped += 1
                else:
                    raise
    else:
        added, skipped = ledger.add_from_inputs(
            inputs, imported_from="inputs/evidence.json", received=args.received
        )
    state["imports"].append(
        {
            "at": iso(now_utc()),
            "source": str(source),
            "added": added,
            "duplicates_skipped": skipped,
        }
    )
    _save(workdir, state, ledger)
    _ok(f"导入观察 {added} 条，重复投递跳过 {skipped} 条（补交同次演练不产生新独立证据）")
    return 0


def cmd_evidence(args: argparse.Namespace) -> int:
    _, _, ledger = _load(_workdir(args))
    if args.json:
        _print_json(
            {
                "observations": ledger.to_dict(),
                "capabilities": [c.to_dict() for c in ledger.capability_matrix()],
                "disputes": ledger.disputed_observations(),
            }
        )
        return 0
    print("证据台账：")
    for obs in ledger.all():
        flag = "（借调设备）" if obs.borrowed else ""
        print(
            f"  [{obs.observation}] 演练 {obs.drill} 业务 {obs.business} "
            f"配置 {obs.config} 终端 {obs.resource} {obs.source}={obs.result}{flag}"
        )
    print("\n能力判断（业务 × 配置 × 终端）：")
    for cap in ledger.capability_matrix():
        print(
            f"  - {cap.business} / {cap.config} / {cap.resource}：{STATUS_LABELS[cap.status]}"
            + (f"，支撑演练 {','.join(cap.supporting_drills)}" if cap.supporting_drills else "")
            + ("（仅借调证据，不证明原配置）" if cap.borrowed_only else "")
        )
    disputes = ledger.disputed_observations()
    if disputes:
        print("\n争议明细（机器日志与观察员冲突，不得当作已证实能力）：")
        for item in disputes:
            for conflict in item["conflicts"]:
                print(
                    f"  ! {item['business']}/{item['config']}/{item['resource']} "
                    f"演练 {conflict['drill']}：machine={conflict['machine_result']} "
                    f"observer={conflict['observer_result']}"
                )
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    _, inputs, ledger = _load(_workdir(args))
    report = analyze_scenario(inputs, ledger, args.scenario)
    if args.json:
        _print_json(report.to_dict())
        return 0
    _render_report(report)
    return 0


def _render_report(report) -> None:
    print(f"情景 {report.scenario}：站点 {','.join(report.outage_sites)} 同时中断")
    print(
        f"枚举 {report.variant_count} 个确定性方案（设备预置 × 业务次序 × 操作员裁决），"
        f"其中 {report.valid_count} 个通过独立校验\n"
    )
    best = report.best_plan
    print("推荐方案（不重复使用资源、不违反依赖）：")
    print(f"  预置：{best.preposition}")
    print(f"  次序策略：{best.ordering}，资源裁决：{best.tie_policy}")
    for step in best.steps:
        moves = (
            "；转移 " + "，".join(
                f"{m.resource}:{m.from_site}→{m.to_site}({m.arrival_minute - m.start_minute}分钟)"
                for m in step.moves
            )
            if step.moves else ""
        )
        print(
            f"  t={step.start_minute:>3}-{step.end_minute:<3} 业务 {step.business} "
            f"@ {step.site}（终端 {step.terminal} / 操作员 {step.operator} / 通道 {step.channel}，"
            f"最晚开始 {step.deadline_minute}）{moves}"
        )
    print("\n承诺核对：")
    for c in report.commitments:
        if c.scheduled:
            cap = STATUS_LABELS[c.capability.status]
            assumption = "【仅为待确认假设】" if c.assumption else ""
            print(
                f"  ✓ {c.business}（优先级{c.priority}）可满足，开始 t={c.planned_start_minute}，"
                f"证据：{cap}{assumption}"
            )
        else:
            print(
                f"  ✗ {c.business}（优先级{c.priority}）无法满足："
                f"{REASON_LABELS.get(c.unmet_reason, c.unmet_reason)} — {c.unmet_detail}"
            )
    if report.critical_gaps:
        print("\n关键业务缺口（不提供表面全覆盖清单）：")
        for gap in report.critical_gaps:
            print(f"  ! {gap['conclusion']}")
            print(f"    各方案失守原因：{', '.join(gap['reasons_across_variants'])}")
    if report.bottlenecks:
        print("\n共享瓶颈：")
        for b in report.bottlenecks:
            print(
                f"  ! {b['resource']}（{b['type']}，容量 {b['capacity']}）："
                f"峰值并发 {b['peak_concurrent']}，阻断业务 {b['blocked_business_count']} 个"
            )
    print("\n方案对比（前 5 个）：")
    for i, cmp in enumerate(report.comparisons[:5], 1):
        print(
            f"  {i}. 预置 {cmp['preposition']} | {cmp['ordering']}/{cmp['tie_policy']} | "
            f"满足 {','.join(cmp['met']) or '无'} | 失守 {','.join(cmp['unmet']) or '无'} | "
            f"转移合计 {cmp['total_move_minutes']} 分钟"
        )


def cmd_compare(args: argparse.Namespace) -> int:
    _, inputs, _ = _load(_workdir(args))
    scenario, plans = enumerate_plans(inputs, args.scenario)
    ranked = rank_plans(inputs, plans)
    rows = [
        {
            "preposition": p.preposition,
            "ordering": p.ordering,
            "tie_policy": p.tie_policy,
            "valid": p.valid,
            "validation_errors": p.validation_errors,
            "met": p.met_businesses,
            "unmet": [u.to_dict() for u in p.unmet],
        }
        for p in ranked
    ]
    if args.json:
        _print_json(rows)
        return 0
    print(f"情景 {args.scenario} 全部 {len(rows)} 个方案：")
    for i, row in enumerate(rows, 1):
        mark = "可行" if row["valid"] else "违规"
        print(
            f"  {i:>2}. [{mark}] {row['preposition']} {row['ordering']}/{row['tie_policy']} "
            f"满足={','.join(row['met']) or '无'} 失守={','.join(u['business'] for u in row['unmet']) or '无'}"
        )
        if not row["valid"]:
            for err in row["validation_errors"]:
                print(f"       └ {err}")
    return 0


def _select_plan(inputs, scenario_name: str, plan_index: int):
    scenario, plans = enumerate_plans(inputs, scenario_name)
    ranked = rank_plans(inputs, plans)
    if plan_index < 0 or plan_index >= len(ranked):
        raise CliError(f"方案序号超出范围 0..{len(ranked) - 1}")
    plan = ranked[plan_index]
    if not plan.valid:
        raise CliError("所选方案未通过校验，不能登记：" + "；".join(plan.validation_errors))
    return plan, ranked


def cmd_book(args: argparse.Namespace) -> int:
    workdir = _workdir(args)
    state, inputs, ledger = _load(workdir)
    plan, ranked = _select_plan(inputs, args.scenario, args.plan_index)
    resources = sorted(
        {s.terminal for s in plan.steps}
        | {s.operator for s in plan.steps}
        | {s.channel for s in plan.steps}
    )
    conflicts = check_resource_conflicts(
        inputs, state, resources, args.window_start, args.window_end
    )
    if conflicts and not args.force:
        if args.json:
            _print_json({"accepted": False, "conflicts": conflicts})
        else:
            print("登记被拒绝，资源占用冲突：")
            for c in conflicts:
                print(f"  ! {c['reason']}")
            print("请另选时间窗，或取消原安排后重试。")
        return 2
    arrangement_id = next_id(state, "arrangement", "AR")
    arrangement = create_arrangement(
        state,
        arrangement_id=arrangement_id,
        title=args.title,
        scenario=args.scenario,
        plan=plan,
        resources=resources,
        window_start=args.window_start,
        window_end=args.window_end,
        note=args.note or "",
    )
    _save(workdir, state, ledger)
    if args.json:
        _print_json({"accepted": True, "arrangement": arrangement})
    else:
        _ok(f"筹备安排 {arrangement_id} 已确认占用：{', '.join(resources)}")
    return 0


def cmd_register_drill(args: argparse.Namespace) -> int:
    workdir = _workdir(args)
    state, inputs, ledger = _load(workdir)
    plan, _ = _select_plan(inputs, args.scenario, args.plan_index)
    resources = sorted(
        {s.terminal for s in plan.steps}
        | {s.operator for s in plan.steps}
        | {s.channel for s in plan.steps}
    )
    conflicts = check_resource_conflicts(
        inputs, state, resources, args.window_start, args.window_end
    )
    if conflicts and not args.force:
        if args.json:
            _print_json({"accepted": False, "conflicts": conflicts})
        else:
            print("演练登记被拒绝，资源占用冲突：")
            for c in conflicts:
                print(f"  ! {c['reason']}")
        return 2
    expected_configs = {
        s.business: inputs.businesses[s.business].config for s in plan.steps
    }
    drill_id = next_id(state, "drill", "DR")
    drill = register_drill(
        state,
        drill_id=drill_id,
        title=args.title,
        scenario=args.scenario,
        plan=plan,
        resources=resources,
        window_start=args.window_start,
        window_end=args.window_end,
        arrangement_id=args.arrangement,
        expected_configs=expected_configs,
        note=args.note or "",
    )
    _save(workdir, state, ledger)
    if args.json:
        _print_json({"accepted": True, "drill": drill})
    else:
        _ok(f"演练 {drill_id} 已登记并锁定资源：{', '.join(resources)}")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    workdir = _workdir(args)
    state, inputs, ledger = _load(workdir)
    arrangement = cancel_arrangement(state, args.arrangement_id, args.reason)
    _save(workdir, state, ledger)
    _ok(f"安排 {args.arrangement_id} 已取消，资源占用已释放")
    if args.json:
        _print_json(arrangement)
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    workdir = _workdir(args)
    state, inputs, ledger = _load(workdir)
    steps_file = Path(args.steps_file).resolve()
    actual_steps = json.loads(steps_file.read_text(encoding="utf-8"))
    if not isinstance(actual_steps, list):
        raise CliError("回填文件顶层必须是步骤数组")
    drill = backfill_drill(
        inputs, state, ledger, args.drill_id, actual_steps,
        imported_from=str(steps_file),
    )
    # 回填后自动重评引用同一共享设备的未执行安排
    reeval = reevaluate_pending_arrangements(inputs, state, ledger)
    _save(workdir, state, ledger)
    if args.json:
        _print_json({"drill": drill, "reevaluated": reeval})
        return 0
    verification = drill["verification"]
    print(f"演练 {args.drill_id} 回填完成，总体结论：{VERDICT_LABELS[verification['overall']]}")
    for step in verification["steps"]:
        print(
            f"  - {step['business']}：{VERDICT_LABELS[step['verdict']]} — {step['detail']}"
        )
    if reeval:
        print("\n以下未执行安排因共享设备证据不足需重新评估：")
        for item in reeval:
            print(f"  ! 安排 {item['arrangement_id']}：{item['flags']}")
    return 0


def cmd_reevaluate(args: argparse.Namespace) -> int:
    workdir = _workdir(args)
    state, inputs, ledger = _load(workdir)
    reeval = reevaluate_pending_arrangements(inputs, state, ledger)
    _save(workdir, state, ledger)
    if args.json:
        _print_json(reeval)
        return 0
    if not reeval:
        print("没有需要重评的未执行安排。")
    for item in reeval:
        print(f"! 安排 {item['arrangement_id']} 需重新规划：")
        for flag in item["flags"]:
            print(
                f"    业务 {flag['business']} 资源 {flag['resource']} "
                f"能力状态 {STATUS_LABELS.get(flag['capability_status'], flag['capability_status'])}"
            )
    return 0


def cmd_supplement(args: argparse.Namespace) -> int:
    workdir = _workdir(args)
    state, inputs, ledger = _load(workdir)
    plan, _ = _select_plan(inputs, args.scenario, args.plan_index)
    result = supplement_targets(inputs, state, ledger, args.scenario, plan)
    if args.json:
        _print_json(result)
        return 0
    print(f"情景 {args.scenario} 的补演目标：")
    if not result["supplement_targets"] and not result["unresolvable_in_plan"]:
        print("  全部承诺已有确认证据，无需补演。")
    for t in result["supplement_targets"]:
        print(
            f"  - {t['business']}：需 {t['required_config']} 配置的 {t['required_resource']}，"
            f"当前 {STATUS_LABELS.get(t['current_status'], t['current_status'])}；{t['reason']}"
        )
    for t in result["unresolvable_in_plan"]:
        print(f"  ✗ {t['business']}：{t['reason']}")
    print("\n必须遵守的既有占用：")
    for occ in result["existing_occupancy_to_respect"]:
        kind = "演练" if occ["holder_type"] == "drill" else "安排"
        print(
            f"  ! {kind} {occ['holder_id']} 占用 {','.join(occ['resources'])} "
            f"{occ['window_start']} ~ {occ['window_end']}"
        )
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    workdir = _workdir(args)
    state, inputs, ledger = _load(workdir)
    result = trace_commitment(inputs, state, ledger, args.business)
    if args.json:
        _print_json(result)
        return 0
    print(f"承诺 {result['business']}（{result['site']}，优先级 {result['priority']}，配置 {result['config']}）")
    print(f"  最晚开始 {result['deadline_minutes']} 分钟，持续 {result['duration_minutes']} 分钟，依赖 {result['depends'] or '无'}")
    print(f"  适用情景：{', '.join(result['scenarios'])}")
    print("  资源占用：")
    for occ in result["resource_occupancy"] or [{}]:
        if not occ:
            print("    （无）")
            break
        print(
            f"    - {occ['holder_type']} {occ['holder_id']}（{occ['status']}）"
            f"情景 {occ['scenario']} {occ['window_start']}~{occ['window_end']} "
            f"资源 {occ['planned_resources']}"
        )
    print("  支撑证据：")
    for obs in result["supporting_evidence"]["observations"] or [{}]:
        if not obs:
            print("    （无观察记录）")
            break
        print(
            f"    - [{obs['observation']}] {obs['drill']} {obs['config']}/{obs['resource']} "
            f"{obs['source']}={obs['result']}" + ("（借调）" if obs["borrowed"] else "")
        )
    for cap in result["supporting_evidence"]["capabilities"]:
        print(f"    能力 {cap['config']}/{cap['resource']}：{STATUS_LABELS[cap['status']]}")
    print("  现场结果：")
    for fr in result["field_results"] or [{}]:
        if not fr:
            print("    （尚无已执行演练）")
            break
        print(f"    - 演练 {fr['drill']}：{VERDICT_LABELS[fr['verdict']]} — {fr['detail']}")
    print("  待办动作：")
    for action in result["pending_actions"] or [{}]:
        if not action:
            print("    （无）")
            break
        print(f"    - [{action['id']}] {action['title']}（{action['status']}）：{action['reason']}")
    return 0


def cmd_action_add(args: argparse.Namespace) -> int:
    workdir = _workdir(args)
    state, inputs, ledger = _load(workdir)
    for name in args.businesses:
        if name not in inputs.businesses:
            raise CliError(f"未知业务：{name}")
    action_id = next_id(state, "action", "AC")
    item = add_action_item(
        state, action_id, args.title, args.businesses, args.reason, args.due
    )
    _save(workdir, state, ledger)
    _ok(f"待办动作 {action_id} 已登记")
    if args.json:
        _print_json(item)
    return 0


def cmd_action_close(args: argparse.Namespace) -> int:
    workdir = _workdir(args)
    state, inputs, ledger = _load(workdir)
    item = close_action_item(state, args.action_id)
    _save(workdir, state, ledger)
    _ok(f"待办动作 {args.action_id} 已关闭")
    if args.json:
        _print_json(item)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    workdir = _workdir(args)
    state, inputs, ledger = _load(workdir)
    summary = {
        "workdir": str(workdir),
        "input_dir": state["input_dir"],
        "created_at": state["created_at"],
        "updated_at": state["updated_at"],
        "observations": len(ledger.all()),
        "capabilities": [c.to_dict() for c in ledger.capability_matrix()],
        "arrangements": [
            {
                "id": a["id"],
                "title": a["title"],
                "scenario": a["scenario"],
                "status": a["status"],
                "resources": a["resources"],
                "window": [a["window_start"], a["window_end"]],
                "reevaluation": a.get("reevaluation"),
            }
            for a in state["arrangements"]
        ],
        "drills": [
            {
                "id": d["id"],
                "title": d["title"],
                "scenario": d["scenario"],
                "status": d["status"],
                "resources": d["resources"],
                "window": [d["window_start"], d["window_end"]],
                "verification_overall": d["verification"]["overall"] if d.get("verification") else None,
            }
            for d in state["drills"]
        ],
        "open_actions": [a for a in state["action_items"] if a["status"] == "open"],
    }
    if args.json:
        _print_json(summary)
        return 0
    print(f"工作目录：{workdir}（输入：{state['input_dir']}）")
    print(f"观察记录 {summary['observations']} 条；安排 {len(summary['arrangements'])} 条；"
          f"演练 {len(summary['drills'])} 条；未关闭待办 {len(summary['open_actions'])} 条")
    for a in summary["arrangements"]:
        flag = "【需重评】" if a["reevaluation"] else ""
        print(f"  安排 {a['id']}（{a['status']}）{a['title']} {flag}")
    for d in summary["drills"]:
        verdict = VERDICT_LABELS.get(d["verification_overall"], "未执行") if d["verification_overall"] else "未执行"
        print(f"  演练 {d['id']}（{d['status']}）{d['title']}：{verdict}")
    return 0


# --------------------------------------------------------------------- 装配

def build_parser() -> argparse.ArgumentParser:
    # 父解析器：全局参数在子命令前后均可出现。
    # default=SUPPRESS 使子解析器在未收到该参数时不覆盖顶层已解析的值。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workdir", default=argparse.SUPPRESS,
                        help="工作目录（默认 ./work，也可用 EMERGENCY_PREP_WORKDIR）")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="以 JSON 输出，便于脚本集成")

    parser = argparse.ArgumentParser(
        prog="emergency_prep",
        description="卫星备援应急业务覆盖分析与演练筹备登记（离线，仅标准库）",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("init", parents=[common], help="初始化工作目录")
    p.add_argument("--inputs", required=True, help="只读源资料目录（含 sites.json 等）")
    p.add_argument("--force", action="store_true", help="覆盖已存在的工作目录")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("scenarios", parents=[common], help="列出多站点中断情景")
    p.set_defaults(func=cmd_scenarios)

    p = sub.add_parser("import-evidence", parents=[common], help="从源资料或同契约文件导入演练证据")
    p.add_argument("--file", help="同契约的新证据 JSON 文件；省略则导入 inputs/evidence.json")
    p.add_argument("--received", help="事实进入系统时点（ISO UTC），默认当前时刻")
    p.set_defaults(func=cmd_import_evidence)

    p = sub.add_parser("evidence", parents=[common], help="查看证据台账、能力判断与争议")
    p.set_defaults(func=cmd_evidence)

    p = sub.add_parser("analyze", parents=[common], help="分析一个多站点中断情景的业务覆盖")
    p.add_argument("scenario", help="情景名称（如 dual/all）")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("compare", parents=[common], help="列出情景的全部方案对比")
    p.add_argument("scenario")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("book", parents=[common], help="确认一条筹备安排并占用资源与时间窗")
    p.add_argument("scenario")
    p.add_argument("--plan-index", type=int, default=0, help="compare 中的方案序号（默认 0=最优）")
    p.add_argument("--title", required=True)
    p.add_argument("--window-start", required=True, help="ISO UTC，如 2026-11-25T08:00:00Z")
    p.add_argument("--window-end", required=True)
    p.add_argument("--note", default="")
    p.add_argument("--force", action="store_true", help="冲突时仍强制（不建议，会破坏排他规则）")
    p.set_defaults(func=cmd_book)

    p = sub.add_parser("register-drill", parents=[common], help="登记一组演练并锁定资源")
    p.add_argument("scenario")
    p.add_argument("--plan-index", type=int, default=0)
    p.add_argument("--title", required=True)
    p.add_argument("--window-start", required=True)
    p.add_argument("--window-end", required=True)
    p.add_argument("--arrangement", help="关联的筹备安排 ID")
    p.add_argument("--note", default="")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_register_drill)

    p = sub.add_parser("cancel", parents=[common], help="取消安排并释放资源（已执行演练不可取消）")
    p.add_argument("arrangement_id")
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("backfill", parents=[common], help="回填演练实际步骤并按原情景核验")
    p.add_argument("drill_id")
    p.add_argument("steps_file", help="实际步骤 JSON 文件路径")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("reevaluate", parents=[common], help="按最新证据重评未执行安排")
    p.set_defaults(func=cmd_reevaluate)

    p = sub.add_parser("supplement", parents=[common], help="列出补演目标与必须遵守的既有占用")
    p.add_argument("scenario")
    p.add_argument("--plan-index", type=int, default=0)
    p.set_defaults(func=cmd_supplement)

    p = sub.add_parser("trace", parents=[common], help="从一条应急承诺追溯全貌")
    p.add_argument("business")
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("action-add", parents=[common], help="登记待落实动作")
    p.add_argument("--title", required=True)
    p.add_argument("--businesses", nargs="+", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--due")
    p.set_defaults(func=cmd_action_add)

    p = sub.add_parser("action-close", parents=[common], help="关闭待落实动作")
    p.add_argument("action_id")
    p.set_defaults(func=cmd_action_close)

    p = sub.add_parser("status", parents=[common], help="工作目录总览（重启后可继续筹备）")
    p.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.func is None:
        parser.print_help()
        return 1
    # 环境变量兜底（SUPPRESS 默认下属性可能不存在）
    if not getattr(args, "workdir", None):
        import os
        args.workdir = os.environ.get("EMERGENCY_PREP_WORKDIR")
    if not hasattr(args, "json"):
        args.json = False
    try:
        return args.func(args)
    except (InputError, SchedulingError, StoreError, BookingError, CliError, KeyError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"错误：文件不存在：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
