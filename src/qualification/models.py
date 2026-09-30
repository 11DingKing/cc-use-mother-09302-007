"""领域模型：角色、命令、事件、核验快照与决定。

写入路径全部以“事件”表达；资质档案是只追加事件流的投影。
核验快照在预约与签到时各自生成一次，之后不再随策略变化而改变。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from .clock import parse_iso, to_iso


class Role(str, enum.Enum):
    ADMIN = "校外人员管理员"
    COORDINATOR = "排课老师"
    STAFF = "普通人员"


# 角色可读取/执行的范围在 permissions 模块中集中定义。


class ReviewResult(str, enum.Enum):
    """一次核验的总体结论。"""

    APPROVED = "放行"
    DENIED = "拒绝"


class DenialReason(str, enum.Enum):
    UNREGISTERED = "未登记资质"
    NO_VALID_CREDENTIAL = "证明缺失或已过期"
    AGE_OUT_OF_RANGE = "适用年龄不覆盖排课对象"
    SPECIALTY_MISMATCH = "擅长项目与活动项目不匹配"
    REVOKED = "证明已被撤销"
    TRAINING_REQUIRED = "缺少覆盖该日期的培训记录"
    PERSON_STATUS = "档案状态不允许进校"


# --- 写入命令 -------------------------------------------------------------


@dataclass(frozen=True)
class RegisterProfile:
    mentor_id: str
    name: str
    id_tail: str  # 证件号后四位等敏感片段，仅管理员可读
    specialties: tuple[str, ...]
    age_min: int
    age_max: int
    credential_source: str
    credential_no: str
    valid_from: str
    valid_to: str
    trainings: tuple[dict[str, Any], ...] = ()
    actor: str = ""
    command_id: str = ""


@dataclass(frozen=True)
class AddTraining:
    mentor_id: str
    topic: str
    trained_on: str  # ISO 日期
    actor: str = ""
    command_id: str = ""


@dataclass(frozen=True)
class RenewCredential:
    """续证：产生新版本，有效期取 [start, end] 区间。"""

    mentor_id: str
    credential_source: str
    credential_no: str
    start: str
    end: str
    reason: str = ""
    actor: str = ""
    command_id: str = ""


@dataclass(frozen=True)
class RevokeCredential:
    """撤销：只影响 [start, end] 规定区间（通常是撤销时点起）。"""

    mentor_id: str
    start: str
    end: str | None  # None 表示开放区间，长期有效
    reason: str
    actor: str = ""
    command_id: str = ""


@dataclass(frozen=True)
class GrantExemption:
    """限时豁免：在 [start, end] 内豁免指定检查项。"""

    mentor_id: str
    start: str
    end: str
    scope: str  # DenialReason 的值，或 "全部"
    reason: str
    actor: str = ""
    command_id: str = ""


@dataclass(frozen=True)
class DecideAppeal:
    """申诉处理：支持/驳回。支持时等价于一次限时豁免。"""

    mentor_id: str
    appeal_id: str
    upheld: bool
    start: str
    end: str
    scope: str
    note: str = ""
    actor: str = ""
    command_id: str = ""


# --- 事件（只追加，不可改写） ----------------------------------------------


@dataclass(frozen=True)
class Event:
    seq: int
    at: str  # ISO，事件被接受的时间
    kind: str
    mentor_id: str
    payload: dict[str, Any]
    prev_hash: str
    event_hash: str = ""
    actor: str = ""
    command_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at,
            "kind": self.kind,
            "mentor_id": self.mentor_id,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "event_hash": self.event_hash,
            "actor": self.actor,
            "command_id": self.command_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Event":
        return cls(
            seq=value["seq"],
            at=value["at"],
            kind=value["kind"],
            mentor_id=value["mentor_id"],
            payload=value["payload"],
            prev_hash=value["prev_hash"],
            event_hash=value.get("event_hash", ""),
            actor=value.get("actor", ""),
            command_id=value.get("command_id", ""),
        )


# --- 核验快照与决定 --------------------------------------------------------


@dataclass(frozen=True)
class CredentialSnapshot:
    """核验当时资质数据的不可改写快照。"""

    version: int
    specialties: tuple[str, ...]
    age_min: int
    age_max: int
    id_tail: str
    credential_source: str
    credential_no: str
    valid_from: str
    valid_to: str
    revoked_intervals: tuple[tuple[str, str | None], ...]
    exemption_intervals: tuple[dict[str, Any], ...]
    trainings: tuple[dict[str, Any], ...]
    data_event_seq: int  # 快照数据截至的事件序号

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "specialties": list(self.specialties),
            "age_min": self.age_min,
            "age_max": self.age_max,
            "id_tail": self.id_tail,
            "credential_source": self.credential_source,
            "credential_no": self.credential_no,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "revoked_intervals": [list(item) for item in self.revoked_intervals],
            "exemption_intervals": list(self.exemption_intervals),
            "trainings": list(self.trainings),
            "data_event_seq": self.data_event_seq,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CredentialSnapshot":
        return cls(
            version=value["version"],
            specialties=tuple(value["specialties"]),
            age_min=value["age_min"],
            age_max=value["age_max"],
            id_tail=value.get("id_tail", ""),
            credential_source=value["credential_source"],
            credential_no=value["credential_no"],
            valid_from=value["valid_from"],
            valid_to=value["valid_to"],
            revoked_intervals=tuple((item[0], item[1]) for item in value["revoked_intervals"]),
            exemption_intervals=tuple(value["exemption_intervals"]),
            trainings=tuple(value["trainings"]),
            data_event_seq=value["data_event_seq"],
        )


@dataclass(frozen=True)
class Decision:
    """对一次放行/拒绝依据的完整解释。"""

    result: str
    reasons: tuple[str, ...]
    waived: tuple[str, ...]  # 被豁免覆盖的检查项
    checked_at: str
    activity_specialty: str
    audience_age: int
    basis_event_seq: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "reasons": list(self.reasons),
            "waived": list(self.waived),
            "checked_at": self.checked_at,
            "activity_specialty": self.activity_specialty,
            "audience_age": self.audience_age,
            "basis_event_seq": self.basis_event_seq,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Decision":
        return cls(
            result=value["result"],
            reasons=tuple(value["reasons"]),
            waived=tuple(value["waived"]),
            checked_at=value["checked_at"],
            activity_specialty=value["activity_specialty"],
            audience_age=value["audience_age"],
            basis_event_seq=value["basis_event_seq"],
        )


@dataclass(frozen=True)
class VerificationSnapshot:
    """预约或签到固定下来的核验快照。"""

    kind: str  # "预约" | "签到"
    at: str
    activity_id: str
    mentor_id: str
    credential: CredentialSnapshot
    decision: Decision
    event_seq: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "at": self.at,
            "activity_id": self.activity_id,
            "mentor_id": self.mentor_id,
            "credential": self.credential.to_dict(),
            "decision": self.decision.to_dict(),
            "event_seq": self.event_seq,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "VerificationSnapshot":
        return cls(
            kind=value["kind"],
            at=value["at"],
            activity_id=value["activity_id"],
            mentor_id=value["mentor_id"],
            credential=CredentialSnapshot.from_dict(value["credential"]),
            decision=Decision.from_dict(value["decision"]),
            event_seq=value["event_seq"],
        )


# --- 巡检待办案件 ----------------------------------------------------------


@dataclass(frozen=True)
class InspectionCase:
    case_id: str
    mentor_id: str
    kind: str  # "到期预警" | "已过期" | "撤销待处理"
    detail: str
    generated_at: str
    effective_at: str  # 案件对应的数据时点
    basis_event_seq: int
    status: str = "待办"  # 待办 | 已处置
    resolved_at: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "mentor_id": self.mentor_id,
            "kind": self.kind,
            "detail": self.detail,
            "generated_at": self.generated_at,
            "effective_at": self.effective_at,
            "basis_event_seq": self.basis_event_seq,
            "status": self.status,
            "resolved_at": self.resolved_at,
            "note": self.note,
        }


def as_iso(value: Any) -> str:
    return to_iso(parse_iso(value))
