"""证据台账测试：去重、争议、能力三元组匹配、借调设备不证明原配置。"""

import unittest

from emergency_prep.evidence import (
    CONFIRMED,
    DISPUTED,
    FAILED,
    UNCONFIRMED,
    EvidenceLedger,
)


def obs(observation, drill, business, config, resource, source, result,
        start="2026-11-15T08:00:00Z", end="2026-11-15T08:20:00Z", borrowed=False):
    return {
        "observation": observation, "drill": drill, "business": business,
        "config": config, "resource": resource, "source": source,
        "result": result, "start": start, "end": end, "borrowed": borrowed,
    }


class EvidenceLedgerTest(unittest.TestCase):
    def setUp(self):
        self.ledger = EvidenceLedger()

    def test_duplicate_observation_rejected_not_double_counted(self):
        row = obs("E1", "D1", "call-A", "v2", "T1", "machine", "success")
        self.ledger.add_observation(row, imported_from="test")
        with self.assertRaises(ValueError):
            self.ledger.add_observation(dict(row), imported_from="test")
        self.assertEqual(len(self.ledger.all()), 1)

    def test_machine_observer_conflict_is_disputed(self):
        self.ledger.add_observation(
            obs("E3", "D2", "dispatch-A", "v2", "T1", "machine", "success",
                "2026-11-15T09:00:00Z", "2026-11-15T09:25:00Z"),
            "test",
        )
        self.ledger.add_observation(
            obs("E4", "D2", "dispatch-A", "v2", "T1", "observer", "fail",
                "2026-11-15T09:00:00Z", "2026-11-15T09:25:00Z"),
            "test",
        )
        cap = self.ledger.capability("dispatch-A", "v2", "T1")
        self.assertEqual(cap.status, DISPUTED)
        self.assertEqual(len(self.ledger.disputed_observations()), 1)

    def test_agreement_across_sources_is_confirmed(self):
        self.ledger.add_observation(obs("E1", "D1", "call-A", "v2", "T1", "machine", "success"), "t")
        self.ledger.add_observation(obs("E2", "D1", "call-A", "v2", "T1", "observer", "success"), "t")
        cap = self.ledger.capability("call-A", "v2", "T1")
        self.assertEqual(cap.status, CONFIRMED)

    def test_capability_requires_exact_config_and_terminal(self):
        # call-B 只有 v1/T1 的成功，v2/T1 与 v2/T2 都必须是待确认
        self.ledger.add_observation(obs("E5", "D3", "call-B", "v1", "T1", "machine", "success"), "t")
        self.assertEqual(self.ledger.capability("call-B", "v1", "T1").status, CONFIRMED)
        self.assertEqual(self.ledger.capability("call-B", "v2", "T1").status, UNCONFIRMED)
        self.assertEqual(self.ledger.capability("call-B", "v2", "T2").status, UNCONFIRMED)

    def test_borrowed_device_success_does_not_prove_original_config(self):
        self.ledger.add_observation(
            obs("B1", "D9", "call-B", "v2", "T1", "machine", "success", borrowed=True), "t"
        )
        cap = self.ledger.capability("call-B", "v2", "T1")
        self.assertEqual(cap.status, UNCONFIRMED)
        self.assertTrue(cap.borrowed_only)

    def test_failure_only_marks_failed(self):
        self.ledger.add_observation(obs("F1", "D8", "call-A", "v2", "T1", "machine", "fail"), "t")
        self.assertEqual(self.ledger.capability("call-A", "v2", "T1").status, FAILED)

    def test_contradiction_across_drills_is_disputed(self):
        self.ledger.add_observation(obs("S1", "D1", "call-A", "v2", "T1", "machine", "success"), "t")
        self.ledger.add_observation(
            obs("F2", "D2", "call-A", "v2", "T1", "machine", "fail",
                "2026-11-16T08:00:00Z", "2026-11-16T08:20:00Z"),
            "t",
        )
        self.assertEqual(self.ledger.capability("call-A", "v2", "T1").status, DISPUTED)

    def test_same_drill_resubmission_is_not_new_independent_evidence(self):
        # 同一 drill 的多条观察仍只归为一个独立证据来源
        self.ledger.add_observation(obs("X1", "D1", "call-A", "v2", "T1", "machine", "success"), "t")
        self.ledger.add_observation(
            obs("X2", "D1", "call-A", "v2", "T1", "observer", "success",
                "2026-11-15T08:05:00Z", "2026-11-15T08:25:00Z"),
            "t",
        )
        cap = self.ledger.capability("call-A", "v2", "T1")
        self.assertEqual(cap.supporting_drills, ["D1"])

    def test_as_of_hides_facts_not_yet_received(self):
        self.ledger.add_observation(
            obs("E1", "D1", "call-A", "v2", "T1", "machine", "success"),
            "t", received="2026-11-15T00:00:00Z",
        )
        self.ledger.add_observation(
            obs("E2", "D1", "call-A", "v2", "T1", "observer", "success"),
            "t", received="2026-11-20T00:00:00Z",
        )
        early = self.ledger.as_of("2026-11-18T00:00:00Z")
        self.assertEqual([o.observation for o in early.all()], ["E1"])

    def test_round_trip_serialization(self):
        self.ledger.add_observation(obs("E1", "D1", "call-A", "v2", "T1", "machine", "success"), "t")
        restored = EvidenceLedger.from_dict(self.ledger.to_dict())
        self.assertEqual(restored.capability("call-A", "v2", "T1").status, CONFIRMED)


if __name__ == "__main__":
    unittest.main()
