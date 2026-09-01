# 大文件模块拆分生产发布手册

日期：2026-09-01
状态：**NO-GO；可进入最终候选收口，不授权生产操作**

本文是本次“大文件标准模块化拆分”迭代的具体发布执行手册，必须与
`.agents/rules/release.md` 和 `.agents/runbooks/production_release.md` 一起使用。
若本文与通用生产 runbook 冲突，以通用 runbook 为准。

本文只授权计划评审和本地候选准备，不授权 fetch/merge、push、Tag、镜像
推送、生产只读检查、备份、迁移、切换、真实外部通道消息或回滚。每个外部
状态变更都必须在对应步骤获得明确授权。

## 1. 发布目标和完成定义

本次发布只有一个产品目标：把超过 800 行的手写源代码拆成符合现有架构的
标准模块，保持所有外部行为、权限、持久化、协议、工具 schema、UI 和运行
拓扑不变。

完成必须同时满足：

- 所有非豁免手写源文件不超过 800 个物理行；
- 相对发布基线没有有意业务逻辑、API、数据库 schema、seed 数据、配置或
  用户界面行为变化；
- Webchat、H5 Chat、A2A、Project、IM、MCP、工具、定时任务、Webhook、
  Browser/Web RPA、后台任务和管理端行为与基线一致；
- backend、frontend 和 AIO 都来自同一个最终不可变 Git SHA；
- 所有生产引用都固定到 registry digest，不仅固定 tag；
- 候选和上一版本都能在切换前启动，回滚命令已渲染并演练；
- 切换后通过验收矩阵，并完成至少 30 分钟重点观察和 24 小时跟踪。

任何“只是拆分”的推断都不能代替行为验证。发现能力差异时必须先停止发布，
把它当作回归处理，不能在发布窗口现场重构。

## 2. 当前候选事实

以下是编写本文时的可核验事实，不是最终发布身份：

| 项目 | 当前值 |
|---|---|
| 权威集成基线 | 本地 `company/main@7cbda6b92a51902fe837679191f8a9109e006a5c` |
| 候选分支 | `feat/agent-handbook-self-contained` |
| 已审计应用 HEAD | `9cea971e297623a77e14d08c15a19bd826a2a953` |
| 主线关系 | 上述 `company/main` 是候选祖先；候选领先 266 个提交 |
| backend/frontend 上游版本 | `1.10.3` / `1.10.3` |
| AIO 上游基础镜像 | `all-in-one-sandbox:1.9.3` |
| 变更规模 | 新增 504、修改 148、删除 2、重命名 1 个路径 |
| 迁移变化 | 无新 revision；历史迁移的 helper/downgrade 迁到应用模块 |
| AIO 变化 | 有；运行脚本、Shell/Bash 实现拆为同目录模块 |

本文提交后 HEAD 会变化，且发布前仍要同步最新 `company/main`。因此最终字段
必须在候选冻结后重新填写：

```text
FINAL_MAIN_SHA=
RELEASE_SHA=
RELEASE_SHA7=
APP_RELEASE_ID=v1.10.3-<RELEASE_SHA7>
AIO_RELEASE_ID=1.9.3-patched-<RELEASE_SHA7>
BACKEND_DIGEST=
FRONTEND_DIGEST=
AIO_DIGEST=
PREVIOUS_BACKEND_DIGEST=
PREVIOUS_FRONTEND_DIGEST=
PREVIOUS_AIO_DIGEST=
```

禁止把 `9cea971e`、分支名或本地验证 tag 直接当作最终生产版本。

## 3. 当前 GO/NO-GO 状态

### 已有证据

| 门禁 | 结果 |
|---|---|
| 800 行门禁 | Docker 中扫描 1623 个手写源文件，0 个超限 |
| AIO UTM 原生生命周期测试 | 21/21 通过 |
| 当前 Backend 到 UTM AIO 远程回归 | 25 passed，3 deselected |
| Browser/Web RPA | 包含在上述远程回归中，相关 9 项通过 |
| AIO 健康 | `healthy`，验证期间 restart count 为 0 |
| AIO 候选架构 | `linux/amd64` |
| AIO 本地验证镜像 ID | `sha256:e68a7fa677eecc2d58a03b15042291e782efd95978512e3e83c4d2ee2762fb0c` |
| 拆分遗漏修复 | `_STDOUT_LIMIT` 导入已在 `9cea971e` 修复并提交 |

UTM 验证镜像复用了已有构建结果和缓存，没有重新下载基础镜像。它只用于
候选验证，没有最终 release SHA 的完整生产 provenance，不能直接推送生产。

### 当前阻断项

当前为 NO-GO，至少还有以下阻断项：

1. `git diff --check company/main...HEAD` 当前输出 232 行空白错误，必须清零；
2. 最终 `company/main` 尚未在发布窗口前重新获取和合并；
3. 本文提交后必须选定新的最终 SHA，并在该 SHA 重跑所有硬门禁；
4. 完整 backend、frontend、数据库升级、旧镜像兼容和最终 3008 回归证据尚未
   绑定到最终 SHA；
5. AIO 的 3 个共享文件系统场景尚未在共享 `/data/agents` 的环境中验证；
6. backend、frontend、AIO 的生产镜像尚未构建、推送并记录 digest；
7. 生产拓扑、负责人、窗口、备份遗漏和中断策略尚未由现场确认；
8. 候选/回滚 Compose 尚未按生产实际服务名渲染。

以上任意一项未关闭都不得切换生产。

## 4. 角色、授权和中断策略

发布会议开始前填写：

```text
RELEASE_OWNER=
OPERATOR=
VERIFIER=
DATA_OWNER=
ROLLBACK_OWNER=
WINDOW_START=
WINDOW_END=
ROLLBACK_DECISION_DEADLINE=
AUTHORIZED_APP_INTERRUPTION=
AUTHORIZED_AIO_SESSION_INTERRUPTION=
COMMUNICATION_CHANNEL=
```

授权边界：

1. **AUTH-0：计划批准**。允许收口本地候选，不允许外部写入；
2. **AUTH-1：主线同步和最终提交**。允许 fetch/merge/commit；
3. **AUTH-2：push 和 Tag**。push 与 annotated tag 分别确认；
4. **AUTH-3：生产镜像构建和 registry push**；
5. **AUTH-4：生产只读盘点和候选/回滚镜像预拉取**；
6. **AUTH-5：备份或明确批准备份遗漏**；
7. **AUTH-6：AIO 切换**；
8. **AUTH-7：应用角色一次性切换**；
9. **AUTH-8：专用身份的真实 IM/Webhook smoke**；
10. **AUTH-9：数据恢复**。二进制回滚不包含数据恢复授权。

单副本 Compose 替换可能短暂中断请求。AIO 容器替换还会终止内存中的 Shell、
Jupyter、Browser 和 MCP 会话，即使 Agent 工作区使用持久卷。若不能接受任何
请求或运行会话中断，本手册的单副本方案为 NO-GO，必须先提供经过演练的
蓝绿/滚动拓扑和原子流量切换。

## 5. 冻结最终候选

获得 AUTH-1 后，在独立 worktree 中执行：

```bash
git status --short
git fetch company company/main
git rev-parse company/main
git merge-base --is-ancestor company/main HEAD
git log --oneline company/main..HEAD
git diff --stat company/main...HEAD
```

如果 `company/main` 已移动，先合并并解决冲突，然后重新执行全部差异审计和
相关 Docker 门禁。不得用 rebase/force push 改写已经审计的迭代历史。

清理当前空白错误后执行：

```bash
git diff --check company/main...HEAD
git status --short
git rev-parse HEAD
```

工作树必须干净，`git diff --check` 必须无输出。再确认：

```bash
test "$(cat backend/VERSION)" = "1.10.3"
test "$(cat frontend/VERSION)" = "1.10.3"
```

在 Docker 内执行最高优先级行数门禁：

```bash
docker run --rm --entrypoint python \
  -v "<repository-root>:<repository-root>" \
  -w "<exact-release-worktree>" \
  <backend-image-with-git> \
  scripts/check_source_line_limit.py
```

必须记录扫描文件数、退出码和完整输出。任何文件超过 800 行立即 NO-GO，
不得加历史债务 allowlist，也不得通过压缩代码规避。

最终冻结：

```bash
RELEASE_SHA=$(git rev-parse HEAD)
RELEASE_SHA7=$(git rev-parse --short=7 HEAD)
APP_RELEASE_ID="v1.10.3-${RELEASE_SHA7}"
AIO_RELEASE_ID="1.9.3-patched-${RELEASE_SHA7}"
test -z "$(git status --short)"
```

由独立 Reviewer 对 `company/main...$RELEASE_SHA` 做中立审计，确认所有改变均
属于模块边界、导入/导出兼容、测试拆分、800 行门禁或配套手册；任何无法
解释的行为差异都阻断发布。

## 6. 最终 SHA 的 Docker 验证

所有 Python、pytest、lint、构建、集成和浏览器验证只在 Docker 内进行，使用
隔离 PostgreSQL，禁止写入共享开发数据库或生产数据。

### 6.1 结构和导入兼容

- 运行 `scripts/compare_python_symbols.py` 对拆分前基线和最终候选比较公共符号；
- 运行 `scripts/capture_runtime_contracts.py` 比较关键 facade 的运行时导出；
- 对 backend 执行 `compileall` 和全量 import smoke；
- 对 frontend 执行 TypeScript 和生产构建；
- 检查 circular import、lazy import、monkeypatch/export 兼容和模块副作用；
- 结构比较只能做辅助证据，不能替代行为测试。

### 6.2 Backend 和 PostgreSQL

使用专用测试镜像、当前源码只读挂载和隔离 PostgreSQL：

```bash
docker run --rm --entrypoint python \
  --network <isolated-compose-network> \
  -v "<exact-release-worktree>/backend:/app:ro" -w /app \
  -e PYTHONPATH=/app \
  -e DATABASE_URL="postgresql+asyncpg://<test-user>:<test-password>@<test-postgres>:5432/<isolated-test-db>" \
  -e AGENT_DATA_DIR=/tmp/agents \
  <backend-test-image> \
  -m pytest -q -p no:cacheprovider
```

要求：

- 全量 suite 0 failed；所有 skip 必须逐项分类并与基线比较；
- 对 071 历史迁移拆出的 helper/downgrade 做 fresh database 和既有结构升级；
- 记录升级前/后 Alembic revision、schema 差异和关键表行数；
- 最终 diff 若确认没有新 migration/seed 语义，生产迁移应为 no-op；
- 用上一 backend 镜像连接升级后的隔离数据库完成兼容启动和关键 API smoke；
- 工具 seeder 必须验证数据库工具 schema、AgentTool enablement 和实际 LLM 工具
  输出与基线一致，而不是只比较 Python 常量。

### 6.3 Frontend 和本地 3008

在 Docker 中对最终 frontend 源码执行：

- prebuild 脚本；
- TypeScript 类型检查；
- production build；
- `frontend/nginx.conf.template` 渲染检查；
- 浏览器通过 `http://localhost:3008` 验证真实构建产物。

浏览器矩阵至少覆盖：

| 能力 | 最低验收 |
|---|---|
| 登录/企业管理 | 登录、用户、企业设置、渠道配置正常 |
| Agent | 列表、创建、详情、设置、工具、Skill、MCP 正常 |
| Webchat | 新会话、历史、流式回复、工具调用、断线恢复正常 |
| H5 Chat | 登录态、历史、流式回复、工具卡片、移动布局正常 |
| A2A | 创建、发送、历史持久化、双方身份和失败恢复正常 |
| Project | 创建、成员、工作项、Runs、文件、Git、图谱、能力设置正常 |
| Published Page | 列表、访问控制、渲染、会话正常 |
| 文件/媒体 | 上传、下载、预览、消息投递和回执正常 |
| 后台能力 | 定时任务、Trigger、Webhook、后台任务正常 |
| 通道 | 使用专用测试身份验证钉钉，禁止触碰无关 Agent/会话 |

必须检查浏览器 Console、Network、WebSocket 和最终持久化结果。不能只确认页面
能打开。真实 IM/Webhook 外发需要 AUTH-8。

### 6.4 AIO 的 amd64 和 UTM 验证

AIO 源码发生变化，必须在最终 SHA 重新生成生产制品。构建目标固定
`linux/amd64`，优先使用已有 buildx builder 和 cache。若未改变依赖却开始完整
下载，约 10 秒内取消，确认没有后台构建，再检查 builder、cache ref、Dockerfile
和 context；不得把异常 cache miss 当正常重建。

UTM 中只运行 AIO。Backend 保持在宿主机 Docker，通过受控隧道或批准的测试
网络访问 AIO；不得为了方便在 UTM 启动 Backend。

最终 AIO 验证包含：

1. 镜像 manifest 为 `linux/amd64`；
2. 镜像内拆分文件 hash 与最终 SHA 完全一致；
3. AIO Shell lifecycle、Jupyter timeout/lifecycle、MCP lifecycle 全部通过；
4. 当前候选 Backend 到 UTM AIO 的 Shell、Python、后台任务、Browser 和
   Web RPA 回归全部通过；
5. 上一生产 Backend 到新 AIO 的同一套 API smoke 通过；
6. 新 Backend 到上一生产 AIO 的同一套 API smoke 通过；
7. AIO health 为 healthy，restart count 为 0，日志没有非预期 traceback；
8. 验证完成后只清理本次临时容器/卷/隧道，不动共享或上一版本实例。

此前远程回归 deselect 的 3 项依赖 Backend 与 AIO 共享 `/data/agents`：两个签名
身份上下文场景和一个运行中日志场景。它们不计入远程 API 的 25 项绿灯，但也
不能永久忽略。发布前必须二选一：

- 在与生产一致的共享存储拓扑中让 3 项全部通过；或
- 证明生产基线本来就不提供这些共享存储语义，并由 Release Owner 明确记录
  它们不属于生产承诺，同时完成旧/新同拓扑基线对比。

若生产宣称支持这 3 项而无法提供共享存储验证，立即 NO-GO。

## 7. 构建和发布不可变镜像

获得 AUTH-3 后，在干净的最终 SHA context 构建。backend/frontend 必须同批，
AIO 使用同一个 Git revision，但使用其上游基础镜像版本命名。

```bash
RELEASE_SHA=<full-release-sha>
RELEASE_SHA7=<release-sha7>
APP_RELEASE_ID="v1.10.3-${RELEASE_SHA7}"
AIO_RELEASE_ID="1.9.3-patched-${RELEASE_SHA7}"
REGISTRY=<approved-registry-namespace>
BUILDER=<verified-buildx-builder>

BACKEND_IMAGE="$REGISTRY/backend:$APP_RELEASE_ID"
FRONTEND_IMAGE="$REGISTRY/frontend:$APP_RELEASE_ID"
AIO_IMAGE="$REGISTRY/aio-sandbox:$AIO_RELEASE_ID"

BACKEND_CACHE="$REGISTRY/backend:buildcache-amd64"
FRONTEND_CACHE="$REGISTRY/frontend:buildcache-amd64"
AIO_CACHE="$REGISTRY/aio-sandbox:buildcache-amd64"
```

在构建 context 中写入完整 SHA 的 `backend/COMMIT`，然后执行已审核的稳定参数：

```bash
docker buildx build --builder "$BUILDER" \
  --platform linux/amd64 --progress=plain \
  --cache-from type=registry,ref="$BACKEND_CACHE" \
  --cache-to type=registry,ref="$BACKEND_CACHE",mode=max \
  --build-arg CLAWITH_IMAGE_MIRROR=docker.m.daocloud.io \
  --build-arg CLAWITH_PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  --label org.opencontainers.image.revision="$RELEASE_SHA" \
  --label org.opencontainers.image.version="$APP_RELEASE_ID" \
  --tag "$BACKEND_IMAGE" --push backend

docker buildx build --builder "$BUILDER" \
  --platform linux/amd64 --progress=plain \
  --cache-from type=registry,ref="$FRONTEND_CACHE" \
  --cache-to type=registry,ref="$FRONTEND_CACHE",mode=max \
  --build-arg CLAWITH_IMAGE_MIRROR=docker.m.daocloud.io \
  --label org.opencontainers.image.revision="$RELEASE_SHA" \
  --label org.opencontainers.image.version="$APP_RELEASE_ID" \
  --tag "$FRONTEND_IMAGE" --push frontend

docker buildx build --builder "$BUILDER" \
  --platform linux/amd64 --progress=plain \
  --cache-from type=registry,ref="$AIO_CACHE" \
  --cache-to type=registry,ref="$AIO_CACHE",mode=max \
  --label org.opencontainers.image.revision="$RELEASE_SHA" \
  --label org.opencontainers.image.version="$AIO_RELEASE_ID" \
  --tag "$AIO_IMAGE" --push docker/aio-sandbox
```

禁止用 `docker compose build` 或 `docker compose up --build` 生产制品。推送后：

```bash
docker buildx imagetools inspect "$BACKEND_IMAGE"
docker buildx imagetools inspect "$FRONTEND_IMAGE"
docker buildx imagetools inspect "$AIO_IMAGE"
```

记录三个 index digest、`linux/amd64` manifest、OCI revision/version、backend
`COMMIT` 和构建日志 checksum。Compose 引用必须使用
`<tag>@sha256:<digest>`。

## 8. 生产只读准备和备份决策

获得 AUTH-4 后，从批准的运维清单和实际运行状态重新解析，不依赖历史记忆：

- Compose 项目名、目录、实际 service 名和 role 映射；
- backend、worker、connector、frontend 是否分服务，或是否由组合服务承载；
- AIO 是 Compose service 还是独立容器、其 AgentData 挂载和网络边界；
- 当前/候选/回滚 tag、digest、revision、健康和 restart count；
- 当前 Alembic revision、工具 seed 状态、活动 turn/task/trigger/AIO session；
- nginx 渲染配置、API/WS/MCP/上传/对象存储 upstream；
- 磁盘、数据库锁、连接池、5xx、延迟和通道错误基线。

仓库存在单 backend `PROCESS_ROLE=all` 和多服务组合 role 两种示例，不能据此
猜测生产服务名。候选和回滚 Compose 必须按现场实际拓扑渲染，并证明一条命令
覆盖所有应用角色。

本次预期无 schema、seed、Redis、对象存储或 AgentData 格式变化，因此默认
备份矩阵为：

| 权威状态 | 默认决策 | 必须保存的证据 |
|---|---|---|
| PostgreSQL | 最终 diff 确认无语义变化后可不 dump | revision、schema/seed 对比、Data Owner 批准 |
| Redis | 不快照 | 最终 diff 无语义变化及遗漏理由 |
| AgentData | 不归档、不删除 | 挂载路径/卷、权限、格式无变化证据 |
| 对象存储 | 不快照 | 最终 diff 无写入/格式变化证据 |
| AIO 会话 | 视为可中断的内存态 | 排空结果和批准的中断策略 |
| 配置/Compose | 必须保存 | 当前/候选/回滚文件、渲染结果和 checksum |
| 镜像 | 必须保存 | 当前/候选 digest、manifest 和 revision |

如果最终主线同步引入 migration、seed、数据修复或格式变化，以上遗漏决定立即
失效，必须按通用生产 runbook 重新计算在线备份范围。无法在线一致快照且回滚
依赖数据恢复时，本次发布 NO-GO，不能临时 `stop`/`down` 解决。

即使没有新 migration，仍要从候选镜像用覆盖 entrypoint 的一次性命令确认
`alembic upgrade heads` 为安全 no-op，并验证上一镜像能连接当前 schema。

## 9. 候选和回滚文件

生产切换前必须准备两套 digest-pinned Compose：

- `<candidate-compose>`：三个候选 digest；
- `<rollback-compose>`：三个上一版本 digest；
- 两者使用同一个明确的 `<production-compose-project>`；
- 数据库、Redis、对象存储、AgentData 卷、网络、权限和环境完全一致；
- 不在切换窗口 build、pull 或现场编辑 Compose。

执行：

```bash
docker compose -p <production-compose-project> -f <candidate-compose> config > <candidate-rendered>
docker compose -p <production-compose-project> -f <rollback-compose> config > <rollback-rendered>
```

审查渲染结果后预拉取候选和回滚镜像，并再次核对 digest。准备精确服务集合：

```text
AIO_SERVICE=<actual-aio-service>
APP_SERVICES=<all services carrying api, worker, connector and frontend roles>
```

例如，多服务模板可能映射为 `backend-api backend-trigger frontend`，其中
`backend-trigger` 承载 bootstrap/worker/connector；单服务模板可能映射为
`backend frontend`，其中 backend 承载 `PROCESS_ROLE=all`。只能采用生产现场
验证后的映射。

## 10. 切换步骤

### 10.1 最终 GO 检查

Release Owner 逐项签字：

- 最终 SHA 是最新 `company/main` 后的干净、已审计提交；
- `git diff --check` 和 800 行门禁通过；
- backend/frontend/AIO 最终 SHA 测试全部通过；
- 双向 Backend/AIO 兼容通过；
- digest、Compose、nginx、schema、seed、备份遗漏和回滚均已核对；
- 活动 turn、task、trigger、connector 和 AIO session 已观察或排空；
- 单副本中断策略已批准；
- 切换窗口内不需要 build、pull、编辑或依赖下载。

### 10.2 先替换 AIO

只有在“上一 Backend→候选 AIO”兼容通过后才允许先切 AIO。获得 AUTH-6，
旧应用保持运行，不停止数据库、Redis 或应用角色：

```bash
docker compose -p <production-compose-project> \
  -f <candidate-compose> up -d --no-deps --no-build \
  <actual-aio-service>
```

禁止预先 stop/down、删除 AIO AgentData 卷、`--remove-orphans` 或临时改端口。
立即验证：digest/revision、health、restart count、AgentData 权限、Shell、Jupyter、
MCP、Browser/Web RPA，以及上一 Backend 的真实 AIO API smoke。

若 AIO 验收失败，先只回滚 AIO，应用不切换：

```bash
docker compose -p <production-compose-project> \
  -f <rollback-compose> up -d --no-deps --no-build \
  <actual-aio-service>
```

### 10.3 一次性替换全部应用角色

AIO 稳定后获得 AUTH-7。使用生产实际 service 集合执行一条命令：

```bash
docker compose -p <production-compose-project> \
  -f <candidate-compose> up -d --no-deps --no-build \
  <all-application-services>
```

这一条命令必须同时覆盖 API、worker、connector 和 frontend 角色。禁止
backend-first、逐角色替换、预先 stop/down、`--remove-orphans`、build、pull，
也不得重启 PostgreSQL、Redis、对象存储或已经通过验收的 AIO。

## 11. 生产验收矩阵

### 11.1 身份、拓扑和健康

- 所有应用角色和 AIO 都运行目标 digest/revision；
- backend/worker/connector/AIO healthy，frontend 返回 200；
- restart count 为 0，实例数和 connector 数与切换前一致；
- `/api/health`、版本和 provenance 正确；
- Alembic revision、工具/Skill seed 和 AgentTool enablement 与基线一致；
- nginx 的 `/api`、`/ws`、`/mcp`、uploads 和对象存储路由正确。

### 11.2 核心行为

- 登录、租户边界、用户和企业设置；
- Webchat 普通对话、历史、工具调用、断线重连和恢复；
- H5 Chat 登录态、消息、工具卡片和实时更新；
- A2A 会话、消息持久化、身份、失败恢复；
- Project 创建、成员、工作项、Runs、文件、Git 和图谱；
- MCP server 注册、工具发现、调用、超时和错误传递；
- AIO Shell/Python/Jupyter、后台 job、运行中日志、Browser/Web RPA；
- Schedule/Trigger/Webhook 创建、触发、幂等、记录和失败恢复；
- 文件/媒体上传下载、外发回执、确认和 recall；
- 管理端 Agent、工具、Skill、渠道和 Published Page。

### 11.3 真实通道

获得 AUTH-8 后只使用批准的测试 Agent、测试用户和测试会话。钉钉可使用专用
测试通道完成入站、回复、文件/媒体和回执验证。禁止创建无关用户、PAT、会话
或消息，禁止触碰“小智”之外未授权的真实 Agent。

每项记录请求/会话 ID、时间、期望、实际结果和清理动作。任何租户越权、消息
重复、错误身份、错误 recall、持久化缺失或恢复失败立即回滚。

## 12. 观察和回滚

切换后至少重点观察 30 分钟，并保留 24 小时跟踪。与切换前基线比较：

- HTTP 5xx、P95/P99、WebSocket 断连和 restart count；
- PostgreSQL 锁、长事务、连接池和 migration/seed 错误；
- LLM、tool、MCP、AIO、context、turn recovery 错误；
- Schedule、Trigger、Webhook、worker 和 connector backlog；
- 外发 `failed`、`unknown`、`partial`、stale `pending` 和重复投递；
- Browser、Jupyter、Shell session 泄漏和 AIO 资源占用。

立即回滚条件：错误 digest/SHA、启动不健康、持续重启、租户隔离/认证回归、
数据丢失、错误身份、重复外发、A2A/Project/聊天主路径失败、MCP/AIO 大面积失败、
无法执行已准备的回滚命令。

应用回滚必须一次性恢复全部应用角色：

```bash
docker compose -p <production-compose-project> \
  -f <rollback-compose> up -d --no-deps --no-build \
  <all-application-services>
```

如果问题在 AIO，再独立恢复上一 AIO digest：

```bash
docker compose -p <production-compose-project> \
  -f <rollback-compose> up -d --no-deps --no-build \
  <actual-aio-service>
```

禁止 `down`、删除 volume、逐角色混用 SHA、现场重建或默认执行 Alembic
downgrade。由于本次预期无 schema/data 语义变化，默认二进制回滚保留当前
schema 和发布后数据。任何数据恢复都需要 Data Owner 的 AUTH-9，并先在隔离
目标验证恢复材料。

回滚后重复健康、身份、Webchat、A2A、MCP/AIO、后台任务和真实通道的相关
子集，并继续观察。

## 13. 发布证据包

发布结束后保存不可变、可审计的证据包：

```text
release-record.txt
source-main-release-sha.txt
git-diff-check.txt
git-diff-stat.txt
source-line-limit.txt
independent-review.txt
backend-tests.txt
frontend-checks.txt
migration-and-old-image-compatibility.txt
aio-build-and-utm-tests.txt
browser-3008-matrix.txt
real-channel-smoke.txt
backend-manifest.txt
frontend-manifest.txt
aio-manifest.txt
candidate-compose-rendered.yml
rollback-compose-rendered.yml
nginx-rendered.conf
pre-and-post-baseline.txt
cutover-log.txt
rollback-drill.txt
observation-30m.txt
observation-24h.txt
```

`release-record.txt` 至少记录负责人、授权点、主线/发布 SHA、三个候选和上一
digest、Compose 项目和 service 映射、migration before/after、备份范围和遗漏
批准、切换/验收时间、所有 skip/deselect、告警、最终 GO/回滚结论。

## 14. 最终签字清单

以下全部为“是”才能宣布发布完成：

- [ ] 最新权威主线已合并，最终 worktree 干净；
- [ ] `git diff --check` 无输出；
- [ ] 1623 或最终实际数量的手写源文件全部不超过 800 行；
- [ ] 中立 Reviewer 确认没有有意业务逻辑变化；
- [ ] backend 全量、PostgreSQL、frontend build 和 3008 回归通过；
- [ ] Webchat、H5、A2A、Project、IM、MCP、任务、Webhook 均通过；
- [ ] AIO 最终 amd64 镜像、双向兼容和共享存储决策通过；
- [ ] backend、frontend、AIO 来自同一最终 SHA 且按 digest 固定；
- [ ] 候选和回滚 Compose 已预拉取、渲染和审查；
- [ ] 备份范围/遗漏由 Data Owner 批准；
- [ ] AIO 和应用切换均使用预先审核的一条命令；
- [ ] 生产验收无失败，30 分钟观察通过；
- [ ] 发布证据包完整，24 小时观察责任人已接手。

任何未勾选项都必须保持 NO-GO，不能用“只是代码拆分”豁免。
