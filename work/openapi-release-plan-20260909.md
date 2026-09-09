# OpenAPI / H5 变更审计与发布计划

日期：2026-09-09。结论：**主分支已合并；本轮审查未发现新增阻断性代码问题，可进入发布准备。尚未达到生产切换 GO。**

本次授权范围是合并、审计和制定计划。本轮没有推送、打标签、制作/推送生产镜像或访问生产。
规范依据：[生产发布 runbook](../.agents/runbooks/production_release.md)。普通研发沿用用户指定的 3008，不为本轮审计另起应用或数据库环境。

## 1. 代码锚点与范围

| 项目 | 已核实结果 |
| --- | --- |
| 当前功能分支 | `feat/openapi-h5-integration` |
| 权威主分支 | `yybpc/company/main`，不是陈旧的本地 `company/main` |
| 本轮 fetch 的主分支 SHA | `7e38af39e541a5541972a40dcb1c88868f23c3c7` |
| 合并后的审计候选 | `d913183f7a766ea488b4edf4bb77ebb1220fc559` |
| 主分支合并 | 无冲突；新增 MCP 本地化错误断言修正及发布中断说明，未新增产品代码 |
| 产品差异范围 | `7e38af39..d913183f`，84 文件，+6485 / -151 行（含文档、生成 JSON、既有测试） |
| 分支关系 | 主分支独有 0 个提交；功能分支独有 15 个提交，含合并提交 |
| 产品版本 | backend/frontend 均为 `1.10.3`，本轮不升级语义版本 |
| 发布 SHA / ID | 产品候选为上述合并 SHA；正式制品 SHA 在发布准备冻结，格式 `v1.10.3-<SHA7>`；本计划记录提交不等于已经发布 |
| 3008 已部署应用 SHA | `3f0167d3d23bfa4a9c550f7f15a2e18f0cf0ddcc`；本轮合并仅文档/测试变化，应用代码与审计候选一致，无需为此重复重启 |
| 当前生产 SHA / digest / migration / Compose project | 尚未授权生产读取，不猜测，不以主分支或 3008 代替 |

发布准备冻结 SHA 后，将本计划中的候选、实际生产基线和最终制品信息写入独立发布记录。
若生产当前版本不是本轮主分支基线，必须补审 `CURRENT_RELEASE_SHA..RELEASE_SHA`；本审计不能替代未覆盖的生产差异。

## 2. 产品变更审计

| 能力 | 审计结果及边界 |
| --- | --- |
| 企业 OpenAPI 应用 | 企业管理员创建、编辑、查看凭据、轮换及撤销；查询和操作限制在当前企业。按已确认产品要求，应用密钥可再次查看，不新增加密方案 |
| OAuth 与用户委托 | 标准 Client Credentials、短期 Bearer、撤销和能力发现；使用现有身份、企业成员和员工权限，不自动开户或授权；平台管理员身份不能由企业应用委托 |
| 员工/场景发现 | 查询当前委托用户可见员工、可用已发布场景；`access.scene_key` 复用已有场景激活 |
| 临时登录 | 一次性 code，兑换时重新检查应用、用户及员工权限；配置的公开地址生成入口；AK 不进入 iframe 或 SDK |
| 首问和实例 | `interaction` 准备首问，兑换后才进入普通会话执行；相同 request ID 不重复执行；`instance_ref` 恢复对应会话 |
| H5 动态资料 | 可选 `host_context` 在每次真实提问前取宿主快照；保留普通输入、附件、STOP、确认、续答和历史；页面切换不自动提问、不重载会话 |
| 统一消息链路 | `external_context` 随消息保存，首轮、追加、历史、Gateway 使用共享投影；同 ID 改问题/附件/资料返回冲突 |
| Web/H5 展示 | 共用“参考资料”组件；Web 首次历史和翻页复用共享消息转换，不再丢字段；调用方名称和 JSON 原样保留，无额外系统说明 |
| SDK | 系统自动托管 `/sdk/h5-host-context.js` 和 `.d.ts`，公开静态模块支持跨域加载；窗口通信仍按精确 source/origin/request/attempt 匹配 |

未加入新的聊天运行时、上下文服务、业务表格 schema、专用授权 scope 或任意行数/字节数限制。
原 OpenAPI origin 白名单为空允许任意有效嵌入来源；浏览器窗口来源校验与 OAuth 调用域名无关。

### 已关闭问题与审计限度

- 此前中立审计发现的确定拒绝丢草稿、STOP 后未知发送重放、`/continue` 错入快照等待，均已修复并复核；对应浏览器行为验证通过。
- Web 历史字段遗漏已修复，3008 同一真实历史会话在 Web/H5 均能显示并展开参考资料。
- 此次为主代理对最终分支的变更审查；此前中立审计覆盖动态资料 API、消息链路、SDK 和上述修复。本轮没有另设中立代理重新审计整个分支，不能把既有审计说成完整生产基线审计。
- 后端单一迁移 head 为 `merge_openapi_mcp_index`；全部变更源码均不超过 800 行，最大为 `canonical_user_resolver.py` 的 789 行。
- 生成 schema 时出现既有的文件/项目下载接口 operation ID 重复警告，不属于 OpenAPI 新增 8 个业务路径；本轮不扩大修改无关路由。

## 3. 数据、依赖和回滚影响

相对主分支有 5 个迁移文件，其中 2 个仅合并迁移分支：

1. `openapi_applications_v1`：新建 `openapi_applications`、`openapi_user_bindings`、`openapi_credentials`。
2. `merge_openapi_bootstrap`：与 `repair_bootstrap_indexes` 汇合，无数据变更。
3. `openapi_secret_reveal`：新增可空 `client_secret`。
4. `openapi_interaction`：凭据新增可空 `launcher`；新建 `openapi_interactions`。
5. `merge_openapi_mcp_index`：与 `agent_tool_lookup_index` 汇合，无数据变更。

升级侧没有删除/重命名既有表、回填全库用户或重写历史消息；新模块执行后会通过原会话所有者写入 ChatSession、ChatMessage 及其既有执行状态。新增迁移的在线兼容性仍须以真实生产父版本演练确认，不能仅凭“加表/加列”视为通过。

相对本轮主分支：Python/Node 依赖清单和锁文件、Dockerfile、builtin seed、AIO、CLI、对象存储格式未变。发布时根据**真实生产 SHA**重新比较这些输入；满足 runbook 的五项校验才复用旧后端依赖镜像，否则明确走依赖重建模式。

回滚默认保留新增表、列和切换后的数据，不执行 downgrade 或数据库恢复。旧应用不认识新增 Alembic revision，必须验证回滚配置的应用角色不包含 `bootstrap`/`all`，不能用忽略迁移错误的开关绕过。
旧版本不会提供 OpenAPI/SDK/动态资料能力，也不能承诺继续以新快照语义恢复未完成的问题。回滚前尽量让这类工作结束；紧急中断时保存其精确 anchor/generation，单独核实结果，不自动重放已经执行过工具的首问。

## 4. 已有验证与发布前补齐项

| 证据 | 状态 |
| --- | --- |
| 本轮 merge、`git diff --check`、源码行数 | 通过，无冲突、无超限 |
| 本轮 Docker 文案扫描 | `scripts/check_user_visible_keyword.py` 通过 |
| 本轮精确 checkout Docker import | OpenAPI、管理、WebSocket、身份、首问及资料模块导入通过；8 个 OpenAPI 业务路径存在 |
| 本轮迁移图 / 3008 migration | 均为单一 `merge_openapi_mcp_index` |
| 本轮 3008 | 健康、OAuth 能力发现、SDK 200、nginx 配置检查通过；检查 token 随后撤销 |
| 已有前端构建 | Web/H5 展示变更 TypeScript + Vite 通过；删除冗余说明后的最终 Vite 构建通过 |
| 已有 API/数据库验证 | 企业管理→OAuth→access→exchange、开关、会话恢复、幂等冲突、JSON/附件、Gateway 和历史投影通过 |
| 已有聊天回归 | 原 WebSocket 初始化与 inbox 23 项通过；未新增测试框架或重复测试文件 |
| 已有真实浏览器 | A/B 快照、同 ID 重试、明确拒绝恢复、STOP、`/continue`、跨域 SDK、3008 Web/H5 资料展示通过 |
| 本次主分支 MCP 测试 | 修改断言已审查、编译通过；本轮未重新运行会写测试数据的数据库用例 |
| 生产父版本升级/旧镜像回滚演练 | 未做；生产基线尚未知，不能以研发数据库已在 head 替代 |
| 双镜像 amd64 / digest / 生产配置 | 未做；属于后续发布准备和授权生产步骤 |

不重开独立日常研发环境，不全量跑库。只有真实生产父版本的迁移/旧镜像兼容和恢复演练需要一次专用隔离目标，避免覆盖 3008；它是这次新增 schema 的发布前保护，不是每次代码改动的惯例。

本轮已执行的主要命令（`RC_DIR` 为当前分支路径，`CHECK_IMAGE` 为具备依赖的本地校验镜像）：

```bash
git fetch yybpc company/main
git merge --no-edit yybpc/company/main
git merge-base --is-ancestor yybpc/company/main HEAD
git diff --check yybpc/company/main...HEAD
docker run --rm --entrypoint python -v "$RC_DIR:/repo:ro" -w /repo \
  "$CHECK_IMAGE" scripts/check_user_visible_keyword.py
```

后端 import/head/route 检查在 `backend:/app:ro` 的一次性 Docker 进程中完成；3008 检查使用现有容器。具体行为验收只保留临时脚本和结果，不新增仓库测试文件。

## 5. 发布步骤

### A. 冻结与制作制品

1. 再 fetch 主分支；若移动则合并并重审实际新增差异。冻结干净的 `RELEASE_SHA`，记录负责人、版本和验收结果。
2. 获得发布准备授权后推送审定代码；不触发会自动升级语义版本的通用 Release workflow。
3. 核实依赖复用门禁：真实旧版本到候选的 manifest 一致、候选文件校验和等于依赖镜像内文件、依赖镜像是可信线上 amd64 digest、Python 与基座一致、构建命令和运行库/copy contract 未变。
4. 同一 SHA 构建 backend/frontend 的 `linux/amd64` 镜像，worker/connector 共用 backend。AIO 无变化不重建。

以下变量从授权库存和冻结源码解析，不填入生产私有地址或密钥。干净构建目录须写入准确 COMMIT，并避免父目录 Git 元信息污染构建 provenance。

```bash
RELEASE_SHA=<frozen-full-sha>
RELEASE_SHA7=${RELEASE_SHA:0:7}
RELEASE_ID="v1.10.3-$RELEASE_SHA7"
BACKEND_DEPS_IMAGE=<verified-current-backend-amd64-digest>

docker buildx build --builder "$RELEASE_BUILDER" --platform linux/amd64 \
  --build-arg "CLAWITH_DEPS_IMAGE=$BACKEND_DEPS_IMAGE" \
  --cache-from "type=registry,ref=$BACKEND_REPOSITORY:buildcache-$CURRENT_SHA7-amd64" \
  --cache-to "type=registry,ref=$BACKEND_REPOSITORY:buildcache-$RELEASE_SHA7-amd64,mode=max" \
  --label "org.opencontainers.image.revision=$RELEASE_SHA" \
  --label "org.opencontainers.image.version=$RELEASE_ID" \
  --tag "$BACKEND_REPOSITORY:$RELEASE_ID" --push "$CLEAN_CONTEXT/backend"
docker buildx build --builder "$RELEASE_BUILDER" --platform linux/amd64 \
  --cache-from "type=registry,ref=$FRONTEND_REPOSITORY:buildcache-$CURRENT_SHA7-amd64" \
  --cache-to "type=registry,ref=$FRONTEND_REPOSITORY:buildcache-$RELEASE_SHA7-amd64,mode=max" \
  --label "org.opencontainers.image.revision=$RELEASE_SHA" \
  --label "org.opencontainers.image.version=$RELEASE_ID" \
  --tag "$FRONTEND_REPOSITORY:$RELEASE_ID" --push "$CLEAN_CONTEXT/frontend"
docker buildx imagetools inspect "$BACKEND_REPOSITORY:$RELEASE_ID"
docker buildx imagetools inspect "$FRONTEND_REPOSITORY:$RELEASE_ID"
```

记录 index digest 和 amd64 manifest digest，并用镜像 COMMIT/OCI 标签复核 SHA。
缓存读取上一版、写入本次 SHA；不得覆盖正在消费的 cache tag。

### B. 生产准备与在线保护

取得对应生产授权后，读取实际 Compose project、四角色配置、SHA/digest、数据库 revision、PUBLIC_BASE_URL、运行中会话/触发器和错误基线。
公开地址必须是对接方浏览器实际可达的平台地址，不能留研发 localhost；这是生成入口和 SDK origin 的地址配置，不增加 OAuth 调用方域名限制。

生成 digest 固定的候选和旧版回滚 Compose，均保留原 PostgreSQL、Redis、对象存储和 AIO；预拉取并验证两套镜像及 nginx，旧应用全程继续运行。

| 资产 | 计划与理由 |
| --- | --- |
| PostgreSQL | 本次含新表/列以及用户、会话引用；切换前做一次在线一致性 custom-format 备份，范围由数据负责人确认。跨表恢复需要时采用全库；`pg_restore --list`、校验和及隔离恢复可读性验证必须通过 |
| 配置/制品 | 保存安全配置快照、渲染后的两套 Compose、旧新 digest、源 SHA 和回滚命令；不入 Git |
| Redis | 此增量未改存储格式或恢复协议；计划不额外快照，核对真实生产差异后确认遗漏理由 |
| Agent 工作区/对象存储/CLI | 此增量未改格式或纠正数据；不额外复制，保留原卷/存储。若实际生产差异涉及这些所有者，重新确定范围 |

### C. 迁移和旧镜像兼容

迁移演练使用真实生产父 revision/结构；旧版本在升级后仍应能登录、读取会话并完成原有聊天。数据库快照来源读取及恢复目标需要明确授权，不能在 3008 原库做回滚演练。

迁移使用候选镜像、覆盖 entrypoint，仅执行 Alembic。审计确认当前入口使用 asyncpg，不能依赖 libpq 的 PGOPTIONS；以下连接级限时设置已在 3008 的只读连接中验证生效，但未执行升级演练。限时数值须经生产父版本演练确认：

```bash
docker compose -p "$COMPOSE_PROJECT" -f "$CANDIDATE_COMPOSE" run -T --rm --no-deps \
  --entrypoint python backend - <<'PY'
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, event

@event.listens_for(Engine, 'connect')
def migration_limits(connection, _):
    cursor = connection.cursor()
    cursor.execute("SET SESSION lock_timeout = '5s'")
    cursor.execute("SET SESSION statement_timeout = '120s'")
    connection.commit()
    cursor.execute('SHOW lock_timeout')
    assert cursor.fetchone()[0] == '5s'
    cursor.execute('SHOW statement_timeout')
    assert cursor.fetchone()[0] == '2min'
    cursor.close()

config = Config('alembic.ini')
config.attributes['bootstrap_current_schema'] = True
command.upgrade(config, 'heads')
PY
```

锁等待或迁移失败即停止，不切换应用，不自动 downgrade。
验收单一 head `merge_openapi_mcp_index`、新表/列与旧应用健康。相对真实生产新增的其他索引或 seed 同样纳入范围。

### D. 窗口与一次切换

发布负责人、执行人、验证人、数据负责人、回滚负责人及窗口须在执行前明确。当前提议执行/验证由 Codex 在授权后承担，其余由用户指定，不擅自指定他人职责。

提议选择安静窗口，接受单副本替换造成的短时连接中断；尚未获得本次生产中断授权。若要求绝对连续服务，须改用已验证的滚动/蓝绿拓扑，不将一条 Compose 命令称为零停机。

切换前最后一次采样：会话 anchor/generation 与 live lease；触发器使用真实状态 `pending`/`processing`，并检查 lease 到期与计划时间。将最后采样后的新增任务也纳入核对。没有 durable origin/generation 的后台调用可能转为 failed，不承诺自动恢复，不盲目重放有外部副作用的调用。

所有准备完成后只运行一次四角色替换：

```bash
docker compose -p "$COMPOSE_PROJECT" -f "$CANDIDATE_COMPOSE" \
  up -d --no-deps --no-build backend worker connector frontend
```

不预先 stop/down，不分角色发布，不在窗口内 build/pull，不使用 `--remove-orphans`。

### E. 验收与观察

1. 四角色 digest/SHA 正确，健康、前端 200，无异常重启；数据库 head 正确。
2. 授权专用身份完成 OAuth、员工/场景发现、临时登录；权限仍按企业/用户隔离。
3. 开启 host_context 的 iframe 正常接收快照；普通 H5 保持原流程；Web/H5 同一消息可查看相同参考资料；SDK 跨域 import 正常。
4. 首问重复打开不重复执行；STOP、明确拒绝、续答和断线恢复符合已验证行为。生产真实提问只在明确授权的验收会话执行。
5. 观察至少 30 分钟，随后 24 小时跟踪：HTTP 5xx/延迟、重启、数据库锁、执行/恢复错误、触发器 failed、外部发送重复及不明结果。与切换前基线比较，不制造脱离当前系统的性能阈值。

错误 SHA/digest、启动不健康、鉴权/隔离退化、数据丢失、重复外部输出或不可恢复的任务错误触发停止放量和已授权回滚。

## 6. 回滚

保留旧镜像和预验证回滚 Compose，四个角色一次整体回退：

```bash
docker compose -p "$COMPOSE_PROJECT" -f "$ROLLBACK_COMPOSE" \
  up -d --no-deps --no-build backend worker connector frontend
```

- 新增 schema 和业务数据默认保留；旧应用角色不运行未知 revision 的 bootstrap。
- 相对本轮主分支没有新增 builtin schema，因此不额外发明 OpenAPI 回滚脚本。若真实生产基线更老，按其实际差异选用 runbook 已有 scene/continue/project 等 helper，并先演练。
- 旧版本缺失的新接口/SDK属于产品能力回退；暂停外部新入口，防止调用方继续拿新协议访问旧服务。
- 原会话数据仍保留；未完成的动态资料工作按精确标识核实，不宣称旧版本能无损续跑。
- 数据库恢复/downgrade 不是默认回滚动作；需要数据负责人明确接受丢弃切换后写入并演练后才执行。

## 7. 执行前尚缺的事实和授权

当前可交付的是审计候选与此计划。生产 GO 尚需：真实生产基线/四角色拓扑与 digest、最终不可变制品、在线迁移及旧镜像兼容证据、备份与回滚记录、人员/窗口/中断策略。

代码 push、镜像 push、生产读取、备份/迁移、真实验收提问、切换及回滚按 runbook 分别取得所需授权；本计划本身不执行这些操作，也不发起通用 Release workflow。

## 8. 追加：IM `/status` 自动场景显示

本次一起发布一个小修复：`/status` 调用既有 `resolve_session_scene`，与 `/scene status`、消息入库快照的场景解析保持一致。自动命中但没有手动 `scene_key` 的会话现在显示有效场景；未激活仍使用原文案。未新增配置、表、激活状态或专用服务，查询不写回会话，也不改变场景优先级和 `/scene off` 行为。

只修改通道命令的场景读取及既有用例；发布制品需包含本追加提交，不能仍使用上文最初的 `d913183f` 候选。仍按同 SHA 四角色整体发布；不因此新增迁移、seed、前端或 AIO 构建范围（正式发布的前后端制品仍须同 SHA）。

验证复用既有命令检查，不新增测试文件、数据库或应用环境。原 `/scene off` 用例仍预期不含 `scene_disabled`，与当前关闭自动场景的既有行为不符；对比修改前代码同样失败后，修正了这个既有断言。3008 没有当前有效的自动激活钉钉会话，因此本地未验证真实钉钉自动场景回包，更未向外部群发送消息。发布验收在授权群中对比 `/status` 与 `/scene status`，确认均显示同一有效场景。

追加修复的 Docker 结果：`tests/test_channel_commands.py` 共 27 项通过；纯内存桩执行，未连接或新建数据库。变更源码最大 784 行，`git diff --check` 通过。
