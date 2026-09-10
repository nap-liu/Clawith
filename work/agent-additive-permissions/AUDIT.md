# Agent 叠加权限独立审计

初审结论：当时审计未通过，默认在线发布为 **NO-GO**。下列发现保留为初审记录；
本轮修复及复审状态见本文件末尾。本记录不是可执行发布计划。

## 审计对象与方式

- 基线：`yybpc/company/main`，`0cc8c01c1eb39a8c17f67f6333dbf701369aa677`。
- 已重新 fetch 并确认候选 HEAD 与公司主线一致。
- 对象：`feat/agent-additive-permissions` 全部未提交修改及新增文件。
- 独立审计者：本任务专门启动的 `neutral_permission_audit` subagent；只读审查，未参与实现。
- 主 agent 另行核对当前发布规则与完整 production release runbook，并复核下列触发链。
- 本轮没有修改产品源码、提交、推送、构建发布镜像或检查、操作生产。

## 发现

### P1：迁移后的旧版本写入会重新激活已撤销权限

位置：`backend/alembic/versions/20260910_additive_agent_permissions.py:28`、
`backend/app/core/permissions.py:284`。

触发顺序：

1. Agent 原来向全公司开放，迁移将有效授权归一化。
2. 迁移提交后，仍在服务的旧版 MCP 把它改为 private/custom。
3. 基线 `tools_config_access.py:406` 只改模式并补充内置管理者，保留 company grant。
4. 切换新版后，授权判断忽略旧模式，company grant 重新让全租户获得访问。

迁移的一次清理不能覆盖提交后旧版本继续写入的窗口。仅增加迁移事务锁也不能解决。

同一边界也影响 downgrade：迁移后旧 REST 将 custom 名单从 A 改为 B，旧 writer
没有写入 `agent_permissions_updated` 审计，且不存在 company grant；当前 downgrade
检查可能通过，随后恢复归档中的 A、删除 B，恢复已经撤销的授权。

修复要求：设计可兼容旧写入的过渡和安全回退检查，并在隔离 PostgreSQL 验证
“迁移 → 旧 MCP 收紧权限 → 新版本读取”与“迁移 → 旧 REST 改名单 → downgrade”。
若选择停写迁移，必须另外完成维护拓扑的审查和授权，不能套用默认在线发布流程。
新叠加授权产生后，直接切回旧镜像也不能保留新语义；拒绝 Alembic downgrade
不等于镜像回退安全。

### P2：公司开关保存会无意删除其他显式授权

位置：`frontend/src/pages/agent-detail/components/AccessPermissionsPanel.tsx:17`、
`frontend/src/components/AgentPermissionsEditor.tsx:26`、
`backend/app/api/agent_routes_permissions.py:73`。

设置页通过展示用的 user_access/department_access 重建全量 grants：

- creator/admin 被标为 is_required 后被前端过滤，既有显式授权随任意保存删除。
  管理员将来降级后，会丢失原本独立存在的授权。
- inactive department 被展示查询隐藏，保存时其 grant 一并删除；重新激活后原授权不再恢复。

修复要求：完整 grants 作为编辑事实，展示名单只补充名称和状态。修改公司开放
只修改 company grant；明确撤销或 creator-only 操作才删除对应其他授权。
验证应覆盖管理员角色降级、部门停用再激活，以及实际 UI 保存后的持久化结果。

### P2：Plaza 工具仍通过旧列授权

位置：`backend/app/services/agent_tools_plaza_ops.py:29`、`:97`、`:180`。

HTTP Plaza 已按 company grant 判定，但工具读帖、发帖、评论仍按 access_mode 判定。
旧列为 company 而没有 company grant 时，工具与 HTTP 会作出不同决定，违背单一授权来源。
`backend/app/services/heartbeat.py:137` 的隐私提示也仍按旧列选择。

修复要求：使用共享 company visibility 判定，并验证 HTTP 与工具在旧列和 grants
不一致时采用相同边界。

## 验证证据与限制

- 上轮 60 项相关测试、后续 34 项 MCP/contact 复验、浏览器操作和迁移演练结果
  见本目录 README。本轮独立审计没有重复运行这些测试。
- 已有测试确实覆盖 API、数据库、MCP 的可观察行为；未覆盖本报告列出的交错写入、
  旧写入后的 downgrade、管理员显式授权与失活部门的 UI 保存。
- 本轮 `git diff --check`、Docker 内用户可见文案扫描通过。
- 重新核对全部修改及新增源码的物理行数，均不超过 800 行，最大 772 行。
- 未验证生产当前镜像、迁移父版本、拓扑、数据量、备份和旧镜像兼容性；
  用户当前授权为审计及有条件的发布计划，不包含生产检查。

## 下一轮顺序与发布条件

先解决迁移和回退兼容，再修复 UI 无意撤权并补齐执行入口，运行上述有针对性的
Docker 行为验证，最后独立复审。通过后，按当前 release runbook 输出完整发布计划，
冻结最终 SHA、明确发布与补救策略及所有尚未授权的外部操作。

当前不提供 GO 结论，不执行发布准备或切换。候选改动和本任务隔离环境保留，
原 checkout 的其他进行中工作不受影响。

## 简化修复与限定复审

用户明确要求简单高效归一化，并排除已废弃 Plaza；第 3 项作为本轮范围排除，
不宣称已修复该废弃入口。

第 1 项采用一次已批准的停写迁移，未引入双写、数据库触发器或双套 ACL：
普通在线升级及已有库 bootstrap 在迁移前失败；显式 offline 参数只表示操作员
已核实所有旧 writer 停止，不能自行证明或强制停止旧 writer。完整前提已更新到
canonical agent-permissions 文档。非空库降级直接拒绝，不再恢复旧名单。

第 2 项设置页从完整 grants 构建编辑状态，显示数据只补充信息。管理员显式
授权保持原级别、可独立撤销；失活部门仍在名单中，公司开关与 picker 保存不会误删。

同一独立 subagent 对修复做限定复审，确认第 2 项关闭，第 1 项通过明确维护切换
收敛，旧 REST 无新审计导致的 downgrade 恢复漏洞关闭。默认在线发布仍 NO-GO，
维护发布必须单独批准；代码修复不代表已获生产操作授权。

最终文档复审也已通过，未发现本次范围内的新增阻塞性代码问题。本轮 5 项
PostgreSQL 测试、前端构建、真实浏览器保存/创建以及角色降级/部门启用后的
数据库授权结果通过。Plaza 仍是用户明确排除项，不计入“修复通过”的范围。
