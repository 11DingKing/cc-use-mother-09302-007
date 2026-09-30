# 传承人资质巡检

本项目维护传承人资质巡检的领域约定、角色边界与样例数据，并提供从零实现的服务端：把校外传承人的擅长项目、适用年龄、培训记录、证明来源与有效期保存为**不可改写的版本**，预约与签到各自固定**核验快照**，续证、撤销、限时豁免与申诉只影响**规定区间**，批量巡检使用**可控时间**生成待办案件，管理员可解释每次放行或拒绝的依据，普通人员无法越权读取个人材料。

## 领域约定

- `domain/contract.json`：领域角色、状态、约束和样例。
  - 角色：校外人员管理员、非遗传承人、排课老师（另设"普通人员"作为无材料访问权的基线角色）。
  - 不变量：资质版本、范围核验、到期巡检、隐私权限。
- `src/domain_contract/`：契约读取与确定性校验。
- `tools/check_contract.py`：命令行摘要检查。

## 服务端（`src/qualification/`，仅依赖 Python 3.11 标准库 + SQLite）

| 模块 | 职责 |
| --- | --- |
| `timeutil` | 统一时间口径：业务按日比较，评估时刻支持显式传入（可控时间） |
| `models` | 传承人、资质版本、区间动作、核验结论、快照、案件等不可变结构 |
| `engine` | 纯函数核验引擎：版本选择、区间优先级、巡检案件生成，结论不读时钟可回放 |
| `store` | SQLite 持久化；资质版本表只 INSERT，永不 UPDATE/DELETE |
| `authz` | 角色权限矩阵与个人材料访问控制 |
| `service` | 用例编排：登记、版本追加、区间动作、预约/签到、巡检、案件、访问留痕 |
| `server` | 标准库 HTTP 接口（`X-User-Id` 识别身份） |

### 核心规则

- **资质版本不可改写**：擅长项目、适用年龄、培训记录、证明来源、有效期按版本号追加；核验只选择"评估时刻前已登记、活动日已生效"的最新版本。事后补录新版本不改变历史核验。
- **预约与签到各自固定快照**：两类核验独立执行，结论、版本号、依据条目、生效区间全部落库，事后任何数据变化都不改写快照。预约核验按**活动当日**评估有效期，因此"排课时已注定过期"的活动在预约阶段即被拒绝。
- **区间只影响闭区间内**：
  - 撤销优先级最高，区间内一律拒绝（豁免不能覆盖撤销）；
  - 限时豁免逐条覆盖过期、项目不符、年龄不符，并在依据中写明；
  - 续证仅在区间内把过期按续证有效处理；
  - 申诉不改变结论，只留痕待复核，并抑制区间内的巡检案件。
  - 区间同样只在"登记时刻之后"的核验中生效，保证受控时间回放可复现。
- **可控时间批量巡检**：给定检查日生成 `expired`（已过期）与 `near_expiry`（30 日内到期）案件；同传承人同日同原因不重复生成；案件处理必须填写处置说明。
- **隐私权限**：管理员可读全部材料；传承人仅能读本人；排课老师只见简名录（无证件哈希、电话、培训记录）；普通人员无任何材料访问权。允许与拒绝的访问尝试全部写入审计日志。

### HTTP 接口

身份通过请求头 `X-User-Id` 传入（演示用简化方案，生产应由网关注入已认证身份）。

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| POST | `/admin/mentors` | 管理员 | 登记传承人（证件号仅存 SHA-256 哈希） |
| POST | `/admin/mentors/{id}/versions` | 管理员 | 追加资质版本（可带 `at_time`） |
| POST | `/admin/mentors/{id}/intervals` | 管理员 | 登记续证/撤销/限时豁免/申诉区间 |
| POST | `/admin/inspections` | 管理员 | 可控时间巡检（body 带 `as_of`） |
| GET | `/admin/cases?status=open` | 管理员 | 案件列表 |
| POST | `/admin/cases/{id}/resolve` | 管理员 | 处理/忽略案件（须填 `note`） |
| GET | `/admin/material/{id}` | 管理员/本人 | 个人材料（读取留痕） |
| GET | `/admin/access-log` | 管理员 | 访问审计 |
| POST | `/scheduler/verify/bookings` | 排课老师 | 预约核验并固定快照 |
| POST | `/scheduler/verify/checkins` | 排课老师 | 签到核验并固定快照 |
| GET | `/scheduler/directory` | 排课老师 | 简名录 |
| GET | `/snapshots/{id}` | 管理员/排课老师/本人 | 查看快照依据 |

### 运行

```bash
# 演示：生成可复现数据并打印事故场景全过程（预约放行→撤销→签到拒绝→续证→巡检→审计）
python3 tools/demo.py demo.sqlite3

# 启动服务
PYTHONPATH=src python3 -m qualification.server --db demo.sqlite3 --port 8080

# 示例：排课老师发起签到核验（可传 at_time 复放历史时刻）
curl -s -XPOST localhost:8080/scheduler/verify/checkins \
  -H 'X-User-Id: teacher1' -H 'Content-Type: application/json' \
  -d '{"mentor_id":"m1","specialty":"剪纸","audience_age":10,
       "activity_day":"2026-06-15","at_time":"2026-06-15T08:30:00+00:00"}'
```

## 验证

- 测试命令：`python3 -m unittest discover -s tests -v`（契约、引擎、服务、HTTP 共 29 个用例）
- 编译命令：`python3 -m compileall -q src tools tests`
- 契约检查：`python3 tools/check_contract.py domain/contract.json`
