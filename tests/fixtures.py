"""测试夹具：在临时目录构造符合契约的合成输入，便于制造边界条件。"""

import json
from pathlib import Path

BASE_SITES = [
    {"site": "A", "region": "north"},
    {"site": "B", "region": "north"},
    {"site": "C", "region": "south"},
]

BASE_TRANSFERS = [
    {"from_site": "A", "to_site": "B", "minutes": 25},
    {"from_site": "B", "to_site": "A", "minutes": 25},
    {"from_site": "A", "to_site": "C", "minutes": 70},
    {"from_site": "C", "to_site": "A", "minutes": 70},
    {"from_site": "B", "to_site": "C", "minutes": 55},
    {"from_site": "C", "to_site": "B", "minutes": 55},
]

BASE_RESOURCES = [
    {"resource": "T1", "type": "terminal", "capacity": 1, "initial_site": "A", "skills": ["voice", "data"]},
    {"resource": "T2", "type": "terminal", "capacity": 1, "initial_site": "C", "skills": ["voice"]},
    {"resource": "SAT", "type": "channel", "capacity": 2, "initial_site": "A", "skills": ["voice", "data"]},
    {"resource": "O1", "type": "operator", "capacity": 1, "initial_site": "A", "skills": ["data", "voice"]},
    {"resource": "O2", "type": "operator", "capacity": 1, "initial_site": "C", "skills": ["voice"]},
]

BASE_BUSINESS = [
    {"business": "call-A", "site": "A", "kind": "voice", "deadline_minutes": 10,
     "duration_minutes": 20, "depends": [], "priority": 1, "config": "v2"},
    {"business": "dispatch-A", "site": "A", "kind": "data", "deadline_minutes": 45,
     "duration_minutes": 25, "depends": ["call-A"], "priority": 1, "config": "v2"},
    {"business": "call-B", "site": "B", "kind": "voice", "deadline_minutes": 15,
     "duration_minutes": 25, "depends": [], "priority": 1, "config": "v2"},
    {"business": "report-C", "site": "C", "kind": "voice", "deadline_minutes": 90,
     "duration_minutes": 20, "depends": [], "priority": 2, "config": "v1"},
]

BASE_SCENARIOS = [
    {"scenario": "dual", "start": "2026-11-19T08:00:00Z", "outage_sites": ["A", "B"],
     "duration_minutes": 120},
    {"scenario": "all", "start": "2026-11-19T08:00:00Z", "outage_sites": ["A", "B", "C"],
     "duration_minutes": 150},
]


BASE_EVIDENCE = [
    {"observation": "E1", "drill": "D1", "business": "call-A", "config": "v2",
     "resource": "T1", "source": "machine", "result": "success",
     "start": "2026-11-15T08:00:00Z", "end": "2026-11-15T08:20:00Z"},
    {"observation": "E2", "drill": "D1", "business": "call-A", "config": "v2",
     "resource": "T1", "source": "observer", "result": "success",
     "start": "2026-11-15T08:00:00Z", "end": "2026-11-15T08:20:00Z"},
    {"observation": "E3", "drill": "D2", "business": "dispatch-A", "config": "v2",
     "resource": "T1", "source": "machine", "result": "success",
     "start": "2026-11-15T09:00:00Z", "end": "2026-11-15T09:25:00Z"},
    {"observation": "E4", "drill": "D2", "business": "dispatch-A", "config": "v2",
     "resource": "T1", "source": "observer", "result": "fail",
     "start": "2026-11-15T09:00:00Z", "end": "2026-11-15T09:25:00Z"},
    {"observation": "E5", "drill": "D3", "business": "call-B", "config": "v1",
     "resource": "T1", "source": "machine", "result": "success",
     "start": "2026-11-14T08:00:00Z", "end": "2026-11-14T08:25:00Z"},
]


def write_inputs(
    tmp_path: Path,
    *,
    sites=None,
    resources=None,
    transfers=None,
    business=None,
    scenarios=None,
    evidence=None,
) -> Path:
    input_dir = tmp_path / "inputs"
    input_dir.mkdir(parents=True, exist_ok=True)
    payloads = {
        "sites.json": sites if sites is not None else BASE_SITES,
        "resources.json": resources if resources is not None else BASE_RESOURCES,
        "transfers.json": transfers if transfers is not None else BASE_TRANSFERS,
        "business.json": business if business is not None else BASE_BUSINESS,
        "scenarios.json": scenarios if scenarios is not None else BASE_SCENARIOS,
        "evidence.json": evidence if evidence is not None else BASE_EVIDENCE,
    }
    for filename, payload in payloads.items():
        (input_dir / filename).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    return input_dir
