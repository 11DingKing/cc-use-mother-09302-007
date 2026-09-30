"""纯函数核验引擎。

所有结论只依赖显式传入的评估时刻/活动日期与历史数据，不读取时钟，
因此预约、签到、批量巡检可以使用可控时间并得到可复现的结论。

区间型动作的优先级（同日可能多个区间生效）：
1. 撤销：最高优先级，区间内一律拒绝（豁免也不能覆盖撤销）；
2. 限时豁免：可覆盖过期与范围（项目/年龄）不符，每条被覆盖的限制都会写入依据；
3. 续证：区间内把"已过期"视为续证有效；
4. 申诉：不改变放行/拒绝结论，只作为依据标注，并在区间内抑制重复巡检案件。
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone

from .models import Evaluation, Interval, QualificationVersion

NEAR_EXPIRY_DAYS = 30  # 批量巡检提前预警的天数


def active_intervals(
    intervals: list[Interval], day: date, registered_by: datetime | None = None
) -> list[Interval]:
    """覆盖该日、且在给定评估时刻之前已登记的区间。"""
    return [
        iv
        for iv in intervals
        if iv.covers(day) and (registered_by is None or iv.created_at <= registered_by)
    ]


def select_version(
    versions: list[QualificationVersion], evaluated_at: datetime, day: date
) -> QualificationVersion | None:
    """选择评估时刻已登记、且在活动日已生效的最新资质版本。"""
    candidates = [
        v
        for v in versions
        if v.created_at <= evaluated_at and v.valid_from <= day
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda v: v.version)


def evaluate(
    versions: list[QualificationVersion],
    intervals: list[Interval],
    *,
    evaluated_at: datetime,
    day: date,
    specialty: str,
    audience_age: int,
) -> Evaluation:
    """对单次进校请求给出结论与完整依据。"""
    active = active_intervals(intervals, day, registered_by=evaluated_at)
    kinds = {iv.kind for iv in active}
    reasons: list[str] = []

    if "revocation" in kinds:
        reasons.append("证明处于撤销区间，禁止进校")
        return Evaluation("deny", day, None, tuple(reasons), tuple(active))

    version = select_version(versions, evaluated_at, day)
    if version is None:
        reasons.append("评估时刻前没有已登记且生效的资质版本")
        if "exemption" in kinds:
            reasons.append("限时豁免不能替代资质登记，仍需管理员线下复核")
        return Evaluation("deny", day, None, tuple(reasons), tuple(active))

    reasons.append(f"采用资质版本 v{version.version}")

    expired = day > version.valid_until
    specialty_ok = specialty in version.specialties
    age_ok = version.covers_age(audience_age)

    if expired:
        reasons.append(
            f"证明已于 {version.valid_until.isoformat()} 过期"
            f"（来源：{version.cert_source}，编号 {version.cert_no}）"
        )
    if not specialty_ok:
        reasons.append(f"擅长项目不含本次活动项目“{specialty}”")
    if not age_ok:
        reasons.append(
            f"适用年龄 {version.age_range[0]}-{version.age_range[1]} 岁"
            f"不含受众年龄 {audience_age} 岁"
        )

    # 续证：仅在续证区间内消除"过期"这一条限制
    if expired and "renewal" in kinds:
        reasons.append("续证区间内，过期状态按续证有效处理")
        expired = False

    # 限时豁免：逐条覆盖仍然存在的限制
    if "exemption" in kinds:
        waived: list[str] = []
        if expired:
            waived.append("过期")
            expired = False
        if not specialty_ok:
            waived.append("项目不符")
            specialty_ok = True
        if not age_ok:
            waived.append("年龄不符")
            age_ok = True
        if waived:
            reasons.append("限时豁免覆盖限制：" + "、".join(waived))

    if "appeal" in kinds:
        reasons.append("申诉处理区间内，结论以原核验为准并留痕待复核")

    if expired or not specialty_ok or not age_ok:
        reasons.append("结论：拒绝")
        return Evaluation(
            "deny", day, version.version, tuple(reasons), tuple(active),
            matched_specialty=specialty if specialty_ok else None,
        )

    reasons.append("结论：放行")
    return Evaluation(
        "allow", day, version.version, tuple(reasons), tuple(active),
        matched_specialty=specialty,
    )


def scan_cases(
    versions_by_mentor: dict[str, list[QualificationVersion]],
    intervals_by_mentor: dict[str, list[Interval]],
    *,
    as_of_day: date,
    horizon_days: int = NEAR_EXPIRY_DAYS,
) -> list[tuple[str, str, str]]:
    """按可控日期生成待办案件，返回 (mentor_id, reason_code, reason) 列表。

    同一传承人当日至多一条案件；申诉区间内抑制生成（申诉本身已在处理）。
    """
    result: list[tuple[str, str, str]] = []
    deadline = date.fromordinal(as_of_day.toordinal() + horizon_days)
    for mentor_id in sorted(versions_by_mentor):
        versions = versions_by_mentor[mentor_id]
        # 巡检只看受控日结束前已登记的区间与版本
        boundary = datetime.combine(as_of_day, time.max, tzinfo=timezone.utc)
        active = active_intervals(intervals_by_mentor.get(mentor_id, []), as_of_day,
                                  registered_by=boundary)
        kinds = {iv.kind for iv in active}
        if "appeal" in kinds or "revocation" in kinds:
            # 申诉已进入处理流程；撤销是管理员主动动作，均不生成巡检案件
            continue
        current = select_version(versions, boundary, as_of_day)
        if current is None:
            continue
        if "renewal" in kinds or "exemption" in kinds:
            continue  # 续证/豁免区间内暂不预警
        if current.valid_until < as_of_day:
            result.append((
                mentor_id,
                "expired",
                f"证明已于 {current.valid_until.isoformat()} 过期（v{current.version}）",
            ))
        elif current.valid_until <= deadline:
            days_left = current.valid_until.toordinal() - as_of_day.toordinal()
            result.append((
                mentor_id,
                "near_expiry",
                f"证明将于 {current.valid_until.isoformat()} 到期，剩余 {days_left} 天（v{current.version}）",
            ))
    return result
