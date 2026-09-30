"""生成可复现的演示数据并打印事故场景全过程。

用法：
    python3 tools/demo.py                 # 使用临时库，仅打印
    python3 tools/demo.py demo.sqlite3    # 写入指定库，可用 --db 启动服务复放

角色账号：admin1（管理员）、teacher1（排课老师）、m1（传承人本人）、staff1（普通人员）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qualification.service import Actor, QualificationService
from qualification.store import Store
from qualification.timeutil import to_jsonable


def show(title: str, payload: object) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))


def run(db_path: str = ":memory:") -> QualificationService:
    svc = QualificationService(Store(db_path))
    s = svc.store
    s.upsert_user("admin1", "admin", "资质管理员")
    s.upsert_user("teacher1", "scheduler", "排课老师")
    s.upsert_user("m1", "mentor", "王剪纸")
    s.upsert_user("staff1", "staff", "普通人员")

    admin = Actor("admin1", "admin", "资质管理员")
    teacher = Actor("teacher1", "scheduler", "排课老师")

    svc.register_mentor(admin, "m1", "王剪纸", "id-number-0001", phone="138****0000")
    svc.register_mentor(admin, "m2", "李年画", "id-number-0002", phone="138****0002")
    svc.add_qualification_version(
        admin, "m1", specialties=["剪纸", "窗花"], age_min=6, age_max=15,
        training_records=["2025 市级非遗传承培训"],
        cert_source="authority", cert_no="WH-CERT-2026-001",
        valid_from="2026-01-01", valid_until="2026-06-30",
        at_time="2026-01-05T09:00:00+00:00",
    )
    svc.add_qualification_version(
        admin, "m2", specialties=["年画"], age_min=8, age_max=14,
        training_records=["2024 省级传承人培训"],
        cert_source="school", cert_no="NH-CERT-2025-007",
        valid_from="2025-10-01", valid_until="2026-10-05",
        at_time="2025-10-08T09:00:00+00:00",
    )

    # 3 月预约 6 月活动：资质有效，快照固定为放行
    booking = svc.verify(
        teacher, "booking", "m1", specialty="剪纸", audience_age=10,
        activity_day="2026-06-15", at_time="2026-03-10T14:00:00+00:00",
    )
    show("① 预约核验（2026-03-10 预约 6-15 活动）", booking)

    # 活动前发现材料问题，管理员登记撤销区间
    svc.add_interval(admin, "m1", "revocation", "2026-06-10", "2026-06-20",
                     "举报核查发现证明来源存疑，暂停进校",
                     at_time="2026-06-09T17:00:00+00:00")
    # 传承人申诉；申诉不改结论，但进入留痕与复核流程
    svc.add_interval(admin, "m1", "appeal", "2026-06-12", "2026-06-25",
                     "传承人提交补充佐证材料，申请复核",
                     at_time="2026-06-12T09:00:00+00:00")

    # 6-15 签到：独立核验，快照固定为拒绝
    checkin = svc.verify(
        teacher, "checkin", "m1", specialty="剪纸", audience_age=10,
        activity_day="2026-06-15", at_time="2026-06-15T08:30:00+00:00",
    )
    show("② 签到核验（撤销区间内）", checkin)

    # 复核后撤销解除、续证登记新版本
    svc.add_qualification_version(
        admin, "m1", specialties=["剪纸", "窗花"], age_min=6, age_max=15,
        training_records=["2025 市级非遗传承培训", "2026 复核补训"],
        cert_source="authority", cert_no="WH-CERT-2026-018",
        valid_from="2026-06-21", valid_until="2027-06-20",
        at_time="2026-06-21T10:00:00+00:00",
    )
    again = svc.verify(
        teacher, "checkin", "m1", specialty="剪纸", audience_age=10,
        activity_day="2026-06-22", at_time="2026-06-22T08:30:00+00:00",
    )
    show("③ 新版本生效后再次签到", again)

    # 可控时间巡检：以 2026-09-20 为检查日批量生成待办
    report = svc.run_inspection(admin, as_of="2026-09-20")
    show("④ 批量巡检（受控日 2026-09-20）", report)

    # 普通人员尝试读取个人材料：被拒绝且留痕
    staff = Actor("staff1", "staff", "普通人员")
    try:
        svc.read_material(staff, "m1")
    except Exception as exc:  # noqa: BLE001 - 演示需要展示拒绝
        print(f"\n普通人员读取材料被拒绝：{exc}")

    show("⑤ 审计留痕（含被拒绝的越权读取尝试）", svc.access_log(admin))
    return svc


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else ":memory:"
    if target != ":memory:" and Path(target).exists():
        raise SystemExit(f"{target} 已存在，请先删除再重新生成")
    svc = run(target)
    if target != ":memory:":
        print(f"\n演示库已写入 {target}，启动服务：")
        print(f"  python3 -m qualification.server --db {target}")
