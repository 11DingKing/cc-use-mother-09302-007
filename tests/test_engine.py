"""核验引擎的区间与版本语义测试。"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qualification import engine
from qualification.models import Interval, QualificationVersion

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def version(valid_from="2026-01-01", valid_until="2026-06-30", specialties=("剪纸",),
            age=(6, 15), no=1) -> QualificationVersion:
    return QualificationVersion(
        mentor_id="m1", version=no, specialties=specialties, age_range=age,
        training_records=("市级非遗培训2025",), cert_source="authority",
        cert_no=f"C{no}", valid_from=date.fromisoformat(valid_from),
        valid_until=date.fromisoformat(valid_until),
        created_at=T0, author="admin1",
    )


def iv(kind, start, end, iid=1) -> Interval:
    return Interval(
        interval_id=iid, mentor_id="m1", kind=kind,
        start=date.fromisoformat(start), end=date.fromisoformat(end),
        reason=f"{kind}原因", created_at=T0, author="admin1",
    )


def eval_at(day, versions=(), intervals=(), specialty="剪纸", age=10, at="2026-01-01T00:00:00+00:00"):
    return engine.evaluate(
        list(versions), list(intervals),
        evaluated_at=datetime.fromisoformat(at),
        day=date.fromisoformat(day),
        specialty=specialty, audience_age=age,
    )


class EngineTest(unittest.TestCase):
    def test_valid_allows(self):
        r = eval_at("2026-03-01", versions=[version()])
        self.assertEqual(r.decision, "allow")
        self.assertEqual(r.version, 1)

    def test_expired_denied_with_reason(self):
        # 核心事故：排课时有效，活动当日证明已过期
        r = eval_at("2026-09-15", versions=[version()])
        self.assertEqual(r.decision, "deny")
        self.assertTrue(any("过期" in x for x in r.reasons))

    def test_scope_mismatch_denied(self):
        r = eval_at("2026-03-01", versions=[version()], specialty="昆曲", age=10)
        self.assertEqual(r.decision, "deny")
        r2 = eval_at("2026-03-01", versions=[version()], specialty="剪纸", age=30)
        self.assertEqual(r2.decision, "deny")

    def test_renewal_only_covers_interval(self):
        r = eval_at("2026-09-15", versions=[version()],
                    intervals=[iv("renewal", "2026-07-01", "2026-09-30")])
        self.assertEqual(r.decision, "allow")
        # 区间外仍然拒绝
        r2 = eval_at("2026-10-01", versions=[version()],
                     intervals=[iv("renewal", "2026-07-01", "2026-09-30")])
        self.assertEqual(r2.decision, "deny")

    def test_exemption_waives_expiry_and_scope(self):
        r = eval_at("2026-09-15", versions=[version()], specialty="昆曲", age=30,
                    intervals=[iv("exemption", "2026-09-01", "2026-09-30")])
        self.assertEqual(r.decision, "allow")
        self.assertTrue(any("豁免覆盖" in x for x in r.reasons))

    def test_revocation_overrides_exemption(self):
        r = eval_at("2026-09-15", versions=[version()],
                    intervals=[iv("exemption", "2026-09-01", "2026-09-30"),
                               iv("revocation", "2026-09-10", "2026-09-20", 2)])
        self.assertEqual(r.decision, "deny")
        self.assertIsNone(r.version)  # 撤销直接拒绝，不选版本

    def test_appeal_does_not_change_decision(self):
        denied = eval_at("2026-09-15", versions=[version()],
                         intervals=[iv("appeal", "2026-09-10", "2026-09-20")])
        self.assertEqual(denied.decision, "deny")
        allowed = eval_at("2026-03-01", versions=[version()],
                          intervals=[iv("appeal", "2026-02-01", "2026-12-31")])
        self.assertEqual(allowed.decision, "allow")

    def test_version_selected_by_register_time_is_immutable_replay(self):
        # v2 在 9 月登记；用 8 月的评估时刻回放，仍只能看到 v1
        v1 = version(valid_until="2026-06-30", no=1)
        v2 = version(valid_from="2026-07-01", valid_until="2027-06-30", no=2)
        v2 = QualificationVersion(**{**v2.__dict__, "created_at": datetime(2026, 9, 1, tzinfo=timezone.utc)})
        r = eval_at("2026-08-15", versions=[v2, v1], at="2026-08-31T23:59:59+00:00")
        self.assertEqual(r.version, 1)
        self.assertEqual(r.decision, "deny")  # v1 已过期
        # 同输入在任何时刻回放结论一致
        r2 = eval_at("2026-08-15", versions=[v2, v1], at="2026-08-31T23:59:59+00:00")
        self.assertEqual(r2.reasons, r.reasons)

    def test_interval_registered_after_eval_does_not_change_past(self):
        # 撤销在核验之后才登记：回放该次核验不受影响，结论仍为放行
        late = Interval(
            interval_id=9, mentor_id="m1", kind="revocation",
            start=date.fromisoformat("2026-03-01"), end=date.fromisoformat("2026-03-31"),
            reason="事后撤销", created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            author="admin1",
        )
        r = eval_at("2026-03-15", versions=[version()], intervals=[late])
        self.assertEqual(r.decision, "allow")
        self.assertEqual(r.active_intervals, ())

    def test_scan_generates_near_and_expired_cases(self):
        vs = {"m1": [version(valid_until="2026-09-20")],
              "m2": [version(valid_until="2025-01-01", no=1)]}
        found = engine.scan_cases(vs, {}, as_of_day=date.fromisoformat("2026-09-15"))
        codes = {m: c for m, c, _ in found}
        self.assertEqual(codes, {"m1": "near_expiry", "m2": "expired"})

    def test_scan_appeal_and_renewal_suppress(self):
        vs = {"m1": [version(valid_until="2025-01-01")]}
        found = engine.scan_cases(
            vs, {"m1": [iv("appeal", "2026-09-01", "2026-09-30")]},
            as_of_day=date.fromisoformat("2026-09-15"),
        )
        self.assertEqual(found, [])
        found2 = engine.scan_cases(
            vs, {"m1": [iv("renewal", "2026-09-01", "2026-09-30")]},
            as_of_day=date.fromisoformat("2026-09-15"),
        )
        self.assertEqual(found2, [])


if __name__ == "__main__":
    unittest.main()
