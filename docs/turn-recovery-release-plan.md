# 内部托管 Turn 统一恢复发布计划

状态：已获发布前准备授权，正在冻结提交与准备制品；生产现场与切换留待发布窗口核实。
执行依据为仓库发布规则与完整 production_release runbook；运行参数在授权准备阶段填写，不能直接执行未填值的模板。

## 1. 发布范围与版本

- 开发分支：`feat/unified-turn-recovery`。当前变更仍未提交。
- 本轮实现差异：基线 `9efe2ccee88f674413103c387588ec5029080ba0` 至当前工作区，包含新文件。
- 集成基线：`company/main`。本轮只检查本地引用，它已被开发基线包含；准备时重新 fetch 所选公司远端并核对，不将本地引用称为远端最新。
- 生产发布差异必须另按 `CURRENT_RELEASE_SHA..RELEASE_SHA` 审核，不能将开发分支历史中已发布的 OpenAPI/MCP 等能力再次算成本次升级。
- 最终 SHA、镜像 digest 待冻结与核实。版本使用 `v<PRODUCT_VERSION>-<RELEASE_SHA7>`，前后端同一 SHA；沿用仓库独立维护的产品版本，不跟随上游发布号、不自行升级 VERSION。

包含：内部 Task、Schedule、Trigger、heartbeat、oneshot 的持久化接收、共享执行与恢复；Web/H5、IM、MCP、Native Gateway、A2A、Subagent/项目与确认续跑的接收和收尾衔接。

排除：OpenClaw 外部执行、停用的 OKR 入口、独立的 OpenAPI 创建者字段需求。本轮没有新增内置工具定义、工作目录格式、AIO 或前端产品改动；最终生产差异仍需复核这些结论。

发布 backend、worker、connector、frontend 四个角色组成的同一镜像集合。前端按同一 SHA 重新构建；依赖输入相同时复用可信后端依赖镜像，AIO 保持原 digest。

## 2. 已有证据与发布前缺项

[可靠性验证记录](turn-recovery-reliability-validation.md) 包含实际证据及未覆盖范围：核心恢复 48 项、Trigger 34 项、后台 9 项、确认边界 6 项等分组通过；组间不累计总数。
两个真实 worker 经两次 SIGKILL、一次正常停机后完成同一任务；已完成工具未重跑、终态与业务日志不重复。独立交叉审阅无阻断。
新增源码 Ruff、diff 与所有交付源码 800 行门禁通过；3010 前端/API 健康检查通过。用户已指定使用 3010，3008 保留。

准备阶段已补充完成：入口/确认/身份 132 项、核心 49 项、后台与进程 15 项；回滚脚本 12 项；迁移超时、并发读写、无效索引重试及重复升级；已发布开发基线旧镜像在新 schema 上启动及 ORM 读写。生产当前镜像和生产 schema 副本仍未核实，不能混称为已通过。

发布前仍需完成：

1. 合入当前公司主分支中尚未包含的改动，审阅真实生产差异，冻结并提交干净候选；如 SHA 改变，按影响重跑验证。
2. 按最终差异及调用方选择后端 API、数据库、事件、租约、确认与恢复行为检查，完成相关模块编译/导入、前端生产构建及文案/i18n 检查。不默认运行无关全量测试；记录选择范围、失败、跳过和基线。
3. 在隔离 PostgreSQL 完成生产实际 migration parent 到候选 head 的升级、并发读写与恢复备份演练。已验证的开发库迁移不能替代实际生产 parent 核对。
4. 用生产当前旧镜像验证升级后 schema，并分别演练旧执行切到新版本、新后台执行切回旧版本；验证任务业务收尾、确认和 Gateway 回执。
5. 使用最终镜像在 3010 验证登录、正常对话、断线重连、后台任务、确认及恢复，核对权限与租户隔离。检查 nginx 实际渲染配置。

所有代码验证均在 Docker、隔离数据中完成。命令模板：

```bash
docker run --rm --entrypoint python --network "$TEST_NETWORK" \
  --env-file "$ISOLATED_TEST_ENV" -v "$RELEASE_DIR/backend:/app:ro" \
  -v "$RELEASE_DIR/frontend:/frontend:ro" -w /app \
  -e PYTHONPATH=/app:/app/tests -e PYTHONDONTWRITEBYTECODE=1 \
  "$BACKEND_TEST_IMAGE" -m pytest \
  tests/test_turn_recovery.py tests/test_turn_recovery_scanner.py \
  tests/test_turn_recovery_capacity.py tests/test_turn_recovery_continuation.py \
  tests/test_turn_recovery_delivery.py tests/test_unified_turn_process_recovery.py \
  -q -p no:cacheprovider
docker run --rm --entrypoint python -v "$RELEASE_DIR/backend:/app:ro" -w /app \
  -e PYTHONPYCACHEPREFIX=/tmp/pycache \
  "$BACKEND_TEST_IMAGE" -m compileall -q app
docker run --rm --entrypoint python -v "$RELEASE_DIR:/repo:ro" -w /repo \
  "$BACKEND_TEST_IMAGE" scripts/check_user_visible_keyword.py
```

上述核心检查之外，入口、后台业务、Trigger、确认、Redis 和身份边界按验证记录列出的现有测试分组执行。前端 Dockerfile 的 `npm run build` 会执行 prebuild、TypeScript 与 Vite；镜像构建结果作为对应证据。测试必须使用有开发依赖的测试镜像，不能假定生产镜像包含 pytest。

## 3. 制品准备

当前镜像、生产 SHA、Compose project、配置路径、构建机和 registry 均从授权现场核实，记录在 Git 外的发布记录。
后端依赖复用需证明：候选与生产 `pyproject.toml` 相同、其校验和与依赖镜像一致、Python 版本相同、运行库和依赖构建契约未变，且依赖镜像为可信 `linux/amd64` digest。任一不符就改为明确的依赖更新构建，不强行复用。

```bash
git -C "$RELEASE_DIR" rev-parse HEAD
git -C "$RELEASE_DIR" diff --check
git -C "$RELEASE_DIR" status --porcelain
printf '%s\n' "$RELEASE_SHA" > "$RELEASE_DIR/backend/COMMIT"
docker buildx build --builder "$BUILDER" --platform linux/amd64 \
  --build-arg "CLAWITH_DEPS_IMAGE=$BACKEND_DEPS_IMAGE" \
  --cache-from "type=registry,ref=$BACKEND_REPOSITORY:buildcache-$CURRENT_SHA7-amd64" \
  --cache-to "type=registry,ref=$BACKEND_REPOSITORY:buildcache-$RELEASE_SHA7-amd64,mode=max" \
  --label "org.opencontainers.image.revision=$RELEASE_SHA" \
  --label "org.opencontainers.image.version=$RELEASE_ID" \
  -t "$BACKEND_REPOSITORY:$RELEASE_ID" --push "$RELEASE_DIR/backend"
docker buildx build --builder "$BUILDER" --platform linux/amd64 \
  --cache-from "type=registry,ref=$FRONTEND_REPOSITORY:buildcache-$CURRENT_SHA7-amd64" \
  --cache-to "type=registry,ref=$FRONTEND_REPOSITORY:buildcache-$RELEASE_SHA7-amd64,mode=max" \
  --label "org.opencontainers.image.revision=$RELEASE_SHA" \
  --label "org.opencontainers.image.version=$RELEASE_ID" \
  -t "$FRONTEND_REPOSITORY:$RELEASE_ID" --push "$RELEASE_DIR/frontend"
docker buildx imagetools inspect "$BACKEND_REPOSITORY:$RELEASE_ID"
docker buildx imagetools inspect "$FRONTEND_REPOSITORY:$RELEASE_ID"
```

补入核实后的镜像源等既有构建参数，记录 manifest digest、amd64 子清单与 OCI revision。缓存按 SHA 只写新引用；缓存不作为依赖来源证明。
提前准备并拉取候选与回滚 digest，渲染两个四角色 Compose，核对 API_UPSTREAM、COMMIT、架构和 nginx。切换窗口不构建、不下载、不临时改配置。

## 4. 清理与在线保护

先按已有保留策略清理确认过期的备份，保留当前可用回滚链和未过期数据；不按文件名或时间猜测可删。记录清理前后空间，不清理业务卷。新备份空间预算按实际关联表大小核算。

| 内容 | 本次保护范围与理由 |
| --- | --- |
| 发布配置与镜像 | 每次保存当前/候选/回滚 Compose、环境配置安全副本、SHA、digest、校验和及命令；凭据不进 Git |
| PostgreSQL | 只热备关联表。初拟 `chat_sessions`、`chat_messages`、`tasks`、`task_logs`、`agent_schedules`、`agent_triggers`、`trigger_executions`、`agents`、`notifications`、`agent_activity_logs`、`audit_logs`、`approval_requests`、`subagent_runs`、`gateway_messages`、`gateway_send_receipts`、`alembic_version`；按最终差异和外键闭包收敛清单，不默认全库备份 |
| Redis | 会话租约/恢复协调涉及 Redis，复用现有在线持久化能力生成校验快照；不重启、不 FLUSH、不用阻塞 SAVE。先检查持久化方式、内存及延迟，不能保证在线安全则该项不执行并解决准备条件 |
| Agent 工作目录、对象存储、CLI 文件 | 当前无格式转换或数据修正，不新增全量归档；保留现有保护与线上文件。若最终差异发现实际变更，补充受影响范围 |

数据负责人在执行记录确认实际表清单及省略项。关联 PostgreSQL 表使用一次 custom-format pg_dump 的一致快照、短锁等待和外部受控输出位置，不锁表停写；备份靠近切换执行。用 `pg_restore --list`、校验和及隔离恢复验证可读。大表备份或 Redis 快照导致业务延迟上升则停止本次备份/准备并改进方式，不牺牲生产运行。

数据库备份用于定点救援，不代表可以把这些表直接覆盖回生产；跨表引用和切换后写入必须在恢复方案中处理。

## 5. 在线迁移与在途任务

唯一新 migration 为 `unfinished_turn_indexes`，父 revision 为 `merge_openapi_mcp_index`，在已有两张会话表上增加三个 partial indexes；无新表、无业务列、无批量数据改写。
旧服务运行期间从候选镜像执行：

```bash
docker compose -p "$COMPOSE_PROJECT" -f "$CANDIDATE_COMPOSE" \
  run --rm --no-deps -T --entrypoint python backend - <<'PY'
from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.engine import Engine

@event.listens_for(Engine, "do_connect")
def migration_limits(dialect, connection_record, args, params):
    params["server_settings"] = {
        **params.get("server_settings", {}),
        "lock_timeout": "2s",
        "statement_timeout": "10min",
    }

config = Config("alembic.ini")
config.attributes["bootstrap_current_schema"] = True
command.upgrade(config, "heads")
PY
```

该一次性命令通过 asyncpg 的连接参数设置超时，不依赖 PGOPTIONS；准备时必须在隔离库先核实 SHOW 值及竞争锁失败行为。以上 2 秒锁等待、10 分钟语句上限为初始预算，需先在生产规模副本核实。索引使用 CONCURRENTLY；失败就保持旧服务，核对无效索引并修复后重试，不自动切换。确认唯一预期 head、三个 indisvalid=true、旧版本读写正常。

首次升级特别区分：旧版本已持久化、且通过旧→新演练的在途 Turn 可直接切换自动恢复；旧版本尚未保存执行输入的后台任务不能因新代码上线而追溯恢复，应在旧服务在线时等待其自然完成。等待确认/项目暂停保持原语义，不强行执行。
这只是首次切换处理，不给新模块新增来源、次数或时长限制。后续所有内部后台接收都先持久化，再由同一机制接续。

现有单副本 Compose 允许短暂请求/连接中断；不宣称零停机。延续用户已接受的“可恢复 Turn 可以切换”原则，发布记录中列出实际在途状态及验收结果。选择低峰，旧服务不提前关入口、不 stop/down；条件不满足就推迟窗口。

## 6. 切换、验收与观察

所有门禁通过，发布负责人确认 GO 后仅执行一次：

```bash
docker compose -p "$COMPOSE_PROJECT" -f "$CANDIDATE_COMPOSE" \
  up -d --no-deps --no-build backend worker connector frontend
```

不拆分角色、不 remove-orphans，不重启 PostgreSQL、Redis、对象存储和 AIO。

立即核对四角色 SHA/digest、健康、单一 connector 实例、数据库 head、前端/API、登录和 WebSocket 重连。在已授权专用对话验证普通消息、后台任务与确认回执；不在生产杀进程做故障演练。
以切换前记录的 anchor 为准观察接管、工具结果复用、任务完成、webhook 消费和最终投递；STOP 不复活、确认不越过、无重复外部输出。

默认验收预算：5 分钟内四角色健康；可执行的未完成 anchor 在租约释放/到期加两轮扫描后仍无领取迹象即异常，不能仅按总执行时长判断失败。持续 5 分钟 5xx 比基线上升至少 1 个百分点或 p95 超过基线 2 倍进入故障处置；重复输出、权限串租户、数据损失、错误 SHA 或持续重启立即停止验收并触发回滚决策。
预算在窗口前按实际基线确认，指标异常需区分外部供应商与应用回归。至少观察 30 分钟，并保留 24 小时跟踪。

## 7. 回滚

准备阶段必须先验证“旧二进制 + 新 schema + 新版后台运行状态”，不能只验证旧程序能启动。实际审阅与隔离验证已证明旧开发基线不能正确接续新版后台业务收尾，当前旧镜像回滚尚不满足 GO 条件。
只在某次采样时看到后台任务为零仍不够：开放入口与逐容器替换之间可能再接收新任务。需证明候选最后写入结束后没有未完成后台执行、确认或投递尾部，否则不能将旧镜像当作安全回滚点；不得伪造终态、覆盖输入或无期限延长租约。
既有 `resume_turns_after_rollback` 脚本已修复假成功：只处理快照固定身份，使用共享发现和恢复，补齐原终态尾部；业务失败、待确认和未收尾返回非零。脚本不会启动快照外被提升的新轮次，正常应用默认仍自动推进。这个脚本修复不解决旧 worker 对新版后台状态的所有权冲突，不能代替上述 GO 条件。
回滚 Compose 使用旧镜像能支持的非 bootstrap 角色，保留新索引，避免旧 Alembic 图不识别新 revision；不得用忽略迁移错误掩盖问题。

```bash
docker compose -p "$COMPOSE_PROJECT" -f "$ROLLBACK_COMPOSE" \
  up -d --no-deps --no-build backend worker connector frontend
```

整套回退后重新验收健康、身份、会话与投递。默认保留切换后数据和兼容索引，不自动 downgrade、不恢复旧数据库覆盖新消息。
本轮无新增 builtin/schema 兼容助手；修改了既有回滚脚本的恢复结果及固定身份校验。若生产实际差异包含其他已审核兼容边界，按对应 runbook helper 处理，不能照搬上一轮 helper。

## 8. 责任与收尾

计划角色：用户任发布负责人/数据决策人；执行代理负责准备、操作和记录；独立审阅代理负责候选与回滚验证；回滚执行人沿用执行代理，外部动作以明确授权为准。具体窗口、现场操作者及各负责人姓名在发布记录确认。

用户已授权“发布前一切准备”，覆盖提交/推送/构建/registry 发布。生产检查、备份清理、热备、迁移、切换、外部验证消息和回滚按本轮发布的已有明确授权逐项记录；既有授权持续有效，但不把上一轮已完成发布的授权推定为本轮发布指令。无需重复申请已经明确覆盖本轮的动作。

发布记录与原始故障验证证据归档到 Git 外、可移除工作区之外的受控目录，包含前后 SHA/digest、备份清单、门禁结果、窗口时间和回滚命令。先归档临时目录证据，再进行清理。
验收及 30 分钟观察后，由执行代理按 worktree_cleanup 流程清理本轮已合并且无运行引用的工作区。当前 3010 仍绑定开发工作区时应明确保留，不为清理停止它；3008、主工作区、其他迭代、数据库卷和未提交工作不在清理范围。24 小时观察与回滚必须不依赖待删工作区。
