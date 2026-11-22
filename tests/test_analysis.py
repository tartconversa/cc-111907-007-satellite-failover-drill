"""情景分析测试：承诺满足、关键缺口、瓶颈与方案对比、证据假设不被掩盖。"""

import tempfile
import unittest
from pathlib import Path

from emergency_prep.analysis import analyze_scenario
from emergency_prep.evidence import DISPUTED, EvidenceLedger
from emergency_prep.inputs import load_inputs
from tests.fixtures import write_inputs


class AnalysisTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.inputs = load_inputs(write_inputs(self.tmp))
        self.ledger = EvidenceLedger()

    def tearDown(self):
        self._tmp.cleanup()

    def test_dual_report_lists_commitments_with_assumption_flags(self):
        # 只确认 call-A；其余即使排得进计划，也必须标为待确认假设
        self.ledger.add_observation(
            {
                "observation": "E1", "drill": "D1", "business": "call-A",
                "config": "v2", "resource": "T1", "source": "machine",
                "result": "success",
                "start": "2026-11-15T08:00:00Z", "end": "2026-11-15T08:20:00Z",
            },
            "test",
        )
        report = analyze_scenario(self.inputs, self.ledger, "dual")
        by_name = {c.business: c for c in report.commitments}
        self.assertTrue(by_name["call-A"].scheduled)
        self.assertFalse(by_name["call-A"].assumption)
        self.assertTrue(by_name["call-B"].assumption)
        self.assertTrue(by_name["dispatch-A"].assumption)
        # 所有方案都通过独立校验
        self.assertEqual(report.valid_count, report.variant_count)

    def test_disputed_evidence_remains_assumption_in_plan(self):
        for obs_id, source, result in (
            ("E3", "machine", "success"),
            ("E4", "observer", "fail"),
        ):
            self.ledger.add_observation(
                {
                    "observation": obs_id, "drill": "D2", "business": "dispatch-A",
                    "config": "v2", "resource": "T1", "source": source,
                    "result": result,
                    "start": "2026-11-15T09:00:00Z", "end": "2026-11-15T09:25:00Z",
                },
                "test",
            )
        report = analyze_scenario(self.inputs, self.ledger, "dual")
        dispatch = next(c for c in report.commitments if c.business == "dispatch-A")
        self.assertEqual(dispatch.capability.status, DISPUTED)
        self.assertTrue(dispatch.assumption)

    def test_critical_gap_reported_when_even_sacrificing_secondary_is_insufficient(self):
        # 构造：唯一终端 T1 在 A，只支持 voice；B 不可达。
        # call-B（关键业务）在任何方案、即使放弃次要业务后都无法满足。
        resources = [
            {"resource": "T1", "type": "terminal", "capacity": 1, "initial_site": "A", "skills": ["voice"]},
            {"resource": "SAT", "type": "channel", "capacity": 2, "initial_site": "A", "skills": ["voice"]},
            {"resource": "O1", "type": "operator", "capacity": 1, "initial_site": "A", "skills": ["voice"]},
        ]
        transfers = [
            {"from_site": "A", "to_site": "C", "minutes": 70},
            {"from_site": "C", "to_site": "A", "minutes": 70},
            {"from_site": "B", "to_site": "A", "minutes": 25},
            {"from_site": "B", "to_site": "C", "minutes": 55},
        ]
        business = [
            {"business": "call-B", "site": "B", "kind": "voice", "deadline_minutes": 15,
             "duration_minutes": 25, "depends": [], "priority": 1, "config": "v2"},
            {"business": "report-C", "site": "C", "kind": "voice", "deadline_minutes": 90,
             "duration_minutes": 20, "depends": [], "priority": 2, "config": "v1"},
        ]
        scenarios = [
            {"scenario": "gap", "start": "2026-11-19T08:00:00Z",
             "outage_sites": ["A", "B", "C"], "duration_minutes": 150}
        ]
        inputs2 = load_inputs(
            write_inputs(Path(tempfile.mkdtemp()), resources=resources,
                         transfers=transfers, business=business,
                         scenarios=scenarios, evidence=[])
        )
        report = analyze_scenario(inputs2, EvidenceLedger(), "gap")
        self.assertEqual(len(report.critical_gaps), 1)
        gap = report.critical_gaps[0]
        self.assertEqual(gap["business"], "call-B")
        self.assertFalse(gap["sacrifice_sufficient"])
        self.assertIn("report-C", gap["secondary_businesses_sacrificed"])
        # 没有任何方案假装全覆盖：最优方案也明确把 call-B 列为失守
        self.assertTrue(
            any(u.business == "call-B" for u in report.best_plan.unmet)
        )

    def test_best_plan_never_violates_deadline_or_dependency(self):
        report = analyze_scenario(self.inputs, self.ledger, "all")
        plan = report.best_plan
        completed: dict[str, int] = {}
        for step in sorted(plan.steps, key=lambda s: s.start_minute):
            biz = self.inputs.businesses[step.business]
            self.assertLessEqual(step.start_minute, biz.deadline_minutes)
            for dep in biz.depends:
                self.assertIn(dep, completed)
                self.assertGreaterEqual(step.start_minute, completed[dep])
            completed[step.business] = step.end_minute

    def test_report_serializes_to_json_ready_dict(self):
        report = analyze_scenario(self.inputs, self.ledger, "dual")
        import json
        encoded = json.dumps(report.to_dict(), ensure_ascii=False)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["scenario"], "dual")
        self.assertIn("best_plan", decoded)


if __name__ == "__main__":
    unittest.main()
