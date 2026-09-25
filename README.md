# 餐桌安全食品追溯服务

这是一个面向农产品监管部门、检测实验室和蔬菜配送企业的模块化后端，集中管理供应商、蔬菜批次、抽样检测、农残限值、运输链、风险处置、用户权限、会话、审计和可恢复后台任务。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 主要模块

- 农产品档案：登记供应商、种植地、蔬菜批次和追溯标识。
- 检测业务：登记抽样、实验室结果、农残限值和风险判定。
- 规则预演：农残限值与抽检比例调整先预演评估，审批后原子发布，全程留痕。
- 运输追踪：记录装车、转运、到货、温度与异常处置。
- 风险协同：支持批次隔离、召回、监管公告和跨部门办理。
- 身份与权限：用户、角色、细粒度权限、会话令牌、账号停用和会话撤销。
- 审计记录：关键身份操作留痕，并对口令和令牌等敏感字段做过滤。
- 后台任务：使用 SQLite 保存待执行任务，支持去重、租约、重试和完成回执。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

配置项均以 `TOWNSHIP_` 开头。可以复制 `.env.example` 后按需设置，默认数据库位于 `./data/township.db`。

## 初始化与检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

首次部署可创建唯一的初始管理员：

```bash
curl -sS -X POST http://127.0.0.1:8432/api/auth/bootstrap   -H 'Content-Type: application/json'   -d '{"username":"admin","password":"Admin!23456","client_label":"initial-setup"}'
```

之后通过 `/api/auth/login` 获取会话令牌，并在管理接口请求头中使用 `Authorization: Bearer <token>`。

## 测试

```bash
python -m pytest
```

测试覆盖身份初始化、登录、用户与角色维护、权限计算、账号停用后的会话撤销、审计脱敏、农产品批次、抽样检测、运输追踪、风险任务和数据库时间格式。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内启动应用并检查服务根路径与健康接口，适合部署前快速确认路由和数据库初始化是否正常。

## 目录结构

```text
app/
  api/             用户、角色、审计、认证和系统接口
  core/            时钟、安全、异常和分页能力
  repositories/    SQLite 查询与持久化读取
  routers/         灾情、事件、公告、部门和信访业务接口
  food/             农产品、检测、运输和风险处置服务
  schemas/         管理接口输入模型
  services/        身份、审计和后台任务领域服务
  cli.py           初始化、检查和冒烟入口
  database.py      SQLite 连接、事务、表结构与基础权限
tests/             核心、管理接口和原有业务回归测试
tools/             本地维护脚本
```

## 限值规则预演与发布

监管部门调整农残限值或抽检比例时，不能直接改动生效规则（会破坏责任追踪），必须走"预演 → 审批 → 发布"流程：

1. **创建预演**：`POST /api/food/rule-rehearsals`（需 `food.rules.rehearse` 权限）。系统以当前生效规则版本为基准，用候选规则重算全部在途批次（pending/testing/released/held），只写预演表，不修改任何当前结论。报告包含：
   - 结论差异：哪些批次会从合格变为待复核（或相反），含具体超标项目与限值对比；
   - 影响供应商：按供应商汇总结论变化批次与补采量；
   - 建议补采量：按候选抽检比例（份/吨）逐批计算需补采样本数及复核检测量；
   - 权限检查：创建人持有的流程权限、审批/发布待办步骤及职责分离要求。
2. **审批**：`POST /api/food/rule-rehearsals/{id}/approval`（需 `food.rules.approve` 权限，且审批人不能是创建人）。`decision` 为 `approve` 或 `reject`，`opinion` 为审批意见。重复审批、审批已驳回/已发布的预演都会被拒绝。
3. **发布**：`POST /api/food/rule-rehearsals/{id}/publish`（需 `food.rules.publish` 权限）。在单个事务内作废旧版本、写入新版本规则并标记预演已发布，保证原子性；若期间已有其他预演发布（基准版本变化），该预演作废需重新预演。

候选预演有有效期（默认 72 小时，可用 `expires_in_hours` 或 `expires_at` 指定）。过期候选不能审批也不能发布；已审批但未及发布的候选过期后同样失效。所有状态保存在 SQLite 中，服务重启不会把未完成的预演误当成已生效——生效规则只取决于 `food_rule_sets` 中唯一的 `active` 版本。

查询接口（需 `food.rules.read` 权限）：

- `GET /api/food/rule-rehearsals`：预演列表与报告摘要，可按 `status` 过滤；
- `GET /api/food/rule-rehearsals/{id}`：预演状态、审批意见与审批记录；
- `GET /api/food/rule-rehearsals/{id}/report`：完整预演报告；
- `GET /api/food/rule-sets/current`：当前生效规则版本；
- `GET /api/food/rule-sets`：规则版本历史。

## 数据一致性

SQLite 连接默认启用外键、WAL、busy timeout 与同步写入策略。需要跨多张表更新的管理操作在即时事务中执行，失败会整体回滚。会话令牌只保存摘要；用户停用会撤销仍有效的会话。审计事件保存操作者、动作、资源、结果和前后状态，但不会保存明文密码或令牌。
