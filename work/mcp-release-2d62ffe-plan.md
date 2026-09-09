# MCP 修复发布执行计划

日期：2026-09-09。当前状态：**代码修复及限定中立审计通过，可以进入发布准备；生产切换尚未 GO。** 本文件替代旧候选的执行计划，历史问题和修复证据保留在 [修复交付记录](mcp-audit-fixes-20260909.md)。规范依据：[生产发布 runbook](../.agents/runbooks/production_release.md)。

## 1. 候选、范围和已通过证据

| 项目 | 当前核实结果 |
| --- | --- |
| 发布候选 | `2d62ffed0c0e30f96e15b0fce9b849e6cc3c84c2` |
| 工作分支 | `fix/mcp-global-disable` |
| 权威公司主分支 | `yybpc/company/main`，当前 `5ed4e42cae8fbfc72d2747f375e4290516b1758b` |
| 集成状态 | 本轮 fetch 后主分支独有 0 个提交，候选独有 6 个提交，已包含最新主分支 |
| 当前候选差异 | `5ed4e42c..2d62ffed`，61 个文件；生产实际差异须另以运行 SHA 比较 |
| 产品版本 / 发布 ID | `1.10.3` / `v1.10.3-2d62ffe` |
| 发布组件 | backend、worker、connector 共用后端镜像；frontend 使用同 SHA 前端镜像 |
| 默认不变组件 | PostgreSQL、Redis、对象存储、AIO；实际生产差异若涉及它们，重新判断范围 |
| 当前镜像 digest / 生产身份 | 尚未收集，不填写猜测值 |

发布包含平台禁用在 MCP 可见性与执行端生效、私有同名安装隔离及后缀、共享目录不可变与私有配置覆盖、绑定管理权限、刷新/清理归属保护、Project 和 Smithery 连接身份一致性。不包含 CLI 二进制升级、凭据自动修复或对历史误分类/误删数据的批量修写。

已有验证可复用，不以全量测试或新增测试数量作为发布目标：

- 中立审计的两项 P1 及后续 Project 历史凭据问题已关闭，限定复核无剩余明确阻断。
- 20 文件共 111 项受影响回归：合并运行 110 通过、1 失败；修复 scope_id 兼容问题后，相关 4 文件 21 项通过；占位符 i18n 调整后另有 2 项通过。不能将第一次运行写成全部通过。
- 实际本地 HTTP MCP、真实工具执行子进程、Project 凭据检查/调用、3008 Docker 浏览器通过。
- 前端完整 prebuild、TypeScript、Vite 构建通过；隔离索引迁移往返、数据库种子落库通过。
- 从原集成基线累计 54 个交付源码文件满足 800 行门禁，最高 752 行；编译、措辞扫描和 diff 检查通过。定向 Ruff 通过；tool_seeder 的 10 项原有 F401/E402 已与父提交核对无新增。

本轮仅编制计划，未重复执行上述验证。精确 Docker 测试命令、日志及失败修复过程见交付记录。后续只为代码变化、镜像差异和下面尚未覆盖的风险补验。

## 2. 发布前必须补齐的四组门禁

| 门禁 | 执行工作 | 通过条件 |
| --- | --- | --- |
| A 来源与镜像 | 再次核实主分支，按候选 SHA 导出干净上下文；构建和推送同 SHA 的两个 amd64 镜像 | commit、OCI 标签、COMMIT、provenance 一致；双 digest 固定且可拉取 |
| B 提供者与镜像运行 | 用专用授权身份验证真实 Smithery OAuth/Connect 和 AIO stdio；从候选镜像渲染 nginx、启动隔离四角色及 API/WS/MCP smoke | 检查和执行身份/路由一致；stdio cwd/注销正确；连接检查不改目录、不恢复连接；四角色健康 |
| C 数据与回滚 | 授权读取生产版本/schema；演练实际生产 revision → head；旧/回退镜像运行在新 schema 和新私有配置数据上 | 无权限回退、串凭据或目录删除；不存在必须临时降库/停止写入的隐藏步骤 |
| D 生产准备 | 填写责任人和窗口；确认在线备份范围，准备并预拉候选/回滚镜像，渲染精确 compose | 备份可读且已隔离恢复；命令和中断策略已确认；切换窗口不构建、不拉取、不临时改配置 |

门禁 B 的本地 MockTransport 已覆盖 Smithery JSON/SSE/401，但不等同真实外部服务验收。C 的本地索引往返不等同生产 schema 副本或旧生产镜像兼容演练。未通过的适用门禁不得标记为已完成。

## 3. 冻结、推送和构建

阶段 A 在构建机执行；源码/镜像推送需已有对应授权。生产源代码构建不在本计划内。若主分支或候选改变，复核新增差异，记录新 SHA 和发布 ID，不能继续使用旧候选的标签。产品版本保持 1.10.3。

```bash
RELEASE_SHA=2d62ffed0c0e30f96e15b0fce9b849e6cc3c84c2
PRODUCT_VERSION=1.10.3
RELEASE_SHA7=${RELEASE_SHA:0:7}
RELEASE_ID="v${PRODUCT_VERSION}-${RELEASE_SHA7}"
git fetch yybpc company/main
git merge-base --is-ancestor yybpc/company/main "$RELEASE_SHA"
git diff --check "yybpc/company/main..$RELEASE_SHA"
```

源码经正常受保护分支流程集成并推送，不 force-push；若流程生成新合并 SHA，就以最终集成 SHA 重新冻结和构建。不自动创建 Git tag/Hosted Release，不运行会自动升级语义版本的通用 release workflow。

依赖模式优先选择可信的当前生产后端 **linux/amd64 manifest digest**，作为 `CLAWITH_DEPS_IMAGE`。必须核实：生产到候选 pyproject 无变化；与镜像内文件校验和一致；镜像可信且架构正确；Python 与候选固定 base 版本一致；依赖安装、运行库及复制契约未改变。任一不满足则省略该 build-arg，显式选择 deps-build 并审查解析依赖。

下列变量在授权执行时从安全清单和当前部署填入：`BUILDER`、两个 `*_REPOSITORY`、`CURRENT_SHA7`、`BACKEND_DEPS_IMAGE`、已创建的空 `RELEASE_CONTEXT` 和 `EVIDENCE_DIR`。归档目录放在其他 Git checkout 之外，不把秘密或镜像凭据写入源码。

```bash
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

构建参数采用已审配置，必要时在上述命令加入已核实的镜像/包镜像参数。cache 输入输出必须不同；不存在旧 SHA cache 可省略读取。carrier 模式意外下载 Python 包则中止诊断。记录 index/amd64 manifest digest，校验 OCI 和 provenance；缓存不承担依赖可信性。AIO 仅在实际差异涉及其代码/base 或明确要求时重建。

## 4. 生产核实、备份与迁移

本轮未查询生产。授权执行时核实运行 SHA、四角色、Compose project、后端/前端/AIO digest、migration revision、健康/重启/活跃 turn/5xx/p95 基线和备份空间。以 `CURRENT_RELEASE_SHA..RELEASE_SHA` 确定真实范围，不能以相对主分支的 MCP 差异代替。

特别检查历史混合来源目录和零绑定共享目录：代码只防止继续产生问题，不会恢复已删除数据或判断混合目录归属。若发现受影响记录，按确切 server/tool/owner 和审计记录制定独立数据修复；不要依据重名、URL 或某个绑定直接批量改 source。

| 数据面 | 在线保护方案 |
| --- | --- |
| compose/env/身份 | 保存当前、候选、回滚文件及校验和，记录 digest/版本；配置秘密在安全位置存储，Git 只留脱敏清单 |
| PostgreSQL | 本次涉及种子及潜在索引迁移，做一次一致性 custom dump；覆盖 MCP server/tool/binding/override、引用和实际种子影响；跨表范围不确定则整库，由数据负责人确认 |
| Redis | MCP 已知差异不改其持久化/恢复契约；核实真实生产差异后可省略新增专用快照，并记录理由 |
| workspace/对象存储/CLI | 已知差异不改存储格式或二进制；确认实际差异后可省略本次新增快照，保留既有备份策略 |

旧服务保持运行，镜像就绪后、靠近窗口时在线备份，使用有界锁等待，校验 `pg_restore --list`、校验和及隔离恢复结果。不可在线一致备份时 NO-GO，另设计维护/蓝绿流程。

候选包含主分支索引 `agent_tool_lookup_index`，父 revision `repair_bootstrap_indexes`，在 `agent_tools(agent_id, tool_id)` 上创建非唯一 btree。生产若已应用，只检查 valid/ready；未应用则完成实际 schema 副本和并发读写演练后在线执行，5 秒锁超时、120 秒语句超时。实际旧版本若更早，补齐所有中间迁移的兼容检查。

```bash
docker compose -p "$COMPOSE_PROJECT" -f "$CANDIDATE_COMPOSE" \
  run --rm --no-deps --entrypoint alembic backend upgrade heads < /dev/null
```

确认唯一预期 head、索引状态、旧服务健康。失败立即停止切换，不自动 downgrade。验证候选正常 bootstrap 的种子效果，尤其两项 MCP builtin 描述/schema；不得在生产临时试跑未经演练的全库清理或数据纠正。

## 5. 切换窗口

当前/候选/回滚均是 backend、worker、connector、frontend 四角色的一致发布集合。实际服务名和显式 project 从部署核实后填入，不能从开发 compose 推断。三后端角色用相同 digest，前端匹配相同 SHA；PostgreSQL、Redis、对象存储和未变 AIO 不参与替换。

旧服务运行时预拉两个发布集合，校验 digest/架构/COMMIT，渲染两份 compose 和候选 nginx，验证 API_UPSTREAM、API/WS/MCP 路由。观察活跃工作，优先安静窗口，在批准时限内自然排空并记录未完成任务。

单副本 Compose 替换可能短暂中断，窗口开始前须明确接受；严格不中断要求必须改用已演练的蓝绿/滚动方案。完成 GO 后只执行一条应用替换命令：

```bash
docker compose -p "$COMPOSE_PROJECT" -f "$CANDIDATE_COMPOSE" \
  up -d --no-deps --no-build backend worker connector frontend
```

不预停服务、不 down、不 remove-orphans、不逐角色更新、不在窗口内构建/拉镜像。验收通过后原子替换规范 compose 文件，不因此二次重启。

## 6. 验收、观察与失败条件

| 验收项 | 通过标准 |
| --- | --- |
| 发布身份 | 四角色是预定 SHA/digest，预期 connector 数量，无重复实例或异常重启 |
| 基础链路 | 后端各角色健康、前端 200、health/版本正确；登录态 API、历史会话、普通 turn、WS 重连正常 |
| 全局禁用 | 工具清单/schema/prompt 隐藏；规范名、别名及旧场景中的后续未获准请求不发往提供者；不承诺撤回已发出的请求 |
| 私有安装 | 同名不同 key 为独立 ID/后缀，A/B 调用身份准确；无 Agent 范围刷新拒绝；刷新不变共享 |
| 共享目录 | 多 Agent 同定义，只能覆盖凭据/header/env 和二次启停；无绑定也不被清理；删除绑定有管理权限 |
| Project/提供者 | 检查与调用身份一致，历史复制凭据不用，Smithery 路由不串线，stdio 工作目录/注销正确 |
| 持久状态 | migration/种子正确；无长 idle transaction、目录丢失、重复外部输出或失控恢复 |

上述行为先在隔离环境验收，生产只进行专门授权的测试身份/会话 smoke；不得为健康检查随意新建用户、PAT、会话或发外部消息。生产不通过即按已准备的处置执行，不现场改产品代码。

切换后至少紧密观察 30 分钟，保留 24 小时跟踪。以下建议阈值须在 GO 时由负责人确定：

- 错误 SHA、鉴权/租户隔离回归、数据/目录丢失、重复外部输出：立即处置并按安全回滚方案回退。
- 超过演练确认的启动时限仍不健康：回退；建议上限 5 分钟，按实测调整。
- 5xx 较基线高 1 个百分点持续 5 分钟，或 p95 超过基线两倍持续 5 分钟：判定验收失败；明确证实无关外部故障才由负责人记录例外。
- 迁移失败且未切换：保持旧应用运行，停止推进。发布前若无法执行安全回滚，本身就是 NO-GO。

## 7. 回滚

**旧版可能重新引入全局禁用绕过和启动误删共享目录，不能把“旧镜像能启动”当作安全回滚。** 必须先演练旧镜像读取新配置、共享目录零绑定、权限及凭据行为；不满足时，提前准备保留安全修复的回退镜像。不存在已验证安全回退方案时不切换。

```bash
docker compose -p "$COMPOSE_PROJECT" -f "$ROLLBACK_COMPOSE" \
  up -d --no-deps --no-build backend worker connector frontend
```

默认保留兼容索引和切换后数据。旧镜像若不认识新 Alembic revision，回退 compose 只能启动已演练的非 bootstrap 角色，不能以 ALLOW_MIGRATION_FAILURE 掩盖问题；重新承担 bootstrap 的镜像必须识别当前迁移图。

根据真实旧 SHA 判定 runbook 中 scene_runtime_schema、rollback_im_recall、rollback_agent_self_settings、rollback_reasoning_controls、project_legacy_rollback 的适用性，适用者提前演练并填入回滚清单。旧版早于显式 continue 时，执行精确 anchor snapshot/apply 和 TURN_RECOVERY_ENABLED=false 流程，不扫描重放新任务。MCP 当前变更没有新增 builtin 参数，不能无条件清理其他领域的 schema。

数据库恢复会丢弃切换后写入，只能由数据负责人明确决定，对相关存储恢复到一致点；不自动 downgrade/restore，不删除私有安装或改审计历史来迁就旧代码。回退后重复健康、身份和 MCP 验收并继续观察。

## 8. 责任、授权和最终发布记录

执行前填入具名负责人和以下字段，本轮不编造生产信息：

```text
RELEASE_OWNER / OPERATOR / VERIFIER / DATA_OWNER / ROLLBACK_OWNER=待指定姓名
WINDOW_START / WINDOW_END / AUTHORIZED_INTERRUPTION_POLICY=待确定
CURRENT_RELEASE_SHA / CURRENT_BACKEND_DIGEST / CURRENT_FRONTEND_DIGEST=待授权核实
CURRENT_AIO_DIGEST / COMPOSE_PROJECT / CURRENT_DB_REVISION=待授权核实
RELEASE_SHA=2d62ffed0c0e30f96e15b0fce9b849e6cc3c84c2（若集成变更则重新冻结）
PRODUCT_VERSION=1.10.3
RELEASE_ID=v1.10.3-2d62ffe（跟随最终 SHA）
NEW_BACKEND_DIGEST / NEW_FRONTEND_DIGEST / BACKUP_ID=准备完成后填写
```

先完成可审阅的准备成果，再进入对应外部操作。源码/镜像/cache push、Git tag/Release、生产只读、备份、迁移、配置变更、切换、回滚及外部 smoke 消息按 runbook 各自确认授权；已有明确授权覆盖的操作不重复索要。本轮“出发布计划”不执行这些操作。

归档 source/main SHA、镜像与 AIO 身份、最终 Docker 命令及审计证据、生产差异、迁移前后、备份与省略理由、回滚演练、授权人/时间、窗口中断记录、30 分钟验收及 24 小时跟踪结论。交付完成后按授权清理本任务临时资源，保留用户和其他任务的工作树/数据。
