# 媒体工具与会话预览发布计划

状态：独立审计及发布计划复核通过，当前范围无未解决 P1/P2 发布阻塞；用户已授权完成发布前准备，生产切换等待用户指定时间。

## 1. 发布范围和当前基线

- 新增 `list_models`：名称、用途、输入类型、服务商筛选和分页，返回当前企业已启用模型及默认参数。
- `read_media` 增加可选模型参数；与 `generate_media` 统一可选模型、参数入口和简短说明。
- 历史媒体任务输入通过共享附件组件展示；修复精确文件的播放授权引用、第三方 URL 展示下载和图片预览层级。
- 不修改抽屉物理尺寸、不新增模型或修改企业默认模型、不增加调度系统、不进行历史消息重写。
- 这次没有新增针对所有真实供应商的付费回归，不把参数透传验证等同于所有模型参数都受支持，也不宣称能消除供应商模型的内容误判。

当前开发分支 `feat/aware-ux-restructure`，HEAD `a6904c9615b6e04aabe9b329c3ea65a378f6ed4f`；本轮审计范围为该 HEAD 上的明确未提交应用变更、新增测试及两份相关架构文档。

权威集成分支为远端 `yybpc` 的 `company/main`。准备阶段已执行 `git fetch yybpc company/main`，`yybpc/company/main` 与当前 HEAD 均为 `a6904c9615b6e04aabe9b329c3ea65a378f6ed4f`，无须额外合并。之前检查的本地 `company/main` 是旧本地分支，不能作为远端基线。切换前仍须核对实际生产 SHA 与候选 SHA 的完整差异，若出现未审计改动，重新确认范围并补齐受影响检查。

后端、前端 VERSION 当前均为 `1.10.3`。最终 RELEASE_SHA 尚未产生，发布标识为 `v1.10.3-<最终 SHA 前七位>`，不能用当前未含本轮代码的 HEAD 代替。

## 2. 审计与验收门槛

独立审计发现一个 P2：错误 `data:` 输入延后解码时，历史附件投影可能抛错并影响整个会话展示。已在投影边界捕获单条解析错误，保留文字及其他附件，不增加工具输入限制；现有 Docker 历史/查询测试补充该路径后 2 项通过。中立审计者 `media_change_audit` 独立 Docker 复验通过，确认原 P2 关闭，当前范围无未解决 P1/P2；同时完整复核本发布计划，无新增阻塞。任何后续确认的功能或权限阻塞必须先修复并复核后发布。

现有本地证据见 `media-viewer-model-controls-20260910.md`：

- Docker 受影响后端回归 99 项通过；最终工具说明调整后，模型查询和理解参数 5 项复验通过。
- 前端标准构建及其 prebuild 检查通过；新增附件测试的初始 fixture 错误已修正并重跑成功。
- 3008 小智实际工具调度可查询和筛选模型；数据库工具定义包含新增字段。
- 3008 历史媒体可见、音频播放、WebKit H.264 视频播放、图片预览层级和 Escape 行为通过。
- 21 个变更或新增手写源文件最大 723 行，满足 800 行门槛；diff 空白检查通过。
- Docker 用户可见文案扫描通过；最终补丁的历史读取和精确播放引用复验通过。

准备阶段在最终干净候选上下文上检查差异、导入、文案、行数，依据最终差异复用有效证据；代码变动或审计发现涉及的路径需要补验。前端最终镜像照常执行构建检查。

关键后端检查命令（Docker、隔离 PostgreSQL，连接值来自本地验证环境）：

```bash
docker run --rm --network "$TEST_NETWORK" --entrypoint python \
  -v "$RELEASE_CONTEXT/backend:/app:ro" \
  -v "$RELEASE_CONTEXT/frontend:/frontend:ro" -w /app \
  -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 \
  -e DATABASE_URL="$TEST_DATABASE_URL" -e REDIS_URL="$TEST_REDIS_URL" \
  -e AGENT_DATA_DIR=/tmp/agents "$TEST_IMAGE" -m pytest \
  tests/test_media_viewer_catalog.py tests/test_media_understanding_parameters.py \
  tests/test_media_generation_precedence.py tests/test_media_ai_provider.py \
  tests/test_media_ai_headers.py tests/test_media_playback.py \
  tests/test_chat_attachments.py tests/test_media_model_selection.py \
  -q -p no:cacheprovider
docker run --rm --entrypoint python -v "$RELEASE_CONTEXT:/repo:ro" \
  -w /repo "$TEST_IMAGE" scripts/check_user_visible_keyword.py
```

## 3. 必要数据保护与工具同步

本轮没有 Alembic、表结构、依赖清单、代理配置或工作区格式变更。不重建数据库、不全库 dump、不重写历史会话，不安排无意义的 Alembic downgrade/upgrade 演练。

| 对象 | 处理 |
| --- | --- |
| 发布配置 | 保存当前 Compose、配置安全副本、镜像 digest、revision 和校验值，密钥不进 Git |
| `tools`、`agent_tools` | 在线一致性 custom-format dump；包含工具 schema 和安装配置，安全保存 |
| 企业配置、模型数据 | 预检标准 seeder 的既有迁移是否已完成；若本次会写入，加入同一快照及隔离演练；确认为无变化时记录省略理由 |
| 大型消息、任务、工作区和对象存储 | 本轮不改格式或批量内容，省略全量备份；保留现有耐久输入和恢复状态 |
| Redis | 不改键结构或恢复语义，省略专项快照；发布前后核验执行租约和调度状态 |

批准上述范围后，旧应用保持服务，镜像准备完再接近切换时做在线备份；设置短锁等待，检查 `pg_restore --list` 和校验值，在隔离库验证可读性。数据备份是审计保护，不用于覆盖发布后的新写入。

使用现有 `seed_builtin_tools()`，不增加第二套工具维护流程。隔离演练并记录实际更新集合和幂等性：新增 `list_models`，更新两个媒体工具的 schema/说明，保留显式关闭和项目/场景的独立配置。普通 Agent 安装新默认工具，项目 Agent 不自动扩权；`generate_media` 保持原有安装状态。

标准 seeder 还有历史清理与迁移逻辑，必须依据实际数据库确认不会产生未纳入备份范围的额外变更。新增工具定义不提前暴露给旧执行实例，随候选应用启动同步。启动健康不等于 seed 成功，验收须查数据库定义、实际工具清单及一次 dispatch；失败即停止验收并向前修复。

## 4. 不可变发布物

先整理只含已审计变更的提交，保留当前目录其他用户 WIP。经授权后提交、推送权威分支，冻结最终 SHA；导出到仓库外干净构建上下文，避免父仓库影响自动 provenance。同一 SHA 构建前后端 linux/amd64 镜像，AIO 无变更不重建。

优先使用已验证当前生产后端 amd64 digest 复用依赖：需确认候选 pyproject 与当前发布及载体内文件相等、Python 版本一致、运行库和依赖构建逻辑未变化。未满足时明确改为依赖重建模式并审计依赖，不能强行复用。

```bash
docker buildx build --builder "$BUILDER" --platform linux/amd64 \
  --cache-from "type=registry,ref=$BACKEND_REPO:buildcache-$CURRENT_SHA7-amd64" \
  --cache-to "type=registry,ref=$BACKEND_REPO:buildcache-$RELEASE_SHA7-amd64,mode=max" \
  --build-arg "CLAWITH_DEPS_IMAGE=$BACKEND_DEPS_IMAGE" \
  --label "org.opencontainers.image.revision=$RELEASE_SHA" \
  --label "org.opencontainers.image.version=$RELEASE_ID" \
  --tag "$BACKEND_REPO:$RELEASE_ID" --push "$RELEASE_CONTEXT/backend"
docker buildx build --builder "$BUILDER" --platform linux/amd64 \
  --cache-from "type=registry,ref=$FRONTEND_REPO:buildcache-$CURRENT_SHA7-amd64" \
  --cache-to "type=registry,ref=$FRONTEND_REPO:buildcache-$RELEASE_SHA7-amd64,mode=max" \
  --label "org.opencontainers.image.revision=$RELEASE_SHA" \
  --label "org.opencontainers.image.version=$RELEASE_ID" \
  --tag "$FRONTEND_REPO:$RELEASE_ID" --push "$RELEASE_CONTEXT/frontend"
docker buildx imagetools inspect "$BACKEND_REPO:$RELEASE_ID"
docker buildx imagetools inspect "$FRONTEND_REPO:$RELEASE_ID"
```

构建前将完整 RELEASE_SHA 写入候选 backend/COMMIT。保存 manifest/index digest，核验 amd64、OCI revision、应用 COMMIT、前端版本。镜像仓库、当前 digest、builder、稳定构建参数和生产 Compose project 都在授权准备时从现行配置解析；本次计划不猜填生产值。

## 5. 切换步骤

1. 经授权核实现行 backend/worker/connector/frontend 拓扑、SHA、restart、健康、调度与错误基线；核定窗口及短暂中断政策。
2. 旧应用运行期间，预拉候选 digest，渲染并校验固定 digest 的 Compose、nginx upstream。生产窗口内不构建、不拉取、不临时编辑 Compose。
3. 观察普通会话、媒体子任务、trigger `pending/processing`、租约和投递尾部。尽量等执行排空，切换前最后一次记录活跃身份和输入锚点。
4. 媒体理解在中断后可能按现有机制明确失败而非重发；需要无损完成的理解任务先等待结束。已提交生成按原 provider task 恢复，不能重复提交。单副本不能承诺零中断；严格连续性要求未满足时不切换。
5. 完成备份、检查和 GO 后一次替换四个应用角色，数据库、Redis、对象存储及 AIO 保持运行：

```bash
docker compose -p "$COMPOSE_PROJECT" -f "$CANDIDATE_COMPOSE" \
  up -d --no-deps --no-build backend worker connector frontend
```

6. 四角色验收通过后，原子替换规范 Compose 文件，不因此再重启一次。

## 6. 验收与向前修复

发布后必验：四角色 SHA/digest 正确、健康、前端/API 200、登录/历史/WebSocket 重连、普通对话、工具数据库定义及实际模型过滤。通过既有授权验收身份核验媒体省略参数和指定参数、批量输入、连续追问、历史图像/音频/视频展示；真实付费供应商调用须处于明确授权范围。

连续近距离观察至少 30 分钟，保留 24 小时监控。对比发布前基线，关注 HTTP 5xx、延迟、重启、数据库锁/空闲事务、工具失败、媒体队列、trigger processing 超租约、投递 failed/unknown 和重复输出。30 分钟健康验收中出现新增容器重启、持续探活失败或新增任务超出正常租约且不能恢复，立即进入故障处理；权限串租、数据丢失、重复外部输出、错误 SHA/digest 为立即停止验收条件。短暂切换 5xx 单独记录，不能冒充零中断。

遵循用户既定 `REMEDIATION_POLICY=forward-only`：不退旧镜像、不降级结构、不恢复旧快照覆盖新数据、不在生产容器内改代码。以已发布 SHA 出最小修复，跑受影响 Docker 检查，构建同 SHA 的前后端新镜像，预拉验证后使用同一四角色替换命令。沿用现有恢复机制核对活跃任务，不盲目重放已产生外部效果的调用。

## 7. 负责人、权限和收尾

发布/数据范围最终决策人为用户；执行与记录由主代理承担；独立审计由 `media_change_audit` 子代理承担。热修复由主代理执行、用户按发布权限决策。具体窗口、操作身份、最终 SHA 和生产参数在执行记录中确认。

用户已授权“发布前的一切准备”：本轮候选提交、权威分支推送、前后端不可变镜像构建推送及 digest 记录。生产切换等待用户给定时间；生产检查/备份、真实烟测、tag/Release 按仓库规则及已有明确授权分别判断。

本轮未创建新 worktree。当前工作目录、3008、隔离测试数据和无关 worktree 保留；后续创建的候选 worktree 记入发布记录。验收与 30 分钟观察后按标准流程清理本轮已合并、无本地数据和运行依赖的临时 checkout；保留原因明确的未完成工作。发布记录、备份、镜像证据及 24 小时观察脚本先存入清理范围外的批准位置，不包含凭据或个人路径。
