"""情景内的资源排程模拟与计划校验。

硬约束（任何被提交的计划都必须满足，违反即判定不可行）：
1. 一个业务同时占用一个兼容终端、一个合格操作员和一个通道槽；
   资源在同一分钟内的占用数不得超过 capacity（不重复计算）。
2. 终端与操作员必须物理位于业务站点；未列转移路径不可假设瞬移，
   转移沿 transfers 有向图取最短路，转移期间资源不可用。
3. 业务开始不得早于其全部依赖的完成时刻；开始时刻不得晚于
   deadline_minutes（deadline 是开始期限）；完成时刻不得超出中断窗口。
4. 资源技能必须兼容业务 kind；SAT 通道无须转移但占用容量。

本模块不追求数学最优，只枚举有限的确定性方案族并逐一校验，
保证提交的计划不重复使用资源、不违反依赖。
"""

import heapq
from dataclasses import dataclass, field

from .inputs import Business, Inputs, Resource, Scenario

# 方案枚举上限，防止组合爆炸；当前数据规模下预置组合 ×6 次序策略仍远低于此
MAX_VARIANTS = 768


class SchedulingError(ValueError):
    """排程输入本身非法（如引用未知情景）。"""


@dataclass
class Move:
    resource: str
    from_site: str
    to_site: str
    start_minute: int
    arrival_minute: int
    path: tuple[str, ...]


@dataclass
class Step:
    business: str
    site: str
    kind: str
    terminal: str
    operator: str
    channel: str
    start_minute: int
    end_minute: int
    deadline_minute: int
    deadline_met: bool
    moves: list[Move] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "business": self.business,
            "site": self.site,
            "kind": self.kind,
            "terminal": self.terminal,
            "operator": self.operator,
            "channel": self.channel,
            "start_minute": self.start_minute,
            "end_minute": self.end_minute,
            "deadline_minute": self.deadline_minute,
            "deadline_met": self.deadline_met,
            "moves": [
                {
                    "resource": m.resource,
                    "from_site": m.from_site,
                    "to_site": m.to_site,
                    "start_minute": m.start_minute,
                    "arrival_minute": m.arrival_minute,
                    "path": list(m.path),
                }
                for m in self.moves
            ],
        }


@dataclass
class Unmet:
    business: str
    reason: str
    detail: str
    earliest_feasible_minute: int | None = None
    blocking_resources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "business": self.business,
            "reason": self.reason,
            "detail": self.detail,
            "earliest_feasible_minute": self.earliest_feasible_minute,
            "blocking_resources": self.blocking_resources,
        }


@dataclass
class Plan:
    scenario: str
    preposition: dict[str, str]
    ordering: str
    tie_policy: str
    steps: list[Step]
    unmet: list[Unmet]
    validation_errors: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.validation_errors

    @property
    def met_businesses(self) -> list[str]:
        return [s.business for s in self.steps]

    def to_dict(self) -> dict:
        return {
            "scenario": self.scenario,
            "valid": self.valid,
            "validation_errors": self.validation_errors,
            "preposition": dict(self.preposition),
            "ordering": self.ordering,
            "tie_policy": self.tie_policy,
            "steps": [s.to_dict() for s in self.steps],
            "unmet": [u.to_dict() for u in self.unmet],
            "metrics": plan_metrics(self),
        }


# ---------------------------------------------------------------------------
# 转移图：有向最短路；不可达返回 None
# ---------------------------------------------------------------------------

def shortest_travel(
    inputs: Inputs, from_site: str, to_site: str
) -> tuple[int, tuple[str, ...]] | None:
    """返回（分钟数, 路径站点序列）；同站为 (0, (site,))，不可达为 None。"""
    if from_site == to_site:
        return 0, (from_site,)
    if from_site not in inputs.sites or to_site not in inputs.sites:
        return None
    dist: dict[str, int] = {from_site: 0}
    prev: dict[str, str] = {}
    queue: list[tuple[int, str]] = [(0, from_site)]
    while queue:
        d, site = heapq.heappop(queue)
        if d != dist[site]:
            continue
        if site == to_site:
            break
        for (a, b), minutes in inputs.transfer_minutes.items():
            if a != site:
                continue
            nd = d + minutes
            if nd < dist.get(b, 10**12):
                dist[b] = nd
                prev[b] = site
                heapq.heappush(queue, (nd, b))
    if to_site not in dist:
        return None
    path: list[str] = []
    cur = to_site
    while cur != from_site:
        path.append(cur)
        cur = prev[cur]
    path.append(from_site)
    path.reverse()
    return dist[to_site], tuple(path)


# ---------------------------------------------------------------------------
# 方案族：预置位置 × 业务次序 × 资源裁决策略
# ---------------------------------------------------------------------------

def preposition_variants(
    inputs: Inputs, scenario: Scenario
) -> list[dict[str, str]]:
    """枚举移动资源在中断前（t=0 之前）的预置位置。

    目标限定为初始站点或受影响站点；预置本身沿可达路径进行，
    不可达的组合直接剔除。SAT 不参与预置。
    """
    mobile = inputs.mobile_resources()
    targets_per: list[tuple[str, list[str]]] = []
    outage = set(scenario.outage_sites)
    for name, res in sorted(mobile.items()):
        choices = [res.initial_site] + sorted(outage - {res.initial_site})
        reachable = []
        for target in choices:
            travel = shortest_travel(inputs, res.initial_site, target)
            if travel is not None:
                reachable.append(target)
        if not reachable:
            raise SchedulingError(f"资源 {name} 从 {res.initial_site} 无任何可达预置点")
        targets_per.append((name, reachable))

    combos: list[dict[str, str]] = [{}]
    for name, choices in targets_per:
        combos = [
            {**combo, name: target}
            for combo in combos
            for target in choices
        ]
        if len(combos) > MAX_VARIANTS:
            # 保留前 MAX_VARIANTS 个确定性组合（排序稳定，可复现）
            combos = sorted(combos, key=lambda c: tuple(sorted(c.items())))[:MAX_VARIANTS]
    return combos


def topo_orders(inputs: Inputs, businesses: dict[str, Business]) -> dict[str, list[str]]:
    """三种确定性的依赖拓扑次序。"""
    orders: dict[str, list[str]] = {}
    key_fns = {
        "priority_first": lambda b: (b.priority, b.deadline_minutes, b.business),
        "deadline_first": lambda b: (b.deadline_minutes, b.priority, b.business),
        "site_then_priority": lambda b: (b.site, b.priority, b.deadline_minutes, b.business),
    }
    for label, key_fn in key_fns.items():
        remaining = dict(businesses)
        indegree = {
            name: sum(1 for d in biz.depends if d in businesses)
            for name, biz in businesses.items()
        }
        ordered: list[str] = []
        while remaining:
            ready = [n for n in remaining if indegree[n] == 0]
            if not ready:
                raise SchedulingError("业务依赖存在环，无法拓扑排序")
            pick = min(ready, key=lambda n: key_fn(businesses[n]))
            ordered.append(pick)
            for name, biz in remaining.items():
                if pick in biz.depends:
                    indegree[name] -= 1
            del remaining[pick]
        orders[label] = ordered
    return orders


# ---------------------------------------------------------------------------
# 单次模拟
# ---------------------------------------------------------------------------

@dataclass
class _LocState:
    site: str
    available_minute: int  # 移动完成、可再次使用的时刻


@dataclass
class _SimState:
    # 移动资源当前位置与可用时刻
    locations: dict[str, _LocState]
    # 资源 -> 已排占用区间（左闭右开，分钟）
    busy: dict[str, list[tuple[int, int]]]
    steps: list[Step]
    moves: dict[str, list[Move]]
    completed_at: dict[str, int]


def _earliest_free(busy: list[tuple[int, int]], not_before: int) -> int:
    """容量为 1 的资源：返回 >= not_before 的最早空闲时刻。"""
    t = not_before
    changed = True
    while changed:
        changed = False
        for start, end in busy:
            if start <= t < end:
                t = end
                changed = True
    return t


def _slot_available(
    busy: list[tuple[int, int]], capacity: int, start: int, duration: int
) -> bool:
    """容量为 capacity 的资源在 [start, start+duration) 是否始终有空槽。"""
    end = start + duration
    for minute in range(start, end):
        used = sum(1 for s, e in busy if s <= minute < e)
        if used >= capacity:
            return False
    return True


def _earliest_channel_slot(
    busy: list[tuple[int, int]], capacity: int, not_before: int, duration: int
) -> int | None:
    """通道资源：寻找 >= not_before 且整段有空槽的最早开始时刻。

    候选时刻只需在 not_before 与各占用区间的边界上试探。
    """
    candidates = {not_before}
    for start, end in busy:
        if end >= not_before:
            candidates.add(end)
        if start >= not_before:
            candidates.add(start)
    for cand in sorted(candidates):
        if _slot_available(busy, capacity, cand, duration):
            return cand
    return None


def _compatible(res: Resource, kind: str) -> bool:
    return kind in res.skills


def simulate(
    inputs: Inputs,
    scenario: Scenario,
    preposition: dict[str, str],
    order: list[str],
    order_label: str,
    tie_policy: str,
) -> Plan:
    """按给定预置、次序和裁决策略执行一次贪心模拟。"""
    affected = inputs.businesses_for_scenario(scenario)
    state = _SimState(
        locations={},
        busy={name: [] for name in inputs.resources},
        steps=[],
        moves={},
        completed_at={},
    )
    # 初始化移动资源位置：预置点必须从初始站点可达
    for name, res in inputs.mobile_resources().items():
        target = preposition.get(name, res.initial_site)
        travel = shortest_travel(inputs, res.initial_site, target)
        if travel is None:
            raise SchedulingError(
                f"预置非法：资源 {name} 无法从 {res.initial_site} 到达 {target}"
            )
        state.locations[name] = _LocState(site=target, available_minute=0)

    unmet: list[Unmet] = []
    failed: set[str] = set()

    for biz_name in order:
        biz = affected[biz_name]
        # 依赖：依赖业务不在情景范围内或已失败 => 依赖失守
        broken_dep = next(
            (d for d in biz.depends if d not in affected or d in failed),
            None,
        )
        if broken_dep is not None:
            failed.add(biz_name)
            unmet.append(
                Unmet(
                    business=biz_name,
                    reason="dependency_unmet",
                    detail=f"前置依赖 {broken_dep} 未能在本情景中完成",
                    blocking_resources=[broken_dep],
                )
            )
            continue

        dep_end = max(
            (state.completed_at[d] for d in biz.depends if d in affected),
            default=0,
        )
        chosen = _choose_resources(inputs, scenario, biz, dep_end, state, tie_policy)
        if chosen is None:
            failed.add(biz_name)
            unmet.append(_diagnose_no_candidate(inputs, biz, dep_end, state))
            continue

        terminal, operator, channel, start, step_moves = chosen
        end = start + biz.duration_minutes

        if start > biz.deadline_minutes:
            failed.add(biz_name)
            unmet.append(
                Unmet(
                    business=biz_name,
                    reason="deadline_miss",
                    detail=(
                        f"最早可行开始 {start} 分钟晚于最晚开始 {biz.deadline_minutes} 分钟"
                    ),
                    earliest_feasible_minute=start,
                    blocking_resources=[terminal, operator, channel],
                )
            )
            continue
        if end > scenario.duration_minutes:
            failed.add(biz_name)
            unmet.append(
                Unmet(
                    business=biz_name,
                    reason="outage_window_exceeded",
                    detail=(
                        f"完成时刻 {end} 分钟超出中断窗口 {scenario.duration_minutes} 分钟"
                    ),
                    earliest_feasible_minute=start,
                    blocking_resources=[channel],
                )
            )
            continue

        for move in step_moves:
            state.moves.setdefault(move.resource, []).append(move)
            state.locations[move.resource] = _LocState(
                site=move.to_site, available_minute=move.arrival_minute
            )
        step = Step(
            business=biz_name,
            site=biz.site,
            kind=biz.kind,
            terminal=terminal,
            operator=operator,
            channel=channel,
            start_minute=start,
            end_minute=end,
            deadline_minute=biz.deadline_minutes,
            deadline_met=True,
            moves=step_moves,
        )
        state.steps.append(step)
        state.completed_at[biz_name] = end
        for res_name in (terminal, operator, channel):
            state.busy[res_name].append((start, end))

    plan = Plan(
        scenario=scenario.scenario,
        preposition=dict(preposition),
        ordering=order_label,
        tie_policy=tie_policy,
        steps=state.steps,
        unmet=unmet,
    )
    plan.validation_errors = validate_plan(inputs, scenario, plan)
    return plan


def _choose_resources(
    inputs: Inputs,
    scenario: Scenario,
    biz: Business,
    dep_end: int,
    state: _SimState,
    tie_policy: str,
):
    """枚举兼容的终端/操作员组合，接入通道槽，返回最早可行方案。"""
    terminal_cands = []
    for name, res in inputs.terminals().items():
        if not _compatible(res, biz.kind):
            continue
        ready, moves = _resource_ready_at_site(inputs, name, res, biz.site, dep_end, state)
        if ready is None:
            continue
        terminal_cands.append((name, ready, moves))

    operator_cands = []
    for name, res in inputs.operators().items():
        if not _compatible(res, biz.kind):
            continue
        ready, moves = _resource_ready_at_site(inputs, name, res, biz.site, dep_end, state)
        if ready is None:
            continue
        operator_cands.append((name, ready, moves))

    if not terminal_cands or not operator_cands:
        return None

    options = []
    for t_name, t_ready, t_moves in terminal_cands:
        for o_name, o_ready, o_moves in operator_cands:
            if t_name == o_name:
                continue
            earliest = max(t_ready, o_ready, dep_end)
            channel_name, channel_res = _pick_channel(inputs, biz.kind)
            if channel_res is None:
                return None
            start = _earliest_channel_slot(
                state.busy[channel_name],
                channel_res.capacity,
                earliest,
                biz.duration_minutes,
            )
            if start is None:
                continue
            move_count = len(t_moves) + len(o_moves)
            options.append(
                {
                    "terminal": t_name,
                    "operator": o_name,
                    "channel": channel_name,
                    "start": start,
                    "moves": t_moves + o_moves,
                    "move_count": move_count,
                    "earliest": earliest,
                }
            )
    if not options:
        return None

    if tie_policy == "minimize_moves":
        options.sort(
            key=lambda o: (
                o["start"],
                o["move_count"],
                o["terminal"],
                o["operator"],
                o["channel"],
            )
        )
    else:  # earliest_ready：稳定按名称裁决
        options.sort(
            key=lambda o: (
                o["start"],
                o["earliest"],
                o["terminal"],
                o["operator"],
                o["channel"],
            )
        )
    best = options[0]
    return (
        best["terminal"],
        best["operator"],
        best["channel"],
        best["start"],
        best["moves"],
    )


def _pick_channel(inputs: Inputs, kind: str) -> tuple[str, Resource] | tuple[None, None]:
    cands = sorted(
        ((n, r) for n, r in inputs.channels().items() if _compatible(r, kind)),
        key=lambda nr: nr[0],
    )
    if not cands:
        return None, None
    return cands[0]


def _resource_ready_at_site(
    inputs: Inputs,
    name: str,
    res: Resource,
    site: str,
    dep_end: int,
    state: _SimState,
) -> tuple[int | None, list[Move]]:
    """移动资源到达指定站点并空闲的最早时刻；不可达返回 (None, [])。"""
    loc = state.locations[name]
    travel = shortest_travel(inputs, loc.site, site)
    moves: list[Move] = []
    if travel is None:
        return None, []
    travel_minutes, path = travel
    depart = max(loc.available_minute, dep_end)
    arrival = depart + travel_minutes
    if travel_minutes > 0:
        moves.append(
            Move(
                resource=name,
                from_site=loc.site,
                to_site=site,
                start_minute=depart,
                arrival_minute=arrival,
                path=path,
            )
        )
    free = _earliest_free(state.busy[name], max(arrival, dep_end))
    # 若资源在到达后又被前序占用推后，位置不变
    return free, moves


def _diagnose_no_candidate(
    inputs: Inputs, biz: Business, dep_end: int, state: _SimState
) -> Unmet:
    """排不出资源时区分：无兼容资源 / 不可达 / 通道争用。"""
    terminals = [
        n for n, r in inputs.terminals().items() if _compatible(r, biz.kind)
    ]
    operators = [
        n for n, r in inputs.operators().items() if _compatible(r, biz.kind)
    ]
    channel = _pick_channel(inputs, biz.kind)[0]
    blocking: list[str] = []

    if not terminals or not operators or channel is None:
        missing = []
        if not terminals:
            missing.append("兼容终端")
        if not operators:
            missing.append("合格操作员")
        if channel is None:
            missing.append("兼容通道")
        return Unmet(
            business=biz.business,
            reason="no_compatible_resource",
            detail=f"全局缺少：{'、'.join(missing)}",
        )

    unreachable_t, unreachable_o = [], []
    for name in terminals:
        travel = shortest_travel(inputs, state.locations[name].site, biz.site)
        if travel is None:
            unreachable_t.append(name)
    for name in operators:
        travel = shortest_travel(inputs, state.locations[name].site, biz.site)
        if travel is None:
            unreachable_o.append(name)
    if len(unreachable_t) == len(terminals) or len(unreachable_o) == len(operators):
        blocking = unreachable_t + unreachable_o
        return Unmet(
            business=biz.business,
            reason="unreachable",
            detail=(
                f"站点 {biz.site} 对全部兼容终端/操作员不可达"
                f"（终端 {terminals}，操作员 {operators}）"
            ),
            blocking_resources=blocking,
        )

    # 资源能到但通道槽整段争用
    if channel is not None:
        slot = _earliest_channel_slot(
            state.busy[channel],
            inputs.resources[channel].capacity,
            dep_end,
            biz.duration_minutes,
        )
        if slot is None or slot > biz.deadline_minutes:
            return Unmet(
                business=biz.business,
                reason="channel_contention",
                detail=(
                    f"通道 {channel} 在最晚开始 {biz.deadline_minutes} 分钟内"
                    f"无持续 {biz.duration_minutes} 分钟的空槽"
                ),
                earliest_feasible_minute=slot,
                blocking_resources=[channel],
            )
    return Unmet(
        business=biz.business,
        reason="capacity_contention",
        detail="兼容资源均被占用，无法在截止时间前腾出",
        blocking_resources=terminals + operators,
    )


# ---------------------------------------------------------------------------
# 计划校验：对任何来源的计划做独立复核，不相信模拟过程
# ---------------------------------------------------------------------------

def _build_location_timeline(inputs: Inputs, plan: Plan) -> dict[str, list[tuple[int, str]]]:
    """按时间汇总每个移动资源的位置变化：[(生效分钟, 站点), ...]，按分钟升序。"""
    timeline: dict[str, list[tuple[int, str]]] = {}
    for name, res in inputs.mobile_resources().items():
        origin = plan.preposition.get(name, res.initial_site)
        timeline[name] = [(0, origin)]
    for step in sorted(plan.steps, key=lambda s: s.start_minute):
        for move in step.moves:
            if move.resource in timeline:
                timeline[move.resource].append((move.arrival_minute, move.to_site))
    for name in timeline:
        timeline[name].sort()
    return timeline


def _location_at(
    timeline: dict[str, list[tuple[int, str]]], resource: str, minute: int
) -> str | None:
    events = timeline.get(resource)
    if not events:
        return None
    current = events[0][1]
    for effective, site in events:
        if minute >= effective:
            current = site
        else:
            break
    return current


def validate_plan(inputs: Inputs, scenario: Scenario, plan: Plan) -> list[str]:
    """独立校验计划，返回错误清单；空清单代表可行。"""
    errors: list[str] = []
    affected = inputs.businesses_for_scenario(scenario)
    seen: set[str] = set()
    occupancy: dict[str, list[tuple[int, int]]] = {n: [] for n in inputs.resources}
    completed_at: dict[str, int] = {}

    for res_name, target in plan.preposition.items():
        res = inputs.resources.get(res_name)
        if res is None or res.type not in ("terminal", "operator"):
            errors.append(f"预置引用了非移动资源：{res_name}")
            continue
        if target not in inputs.sites:
            errors.append(f"预置目标站点不存在：{res_name} -> {target}")
            continue
        if shortest_travel(inputs, res.initial_site, target) is None:
            errors.append(f"预置路径不可达：{res_name} {res.initial_site} -> {target}")

    # 移动资源的位置时间线：预置点 -> 各次转移；据此独立核对位置连续性
    location_timeline = _build_location_timeline(inputs, plan)

    for step in sorted(plan.steps, key=lambda s: s.start_minute):
        biz = inputs.businesses.get(step.business)
        if biz is None:
            errors.append(f"步骤引用未知业务：{step.business}")
            continue
        if step.business in seen:
            errors.append(f"业务被重复安排：{step.business}")
        seen.add(step.business)
        if biz not in affected.values():
            errors.append(f"业务 {step.business} 不属于情景 {scenario.scenario} 的受影响站点")
        if step.site != biz.site:
            errors.append(f"业务 {step.business} 执行站点 {step.site} 与登记站点 {biz.site} 不符")
        for res_name in (step.terminal, step.operator, step.channel):
            if res_name not in inputs.resources:
                errors.append(f"业务 {step.business} 引用未知资源 {res_name}")
        terminal = inputs.resources.get(step.terminal)
        operator = inputs.resources.get(step.operator)
        channel = inputs.resources.get(step.channel)
        if terminal and terminal.type != "terminal":
            errors.append(f"业务 {step.business} 的 {step.terminal} 不是终端")
        if operator and operator.type != "operator":
            errors.append(f"业务 {step.business} 的 {step.operator} 不是操作员")
        if channel and channel.type != "channel":
            errors.append(f"业务 {step.business} 的 {step.channel} 不是通道")
        if terminal and not _compatible(terminal, biz.kind):
            errors.append(f"终端 {step.terminal} 不支持业务 {step.business} 的 {biz.kind} 能力")
        if operator and not _compatible(operator, biz.kind):
            errors.append(f"操作员 {step.operator} 无业务 {step.business} 的 {biz.kind} 资格")
        if channel and not _compatible(channel, biz.kind):
            errors.append(f"通道 {step.channel} 不承载业务 {step.business} 的 {biz.kind}")
        if step.start_minute < 0 or step.end_minute <= step.start_minute:
            errors.append(f"业务 {step.business} 时间区间非法")
        if step.end_minute - step.start_minute != biz.duration_minutes:
            errors.append(
                f"业务 {step.business} 时长 {step.end_minute - step.start_minute}"
                f" 与登记 {biz.duration_minutes} 不符"
            )
        if step.start_minute > biz.deadline_minutes:
            errors.append(
                f"业务 {step.business} 开始 {step.start_minute} 晚于最晚开始 {biz.deadline_minutes}"
            )
        if step.end_minute > scenario.duration_minutes:
            errors.append(
                f"业务 {step.business} 完成 {step.end_minute} 超出中断窗口 {scenario.duration_minutes}"
            )
        for dep in biz.depends:
            if dep not in completed_at:
                errors.append(f"业务 {step.business} 开始时依赖 {dep} 尚未完成")
            elif step.start_minute < completed_at[dep]:
                errors.append(
                    f"业务 {step.business} 开始早于依赖 {dep} 完成时刻"
                )
        # 转移段复核：每段必须存在于 transfers，耗时与路径一致
        for move in step.moves:
            res = inputs.resources.get(move.resource)
            if res is None:
                errors.append(f"转移引用未知资源：{move.resource}")
                continue
            if len(move.path) < 2 or move.path[0] != move.from_site or move.path[-1] != move.to_site:
                errors.append(f"转移 {move.resource} 路径端点与 from/to_site 不符")
                continue
            leg_total = 0
            for a, b in zip(move.path, move.path[1:]):
                if (a, b) not in inputs.transfer_minutes:
                    errors.append(f"资源 {move.resource} 转移段 {a}->{b} 未登记，不可假设瞬移")
                else:
                    leg_total += inputs.transfer_minutes[(a, b)]
            if leg_total != move.arrival_minute - move.start_minute:
                errors.append(
                    f"转移 {move.resource} 耗时 {move.arrival_minute - move.start_minute}"
                    f" 与路径合计 {leg_total} 不符"
                )
        # 位置复核：业务开始时终端与操作员必须位于业务站点且转移已完成
        for res_name in (step.terminal, step.operator):
            res = inputs.resources.get(res_name)
            if res is None:
                continue
            location = _location_at(location_timeline, res_name, step.start_minute)
            if location != biz.site:
                errors.append(
                    f"业务 {step.business} 开始时资源 {res_name} 位于 {location}，"
                    f"不在站点 {biz.site}"
                )
        for res_name in (step.terminal, step.operator, step.channel):
            occupancy[res_name].append((step.start_minute, step.end_minute))
        completed_at[step.business] = step.end_minute

    # 容量复核：逐分钟统计，超过 capacity 即重复使用
    for res_name, intervals in occupancy.items():
        cap = inputs.resources[res_name].capacity
        for start, end in intervals:
            for minute in range(start, end):
                used = sum(1 for s, e in intervals if s <= minute < e)
                if used > cap:
                    errors.append(
                        f"资源 {res_name} 在第 {minute} 分钟被 {used} 个业务占用，"
                        f"超过容量 {cap}（重复使用）"
                    )
                    break

    # 情景内业务要么在计划中，要么在 unmet 中
    unmet_names = {u.business for u in plan.unmet}
    for name in affected:
        if name not in seen and name not in unmet_names:
            errors.append(f"业务 {name} 既未安排也未标记失守，结论不完整")
    if seen & unmet_names:
        errors.append("同一业务同时出现在已安排与失守清单中")
    # 去重错误信息，保持稳定顺序
    return sorted(set(errors))


# ---------------------------------------------------------------------------
# 方案枚举与指标
# ---------------------------------------------------------------------------

TIE_POLICIES = ("earliest_ready", "minimize_moves")


def enumerate_plans(
    inputs: Inputs, scenario_name: str
) -> tuple[Scenario, list[Plan]]:
    scenario = inputs.scenarios.get(scenario_name)
    if scenario is None:
        raise SchedulingError(f"未知情景：{scenario_name}")
    affected = inputs.businesses_for_scenario(scenario)
    orders = topo_orders(inputs, affected)
    plans: list[Plan] = []
    seen_variants: set[tuple] = set()
    for prep in preposition_variants(inputs, scenario):
        for order_label, order in orders.items():
            for policy in TIE_POLICIES:
                key = (
                    tuple(sorted(prep.items())),
                    order_label,
                    policy,
                )
                if key in seen_variants:
                    continue
                seen_variants.add(key)
                plans.append(simulate(inputs, scenario, prep, order, order_label, policy))
    return scenario, plans


def plan_metrics(plan: Plan) -> dict:
    steps = plan.steps
    total_moves = sum(len(s.moves) for s in steps)
    move_minutes = sum(
        m.arrival_minute - m.start_minute
        for s in steps
        for m in s.moves
    )
    return {
        "met_count": len(steps),
        "unmet_count": len(plan.unmet),
        "total_moves": total_moves,
        "total_move_minutes": move_minutes,
        "max_finish_minute": max((s.end_minute for s in steps), default=0),
    }


def rank_plans(inputs: Inputs, plans: list[Plan]) -> list[Plan]:
    """按 可行 > 一级业务满足数 > 总满足数 > 失守数 > 转移成本 排序。"""
    def priority_met(plan: Plan) -> int:
        return sum(
            1
            for s in plan.steps
            if inputs.businesses[s.business].priority == 1
        )

    def key(plan: Plan):
        metrics = plan_metrics(plan)
        return (
            0 if plan.valid else 1,
            -priority_met(plan),
            -metrics["met_count"],
            metrics["unmet_count"],
            metrics["total_move_minutes"],
            metrics["total_moves"],
            tuple(sorted(plan.preposition.items())),
            plan.ordering,
            plan.tie_policy,
        )

    return sorted(plans, key=key)


def shared_bottlenecks(inputs: Inputs, plan: Plan) -> list[dict]:
    """汇总共享瓶颈：每个资源的峰值并发、容量与被阻断次数。"""
    intervals: dict[str, list[tuple[int, int]]] = {n: [] for n in inputs.resources}
    for step in plan.steps:
        for res_name in (step.terminal, step.operator, step.channel):
            intervals[res_name].append((step.start_minute, step.end_minute))
    blocked_counts: dict[str, int] = {}
    for unmet in plan.unmet:
        for res_name in unmet.blocking_resources:
            if res_name in inputs.resources:
                blocked_counts[res_name] = blocked_counts.get(res_name, 0) + 1
    report = []
    for name in sorted(inputs.resources):
        iv = intervals[name]
        peak = 0
        for start, end in iv:
            for minute in range(start, end):
                peak = max(peak, sum(1 for s, e in iv if s <= minute < e))
        res = inputs.resources[name]
        blocked = blocked_counts.get(name, 0)
        report.append(
            {
                "resource": name,
                "type": res.type,
                "capacity": res.capacity,
                "peak_concurrent": peak,
                "saturation": peak / res.capacity,
                "blocked_business_count": blocked,
                "is_bottleneck": peak >= res.capacity or blocked > 0,
            }
        )
    # 瓶颈 = 并发打满容量，或直接阻断了失守业务；其余资源仅作利用率参考
    return [r for r in report if r["is_bottleneck"]]
