"""只读源资料的契约校验测试：坏数据必须被明确拒绝。"""

import tempfile
import unittest
from pathlib import Path

from emergency_prep.inputs import InputError, load_inputs
from tests.fixtures import (
    BASE_BUSINESS,
    BASE_RESOURCES,
    BASE_SCENARIOS,
    write_inputs,
)


class InputValidationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_baseline_loads(self):
        inputs = load_inputs(write_inputs(self.tmp))
        self.assertEqual(set(inputs.sites), {"A", "B", "C"})

    def test_unknown_dependency_rejected(self):
        business = [dict(BASE_BUSINESS[0], depends=["ghost"])]
        with self.assertRaises(InputError):
            load_inputs(write_inputs(self.tmp, business=business))

    def test_dependency_cycle_rejected(self):
        business = [
            {"business": "x", "site": "A", "kind": "voice", "deadline_minutes": 10,
             "duration_minutes": 10, "depends": ["y"], "priority": 1, "config": "v2"},
            {"business": "y", "site": "A", "kind": "voice", "deadline_minutes": 10,
             "duration_minutes": 10, "depends": ["x"], "priority": 1, "config": "v2"},
        ]
        with self.assertRaises(InputError):
            load_inputs(write_inputs(self.tmp, business=business))

    def test_bad_resource_type_rejected(self):
        resources = [dict(BASE_RESOURCES[0], type="satellite")]
        with self.assertRaises(InputError):
            load_inputs(write_inputs(self.tmp, resources=resources))

    def test_naive_datetime_rejected(self):
        from tests.fixtures import BASE_EVIDENCE
        evidence = [dict(BASE_EVIDENCE[0], start="2026-11-15T08:00:00")]
        with self.assertRaises(InputError):
            load_inputs(write_inputs(self.tmp, evidence=evidence))

    def test_duplicate_observation_id_rejected(self):
        from tests.fixtures import BASE_EVIDENCE
        evidence = BASE_EVIDENCE + [dict(BASE_EVIDENCE[0])]
        with self.assertRaises(InputError):
            load_inputs(write_inputs(self.tmp, evidence=evidence))

    def test_unknown_site_in_scenario_rejected(self):
        scenarios = [dict(BASE_SCENARIOS[0], outage_sites=["A", "Z"])]
        with self.assertRaises(InputError):
            load_inputs(write_inputs(self.tmp, scenarios=scenarios))

    def test_missing_file_rejected(self):
        input_dir = write_inputs(self.tmp)
        (input_dir / "sites.json").unlink()
        with self.assertRaises(InputError):
            load_inputs(input_dir)


if __name__ == "__main__":
    unittest.main()
