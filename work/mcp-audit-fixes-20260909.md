# MCP 审计问题修复交付记录

日期：2026-09-09。分支：`fix/mcp-global-disable`。本记录随修复代码提交。

## 结果

已合入权威远端 `yybpc/company/main` 的 `5ed4e42c`，合并提交为 `bf30af73`。产品版本保持 `1.10.3`。

- 私有 MCP 不带 Agent 范围的刷新/检查在提供者调用前拒绝；新工具来源由已有目录决定。stdio upsert 省略来源参数也不能把私有目录变共享。原管理者继续可管理，其他 Agent 不获得目录可见性。
- 启动清理只选择可证明为私有的孤立工具，按统一顺序锁定后重新检查绑定和 Project 引用；共享/未分类目录在零绑定时仍保留。
- 检查和执行共用有效配置、Project 来源、占位符及非 ASCII header 处理、精确 Smithery 安装路由。已知安装缺少路由/key 时失败，不借用全局其他连接；检查不触发恢复或写目录。
- Project 检查使用正确工作目录；历史 Project 覆盖即使本地绑定被禁用也不参与凭据解析，来源引用仍受启用、同租户及来源创建者身份限制。
- 未绑定的共享目录保留先配置/检查能力，不自动创建绑定；全平台禁用不能利用该回退。共享安全配置投影保留来源身份字段，不放开 URL/command/args/prompt。
- 临时 stdio 发现注册使用独立 invocation，避免与并发调用共用和注销同一注册。

规范已同步到 [tools-and-sandboxes](../.agents/architecture/tools-and-sandboxes.md)。新错误文案进入中英文 i18n。没有批量重命名或修写历史安装数据；已经在旧版误分类/误删的数据，需要授权核实实际数据后另行定向修复，不能凭名称猜测归属。

## 验证证据

全部后端、前端、浏览器验证运行于本地 Docker；没有用生产作为测试目标。

| 边界 | 结果 |
| --- | --- |
| 中立审计 | 原两项 P1 关闭，独立数据库核心 7 项通过；复审发现 disabled Project 历史凭据残留，修复后独立对应检查 1 项通过；身份字段保留另经只读复核确认不放宽权限 |
| 受影响回归 | 20 文件共 111 项；合并运行 110 passed、1 failed，失败定位为共享配置投影丢失 scope_id；修复后相关 4 文件 21 passed、4 warnings，原失败项通过。其余已通过用例未作无理由重复 |
| 占位符错误 | 输出接入 i18n 后，creator/匿名凭据边界 2 passed、3 warnings；匿名不能取得创建者占位符或发出提供者调用 |
| 真实本地 HTTP | 实际 initialize/tools-list/tools-call；产品 execute_tool 真实子进程执行私有 A/B/A，独立安装/凭据正确；共享双 Agent 覆盖隔离；旧 scene 下平台撤权无新增提供者请求 |
| Project 真实 HTTP | 检查和实际调用分别得到来源创建者 A 或共享身份；本地禁用后匿名身份的检查忽略旧 B 凭据；没有复制配置或保留数据库读事务 |
| Smithery 协议适配 | 使用真实 MCPClient/HTTP 请求构造及隔离 MockTransport；两个安装的 namespace/connection/key 分别一致，JSON/SSE 均成功，401 检查不恢复、不改数据库 |
| 浏览器 | Docker Chromium → 隔离 3008 前端 → 实际 API；共享 B key/header 保存并检查，提供者收到 B/B；共享基础编辑/刷新不显示；私有后缀及刷新入口存在；pageerror 为空，已检查截图 |
| 前端完整构建 | Docker `npm run build`，包括全部既有 prebuild、TypeScript、Vite，成功；保留已有大 chunk 提示 |
| 新主分支索引 | 独立库 bootstrap → downgrade 到 repair_bootstrap_indexes → upgrade heads；最终 agent_tool_lookup_index，索引 valid/ready 均 true |
| 数据库种子 | 隔离 PostgreSQL 执行 seed_builtin_tools + clean_orphaned_mcp_tools；两项 MCP builtin 描述/schema 与种子定义一致 |
| 工程门禁 | 从原集成基线累计 54 个新/改源码文件均不超过 800 物理行，最高 752 行。Python 编译、措辞扫描、diff 检查通过 |

定向 Ruff 新改业务模块/测试通过。`tool_seeder.py` 存在 10 项原有 F401/E402；已对合并后父提交同文件运行 Ruff，数量及问题相同，本次没有引入新项。未为此修改无关种子导出和导入顺序。

## 可复核命令与环境

测试镜像为本地 `clawith-backend-test:agent-isolation-lint`；backend/frontend 以只读方式挂载本候选。隔离配置通过权限受限的临时 env 文件注入，不入库。基础测试库为 `test_mcp_release_fix`，Project API 测试另用 `test_mcp_project_fix`，索引演练另用 `test_mcp_index_fix`；三个数据库均属于本任务。

```bash
docker run --rm --network container:mcp-ownership-pg \
  --env-file "$TEST_ENV" --env-file "$PROJECT_TEST_ENV" \
  --entrypoint python -v "$PWD/backend:/app:ro" -v "$PWD/frontend:/frontend:ro" \
  -w /app -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 \
  -e AGENT_EXECUTION_ISOLATION=0 clawith-backend-test:agent-isolation-lint \
  -m pytest -q -p no:cacheprovider \
  tests/test_mcp_connection_check.py tests/test_mcp_installation_ownership.py \
  tests/test_mcp_refresh_revocation.py tests/test_mcp_binding_management.py \
  tests/test_mcp_platform_visibility.py tests/test_mcp_runtime_header_encoding.py \
  tests/test_mcp_runtime_placeholder.py tests/test_mcp_servers_dry_run.py \
  tests/test_mcp_recovery.py tests/test_mcp_refresh_project_references.py \
  tests/test_agent_self_install_stdio.py tests/test_mcp_runtime_creator_fallback.py \
  tests/test_mcp_servers_overrides_api.py tests/test_mcp_tool_refresh.py \
  tests/test_resource_discovery_mcp_bridge.py tests/test_scene_runtime_settings.py \
  tests/test_mcp_server_bulk_update_perms.py tests/test_mcp_server_perms_relaxed.py \
  tests/test_project_actions_create.py tests/test_mcp_execution_process.py
```

21 项最终定向复验使用同一 Docker 命令，仅选择 `test_project_actions_create.py`、`test_mcp_connection_check.py`、`test_mcp_servers_overrides_api.py`、`test_mcp_installation_ownership.py`。协议替身用例关闭子进程隔离；真实 HTTP 交付脚本另显式开启 `AGENT_EXECUTION_ISOLATION=1`，不把替身传入父进程等同于子进程验证。

本地原始日志：`/tmp/mcp-fixed-final-regression.log`、`/tmp/mcp-final-project-config.log`、`/tmp/mcp-final-placeholder.log`、`/tmp/mcp-live-fix-acceptance.log`、`/tmp/mcp-ui-fix-acceptance.log`、`/tmp/mcp-release-full-frontend.log`、`/tmp/mcp-index-migration-fix.log`。截图为 `/tmp/mcp-shared-fix-browser.png`、`/tmp/mcp-private-fix-browser.png`；日志和临时测试身份不进 Git。

早期尝试如实记录：Ruff 初次尝试写只读缓存后改用 no-cache；一次测试路径拼错未运行；替身用例未关闭子进程隔离导致无法拦截 provider；Project fixture 初次未设置其专用 PostgreSQL 环境变量而触发 SQLite JSONB 装配错误，修正后在独立 PostgreSQL 重验；管理员刷新旧 fixture 改为实际 admin 来源；浏览器旧定位器未兼容空白、图标及页面其他 tabs，改为实际控件定位后通过。这些失败没有计入通过证据。

## 发布边界

本地修复和限定审计完成，未推送、未构建/推送生产镜像、未部署。外部 Smithery OAuth、真实 AIO stdio sandbox、生产 schema 副本与旧生产镜像回滚兼容尚未验证；当前 stdio 是适配器/生命周期回归，不能写成外部服务验收。按 [发布计划](mcp-release-plan-20260909.md) 完成这些适用门禁、镜像 digest、生产现状核实和授权后再作 GO 决策。
