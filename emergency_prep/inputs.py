"""只读源资料的加载与契约校验。

输入目录下的文件（sites/resources/transfers/business/scenarios/evidence）
是只读事实；本模块负责加载、字段检查与一致性校验，不产生任何规划结论。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from .timeutil import parse_iso

SOURCE_FILES = (
    "sites.json",
    "resources.json",
    "transfers.json",
    "business.json",
    "scenarios.json",
    "evidence.json",
)


class InputError(ValueError):
    """源资料违反字段契约。"""


@dataclass(frozen=True)
class Site:
    site: str
    region: str


@dataclass(frozen=True)
class Resource:
    resource: str
    type: str  # terminal / channel / operator
    capacity: int
    initial_site: str
    skills: tuple[str, ...]


@dataclass(frozen=True)
class Transfer:
    from_site: str
    to_site: str
    minutes: int


@dataclass(frozen=True)
class Business:
    business: str
    site: str
    kind: str  # voice / data
    deadline_minutes: int
    duration_minutes: int
    depends: tuple[str, ...]
    priority: int
    config: str


@dataclass(frozen=True)
class Scenario:
    scenario: str
    start: str
    outage_sites: tuple[str, ...]
    duration_minutes: int


@dataclass(frozen=True)
class Observation:
    observation: str
    drill: str
    business: str
    config: str
    resource: str
    source: str  # machine / observer
    result: str  # success / fail
    start: str
    end: str


@dataclass
class Inputs:
    sites: dict[str, Site]
    resources: dict[str, Resource]
    transfers: list[Transfer]
    businesses: dict[str, Business]
    scenarios: dict[str, Scenario]
    evidence: dict[str, Observation]
    transfer_minutes: dict[tuple[str, str], int] = field(default_factory=dict)

    def mobile_resources(self) -> dict[str, Resource]:
        """需要物理转移的资源（终端与操作员）；SAT 通道无须转移。"""
        return {
            name: r
            for name, r in self.resources.items()
            if r.type in ("terminal", "operator")
        }

    def terminals(self) -> dict[str, Resource]:
        return {name: r for name, r in self.resources.items() if r.type == "terminal"}

    def operators(self) -> dict[str, Resource]:
        return {name: r for name, r in self.resources.items() if r.type == "operator"}

    def channels(self) -> dict[str, Resource]:
        return {name: r for name, r in self.resources.items() if r.type == "channel"}

    def businesses_for_scenario(self, scenario: Scenario) -> dict[str, Business]:
        """情景内需要保障的业务：位于受影响站点的业务。"""
        return {
            name: b
            for name, b in self.businesses.items()
            if b.site in scenario.outage_sites
        }


def _load_rows(path: Path) -> list[dict]:
    if not path.exists():
        raise InputError(f"缺少源资料文件：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InputError(f"JSON 解析失败 {path.name}：{exc}") from exc
    if not isinstance(data, list):
        raise InputError(f"{path.name} 顶层必须是数组")
    return data


def _require_fields(rows: list[dict], fields: set[str], filename: str) -> None:
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise InputError(f"{filename} 第 {index} 行不是对象")
        if set(row) != fields:
            raise InputError(
                f"{filename} 第 {index} 行字段不符：期望 {sorted(fields)}，实际 {sorted(row)}"
            )


def load_inputs(input_dir: str | Path) -> Inputs:
    """加载并校验整个源资料目录。"""
    input_dir = Path(input_dir)
    raw: dict[str, list[dict]] = {}
    for filename in SOURCE_FILES:
        raw[filename[:-5]] = _load_rows(input_dir / filename)

    _require_fields(raw["sites"], {"site", "region"}, "sites.json")
    _require_fields(
        raw["resources"],
        {"resource", "type", "capacity", "initial_site", "skills"},
        "resources.json",
    )
    _require_fields(
        raw["transfers"], {"from_site", "to_site", "minutes"}, "transfers.json"
    )
    _require_fields(
        raw["business"],
        {
            "business",
            "site",
            "kind",
            "deadline_minutes",
            "duration_minutes",
            "depends",
            "priority",
            "config",
        },
        "business.json",
    )
    _require_fields(
        raw["scenarios"],
        {"scenario", "start", "outage_sites", "duration_minutes"},
        "scenarios.json",
    )
    _require_fields(
        raw["evidence"],
        {
            "observation",
            "drill",
            "business",
            "config",
            "resource",
            "source",
            "result",
            "start",
            "end",
        },
        "evidence.json",
    )

    sites: dict[str, Site] = {}
    for row in raw["sites"]:
        if row["site"] in sites:
            raise InputError(f"sites 出现重复站点：{row['site']}")
        sites[row["site"]] = Site(site=row["site"], region=row["region"])

    resources: dict[str, Resource] = {}
    for row in raw["resources"]:
        if row["resource"] in resources:
            raise InputError(f"resources 出现重复资源：{row['resource']}")
        if row["type"] not in ("terminal", "channel", "operator"):
            raise InputError(f"资源 {row['resource']} 类型非法：{row['type']}")
        if not isinstance(row["capacity"], int) or row["capacity"] < 1:
            raise InputError(f"资源 {row['resource']} capacity 必须为正整数")
        if not isinstance(row["skills"], list) or not row["skills"]:
            raise InputError(f"资源 {row['resource']} skills 必须为非空数组")
        if row["initial_site"] not in sites:
            raise InputError(
                f"资源 {row['resource']} initial_site {row['initial_site']} 不存在"
            )
        resources[row["resource"]] = Resource(
            resource=row["resource"],
            type=row["type"],
            capacity=row["capacity"],
            initial_site=row["initial_site"],
            skills=tuple(row["skills"]),
        )

    transfer_minutes: dict[tuple[str, str], int] = {}
    transfers: list[Transfer] = []
    for row in raw["transfers"]:
        if row["from_site"] not in sites or row["to_site"] not in sites:
            raise InputError(f"transfers 引用了未知站点：{row}")
        if not isinstance(row["minutes"], int) or row["minutes"] < 0:
            raise InputError(f"transfers 耗时必须为非负整数：{row}")
        key = (row["from_site"], row["to_site"])
        if key in transfer_minutes:
            raise InputError(f"transfers 重复路径：{key}")
        transfer_minutes[key] = row["minutes"]
        transfers.append(
            Transfer(
                from_site=row["from_site"],
                to_site=row["to_site"],
                minutes=row["minutes"],
            )
        )

    businesses: dict[str, Business] = {}
    for row in raw["business"]:
        if row["business"] in businesses:
            raise InputError(f"business 出现重复业务：{row['business']}")
        if row["site"] not in sites:
            raise InputError(f"业务 {row['business']} 站点 {row['site']} 不存在")
        if row["kind"] not in ("voice", "data"):
            raise InputError(f"业务 {row['business']} kind 非法：{row['kind']}")
        for field_name in ("deadline_minutes", "duration_minutes", "priority"):
            if not isinstance(row[field_name], int) or row[field_name] < 0:
                raise InputError(
                    f"业务 {row['business']} {field_name} 必须为非负整数"
                )
        if not isinstance(row["depends"], list):
            raise InputError(f"业务 {row['business']} depends 必须为数组")
        businesses[row["business"]] = Business(
            business=row["business"],
            site=row["site"],
            kind=row["kind"],
            deadline_minutes=row["deadline_minutes"],
            duration_minutes=row["duration_minutes"],
            depends=tuple(row["depends"]),
            priority=row["priority"],
            config=row["config"],
        )
    # 依赖引用检查与环检查
    for biz in businesses.values():
        for dep in biz.depends:
            if dep not in businesses:
                raise InputError(f"业务 {biz.business} 依赖未知业务 {dep}")
    _check_dependency_cycle(businesses)

    scenarios: dict[str, Scenario] = {}
    for row in raw["scenarios"]:
        if row["scenario"] in scenarios:
            raise InputError(f"scenarios 重复情景：{row['scenario']}")
        outage = tuple(row["outage_sites"])
        for site in outage:
            if site not in sites:
                raise InputError(f"情景 {row['scenario']} 含未知站点 {site}")
        if not outage:
            raise InputError(f"情景 {row['scenario']} outage_sites 不能为空")
        if not isinstance(row["duration_minutes"], int) or row["duration_minutes"] <= 0:
            raise InputError(f"情景 {row['scenario']} duration_minutes 必须为正整数")
        try:
            parse_iso(row["start"])  # 仅校验格式
        except ValueError as exc:
            raise InputError(f"情景 {row['scenario']} start 时间非法：{exc}") from exc
        scenarios[row["scenario"]] = Scenario(
            scenario=row["scenario"],
            start=row["start"],
            outage_sites=outage,
            duration_minutes=row["duration_minutes"],
        )

    evidence: dict[str, Observation] = {}
    for row in raw["evidence"]:
        if row["observation"] in evidence:
            raise InputError(f"evidence 重复观察编号：{row['observation']}")
        if row["source"] not in ("machine", "observer"):
            raise InputError(f"观察 {row['observation']} source 非法：{row['source']}")
        if row["result"] not in ("success", "fail"):
            raise InputError(f"观察 {row['observation']} result 非法：{row['result']}")
        try:
            start = parse_iso(row["start"])
            end = parse_iso(row["end"])
        except ValueError as exc:
            raise InputError(f"观察 {row['observation']} 时间非法：{exc}") from exc
        if end <= start:
            raise InputError(f"观察 {row['observation']} 结束必须晚于开始（左闭右开）")
        if row["business"] not in businesses:
            raise InputError(
                f"观察 {row['observation']} 引用未知业务 {row['business']}"
            )
        if row["resource"] not in resources:
            raise InputError(
                f"观察 {row['observation']} 引用未知资源 {row['resource']}"
            )
        evidence[row["observation"]] = Observation(
            observation=row["observation"],
            drill=row["drill"],
            business=row["business"],
            config=row["config"],
            resource=row["resource"],
            source=row["source"],
            result=row["result"],
            start=row["start"],
            end=row["end"],
        )

    return Inputs(
        sites=sites,
        resources=resources,
        transfers=transfers,
        businesses=businesses,
        scenarios=scenarios,
        evidence=evidence,
        transfer_minutes=transfer_minutes,
    )


def _check_dependency_cycle(businesses: dict[str, Business]) -> None:
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(name: str, stack: list[str]) -> None:
        if name in done:
            return
        if name in visiting:
            raise InputError(f"业务依赖存在环：{' -> '.join(stack + [name])}")
        visiting.add(name)
        for dep in businesses[name].depends:
            visit(dep, stack + [name])
        visiting.discard(name)
        done.add(name)

    for name in businesses:
        visit(name, [])
