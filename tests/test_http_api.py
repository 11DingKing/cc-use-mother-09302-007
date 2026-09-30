"""HTTP 接口端到端测试。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qualification.server import ApiHandler, build_service


class HttpServer:
    def __init__(self, data_dir: str):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        self.httpd.service = build_service(data_dir)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


def request(method, url, role=None, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if role:
        headers["X-Role"] = role
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class HttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.server = HttpServer(self._tmp.name)
        self.server.__enter__()

    def tearDown(self) -> None:
        self.server.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_full_flow_over_http(self) -> None:
        base = self.server.base
        status, body = request("GET", f"{base}/health")
        self.assertEqual(status, 200)

        # 无角色头一律拒绝。
        status, body = request("POST", f"{base}/admin/profiles", body={})
        self.assertEqual(status, 403)

        register = {
            "mentor_id": "M001",
            "name": "张三",
            "id_tail": "1234",
            "specialties": ["剪纸"],
            "age_min": 6,
            "age_max": 15,
            "credential_source": "市非遗保护中心",
            "credential_no": "CR-2026-001",
            "valid_from": "2026-09-01T00:00:00Z",
            "valid_to": "2026-10-01T00:00:00Z",
            "trainings": [{"topic": "校园安全", "trained_on": "2026-08-01T00:00:00Z"}],
            "command_id": "cmd-reg-1",
        }
        status, body = request("POST", f"{base}/admin/profiles", "admin", register)
        self.assertEqual(status, 201)
        self.assertEqual(body["version"], 1)

        # 命令重放幂等冲突。
        status, _ = request("POST", f"{base}/admin/profiles", "admin", register)
        self.assertEqual(status, 409)

        # 普通人员无法越权读取个人材料。
        status, _ = request("GET", f"{base}/admin/profiles/M001", "staff")
        self.assertEqual(status, 403)
        status, profile = request("GET", f"{base}/admin/profiles/M001", "admin")
        self.assertEqual(status, 200)
        self.assertEqual(profile["credential"]["no"], "CR-2026-001")

        # 预约放行。
        status, booking = request(
            "POST",
            f"{base}/verifications",
            "coordinator",
            {
                "kind": "预约",
                "mentor_id": "M001",
                "activity_id": "ACT-1",
                "activity_specialty": "剪纸",
                "audience_age": 10,
                "at": "2026-09-20T08:00:00Z",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(booking["decision"]["result"], "放行")
        self.assertEqual(booking["credential"]["credential_no"], "******")

        # 签到时已过期：拒绝且给出依据。
        status, checkin = request(
            "POST",
            f"{base}/verifications",
            "coordinator",
            {
                "kind": "签到",
                "mentor_id": "M001",
                "activity_id": "ACT-1",
                "activity_specialty": "剪纸",
                "audience_age": 10,
                "at": "2026-10-05T08:00:00Z",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(checkin["decision"]["result"], "拒绝")
        self.assertIn("证明缺失或已过期", checkin["decision"]["reasons"])

        # 巡检与解释。
        status, cases = request(
            "POST", f"{base}/inspections/run", "admin", {"at": "2026-09-25T00:00:00Z"}
        )
        self.assertEqual(status, 201)
        self.assertTrue(cases)
        status, explained = request(
            "GET",
            f"{base}/verifications/explain?"
            + urllib.parse.urlencode(
                {"kind": "预约", "activity_id": "ACT-1", "mentor_id": "M001"}
            ),
            "admin",
        )
        self.assertEqual(status, 200)
        self.assertEqual(explained["decision"]["result"], "放行")

        # 非法 JSON 返回 400。
        req = urllib.request.Request(
            f"{base}/admin/profiles",
            data=b"{not-json",
            headers={"Content-Type": "application/json", "X-Role": "admin"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
