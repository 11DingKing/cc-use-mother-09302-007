"""应用服务：命令写入、预约/签到双快照、批量巡检。

所有写入经命令幂等（command_id）进入同一条只追加、带哈希链的事件日志：
资质变更、核验快照、巡检案件都是不可改写的事件。预约与签到各自固定
一份核验快照，互不覆盖；巡检以外部给定的“可控时间”生成案件，案件
记录依据事件序号，结论可随时复算解释。
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from . import permissions as perm
from .aggregate import Profile, build_profile, evaluate, make_snapshot, replay
from .clock import Clock, SystemClock, parse_iso, to_iso
from .errors import ConflictError, NotFoundError, ValidationError
from .eventlog import EventLog
from .models import (
    AddTraining,
    CredentialSnapshot,
    DecideAppeal,
    GrantExemption,
    RegisterProfile,
    RenewCredential,
    RevokeCredential,
    Role,
    VerificationSnapshot,
)

WARN_DAYS = 30


class QualificationService:
    def __init__(self, log: EventLog, clock: Clock | None = None):
        self._log = log
        self._clock = clock or SystemClock()

    # -- 写入：档案与资质 ---------------------------------------------------

    def register(self, role: Role, cmd: RegisterProfile) -> dict[str, Any]:
        perm.require(role, perm.PROFILE_WRITE, "登记传承人资质")
        self._validate_interval(cmd.valid_from, cmd.valid_to)
        if not cmd.specialties:
            raise ValidationError("擅长项目不能为空")
        if not (0 <= cmd.age_min <= cmd.age_max):
            raise ValidationError("适用年龄区间不合法")
        existing = build_profile(self._log.events, cmd.mentor_id)
        if existing.registered:
            raise ConflictError(f"传承人已登记：{cmd.mentor_id}")
        payload = {
            "name": cmd.name,
            "id_tail": cmd.id_tail,
            "specialties": list(cmd.specialties),
            "age_min": cmd.age_min,
            "age_max": cmd.age_max,
            "credential_source": cmd.credential_source,
            "credential_no": cmd.credential_no,
            "valid_from": to_iso(parse_iso(cmd.valid_from)),
            "valid_to": to_iso(parse_iso(cmd.valid_to)),
            "trainings": [self._normalize_training(item) for item in cmd.trainings],
        }
        event = self._log.append(
            "档案登记", cmd.mentor_id, payload, actor=cmd.actor, command_id=cmd.command_id
        )
        return {"mentor_id": cmd.mentor_id, "version": 1, "event_seq": event.seq}

    def add_training(self, role: Role, cmd: AddTraining) -> dict[str, Any]:
        perm.require(role, perm.PROFILE_WRITE, "追加培训记录")
        self._require_registered(cmd.mentor_id)
        payload = self._normalize_training(
            {"topic": cmd.topic, "trained_on": cmd.trained_on}
        )
        event = self._log.append(
            "培训记录追加", cmd.mentor_id, payload, actor=cmd.actor, command_id=cmd.command_id
        )
        return {"mentor_id": cmd.mentor_id, "event_seq": event.seq}

    def renew(self, role: Role, cmd: RenewCredential) -> dict[str, Any]:
        """续证：只影响 [start, end] 区间，并生成新的不可改写版本。"""
        perm.require(role, perm.PROFILE_WRITE, "证明续期")
        profile = self._require_registered(cmd.mentor_id)
        self._validate_interval(cmd.start, cmd.end)
        version = len(profile.versions) + 1
        payload = {
            "version": version,
            "credential_source": cmd.credential_source,
            "credential_no": cmd.credential_no,
            "valid_from": to_iso(parse_iso(cmd.start)),
            "valid_to": to_iso(parse_iso(cmd.end)),
            "reason": cmd.reason,
        }
        event = self._log.append(
            "证明续期", cmd.mentor_id, payload, actor=cmd.actor, command_id=cmd.command_id
        )
        return {"mentor_id": cmd.mentor_id, "version": version, "event_seq": event.seq}

    def revoke(self, role: Role, cmd: RevokeCredential) -> dict[str, Any]:
        """撤销：只影响 [start, end] 规定区间（end 为空表示开放区间）。"""
        perm.require(role, perm.PROFILE_WRITE, "撤销证明")
        self._require_registered(cmd.mentor_id)
        payload = {
            "start": to_iso(parse_iso(cmd.start)),
            "end": to_iso(parse_iso(cmd.end)) if cmd.end else None,
            "reason": cmd.reason,
        }
        event = self._log.append(
            "证明撤销", cmd.mentor_id, payload, actor=cmd.actor, command_id=cmd.command_id
        )
        return {"mentor_id": cmd.mentor_id, "event_seq": event.seq}

    def grant_exemption(self, role: Role, cmd: GrantExemption) -> dict[str, Any]:
        """限时豁免：仅在 [start, end] 内豁免指定检查项。"""
        perm.require(role, perm.PROFILE_WRITE, "授予限时豁免")
        self._require_registered(cmd.mentor_id)
        self._validate_interval(cmd.start, cmd.end)
        payload = {
            "start": to_iso(parse_iso(cmd.start)),
            "end": to_iso(parse_iso(cmd.end)),
            "scope": cmd.scope,
            "reason": cmd.reason,
        }
        event = self._log.append(
            "豁免授予", cmd.mentor_id, payload, actor=cmd.actor, command_id=cmd.command_id
        )
        return {"mentor_id": cmd.mentor_id, "event_seq": event.seq}

    def decide_appeal(self, role: Role, cmd: DecideAppeal) -> dict[str, Any]:
        """申诉裁决：支持时仅在规定区间内生效，驳回不留任何豁免。"""
        perm.require(role, perm.PROFILE_WRITE, "裁决申诉")
        self._require_registered(cmd.mentor_id)
        if cmd.upheld:
            self._validate_interval(cmd.start, cmd.end)
        payload = {
            "appeal_id": cmd.appeal_id,
            "upheld": cmd.upheld,
            "start": to_iso(parse_iso(cmd.start)) if cmd.upheld else None,
            "end": to_iso(parse_iso(cmd.end)) if cmd.upheld else None,
            "scope": cmd.scope if cmd.upheld else None,
            "note": cmd.note,
        }
        event = self._log.append(
            "申诉裁决", cmd.mentor_id, payload, actor=cmd.actor, command_id=cmd.command_id
        )
        return {"mentor_id": cmd.mentor_id, "upheld": cmd.upheld, "event_seq": event.seq}

    # -- 预约 / 签到：各自固定核验快照 --------------------------------------

    def verify(
        self,
        role: Role,
        kind: str,
        mentor_id: str,
        activity_id: str,
        activity_specialty: str,
        audience_age: int,
        at: str | None = None,
        actor: str = "",
        command_id: str = "",
    ) -> dict[str, Any]:
        perm.require(role, perm.VERIFICATION_CREATE, f"{kind}核验")
        if kind not in ("预约", "签到"):
            raise ValidationError("核验类型必须是预约或签到")
        moment = to_iso(parse_iso(at)) if at else to_iso(self._clock.now())
        for existing in replay_snapshots(self._log.events):
            if (
                existing.kind == kind
                and existing.activity_id == activity_id
                and existing.mentor_id == mentor_id
            ):
                raise ConflictError(f"{kind}快照已固定，不可再次核验：{activity_id}/{mentor_id}")
        basis = self._log.head_seq()
        profile = build_profile(self._log.events, mentor_id)
        decision = evaluate(profile, moment, activity_specialty, audience_age, basis)
        if not profile.registered:
            snapshot = CredentialSnapshot(
                version=0,
                specialties=(),
                age_min=0,
                age_max=0,
                id_tail="",
                credential_source="",
                credential_no="",
                valid_from="",
                valid_to="",
                revoked_intervals=(),
                exemption_intervals=(),
                trainings=(),
                data_event_seq=basis,
            )
        else:
            snapshot = make_snapshot(profile, moment, basis)
        record = VerificationSnapshot(
            kind=kind,
            at=moment,
            activity_id=activity_id,
            mentor_id=mentor_id,
            credential=snapshot,
            decision=decision,
            event_seq=basis,
        )
        # 快照本身作为不可改写事件固定；预约与签到是两条独立事件。
        event = self._log.append(
            f"{kind}核验",
            mentor_id,
            record.to_dict(),
            actor=actor,
            command_id=command_id,
        )
        rendered = self._render_snapshot(record, role)
        rendered["event_seq"] = event.seq
        return rendered

    def list_snapshots(self, role: Role, activity_id: str | None = None) -> list[dict[str, Any]]:
        perm.require(role, perm.DECISION_READ, "读取核验快照")
        records = replay_snapshots(self._log.events)
        if activity_id is not None:
            records = [s for s in records if s.activity_id == activity_id]
        return [self._render_snapshot(item, role) for item in records]

    def explain(self, role: Role, kind: str, activity_id: str, mentor_id: str) -> dict[str, Any]:
        """解释一次放行或拒绝的依据：取回当时固定的快照（最新一次为准）。"""
        perm.require(role, perm.DECISION_READ, "解释核验依据")
        for snap in reversed(replay_snapshots(self._log.events)):
            if (
                snap.kind == kind
                and snap.activity_id == activity_id
                and snap.mentor_id == mentor_id
            ):
                return self._render_snapshot(snap, role)
        raise NotFoundError(f"未找到{kind}快照：{activity_id}/{mentor_id}")

    # -- 读取：个人材料（仅管理员） -----------------------------------------

    def get_profile(self, role: Role, mentor_id: str) -> dict[str, Any]:
        perm.require(role, perm.PERSONAL_MATERIAL_READ, "读取个人材料")
        profile = self._require_registered(mentor_id)
        latest = profile.latest_version()
        return {
            "mentor_id": profile.mentor_id,
            "name": profile.name,
            "id_tail": profile.id_tail,
            "specialties": list(profile.specialties),
            "age_min": profile.age_min,
            "age_max": profile.age_max,
            "credential": {
                "version": latest.version,
                "source": latest.credential_source,
                "no": latest.credential_no,
                "valid_from": latest.valid_from,
                "valid_to": latest.valid_to,
            },
            "trainings": list(profile.trainings),
            "revocations": list(profile.revocations),
            "exemptions": list(profile.exemptions),
            "appeals": list(profile.appeals),
        }

    # -- 批量巡检：可控时间生成待办案件 -------------------------------------

    def run_inspection(self, role: Role, at: str, warn_days: int = WARN_DAYS) -> list[dict[str, Any]]:
        """以指定时间为基准批量巡检；同一档案同类待办案件不重复生成。"""
        perm.require(role, perm.INSPECTION_WRITE, "批量巡检")
        from .policy import Interval

        moment = parse_iso(at)
        warn_at = moment + timedelta(days=warn_days)
        generated = to_iso(self._clock.now())
        created: list[dict[str, Any]] = []
        basis = self._log.head_seq()
        profiles = replay(self._log.events)
        existing_open = {
            (case["mentor_id"], case["kind"])
            for case in replay_cases(self._log.events)
            if case["status"] == "待办"
        }
        for mentor_id in sorted(profiles):
            profile = profiles[mentor_id]
            if not profile.registered:
                continue
            kind, detail, case_key = self._inspect_one(profile, moment, warn_at, Interval)
            if kind is None:
                continue
            if (mentor_id, kind) in existing_open:
                continue
            payload = {
                "case_id": f"INSPECT-{mentor_id}-{case_key}-{basis}",
                "kind": kind,
                "detail": detail,
                "generated_at": generated,
                "effective_at": to_iso(moment),
                "basis_event_seq": basis,
            }
            event = self._log.append("巡检案件生成", mentor_id, payload)
            created.append(
                {
                    **payload,
                    "mentor_id": mentor_id,
                    "status": "待办",
                    "resolved_at": None,
                    "note": "",
                    "event_seq": event.seq,
                }
            )
        return created

    def list_cases(self, role: Role, status: str | None = None) -> list[dict[str, Any]]:
        perm.require(role, perm.CASE_WRITE, "读取巡检案件")
        cases = replay_cases(self._log.events)
        if status:
            cases = [c for c in cases if c["status"] == status]
        return cases

    def resolve_case(self, role: Role, case_id: str, note: str = "") -> dict[str, Any]:
        perm.require(role, perm.CASE_WRITE, "处置巡检案件")
        target = None
        for case in replay_cases(self._log.events):
            if case["case_id"] == case_id:
                target = case
        if target is None:
            raise NotFoundError(f"未找到案件：{case_id}")
        if target["status"] != "待办":
            raise ConflictError(f"案件已处置：{case_id}")
        event = self._log.append(
            "巡检案件处置",
            target["mentor_id"],
            {"case_id": case_id, "resolved_at": to_iso(self._clock.now()), "note": note},
        )
        return {**target, "status": "已处置", "resolved_at": event.payload["resolved_at"], "note": note}

    # -- 完整性 -------------------------------------------------------------

    def verify_integrity(self) -> None:
        """重放哈希链，任何历史改写都会抛出 TamperError。"""
        self._log.verify_chain()

    # -- 内部辅助 -----------------------------------------------------------

    def _inspect_one(self, profile: Profile, moment, warn_at, interval_cls) -> tuple[str | None, str, str]:
        if profile.revoked_at(moment):
            reason = next(
                (
                    item.get("reason", "")
                    for item in profile.revocations
                    if interval_cls(item["start"], item.get("end")).covers(moment)
                ),
                "",
            )
            return "撤销待处理", f"证明处于撤销区间：{reason}", "REVOKE"
        version = profile.valid_version_at(moment)
        if version is None:
            return "已过期", "证明已过有效期且无有效版本", "EXPIRED"
        if parse_iso(version.valid_to) <= warn_at:
            return "到期预警", f"证明将于 {version.valid_to} 到期", "EXPIRING"
        return None, "", ""

    def _require_registered(self, mentor_id: str) -> Profile:
        profile = build_profile(self._log.events, mentor_id)
        if not profile.registered:
            raise NotFoundError(f"传承人未登记：{mentor_id}")
        return profile

    def _render_snapshot(self, snap: VerificationSnapshot, role: Role) -> dict[str, Any]:
        credential = snap.credential.to_dict()
        if role is Role.COORDINATOR:
            credential = perm.redact_credential(credential)
        return {
            "kind": snap.kind,
            "at": snap.at,
            "activity_id": snap.activity_id,
            "mentor_id": snap.mentor_id,
            "credential": credential,
            "decision": snap.decision.to_dict(),
            "event_seq": snap.event_seq,
        }

    @staticmethod
    def _normalize_training(item: dict[str, Any]) -> dict[str, Any]:
        normalized = {
            "topic": item["topic"],
            "trained_on": to_iso(parse_iso(item["trained_on"])),
        }
        if item.get("valid_through"):
            normalized["valid_through"] = to_iso(parse_iso(item["valid_through"]))
        return normalized

    @staticmethod
    def _validate_interval(start: str, end: str) -> None:
        if parse_iso(end) <= parse_iso(start):
            raise ValidationError("有效期结束必须晚于开始")


# --- 只读投影：从事件流重放快照与案件 ---------------------------------------


def replay_snapshots(events) -> list[VerificationSnapshot]:
    records: list[VerificationSnapshot] = []
    for event in events:
        if event.kind in ("预约核验", "签到核验"):
            payload = event.payload
            record = VerificationSnapshot(
                kind=payload["kind"],
                at=payload["at"],
                activity_id=payload["activity_id"],
                mentor_id=event.mentor_id,
                credential=CredentialSnapshot.from_dict(payload["credential"]),
                decision=_decision_from_dict(payload["decision"]),
                event_seq=event.seq,
            )
            records.append(record)
    return records


def replay_cases(events) -> list[dict[str, Any]]:
    cases: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.kind == "巡检案件生成":
            payload = event.payload
            cases[payload["case_id"]] = {
                "case_id": payload["case_id"],
                "mentor_id": event.mentor_id,
                "kind": payload["kind"],
                "detail": payload["detail"],
                "generated_at": payload["generated_at"],
                "effective_at": payload["effective_at"],
                "basis_event_seq": payload["basis_event_seq"],
                "status": "待办",
                "resolved_at": None,
                "note": "",
            }
        elif event.kind == "巡检案件处置":
            case = cases.get(event.payload["case_id"])
            if case is not None:
                case["status"] = "已处置"
                case["resolved_at"] = event.payload["resolved_at"]
                case["note"] = event.payload.get("note", "")
    return list(cases.values())


def _decision_from_dict(value: dict[str, Any]):
    from .models import Decision

    return Decision(
        result=value["result"],
        reasons=tuple(value["reasons"]),
        waived=tuple(value["waived"]),
        checked_at=value["checked_at"],
        activity_specialty=value["activity_specialty"],
        audience_age=value["audience_age"],
        basis_event_seq=value["basis_event_seq"],
    )
