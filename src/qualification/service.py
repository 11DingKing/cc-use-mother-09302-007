"""应用服务层：事务用例、权限校验、访问留痕的唯一入口。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timezone

from . import authz, engine
from .models import (
    Case,
    Interval,
    Mentor,
    QualificationVersion,
    VerificationSnapshot,
)
from .store import Store
from .timeutil import parse_date, parse_instant, utcnow

INTERVAL_KINDS = {"renewal", "revocation", "exemption", "appeal"}
CERT_SOURCES = {"authority", "school", "third_party"}


class ServiceError(Exception):
    """业务规则错误（输入不合法、对象不存在等）。"""


@dataclass(frozen=True)
class Actor:
    user_id: str
    role: str
    name: str


class QualificationService:
    def __init__(self, store: Store) -> None:
        self.store = store

    def actor_for(self, user_id: str) -> Actor:
        """根据用户标识解析角色；未知用户视为无权限的普通人员。"""
        row = self.store.get_user(user_id)
        if row is None:
            return Actor(user_id=user_id, role="staff", name=user_id)
        return Actor(user_id=row["user_id"], role=row["role"], name=row["name"])

    # ---- 人员登记 ----
    def register_mentor(
        self, actor: Actor, mentor_id: str, name: str, id_number: str,
        phone: str | None = None,
    ) -> dict:
        authz.require(actor.role, "mentor.register")
        if self.store.get_mentor(mentor_id) is not None:
            raise ServiceError("传承人已登记，身份信息不可改写")
        if not name or not id_number:
            raise ServiceError("姓名与证件号不能为空")
        digest = hashlib.sha256(id_number.encode("utf-8")).hexdigest()
        mentor = Mentor(
            mentor_id=mentor_id, name=name, id_digest=digest,
            phone=phone, created_at=utcnow(),
        )
        self.store.add_mentor(mentor)
        return mentor.to_dict()

    # ---- 资质版本追加（不可改写） ----
    def add_qualification_version(
        self, actor: Actor, mentor_id: str, *,
        specialties: list[str], age_min: int, age_max: int,
        training_records: list[str], cert_source: str, cert_no: str,
        valid_from: str, valid_until: str, at_time: str | None = None,
    ) -> dict:
        authz.require(actor.role, "qualification.version")
        if self.store.get_mentor(mentor_id) is None:
            raise ServiceError("传承人不存在")
        if cert_source not in CERT_SOURCES:
            raise ServiceError("证明来源不合法")
        if not specialties:
            raise ServiceError("至少登记一个擅长项目")
        if not training_records:
            raise ServiceError("至少登记一条培训记录")
        try:
            age_min_i, age_max_i = int(age_min), int(age_max)
        except (TypeError, ValueError) as exc:
            raise ServiceError("年龄边界必须是整数") from exc
        if not 0 <= age_min_i <= age_max_i:
            raise ServiceError("适用年龄区间不合法")
        start = parse_date(valid_from, "有效期开始")
        until = parse_date(valid_until, "有效期截止")
        if until < start:
            raise ServiceError("有效期截止不能早于开始")
        if not cert_no:
            raise ServiceError("证明编号不能为空")

        version_no = self.store.next_version(mentor_id)
        ver = QualificationVersion(
            mentor_id=mentor_id, version=version_no,
            specialties=tuple(specialties), age_range=(age_min_i, age_max_i),
            training_records=tuple(training_records),
            cert_source=cert_source, cert_no=cert_no,
            valid_from=start, valid_until=until,
            created_at=parse_instant(at_time), author=actor.user_id,
        )
        self.store.add_version(ver)
        return ver.to_dict()

    # ---- 区间型动作 ----
    def add_interval(
        self, actor: Actor, mentor_id: str, kind: str,
        start_day: str, end_day: str, reason: str,
        at_time: str | None = None,
    ) -> dict:
        authz.require(actor.role, "interval.add")
        if kind not in INTERVAL_KINDS:
            raise ServiceError("区间类型不合法")
        if self.store.get_mentor(mentor_id) is None:
            raise ServiceError("传承人不存在")
        start = parse_date(start_day, "区间开始")
        end = parse_date(end_day, "区间结束")
        if end < start:
            raise ServiceError("区间结束不能早于开始")
        if not reason:
            raise ServiceError("区间动作必须说明原因")
        iv = Interval(
            interval_id=0, mentor_id=mentor_id, kind=kind,  # type: ignore[arg-type]
            start=start, end=end, reason=reason,
            created_at=parse_instant(at_time), author=actor.user_id,
        )
        saved = self.store.add_interval(iv)
        return saved.to_dict()

    # ---- 预约 / 签到：各自固定核验快照 ----
    def verify(
        self, actor: Actor, scope: str, mentor_id: str, *,
        specialty: str, audience_age: int, activity_day: str,
        at_time: str | None = None,
    ) -> dict:
        permission = "verify.booking" if scope == "booking" else "verify.checkin"
        authz.require(actor.role, permission)
        if scope not in ("booking", "checkin"):
            raise ServiceError("核验场景必须是 booking 或 checkin")
        if self.store.get_mentor(mentor_id) is None:
            raise ServiceError("传承人不存在")
        try:
            age = int(audience_age)
        except (TypeError, ValueError) as exc:
            raise ServiceError("受众年龄必须是整数") from exc
        if age < 0:
            raise ServiceError("受众年龄不合法")
        if not specialty:
            raise ServiceError("活动项目不能为空")

        evaluated_at = parse_instant(at_time)
        day = parse_date(activity_day, "活动日期")
        result = engine.evaluate(
            self.store.list_versions(mentor_id),
            self.store.list_intervals(mentor_id),
            evaluated_at=evaluated_at, day=day,
            specialty=specialty, audience_age=age,
        )
        snap = VerificationSnapshot(
            snapshot_id=0, scope=scope, mentor_id=mentor_id,  # type: ignore[arg-type]
            specialty=specialty, audience_age=age,
            evaluated_at=evaluated_at, effective_day=day,
            decision=result.decision, reasons=result.reasons,
            version=result.version, active_intervals=result.active_intervals,
            created_by=actor.user_id,
        )
        saved = self.store.add_snapshot(snap)
        return saved.to_dict()

    def get_snapshot(self, actor: Actor, snapshot_id: int) -> dict:
        snap = self.store.get_snapshot(int(snapshot_id))
        if snap is None:
            raise ServiceError("快照不存在")
        if not authz.can_read_snapshot(
            actor.role, actor_id=actor.user_id, owner_mentor_id=snap.mentor_id
        ):
            self.store.log_access(
                actor.user_id, actor.role, snap.mentor_id,
                f"snapshot:{snapshot_id}", False, utcnow(),
            )
            raise authz.AuthorizationError("无权读取该核验快照")
        self.store.log_access(
            actor.user_id, actor.role, snap.mentor_id,
            f"snapshot:{snapshot_id}", True, utcnow(),
        )
        return snap.to_dict()

    # ---- 批量巡检（可控时间） ----
    def run_inspection(self, actor: Actor, as_of: str | None = None,
                       horizon_days: int = engine.NEAR_EXPIRY_DAYS) -> dict:
        authz.require(actor.role, "inspection.run")
        as_of_day = parse_date(as_of, "巡检日期") if as_of else utcnow().date()
        findings = engine.scan_cases(
            self.store.all_versions(), self.store.all_intervals(),
            as_of_day=as_of_day, horizon_days=int(horizon_days),
        )
        created: list[dict] = []
        # 案件登记时刻固定为受控日当天结束，保证回放同日巡检结果一致
        created_at = datetime.combine(as_of_day, time.max, tzinfo=timezone.utc)
        for mentor_id, code, reason in findings:
            if self.store.case_exists(mentor_id, as_of_day, code):
                continue
            case = Case(
                case_id=0, mentor_id=mentor_id, generated_for_day=as_of_day,
                reason_code=code, reason=reason, status="open",
                created_at=created_at,
            )
            self.store.add_case(case)
            created.append({"mentor_id": mentor_id, "reason_code": code, "reason": reason})
        return {"as_of_day": as_of_day.isoformat(), "generated": created,
                "generated_count": len(created)}

    def list_cases(self, actor: Actor, status: str | None = None) -> list[dict]:
        authz.require(actor.role, "case.list")
        if status not in (None, "open", "resolved", "ignored"):
            raise ServiceError("案件状态不合法")
        return [c.to_dict() for c in self.store.list_cases(status)]

    def resolve_case(self, actor: Actor, case_id: int, note: str,
                     ignore: bool = False) -> dict:
        authz.require(actor.role, "case.resolve")
        case = self.store.get_case(int(case_id))
        if case is None:
            raise ServiceError("案件不存在")
        if case.status != "open":
            raise ServiceError("案件已处理，不能重复处理")
        if not note:
            raise ServiceError("处理案件必须填写处置说明")
        self.store.resolve_case(
            case.case_id, actor.user_id,
            ("忽略：" if ignore else "处置：") + note, utcnow(),
        )
        return self.store.get_case(case.case_id).to_dict()  # type: ignore[union-attr]

    # ---- 个人材料读取（留痕） ----
    def read_material(self, actor: Actor, mentor_id: str) -> dict:
        allowed = authz.can_read_material(
            actor.role, actor_id=actor.user_id, mentor_id=mentor_id
        )
        self.store.log_access(
            actor.user_id, actor.role, mentor_id, "material.read", allowed, utcnow()
        )
        if not allowed:
            raise authz.AuthorizationError("无权读取该传承人的个人材料")
        mentor = self.store.get_mentor(mentor_id)
        if mentor is None:
            raise ServiceError("传承人不存在")
        return {
            "mentor": mentor.to_dict(),
            "versions": [v.to_dict() for v in self.store.list_versions(mentor_id)],
            "intervals": [iv.to_dict() for iv in self.store.list_intervals(mentor_id)],
        }

    def directory(self, actor: Actor) -> list[dict]:
        """排课老师可见的简名录：不含证件哈希、电话与培训记录。"""
        authz.require(actor.role, "directory.read")
        out = []
        for m in self.store.list_mentors():
            versions = self.store.list_versions(m.mentor_id)
            latest = versions[-1] if versions else None
            out.append({
                "mentor_id": m.mentor_id,
                "name": m.name,
                "latest_version": latest.version if latest else None,
                "valid_until": latest.valid_until.isoformat() if latest else None,
                "specialties": list(latest.specialties) if latest else [],
            })
        return out

    def access_log(self, actor: Actor) -> list[dict]:
        authz.require(actor.role, "access_log.read")
        return self.store.list_access_log()
