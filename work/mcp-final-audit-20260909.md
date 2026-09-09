# MCP 平台禁用修复：独立审计与验证记录

日期：2026-09-09。此文件记录本次交付证据，不替代仓库规范。

## 范围与结论

基线为 `36195133`，初始修复为 `01a18932`，收尾修复与此记录一同提交。
分支为 `fix/mcp-global-disable`。新的独立 subagent 从基线重新审查原始
20 个变更文件及相关调用链，随后复核收尾修复；未依赖此前“审计通过”结论。
审计者只读审查，Docker 验证由主线程执行。

审计发现的两项问题均已修复并验证，没有未关闭的审计阻断项：

| 发现 | 性质 | 修复与证据 |
| --- | --- | --- |
| 其他租户或非 MCP 工具的规范名阻断当前 Agent 的远端别名 | 初始修复引入的回归 | 规范名存在性查询限定 MCP 类型与当前 Agent 可见范围；仍忽略 enabled，防止被禁用的自身规范名借别名回退。两类外租户占名测试及旧规范名/歧义测试通过。 |
| 单个 AgentTool 删除接口仅验证登录 | 基线已有、位于本次影响路径的授权缺口 | 复用 check_agent_access，要求 manage。真实 API 验证跨租户和 use-only 被拒绝，owner、org_admin、platform_admin 可删除；其他 Agent 的绑定保留，最后绑定删除后清理孤立 MCP Tool。 |

## 最终统一验证

在独立本地 PostgreSQL 15 容器初始化当前 schema，测试容器挂载本次准确
工作区，未写入共享开发数据库或生产。最终一次统一执行结果：

**140 passed，5 warnings，14.31 秒；无失败、无跳过。**

传统 provider mock 测试设置 `AGENT_EXECUTION_ISOLATION=0`；新增
`test_mcp_execution_process.py` 在测试内强制设为 `1`，使用真正执行子进程
与本地 HTTP MCP 服务。它验证场景允许的别名可调用，并在保持旧场景快照的
情况下关闭平台工具，随后规范名和别名均不再触达 provider。

| 影响面 | 已执行的测试文件（均位于 backend/tests） |
| --- | --- |
| 安装清单、平台禁用、规范名/别名、跨租户过滤、管理端保留禁用行、独立 CLI 展示 | test_mcp_platform_visibility.py、test_agent_mcp_lifecycle.py |
| 刷新、撤权/卸载/配置并发、旧安装迁移、已有目标禁用、项目引用并发关闭 | test_mcp_refresh_revocation.py、test_mcp_tool_refresh.py、test_mcp_refresh_project_references.py |
| 场景配置、项目授权、schema、toolscall 与 CLI 注入适配 | test_scene_tool_adapters.py、test_scene_runtime_settings.py、test_scene_project_tools.py、test_scene_runtime_schema.py |
| 扩展 prompt、HTTP/stdio、占位符、身份回退、header 编码、恢复 | test_agent_context_mcp_prompts.py、test_execute_mcp_tool_stdio.py、test_mcp_runtime_placeholder.py、test_mcp_runtime_creator_fallback.py、test_mcp_runtime_header_encoding.py、test_mcp_recovery.py |
| MCP 管理 API、批量更新、旧配置桥接、override | test_mcp_servers_api.py、test_mcp_server_bulk_update_perms.py、test_mcp_server_create_on_write_perms.py、test_tools_mcp_server_bridge.py、test_mcp_servers_overrides_api.py |
| 必需工具、租户可见性、启用规则 | test_required_tool_control_plane.py、test_tool_tenant_scope.py、test_tool_enablement.py |
| 删除管理授权与共享绑定保留 | test_mcp_binding_management.py |
| 真实执行子进程与 HTTP MCP | test_mcp_execution_process.py |

`test_agent_mcp_lifecycle.py` 包含真实数据库 seed 后的持久化 schema 与
`get_agent_tools_for_llm()` 输出一致性检查。管理 UI 使用的工具目录和删除接口
通过 FastAPI HTTP 边界验证；未以源码文字匹配代替行为验证。

## 工程门禁

- `git diff --check`：通过。
- 从 `36195133` 起全部交付源码：19 个文件，最高 795 物理行，均不超过 800。
- 本轮新增/修改代码定向 Ruff：通过。
- 全部变更源码的 Ruff 仍报告 6 项基线既存问题；已与原始基线核对，无新增：
  `api/mcp_servers.py` 的 3 项 E402，`api/tools_platform.py` 的 1 项 E711，
  `services/tool_seeder_builtin_4.py` 的 2 项 F401。因此不将整体 lint 宣称为全绿。
- 同步更新中英文资源与规范文档 `.agents/architecture/tools-and-sandboxes.md`。
- 无数据库迁移；原工作区及共享 Docker 栈不受修改。

## 边界与发布状态

本结论覆盖本次改动及已识别的受影响调用链，不是对整个仓库无缺陷的保证。
未运行全仓库测试、浏览器 3008 验收或生产验收。stdio/provider 行为主要通过
适配器 mock 验证，新增 HTTP 测试使用本地测试 provider；未调用生产 MCP 或
修复独立 CLI 的真实凭据。历史对话和已有 Skill 文本不会被此次改动清除。

平台 bulk 开关仍作用于明确请求的工具 ID，不新增对未来工具持续生效的
server 总开关。管理员显式发现保持既有新工具策略；已通过授权进入 provider
的请求可能完成，关闭提交后的后续授权检查拒绝调用。

仅本地提交，未推送、未部署、未修改生产环境。
