"""角色权限边界。

- 校外人员管理员：资质全部写入、读取完整个人材料、巡检与案件处置、读取全部快照。
- 排课老师：发起预约/签到核验，读取与自己活动相关的结论（敏感字段打码）。
- 普通人员：无权读取任何个人材料，也不能发起核验或巡检。
"""
from __future__ import annotations

from .errors import PermissionDenied
from .models import Role

PROFILE_WRITE = {Role.ADMIN}
INSPECTION_WRITE = {Role.ADMIN}
CASE_WRITE = {Role.ADMIN}
PERSONAL_MATERIAL_READ = {Role.ADMIN}
VERIFICATION_CREATE = {Role.ADMIN, Role.COORDINATOR}
DECISION_READ = {Role.ADMIN, Role.COORDINATOR}

SENSITIVE_KEYS = ("id_tail", "credential_no")


def require(role: Role, allowed: set[Role], action: str) -> None:
    if role not in allowed:
        raise PermissionDenied(f"{role.value}无权执行：{action}")


def redact_credential(snapshot_dict: dict) -> dict:
    """对排课老师隐去证件类敏感字段，保留结论解释所需信息。"""
    redacted = dict(snapshot_dict)
    for key in SENSITIVE_KEYS:
        if key in redacted:
            redacted[key] = "******"
    return redacted
