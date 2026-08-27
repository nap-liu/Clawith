# AI 原生项目管理生产发布计划

日期：2026-08-27
状态：**GO for production preparation / NO-GO for production execution until explicitly authorized**

本计划依据仓库已固化的 Claude 项目记忆研发手册、`.agents/rules/release.md` 与 `.agents/runbooks/production_release.md` 编制。本文只授权评审和准备，不授权合并、推送、打 Tag、生产构建、停写、备份、迁移、切换、真实渠道验证或回滚。

本次备份范围按用户在 2026-08-27 的明确授权做例外收敛：**不备份 AgentData，不做全库备份，只对迁移和启动 seed 直接影响的既有表执行在线逻辑热备份**。该授权覆盖本发布的默认完整备份要求，但不改变候选迁移必须在短维护窗口排空 writer 后执行的约束。

## 一、发布目标与不可变边界

### 1.1 权威代码

| 项目 | 当前值 |
|---|---|
| 权威主线 | `yybpc/company/main@d963d85cbc4852feaa070a13915023f231ab8b4e` |
| 候选分支 | `feature/ai-native-project-management` |
| 已验证应用代码 SHA | `d8d6263d8fc96832b15fb9e27f196ee025425a00` |
| 当前计划基线 HEAD | `51730a783aa9b43d69400fbad57df8f8fc878070` |
| backend/frontend 版本 | `1.10.3` / `1.10.3` |
| 当前候选 Alembic head | `repair_tenant_boundary_triggers` |
| AIO 变更 | 0；不构建、不推送、不切换 |

`RELEASE_SHA` 必须在本计划提交、最终主线合并和文档收口后重新读取，不能直接使用可变分支名。私有发布标识为：

```text
v1.10.3-<RELEASE_SHA 前 7 位>
```

如果 `yybpc/company/main` 移动，必须重新合并、重新做完整差异审计，并根据实际代码变化重跑对应 Docker 门禁。若最终 `RELEASE_SHA` 相对 `d8d6263d` 只有文档变化，可复用应用 SHA 的行为验证；若 backend、frontend、deploy、Helm、Compose、CI 或迁移任一生产文件变化，则相关 SHA 门禁全部失效并重跑。

### 1.2 发布范围

包含：

- backend 与 frontend 同一不可变 SHA 的生产镜像；
- AI 原生项目管理、协作网络、项目 Agent、项目工具/MCP/Skill、项目设置、模板、Skill Market、文件与 Git 交付链路；
- 项目 Agent 与标准 Agent 的资产、会话、工作区、工具和运行隔离；
- 工作负载并发门禁、数据库会话释放、项目故障隔离、Subagent/Scheduler/Trigger/恢复链路；
- 12 个候选迁移文件，升级目标为单一 head `repair_tenant_boundary_triggers`；
- 旧服务兼容回滚 helper 与必要的 IM recall rollback helper。

不包含：

- AIO sandbox 镜像；
- 私有语义版本升级；
- Plaza 恢复。Plaza 全局退场是已授权产品决策，必须保持路由跳转、API 404、工具关闭和 runtime 拒绝；
- 完整键盘与读屏复核，已按用户决定排除；
- 生产拓扑扩容、滚动新旧版本并行、全新灰度系统或无关重构。

## 二、已完成候选证据

| 门禁 | 结果 |
|---|---|
| 主线关系 | `yybpc/company/main@d963d85c` 是当前候选祖先 |
| 完整后端分区 | 2026-08-27 最终 Docker/PostgreSQL 主套件 `2676 passed, 28 skipped`；需独立 schema 前提的项目 API/工具矩阵 `65 passed`、legacy 回滚 `1 passed`，均无产品断言失败 |
| 项目协作 | `515 passed` |
| 市场与项目设置 | `41 passed` |
| 最新主线影响套件 | `78 passed` |
| 项目故障隔离 | 专项 `10 passed`；相关标准后台/手动/调度回归 `36 passed` |
| legacy 回滚行为 | PostgreSQL 长期行为 `1 passed`；精确旧镜像 API/worker 演练通过 |
| 前端 | 完整 prebuild、TypeScript、Vite production build 通过，10,297 modules transformed |
| 严格真实项目 RC3 | 4/4 工作项、32 Runs、5 个角色、200 个事件、2 个里程碑；失败、活跃、未恢复、未关联均为 0 |
| 当前压力验证 | 2026-08-27 最终复跑 5×700，共 3,500 次；错误 0；125/125 溢出正确拒绝；P50/P95/P99 为 399.67/692.55/772.39 ms；DB pool 峰值 30/30；结束 active=0、waiting=0；恢复 0.01 ms；健康前后 200 |
| Plaza 授权退场 | 3011 `/api/plaza/posts` 为 404；三项工具均 `enabled=false / is_default=false`；runtime switch 为 false |
| 公开页面兼容 | 修正验证容器的统一存储根后，公开/受保护页面、水印、会话和发布工具行为 `36 passed` |
| 真实项目复核 | 3011 通过公共 API 重新严格核验 RC3：4/4 工作项、32 Runs、5 个角色、200 事件、2 里程碑，失败/活跃/未恢复/未关联均为 0 |
| 生产结构迁移演练 | 从本地 1.10.3 数据库只读副本的 `webhook_event_sequence` 升级至 `repair_tenant_boundary_triggers` 成功；既有受影响表迁移前后行数一致 |
| 热备恢复演练 | 11 张 allowlist 表在线逻辑备份、`pg_restore --list`、结构校验和与隔离恢复行数比对全部通过；archive SHA-256 `2ebf8f7c31abef8755a8268c0f9ce8a5565b5af8d183ae7cedebad32dba5c727` |
| 本地候选 | 3011 返回健康 200 / 版本 1.10.3；backend 关键源码 checksum 与当前工作树一致，frontend `dist/index.html` checksum 与运行容器一致；应用代码冻结后仅文档变化 |

压力验证使用真实 Docker、PostgreSQL、正式 admission/session/数据库边界代码，供应商等待为受控模拟；它证明当前单实例容量与恢复机制，不构成真实供应商或多实例 fleet 容量承诺。

## 三、已知风险与发布条件

### R1 · 项目能力故障扩散

已通过故障注入证明项目查询、项目状态和项目服务异常不会阻断标准 Agent 普通会话、计划任务、手动任务、Trigger 和普通 Subagent。共享 PostgreSQL、Redis、LLM、宿主机或网络整体故障仍会影响两类能力，不能错误归类为项目域隔离可解决的问题。

### R2 · 并发门禁是实例级

当前容量门禁和部分 turn 状态是进程/实例级。发布不得采用未验证的新旧版本重叠或临时扩副本策略。生产准备必须记录当前 API、worker、connector 拓扑；若发现多副本语义依赖 fleet 全局容量或共享进程内状态，立即 NO-GO，先完成独立验证。

### R3 · 兼容回滚数据库身份

旧服务只能使用 `project_legacy_rollback apply` 创建的专用非 owner 数据库身份，并拆分为 `PROCESS_ROLE=api` 和 `PROCESS_ROLE=worker`。owner DSN 会绕过 RLS，`PROCESS_ROLE=all` 会尝试执行旧版本不理解的迁移，两者均是禁止项。

### R4 · Fresh Database 历史迁移

从空 PostgreSQL 执行全历史迁移存在主线既有重复列问题；现有生产结构升级路径已通过。发布窗口不得把“从空库初始化”作为候选启动路径。若灾备方案依赖现场从空库重建，必须先提供经验证的基线恢复步骤，否则 NO-GO。

### R5 · 精简热备份边界

用户已明确批准不备份 AgentData、不做全库 dump，只备份本次迁移与 seed 直接影响的既有表。热备份必须使用单连接一致性快照、短锁等待和低峰执行，不停止生产写入；优先使用复制延迟在批准阈值内、且不承载线上读取的只读副本，否则仅在主库资源余量满足门禁时执行受控单连接 dump。`pg_dump` 仍会对目标表申请 `ACCESS SHARE` 锁，本文不把在线热备份描述成零锁或零负载。热备份只提供受影响表的选择性恢复材料，不构成全库同点恢复，也不能恢复未备份的 AgentData。备份期间出现锁等待、延迟、连接池或 IO 异常必须立即中止，不得以完成备份为由影响正常业务。

## 四、人员、窗口与授权点

以下字段必须在发布会议中填写，未填写不得进入停写：

| 项目 | 负责人/值 |
|---|---|
| Release Owner / GO-NO-GO | `<name>` |
| Operator | `<name>` |
| Verifier | `<name>` |
| Data Owner | `<name>` |
| Rollback Owner | `<name>` |
| 维护窗口 | `<start> — <end>` |
| 预计停止写入 | `<minutes>` |
| 回滚决策截止时间 | `<timestamp>` |
| 沟通频道 | `<channel>` |
| 用户通知文本 | `<approved message>` |

授权检查点：

1. **AUTH-0 计划批准**：只允许只读核对生产事实和准备命令。
2. **AUTH-1 合并/Tag/构建推送**：合并、Tag、backend/frontend 构建与推送分别授权。
3. **AUTH-2 生产预拉取**：只允许按 digest pull、渲染配置和只读基线核对。
4. **AUTH-3 在线热备份**：允许在不停止生产写入的前提下，对明确表清单执行低负载热备份。
5. **AUTH-4 排空、迁移与切换**：热备份验证通过后，允许短时排空 writer、启动候选 bootstrap 和切流。
6. **AUTH-5 真实渠道 smoke**：只在专用测试会话执行，单独授权。
7. **AUTH-6 数据恢复**：仅数据损坏或兼容回滚失败时由 Data Owner 单独批准。

## 五、冻结候选与构建制品

### 5.1 冻结

```bash
git fetch yybpc main
git rev-parse yybpc/company/main
git rev-parse HEAD
git merge-base --is-ancestor yybpc/company/main HEAD
git status --short
git diff --check
```

确认工作树干净、主线是候选祖先、backend/frontend `VERSION` 均为 `1.10.3`。记录：

```text
RELEASE_SHA=<full sha>
RELEASE_ID=v1.10.3-<sha7>
```

### 5.2 SHA 绑定门禁

- 对最终生产树执行中立 `yybpc/company/main...RELEASE_SHA` diff 审计。
- 在隔离 PostgreSQL 中从生产只读结构副本升级到 `repair_tenant_boundary_triggers`。
- 若应用代码与 `d8d6263d` 完全相同，核对并引用第二节证据；否则重跑完整 backend、前端 build、项目协作、标准能力影响、压力和 rollback 门禁。
- Docker 验证不得使用生产数据库，不得用源码正则/形状测试替代行为验证。
- 验证候选 nginx 渲染结果中的 `API_UPSTREAM` 非空，并覆盖 `/api`、`/ws`、`/mcp`、uploads、对象存储和 SDK 路由。

### 5.3 构建与推送

在授权构建机上从 `RELEASE_SHA` 的干净上下文同时构建 backend/frontend，写入同一 `COMMIT` provenance，并只发布 `linux/amd64`：

```bash
RELEASE_SHA=<full-release-sha>
RELEASE_ID=v1.10.3-<release-sha7>
REGISTRY=<approved-registry-namespace>

docker buildx build --platform linux/amd64 --progress=plain \
  --label org.opencontainers.image.revision="$RELEASE_SHA" \
  --label org.opencontainers.image.version="$RELEASE_ID" \
  --tag "$REGISTRY/backend:$RELEASE_ID" --push <clean-context>/backend

docker buildx build --platform linux/amd64 --progress=plain \
  --label org.opencontainers.image.revision="$RELEASE_SHA" \
  --label org.opencontainers.image.version="$RELEASE_ID" \
  --tag "$REGISTRY/frontend:$RELEASE_ID" --push <clean-context>/frontend
```

记录并校验两个 registry index digest 均包含 `linux/amd64`。生产 Compose 必须引用 `tag@sha256:digest`，禁止仅使用 mutable tag。AIO 保持原 digest。

| 制品 | 记录值 |
|---|---|
| RELEASE_SHA | `<fill>` |
| RELEASE_ID | `<fill>` |
| backend index digest | `<fill>` |
| frontend index digest | `<fill>` |
| previous backend digest | `<fill from running container>` |
| previous frontend digest | `<fill from running container>` |
| AIO digest | `<unchanged; fill>` |
| migration before | `<fill>` |
| migration after | `repair_tenant_boundary_triggers` |

## 六、生产只读准备

在不修改生产状态的前提下完成：

1. 从批准的运维清单解析生产 Compose 项目、目录、域名和备份目标，不把凭证写入文档。
2. 记录运行中的 backend/frontend/AIO tag、digest、实例数、角色、健康和 restart count。
3. 记录当前 Alembic revision、PostgreSQL/Redis/AgentData/对象存储大小、磁盘空间和近期错误基线。
4. 保存当前 Compose 文件；渲染候选 Compose 并逐项审查差异。
5. 验证 frontend 实际模板生成的 nginx 配置及 upstream，不使用同名但未装载的文件代替。
6. 按不可变 digest 预拉取 backend/frontend；旧服务保持运行。
7. 准备 `<approved-data-root>/releases/<timestamp>-<release-id>/` 作为受影响表热备份与发布证据目录；不创建 AgentData 归档或全库 dump。
8. 生成引用 previous digest 的回滚脚本；脚本不得执行 `docker compose down`、删除 volume 或默认 Alembic downgrade。

任一 digest、拓扑、受影响表清单、热备份目标、迁移起点或 upstream 无法确认：**NO-GO**。

## 七、受影响表在线热备份

获得 AUTH-3 后：

1. 不关闭入口、不停止 API/worker/trigger/scheduler/connector，不创建 AgentData、Redis 或对象存储备份。
2. 备份清单只包含迁移或启动 seed 直接修改的既有表：

   ```text
   alembic_version
   agents
   skills
   skill_files
   tools
   agent_tools
   chat_sessions
   subagent_runs
   agent_relationships
   agent_agent_relationships
   agent_activity_logs
   ```

   新建的 `projects`、`project_*`、`skill_installs` 表在迁移前不存在，不进入迁移前热备份；候选代码和迁移是其结构来源。任何新增既有表修改必须先更新该 allowlist，再重新批准计划。
3. 在热备份前记录主库或只读副本的 LSN、复制延迟、事务数、连接池、锁等待、CPU、IO、HTTP P95/P99 与 5xx 基线。优先选择不承载线上读取且复制延迟处于批准阈值内的副本；若只能使用主库，当连接池、CPU、IO 或存储吞吐任一指标已达到 60%，或存在长事务、锁等待、复制积压时不得启动备份，直接 NO-GO。
4. 使用单个 `pg_dump` 进程取得一个一致性 snapshot，不使用并行 jobs。命令模板：

   ```bash
   BACKUP_DIR=<approved-data-root>/releases/<timestamp>-<release-id>
   PG_DSN=<approved-readonly-or-primary-dsn>

   nice -n 10 ionice -c2 -n7 pg_dump "$PG_DSN" \
     --format=custom \
     --no-owner --no-privileges \
     --lock-wait-timeout=3s \
     --file="$BACKUP_DIR/affected-tables.dump" \
     --table=public.alembic_version \
     --table=public.agents \
     --table=public.skills \
     --table=public.skill_files \
     --table=public.tools \
     --table=public.agent_tools \
     --table=public.chat_sessions \
     --table=public.subagent_runs \
     --table=public.agent_relationships \
     --table=public.agent_agent_relationships \
     --table=public.agent_activity_logs
   ```

   若目标主机不支持 `ionice`，去掉该包装但仍保持单进程；不得因此提升并发。`nice`/`ionice` 只降低客户端进程竞争，不替代数据库侧监控与中止门禁。凭证只从批准的 secret/config 注入，不写入脚本或记录。
5. 同时导出受影响表 DDL、`activity_action_enum`、三个 tenant-boundary function/trigger 定义、行数和 checksum manifest，用于选择性恢复审计；不导出无关业务表。
6. 热备份期间每 5 秒观察一次基线指标。任一条件发生立即终止 `pg_dump`：
   - 产生超过 3 秒的锁等待；
   - HTTP P95 较基线上升超过 20% 或出现新增 5xx；
   - 数据库连接池持续超过 80%；
   - CPU、IO wait 或复制延迟超过运维批准阈值；
   - 业务方报告可感知延迟。
7. `pg_restore --list "$BACKUP_DIR/affected-tables.dump"` 必须成功；校验 archive checksum、表清单、snapshot LSN、开始/结束时间和监控曲线。发布窗口前在隔离 PostgreSQL 中完成一次受影响表 restore drill。

热备份失败或对线上产生可感知影响：**立即中止备份，本次发布 NO-GO；生产服务保持原样运行**。不允许通过停写补做全库或 AgentData 备份。

## 八、切换顺序

获得 AUTH-4 后严格按顺序执行。热备份阶段本身不停服；由于当前迁移包含 `ALTER TABLE`、约束、索引和 seed 更新，不能承诺完全无锁切换，必须在批准的短维护窗口排空 writer，避免把 DDL 锁竞争转嫁给在线请求：

1. 关闭新入口，等待活动 turn 在批准时限内排空；记录未完成 turn。
2. 停止 frontend ingress、API、worker、trigger、scheduler、connector 和其他全部 writer；确认旧 backend 进程数为 0，不允许新旧版本重叠。
3. 激活同时固定 candidate backend/frontend digest 的 Compose。
4. 仅启动一个具备 bootstrap 权限的 candidate backend。
5. 等待安全补丁、Alembic upgrade、builtin tool seed、Uvicorn startup 和 `/api/health` 200。
6. 确认 migration head 为 `repair_tenant_boundary_triggers`，版本与 provenance 为目标 `RELEASE_SHA`。
7. 确认 Plaza API 为 404，三项 Plaza 工具保持 disabled/non-default。
8. 启动已批准的 API/worker/connector 角色；不得临时改变副本数或角色拓扑。
9. 启动 frontend，确认使用同一 `RELEASE_SHA` digest 组合。
10. 开放验证入口，暂不全面恢复流量，执行第九节验收。

迁移失败、启动不健康、SHA/digest 混用、工具 seed 异常或预期退场能力重新出现：**立即停止候选 writer 并进入兼容回滚**。

## 九、生产验收矩阵

### 9.1 原有平台能力

- backend health、frontend home/login、认证 API 均成功，restart count 保持 0；
- 一个标准 Agent 正常对话、历史读取、WebSocket 重连、附件上传/下载和工作区文件操作成功；
- 标准 Agent 的工具、MCP、Skill、记忆、配置、会话、任务、Schedule 和 Trigger 与切换前快照一致；
- 一个普通 Subagent 能创建、执行、完成和恢复，不受项目容量与项目状态影响；
- 既有 IM 专用测试会话完成发送、持久回执、幂等 replay 和支持渠道 recall；不支持渠道明确返回 unsupported；
- 普通非公开页面水印存在；公开发布页按自身 SDK 水印规则显示，不重复、不缺失；
- Plaza 保持退场：旧路由跳转、API 404、三项工具关闭且 runtime 不可调用。

### 9.2 项目管理与市场

- 创建一个发布 smoke 项目，指定 owner、editor、viewer 和明确 execution user；
- 项目 Agent 从来源 Agent 复制后拥有独立 ID、工作区、会话、工具开关、MCP 和 Skill 资产；修改项目设置不改变来源 Agent；
- Skill Market 浏览、复制为项目资产、项目内编辑/删除/恢复成功；平台工具与 MCP 只引用授权配置，不复制全局凭证；
- 项目规划、负责人启动、A2A 协作、任务完成、文件写入、Git commit、里程碑证据和审计事件完整；
- 工具关闭后真实执行被拒绝，开启后仅当前项目 Agent 可用；
- 项目暂停或项目服务局部失败时只阻断项目入口，标准 Agent smoke 仍成功；生产不做破坏性故障注入，引用本地故障矩阵并监测真实错误边界；
- 模板发布明确选择是否携带项目 Skill 文件，未选择时不泄漏项目私有资产。

### 9.3 并发与运行健康

- 读取 workload capacity、队列、DB pool、HTTP 5xx、延迟和后台 lease 指标；
- 验证正常业务下 active/waiting 可回落，数据库连接不持续占满；
- 不在生产重放 3,500 次模拟压力；引用最终本地证据，只做低风险并发 smoke；
- 任何持续队列增长、容量不释放、标准任务饥饿、重复执行或 stale lease 均判失败。

全部矩阵通过后才恢复完整入口；真实供应商/渠道验证只能在 AUTH-5 后使用专用测试会话。

## 十、观察与停止条件

恢复流量后至少现场观察 30 分钟，并保留 24 小时增强观察：

- HTTP 5xx、P95/P99、backend/frontend restart count；
- capacity active/waiting/rejected、DB pool、Redis lease、Scheduler/Trigger/Subagent backlog；
- 项目 Run 的 queued/running/failed/recovered 和审计链完整性；
- IM delivery failed/unknown/partial/stale pending、recall failure、重复发送报告；
- PostgreSQL、Redis、AgentData、对象存储和 Git workspace 错误；
- 标准 Agent 与项目 Agent 资产、会话、设置或工具的任何串扰。

立即回滚条件：

- 迁移失败或启动超时；
- 认证、租户、项目 ACL 或 Agent 资产隔离回归；
- 标准 Agent 原能力因项目模块故障不可用；
- 数据损坏、错误工作区写入、项目设置污染来源 Agent；
- wrong-message recall、重复 provider 输出或不可恢复的 pending；
- 容量/连接池泄漏、持续队列增长、项目任务被旧 worker 消费；
- 无法执行已准备的兼容回滚。

## 十一、兼容回滚

默认回滚目标是“旧服务和全部原有能力安全运行”，不是删除项目表、列或历史，也不默认 Alembic downgrade。

### 11.1 回到旧应用

1. 关闭入口并停止全部 candidate writer，确认 candidate backend/worker 数量为 0。
2. 使用 candidate 镜像和 owner DSN 执行适用的幂等 helper：

   ```bash
   # 旧版本早于 normalized IM recall 时执行
   python -m app.scripts.rollback_im_recall

   PROJECT_LEGACY_ROLLBACK_PASSWORD='<secret-manager-value>' \
     python -m app.scripts.project_legacy_rollback apply
   python -m app.scripts.project_legacy_rollback status
   ```

3. 使用 helper 返回的 `clawith_project_legacy` 专用 DSN 启动旧 API 与 worker，分别设置 `PROCESS_ROLE=api`、`PROCESS_ROLE=worker`。禁止 owner DSN 和 `PROCESS_ROLE=all`。
4. 恢复 previous backend/frontend digest 组合，不混用候选制品。
5. 验证旧服务 health、登录、标准 Agent CRUD、对话、文件、工具、Task、Schedule、Trigger、Subagent 和 IM；已知项目 Agent 及其 session/task/schedule/trigger/tool/activity/approval/gateway 深链必须全部 not-found，旧 worker 不得领取项目队列。
6. Plaza 在候选和旧版本产品表现可能不同，但全局列表不得包含项目 Agent 标记；不得因回滚修改或删除项目历史。

### 11.2 再次前进到候选

1. 停止全部 legacy API/worker。
2. 使用 candidate image 与 owner DSN 执行：

   ```bash
   python -m app.scripts.project_legacy_rollback restore
   python -m app.scripts.project_legacy_rollback status
   ```

3. 确认专用角色、helper policies、classifier 清除，各表恢复 apply 前 RLS 状态。
4. 确认项目 Agent/member/session/Run/event/message ID、数量和 checksum 与回滚前一致。
5. 再按第八、九节启动并验收候选。

### 11.3 受影响表选择性恢复

本次没有 AgentData、Redis、对象存储或全库备份，因此默认回滚只能使用兼容 helper 和旧应用 digest，保留现有数据。只有确认某个 allowlist 表发生迁移性损坏且 Data Owner 单独授予 AUTH-6 时，才从热备份在隔离数据库恢复、比较并制定逐表/逐行修复方案；禁止直接把整份 table dump 覆盖回生产。受影响表热备份无法提供全库同点恢复，也无法恢复 AgentData，发布负责人必须在 AUTH-4 前确认接受该边界。

## 十二、最终发布记录

发布结束后保存一条不可变记录：

| 字段 | 值 |
|---|---|
| 主线 SHA | `<fill>` |
| RELEASE_SHA / RELEASE_ID | `<fill>` |
| backend/frontend/AIO digest | `<fill>` |
| previous digest 组合 | `<fill>` |
| migration before/after | `<fill>` |
| affected-table hot-backup ID、snapshot LSN 与 manifest checksum | `<fill>` |
| 验证证据与执行人 | `<fill>` |
| 维护窗口、停写和恢复流量时间 | `<fill>` |
| 30 分钟观察结论 | `<fill>` |
| 24 小时观察负责人 | `<fill>` |
| 最终 GO/rollback 决策人 | `<fill>` |

在所有动态值、授权人、备份证据、验收结果和观察结论填写完成前，不得把本计划标记为“已发布”。
