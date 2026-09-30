# 传承人资质巡检

本项目维护传承人资质巡检的领域约定、角色边界，并提供一个**从零实现、零第三方依赖**的服务端：把校外传承人的擅长项目、适用年龄、培训记录、证明来源与有效期保存为不可改写的版本；预约与签到各自固定核验快照；续证、撤销、限时豁免与申诉只作用于规定区间；批量巡检在可控时间下生成待办案件；管理员能解释每次放行或拒绝的依据，普通人员无法越权读取个人材料。

## 领域角色

| 角色（HTTP 头 `X-Role`） | 能力 |
| --- | --- |
| 校外人员管理员（`admin`） | 资质全部写入、读取完整个人材料、批量巡检、处置案件、解释核验依据 |
| 排课老师（`coordinator`） | 发起预约/签到核验、读取结论与打码后的快照（证件号、证件后四位脱敏） |
| 普通人员（`staff`） | 无任何个人材料、核验、巡检权限 |

## 核心设计

- **不可改写的版本与事件流**：档案登记、培训追加、证明续期、撤销、豁免、申诉裁决、预约/签到核验快照、巡检案件全部是只追加事件，落盘为 `events.jsonl`。每条事件含前一条事件的 SHA-256，形成哈希链；服务启动与每次请求前重算校验，任何历史改写都会触发 `TamperError`。
- **预约 / 签到双快照**：两类核验各自固定一份当时的资质数据快照与决定（结果、拒绝原因、被豁免项、核验时点、依据事件序号），互不覆盖；后续续证或撤销不会改变历史快照，可随时 `explain` 回放“当时为什么放行”。
- **规定区间策略**：续证版本有效期、撤销区间、限时豁免、申诉支持均采用半开区间 `[start, end)`（撤销允许开放区间）。区间之外不产生任何效果。
- **可控时间巡检**：巡检基准时间由调用方显式给定（`FixedClock`/接口入参 `at`），同一输入确定性产出案件；案件分为到期预警、已过期、撤销待处理，记录数据时点与依据事件序号，支持去重与处置闭环。
- **核验项**：登记状态、证明在有效期内、未被撤销、擅长项目匹配、适用年龄覆盖、培训记录覆盖；豁免可按检查项或“全部”在窗口内放行，并在决定中显式列出 `waived`。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/qualification/`：服务端实现
  - `eventlog.py` 只追加哈希链日志（JSONL 持久化、命令幂等）
  - `models.py` 命令、事件、快照、决定、案件
  - `policy.py` 时间区间与覆盖判定
  - `aggregate.py` 事件投影与核验引擎
  - `service.py` 应用服务（写入、双快照、巡检、重放投影）
  - `permissions.py` 角色边界与敏感字段打码
  - `server.py` / `__main__.py` 标准库 HTTP 接口
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约回归、领域场景与 HTTP 端到端测试。

## 运行

```bash
PYTHONPATH=src python3 -m qualification --data-dir data --port 8080
```

主要接口（JSON；角色经 `X-Role: admin|coordinator|staff` 传入）：

| 方法与路径 | 角色 | 说明 |
| --- | --- | --- |
| `POST /admin/profiles` | admin | 登记档案与首版证明 |
| `POST /admin/profiles/{id}/trainings` | admin | 追加培训记录 |
| `POST /admin/profiles/{id}/renew` | admin | 续证，产生新版本 |
| `POST /admin/profiles/{id}/revoke` | admin | 撤销（规定区间，可开放） |
| `POST /admin/profiles/{id}/exemptions` | admin | 限时豁免 |
| `POST /admin/profiles/{id}/appeals` | admin | 申诉裁决（支持=区间豁免，驳回=无效果） |
| `GET  /admin/profiles/{id}` | admin | 读取完整个人材料 |
| `POST /verifications` | admin/coordinator | 预约或签到核验，固定快照 |
| `GET  /verifications` | admin/coordinator | 列出核验快照 |
| `GET  /verifications/explain` | admin/coordinator | 解释某次放行/拒绝依据 |
| `POST /inspections/run` | admin | 以给定时间批量巡检生成待办 |
| `GET  /cases` | admin | 案件列表 |
| `POST /cases/{id}/resolve` | admin | 处置案件 |

写入命令支持 `command_id` 幂等：重复提交同一命令返回 409，不会产生重复版本。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json
```
