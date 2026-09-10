# Agent 叠加权限维护发布计划

代码及限定独立复审通过，可进入发布准备。**当前没有生产 GO：维护窗口、生产检查、
备份/恢复决策、最终 SHA 和镜像均待授权或准备。默认在线迁移不可用。**
本计划依据当前 release rules 和完整 production release runbook；不授权执行下列操作。

## 范围与候选身份

- 公司主线：`yybpc/company/main`；已合入
  `a6904c9615b6e04aabe9b329c3ea65a378f6ed4f`，原实现基线为 `0cc8c01c`。
- 候选：`feat/agent-additive-permissions` 本地提交与主线合并；尚未冻结发布 RELEASE_SHA。
  发布准备时重新 fetch 主线，若移动则集成、复审受影响差异并重新验证。
- 当前产品版本：backend/frontend 均为 `1.10.3`，不升语义版本；
  最终 RELEASE_ID 为 `v1.10.3-<RELEASE_SHA7>`。
- 包含授权读写、创建/设置/MCP、共用权限编辑器及一次 PostgreSQL 数据归一化。
  backend、worker、connector 使用同一 backend digest，frontend 来自同一 SHA。
- Plaza 已废弃，按用户要求不处理；无依赖、AIO、Redis、workspace 或对象存储格式变更，
  不更改数据库内置工具 schema。MCP 配置工具沿现有 FastMCP 定义更新。
- 权限迁移接在 `model_extra_headers` 之后，保持唯一 head。若实际生产尚未包含
  本次合入的模型能力/请求头主线提交，最终发布差异也须包含其影响及验证，
  不得将整次发布仍描述为仅权限改动。

## 已有证据与发布前补齐项

初轮 60 项相关测试、修复后 5 项聚焦 PostgreSQL 测试、完整前端 prebuild/类型检查/构建、
实际浏览器保存/创建及数据库结果、Ruff、差异和 800 行检查见 README 与 AUDIT。
独立 subagent 确认 P1/P2 修复和 canonical 文档一致，未执行生产检查。

最终 clean SHA 上复核差异、文案、800 行及受主线集成影响的测试，不默认跑全套。
生产父版本/schema-derived 隔离副本、实际数据量下的转换耗时、备份恢复、维护编排、
真实旧镜像/候选启动及四角色恢复演练尚未验证，是 GO 前必需项。
拒绝 downgrade 的测试不等价于成功的备份恢复演练。

下列事实仅在获准的发布会话内由运维清单和实际部署确定，不从历史记录填入：

| 待确定项 | GO 前要求 |
|---|---|
| 发布负责人、操作人、复核人、数据负责人、补救负责人、收尾负责人 | 指定具体人员并确认职责 |
| 维护开始/结束时间、最长中断和排空时间 | 用户明确接受中断策略及超时处置 |
| CURRENT_RELEASE_SHA、三类现有镜像 digest、数据库 revision | 只读检查取得 |
| RELEASE_SHA、两个候选 digest、RELEASE_ID | clean checkout 构建并记录 |
| COMPOSE_PROJECT、当前/候选/恢复 compose、存储清单 | 从实际生产拓扑解析并安全保存 |
| BACKUP_ID、校验和、恢复演练及恢复授权边界 | 数据负责人确认 |

## 发布准备：维护窗口之前完成

授权范围分别确认：源码提交/主线推送、镜像构建/推送、生产只读检查、维护停写、
备份、迁移、配置/切换、专用测试身份 smoke、数据恢复。Git tag/hosted Release 不在本计划执行范围。

按 runbook 4.1 验证当前可信 linux/amd64 backend digest 的依赖复用条件：
pyproject 相等且与镜像内校验和一致、Python 版本一致、依赖安装和运行库合同未变。
满足后使用该 digest 作为 CLAWITH_DEPS_IMAGE；不满足则明确转依赖刷新，不能强行复用。
现有运行镜像未知，因此当前仅确定采用该判定流程。

在获准的干净构建目录写入完整 backend/COMMIT；以下变量必须先由发布记录解析。
稳定 build args 和 digest-pinned base 按候选 Dockerfile、runbook 4 完整核对。
不使用 compose build，切换窗口不构建或下载镜像。

```sh
docker buildx build --platform linux/amd64 \
  --build-arg "CLAWITH_DEPS_IMAGE=$BACKEND_DEPS_IMAGE" \
  --cache-from "type=registry,ref=$BACKEND_REPO:buildcache-$CURRENT_SHA7-amd64" \
  --cache-to "type=registry,ref=$BACKEND_REPO:buildcache-$RELEASE_SHA7-amd64,mode=max" \
  --label "org.opencontainers.image.revision=$RELEASE_SHA" \
  --label "org.opencontainers.image.version=$RELEASE_ID" \
  -t "$BACKEND_REPO:$RELEASE_ID" --push "$CANDIDATE_CONTEXT/backend"
docker buildx build --platform linux/amd64 \
  --cache-from "type=registry,ref=$FRONTEND_REPO:buildcache-$CURRENT_SHA7-amd64" \
  --cache-to "type=registry,ref=$FRONTEND_REPO:buildcache-$RELEASE_SHA7-amd64,mode=max" \
  --label "org.opencontainers.image.revision=$RELEASE_SHA" \
  --label "org.opencontainers.image.version=$RELEASE_ID" \
  -t "$FRONTEND_REPO:$RELEASE_ID" --push "$CANDIDATE_CONTEXT/frontend"
docker buildx imagetools inspect "$BACKEND_REPO:$RELEASE_ID"
docker buildx imagetools inspect "$FRONTEND_REPO:$RELEASE_ID"
```

记录 index 和 amd64 manifest digest、OCI revision、镜像内 COMMIT 与构建 provenance；
输入和输出 cache 不得同名。候选和恢复 compose 都固定 digest；提前拉取并验证
四角色、API_UPSTREAM、nginx、依赖服务保持原配置。AIO 不重建。

## 维护例外与数据保护

这是需单独批准的停写维护方案，不能按 runbook 的默认不预停在线切换执行。
不保证零中断。维护前观察并排空人类会话、触发器和 connector 工作；按 runbook
区分 pending/processing 与实际 lease，记录无法排空的工作及允许的恢复/失败策略。
无可接受的中断策略或无法停止全部 writer 时 NO-GO。

| 数据/证据 | 本次保护范围 |
|---|---|
| PostgreSQL | 停写后、转换前完整 custom-format dump，含关联表和审计；校验并隔离恢复演练 |
| compose、配置、镜像身份 | 安全位置保存当前/候选/恢复文件及脱敏清单、校验和 |
| Redis、workspace、对象存储、CLI | 转换不改变这些格式或内容，不因本次转换自动快照；数据负责人确认省略理由 |
| 新版本接受写入后的恢复 | 重新评估所有耦合状态与新写入损失，禁止只恢复旧权限表 |

备份使用同一个一致性快照，记录 BACKUP_ID，并执行 pg_restore --list 和文件校验。
完整恢复只在隔离目标演练；任何生产恢复必须按已经审核的数据负责人决策执行。

## 已批准维护窗口内的唯一转换路径

1. 启用已演练的维护入口策略，阻止新增工作；完成最终排空和保护对象记录。
2. 明确停止所有旧 backend/worker/connector writer，包含其它副本、一次性进程、
   自动重启/调度和外部管理入口；同时暂停 frontend。核对实际实例和数据库连接，
   不能仅依据四个 Compose 服务名假设停止完整。保持 PostgreSQL 等依赖运行。
3. 完成上述停写备份。旧 writer 从这一步到候选启动期间都不得恢复。
4. 用候选镜像执行一次转换，entrypoint 必须覆盖：

```sh
docker compose -p "$COMPOSE_PROJECT" -f "$CANDIDATE_COMPOSE" \
  run --rm --no-deps --entrypoint alembic backend \
  -x agent_permissions_cutover=offline upgrade heads < /dev/null
```

offline 参数仅断言操作员已核实停写，不会自行停止 writer。迁移使用 5s lock_timeout、
60s statement_timeout；超时即中止，不在现场无审查扩大限制或启动候选绕过失败。
核对目标 head 为 additive_agent_permissions，审计快照完整，原有效访问保持，
公司/私有模式下原隐藏授权未被激活。

5. 确认迁移成功后只启动候选四角色，不能重启旧 writer：

```sh
docker compose -p "$COMPOSE_PROJECT" -f "$CANDIDATE_COMPOSE" \
  up -d --no-deps --no-build backend worker connector frontend
```

无 down、remove-orphans、依赖重启或逐角色混版。完成维护内验收后恢复入口，
验收通过再原子更新 canonical compose，不因此再次重启。

## 验收、补救和观察

四角色 SHA/digest 一致、健康且无异常重启；代理 health/version、认证、历史、
普通会话和 WebSocket 重连正常。仅用事先授权的专用身份验证：
公司 use + 部门/个人 manage 的最高权限、关闭公司仍保留局部授权、跨租户拒绝、
管理员角色降级后的显式授权、失活部门重新启用、REST/MCP/创建/设置一致性。
无新增测试身份或外部 smoke 消息的隐含授权。

权限扩大/跨租户访问、数据损失、错误镜像、启动不健康、重复外发立即停止验收并进入
补救；锁超时或迁移错误立即 NO-GO。延迟/5xx 的数值阈值须由生产基线和发布负责人
在 GO 前确定；不能留到现场决定。近距离观察至少 30 分钟，保留 24 小时跟踪。

- **转换失败且事务完全回滚**：确认 parent revision、原 grants/审计未变后，使用
  预备旧 digest compose 一次启动四角色；这只适用于数据库未成功转换的情况：

```sh
docker compose -p "$COMPOSE_PROJECT" -f "$PREVIOUS_COMPOSE" \
  up -d --no-deps --no-build backend worker connector frontend
```

- **转换已成功**：不执行 Alembic downgrade、不直接切回旧镜像。优先从最终发布 SHA
  制作最小兼容修复，完成相关 Docker 验证和同 SHA 双镜像构建、提前拉取，再四角色切换。
  热修复负责人及构建/cache 路径须提前就绪；不提前伪造热修复产物。
- 如需恢复转换前数据库，必须重新停写，在已批准并演练的完整恢复方案下恢复一致状态，
  明确新写入损失。完成后方可使用上面的旧四角色启动命令。未授权、未演练时不能当作可用回退。

## 证据与收尾

保存源码/主线 SHA、digest、版本、前后 revision、备份和授权、命令结果、验收、
观察和中断记录到可长期保留且位于可删除 checkout 之外的批准位置。
维护配置、补救能力和 24h 观察不可依赖本任务 worktree。

验收及近距离观察完成后，由收尾负责人按 worktree_cleanup workflow 处理本任务
`.worktrees/agent-additive-permissions` 与届时实际创建的发布/验证 checkout。
当前候选未提交且本地验证环境仍引用它，必须保留；共享 3008 环境和其它未完成工作
受保护。任务自有容器、依赖卷和证据由实际引用决定去留，不清理共享资源。
报告移除/保留项及原因，完成记录后才关闭发布。
