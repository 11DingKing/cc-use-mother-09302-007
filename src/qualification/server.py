"""HTTP 接口（标准库实现，零第三方依赖）。

角色通过 X-Role 头传入；所有请求与响应均为 JSON。服务把事件日志落盘到
数据目录，重启后自动重放，且在启动与每次请求前校验哈希链。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import permissions as perm
from .clock import Clock, SystemClock
from .errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    QualificationError,
    TamperError,
    ValidationError,
)
from .eventlog import EventLog
from .models import (
    AddTraining,
    DecideAppeal,
    GrantExemption,
    RegisterProfile,
    RenewCredential,
    RevokeCredential,
    Role,
)
from .service import QualificationService

ROLE_HEADER = "X-Role"


def build_service(data_dir: str | Path, clock: Clock | None = None) -> QualificationService:
    path = Path(data_dir)
    path.mkdir(parents=True, exist_ok=True)
    log = EventLog(path / "events.jsonl", clock=clock or SystemClock())
    return QualificationService(log, clock=clock or SystemClock())


def _role_of(handler: "ApiHandler") -> Role:
    raw = handler.headers.get(ROLE_HEADER, "")
    alias = {
        "admin": Role.ADMIN,
        "coordinator": Role.COORDINATOR,
        "staff": Role.STAFF,
    }
    if raw in alias:
        return alias[raw]
    try:
        return Role(raw)
    except ValueError:
        raise PermissionDenied(f"未知角色：{raw or '(空)'}")


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "QualificationService/1.0"

    # -- 框架辅助 -----------------------------------------------------------

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(value, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return value

    def _send(self, status: int, body) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, fn):
        try:
            fn()
        except PermissionDenied as exc:
            self._send(403, {"error": "权限不足", "detail": str(exc)})
        except ValidationError as exc:
            self._send(400, {"error": "请求不合法", "detail": str(exc)})
        except NotFoundError as exc:
            self._send(404, {"error": "资源不存在", "detail": str(exc)})
        except ConflictError as exc:
            self._send(409, {"error": "状态冲突", "detail": str(exc)})
        except TamperError as exc:
            self._send(500, {"error": "数据完整性校验失败", "detail": str(exc)})
        except QualificationError as exc:
            self._send(400, {"error": "业务拒绝", "detail": str(exc)})
        except KeyError as exc:
            self._send(400, {"error": "缺少必填字段", "detail": str(exc.args[0])})
        except (TypeError, ValueError) as exc:
            self._send(400, {"error": "请求不合法", "detail": str(exc)})

    def _service(self) -> QualificationService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # 静默标准访问日志
        return

    # -- 路由 ---------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        def route():
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._send(200, {"status": "ok"})
                return
            self._service().verify_integrity()
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            role = _role_of(self)
            if parsed.path.startswith("/admin/profiles/"):
                mentor_id = parsed.path.rsplit("/", 1)[1]
                self._send(200, self._service().get_profile(role, mentor_id))
            elif parsed.path == "/verifications":
                self._send(200, self._service().list_snapshots(role, query.get("activity_id")))
            elif parsed.path == "/verifications/explain":
                self._send(
                    200,
                    self._service().explain(
                        role, query["kind"], query["activity_id"], query["mentor_id"]
                    ),
                )
            elif parsed.path == "/cases":
                self._send(200, self._service().list_cases(role, query.get("status")))
            else:
                self._send(404, {"error": "未知接口"})

        self._handle(route)

    def do_POST(self) -> None:  # noqa: N802
        def route():
            self._service().verify_integrity()
            parsed = urlparse(self.path)
            role = _role_of(self)
            body = self._read_json()
            svc = self._service()
            path = parsed.path

            # 路径级角色门槛：越权请求在进入字段解析前即被拒绝。
            if path.startswith("/admin/") or path == "/inspections/run" or path.startswith("/cases/"):
                perm.require(role, perm.PROFILE_WRITE, path)
            elif path == "/verifications":
                perm.require(role, perm.VERIFICATION_CREATE, path)

            if path == "/admin/profiles":
                cmd = RegisterProfile(
                    mentor_id=body["mentor_id"],
                    name=body["name"],
                    id_tail=body["id_tail"],
                    specialties=tuple(body["specialties"]),
                    age_min=int(body["age_min"]),
                    age_max=int(body["age_max"]),
                    credential_source=body["credential_source"],
                    credential_no=body["credential_no"],
                    valid_from=body["valid_from"],
                    valid_to=body["valid_to"],
                    trainings=tuple(body.get("trainings", [])),
                    actor=body.get("actor", ""),
                    command_id=body.get("command_id", ""),
                )
                self._send(201, svc.register(role, cmd))
            elif path.startswith("/admin/profiles/"):
                rest = path[len("/admin/profiles/") :].split("/")
                mentor_id = rest[0]
                action = rest[1] if len(rest) > 1 else ""
                self._send(201, self._admin_subresource(role, svc, mentor_id, action, body))
            elif path == "/verifications":
                self._send(
                    201,
                    svc.verify(
                        role,
                        kind=body["kind"],
                        mentor_id=body["mentor_id"],
                        activity_id=body["activity_id"],
                        activity_specialty=body["activity_specialty"],
                        audience_age=int(body["audience_age"]),
                        at=body.get("at"),
                        actor=body.get("actor", ""),
                        command_id=body.get("command_id", ""),
                    ),
                )
            elif path == "/inspections/run":
                self._send(201, svc.run_inspection(role, body["at"], int(body.get("warn_days", 30))))
            elif path.startswith("/cases/") and path.endswith("/resolve"):
                case_id = path[len("/cases/") : -len("/resolve")]
                self._send(200, svc.resolve_case(role, case_id, body.get("note", "")))
            else:
                self._send(404, {"error": "未知接口"})

        self._handle(route)

    def _admin_subresource(self, role: Role, svc: QualificationService, mentor_id: str, action: str, body: dict) -> dict:
        if action == "trainings":
            cmd = AddTraining(
                mentor_id=mentor_id,
                topic=body["topic"],
                trained_on=body["trained_on"],
                actor=body.get("actor", ""),
                command_id=body.get("command_id", ""),
            )
            return svc.add_training(role, cmd)
        if action == "renew":
            cmd = RenewCredential(
                mentor_id=mentor_id,
                credential_source=body["credential_source"],
                credential_no=body["credential_no"],
                start=body["start"],
                end=body["end"],
                reason=body.get("reason", ""),
                actor=body.get("actor", ""),
                command_id=body.get("command_id", ""),
            )
            return svc.renew(role, cmd)
        if action == "revoke":
            cmd = RevokeCredential(
                mentor_id=mentor_id,
                start=body["start"],
                end=body.get("end"),
                reason=body.get("reason", ""),
                actor=body.get("actor", ""),
                command_id=body.get("command_id", ""),
            )
            return svc.revoke(role, cmd)
        if action == "exemptions":
            cmd = GrantExemption(
                mentor_id=mentor_id,
                start=body["start"],
                end=body["end"],
                scope=body["scope"],
                reason=body.get("reason", ""),
                actor=body.get("actor", ""),
                command_id=body.get("command_id", ""),
            )
            return svc.grant_exemption(role, cmd)
        if action == "appeals":
            cmd = DecideAppeal(
                mentor_id=mentor_id,
                appeal_id=body["appeal_id"],
                upheld=bool(body["upheld"]),
                start=body.get("start", ""),
                end=body.get("end", ""),
                scope=body.get("scope", ""),
                note=body.get("note", ""),
                actor=body.get("actor", ""),
                command_id=body.get("command_id", ""),
            )
            return svc.decide_appeal(role, cmd)
        raise NotFoundError(f"未知管理操作：{action}")


def serve(data_dir: str, host: str = "127.0.0.1", port: int = 8080, clock: Clock | None = None) -> None:
    service = build_service(data_dir, clock=clock)
    httpd = ThreadingHTTPServer((host, port), ApiHandler)
    httpd.service = service  # type: ignore[attr-defined]
    print(f"传承人资质巡检服务监听于 http://{host}:{port}（数据目录：{data_dir}）")
    httpd.serve_forever()
