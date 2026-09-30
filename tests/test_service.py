"""服务层端到端：登记、版本追加、预约/签到快照、巡检、权限留痕。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qualification.authz import AuthorizationError
from qualification.service import Actor, QualificationService, ServiceError
from qualification.store import Store

ADMIN = Actor("admin1", "admin", "管理员")
TEACHER = Actor("teacher1", "scheduler", "排课老师")
MENTOR = Actor("m1", "mentor", "王剪纸")
STAFF = Actor("x9", "staff", "普通人员")


def new_service() -> QualificationService:
    return QualificationService(Store(":memory:"))


def seed(svc: QualificationService) -> None:
    svc.register_mentor(ADMIN, "m1", "王剪纸", "id-001", phone="13800000000")
    svc.add_qualification_version(
        ADMIN, "m1", specialties=["剪纸"], age_min=6, age_max=15,
        training_records=["市级非遗培训2025"], cert_source="authority",
        cert_no="C-2026-001", valid_from="2026-01-01", valid_until="2026-06-30",
        at_time="2026-01-02T09:00:00+00:00",
    )


class VersionImmutabilityTest(unittest.TestCase):
    def test_versions_are_append_only(self):
        svc = new_service()
        seed(svc)
        v2 = svc.add_qualification_version(
            ADMIN, "m1", specialties=["剪纸", "年画"], age_min=6, age_max=18,
            training_records=["省培2026"], cert_source="school",
            cert_no="C-2026-002", valid_from="2026-09-01", valid_until="2027-08-31",
            at_time="2026-09-01T09:00:00+00:00",
        )
        self.assertEqual(v2["version"], 2)
        versions = svc.store.list_versions("m1")
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0].cert_no, "C-2026-001")  # v1 原样保留
        self.assertEqual(versions[1].specialties, ("剪纸", "年画"))

    def test_re_register_rejected(self):
        svc = new_service()
        seed(svc)
        with self.assertRaises(ServiceError):
            svc.register_mentor(ADMIN, "m1", "改名", "id-002")

    def test_invalid_interval_rejected(self):
        svc = new_service()
        seed(svc)
        with self.assertRaises(ServiceError):
            svc.add_interval(ADMIN, "m1", "revocation", "2026-10-01", "2026-09-01", "x")
        with self.assertRaises(ServiceError):
            svc.add_interval(ADMIN, "m1", "unknown", "2026-09-01", "2026-09-30", "x")


class SnapshotScenarioTest(unittest.TestCase):
    def test_accident_scenario_booking_allow_then_checkin_deny(self):
        """复现事故：预约时资质有效并固定放行依据，之后出现撤销这一新事实，
        签到独立核验固定为拒绝。两张快照互不改写，管理员既能解释此前为何放行，
        也能解释当天为何拒绝。
        """
        svc = new_service()
        seed(svc)
        booking = svc.verify(
            TEACHER, "booking", "m1", specialty="剪纸", audience_age=10,
            activity_day="2026-06-15", at_time="2026-03-01T10:00:00+00:00",
        )
        self.assertEqual(booking["decision"], "allow")
        self.assertEqual(booking["version"], 1)

        # 预约后、活动前被举报核查，管理员在 6-09 登记撤销区间
        svc.add_interval(ADMIN, "m1", "revocation", "2026-06-10", "2026-06-20",
                         "材料造假，撤销进校资格",
                         at_time="2026-06-09T17:00:00+00:00")

        checkin = svc.verify(
            TEACHER, "checkin", "m1", specialty="剪纸", audience_age=10,
            activity_day="2026-06-15", at_time="2026-06-15T08:30:00+00:00",
        )
        self.assertEqual(checkin["decision"], "deny")
        self.assertTrue(any("撤销" in r for r in checkin["reasons"]))

        # 撤销区间过后再补录新版本，也不改变既有快照内容
        svc.add_qualification_version(
            ADMIN, "m1", specialties=["剪纸"], age_min=6, age_max=15,
            training_records=["市培2026"], cert_source="authority",
            cert_no="C-2026-009", valid_from="2026-09-10", valid_until="2027-09-09",
            at_time="2026-09-20T00:00:00+00:00",
        )
        stored_booking = svc.get_snapshot(ADMIN, booking["snapshot_id"])
        stored_checkin = svc.get_snapshot(ADMIN, checkin["snapshot_id"])
        self.assertEqual(stored_booking["decision"], "allow")
        self.assertEqual(stored_checkin["decision"], "deny")
        self.assertEqual(stored_checkin["reasons"], checkin["reasons"])

    def test_booking_rejects_activity_day_beyond_expiry(self):
        """事故防线：排课时活动日已超出证明有效期，预约阶段就必须拒绝并说明。"""
        svc = new_service()
        seed(svc)
        booking = svc.verify(
            TEACHER, "booking", "m1", specialty="剪纸", audience_age=10,
            activity_day="2026-09-15", at_time="2026-06-01T10:00:00+00:00",
        )
        self.assertEqual(booking["decision"], "deny")
        self.assertTrue(any("过期" in r for r in booking["reasons"]))

    def test_exemption_limited_to_interval(self):
        svc = new_service()
        seed(svc)
        svc.add_interval(ADMIN, "m1", "exemption", "2026-09-14", "2026-09-16",
                         "展映急需，证明补办中",
                         at_time="2026-09-13T12:00:00+00:00")
        inside = svc.verify(
            TEACHER, "checkin", "m1", specialty="剪纸", audience_age=10,
            activity_day="2026-09-15", at_time="2026-09-15T08:00:00+00:00",
        )
        outside = svc.verify(
            TEACHER, "checkin", "m1", specialty="剪纸", audience_age=10,
            activity_day="2026-09-17", at_time="2026-09-17T08:00:00+00:00",
        )
        self.assertEqual(inside["decision"], "allow")
        self.assertEqual(outside["decision"], "deny")

    def test_revocation_denies_even_with_exemption(self):
        svc = new_service()
        seed(svc)
        svc.add_interval(ADMIN, "m1", "exemption", "2026-03-01", "2026-12-31", "豁免",
                         at_time="2026-02-01T00:00:00+00:00")
        svc.add_interval(ADMIN, "m1", "revocation", "2026-09-01", "2026-09-30", "违规撤销",
                         at_time="2026-08-31T00:00:00+00:00")
        r = svc.verify(
            TEACHER, "checkin", "m1", specialty="剪纸", audience_age=10,
            activity_day="2026-09-15", at_time="2026-09-15T08:00:00+00:00",
        )
        self.assertEqual(r["decision"], "deny")

    def test_scheduler_cannot_register(self):
        svc = new_service()
        with self.assertRaises(AuthorizationError):
            svc.register_mentor(TEACHER, "m2", "新人", "id-2")


class InspectionTest(unittest.TestCase):
    def test_controlled_time_scan_is_idempotent_and_explainable(self):
        svc = new_service()
        seed(svc)
        first = svc.run_inspection(ADMIN, as_of="2026-09-15")
        self.assertEqual(first["generated_count"], 1)
        self.assertEqual(first["generated"][0]["reason_code"], "expired")

        # 重复巡检同日不重复生成案件
        second = svc.run_inspection(ADMIN, as_of="2026-09-15")
        self.assertEqual(second["generated_count"], 0)
        cases = svc.list_cases(ADMIN, status="open")
        self.assertEqual(len(cases), 1)
        self.assertIn("过期", cases[0]["reason"])

        # 处理案件必须留说明
        with self.assertRaises(ServiceError):
            svc.resolve_case(ADMIN, cases[0]["case_id"], "")
        resolved = svc.resolve_case(ADMIN, cases[0]["case_id"], "已通知补办并暂停排课")
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["handled_by"], "admin1")

    def test_near_expiry_warning(self):
        svc = new_service()
        svc.register_mentor(ADMIN, "m2", "李年画", "id-002")
        svc.add_qualification_version(
            ADMIN, "m2", specialties=["年画"], age_min=6, age_max=12,
            training_records=["培训"], cert_source="school", cert_no="N1",
            valid_from="2026-01-01", valid_until="2026-10-05",
            at_time="2026-01-02T09:00:00+00:00",
        )
        result = svc.run_inspection(ADMIN, as_of="2026-09-15")
        self.assertEqual(result["generated"][0]["reason_code"], "near_expiry")

    def test_teacher_cannot_run_inspection(self):
        svc = new_service()
        seed(svc)
        with self.assertRaises(AuthorizationError):
            svc.run_inspection(TEACHER, as_of="2026-09-15")


class PrivacyTest(unittest.TestCase):
    def test_staff_cannot_read_material_and_attempt_is_logged(self):
        svc = new_service()
        seed(svc)
        with self.assertRaises(AuthorizationError):
            svc.read_material(STAFF, "m1")
        log = svc.access_log(ADMIN)
        denied = [r for r in log if r["allowed"] == 0]
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["actor_id"], "x9")

    def test_teacher_sees_directory_but_not_material(self):
        svc = new_service()
        seed(svc)
        directory = svc.directory(TEACHER)
        self.assertEqual(directory[0]["name"], "王剪纸")
        self.assertNotIn("id_digest", directory[0])
        self.assertNotIn("phone", directory[0])
        with self.assertRaises(AuthorizationError):
            svc.read_material(TEACHER, "m1")

    def test_mentor_reads_only_own_material(self):
        svc = new_service()
        seed(svc)
        svc.register_mentor(ADMIN, "m2", "李年画", "id-002")
        own = svc.read_material(MENTOR, "m1")
        self.assertEqual(own["mentor"]["mentor_id"], "m1")
        with self.assertRaises(AuthorizationError):
            svc.read_material(MENTOR, "m2")

    def test_unknown_user_is_staff(self):
        svc = new_service()
        seed(svc)
        ghost = svc.actor_for("nobody")
        self.assertEqual(ghost.role, "staff")
        with self.assertRaises(AuthorizationError):
            svc.read_material(ghost, "m1")

    def test_snapshot_access_restricted_and_logged(self):
        svc = new_service()
        seed(svc)
        snap = svc.verify(
            TEACHER, "booking", "m1", specialty="剪纸", audience_age=10,
            activity_day="2026-03-01", at_time="2026-02-01T10:00:00+00:00",
        )
        # 本人可读自己的快照
        self.assertEqual(svc.get_snapshot(MENTOR, snap["snapshot_id"])["decision"], "allow")
        # 普通人员不可读，拒绝也留痕
        with self.assertRaises(AuthorizationError):
            svc.get_snapshot(STAFF, snap["snapshot_id"])


if __name__ == "__main__":
    unittest.main()
