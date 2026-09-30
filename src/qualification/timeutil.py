"""统一时间口径。

全系统只认两种时间：
- 业务日期 ``YYYY-MM-DD``：证明有效期、豁免/撤销/续证/申诉区间按日比较，区间为闭区间；
- 评估时刻 ISO-8619（带秒）：资质版本的生效与核验快照固定时刻。

巡检、预约、签到都允许显式给定评估时刻，保证同输入同结论（可控时间）。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

DATE_FMT = "%Y-%m-%d"


def utcnow() -> datetime:
    """当前评估时刻，统一使用 UTC 感知对象。"""
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utcnow().isoformat()


def today() -> date:
    return utcnow().date()


def parse_date(value: str | date, field: str = "日期") -> date:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{field}必须是 YYYY-MM-DD 字符串")
    try:
        return datetime.strptime(value, DATE_FMT).date()
    except ValueError as exc:
        raise ValueError(f"{field}必须是 YYYY-MM-DD 格式") from exc


def parse_instant(value: str | datetime | None, field: str = "评估时刻") -> datetime:
    """解析评估时刻；缺省取当前时刻。

    接受 ``YYYY-MM-DD``（按该日 00:00 UTC）或完整 ISO 字符串。
    """
    if value is None:
        return utcnow()
    if isinstance(value, datetime):
        dt = value
    else:
        text = value.strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field}必须是 ISO-8601 时间") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def d(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def instant_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def to_jsonable(value: Any) -> Any:
    """把领域对象递归转成可 JSON 序列化的普通值。"""
    if isinstance(value, datetime):
        return instant_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    return value
