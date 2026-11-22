"""演练证据台账。

规则（对应资料契约与题述）：
- observation 编号是事实主键，重复投递同一事实不产生新的业务活动；
- 同一 drill 内补交的观察不形成新的独立证据，独立证据以不同 drill 计；
- 同一演练步骤上 machine 与 observer 结果冲突 => 争议，不得当作已证实能力；
- 能力只在（业务, 配置, 终端）三元组完全相符时成立；
- 现场临时借调设备（borrowed=True）不证明原配置具备该能力；
- 资料不够的能力保持 unconfirmed，不能被其他成功步骤掩盖。
"""

from dataclasses import dataclass, field

from .inputs import Inputs, Observation
from .timeutil import iso, now_utc, parse_iso

# 能力状态
CONFIRMED = "confirmed"      # 有成功证据且无冲突
DISPUTED = "disputed"        # 机器日志与观察员冲突，或跨演练结论不一致
FAILED = "failed"            # 仅有失败证据
UNCONFIRMED = "unconfirmed"  # 无相符证据


@dataclass
class StoredObservation:
    observation: str
    drill: str
    business: str
    config: str
    resource: str
    source: str
    result: str
    start: str
    end: str
    received: str
    borrowed: bool = False
    imported_from: str = ""

    def to_dict(self) -> dict:
        return {
            "observation": self.observation,
            "drill": self.drill,
            "business": self.business,
            "config": self.config,
            "resource": self.resource,
            "source": self.source,
            "result": self.result,
            "start": self.start,
            "end": self.end,
            "received": self.received,
            "borrowed": self.borrowed,
            "imported_from": self.imported_from,
        }

    @classmethod
    def from_dict(cls, row: dict) -> "StoredObservation":
        return cls(
            observation=row["observation"],
            drill=row["drill"],
            business=row["business"],
            config=row["config"],
            resource=row["resource"],
            source=row["source"],
            result=row["result"],
            start=row["start"],
            end=row["end"],
            received=row["received"],
            borrowed=row.get("borrowed", False),
            imported_from=row.get("imported_from", ""),
        )


@dataclass
class CapabilityStatus:
    business: str
    config: str
    resource: str
    status: str
    supporting_drills: list[str] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    borrowed_only: bool = False

    def to_dict(self) -> dict:
        return {
            "business": self.business,
            "config": self.config,
            "resource": self.resource,
            "status": self.status,
            "supporting_drills": self.supporting_drills,
            "conflicts": self.conflicts,
            "observations": self.observations,
            "borrowed_only": self.borrowed_only,
        }


class EvidenceLedger:
    """内存证据台账，可从工作目录状态重建。"""

    def __init__(self, observations: list[StoredObservation] | None = None):
        self._observations: dict[str, StoredObservation] = {}
        for obs in observations or []:
            self._observations[obs.observation] = obs

    # ------------------------------------------------------------------ 导入
    def add_from_inputs(
        self, inputs: Inputs, imported_from: str, received: str | None = None
    ) -> tuple[int, int]:
        """从只读源资料 evidence 行导入。返回 (新增数, 去重跳过数)。"""
        received = received or iso(now_utc())
        added = skipped = 0
        for obs_id, obs in inputs.evidence.items():
            if obs_id in self._observations:
                skipped += 1
                continue
            self._observations[obs_id] = StoredObservation(
                observation=obs.observation,
                drill=obs.drill,
                business=obs.business,
                config=obs.config,
                resource=obs.resource,
                source=obs.source,
                result=obs.result,
                start=obs.start,
                end=obs.end,
                received=received,
                borrowed=False,
                imported_from=imported_from,
            )
            added += 1
        return added, skipped

    def add_observation(
        self, row: dict, imported_from: str, received: str | None = None
    ) -> StoredObservation:
        """登记单条观察（含现场回填）；observation 重复时拒绝而非覆盖。"""
        obs_id = row.get("observation")
        if not obs_id:
            raise ValueError("观察记录缺少 observation 主键")
        if obs_id in self._observations:
            raise ValueError(f"观察 {obs_id} 已存在，重复投递不产生新的业务活动")
        for field_name in ("drill", "business", "config", "resource", "source", "result", "start", "end"):
            if field_name not in row:
                raise ValueError(f"观察记录缺少字段：{field_name}")
        if row["source"] not in ("machine", "observer"):
            raise ValueError(f"source 非法：{row['source']}")
        if row["result"] not in ("success", "fail"):
            raise ValueError(f"result 非法：{row['result']}")
        start = parse_iso(row["start"])
        end = parse_iso(row["end"])
        if end <= start:
            raise ValueError("观察结束必须晚于开始（左闭右开）")
        stored = StoredObservation(
            observation=obs_id,
            drill=row["drill"],
            business=row["business"],
            config=row["config"],
            resource=row["resource"],
            source=row["source"],
            result=row["result"],
            start=row["start"],
            end=row["end"],
            received=received or iso(now_utc()),
            borrowed=bool(row.get("borrowed", False)),
            imported_from=imported_from,
        )
        self._observations[stored.observation] = stored
        return stored

    def all(self) -> list[StoredObservation]:
        return [self._observations[k] for k in sorted(self._observations)]

    def for_drill(self, drill: str) -> list[StoredObservation]:
        return [o for o in self.all() if o.drill == drill]

    def as_of(self, cutoff: str) -> "EvidenceLedger":
        """返回只含 received <= cutoff 的快照（事实进入系统前不可使用）。"""
        cutoff_dt = parse_iso(cutoff)
        kept = [
            o
            for o in self.all()
            if parse_iso(o.received) <= cutoff_dt
        ]
        return EvidenceLedger(kept)

    # ------------------------------------------------------------- 能力判断
    def capability(
        self, business: str, config: str, resource: str
    ) -> CapabilityStatus:
        """评估（业务, 配置, 终端）三元组的能力状态。"""
        matched = [
            o
            for o in self.all()
            if o.business == business
            and o.config == config
            and o.resource == resource
        ]
        if not matched:
            return CapabilityStatus(business, config, resource, UNCONFIRMED)

        conflicts = self._conflicts(matched)
        drills = sorted({o.drill for o in matched})
        non_borrowed = [o for o in matched if not o.borrowed]
        borrowed_only = bool(matched) and not non_borrowed
        evaluated = non_borrowed if non_borrowed else matched
        results = {o.result for o in evaluated}

        if conflicts:
            status = DISPUTED
        elif len(drills) > 1 and results == {"success", "fail"}:
            # 不同演练结论互相矛盾，且无直接冲突行，同样列为争议
            status = DISPUTED
        elif results == {"success"}:
            status = CONFIRMED
        elif results == {"fail"}:
            status = FAILED
        else:
            status = DISPUTED

        # 借调设备的成功不能证明原配置：降级为待确认并显式标注
        if borrowed_only and status == CONFIRMED:
            status = UNCONFIRMED

        return CapabilityStatus(
            business=business,
            config=config,
            resource=resource,
            status=status,
            supporting_drills=sorted(
                {o.drill for o in evaluated if o.result == "success"}
            ),
            conflicts=conflicts,
            observations=[o.observation for o in matched],
            borrowed_only=borrowed_only,
        )

    def capability_matrix(self) -> list[CapabilityStatus]:
        """所有出现过的（业务, 配置, 终端）组合的能力状态。"""
        keys = sorted({(o.business, o.config, o.resource) for o in self.all()})
        return [self.capability(b, c, r) for b, c, r in keys]

    @staticmethod
    def _conflicts(matched: list[StoredObservation]) -> list[dict]:
        """同一演练、同一时间窗口上 machine 与 observer 结果不一致即争议。"""
        conflicts: list[dict] = []
        by_window: dict[tuple, list[StoredObservation]] = {}
        for obs in matched:
            key = (obs.drill, obs.start, obs.end)
            by_window.setdefault(key, []).append(obs)
        for (drill, start, end), group in sorted(by_window.items()):
            sources = {o.source for o in group}
            results = {o.result for o in group}
            if len(sources) > 1 and len(results) > 1:
                conflicts.append(
                    {
                        "drill": drill,
                        "start": start,
                        "end": end,
                        "machine_result": next(
                            (o.result for o in group if o.source == "machine"), None
                        ),
                        "observer_result": next(
                            (o.result for o in group if o.source == "observer"), None
                        ),
                        "observations": sorted(o.observation for o in group),
                    }
                )
        return conflicts

    def disputed_observations(self) -> list[dict]:
        """全台账范围内的争议明细，供计划页显式展示。"""
        groups: dict[tuple, list[StoredObservation]] = {}
        for obs in self.all():
            groups.setdefault(
                (obs.business, obs.config, obs.resource), []
            ).append(obs)
        out: list[dict] = []
        for (business, config, resource), group in sorted(groups.items()):
            conflicts = self._conflicts(group)
            if conflicts:
                out.append(
                    {
                        "business": business,
                        "config": config,
                        "resource": resource,
                        "conflicts": conflicts,
                    }
                )
        return out

    # ------------------------------------------------------------- 序列化
    def to_dict(self) -> list[dict]:
        return [o.to_dict() for o in self.all()]

    @classmethod
    def from_dict(cls, rows: list[dict]) -> "EvidenceLedger":
        return cls([StoredObservation.from_dict(r) for r in rows])


def seed_ledger_from_observations(observations: list[Observation]) -> EvidenceLedger:
    """便于测试：直接由源资料 observation 构造台账。"""
    ledger = EvidenceLedger()
    for obs in observations:
        ledger.add_observation(
            {
                "observation": obs.observation,
                "drill": obs.drill,
                "business": obs.business,
                "config": obs.config,
                "resource": obs.resource,
                "source": obs.source,
                "result": obs.result,
                "start": obs.start,
                "end": obs.end,
            },
            imported_from="inputs/evidence.json",
        )
    return ledger
