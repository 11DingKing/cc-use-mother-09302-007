"""领域数据结构。

所有结构都以不可变视角使用：资质版本一旦写入只能追加新版本；
区间型动作（续证、撤销、豁免、申诉）各自只影响规定的闭区间。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from .timeutil import d, instant_iso

# 角色（与领域契约 actors 对齐，新增"普通人员"作为无个人材料访问权的基线角色）
Role = Literal["admin", "scheduler", "mentor", "staff"]
ROLE_NAMES = {
    "admin": "校外人员管理员",
    "scheduler": "排课老师",
    "mentor": "非遗传承人",
    "staff": "普通人员",
}

# 证明来源
CertSource = Literal["authority", "school", "third_party"]
SOURCE_NAMES = {
    "authority": "主管部门颁发",
    "school": "合作学校出具",
    "third_party": "第三方机构认证",
}

# 区间型动作
IntervalKind = Literal["renewal", "revocation", "exemption", "appeal"]
INTERVAL_NAMES = {
    "renewal": "续证",
    "revocation": "撤销",
    "exemption": "限时豁免",
    "appeal": "申诉",
}

# 核验结论
Decision = Literal["allow", "deny"]

# 案件状态
CaseStatus = Literal["open", "resolved", "ignored"]


@dataclass(frozen=True)
class Mentor:
    """传承人主档：仅保存身份与联系方式等基础信息。"""

    mentor_id: str
    name: str
    id_digest: str  # 证件号哈希，不明文保存
    phone: str | None
    created_at: datetime

    def to_dict(self) -> dict:
        return {
            "mentor_id": self.mentor_id,
            "name": self.name,
            "id_digest": self.id_digest,
            "phone": self.phone,
            "created_at": instant_iso(self.created_at),
        }


@dataclass(frozen=True)
class QualificationVersion:
    """资质版本：擅长项目、适用年龄、培训记录、证明来源与有效期。

    版本号单调递增；valid_from 之后生效，永不修改，只能被新版本取代。
    校验为 at_time 时刻的核验快照指明使用的版本。
    """

    mentor_id: str
    version: int
    specialties: tuple[str, ...]          # 擅长项目
    age_range: tuple[int, int]            # 适用年龄（闭区间，岁）
    training_records: tuple[str, ...]     # 培训记录（摘要标识）
    cert_source: CertSource               # 证明来源
    cert_no: str                          # 证明编号
    valid_from: date                      # 有效期开始
    valid_until: date                     # 有效期截止（当日仍有效）
    created_at: datetime
    author: str

    def covers_age(self, age: int) -> bool:
        return self.age_range[0] <= age <= self.age_range[1]

    def to_dict(self) -> dict:
        return {
            "mentor_id": self.mentor_id,
            "version": self.version,
            "specialties": list(self.specialties),
            "age_min": self.age_range[0],
            "age_max": self.age_range[1],
            "training_records": list(self.training_records),
            "cert_source": self.cert_source,
            "cert_source_name": SOURCE_NAMES.get(self.cert_source, self.cert_source),
            "cert_no": self.cert_no,
            "valid_from": d(self.valid_from),
            "valid_until": d(self.valid_until),
            "created_at": instant_iso(self.created_at),
            "author": self.author,
        }


@dataclass(frozen=True)
class Interval:
    """区间型动作：续证/撤销/限时豁免/申诉，只影响 [start, end] 闭区间。"""

    interval_id: int
    mentor_id: str
    kind: IntervalKind
    start: date
    end: date
    reason: str
    created_at: datetime
    author: str

    def covers(self, day: date) -> bool:
        return self.start <= day <= self.end

    def to_dict(self) -> dict:
        return {
            "interval_id": self.interval_id,
            "mentor_id": self.mentor_id,
            "kind": self.kind,
            "kind_name": INTERVAL_NAMES.get(self.kind, self.kind),
            "start": d(self.start),
            "end": d(self.end),
            "reason": self.reason,
            "created_at": instant_iso(self.created_at),
            "author": self.author,
        }


@dataclass(frozen=True)
class Evaluation:
    """一次核验结论与完整依据，供管理员解释每次放行或拒绝。"""

    decision: Decision
    at_date: date
    version: int | None
    reasons: tuple[str, ...]
    active_intervals: tuple[Interval, ...]
    matched_specialty: str | None = None

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "at_date": d(self.at_date),
            "version": self.version,
            "matched_specialty": self.matched_specialty,
            "reasons": list(self.reasons),
            "active_intervals": [iv.to_dict() for iv in self.active_intervals],
        }


@dataclass(frozen=True)
class VerificationSnapshot:
    """预约与签到各自固定的核验快照：结论与依据在写入后永不改变。"""

    snapshot_id: int
    scope: Literal["booking", "checkin"]
    mentor_id: str
    specialty: str
    audience_age: int
    evaluated_at: datetime               # 实际执行核验的时刻（可控时间）
    effective_day: date                  # 活动当日（资质按此日评估）
    decision: Decision
    reasons: tuple[str, ...]
    version: int | None
    active_intervals: tuple[Interval, ...]
    created_by: str

    def to_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "scope": self.scope,
            "mentor_id": self.mentor_id,
            "specialty": self.specialty,
            "audience_age": self.audience_age,
            "evaluated_at": instant_iso(self.evaluated_at),
            "effective_day": d(self.effective_day),
            "decision": self.decision,
            "version": self.version,
            "reasons": list(self.reasons),
            "active_intervals": [iv.to_dict() for iv in self.active_intervals],
            "created_by": self.created_by,
        }


@dataclass(frozen=True)
class Case:
    """批量巡检按可控时间生成的待办案件。"""

    case_id: int
    mentor_id: str
    generated_for_day: date
    reason_code: str
    reason: str
    status: CaseStatus
    created_at: datetime
    handled_by: str | None = None
    handled_at: datetime | None = None
    note: str | None = None

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "mentor_id": self.mentor_id,
            "generated_for_day": d(self.generated_for_day),
            "reason_code": self.reason_code,
            "reason": self.reason,
            "status": self.status,
            "created_at": instant_iso(self.created_at),
            "handled_by": self.handled_by,
            "handled_at": instant_iso(self.handled_at) if self.handled_at else None,
            "note": self.note,
        }
