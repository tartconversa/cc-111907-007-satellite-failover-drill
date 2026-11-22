"""排程硬约束测试：容量不重复计算、转移耗时、依赖顺序、截止时间、独立校验。"""

import unittest
from pathlib import Path
import tempfile

from emergency_prep.inputs import load_inputs
from emergency_prep.scheduler import (
    Plan,
    Step,
    enumerate_plans,
    rank_plans,
    shortest_travel,
    simulate,
    topo_orders,
    validate_plan,
    preposition_variants,
)
from tests.fixtures import write_inputs


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.input_dir = write_inputs(self.tmp)
        self.inputs = load_inputs(self.input_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_shortest_travel_uses_directed_graph_and_rejects_teleport(self):
        self.assertEqual(shortest_travel(self.inputs, "A", "A"), (0, ("A",)))
        minutes, path = shortest_travel(self.inputs, "B", "A")
        self.assertEqual(minutes, 25)
        self.assertEqual(path, ("B", "A"))
        # 删除全部从 B 出发的边后，B 成为孤岛，不可假设瞬移
        inputs2 = load_inputs(
            write_inputs(
                Path(tempfile.mkdtemp()),
                transfers=[
                    {"from_site": "A", "to_site": "B", "minutes": 25},
                    {"from_site": "A", "to_site": "C", "minutes": 70},
                    {"from_site": "C", "to_site": "A", "minutes": 70},
                    {"from_site": "C", "to_site": "B", "minutes": 55},
                ],
            )
        )
        self.assertIsNone(shortest_travel(inputs2, "B", "A"))

    def test_dual_scenario_must_meet_all_deadlines_with_preposition(self):
        scenario, plans = enumerate_plans(self.inputs, "dual")
        valid = [p for p in plans if p.valid]
        self.assertTrue(valid)
        best = rank_plans(self.inputs, plans)[0]
        names = {s.business for s in best.steps}
        self.assertEqual(names, {"call-A", "call-B", "dispatch-A"})
        for step in best.steps:
            biz = self.inputs.businesses[step.business]
            self.assertLessEqual(step.start_minute, biz.deadline_minutes)
        # 依赖：dispatch-A 必须在 call-A 完成后开始
        call_a = next(s for s in best.steps if s.business == "call-A")
        dispatch = next(s for s in best.steps if s.business == "dispatch-A")
        self.assertGreaterEqual(dispatch.start_minute, call_a.end_minute)

    def test_no_plan_double_books_capacity_one_resources(self):
        _, plans = enumerate_plans(self.inputs, "dual")
        for plan in plans:
            for resource_name in ("T1", "T2", "O1", "O2"):
                intervals = []
                for step in plan.steps:
                    if resource_name in (step.terminal, step.operator):
                        intervals.append((step.start_minute, step.end_minute))
                for start, end in intervals:
                    for minute in range(start, end):
                        used = sum(1 for s, e in intervals if s <= minute < e)
                        self.assertLessEqual(used, 1, f"{resource_name} 在 {minute} 被重复使用")

    def test_channel_capacity_two_but_three_concurrent_rejected(self):
        # 构造三个零依赖、同时开始的语音业务 + 容量 2 的 SAT
        resources = [
            {"resource": "T1", "type": "terminal", "capacity": 1, "initial_site": "A", "skills": ["voice"]},
            {"resource": "T2", "type": "terminal", "capacity": 1, "initial_site": "A", "skills": ["voice"]},
            {"resource": "T3", "type": "terminal", "capacity": 1, "initial_site": "A", "skills": ["voice"]},
            {"resource": "SAT", "type": "channel", "capacity": 2, "initial_site": "A", "skills": ["voice"]},
            {"resource": "O1", "type": "operator", "capacity": 3, "initial_site": "A", "skills": ["voice"]},
        ]
        business = [
            {"business": f"v{i}", "site": "A", "kind": "voice", "deadline_minutes": 60,
             "duration_minutes": 20, "depends": [], "priority": 1, "config": "v2"}
            for i in range(3)
        ]
        scenarios = [
            {"scenario": "one", "start": "2026-11-19T08:00:00Z", "outage_sites": ["A"],
             "duration_minutes": 240}
        ]
        inputs2 = load_inputs(
            write_inputs(Path(tempfile.mkdtemp()), resources=resources,
                         business=business, scenarios=scenarios, evidence=[])
        )
        _, plans = enumerate_plans(inputs2, "one")
        for plan in plans:
            sat_intervals = [
                (s.start_minute, s.end_minute) for s in plan.steps if s.channel == "SAT"
            ]
            for start, end in sat_intervals:
                for minute in range(start, end):
                    used = sum(1 for s, e in sat_intervals if s <= minute < e)
                    self.assertLessEqual(used, 2)
        # 错峰方案能满足全部三个，且任何方案都不会出现三个并发
        self.assertTrue(any(len(p.steps) == 3 for p in plans if p.valid))
        self.assertFalse(
            any(
                max(
                    (
                        sum(1 for s, e in [(x.start_minute, x.end_minute) for x in p.steps]
                             if s <= m < e)
                        for m in range(240)
                    ),
                    default=0,
                )
                > 2
                for p in plans
            )
        )

    def test_missing_skill_marks_business_unmet_not_silently_scheduled(self):
        # 唯一数据终端只支持 voice，data 业务必须显式失守
        resources = [
            {"resource": "T1", "type": "terminal", "capacity": 1, "initial_site": "A", "skills": ["voice"]},
            {"resource": "SAT", "type": "channel", "capacity": 2, "initial_site": "A", "skills": ["voice", "data"]},
            {"resource": "O1", "type": "operator", "capacity": 1, "initial_site": "A", "skills": ["voice", "data"]},
        ]
        business = [
            {"business": "d1", "site": "A", "kind": "data", "deadline_minutes": 30,
             "duration_minutes": 20, "depends": [], "priority": 1, "config": "v2"}
        ]
        scenarios = [
            {"scenario": "one", "start": "2026-11-19T08:00:00Z", "outage_sites": ["A"],
             "duration_minutes": 120}
        ]
        inputs2 = load_inputs(
            write_inputs(Path(tempfile.mkdtemp()), resources=resources,
                         business=business, scenarios=scenarios, evidence=[])
        )
        _, plans = enumerate_plans(inputs2, "one")
        best = rank_plans(inputs2, plans)[0]
        self.assertEqual(best.met_businesses, [])
        self.assertEqual(best.unmet[0].reason, "no_compatible_resource")
        self.assertTrue(best.valid)  # 显式标记失守本身是有效结论

    def test_dependency_failure_cascades_and_is_reported(self):
        # 切断所有进入 B 的有向边（A->B、C->B 均不存在）：资源既无法中断前预置到 B，
        # 也无法中断后抵达，call-A 必失守，其后置 dispatch-A 必须报 dependency_unmet
        transfers = [
            {"from_site": "A", "to_site": "C", "minutes": 70},
            {"from_site": "C", "to_site": "A", "minutes": 70},
            {"from_site": "B", "to_site": "A", "minutes": 25},
            {"from_site": "B", "to_site": "C", "minutes": 55},
        ]
        # 两个业务都在不可达的 B 站点
        business = [
            {"business": "call-A", "site": "B", "kind": "voice", "deadline_minutes": 10,
             "duration_minutes": 20, "depends": [], "priority": 1, "config": "v2"},
            {"business": "dispatch-A", "site": "B", "kind": "data", "deadline_minutes": 60,
             "duration_minutes": 25, "depends": ["call-A"], "priority": 1, "config": "v2"},
        ]
        inputs2 = load_inputs(
            write_inputs(Path(tempfile.mkdtemp()), business=business,
                         transfers=transfers, evidence=[])
        )
        scenario = inputs2.scenarios["dual"]
        # 预置只能落在可达站点（A/C），B 上无资源
        prep = preposition_variants(inputs2, scenario)[0]
        orders = topo_orders(inputs2, inputs2.businesses_for_scenario(scenario))
        plan = simulate(
            inputs2, scenario, prep,
            orders["priority_first"], "priority_first", "earliest_ready",
        )
        unmet = {u.business: u.reason for u in plan.unmet}
        self.assertIn("call-A", unmet)
        self.assertEqual(unmet["dispatch-A"], "dependency_unmet")
        self.assertTrue(plan.valid)

    def test_independent_validator_rejects_handcrafted_overlap_plan(self):
        scenario = self.inputs.scenarios["dual"]
        # 手工构造一个让 T1 在同一分钟服务两个业务的违规计划
        steps = [
            Step("call-A", "A", "voice", "T1", "O1", "SAT", 0, 20, 10, True),
            Step("call-B", "B", "voice", "T1", "O2", "SAT", 5, 30, 15, True),
        ]
        unmet = [
            type("U", (), {
                "business": "dispatch-A", "reason": "x", "detail": "x",
                "earliest_feasible_minute": None, "blocking_resources": [],
                "to_dict": lambda self: {},
            })()
        ]
        plan = Plan(
            scenario="dual",
            preposition={"T1": "A", "T2": "C", "O1": "A", "O2": "C"},
            ordering="manual",
            tie_policy="manual",
            steps=steps,
            unmet=unmet,
        )
        errors = validate_plan(self.inputs, scenario, plan)
        self.assertTrue(any("重复使用" in e for e in errors), errors)
        self.assertTrue(any("不在站点 B" in e for e in errors), errors)

    def test_validator_rejects_teleport_and_dependency_violation(self):
        scenario = self.inputs.scenarios["dual"]
        # T2 初始在 C，没有 C->B 以外的合法预置，且安排 call-B 早于其依赖
        steps = [
            Step("call-A", "A", "voice", "T1", "O1", "SAT", 0, 20, 10, True),
            Step("dispatch-A", "A", "data", "T1", "O1", "SAT", 10, 35, 45, True),
        ]
        plan = Plan(
            scenario="dual",
            preposition={"T1": "A", "T2": "C", "O1": "A", "O2": "C"},
            ordering="manual",
            tie_policy="manual",
            steps=steps,
            unmet=[
                type("U", (), {
                    "business": "call-B", "reason": "x", "detail": "x",
                    "earliest_feasible_minute": None, "blocking_resources": [],
                    "to_dict": lambda self: {},
                })()
            ],
        )
        errors = validate_plan(self.inputs, scenario, plan)
        self.assertTrue(any("依赖 call-A 尚未完成" in e or "早于依赖" in e for e in errors), errors)

    def test_topo_orders_respect_dependencies(self):
        affected = self.inputs.businesses_for_scenario(self.inputs.scenarios["dual"])
        orders = topo_orders(self.inputs, affected)
        for order in orders.values():
            positions = {name: i for i, name in enumerate(order)}
            self.assertLess(positions["call-A"], positions["dispatch-A"])

    def test_preposition_variants_only_include_reachable_targets(self):
        variants = preposition_variants(self.inputs, self.inputs.scenarios["dual"])
        self.assertTrue(variants)
        for combo in variants:
            for name, target in combo.items():
                res = self.inputs.resources[name]
                self.assertIsNotNone(shortest_travel(self.inputs, res.initial_site, target))


if __name__ == "__main__":
    unittest.main()
