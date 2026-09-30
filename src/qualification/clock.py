"""可控时钟。

业务核验与批量巡检都只依赖 Clock 协议，生产环境使用系统时钟，
测试和巡检回放使用 FixedClock，保证“同一时间输入、同一结论”。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """固定在某一时刻的时钟，可手动推进。"""

    def __init__(self, moment: datetime):
        self._moment = _ensure_aware(moment)

    def now(self) -> datetime:
        return self._moment

    def advance(self, delta: timedelta) -> datetime:
        self._moment = self._moment + delta
        return self._moment

    def set(self, moment: datetime) -> None:
        self._moment = _ensure_aware(moment)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_iso(value: str | datetime) -> datetime:
    """解析 ISO8601 时间，无时区按 UTC 处理。"""
    if isinstance(value, datetime):
        return _ensure_aware(value)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return _ensure_aware(parsed)


def to_iso(value: datetime) -> str:
    return _ensure_aware(value).isoformat().replace("+00:00", "Z")
