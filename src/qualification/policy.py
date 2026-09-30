"""时间区间策略。

续证、撤销、限时豁免、申诉都只作用于规定区间：统一采用半开区间
[start, end)，end 为 None 表示开放区间。所有判定都以“核验时点”
在轴上的覆盖关系计算，区间之外的事件不产生任何效果。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .clock import parse_iso


@dataclass(frozen=True)
class Interval:
    start: str
    end: str | None  # None 表示开放区间

    def covers(self, moment: Any) -> bool:
        m = parse_iso(moment)
        if parse_iso(self.start) > m:
            return False
        if self.end is not None and m >= parse_iso(self.end):
            return False
        return True


def any_covers(intervals: Iterable[Interval], moment: Any) -> bool:
    return any(item.covers(moment) for item in intervals)


def valid_through(valid_from: str, valid_to: str, moment: Any) -> bool:
    """证明在核验时点是否处于有效期内（含当日，按时刻比较）。"""
    m = parse_iso(moment)
    return parse_iso(valid_from) <= m < parse_iso(valid_to)


def training_covers(training: dict[str, Any], moment: Any) -> bool:
    """培训记录是否覆盖核验时点：完成于该时点之前，且未超过自身有效期。"""
    m = parse_iso(moment)
    if parse_iso(training["trained_on"]) > m:
        return False
    valid_through_value = training.get("valid_through")
    if valid_through_value and m >= parse_iso(valid_through_value):
        return False
    return True


def normalize_window(start: Any, end: Any | None) -> Interval:
    start_text = parse_iso(start).isoformat().replace("+00:00", "Z")
    end_text = None
    if end is not None:
        end_parsed = parse_iso(end)
        if end_parsed <= parse_iso(start_text):
            raise ValueError("区间结束必须晚于开始")
        end_text = end_parsed.isoformat().replace("+00:00", "Z")
    return Interval(start_text, end_text)


def exemption_covers(exemption: dict[str, Any], scope: str, moment: Any) -> bool:
    """豁免区间是否在该时点覆盖某项检查。scope 为“全部”时覆盖一切。"""
    if exemption.get("scope") not in ("全部", scope):
        return False
    return Interval(exemption["start"], exemption["end"]).covers(moment)
