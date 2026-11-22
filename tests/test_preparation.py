"""筹备占用、演练排他、回填核验分类、重评估与补演目标测试。"""

import tempfile
import unittest
from pathlib import Path

from emergency_prep.evidence import EvidenceLedger
from emergency_prep.inputs import load_inputs
from emergency_prep.preparation import (
    BookingError,
    CONFIG_INAPPLICABLE,
    DISPUTED_VERDICT,
    MISSING_EVIDENCE,
    PROCESS_FAILURE,
    RESOURCE_INSUFFICIENT,
    SUPPORTED,
    backfill_drill,
    cancel_arrangement,
    check_resource_conflicts,
    create_arrangement,
    register_drill,
    reevaluate_pending_arrangements,
    supplement_targets,
)
from emergency_prep.scheduler import enumerate_plans, rank_plans
from emergency_prep.store import init_workspace, next_id, read_state, write_state
from tests.fixtures import write_inputs


class PreparationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.input_dir = write_inputs(self.tmp)
        self.inputs = load_inputs(self.input_dir)
        self.work = self.tmp / "work"
        init_workspace(self.work, self.input_dir)
        self.state = read_state(self.work)
        self.ledger = EvidenceLedger()
        _, plans = enumerate_plans(self.inputs, "dual")
        self.plan = rank_plans(self.inputs, plans)[0]
        self.resources = sorted(
            {s.terminal for s in self.plan.steps}
            | {s.operator for s in self.plan.steps}
            | {s.channel for s in self.plan.steps}
        )
        self.win = ("2026-11-25T08:00:00Z", "2026-11-25T10:00:00Z")

    def tearDown(self):
        self._tmp.cleanup()

    def _save(self):
        write_state(self.work, self.state)

    def _book(self, window=None, title="安排"):
        start, end = window or self.win
        conflicts = check_resource_conflicts(self.inputs, self.state, self.resources, start, end)
        self.assertEqual(conflicts, [])
        arrangement = create_arrangement(
            self.state, next_id(self.state, "arrangement", "AR"),
            title, "dual", self.plan, self.resources, start, end,
        )
        return arrangement

    def test_overlapping_drill_cannot_reconfirm_same_terminal(self):
        self._book()
        conflicts = check_resource_conflicts(
            self.inputs, self.state, ["T1", "O1"],
            "2026-11-25T09:00:00Z", "2026-11-25T09:30:00Z",
        )
        self.assertTrue(any(c["resource"] == "T1" for c in conflicts))

    def test_non_overlapping_window_allowed(self):
        self._book()
        conflicts = check_resource_conflicts(
            self.inputs, self.state, self.resources,
            "2026-11-26T08:00:00Z", "2026-11-26T10:00:00Z",
        )
        self.assertEqual(conflicts, [])

    def test_cancel_releases_resources(self):
        arrangement = self._book()
        cancel_arrangement(self.state, arrangement["id"], "改期")
        conflicts = check_resource_conflicts(
            self.inputs, self.state, self.resources, *self.win
        )
        self.assertEqual(conflicts, [])

    def test_executed_drill_cannot_be_cancelled(self):
        arrangement = self._book()
        drill = register_drill(
            self.state, next_id(self.state, "drill", "DR"),
            "演练", "dual", self.plan, self.resources,
            self.win[0], self.win[1], arrangement_id=arrangement["id"],
            expected_configs={
                s.business: self.inputs.businesses[s.business].config
                for s in self.plan.steps
            },
        )
        backfill_drill(
            self.inputs, self.state, self.ledger, drill["id"],
            [
                {
                    "business": step["business"],
                    "config": self.inputs.businesses[step["business"]].config,
                    "resource": step["terminal"],
                    "source": "machine",
                    "result": "success",
                    "start": "2026-11-25T08:00:00Z",
                    "end": "2026-11-25T08:20:00Z",
                }
                for step in drill["plan_snapshot"]["steps"]
            ],
            imported_from="test",
        )
        with self.assertRaises(BookingError):
            cancel_arrangement(self.state, arrangement["id"], "试图取消已执行")

    def _register_drill(self):
        drill = register_drill(
            self.state, next_id(self.state, "drill", "DR"),
            "演练", "dual", self.plan, self.resources,
            self.win[0], self.win[1], arrangement_id=None,
            expected_configs={
                s.business: self.inputs.businesses[s.business].config
                for s in self.plan.steps
            },
        )
        return drill

    def test_backfill_supported_when_plan_config_and_terminal_match(self):
        drill = self._register_drill()
        steps = []
        for step in drill["plan_snapshot"]["steps"]:
            biz = self.inputs.businesses[step["business"]]
            for source in ("machine", "observer"):
                steps.append(
                    {
                        "business": step["business"], "config": biz.config,
                        "resource": step["terminal"], "source": source,
                        "result": "success",
                        "start": "2026-11-25T08:00:00Z",
                        "end": "2026-11-25T08:20:00Z",
                    }
                )
        backfill_drill(self.inputs, self.state, self.ledger, drill["id"], steps, "test")
        verdicts = {s["business"]: s["verdict"] for s in drill["verification"]["steps"]}
        self.assertTrue(all(v == SUPPORTED for v in verdicts.values()), verdicts)
        self.assertEqual(drill["verification"]["overall"], SUPPORTED)

    def test_backfill_flags_machine_observer_dispute(self):
        drill = self._register_drill()
        steps = [
            {
                "business": "call-A", "config": "v2", "resource": "T1",
                "source": "machine", "result": "success",
                "start": "2026-11-25T08:00:00Z", "end": "2026-11-25T08:20:00Z",
            },
            {
                "business": "call-A", "config": "v2", "resource": "T1",
                "source": "observer", "result": "fail",
                "start": "2026-11-25T08:00:00Z", "end": "2026-11-25T08:20:00Z",
            },
        ]
        backfill_drill(self.inputs, self.state, self.ledger, drill["id"], steps, "test")
        verdict = next(
            s for s in drill["verification"]["steps"] if s["business"] == "call-A"
        )
        self.assertEqual(verdict["verdict"], DISPUTED_VERDICT)

    def test_backfill_config_mismatch_is_config_inapplicable(self):
        drill = self._register_drill()
        steps = [
            {
                "business": "call-A", "config": "v1", "resource": "T1",
                "source": "machine", "result": "success",
                "start": "2026-11-25T08:00:00Z", "end": "2026-11-25T08:20:00Z",
            }
        ]
        backfill_drill(self.inputs, self.state, self.ledger, drill["id"], steps, "test")
        verdict = drill["verification"]["steps"][0]
        self.assertEqual(verdict["verdict"], CONFIG_INAPPLICABLE)

    def test_backfill_borrowed_terminal_does_not_prove_planned_terminal(self):
        drill = self._register_drill()
        planned = next(s for s in drill["plan_snapshot"]["steps"] if s["business"] == "call-B")
        steps = [
            {
                "business": "call-B", "config": "v2",
                "resource": "T1",  # 计划终端是 T2，临时借调 T1
                "source": "machine", "result": "success", "borrowed": True,
                "start": "2026-11-25T08:00:00Z", "end": "2026-11-25T08:20:00Z",
            }
        ]
        backfill_drill(self.inputs, self.state, self.ledger, drill["id"], steps, "test")
        verdict = next(
            s for s in drill["verification"]["steps"] if s["business"] == "call-B"
        )
        self.assertEqual(verdict["verdict"], CONFIG_INAPPLICABLE)
        self.assertTrue(verdict["borrowed_used"])
        # 原计划终端 T2 的能力仍未被证明
        self.assertEqual(
            self.ledger.capability("call-B", "v2", planned["terminal"]).status,
            "unconfirmed",
        )

    def test_backfill_resource_failure_classified_resource_insufficient(self):
        drill = self._register_drill()
        steps = [
            {
                "business": "call-A", "config": "v2", "resource": "T1",
                "source": "machine", "result": "fail",
                "start": "2026-11-25T08:00:00Z", "end": "2026-11-25T08:20:00Z",
            }
        ]
        backfill_drill(self.inputs, self.state, self.ledger, drill["id"], steps, "test")
        verdict = drill["verification"]["steps"][0]
        self.assertEqual(verdict["verdict"], RESOURCE_INSUFFICIENT)

    def test_backfill_late_success_is_process_failure(self):
        drill = self._register_drill()
        # call-A 最晚开始 10 分钟，实测第 12 分钟才开始
        steps = [
            {
                "business": "call-A", "config": "v2", "resource": "T1",
                "source": "machine", "result": "success",
                "start": "2026-11-25T08:12:00Z", "end": "2026-11-25T08:32:00Z",
            }
        ]
        backfill_drill(self.inputs, self.state, self.ledger, drill["id"], steps, "test")
        verdict = drill["verification"]["steps"][0]
        self.assertEqual(verdict["verdict"], PROCESS_FAILURE)

    def test_missing_observation_is_missing_evidence(self):
        drill = self._register_drill()
        backfill_drill(self.inputs, self.state, self.ledger, drill["id"], [], "test")
        businesses = {s["business"] for s in drill["verification"]["steps"]}
        self.assertEqual(businesses, {s["business"] for s in drill["plan_snapshot"]["steps"]})
        self.assertTrue(
            all(s["verdict"] == MISSING_EVIDENCE for s in drill["verification"]["steps"])
        )

    def test_shared_terminal_failure_triggers_reevaluation_of_pending_only(self):
        # 一条未执行安排引用 T1
        pending = self._book(title="待执行安排")
        # 一条已执行演练保留历史
        drill = self._register_drill()
        backfill_drill(
            self.inputs, self.state, self.ledger, drill["id"],
            [
                {
                    "business": "call-A", "config": "v2", "resource": "T1",
                    "source": "machine", "result": "fail",
                    "start": "2026-11-25T08:00:00Z", "end": "2026-11-25T08:20:00Z",
                }
            ],
            "test",
        )
        affected = reevaluate_pending_arrangements(self.inputs, self.state, self.ledger)
        ids = {a["arrangement_id"] for a in affected}
        self.assertIn(pending["id"], ids)
        # 已执行演练的快照不被重写
        executed_drill = next(d for d in self.state["drills"] if d["id"] == drill["id"])
        self.assertEqual(executed_drill["status"], "executed")
        self.assertIsNotNone(executed_drill["verification"])

    def test_supplement_targets_only_unconfirmed_and_disputed(self):
        # 预置一条 call-A 已确认证据；call-B/dispatch-A 无证据或争议
        self.ledger.add_observation(
            {
                "observation": "OK1", "drill": "DX", "business": "call-A",
                "config": "v2", "resource": "T1", "source": "machine",
                "result": "success",
                "start": "2026-11-15T08:00:00Z", "end": "2026-11-15T08:20:00Z",
            },
            "test",
        )
        result = supplement_targets(self.inputs, self.state, self.ledger, "dual", self.plan)
        target_biz = {t["business"] for t in result["supplement_targets"]}
        self.assertNotIn("call-A", target_biz)
        self.assertIn("call-B", target_biz)
        self.assertIn("dispatch-A", target_biz)

    def test_supplement_respects_existing_occupancy(self):
        self._book()
        result = supplement_targets(self.inputs, self.state, self.ledger, "dual", self.plan)
        occupied = {o["holder_id"] for o in result["existing_occupancy_to_respect"]}
        self.assertTrue(occupied)  # 已确认占用必须列出供补演避让


if __name__ == "__main__":
    unittest.main()
