"""HTTP 接口端到端：真实起服务，验证状态码、鉴权头与快照固定。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from http.server import ThreadingHTTPServer

from qualification.server import _Handler
from qualification.service import QualificationService
from qualification.store import Store


class HttpIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        service = QualificationService(Store(":memory:"))
        service.store.upsert_user("admin1", "admin", "管理员")
        service.store.upsert_user("teacher1", "scheduler", "排课老师")
        service.store.upsert_user("m1", "mentor", "王剪纸")
        handler = type("H", (_Handler,), {"service": service})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def _req(self, method: str, path: str, user: str | None, body: dict | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if user:
            req.add_header("X-User-Id", user)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_full_flow(self) -> None:
        # 无身份头 -> 403
        self.assertEqual(self._req("GET", "/health", None)[0], 200)
        self.assertEqual(self._req("POST", "/admin/mentors", None,
                                   {"mentor_id": "x"})[0], 403)

        status, mentor = self._req("POST", "/admin/mentors", "admin1", {
            "mentor_id": "m1", "name": "王剪纸", "id_number": "id-001",
        })
        self.assertEqual(status, 201)
        self.assertNotIn("id_number", mentor)  # 只回哈希

        status, _ = self._req("POST", "/admin/mentors/m1/versions", "admin1", {
            "specialties": ["剪纸"], "age_min": 6, "age_max": 15,
            "training_records": ["市培2025"], "cert_source": "authority",
            "cert_no": "C-1", "valid_from": "2026-01-01",
            "valid_until": "2026-06-30",
            "at_time": "2026-01-02T09:00:00+00:00",
        })
        self.assertEqual(status, 201)

        # 排课老师对活动日做预约核验（活动日在有效期内）
        status, booking = self._req("POST", "/scheduler/verify/bookings", "teacher1", {
            "mentor_id": "m1", "specialty": "剪纸", "audience_age": 10,
            "activity_day": "2026-05-15",
        })
        self.assertEqual(status, 201)
        self.assertEqual(booking["decision"], "allow")
        self.assertEqual(booking["scope"], "booking")

        # 签到日已过期 -> 独立核验拒绝，依据可解释
        status, checkin = self._req("POST", "/scheduler/verify/checkins", "teacher1", {
            "mentor_id": "m1", "specialty": "剪纸", "audience_age": 10,
            "activity_day": "2026-09-15",
        })
        self.assertEqual(status, 201)
        self.assertEqual(checkin["decision"], "deny")

        # 普通人员读材料 -> 403 且审计留痕
        self.assertEqual(self._req("GET", "/admin/material/m1", "ghost")[0], 403)
        status, log = self._req("GET", "/admin/access-log", "admin1")
        self.assertEqual(status, 200)
        self.assertTrue(any(row["allowed"] == 0 and row["actor_id"] == "ghost"
                            for row in log))

        # 可控时间巡检生成案件
        status, report = self._req("POST", "/admin/inspections", "admin1",
                                   {"as_of": "2026-09-15"})
        self.assertEqual(status, 200)
        self.assertEqual(report["generated_count"], 1)

        # 排课老师只有简名录，无案件权限
        self.assertEqual(self._req("GET", "/scheduler/directory", "teacher1")[0], 200)
        self.assertEqual(self._req("GET", "/admin/cases", "teacher1")[0], 403)

        # 传承人本人可读自己的快照
        self.assertEqual(
            self._req("GET", f"/snapshots/{booking['snapshot_id']}", "m1")[0], 200
        )


if __name__ == "__main__":
    unittest.main()
