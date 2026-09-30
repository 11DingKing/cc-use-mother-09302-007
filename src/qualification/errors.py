"""领域错误类型。"""
from __future__ import annotations


class QualificationError(Exception):
    """所有领域错误的基类。"""


class ValidationError(QualificationError):
    """请求或命令参数不合法。"""


class NotFoundError(QualificationError):
    """聚合或记录不存在。"""


class ConflictError(QualificationError):
    """标识冲突或状态不允许该操作。"""


class PermissionDenied(QualificationError):
    """当前角色无权执行该读取或写入。"""


class TamperError(QualificationError):
    """事件日志的哈希链校验失败，历史可能被改写。"""
