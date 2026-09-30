"""传承人资质巡检服务端。

模块划分：
- timeutil：统一时间口径（业务日期、评估时刻、版本生效时刻）。
- models：角色、资质版本、区间决定、案件等领域数据结构。
- engine：纯函数核验引擎，所有结论只依赖给定时刻与不可改写历史。
- store：SQLite 持久化，资质版本只追加、不改写。
- authz：角色边界与个人材料访问控制。
- service：应用服务，固定预约/签到核验快照并留存每次决定依据。
- server：标准库 HTTP 接口。
"""

__all__ = ["timeutil", "models", "engine", "store", "authz", "service", "server"]
