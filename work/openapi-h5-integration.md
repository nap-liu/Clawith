# Standard system OpenAPI iteration

## Engineering authority

This work is exclusively governed by the Digital Employee Platform repository
`AGENTS.md` and the task routes it selects. No Grid engineering workflow,
architecture boundary or test policy is applied to this repository. Only the
versioned RFC-based integration contract is shared across the two products.

Read for this iteration:

- `AGENTS.md`
- `.agents/workflows/read_architecture.md`
- `ARCHITECTURE_SPEC_EN.md`
- `.agents/rules/design_and_dev.md`
- `.agents/workflows/engineering_change.md`
- `.agents/rules/github.md`
- `.agents/rules/deploy.md`
- `.agents/architecture/identity-directory-and-channel-bindings.md`
- `.agents/architecture/conversations-and-turns.md`
- `.agents/architecture/environments-and-operations.md`

No production release was requested, so the release/runbook path is not invoked.
The implementation is authorized; push, PR, release and deployment are not.
Repository rules permit a local implementation commit. The integration lead
commits only the reviewed worktree changes; no push or release is included.

## Baseline and ownership

Dedicated worktree `.worktrees/openapi-h5-integration`, branch
`feat/openapi-h5-integration`, baseline `89f5c976` selected by integration lead.
The repository has no `company` remote; company branch refs exist under other
remotes. Existing dirty main checkout and all unrelated worktrees are untouched.

The OpenAPI agent owns backend, system-client management UI and canonical
OpenAPI architecture documentation. The integration lead contributes the generic
`OpenApiLogin` frontend page, route and its separate localization block under
this repository's rules. Both contributors review the combined diff before
handoff; neither imports the other project's internal models or permissions.

## Todo

- [x] Read repository rules and inspect baseline, worktrees and containers.
- [x] Agree the standard OAuth and page-independent login business contract.
- [x] Implement system application lifecycle, OAuth and delegated-user APIs.
- [x] Implement generic signed temporary login and reuse normal login owner.
- [x] Add system management UI and canonical architecture/index references.
- [x] Run isolated Docker migration and real API product walkthrough.
- [x] Run focused OAuth/identity/signed-link risk validation.
- [x] Run complete frontend build with repository prebuild checks in Docker.
- [x] Review real browser login/admin UI and two-system result readback.
- [x] Final diff/source-line review and local implementation commit.
- [x] Handoff with explicit validation limits and retention owner.

## Acceptance evidence so far

Isolated `clawith-openapi-validation` Docker PostgreSQL/Redis, disposable
`test_openapi` database. Backend mounts this exact checkout, with workers disabled
and lifespan/bootstrap disabled after explicit schema bring-up. Backend also
joins `grid-digital-employee-lab` solely for integration validation. Independent
frontend proxy uses port 64514; the shared 3008 stack is unchanged.

Fresh database bootstrap and existing-base downgrade/upgrade of the new migration
passed. Live HTTP product walkthrough passed: create client, OAuth Basic+form,
discover delegated user's employee, sign generic login link, consume it, and read
the employee through the ordinary user API. Focused PostgreSQL-backed API test
passed scope, replay, concurrent consumption, redirect, identity, revocation and
rotation risks. Full Docker `npm run build` including prebuild checks passed.
Ruff is not installed in the available backend test image; no host fallback used.

## Completion gate

The repository requires observable Docker evidence, final diff review, canonical
documentation for changed invariants, explicit unverified limits, `git diff
--check`, and at most 800 physical lines in every delivered handwritten source
file. Existing shared services, UI controls, i18n, identity and permission owners
are reused. Completion includes a short local sound. Worktree cleanup follows
this repository's Git/post-release rules; unmerged work is retained and unrelated
worktrees are never removed.

## Agent handoff

Backend and management UI implementation/review complete; the integration lead
continues browser and cross-system acceptance in the same worktree. No commit,
push, deployment, shared-stack change or worktree removal performed by this
agent. Retain this unmerged worktree until reviewed integration is committed.

Additional verified risks: mainland domestic/+86/0086 exact phone equivalence,
duplicate-equivalent Identity refusal, unchanged foreign-phone exact lookup,
unknown OAuth extension acceptance, invalid scope whitespace rejection,
cross-client revocation isolation, expired login credential and inactive user.
The latest PostgreSQL-backed focused test passed after these changes.

Latest full frontend build passed after replacing the custom confirmation area
with the repository's shared ConfirmModal. The independent frontend login route
was re-read as HTTP 200 with no-referrer/no-store after build completion. A brief
404 occurred during Vite's rebuild of the mounted dist directory; it is resolved.
Further validation builds must use a staged output before switching the served
artifact to avoid interrupting integration tests.

All delivered handwritten source counts are <=800; the largest touched source
is `canonical_user_resolver.py` at 789 lines. Localization JSON is declarative
and exempt. `git diff --check` passed. Browser navigation and management UI final
result remain with the integration lead, as does combined-source commit approval.

## Integrated acceptance

The integration lead verified the real browser generic login to `/explore`,
ordinary authenticated navigation and removal of the code from the URL. A reused
link renders a localized expiry message without retaining the query. The login
failure heading uses the existing generic login translation. The final Docker
frontend build passed and staged assets before replacing index.html.

The system administration UI created a client, showed its secret once, rotated
it, revoked it and read its audit. Revoked controls become disabled. Desktop and
mobile views were inspected; the new section fits its parent. Existing platform
settings outside this section still have mobile horizontal overflow.

Both administrator and delegated member passed ordinary WebSocket authentication
and message persistence with the correct owner. The isolated employee has no LLM
model configured, so the normal turn returned the explicit model-missing error;
natural-language model replies were not verified. Cross-origin Safari/device
behavior and production deployment were not tested.

Non-secret browser evidence is retained in Docker volume
`clawith-openapi-evidence`. The worktree and feature branch remain because the
primary checkout contains user-owned uncommitted work and this feature is not
merged. Owner: integration lead. Next action: integrate after that work is
committed; review retention at the next iteration and remove the worktree only
after integration. No unrelated worktree or shared service is cleaned.

Implementation committed locally as `cc7599a8`. Final OpenAPI schema inspection
also confirmed exact per-operation OAuth scopes through FastAPI Security; the
focused PostgreSQL API test passed again after that annotation change. The
generic login failure heading and current frontend production build are verified.
The integration browser and completed build helper were removed after evidence
archival. The service containers remain only until the client iteration finishes
its review; their cleanup does not remove this unmerged feature worktree.

## Follow-up: member H5 connection

- [x] Diagnose the ordinary member WebSocket/session handshake in isolated Docker.
- [x] Confirm no authoritative owner defect in the real handshake; no implementation change needed.

The Builder member bootstrap and ordinary `/api/auth/me` succeed, while the H5
view remains disconnected. Preserve the generic login contract and inspect the
existing Clawith WebSocket path. Do not modify the shared 3008 service or print
credentials, login codes or credential-bearing query strings.

Member follow-up result: the existing backend path passed without any product
patch. A standard delegated login was compared to the fixture member ID inside
the container, then the ordinary WebSocket emitted `connected` with
`read_only=false`. A real member message completed through the shared turn path
with the existing explicit no-model result. Database readback found one message
and confirmed it belongs to that member. The backend has no member handshake or
permission defect in this reproduction. The frontend validation owner is checking
browser foreground/visibility because H5 intentionally suspends hidden pages.

- [x] Diagnose ordinary member WebSocket/session handshake in isolated Docker.
- [x] Verify real member turn and persisted member ownership; no owner patch needed.

## 2026-09-08 接管与主线同步

本节是本次接管的最新状态；前文为原迭代验收记录，不能视为合并后重新验收。

- 工作区继续使用 `.worktrees/openapi-h5-integration`，分支
  `feat/openapi-h5-integration`；本次未创建额外工作区。
- 接管起点为 `ff171faf`。本次 fetch 后的公司主线为
  `yybpc/company/main` 的 `9333e897`，主线有 30 个待合入提交，功能分支有
  3 个独有提交。用户授权方向为把主线合入功能分支，并在此分支继续接管。
- 合并冲突涉及 `backend/alembic/env.py`、`backend/app/main.py` 和
  `backend/entrypoint.sh`。保留主线统一 bootstrap，将 OpenAPI 模型注册迁到
  `backend/app/models/registry.py`，保留全部 OpenAPI 路由。
- 新增 `merge_openapi_bootstrap` 合并迁移，汇合
  `openapi_applications_v1` 与 `repair_bootstrap_indexes`，不重写历史迁移。
- 主目录的未提交改动、其他工作区和既有联调服务继续保留；本次仅做本地合并，
  没有 push、反向合入公司主线或部署。

### 当前产品能力

| 能力 | 当前范围 |
| --- | --- |
| 外部应用管理 | 平台管理员创建、编辑、启停、轮换密钥、永久撤销、查询审计；绑定固定租户，配置 scope、委托身份、有效期、限流与来源白名单 |
| 系统认证 | OAuth Client Credentials，Basic + 表单换取 300 秒 Bearer；scope 限制、令牌撤销和服务发现 |
| 用户委托 | 可信应用提交 subject、已验证手机号和时间戳；匹配既有有效身份及租户成员，拒绝冲突和越租户访问，不自动建人或赋权 |
| 数字员工发现 | 按委托用户原有权限搜索、分页，返回展示信息及普通 H5 入口 |
| 临时登录 | 60 秒一次性登录链接，兑换标准用户登录态后跳转到服务端绑定的目标；可以复用到 H5 以外的页面 |
| 普通 H5 会话 | 复用既有会话、权限、WebSocket 和共享执行链；本次同步也带入主线的自动场景与生成中追加消息能力 |

这是一轮系统接入能力，未增加面向外部应用的独立聊天 REST/流式接口、用户自动同步、
OAuth 授权码登录或 OIDC。嵌入来源白名单校验不等于跨站 iframe/cookie 全浏览器兼容。

### 本次验证与仍需跟进

- 隔离 Docker PostgreSQL：41 项通过，覆盖空库三种启动入口、重复及并发启动、
  两个父迁移版本升级、索引修复、OpenAPI 身份/权限/登录重放风险、账户状态、
  WebSocket 初始化和自动场景。
- Docker 前端全部 prebuild 行为检查、TypeScript 和 Vite 生产构建通过；构建产物
  放在一次性容器内，没有替换既有联调站点。首次构建因只读依赖目录阻止 Vite 写入
  临时配置而退出；增加容器内临时目录后完整重跑通过。保留大分块提示与依赖弃用警告，
  本次未执行全仓库后端测试或完整 lint。
- 最终合并差异检查通过；相对两个父分支的所有交付手写源文件均不超过 800 行，
  最大为 800 行，声明式本地化 JSON 不计入源文件门禁。
- 临时 PostgreSQL 容器 `openapi-merge-pg-20260908` 在验证后移除；测试与构建
  容器为一次性容器。原有 OpenAPI 联调服务及证据卷继续保留，当前工作区由本轮
  接管维护，待功能合入公司主线并按工作区清理规则满足保留条件后再处理。
- 原迭代记录的双系统浏览器联调不作为本次合并后的浏览器验收；本次未重新执行
  真实双系统嵌入、Safari/真机或自然语言模型回复。
- 下一步优先闭环成员 H5 嵌入页面的前台/隐藏生命周期和重连表现；后台已通过的
  鉴权、WebSocket 握手与落库证据不能代替可见页面验收。
- 再完成配置真实模型的完整回复，以及跨站 Safari/真机验收，之后评估发布准备。
  这些是后续验收范围，不代表本次已授权发布或修改另一个产品。

## 企业自助管理与界面调整

用户明确将应用管理归属调整为企业自行配置。本节取代前文的平台级管理口径。

- 入口迁到公司设置的 `#openapi` 页签，移除平台设置里的应用区块。
- 复用共享 Drawer、表单布局、输入框、开关、按钮与确认弹窗；创建/编辑按基本信息、
  开放能力、访问来源、使用限制分组，底部固定保存操作，移除组织选择。
- 管理接口迁到 `/api/enterprise/openapi/applications`，所有操作使用当前已认证企业；
  普通成员不可管理，平台管理员也必须先通过已有切换企业流程建立上下文。
- 存量应用沿用原租户和凭据，无数据迁移。企业管理的应用不得委托平台管理员身份，
  登录兑换时再次检查，防止链接签发后身份升级造成越权。
- 3008 继续直接使用既有数据库和文件目录。产品交付优先，验证围绕实际界面与
  企业权限边界，不扩展无关测试矩阵。
- 已用现有企业管理员在 3008 打开新入口和创建抽屉，桌面及 390px 窄屏通过实页
  检查：无组织选择，抽屉无横向溢出，保存区始终可见；未在现有库创建验收应用。
- Docker 前端完整构建通过。OAuth 原合同与企业权限场景在隔离 PostgreSQL 中通过。
  两文件合跑曾触发现有全局异步连接池跨事件循环错误，按独立进程运行通过；没有扩展
  全量测试。浏览器检查限本地 Chromium，没有新增 Safari/真机验证。
- 最终 diff 检查和 800 行源文件门禁通过，本轮最长交付源文件 715 行。
