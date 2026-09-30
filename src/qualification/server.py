"""标准库 HTTP 接口。

身份识别采用请求头 ``X-User-Id``（教学/内网部署的简化方案，生产环境应替换为
网关注入的已认证身份）。所有授权在应用层完成，普通人员无法越权读取个人材料。

路由：
- POST   /admin/mentors                         登记传承人
- POST   /admin/mentors/{id}/versions           追加不可改写资质版本
- POST   /admin/mentors/{id}/intervals          续证/撤销/限时豁免/申诉区间
- POST   /admin/inspections                     可控时间批量巡检（body: {"as_of": "..."}）
- GET    /admin/cases?status=open               案件列表
- POST   /admin/cases/{id}/resolve              处理案件
- GET    /admin/material/{mentor_id}            读取个人材料（留痕）
- GET    /admin/access-log                      访问审计
- POST   /scheduler/verify/bookings             预约核验并固定快照
- POST   /scheduler/verify/checkins             签到核验并固定快照
- GET    /scheduler/directory                   简名录
- GET    /snapshots/{id}                        查看快照（管理员/排课老师/本人）
- GET    /health                                健康检查
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .authz import AuthorizationError
from .service import QualificationService, ServiceError
from .store import Store
from .timeutil import to_jsonable


class _Handler(BaseHTTPRequestHandler):
    service: QualificationService

    # ---- 基础收发 ----
    def _send(self, status: int, payload: object) -> None:
        body = json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ServiceError("请求体必须是 JSON") from exc
        if not isinstance(value, dict):
            raise ServiceError("请求体必须是 JSON 对象")
        return value

    def _actor(self):
        user_id = self.headers.get("X-User-Id")
        if not user_id:
            raise AuthorizationError("缺少 X-User-Id 身份头")
        return self.service.actor_for(user_id)

    def _query(self, key: str) -> str | None:
        query = parse_qs(urlparse(self.path).query)
        values = query.get(key)
        return values[0] if values else None

    def log_message(self, fmt: str, *args) -> None:  # 静默默认访问日志
        return

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/health":
                self._send(200, {"status": "ok"})
                return
            actor = self._actor()
            if path == "/admin/cases":
                self._send(200, self.service.list_cases(actor, self._query("status")))
            elif path == "/admin/access-log":
                self._send(200, self.service.access_log(actor))
            elif path == "/scheduler/directory":
                self._send(200, self.service.directory(actor))
            else:
                m = re.fullmatch(r"/admin/material/([^/]+)", path)
                if m:
                    self._send(200, self.service.read_material(actor, m.group(1)))
                    return
                m = re.fullmatch(r"/snapshots/(\d+)", path)
                if m:
                    self._send(200, self.service.get_snapshot(actor, int(m.group(1))))
                    return
                self._send(404, {"error": "未找到路由"})
        except AuthorizationError as exc:
            self._send(403, {"error": str(exc)})
        except ServiceError as exc:
            self._send(400, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            actor = self._actor()
            body = self._body()

            if path == "/admin/mentors":
                self._send(201, self.service.register_mentor(
                    actor, body["mentor_id"], body["name"], body["id_number"],
                    body.get("phone"),
                ))
                return

            m = re.fullmatch(r"/admin/mentors/([^/]+)/versions", path)
            if m:
                self._send(201, self.service.add_qualification_version(
                    actor, m.group(1),
                    specialties=body["specialties"],
                    age_min=body["age_min"], age_max=body["age_max"],
                    training_records=body["training_records"],
                    cert_source=body["cert_source"], cert_no=body["cert_no"],
                    valid_from=body["valid_from"], valid_until=body["valid_until"],
                    at_time=body.get("at_time"),
                ))
                return

            m = re.fullmatch(r"/admin/mentors/([^/]+)/intervals", path)
            if m:
                self._send(201, self.service.add_interval(
                    actor, m.group(1), body["kind"],
                    body["start"], body["end"], body["reason"],
                    at_time=body.get("at_time"),
                ))
                return

            if path == "/admin/inspections":
                self._send(200, self.service.run_inspection(
                    actor, body.get("as_of"), body.get("horizon_days", 30),
                ))
                return

            m = re.fullmatch(r"/admin/cases/(\d+)/resolve", path)
            if m:
                self._send(200, self.service.resolve_case(
                    actor, int(m.group(1)), body["note"],
                    ignore=bool(body.get("ignore", False)),
                ))
                return

            if path in ("/scheduler/verify/bookings", "/scheduler/verify/checkins"):
                scope = "booking" if path.endswith("bookings") else "checkin"
                self._send(201, self.service.verify(
                    actor, scope, body["mentor_id"],
                    specialty=body["specialty"],
                    audience_age=body["audience_age"],
                    activity_day=body["activity_day"],
                    at_time=body.get("at_time"),
                ))
                return

            self._send(404, {"error": "未找到路由"})
        except AuthorizationError as exc:
            self._send(403, {"error": str(exc)})
        except ServiceError as exc:
            self._send(400, {"error": str(exc)})
        except KeyError as exc:
            self._send(400, {"error": f"缺少字段：{exc.args[0]}"})


def build_server(host: str, port: int, db_path: str) -> ThreadingHTTPServer:
    store = Store(db_path)
    service = QualificationService(store)
    handler = type("BoundHandler", (_Handler,), {"service": service})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="传承人资质巡检服务端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="qualification.sqlite3")
    args = parser.parse_args()

    httpd = build_server(args.host, args.port, args.db)
    print(f"服务已启动：http://{args.host}:{args.port}（数据库 {args.db}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
