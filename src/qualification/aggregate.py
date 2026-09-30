"""档案聚合：把只追加事件流投影为当前档案，并生成核验快照。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .clock import parse_iso, to_iso
from .models import (
    CredentialSnapshot,
    Decision,
    DenialReason,
    Event,
    ReviewResult,
)
from .policy import Interval, any_covers, exemption_covers, training_covers, valid_through


@dataclass
class CredentialVersion:
    version: int
    credential_source: str
    credential_no: str
    valid_from: str
    valid_to: str
    event_seq: int


@dataclass
class Profile:
    mentor_id: str
    registered: bool = False
    name: str = ""
    id_tail: str = ""
    specialties: tuple[str, ...] = ()
    age_min: int = 0
    age_max: int = 0
    versions: list[CredentialVersion] = field(default_factory=list)
    trainings: list[dict[str, Any]] = field(default_factory=list)
    revocations: list[dict[str, Any]] = field(default_factory=list)
    exemptions: list[dict[str, Any]] = field(default_factory=list)
    appeals: list[dict[str, Any]] = field(default_factory=list)
    register_seq: int = 0

    def latest_version(self) -> CredentialVersion | None:
        return self.versions[-1] if self.versions else None

    def valid_version_at(self, moment: Any) -> CredentialVersion | None:
        """取在该时点处于有效期内的最新版本。"""
        for version in reversed(self.versions):
            if valid_through(version.valid_from, version.valid_to, moment):
                return version
        return None

    def revoked_at(self, moment: Any) -> bool:
        intervals = [
            Interval(item["start"], item.get("end"))
            for item in self.revocations
        ]
        return any_covers(intervals, moment)

    def exemption_scopes_at(self, moment: Any) -> set[str]:
        scopes: set[str] = set()
        for item in self.exemptions:
            interval = Interval(item["start"], item["end"])
            if interval.covers(moment):
                scopes.add(item["scope"])
        return scopes


def apply_event(profile: Profile, event: Event) -> None:
    payload = event.payload
    if event.kind == "档案登记":
        profile.registered = True
        profile.name = payload["name"]
        profile.id_tail = payload["id_tail"]
        profile.specialties = tuple(payload["specialties"])
        profile.age_min = payload["age_min"]
        profile.age_max = payload["age_max"]
        profile.register_seq = event.seq
        profile.versions.append(
            CredentialVersion(
                version=1,
                credential_source=payload["credential_source"],
                credential_no=payload["credential_no"],
                valid_from=payload["valid_from"],
                valid_to=payload["valid_to"],
                event_seq=event.seq,
            )
        )
        profile.trainings.extend(payload.get("trainings", []))
    elif event.kind == "培训记录追加":
        profile.trainings.append(
            {
                "topic": payload["topic"],
                "trained_on": payload["trained_on"],
                "valid_through": payload.get("valid_through"),
            }
        )
    elif event.kind == "证明续期":
        profile.versions.append(
            CredentialVersion(
                version=payload["version"],
                credential_source=payload["credential_source"],
                credential_no=payload["credential_no"],
                valid_from=payload["valid_from"],
                valid_to=payload["valid_to"],
                event_seq=event.seq,
            )
        )
    elif event.kind == "证明撤销":
        profile.revocations.append(
            {
                "start": payload["start"],
                "end": payload.get("end"),
                "reason": payload.get("reason", ""),
                "event_seq": event.seq,
            }
        )
    elif event.kind == "豁免授予":
        profile.exemptions.append(
            {
                "start": payload["start"],
                "end": payload["end"],
                "scope": payload["scope"],
                "reason": payload.get("reason", ""),
                "event_seq": event.seq,
            }
        )
    elif event.kind == "申诉裁决":
        profile.appeals.append(
            {
                "appeal_id": payload["appeal_id"],
                "upheld": payload["upheld"],
                "start": payload.get("start"),
                "end": payload.get("end"),
                "scope": payload.get("scope"),
                "note": payload.get("note", ""),
                "event_seq": event.seq,
            }
        )
        if payload["upheld"]:
            profile.exemptions.append(
                {
                    "start": payload["start"],
                    "end": payload["end"],
                    "scope": payload["scope"],
                    "reason": f"申诉支持：{payload['appeal_id']}",
                    "event_seq": event.seq,
                }
            )


def replay(events: Sequence[Event], mentor_id: str | None = None) -> dict[str, Profile]:
    profiles: dict[str, Profile] = {}
    for event in events:
        if mentor_id is not None and event.mentor_id != mentor_id:
            continue
        profile = profiles.setdefault(event.mentor_id, Profile(mentor_id=event.mentor_id))
        apply_event(profile, event)
    return profiles


def build_profile(events: Sequence[Event], mentor_id: str) -> Profile:
    return replay(events, mentor_id).get(mentor_id, Profile(mentor_id=mentor_id))


def make_snapshot(profile: Profile, moment: Any, basis_event_seq: int) -> CredentialSnapshot:
    version = profile.valid_version_at(moment) or profile.latest_version()
    if version is None:
        raise ValueError("档案没有任何证明版本")
    return CredentialSnapshot(
        version=version.version,
        specialties=profile.specialties,
        age_min=profile.age_min,
        age_max=profile.age_max,
        id_tail=profile.id_tail,
        credential_source=version.credential_source,
        credential_no=version.credential_no,
        valid_from=version.valid_from,
        valid_to=version.valid_to,
        revoked_intervals=tuple(
            (item["start"], item.get("end")) for item in profile.revocations
        ),
        exemption_intervals=tuple(profile.exemptions),
        trainings=tuple(profile.trainings),
        data_event_seq=basis_event_seq,
    )


def evaluate(
    profile: Profile,
    moment: Any,
    activity_specialty: str,
    audience_age: int,
    basis_event_seq: int,
) -> Decision:
    """在指定时点核验，返回完整放行/拒绝依据。"""
    failures: list[str] = []
    waived: list[str] = []
    moment_iso = to_iso(parse_iso(moment))

    def waived_or(scope: str) -> bool:
        for exemption in profile.exemptions:
            if exemption_covers(exemption, scope, moment_iso):
                if scope not in waived:
                    waived.append(scope)
                return True
        return False

    if not profile.registered:
        if not waived_or(DenialReason.UNREGISTERED.value):
            failures.append(DenialReason.UNREGISTERED.value)
        return Decision(
            result=ReviewResult.DENIED.value if failures else ReviewResult.APPROVED.value,
            reasons=tuple(failures),
            waived=tuple(waived),
            checked_at=moment_iso,
            activity_specialty=activity_specialty,
            audience_age=audience_age,
            basis_event_seq=basis_event_seq,
        )

    version = profile.valid_version_at(moment_iso)
    if version is None:
        if not waived_or(DenialReason.NO_VALID_CREDENTIAL.value):
            failures.append(DenialReason.NO_VALID_CREDENTIAL.value)
    if profile.revoked_at(moment_iso):
        if not waived_or(DenialReason.REVOKED.value):
            failures.append(DenialReason.REVOKED.value)
    if activity_specialty not in profile.specialties:
        if not waived_or(DenialReason.SPECIALTY_MISMATCH.value):
            failures.append(DenialReason.SPECIALTY_MISMATCH.value)
    if not (profile.age_min <= audience_age <= profile.age_max):
        if not waived_or(DenialReason.AGE_OUT_OF_RANGE.value):
            failures.append(DenialReason.AGE_OUT_OF_RANGE.value)
    if not any(training_covers(item, moment_iso) for item in profile.trainings):
        if not waived_or(DenialReason.TRAINING_REQUIRED.value):
            failures.append(DenialReason.TRAINING_REQUIRED.value)

    return Decision(
        result=ReviewResult.APPROVED.value if not failures else ReviewResult.DENIED.value,
        reasons=tuple(failures),
        waived=tuple(waived),
        checked_at=moment_iso,
        activity_specialty=activity_specialty,
        audience_age=audience_age,
        basis_event_seq=basis_event_seq,
    )
