# MCP 变更独立审计与发布计划

日期：2026-09-09。首次审计结论：**旧候选 NO-GO**。后续代码修复和中立复核已完成，见 [修复交付记录](mcp-audit-fixes-20260909.md)；生产镜像、回滚及生产验收门禁仍待发布阶段完成，不能把本地修复通过视为生产 GO。

本文件保留首次审计结果和后续执行计划，不是已完成的发布记录。首次审计轮没有修改产品代码、推送、构建发布镜像或操作生产。规范依据为本候选中的 [生产发布 runbook](../.agents/runbooks/production_release.md)、release、deploy、github 规则。

## 1. 审计锚点和独立结论

中立审计者：`neutral_release_audit`，未参与实现；以完整差异为审计范围，重点检查相关权限、生命周期调用方及隔离数据库行为，并非逐行核对全部 55 个文件。审计时工作树干净。审计者另已只读复核本发布计划，未发现会导致错误放行或危险回滚的新增关键遗漏；这不替代修复后候选的重新验收。

| 项目 | 已核实值 |
| --- | --- |
| 分支 | `fix/mcp-global-disable` |
| 本轮审计 SHA | `c85cf8186e81ec12780f1a2f67bc05a2b335ebd3` |
| 审计差异 | `36195133..c85cf818`，55 个文件 |
| 权威公司主分支 | 远端 `yybpc` 的 `company/main`，本地跟踪引用 `yybpc/company/main` |
| 本轮 fetch 后主分支 | `5ed4e42cae8fbfc72d2747f375e4290516b1758b` |
| 分支差异 | 主分支独有 1 个提交，候选独有 4 个提交 |
| 产品版本 | backend/frontend 均为 `1.10.3`，本次不自动升级版本 |
| 最终发布 SHA / ID / digest | 待修复、集成和复核后冻结；当前 SHA 不能作为可发布版本 |

不要使用陈旧的本地 `company/main` 分支代替权威远端跟踪引用。

### P1-1：私有刷新改变目录归属，扩大工具可见范围

- 入口：`POST /api/admin/mcp-servers/{id}/refresh-tools`，私有安装的合法 Agent manager 省略 `agent_id`，远端发现一个新工具。
- 原因：[mcp_refresh_service.py](../backend/app/services/mcp_refresh_service.py) 约 501 行依赖 `assignment_seed` 判定新工具来源；无 Agent 上下文时写成 `source="admin"`。[mcp_catalog_policy.py](../backend/app/services/mcp_catalog_policy.py) 因而把整个目录判成共享。
- 独立 Docker/PostgreSQL 实证：`shared=False → True`，sources 从纯 `agent` 变成 `agent/admin`；同一 manager 后续修改返回 403；新增工具对同租户另一个未安装该 MCP 的 Agent 可见。
- 已确认的是目录暴露和原管理者失权；本轮没有继续绑定并执行，不能宣称已经实证凭据被使用。
- 修复要求：按刷新前可靠的目录归属确定来源；私有无 Agent 范围的请求须明确解析合法安装或拒绝。一起检查 HTTP/stdio 发现、测试连接持久化、恢复和批量入口，不能靠调用参数意外提升共享级别。保留原 owner 管理权限和其他 Agent 不可见性。

### P1-2：启动清理仍删除没有绑定的平台共享工具

- 原因：[tool_seeder.py](../backend/app/services/tool_seeder.py) 约 412 行的 `clean_orphaned_mcp_tools()` 删除 `tenant_id IS NULL` 且无 AgentTool 的 MCP 工具，没有保护共享目录；[main.py](../backend/app/main.py) 约 221 行启动时调用。
- 独立 Docker/PostgreSQL 实证：真实 `source="admin"` 平台工具在清理前存在，清理后被删除。尚未绑定的新工具、最后一次解绑后的工具均受影响。
- 这是既存遗漏，但直接破坏本次“共享目录解绑后保留”的承诺，因此是本次发布阻断。
- 修复要求：清理复用统一共享/私有策略和锁顺序，只删除可证明为私有且孤立的记录；归属不确定的数据保留。验证解绑后重启及清理与绑定并发。

### P2：连接检查与执行路径差异，待提供者边界验证

[mcp_connection_check.py](../backend/app/services/mcp_connection_check.py) 使用直接发现路径；[agent_tools_mcp_runtime.py](../backend/app/services/agent_tools_mcp_runtime.py) 还处理 Project 来源配置和 Smithery Connect 安装路由。可能出现安装实际可用而连接检查失败，或检查未使用实际调用的凭据。

这是代码推断，未独立复现。发布前用真实隔离提供者验证私有 Smithery 和 Project 继承配置；如确认，统一有效配置与路由解析，同时保持连接检查不修改目录。不能以现有通用 HTTP 检查结果关闭此项；无法验证时保持门禁未通过。

## 2. 已完成证据与未验证边界

| 证据 | 当前结论 |
| --- | --- |
| 新中立审计的独立数据库复现 | 两项 P1 已证实；只读挂载候选，新建隔离数据库，完成后已删除，未接触生产 |
| `git diff --check 36195133..HEAD` | 本轮通过 |
| 800 行门禁 | 本轮复核 48 个变更源码文件，最高 752 行，通过；后续新增修复必须重算 |
| Docker 用户可见措辞扫描 | 本轮通过 `scripts/check_user_visible_keyword.py` |
| 既有受影响回归 | 交付记录为 33 文件、171 passed、5 warnings；不是本轮独立重跑，也未覆盖上述遗漏 |
| 既有真实本地验证 | HTTP JSON-RPC initialize/list/call；A/B 私有凭据隔离；共享双 Agent 覆盖；旧 scene 下平台禁用；3008 浏览器保存与检查 |
| 既有前端构建 | TypeScript/Vite 通过；完整 npm prebuild 尚未补齐 |
| 尚未完成 | 两项 P1 修复、P2 关闭、新主分支整合后复核、真实 stdio/Smithery、完整镜像、迁移/旧镜像回滚演练、生产验收 |

既有详细记录见 [交付记录](mcp-shared-private-delivery-20260909.md)。其中“共享目录保留”等产品描述是目标行为，现已发现上述反例，不能再据该记录直接放行。首次审计脚本的 detached User 错误是装配问题，修正装配后成功复现，不列为产品缺陷。

## 3. 执行顺序与每阶段出口

| 阶段 | 工作 | 出口 |
| --- | --- | --- |
| 0 修复及主分支整合 | 合入最新 `yybpc/company/main`，修复两项 P1，关闭 P2；更新规范和验收记录 | 无未关闭的目录归属、清理、凭据/连接检查问题 |
| 1 冻结与本地验收 | 精确最终 SHA 的受影响 Docker 验证、浏览器、独立复审 | 干净已提交候选、版本一致、所有适用门禁通过 |
| 2 发布准备 | 获得相应授权后推送已审代码，构建/推送后端和前端不可变镜像，收集 digest | 双镜像同 SHA，linux/amd64，可拉取、可验证 |
| 3 生产准备 | 获得生产只读/备份/迁移授权后核实拓扑，生成候选与回滚 compose，在线备份和兼容迁移 | 旧服务仍运行，镜像全部预拉取，回滚演练通过 |
| 4 GO 与切换 | 负责人确认窗口和中断策略，一条命令更新四个应用角色 | 身份、健康及 MCP 验收通过 |
| 5 观察与关闭 | 至少 30 分钟紧密观察、24 小时跟踪 | 固化发布记录；异常按已授权的回滚条件执行 |

阶段 0/1 不要求增加测试框架或全量跑库。补齐能证明上述反例已消失的数据库/API/提供者行为检查，优先复用既有用例；不写源码形状或镜像实现的无意义测试。

## 4. 本地验收矩阵与 Docker 命令

所有 Python、测试、编译、构建和浏览器操作仅在隔离 Docker 环境执行。`RC_DIR` 指向最终候选，`RC_TEST_ENV` 是隔离配置文件，`RC_NETWORK` 只能连接任务自己的数据库；密码不写入本文件或 Git。测试镜像先验证工具齐备并记录 image ID。

```bash
git fetch yybpc company/main
git merge-base --is-ancestor yybpc/company/main HEAD
git diff --check 36195133..HEAD
git status --short
docker run --rm --entrypoint python -v "$RC_DIR:/repo:ro" -w /repo \
  "$RC_TEST_IMAGE" scripts/check_user_visible_keyword.py
docker run --rm --network "$RC_NETWORK" --env-file "$RC_TEST_ENV" \
  --entrypoint python -v "$RC_DIR:/repo:ro" -w /repo/backend \
  "$RC_TEST_IMAGE" -m pytest -q -p no:cacheprovider \
  tests/test_mcp_installation_ownership.py tests/test_mcp_refresh_revocation.py \
  tests/test_mcp_binding_management.py tests/test_mcp_platform_visibility.py \
  tests/test_mcp_servers_overrides_api.py tests/test_mcp_servers_dry_run.py \
  tests/test_mcp_execution_process.py tests/test_scene_runtime_settings.py \
  tests/test_agent_self_install_stdio.py tests/test_mcp_recovery.py
```

上述是确定的重点回归子集。最终修复后按受影响调用方补齐既有 33 文件中的其余适用检查、新主分支索引验证，以及下表缺失场景；记录完整最终命令，不将子集结果冒充全部验收。受影响模块 compile/import、Ruff、800 行统计均在 Docker 中运行并保存结果。

| 必须观察的行为 | 通过标准 |
| --- | --- |
| 私有目录无 Agent 范围刷新、HTTP/stdio 发现 | 明确拒绝或正确私有刷新；来源不提升，其他 Agent 看不到，owner 仍可管理 |
| 平台共享目录未绑定、最后解绑、bootstrap 重启 | server/tool/schema 均保留；并发绑定不被误删 |
| 同 Agent 同名不同 key/header 安装 A/B/A | 两个独立 ID，第二个带后缀，A 重试复用；实际提供者分别收到 A/B，名称不暴露秘密 |
| 两个 Agent 使用同一共享 MCP | 共享工具定义一致；私有 key/header/env 覆盖互不影响；工具启停是二次过滤 |
| 越权变更及删除绑定 | 普通 Agent 管理者不能改共享 URL/命令/schema/prompt 或刷新共享目录；删除须目标 Agent 管理权限 |
| 平台全局禁用 | 清单、schema、prompt 隐藏；旧 scene、别名、缓存中的后续调用均不发往提供者；已发出的网络请求不能承诺撤回 |
| Project、Smithery、stdio | 检查与实际执行使用相同有效身份/路由；检查不写目录；真实 sandbox/提供者证据可追溯 |
| 3008 浏览器与代理 | 共享编辑只允许覆盖与过滤；私有安装有独立后缀；保存/检查成功，无 pageerror；API、WS/MCP 路由正常 |

前端使用最终镜像构建中的 `npm run build` 执行既有 prebuild、TypeScript、Vite，不绕过 prebuild。浏览器在 Docker Chromium 中通过隔离的 3008 前端代理访问实际 API；渲染最终镜像 nginx 模板并检查 `API_UPSTREAM` 和相关路由。

## 5. 数据、种子和新主分支影响

当前 MCP 差异没有迁移、依赖清单或 Dockerfile 改动，但修改了 `list_installed_mcp_servers`、`refresh_mcp_server` 数据库种子说明。工具 schema 的权威来自数据库；必须验证最终种子实际落库，不能只看代码。清理修复后再演练 bootstrap，不以未修复的启动流程执行所谓“无害同步”。

最新主分支新增 `202609091100_agent_tool_lookup_index.py`：revision `agent_tool_lookup_index`，parent `repair_bootstrap_indexes`，为 `agent_tools(agent_id, tool_id)` 创建非唯一 btree 索引，使用 CONCURRENTLY、5 秒锁超时、120 秒语句超时。

因此集成后的发布不能直接写“无数据库迁移”。授权核实生产 revision 后：已应用则校验索引有效/ready，无新增 DDL；未应用则在隔离生产 schema 副本演练当前 revision → 最终 head、并发读写和失败重试，随后授权在线执行。若生产与候选存在更多迁移或种子差异，重新扩大计划范围。

```bash
docker compose -p "$COMPOSE_PROJECT" -f "$CANDIDATE_COMPOSE" \
  run --rm --no-deps --entrypoint alembic backend upgrade heads < /dev/null
```

执行后核实唯一预期 head、索引定义/valid/ready 和旧服务健康。超时或迁移失败时停止切换，不自动 downgrade。旧镜像必须在迁移后的隔离库和新版本产生的 MCP 私有配置数据上演练；仅结构兼容不够。

## 6. 不可变镜像与依赖策略

冻结前再次 fetch；主分支变动则整合、重验、重新冻结。`RELEASE_ID=v1.10.3-${RELEASE_SHA7}`。源码归档目录置于任何其他 Git checkout 之外；写入 backend/COMMIT，归档构建设置 `BUILDX_GIT_INFO=false`，校验 OCI 标签及 provenance 都指向最终 SHA。

默认尝试复用正在生产运行且可信的后端 **linux/amd64 manifest digest** 为 `BACKEND_DEPS_IMAGE`。必须同时证明：生产到候选 pyproject 无变化、候选文件与镜像内 `/app/pyproject.toml` 校验和一致、镜像可信且架构正确、Python 版本与候选固定 base 一致、依赖安装/运行库/复制契约未改变。任一不满足就省略 `CLAWITH_DEPS_IMAGE`，显式走 `deps-build` 并审查解析出的依赖。

以下是发布授权后执行的模板；registry、builder、稳定构建参数及 digest 从安全运维清单和已验证环境填入。不得将未填变量的模板当成可执行发布记录。

```bash
RELEASE_SHA7=${RELEASE_SHA:0:7}
RELEASE_ID="v1.10.3-${RELEASE_SHA7}"
git archive "$RELEASE_SHA" | tar -x -C "$RELEASE_CONTEXT"
printf '%s\n' "$RELEASE_SHA" > "$RELEASE_CONTEXT/backend/COMMIT"
export BUILDX_GIT_INFO=false
docker buildx build --builder "$BUILDER" --platform linux/amd64 --progress=plain \
  --cache-from "type=registry,ref=$BACKEND_REPOSITORY:buildcache-$CURRENT_SHA7-amd64" \
  --cache-to "type=registry,ref=$BACKEND_REPOSITORY:buildcache-$RELEASE_SHA7-amd64,mode=max" \
  --build-arg "CLAWITH_DEPS_IMAGE=$BACKEND_DEPS_IMAGE" \
  --label "org.opencontainers.image.revision=$RELEASE_SHA" \
  --label "org.opencontainers.image.version=$RELEASE_ID" \
  --tag "$BACKEND_REPOSITORY:$RELEASE_ID" --push "$RELEASE_CONTEXT/backend"
docker buildx build --builder "$BUILDER" --platform linux/amd64 --progress=plain \
  --cache-from "type=registry,ref=$FRONTEND_REPOSITORY:buildcache-$CURRENT_SHA7-amd64" \
  --cache-to "type=registry,ref=$FRONTEND_REPOSITORY:buildcache-$RELEASE_SHA7-amd64,mode=max" \
  --label "org.opencontainers.image.revision=$RELEASE_SHA" \
  --label "org.opencontainers.image.version=$RELEASE_ID" \
  --tag "$FRONTEND_REPOSITORY:$RELEASE_ID" --push "$RELEASE_CONTEXT/frontend"
docker buildx imagetools inspect "$BACKEND_REPOSITORY:$RELEASE_ID"
docker buildx imagetools inspect --raw "$BACKEND_REPOSITORY:$RELEASE_ID" > "$EVIDENCE_DIR/backend-manifest.json"
docker buildx imagetools inspect "$FRONTEND_REPOSITORY:$RELEASE_ID"
docker buildx imagetools inspect --raw "$FRONTEND_REPOSITORY:$RELEASE_ID" > "$EVIDENCE_DIR/frontend-manifest.json"
```

cache 输入输出不能是同一个 tag，不再覆盖旧 mutable cache。首次无 SHA cache 可省略读取；cache 只用于加速。carrier 模式意外下载 Python 包应中止排查。镜像推送后记录 index digest、amd64 manifest digest、标签、COMMIT 和构建日志；候选及回滚 compose 均按 digest 固定。AIO 仅当最终生产差异涉及其来源/base 或明确要求时重建。

## 7. 生产准备、备份和窗口

本轮没有查询生产，当前 SHA/digest、Compose project、服务数量、数据库 revision、AIO、主机/路径均待授权从实时部署和安全清单核实，不能从旧会话或分支名称推断。

当前、候选、回滚统一按 runbook 的 backend/worker/connector/frontend 四角色描述；三个后端角色使用同一后端 digest，前端匹配同一 SHA。核实实际 service 名称和角色映射后固化命令，不能拿本地开发 compose 猜生产拓扑。保留 PostgreSQL、Redis、对象存储和未变 AIO。

旧服务运行期间预拉候选和回滚镜像，校验架构、标签、COMMIT，使用显式 project 渲染两份 compose 并验证 nginx。切换窗口不构建、不拉镜像、不临时编辑配置。观察 turn/trigger/connector 基线，等待已批准时限内自然排空；单副本替换可能短暂中断，需明确接受。如要求严格不中断，先完成蓝绿/滚动及原子流量切换演练。

| 对象 | 保护及省略决策 |
| --- | --- |
| compose/env/镜像身份 | 保存当前/候选/回滚文件、校验和、digest、revision；秘密快照存安全位置，不进 Git |
| PostgreSQL | 因种子和潜在索引迁移，准备一次在线一致性 custom dump；覆盖 MCP server/tool/binding/override、相关配置及引用；跨表/FK范围不确定时选整库，由数据负责人确认 |
| Redis | 当前 MCP 差异未改其持久化/恢复语义；最终生产差异确认相同后可省略新增专用快照并记录理由 |
| workspace/对象存储/CLI 二进制和上传状态 | 当前差异不改格式、内容或二进制；确认最终差异后可省略本次新增快照，不因此改动既有备份策略 |

在线备份靠近切换时间执行，设置有界锁等待；`pg_restore --list`、校验和及隔离恢复演练通过。不得逐表随意恢复破坏外键，也不得在生产演练恢复。无法在线一致备份或需要未计划的跨存储回溯时 NO-GO。

## 8. 切换与验收

全部适用门禁、备份、回滚演练及授权齐备后，由发布负责人作出 GO。唯一应用替换命令：

```bash
docker compose -p "$COMPOSE_PROJECT" -f "$CANDIDATE_COMPOSE" \
  up -d --no-deps --no-build backend worker connector frontend
```

不预先 stop/down，不按角色分批，不 remove-orphans，不重启依赖。这条命令不代表严格零停机。验收通过后才原子替换规范 compose 文件，文件替换不触发第二次重启。

验收四角色 digest/SHA、预期 connector 数量、健康与 restart count、前端 200、`/api/health`、版本、登录态 API、历史会话、普通 turn、WebSocket 重连。数据库版本及种子符合预期；复核第 4 节 MCP 矩阵关键项。禁止以生产调试代替本地验证；生产 smoke 只使用专门授权的既有测试身份和会话，涉及外部消息需对应明确授权。

至少观察 30 分钟，随后跟踪 24 小时。对比切换前 HTTP 5xx/p95、锁/长 idle transaction、重启、工具错误、turn 恢复、connector 与重复投递。以下为待负责人确认的操作阈值：

- 身份/租户越权、目录或数据丢失、错误 SHA、重复外部输出、迁移失败：立即停止推进并启动预定处置；迁移失败且尚未切换时保持旧服务。
- 启动超过本地演练确认的健康时限仍不就绪：回退；建议上限 5 分钟，最终由实测确定。
- 5xx 比基线高 1 个百分点持续 5 分钟，或 p95 超过基线两倍持续 5 分钟：判定失败并回退，除非明确定位为无关外部事故且负责人记录继续理由。
- 不能执行已准备的安全回滚：发布前就是 NO-GO；不能等故障时再研究。

## 9. 回滚及安全兼容

本次修复涉及权限和目录保留。**旧版可能恢复全局禁用绕过，且旧 bootstrap 可能再次删除无绑定共享目录。能够启动不等于可以安全回滚。** 发布前须证明旧镜像在新数据库/私有配置下不会越权、串凭据或误删；不满足时准备并演练保留安全修复的回退镜像。若需限制受影响 MCP 操作，必须使用已存在且实测有效的控制，并明确授权，不能假设旧版本的全局开关就能阻断调用。

唯一应用回滚命令：

```bash
docker compose -p "$COMPOSE_PROJECT" -f "$ROLLBACK_COMPOSE" \
  up -d --no-deps --no-build backend worker connector frontend
```

默认保留兼容的新增索引和切换后数据，不自动 downgrade/还原数据库。旧镜像若不认识新 Alembic revision，回滚 compose 只能启动非 bootstrap 角色；不能用 `ALLOW_MIGRATION_FAILURE` 掩盖错误。恢复 bootstrap 需要其镜像识别数据库迁移图；这些必须提前演练。

MCP 当前差异只改两项 builtin 描述，未新增参数，不应无条件执行其他领域的回滚清理。按实际生产旧 SHA 逐项判定 runbook 中 scene_runtime_schema、rollback_im_recall、rollback_agent_self_settings、rollback_reasoning_controls、project_legacy_rollback 是否跨越边界，并记录适用/不适用。若旧版早于显式 continue，执行规定的精确 anchor snapshot/apply 流程及 `TURN_RECOVERY_ENABLED=false`，不能扫描并重放新工作。需要 writer freeze 的 helper 必须有事先授权的维护/蓝绿流程。

数据库时间点恢复会丢弃切换后写入，只能由数据负责人明确决定，相关存储一致恢复；不得自动删/合并私有安装或重写审计历史来兼容旧版。回滚后重复身份、健康及 MCP 核心验收，继续观察。

## 10. 发布记录与授权清单

以下字段当前未填，不伪造具名责任人或生产事实。执行前由发布负责人落实：

```text
RELEASE_OWNER=待指定姓名
OPERATOR=待指定获授权执行人
VERIFIER=中立审计者及最终验收负责人
DATA_OWNER=待指定姓名
ROLLBACK_OWNER=待指定姓名
WINDOW_START / WINDOW_END / AUTHORIZED_INTERRUPTION_POLICY=待确认
CURRENT_RELEASE_SHA / CURRENT_BACKEND_DIGEST / CURRENT_FRONTEND_DIGEST=待实时核实
CURRENT_AIO_DIGEST / COMPOSE_PROJECT / CURRENT_DB_REVISION=待实时核实
RELEASE_SHA / PRODUCT_VERSION / RELEASE_ID=修复和复核后冻结
NEW_BACKEND_DIGEST / NEW_FRONTEND_DIGEST / BACKUP_ID=执行后记录
```

本次“审计和发布计划”不授予外部操作权限。源码 push、镜像/cache push、tag/Release、生产只读、备份、迁移、配置变更、切换/回滚、真实外部 smoke 消息分别按 runbook 授权；在现有授权明确覆盖某项时不重复索要。发布准备应先产出可审阅的最终候选、命令和证据，再进入相应授权步骤。

归档最终 diff/main/SHA、审计问题关闭证据、实际 Docker 命令和日志、镜像身份、迁移前后、备份范围/省略原因、批准人及时间、中断观察、30 分钟验收和 24 小时跟踪结果。保存证据后按授权清理本任务临时环境；保留用户未合并修改和其他工作树。
