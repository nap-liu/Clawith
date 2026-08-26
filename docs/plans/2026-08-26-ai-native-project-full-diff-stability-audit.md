# AI 原生项目管理全量差异稳定性审计

日期：2026-08-26

状态：**GO for production preparation（研发与候选验证闭环，尚未生产发布）**

## 一、审计对象与结论边界

本报告审计 AI 原生项目管理候选相对主线基线的完整生产代码差异，并把差异拆成“项目领域本体”和“横切稳定性”两层，避免只验证项目页面而遗漏对原有平台能力的影响。

| 项目 | 审计值 |
|---|---|
| 主线基线 | `yybpc/company/main@d963d85cbc4852feaa070a13915023f231ab8b4e` |
| 初始完整审计 SHA | `e04de8dd7b9a1916a5567ac30b0cc95373d2bc0f` |
| 最终应用 SHA | `828438e1c4daa5b93ac9dfe0b6e5ab0c363682ab` |
| 最终比较范围 | `d963d85...828438e1` |
| 最终变更规模 | 302 files changed，104692 insertions，12866 deletions；178 added，124 modified |
| 审计原则 | 以 API、数据库、事件、Docker 运行、浏览器行为和真实项目证据为准；源码文本匹配不作为稳定性通过证据 |

初始审计发现两个阻断问题，均已在最终应用 SHA 关闭：

1. `e04de8dd` 把 Plaza 作为全局能力退场，超出了项目能力交付范围。`828438e1` 已恢复原入口、API、工具、通知、活动和 Heartbeat，并只对项目 Agent 建立隔离。
2. 兼容降级后的老服务可通过已知项目 Agent ID 触达项目记录，旧 worker 也可领取项目 Subagent Run。`828438e1` 已增加专用旧服务数据库身份和可逆 RLS 边界，并由精确旧镜像闭环验证。

最终结论支持进入生产准备：项目主体、项目故障隔离、标准 Agent 资产隔离、Plaza 原能力、旧服务兼容回滚和真实项目交付链路均已有可观察证据。该结论不授权生产构建推送、停写、迁移或切换。

## 二、差异分层

### 2.1 项目领域本体

项目领域本体包括项目 API、模型与 schema、项目运行服务、项目仓库与资产、项目成员/任务/里程碑/会话/模板，以及前端项目列表和项目工作区。该层负责新增产品能力，主要风险是项目对象自身的正确性、权限、生命周期和交付完整性。

### 2.2 横切稳定性域

横切域是此次审计的重点。它包括所有不属于项目页面或项目领域本身、但为了接入项目能力而修改的公共生产路径：Agent、Chat、WebSocket、Scheduler、Trigger、Subagent、LLM、Files、Tools、公共前端组件、全局页面、i18n、水印、发布页和部署。该层必须证明：

- 标准 Agent 不因项目能力而改变来源配置、工作区、工具、会话或运行语义；
- 项目查询、项目状态或项目服务故障不会阻断旧能力；
- 兼容降级后的老服务不能看见或触达项目域对象；
- 公共组件改造不破坏原有全局页面；
- 新增容量、超时和锁不造成旧后台任务的非预期饥饿或重复执行。

## 三、项目领域证据

| 证据分区 | 结果 | 覆盖内容 | 判断 |
|---|---:|---|---|
| 后端完整分区 | `2741 passed, 28 skipped` | 在完整审计基线执行后端 Docker/PostgreSQL 行为分区；跳过项按测试环境能力记录，不计为通过 | 已通过；最终收口新增的 Plaza 与 legacy RLS 代码另由对应行为套件覆盖 |
| 项目协作分区 | `515 passed` | 项目创建、成员、规划、群聊、A2A、任务、运行、里程碑、文件、Git、模板、权限与回滚相关行为 | 已通过 |
| 市场与设置分区 | `41 passed` | Skill Market 8、项目/设置 9、运行时开关 6、Agent/模板生命周期 15、MCP 生命周期 3 | 已通过 |
| 前端 production build | 通过 | Docker production prebuild、TypeScript、Vite；10,297 modules transformed，仅保留既有大分块告警 | 已通过；构建证据不替代登录态交互验收 |
| 前端关键行为 | 通过 | 附件、Web 对话恢复、H5 时间线、项目会话路由等可执行脚本；候选 nginx 路由 smoke 无 console error/warning | 已通过；纯源码形状脚本不纳入行为证据 |
| 严格 RC3 | 通过 | 项目 `fb454fa3-ccb1-4f71-b587-a29ed200c3b6`：4/4 工作项、32 Runs、5 个角色、200 个事件、2 个里程碑；失败、活跃、未恢复和未关联均为 0；Git `5f46a1483080e32236343a302e560a533352174f`；证据 checksum `1cfbbd26974d3b6a819ce7f317e312a26e7180796e6143ca33ee70411e40a090` | 已通过真实跨域验收 |

项目主体证据说明新增能力可以完成完整生命周期，但不能单独证明原平台不受影响；横切域必须独立审计。

## 四、横切稳定性风险表

| # | 模块 | 生产差异与目的 | 可能影响的原能力 | 已有可观察证据 | 残余风险与结论 |
|---:|---|---|---|---|---|
| 1 | Backend：容量与数据库会话 | 新增全局、类别、租户工作负载门禁、过载等待与指标；Gateway、WebSocket、频道、Scheduler、Trigger、手动 Run、Task、Subagent、恢复链路统一接入；远程等待前释放数据库 session；新增 Redis 租约锁 | 普通 Agent 对话、定时、触发、任务和后台恢复的吞吐、排队、超时与连接池占用 | `test_workload_capacity.py` 8、`test_channel_dispatch_capacity.py` 2、`test_gateway_workload_capacity.py` 3、`test_background_session_boundaries.py` 9、`test_turn_recovery_capacity.py` 2、`test_database_capacity_config.py` 3、Redis lease 行为测试；合并后影响套件 78 passed | **高**。门禁是实例级而非 fleet 全局；生产并发参数、Redis 异常和多副本拥塞需上线观察 |
| 2 | Backend：项目故障隔离 | Scheduler、Task、手动 Run、Trigger claim/catalog/invoke 仅在项目上下文调用项目逻辑，并隔离项目查询或服务失败 | 项目子系统异常时普通 Agent 的会话、定时任务、触发器和手动运行 | `test_project_fault_isolation.py` 10，覆盖标准路径继续运行和暂停项目阻断；相关后台、手动、调度回归 36 passed | **低至中**。主要入口已有故障注入；仍需避免异常边界过宽掩盖非项目错误 |
| 3 | Backend：Agent 可见性与组织目录 | Agent 增加 scope/项目来源；项目 Agent 从标准 Agent 列表、发布页选项和广播等全局面隐藏；权限目录归一到 `org_directory`，纳入无 OrgMember 的活跃租户用户 | 标准 Agent 列表与授权、成员选择、发布页 Agent 选项、通知广播 | `test_agent_permission_directory.py` 6、`test_agent_visibility.py` 9、发布页共享目录行为测试 | **中**。共享目录的大组织分页、排序和资料回退仍需登录态抽样；legacy 深链泄漏属于本模块的阻断项 |
| 4 | Backend：Agent 工作区与文件 | 新增运行时工作区绑定；项目 Agent 定向项目仓库，标准 Agent 保持原工作区；文件读写、预览、下载、锁、版本、上下文、记忆、媒体、沙箱均经统一绑定 | 普通 Agent 文件路径、storage key、附件与媒体、版本历史和工作区协作 | `test_agent_runtime_workspace.py` 5，包含标准 Agent fallback 不变；项目文件、Skill 生命周期和后台工具输出保护行为测试 | **高**。所有旧文件端点都经过新解析层；发布前应抽样旧 Agent 文件读写、附件下载和版本历史 |
| 5 | Backend：工具、MCP、Skill | `agent_tools`、工具 API、刷新与 seed 链路支持项目作用域、组开关和执行前实时复核，同时保留标准 Agent 路径 | 普通 Agent 工具启停、MCP 刷新、Skill Market/安装和 Subagent 工具调用 | 市场与设置分区 41 passed；Agent/MCP 生命周期、MCP refresh、Skill Market、Subagent 行为套件 | **中**。后端资产隔离已覆盖；仍缺普通 Agent 登录态“开启、真实调用、关闭后拒绝”的端到端验证 |
| 6 | Backend：Chat Session/API | 普通会话列表和活动排除项目会话；精确项目会话按 ACL 可读；消息接口新增可选 cursor 分页，默认保留旧数组响应 | 旧会话列表、历史消息加载和会话查看 API 兼容 | `test_chat_sessions_api.py` 14、标准/项目会话隔离、只读监控行为测试 | **中**。旧默认响应已保留；需抽样超长历史和 cursor 边界 |
| 7 | Backend：WebSocket、频道、Gateway | WebSocket 增加项目运行拦截和只读监控；普通聊天继续共享 turn loop；Gateway/频道增加容量、租约和租户上下文 | Web/H5/IM 实时消息、断线恢复、只读监控和 A2A 频道 | WebSocket 断连 6、只读监控 9、频道分发 18、频道/Gateway 容量测试；Web/H5 对话恢复脚本 | **高**。缺旧 Agent 登录态长连接重连、附件、消息锚点和会话抽屉组合浏览器验收 |
| 8 | Backend：Subagent 与 Turn 恢复 | Subagent 统一到耐久运行、执行身份、冻结设置与工具、容量、取消、恢复、确认和审计保护；普通 Subagent claim 与项目运行隔离 | 普通 Agent 委派、同步/异步子任务、确认恢复、取消和撤销用户 | `test_subagent_runtime.py` 约 30 项，覆盖标准 claim、同步/异步、取消、追加、确认恢复、撤销身份和工具开关；Turn recovery 行为套件 | **高**。改动面最大；生产仍需观察长任务竞态、重复恢复和取消时序 |
| 9 | Backend：LLM、上下文与压缩 | 新增租户模型配置克隆；模型选择支持精确快照；Caller 增加 provider slot、总时长、TTFT、不活跃超时；压缩与历史保留精确锚点 | 全部普通 Agent 的模型选择、首 token、重试、长上下文压缩和错误呈现 | 模型配置 3、provider timeout/throttle、context guard 11、history compaction、channel error 12、turn recovery 测试 | **高**。真实供应商延迟和错误分布无法由隔离测试穷尽；上线需观察 TTFT、超时、重试和压缩指标 |
| 10 | Backend：Scheduler、Trigger、Task、Heartbeat | claim 提交后派发、容量与过载重试、远程调用前释放 session、暂停项目门禁；模板和术语同步 | 普通 Agent 定时任务、Trigger、后台 Run 与 Heartbeat | Scheduler/工具并发与过载行为、后台 session 边界、项目故障隔离和 Trigger runtime 行为套件 | **中至高**。多副本时钟、重排延迟和 lease 续期需生产指标；Heartbeat 模板语义需保持兼容 |
| 11 | Backend：A2A、活动、通知 | 新增 A2A 文件投递及发送/接收活动类型；PostgreSQL enum 前向兼容；普通活动过滤项目会话；租户广播排除项目 Agent | 标准 A2A 新会话、文件投递、活动反序列化和广播范围 | A2A 新会话 5、活动 enum 兼容 3、项目文件投递行为测试 | **中**。滚动部署时 enum 与迁移顺序仍需守门；项目 Agent 不进入旧广播是隔离要求 |
| 12 | Backend/Frontend：Plaza | 初始审计发现全局退场；最终 SHA 恢复前端路由、Onboarding、后端 router、标准 Agent seed/目录/runtime、通知、活动和 Heartbeat，只隔离项目 Agent | 原 Plaza 入口、API、工具、通知、活动和 Heartbeat | 新增 PostgreSQL/FastAPI/tool runtime 行为测试 `2 passed`；相关影响套件 `28 passed`；前端完整构建生成 Plaza chunk | **已关闭**。标准能力恢复；项目 Agent 的 post/comment/like/mention/broadcast/history/heartbeat 全部 fail closed |
| 13 | Frontend：公共 Shell 与组件 | `App` auth-loading 路由策略调整；Layout 增加项目入口、窄屏折叠；Dialog、Popover、MultiSelect、Pagination、SplitPane、成员选择器共享化 | 登录初始化、全局导航、响应式侧栏和所有复用弹窗/下拉/分页页面 | Docker production build 通过；nginx 路由 smoke 无 console error/warning | **中至高**。构建和未登录 smoke 不能替代登录态交互；需抽样 auth bootstrap、窄屏导航、弹窗和下拉定位 |
| 14 | Frontend：对话与公共会话抽屉 | Timeline 支持锚点、分析组消息 ID 和运行态占位；SessionViewerDrawer 扩展为完整时间线、输入、附件、WebSocket、分页和群聊 mention | 旧 Web/H5 对话、恢复、附件、引用与群聊 mention | chat attachments、Web resume、H5 timeline、rich mention 可执行行为脚本；后端 WebSocket/session 行为套件 | **高**。缺登录态组件级浏览器验证会话抽屉重连、输入、附件、锚点和分页组合 |
| 15 | Frontend：标准 Agent 工具/Skill UI 与 i18n | `ToolCatalogPanel` 复用到 Agent/项目；ToolsManager 增加 scope/canConfigure；Skill、工具名、分类和 MCP 分组归一；中英文词条与 fallback 更新 | 普通 Agent 工具搜索/启停、MCP/Skill 页面、语言切换和缺失翻译 | Agent tools/skills 路由 smoke 无 console error；后端工具生命周期/开关行为通过；production build 通过 | **中**。无真实前端操作自动化覆盖普通 Agent 工具开关、MCP 分组、Skill 导入和语言切换；源码 i18n 扫描不计行为证据 |
| 16 | Frontend：发布页与全局管理页 | PublishedPages 及样式大幅调整；发布页、用户、企业和邀请码改用共享 Pagination；成员目录复用 | 发布页管理/访问/批量操作、用户/企业/邀请码分页 | `test_published_page_access.py` 覆盖 ACL、租户、会话、分页、批量、目录和水印；Published Page SDK 行为脚本；production build | **高**。缺登录态管理 UI 的分页、筛选和批量操作浏览器验收；共享分页回归可能同时影响四页 |
| 17 | Frontend：水印与公开页 | PlatformWatermark 在 `/p/*` 路由抑制，发布页使用自身 SDK 水印 | 普通页面平台水印、公开发布页重复或缺失水印 | Platform watermark、Published Page SDK 可执行脚本和后端发布页 watermark 行为测试 | **低至中**。最终候选仍应对登录页、普通登录态页面和公开页各做一次视觉确认 |
| 18 | Deploy、CI 与迁移 | Dockerfile、Compose、Drone 增加镜像源参数、前端产物权限、容量环境项和镜像策略；部署编排同步 | 既有服务构建、启动、静态文件读取和环境默认值 | 真实 Docker production build 成功；候选 nginx 到 backend health 正常；3011 backend 重启后 HTTP 200、restart count 0 | **中**。镜像源覆盖和旧 Drone 流程需发布演练；fresh DB 问题继承自主线，仍是部署前置风险 |

## 五、两个必须关闭的稳定性发现

### 5.1 Plaza 全局退场已恢复

审计 SHA 中的 Plaza 改动不是项目领域实现所必需的适配，而是对既有平台产品的全局移除。它同时改变前端入口、路由、后端 API 注册、工具 seed/运行时、通知和活动过滤，因此不能以“新项目能力”授权覆盖。

关闭证据：

1. 恢复 Plaza 原入口、路由和后端 API 注册。
2. 恢复 Plaza 工具的既有 seed、启停和运行语义。
3. 恢复 Plaza 通知与活动可见性，不把项目 Agent 混入旧广播范围。
4. PostgreSQL/FastAPI/tool runtime 行为测试覆盖 Plaza 列表、详情、统计、发帖、评论、点赞、删除、工具可见性和调用边界；前端完整构建确认路由 chunk。
5. 项目 Agent 的隔离由 scope/权限实现，不通过关闭 Plaza 达成。

以上标准全部满足。最终会话的应用内浏览器连接不可用，因此没有把未执行的最终浏览器点击伪写为通过；此前 3011 登录态浏览器证据与最终生产构建、API 行为共同作为候选证据，正式发布窗口仍按发布计划执行 UI smoke。

### 5.2 Legacy 深链与 RLS 隔离已修复

兼容降级验证证明旧服务能够启动且标准 Agent 列表不展示项目 Agent，但进一步审计发现：仅在列表查询中过滤项目记录不足以构成安全边界，旧服务仍可能通过已知 ID 的 legacy 深链触达项目域对象。这违反“兼容回滚后老服务可安全运行”的要求。

关闭证据：

1. 在数据库 RLS、租户边界触发器或同等级不可绕过边界上阻止老服务读取、修改或关联项目域对象。
2. 修复不得要求老服务理解新项目业务，也不得破坏标准 Agent、会话、文件和工具数据。
3. 使用与生产旧版本一致的 backend 二进制，在隔离 PostgreSQL 执行兼容降级后复测。
4. 同时验证列表、直接 ID、嵌套资源、会话、文件、工具和关系深链；项目对象应 fail closed，标准对象保持成功。
5. 精确旧镜像 `sha256:a8f783c27b05e79de40f88b9107ff98b896c6277051c80adeec396f09e33ddd6` 的 API/worker 分角色健康且零重启；标准六接口 200，项目十接口 404，六类全局列表零项目标记，项目 Subagent/Schedule/Trigger 均未被消费。
6. `restore` 幂等，48 张保护表的 helper policy、RLS enable、专用角色和分类函数归零；项目历史快照 checksum 前后均为 `fa0afa5b04f4838c480a24d54d19dce9`；候选恢复后七个项目 API 全部 200。

该项已由长期 PostgreSQL 行为测试 `1 passed` 和精确旧服务二进制演练共同关闭。旧服务必须使用 helper 创建的专用 DSN 和分离的 `PROCESS_ROLE=api` / `worker`；owner DSN 或 `PROCESS_ROLE=all` 均禁止。

## 六、Fresh Database 迁移发现

Fresh PostgreSQL 从零执行 Alembic 时出现重复列问题。Git blob/hash 和迁移行为比较已确认，涉及的 `001_initial_schema.py` 与 `20260430_mcp_prompt_schema.py` 在 `yybpc/company/main@d963d85` 与候选完全相同，因此该问题继承自主线，不是本 feature diff 引入。

中立判断：

- 不把它计为项目能力回归或本 feature 新增缺陷。
- 它仍影响 fresh install、灾备重建和从空库演练，不能因“主线已有”而忽略。
- 正式发布前应明确生产采用既有数据库升级路径，并把 fresh DB 修复纳入独立主线整改；如果发布流程依赖从空库建立验证环境，则必须先修复或提供经验证的基线恢复步骤。

## 七、发布判断

当前证据覆盖项目主体、完整后端分区、项目协作、市场与设置、production build、严格 RC3、项目故障隔离、Plaza 恢复和精确旧服务回滚。相对 `e04de8dd` 的应用收口只包含 Plaza 兼容恢复、项目 Agent Plaza 隔离、legacy 可逆数据库边界和对应行为测试。

最终应用代码已加载到本地隔离栈：3011 的前端运行镜像与最新构建镜像同为 `sha256:d9e8d594b980705ecf956a280db7cf5e923ee00c00434c9b383ffb1b5153bc10`，后端直接挂载最终 worktree；`/api/health`、`/sdk/clawith.js`、`/plaza` 分别返回 200，未登录 `/api/plaza/posts` 返回 401，前后端均运行且重启计数为 0。

结论：**GO for production preparation**。研发尾项为 0；完整键盘与读屏按用户要求排除。Fresh DB 历史迁移问题和生产备份容量属于独立主线/生产准备前提，不是本分支新增回归。生产备份、制品、迁移、切换、真实渠道和发布窗口 UI smoke 仍需单独授权。
