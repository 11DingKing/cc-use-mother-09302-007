"""角色边界与个人材料访问控制。

角色（与领域契约 actors 对应）：
- admin 校外人员管理员：登记、版本追加、区间动作、巡检、案件处理、读取全部材料、查审计；
- scheduler 排课老师：发起预约/签到核验、查看核验快照与人员名录；
- mentor 非遗传承人：只能读取本人材料与本人快照；
- staff 普通人员：无任何个人材料读取权。

每次个人材料读取都会在应用层留痕（允许与拒绝都记录）。
"""
from __future__ import annotations

from .models import Role

PERMISSIONS: dict[str, frozenset[str]] = {
    "admin": frozenset({
        "mentor.register", "qualification.version", "interval.add",
        "verify.booking", "verify.checkin", "inspection.run", "case.resolve",
        "case.list", "material.read", "snapshot.read", "directory.read",
        "access_log.read",
    }),
    "scheduler": frozenset({
        "verify.booking", "verify.checkin", "snapshot.read", "directory.read",
    }),
    "mentor": frozenset({"snapshot.read"}),  # 仅限本人，在 service 层按 mentor 身份约束
    "staff": frozenset(),
}


class AuthorizationError(Exception):
    """越权访问。"""


def can(role: Role, permission: str) -> bool:
    return permission in PERMISSIONS.get(role, frozenset())


def require(role: Role, permission: str) -> None:
    if not can(role, permission):
        raise AuthorizationError(f"角色 {role} 无权执行 {permission}")


def can_read_material(role: Role, *, actor_id: str, mentor_id: str) -> bool:
    """管理员可读任意人材料；传承人仅能读本人；其余角色一律拒绝。"""
    if role == "admin":
        return True
    if role == "mentor" and actor_id == mentor_id:
        return True
    return False


def can_read_snapshot(role: Role, *, actor_id: str, owner_mentor_id: str) -> bool:
    if role in ("admin", "scheduler"):
        return True
    if role == "mentor" and actor_id == owner_mentor_id:
        return True
    return False
