"""传承人资质巡检服务端的端到端回归测试。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qualification.aggregate import build_profile, evaluate
from qualification.clock import FixedClock
from qualification.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    TamperError,
    ValidationError,
)
from qualification.eventlog import EventLog
from qualification.models import (
    AddTraining,
    DecideAppeal,
    GrantExemption,
    RegisterProfile,
    RenewCredential,
    RevokeCredential,
    Role,
)
from qualification.server import build_service
from qualification.service import QualificationService

ADMIN = Role.ADMIN
COORD = Role.COORDINATOR
STAFF = Role.STAFF

T0 = "2026-09-01T08:00:00Z"
T_BOOK = "2026-09-20T08:00:00Z"
T_EXPIRY = "2026-10-01T00:00:00Z"
T_CHECKIN = "2026-10-05T08:00:00Z"


def make_service(tmpdir: str, start: str = T0) -> tuple[QualificationService, FixedClock]:
    clock = FixedClock(datetime.fromisoformat(start.replace("Z", "+00:00")))
    log = EventLog(Path(tmpdir) / "events.jsonl", clock=clock)
    return QualificationService(log, clock=clock), clock


def register_valid(svc, mentor="M001", valid_from=T0, valid_to=T_EXPIRY, trainings=None):
    if trainings is None:
        trainings = [{"topic": "校园安全", "trained_on": "2026-08-01T00:00:00Z"}]
    return svc.register(
        ADMIN,
        RegisterProfile(
            mentor_id=mentor,
            name="张三",
            id_tail="1234",
            specialties=("剪纸",),
            age_min=6,
            age_max=15,
            credential_source="市非遗保护中心",
            credential_no="CR-2026-001",
            valid_from=valid_from,
            valid_to=valid_to,
            trainings=tuple(trainings),
            actor="管理员李",
            command_id=f"reg-{mentor}",
        ),
    )


class ScenarioTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.svc, self.clock = make_service(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- 事故复现：预约放行、签到时证明已过期，各自快照独立固定 --------------

    def test_booking_approved_but_checkin_denied_after_expiry(self) -> None:
        register_valid(
            self.svc,
            trainings=[{"topic": "校园安全", "trained_on": "2026-08-01T00:00:00Z"}],
        )
        booking = self.svc.verify(
            COORD, "预约", "M001", "ACT-1", "剪纸", 10, at=T_BOOK, actor="排课王"
        )
        self.assertEqual(booking["decision"]["result"], "放行")
        self.assertEqual(booking["decision"]["reasons"], [])

        checkin = self.svc.verify(
            COORD, "签到", "M001", "ACT-1", "剪纸", 10, at=T_CHECKIN
        )
        self.assertEqual(checkin["decision"]["result"], "拒绝")
        self.assertIn("证明缺失或已过期", checkin["decision"]["reasons"])

        # 预约快照仍记录当时的放行依据，不随证明过期被改写。
        explained = self.svc.explain(ADMIN, "预约", "ACT-1", "M001")
        self.assertEqual(explained["decision"]["result"], "放行")
        self.assertEqual(explained["credential"]["valid_to"], T_EXPIRY)
        self.assertEqual(explained["credential"]["version"], 1)
        self.assertEqual(explained["decision"]["basis_event_seq"], booking["decision"]["basis_event_seq"])

        # 同一类型快照不能第二次固定。
        with self.assertRaises(ConflictError):
            self.svc.verify(COORD, "预约", "M001", "ACT-1", "剪纸", 10, at=T_BOOK)

    def test_checkin_fails_on_specialty_and_age(self) -> None:
        register_valid(self.svc, trainings=[])
        denied = self.svc.verify(
            COORD, "预约", "M001", "ACT-2", "昆曲", 17, at=T_BOOK
        )
        self.assertEqual(denied["decision"]["result"], "拒绝")
        reasons = set(denied["decision"]["reasons"])
        self.assertIn("擅长项目与活动项目不匹配", reasons)
        self.assertIn("适用年龄不覆盖排课对象", reasons)
        self.assertIn("缺少覆盖该日期的培训记录", reasons)

    # -- 不可改写版本 -------------------------------------------------------

    def test_renewal_creates_new_version_and_old_snapshot_kept(self) -> None:
        register_valid(self.svc)
        self.svc.verify(COORD, "预约", "M001", "ACT-1", "剪纸", 10, at=T_BOOK)
        self.svc.renew(
            ADMIN,
            RenewCredential(
                mentor_id="M001",
                credential_source="省非遗保护中心",
                credential_no="CR-2027-009",
                start="2026-10-15T00:00:00Z",
                end="2027-10-01T00:00:00Z",
                reason="年度续证",
                command_id="renew-1",
            ),
        )
        # 旧证 10-01 到期、新证 10-15 才生效：区间空隙内仍被拒绝。
        gap = self.svc.verify(COORD, "签到", "M001", "ACT-1", "剪纸", 10, at="2026-10-02T08:00:00Z")
        self.assertEqual(gap["decision"]["result"], "拒绝")
        self.assertIn("证明缺失或已过期", gap["decision"]["reasons"])
        after = self.svc.verify(
            COORD, "签到", "M001", "ACT-9", "剪纸", 10, at="2026-10-16T08:00:00Z"
        )
        self.assertEqual(after["decision"]["result"], "放行")
        self.assertEqual(after["credential"]["version"], 2)
        # 排课老师看到打码证件号；管理员解释时可见完整来源与编号。
        self.assertEqual(after["credential"]["credential_no"], "******")
        admin_after = self.svc.explain(ADMIN, "签到", "ACT-9", "M001")
        self.assertEqual(admin_after["credential"]["credential_no"], "CR-2027-009")
        self.assertEqual(admin_after["credential"]["credential_source"], "省非遗保护中心")

        # 老预约快照仍然固定在版本 1。
        explained = self.svc.explain(ADMIN, "预约", "ACT-1", "M001")
        self.assertEqual(explained["credential"]["version"], 1)

        # 当前档案显示最新版本与版本链。
        profile = self.svc.get_profile(ADMIN, "M001")
        self.assertEqual(profile["credential"]["version"], 2)

    # -- 撤销只影响规定区间 -------------------------------------------------

    def test_revocation_interval_scoped(self) -> None:
        register_valid(self.svc)
        self.svc.revoke(
            ADMIN,
            RevokeCredential(
                mentor_id="M001",
                start="2026-09-15T00:00:00Z",
                end="2026-09-25T00:00:00Z",
                reason="材料核查",
                command_id="revoke-1",
            ),
        )
        during = self.svc.verify(
            COORD, "预约", "M001", "ACT-A", "剪纸", 10, at="2026-09-20T08:00:00Z"
        )
        self.assertIn("证明已被撤销", during["decision"]["reasons"])
        after = self.svc.verify(
            COORD, "签到", "M001", "ACT-A", "剪纸", 10, at="2026-09-26T08:00:00Z"
        )
        self.assertNotIn("证明已被撤销", after["decision"]["reasons"])

    def test_open_ended_revocation_persists(self) -> None:
        register_valid(self.svc)
        self.svc.revoke(
            ADMIN,
            RevokeCredential(
                mentor_id="M001", start=T_BOOK, end=None, reason="发现造假", command_id="revoke-2"
            ),
        )
        denied = self.svc.verify(
            COORD, "预约", "M001", "ACT-B", "剪纸", 10, at="2027-01-01T00:00:00Z"
        )
        self.assertIn("证明已被撤销", denied["decision"]["reasons"])

    # -- 限时豁免与申诉 -----------------------------------------------------

    def test_timed_exemption_only_inside_window(self) -> None:
        register_valid(self.svc)
        self.svc.grant_exemption(
            ADMIN,
            GrantExemption(
                mentor_id="M001",
                start="2026-10-01T00:00:00Z",
                end="2026-10-10T00:00:00Z",
                scope="证明缺失或已过期",
                reason="续证办理中",
                command_id="ex-1",
            ),
        )
        inside = self.svc.verify(
            COORD, "签到", "M001", "ACT-C", "剪纸", 10, at="2026-10-05T08:00:00Z"
        )
        self.assertEqual(inside["decision"]["result"], "放行")
        self.assertIn("证明缺失或已过期", inside["decision"]["waived"])
        outside = self.svc.verify(
            COORD, "签到", "M001", "ACT-D", "剪纸", 10, at="2026-10-12T08:00:00Z"
        )
        self.assertEqual(outside["decision"]["result"], "拒绝")

    def test_appeal_upheld_is_scoped_rejected_grants_nothing(self) -> None:
        register_valid(self.svc)
        self.svc.decide_appeal(
            ADMIN,
            DecideAppeal(
                mentor_id="M001",
                appeal_id="AP-1",
                upheld=False,
                start="",
                end="",
                scope="",
                note="证据不足，驳回",
                command_id="ap-1",
            ),
        )
        denied = self.svc.verify(
            COORD, "预约", "M001", "ACT-E", "剪纸", 10, at=T_CHECKIN
        )
        self.assertEqual(denied["decision"]["result"], "拒绝")

        self.svc.decide_appeal(
            ADMIN,
            DecideAppeal(
                mentor_id="M001",
                appeal_id="AP-2",
                upheld=True,
                start="2026-10-01T00:00:00Z",
                end="2026-11-01T00:00:00Z",
                scope="证明缺失或已过期",
                note="确认为发证机关延误",
                command_id="ap-2",
            ),
        )
        approved = self.svc.verify(
            COORD, "签到", "M001", "ACT-F", "剪纸", 10, at="2026-10-08T08:00:00Z"
        )
        self.assertEqual(approved["decision"]["result"], "放行")
        self.assertIn("证明缺失或已过期", approved["decision"]["waived"])

    # -- 可控时间的批量巡检 -------------------------------------------------

    def test_inspection_at_controlled_time_is_deterministic(self) -> None:
        register_valid(self.svc)
        register_valid(
            self.svc,
            mentor="M002",
            valid_from="2025-09-01T00:00:00Z",
            valid_to="2026-09-01T00:00:00Z",
        )
        cases = self.svc.run_inspection(ADMIN, "2026-09-05T00:00:00Z")
        kinds = {c["mentor_id"]: c["kind"] for c in cases}
        self.assertEqual(kinds["M001"], "到期预警")
        self.assertEqual(kinds["M002"], "已过期")
        for case in cases:
            self.assertEqual(case["effective_at"], "2026-09-05T00:00:00Z")
            self.assertGreaterEqual(case["basis_event_seq"], 2)

        # 同基准重复巡检不产生重复待办；处置后再巡检可重新发现。
        again = self.svc.run_inspection(ADMIN, "2026-09-05T00:00:00Z")
        self.assertEqual(again, [])
        target = next(c for c in self.svc.list_cases(ADMIN) if c["mentor_id"] == "M002")
        self.svc.resolve_case(ADMIN, target["case_id"], note="已通知续证")
        regenerated = self.svc.run_inspection(ADMIN, "2026-09-05T00:00:00Z")
        self.assertEqual([c["mentor_id"] for c in regenerated], ["M002"])

        # 撤销区间产生“撤销待处理”，优先级最高。
        self.svc.revoke(
            ADMIN,
            RevokeCredential(
                mentor_id="M001",
                start="2026-09-01T00:00:00Z",
                end=None,
                reason="年检不合格",
                command_id="revoke-m1",
            ),
        )
        revoked_cases = self.svc.run_inspection(ADMIN, "2026-09-06T00:00:00Z")
        self.assertEqual(
            {(c["mentor_id"], c["kind"]) for c in revoked_cases},
            {("M001", "撤销待处理")},
        )

    # -- 权限边界 -----------------------------------------------------------

    def test_staff_cannot_read_personal_material_or_verify(self) -> None:
        register_valid(self.svc)
        with self.assertRaises(PermissionDenied):
            self.svc.get_profile(STAFF, "M001")
        with self.assertRaises(PermissionDenied):
            self.svc.run_inspection(STAFF, T_BOOK)
        with self.assertRaises(PermissionDenied):
            self.svc.verify(STAFF, "预约", "M001", "ACT-1", "剪纸", 10, at=T_BOOK)
        with self.assertRaises(PermissionDenied):
            self.svc.list_cases(STAFF)

    def test_coordinator_reads_redacted_snapshot(self) -> None:
        register_valid(self.svc)
        result = self.svc.verify(COORD, "预约", "M001", "ACT-1", "剪纸", 10, at=T_BOOK)
        self.assertEqual(result["credential"]["id_tail"], "******")
        self.assertEqual(result["credential"]["credential_no"], "******")
        with self.assertRaises(PermissionDenied):
            self.svc.get_profile(COORD, "M001")

        admin_view = self.svc.explain(ADMIN, "预约", "ACT-1", "M001")
        self.assertEqual(admin_view["credential"]["id_tail"], "1234")
        self.assertEqual(admin_view["credential"]["credential_no"], "CR-2026-001")

    def test_writes_require_admin(self) -> None:
        register_valid(self.svc)
        with self.assertRaises(PermissionDenied):
            self.svc.renew(
                COORD,
                RenewCredential(
                    mentor_id="M001",
                    credential_source="x",
                    credential_no="y",
                    start=T0,
                    end=T_EXPIRY,
                ),
            )
        with self.assertRaises(PermissionDenied):
            self.svc.add_training(
                STAFF, AddTraining(mentor_id="M001", topic="t", trained_on=T0)
            )

    # -- 写入约束与幂等 -----------------------------------------------------

    def test_validation_and_command_idempotency(self) -> None:
        with self.assertRaises(ValidationError):
            register_valid(self.svc, valid_from=T_EXPIRY, valid_to=T0)
        register_valid(self.svc)
        with self.assertRaises(ConflictError):
            register_valid(self.svc)

        def renew_once():
            self.svc.renew(
                ADMIN,
                RenewCredential(
                    mentor_id="M001",
                    credential_source="省非遗保护中心",
                    credential_no="CR-2027-009",
                    start="2026-10-01T00:00:00Z",
                    end="2027-10-01T00:00:00Z",
                    command_id="dup-1",
                ),
            )

        renew_once()
        # 相同 command_id 重放必须被拒绝，不能产生第二个版本。
        with self.assertRaises(ConflictError):
            renew_once()
        with self.assertRaises(NotFoundError):
            self.svc.get_profile(ADMIN, "NOPE")

    # -- 持久化重放与防篡改 -------------------------------------------------

    def test_reload_from_disk_replays_state(self) -> None:
        register_valid(self.svc)
        self.svc.verify(COORD, "预约", "M001", "ACT-1", "剪纸", 10, at=T_BOOK)
        self.svc.run_inspection(ADMIN, "2026-09-25T00:00:00Z")

        reloaded = build_service(self._tmp.name)
        profile = reloaded.get_profile(ADMIN, "M001")
        self.assertEqual(profile["name"], "张三")
        snaps = reloaded.list_snapshots(ADMIN)
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["decision"]["result"], "放行")
        cases = reloaded.list_cases(ADMIN)
        self.assertTrue(cases)
        reloaded.verify_integrity()

    def test_tampering_history_is_detected(self) -> None:
        register_valid(self.svc)
        path = Path(self._tmp.name) / "events.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        import json
        record = json.loads(lines[0])
        record["payload"]["credential_no"] = "FORGED"
        lines[0] = json.dumps(record, ensure_ascii=False, sort_keys=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(TamperError):
            EventLog(path)


class PureEvaluationTest(unittest.TestCase):
    def test_unregistered_denied(self) -> None:
        from qualification.aggregate import Profile

        decision = evaluate(Profile(mentor_id="X"), T_BOOK, "剪纸", 10, 0)
        self.assertEqual(decision.result, "拒绝")
        self.assertEqual(decision.reasons, ("未登记资质",))


if __name__ == "__main__":
    unittest.main()
