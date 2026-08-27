# AI 原生项目管理发布前中立审计与发布计划

日期：2026-08-26  
状态：**GO for production preparation（研发与候选验证已闭环；尚未生产发布）**
权威集成分支：`yybpc/company/main`

## 一、审计口径

本审计以运行中的生产基线、`yybpc/company/main`、当前候选分支真实差异、Docker 验证记录和真实项目跨域验收为依据。产品迭代文档中的“已完成”不自动等同于“可发布”；发布结论单独判断。

生产环境仅做了只读核对，没有修改配置、数据或容器：

- 当前生产源码：`bd5cb26113998d4511251f8083f748033e0d2a30`
- 当前版本：`v1.10.3-bd5cb26`
- 后端与前端使用同一源码 SHA，均为不可变 digest 引用。
- 后端健康、重启次数为 0；前端运行中、重启次数为 0。
- 当前 Alembic：`webhook_event_sequence (head)`。
- 数据盘剩余约 70 GB；AgentData 约 37 GB，PostgreSQL 数据目录约 11 GB，数据库逻辑大小约 10 GB。

当前候选：

- 分支：`feature/ai-native-project-management`
- 完整差异审计应用基线：`e04de8dd7b9a1916a5567ac30b0cc95373d2bc0f`。
- 最新主线：`yybpc/company/main@d963d85cbc4852feaa070a13915023f231ab8b4e`，由合并提交 `e04de8dd` 纳入候选。
- 最终收口在该基线上增加可逆 legacy 数据库边界及相应行为测试；Plaza 全局退场是用户已授权的独立产品决策，审计纠偏提交 `d8d6263d` 已撤销错误恢复并保持统一退场。
- `b0c2e14b` 完成里程碑自动关联；`b4c84ec7` 与 `c94f8d52` 完成项目原子并发门禁和普通 Subagent 隔离。
- backend/frontend 版本均仍为 `1.10.3`。
- `docker/aio-sandbox/**` 与 AIO Compose 没有变化，本次不构建、不切换 AIO。

当前判断为 `GO for production preparation`：研发项、主线合并、严格 RC3、旧服务兼容和项目故障隔离均已闭环，可以进入制品构建、备份演练和发布窗口准备。该结论不表示已经生产发布，也不自动授权推送镜像、停写、备份、迁移、切换或生产渠道验收。

## 二、中立审计结论

### A-01 · 已关闭 · 项目创建数据库/文件一致性

项目创建已调整为先完成可前置的共享范围、成员与来源校验，再初始化托管仓库；初始化后的服务失败、成员复制链失败或最终数据库提交失败统一回滚数据库并补偿删除项目根目录，不再只清理单个 Agent 目录。Docker/PostgreSQL 可观察测试已经覆盖“校验失败时不创建存储”“服务链失败后数据库与项目根目录为零”“最终提交失败后数据库与项目根目录为零”。

候选行为矩阵已确认 Agent 资产与 Git 操作记录不存在孤儿；生产准备阶段只需把命令、镜像和摘要固化到发布证据表。

### A-02 · 已关闭 · 兼容降级后的旧服务回滚

回滚标准已按最新产品决定调整：不要求回滚时抹除每张新增表、每个新增列或所有项目历史，只要求执行经过验证的兼容降级后，旧服务能够安全运行且原有能力不受影响。隔离演练已完成完整兼容降级序列，并用精确旧镜像 `v1.10.3-bd5cb26` 启动：

- 候选 helper 创建固定的专用非 owner 旧服务角色，对全部 Agent 外键表、`project_id` 表和 Plaza 多态作者表建立可逆 RLS 边界；不更新或删除业务行。
- 精确旧镜像 `sha256:a8f783c27b05e79de40f88b9107ff98b896c6277051c80adeec396f09e33ddd6` 的 API/worker 分角色运行，健康检查 HTTP 200、版本 `1.10.3`、重启次数为 0。
- 标准 Agent 六类接口保持 HTTP 200；已知项目 Agent 的详情、会话、任务、计划、触发器、工具、权限、活动、审批和网关十类接口全部 404；六类全局列表没有项目标记。
- 旧 worker 不领取项目 Subagent Run、Schedule 或 Trigger；标准队列仍可领取。
- `restore` 可重复执行，helper policy、RLS enable、专用角色和分类函数全部清除；项目 Agent、成员、会话、Run、事件和消息快照恢复前后 checksum 均为 `fa0afa5b04f4838c480a24d54d19dce9`，候选恢复后七个项目 API 全部 HTTP 200。

普通应用回滚因此使用“兼容降级 + 旧 backend/frontend digest”，无需默认恢复整套数据。只有数据损坏、兼容降级失败或显式要求回到同一恢复点时，才启用 PostgreSQL、AgentData、Redis/对象存储的一致恢复路径。

### A-03 · 已关闭 · 候选验证门禁

验证证据包括：以 Docker/PostgreSQL 分区执行的完整后端唯一用例 `2741 passed, 28 skipped`，没有产品断言失败；项目协作 `515 passed`；市场与项目设置 `41 passed`；最新主线合并影响套件 `78 passed`；legacy RLS 持久行为测试 `1 passed`；前端完整 prebuild、TypeScript、Vite production build；授权 Plaza 退场的 3011 API/数据库行为；3011 健康与严格 RC3 跨域验收。源码文本或正则形状测试不计入上述通过数。

### A-04 · 已关闭 · Plaza 授权范围纠偏

Plaza 全局退场是用户已授权的独立产品决策，不是项目能力造成的意外回归。审计一度错误恢复该能力，现已由 `d8d6263d` 撤销：旧前端路由继续跳转发现页，Onboarding 继续进入 Agent 会话，后端不注册 Plaza router，相关工具在 seed、LLM 目录和 runtime 中统一关闭，通知、活动与 Heartbeat 不再暴露 Plaza。3011 `/api/plaza/posts` 返回 404，三项工具均为 `enabled=false / is_default=false`，修正后的前端 production build 通过且不生成 Plaza 页面 chunk。

### A-05 · P2 · 发布面过大且没有项目功能开关

候选同时修改项目领域、会话、A2A、触发器、工具播种、Agent 可见性、文件工作区、后台运行、前端公共组件、部署文件和 CI。项目入口没有租户级发布开关，因此上线即面向全部生产用户，不能做租户灰度。

处理原则：本轮不为此引入复杂灰度系统。阻断项修复后采用一次性维护窗口、关闭入口完成生产验收、通过后再开放。后续迭代再决定是否增加统一功能开关。

### A-06 · P2 · 核心文件规模形成后续维护风险

- `ProjectWorkspacePage.tsx`：8,949 行。
- `projectWorkspace.css`：4,942 行。
- `backend/app/api/projects.py`：4,939 行。

当前不建议在发布前做大规模重构，以免扩大回归面；但发布后应冻结继续堆叠，按“页面区块、领域服务、共享组件”拆分，并保持 API 与用户行为不变。

### A-07 · P1 运行前提 · 完整备份容量必须先确认

生产 AgentData 约 37 GB、数据库约 10 GB，当前数据盘剩余约 70 GB。本次变更同时写数据库与项目仓库，不能省略 AgentData 或数据库备份。正式窗口前必须完成压缩后体积测量或准备独立快照/外部备份目标，禁止在 cutover 时临时尝试并把主数据盘写满。

A-07 是生产发布执行前提，不是未完成的研发任务。它只在获得发布授权并进入生产准备后执行；不影响本轮开发完成结论，但未满足时禁止停写与切换。

### A-08 · 已关闭 · 项目能力故障不得影响原稳定能力

项目运行判断已收敛到明确的项目 Agent 边界：标准 Agent 与普通 Subagent 使用独立非项目路径，不加载项目状态或项目容量。项目表、项目状态查询或项目服务故障时，标准 Agent 的普通会话、计划任务、手动任务、触发器领取与触发调用继续运行；只有对应项目入口局部失败。`max_parallel_runs` 对所有 ProjectRun 类型使用统一原子计数，饱和任务保持 queued，容量释放后才进入 running，不会占用或阻断普通 Subagent。Docker/PostgreSQL 故障隔离专项 `10 passed`、相关后台/手动/调度回归 `36 passed`，最新合并影响套件 `78 passed`。

## 三、最终候选冻结

1. A-01 创建补偿、A-02 legacy RLS、A-03 候选验证、A-04 Plaza 授权范围纠偏和 A-08 故障隔离已关闭。
2. 最新主线 `d963d85c` 已由 `e04de8dd` 合入；Plaza 授权纠偏后的应用代码候选冻结为 `d8d6263d`。
3. 文档收口提交后记录最终不可变 `RELEASE_SHA`；应用制品的代码门禁 SHA 为 `d8d6263d`，其后的提交只允许文档变化。
4. 准确记录“完整后端分区 2741 passed/28 skipped + 合并影响 78 passed + legacy RLS 1 passed + Plaza 退场 API/数据库行为”，不把相互重叠的分区简单相加为唯一测试数。
5. backend/frontend 的 `VERSION` 必须继续一致为 `1.10.3`。
6. 发布标识按 `v1.10.3-<RELEASE_SHA 前 7 位>` 生成，不提升私有语义版本。
7. Git tag 必须直接指向完整 `RELEASE_SHA`；合并、tag、push 和发布分别等待用户明确授权。

## 四、候选证据与生产准备检查

所有命令只在 Docker 中运行，使用本地构建缓存，不在宿主机创建虚拟环境，不在生产执行开发测试。

### 4.1 源码与审计

- `git diff --check` 通过，工作树干净。
- `yybpc/company/main...RELEASE_SHA` 中立复核无 P0/P1 finding。
- 确认 backend/frontend 同一 SHA，AIO 差异为空。
- 用户可见文案扫描通过；新增工具描述只包含中立用途、真实依赖与可理解边界。

### 4.2 数据库与后端

- 从生产数据库只读结构克隆建立隔离 PostgreSQL。
- 从生产当前 `webhook_event_sequence` 升级到候选唯一 head `repair_tenant_boundary_triggers`。
- 验证新增项目表、Agent 项目字段、会话/运行关联、执行用户字段、活动枚举和索引。
- 记录迁移时间和锁等待；当前受影响核心表约为 Agents 125 行、ChatSession 23,345 行、SubagentRun 63 行，预计迁移较短，但以演练实测为准。
- 完整 backend 唯一用例采用适配其数据库前提的 Docker/PostgreSQL 分区执行：`2741 passed, 28 skipped`，无产品断言失败。主线合并影响套件 `78 passed`、legacy RLS 持久行为测试 `1 passed` 和 Plaza 退场 API/数据库行为作为收口证据单列，不重复累加。
- 验证 19 个用户项目工具写入数据库、默认关闭、没有给标准数字员工自动启用；运行时工具集合与数据库一致。
- A-01 创建校验、服务链失败、成员/资产复制失败和最终提交失败均通过可观察行为验证；数据库、项目根目录、AgentDir 和 Git 操作记录无孤儿。
- A-02 使用应用候选中的 helper 和精确旧镜像复跑：专用旧服务角色、48 张表边界、API/worker 分角色、深链 404、项目队列不领取、幂等 restore 和候选恢复全部通过；`d8d6263d` 未修改该 helper 或对应测试。
- 项目故障隔离矩阵通过：项目查询/服务不可用不阻断标准 Agent 会话、计划任务、手动任务、触发器领取和触发执行。
- 标准 Agent 项目工具矩阵通过：组三态、Web/映射 IM Human、合法非 Human 拒绝、owner/editor/viewer/removed、确认恢复 ACL 重检、分页、暂停、跨项目 ID 和独立证据锚点。
- 项目并发矩阵通过：所有 ProjectRun 类型统一进入 `max_parallel_runs` 原子门禁，饱和运行保持 queued；普通 Subagent 不参与项目计数并保持独立可执行。

### 4.3 前端与真实链路

- 前端完整 prebuild、TypeScript 和 Vite production build 通过。
- 前端候选证据：Plaza 授权纠偏后的完整 prebuild、TypeScript 和 Vite production build 通过，转换 10,297 个模块，只有既有大分块提示；附件、Web 恢复、H5 时间线、项目路由、Git diff 和文件工作区等可执行行为命令通过；最终构建不再生成 Plaza 页面 chunk。生产制品仍按第五节从固定 SHA 构建。
- 使用最终 SHA 重建本地 Docker 栈，后端与前端健康。
- 中文、英文、390/768/1280/1920 四档关键页面通过。
- 负责人、编辑者、查看者权限矩阵通过。
- 真实项目链路重新执行一次：创建、共享、指定运行身份、项目 Agent、工具开关、Skill、规划、启动、A2A、任务完成、文件与 Git。
- 来源标准 Agent 的工具、记忆、工作区、会话和触发器保持不变。
- 3011 backend 在最新合并后完成重启，健康检查 HTTP 200，restart count 为 0；Alembic 为 `repair_tenant_boundary_triggers (head)`。
- 严格 RC3 项目 `fb454fa3-ccb1-4f71-b587-a29ed200c3b6` 验收通过：4/4 工作项、32 Runs、5 个角色、200 个事件、2 个里程碑，失败、活跃、未恢复和未关联均为 0；Git HEAD `5f46a1483080e32236343a302e560a533352174f`，证据 checksum `1cfbbd26974d3b6a819ce7f317e312a26e7180796e6143ca33ee70411e40a090`。
- 完整键盘与读屏复核按用户要求不作为本轮门禁。

门禁结果必须形成一张证据表，记录命令、容器镜像、数据库来源、通过数、跳过数、已知基线和验证人。

## 五、构建与制品

1. 从 `RELEASE_SHA` 创建干净的 backend/frontend 构建上下文，不使用带未提交文件的工作目录。
2. backend 与 frontend 一起构建，统一使用 `v1.10.3-<sha7>`。
3. 使用本地 BuildKit 缓存，构建平台固定为 `linux/amd64`；禁止把本地 arm64 Compose 镜像推到生产仓库。
4. 两个镜像写入完整 OCI revision/version；推送后记录 registry index digest，并验证 manifest 包含 `linux/amd64`。
5. 生产候选 Compose 使用 `image:tag@sha256:digest`，不依赖可变 tag。
6. AIO 无代码与基础镜像变化，保留当前已验证的 AIO tag/digest，不构建、不推送、不切换。

制品记录：

| 项目 | 值 |
|---|---|
| RELEASE_SHA | `RELEASE_SHA`（最终收口提交后回填完整 SHA） |
| APPLICATION_SHA | `d8d6263d8fc96832b15fb9e27f196ee025425a00` |
| RELEASE_ID | `v1.10.3-<sha7>` |
| backend digest | 构建后回填 |
| frontend digest | 构建后回填 |
| AIO digest | 保持生产当前值 |
| Git annotated tag | 授权后创建并回填 |

## 六、生产准备

发布前只准备，不切流、不停服务：

1. 记录当前 backend/frontend/AIO 不可变 digest、Compose、迁移 head、健康、重启次数和错误基线。
2. 生成候选 Compose，检查只有 backend/frontend digest 和本轮必要配置变化。
3. 在隔离容器中用生产环境变量渲染 frontend nginx 模板，验证 `/api`、`/ws`、`/mcp`、上传和对象存储代理；`API_UPSTREAM` 必须非空。
4. 生产只拉取候选 digest，不在生产构建源码。
5. 检查数据盘和备份目标；完成 37 GB AgentData 的备份方法与耗时演练。
6. 创建带时间戳的备份目录占位，但权威备份只能在停写后生成。
7. 准备回滚脚本，写入当前生产两个应用 digest；不删除卷、不执行 `docker compose down`。
8. 记录负责人：发布负责人/数据负责人/回滚负责人由刘喜确认；操作人与验证人由授权发布会话确定。
9. 确认维护窗口、预计停写时长、通知渠道和用户通知文本。

## 七、停写、备份与切换

### 7.1 停写

1. 关闭新入口，查询活跃 turn，等待已有 Web、IM、A2A、trigger 和 task 执行排空。
2. 确认 AIO 没有仍在写 AgentData 的后台作业；必要时仅停止 AIO，切换后仍用原镜像恢复。
3. 停止 frontend 和全部应用 writer，包括 backend、worker、connector、trigger 和 schedule 角色。
4. 确认旧 backend 进程数为 0。禁止新旧 backend 重叠。
5. PostgreSQL、Redis 和存储保持运行以完成一致备份。

### 7.2 权威备份

停写后立即生成：

- PostgreSQL custom-format dump；
- Redis RDB；
- AgentData 完整归档或一致存储快照；
- 对象存储快照/清单（若生产对象存储为权威来源）；
- 生效 Compose、候选渲染 Compose、必要的环境配置快照；
- 旧/新 backend/frontend tag 与 digest、AIO digest；
- 迁移前 revision、备份时间、遗漏项和恢复命令；
- SHA-256 清单和可执行回滚脚本。

必须用 `pg_restore --list` 验证数据库备份，检查所有校验和及归档可读性。数据库、Redis、AgentData 或对象存储的任何省略都需要本次发布的单独授权。

### 7.3 切换顺序

1. 启用同时固定两个候选 digest 的 Compose。
2. 只启动一个具备 bootstrap 能力的候选 backend。
3. 等待 Alembic 到 `repair_tenant_boundary_triggers`、工具播种完成、Uvicorn ready 和健康检查通过。
4. 核对 backend 只有一个有效实例，没有旧进程。
5. 启动 frontend，再按候选拓扑启动其他 writer；AIO 保持原版本。
6. 检查每个容器实际 image digest 与计划一致。
7. 任一步迁移失败、启动超时或 digest 不一致，立即停止并进入回滚，不继续启动 frontend。

## 八、生产验收

入口仍受控时先验证：

### 平台稳定能力

- 健康与版本 200，版本端点返回 `RELEASE_SHA`。
- 登录、组织目录、标准数字员工列表、标准 Agent 工具页正常。
- 普通 Web 会话、历史、WebSocket 断线恢复正常。
- 专用测试会话中的一条 IM 发送、回执/召回、webhook 和 MCP 刷新链路正常；真实渠道测试需另行授权。
- 标准数字员工数量和原有工具启用集合与发布前一致。

### 项目完整链路

- 创建一个专用验收项目，选择三名标准数字员工来源。
- 确认生成三名项目独立数字员工；全局 Agent 目录不可见。
- 共享给查看者和编辑者，手动选择运行身份；刷新后保持。
- 每名项目 Agent 默认只开启统一 14 项基础工具；手动开关一个普通平台工具后仅影响当前项目 Agent。
- 复制一个 Skill，确认成为项目资产；来源 Skill 和工作区不变。
- 规划群聊、确认启动、创建唯一任务、A2A 委派、完成、负责人回执全部成功。
- 生成项目报告并进入项目 Git；来源数字员工没有新增会话、触发器或项目文件。
- owner/editor/viewer 及跨租户 404 隔离通过。
- 模板发布默认不携带 Skill；显式选择后，新项目得到独立 Skill 文件。

全部通过后开放入口。任何身份越界、来源 Agent 污染、重复执行、Git/数据库不一致均判定 NO-GO。

## 九、观察与回滚

### 观察

- 密切观察至少 30 分钟，继续保留 24 小时常规观察。
- 对比发布前基线：HTTP 5xx/延迟、容器重启、失败/未知/超时回执、重复消息、数据库/Redis/AgentData 错误、turn 恢复、项目运行排队和 Git 操作失败。

立即回滚条件：

- 迁移失败或 backend 无法健康启动；
- 登录、租户隔离、标准 Agent 或原 IM/MCP 能力回归；
- 项目 Agent 出现在全局目录；
- 来源记忆、工具、会话、触发器或文件被项目修改；
- 重复任务/消息、数据损坏或无法执行既定回滚。

### 回滚决策树

兼容降级固定步骤：停止全部候选 writer；用候选镜像和 owner DSN 执行 `PROJECT_LEGACY_ROLLBACK_PASSWORD=<secret-manager value> python -m app.scripts.project_legacy_rollback apply` 和 `status`；旧 API 与 worker 分别使用专用 `clawith_project_legacy` DSN 及 `PROCESS_ROLE=api` / `PROCESS_ROLE=worker` 启动。禁止旧镜像使用 owner DSN 或 `PROCESS_ROLE=all`。再次升级前停止旧进程，用候选镜像和 owner DSN 执行 `restore` 与 `status`，确认专用角色、policy、RLS enable 和分类函数均已清除，再恢复候选服务。

1. **入口开放前且尚未产生候选写入**：停止候选 writer，执行上述兼容降级步骤，恢复旧 backend/frontend digest，健康后恢复入口。允许保留经演练确认对旧服务无影响的新增表、可空列和历史元数据。
2. **已产生项目数据但数据库未损坏**：停止候选 writer，执行同一兼容降级，确认项目 Agent 不进入旧目录、55 个或发布前记录的标准 Agent 集合完整，再切换精确旧 digest。普通回滚不要求抹除所有项目 schema 或历史变化。
3. **兼容降级失败、出现数据损坏或旧服务无法保持原能力**：停止全部 writer，按同一恢复点恢复 PostgreSQL、AgentData、Redis/对象存储，再恢复旧 backend/frontend digest。该灾备路径会丢弃恢复点之后的写入，必须由数据负责人批准。
4. 不单独执行未经演练的 Alembic downgrade，也不只恢复数据库或只恢复 AgentData。应用回滚使用已经验证的完整兼容序列；灾备恢复必须保持所有权威数据源同点。

## 十、授权检查点

以下操作均未获本计划自动授权：

- 继续修改候选代码或再次合并新的主线；
- 推送 `company/main`；
- 创建或推送 Git tag；
- 构建并推送生产镜像；
- 生产 pull、停写、备份、迁移、切换；
- 真实生产渠道验收；
- 回滚或数据恢复。

A-01、A-02、A-03、A-04 和 A-08 已关闭，应用候选结论为 `GO for production preparation`。A-07 在生产准备阶段作为执行前提完成，不回退成研发任务。该 GO 不构成生产发布授权；只有 A-07 就绪并取得构建推送、停写、备份、迁移、切换和生产验收的逐项明确授权后，才允许执行对应操作。
