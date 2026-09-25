# 餐桌安全食品追溯服务

这是一个面向农产品监管部门、检测实验室和蔬菜配送企业的模块化后端，集中管理供应商、蔬菜批次、抽样检测、农残限值、运输链、风险处置、用户权限、会话、审计和可恢复后台任务。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 主要模块

- 农产品档案：登记供应商、种植地、蔬菜批次和追溯标识。
- 检测业务：登记抽样、实验室结果、农残限值和风险判定。
- 运输追踪：记录装车、转运、到货、温度与异常处置。
- 风险协同：支持批次隔离、召回、监管公告和跨部门办理。
- 规则治理：农残限值与抽检比例调整必须先预演，输出结论差异、供应商影响、建议补采量、检测工作量与权限检查报告，审批后原子发布。
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

## 农残规则预演与审批发布

调整农残限值或抽检比例前，必须先用候选规则预演，确认影响后再由有审批权限的账号（不得是发起人本人）发布。预演全程只读历史数据，不会改动任何批次结论、检测记录或状态。

相关权限：`food.rules.read`（查询）、`food.rules.rehearse`（发起预演）、`food.rules.approve`（审批发布）。

```bash
# 1) 发起预演：候选限值 + 抽检比例 + 代表性批次选择（stratified 按供应商分层 / recent / explicit）
curl -sS -X POST http://127.0.0.1:8432/api/food/rule-governance/rehearsals \
  -H "Authorization: Bearer $PLANNER_TOKEN" -H 'Content-Type: application/json' \
  -d '{"code":"REH-2026-09","title":"秋季加严","limits":[{"analyte":"毒死蜱","category":null,"limit_mg_kg":0.01}],
       "sampling":[{"scope":"global","key":null,"rate":0.001}],
       "selection":{"mode":"stratified","per_supplier":3,"limit":50},"expires_in_minutes":1440}'

# 2) 查看完整报告：结论差异、影响供应商、建议补采量、新增检测项次/工时、权限检查
curl -sS http://127.0.0.1:8432/api/food/rule-governance/rehearsals/1/report -H "Authorization: Bearer $TOKEN"

# 3) 由其他具备审批权限的账号发布（通过后原子生效为新版本），或驳回
curl -sS -X POST http://127.0.0.1:8432/api/food/rule-governance/rehearsals/1/decision \
  -H "Authorization: Bearer $APPROVER_TOKEN" -H 'Content-Type: application/json' \
  -d '{"decision":"approved","comment":"同意按期发布"}'

# 4) 查询：预演状态/报告摘要、全部审批意见、最终规则版本
curl -sS http://127.0.0.1:8432/api/food/rule-governance/rehearsals -H "Authorization: Bearer $TOKEN"
curl -sS http://127.0.0.1:8432/api/food/rule-governance/rehearsals/1/approvals -H "Authorization: Bearer $TOKEN"
curl -sS http://127.0.0.1:8432/api/food/rule-governance/current-version -H "Authorization: Bearer $TOKEN"
```

安全语义：

- 历史结论不可变：预演只在报告里重算，绝不回写 `food_test_results` / `food_lots`；发布新版本同样不追溯改写历史。
- 原子发布：审批通过与新版本插入、旧版本下线在同一个 `IMMEDIATE` 事务内完成，部分唯一索引保证同一时刻只有一个 `active` 版本。
- 重复审批：已通过/驳回的预演再次提交返回 409，尝试动作以 `duplicate` 留痕，不产生新版本。
- 过期候选：超过 `expires_in_minutes` 的预演只能重新发起；过期审批以 `expired` 留痕。
- 基础版本漂移：预演生成后若已有别的候选发布，旧候选以 `basis_stale` 拒绝，要求基于新版本重新预演。
- 服务重启：重启不执行任何自动发布；崩溃残留的 `running` 预演在下次访问时标记为 `interrupted`，审批返回 409 并留痕，永不会误生效。
- 报告防篡改：报告带 SHA-256 校验和，审批前重新校验，不一致则拒绝发布。

## 测试

```bash
python -m pytest
```

测试覆盖身份初始化、登录、用户与角色维护、权限计算、账号停用后的会话撤销、审计脱敏、农产品批次、抽样检测、运输追踪、风险任务、规则预演审批发布（含过期、重复审批、报告篡改与重启残留防护）和数据库时间格式。

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

## 数据一致性

SQLite 连接默认启用外键、WAL、busy timeout 与同步写入策略。需要跨多张表更新的管理操作在即时事务中执行，失败会整体回滚。会话令牌只保存摘要；用户停用会撤销仍有效的会话。审计事件保存操作者、动作、资源、结果和前后状态，但不会保存明文密码或令牌。
