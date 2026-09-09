# OpenAPI 接入指南

协议版本：v1。更新日期：2026-09-09。

**交付状态：本文接口已实现，3008 环境已更新并返回新能力标识。**
首问执行、完整 JSON、幂等和实例会话已在隔离 Docker 环境验证。
其他环境以 `h5_launcher.interaction.supported: true` 为新能力可用标识；本文不表示已发布到生产环境。

机器可读合同：[OpenAPI 3.1 JSON](openapi-v1.json)，从本轮 FastAPI 路由和 schema 导出，
可用于导入接口工具或生成客户端。该文件与本文的新能力上线状态相同。

## 1. 接入流程

企业管理员在「公司设置 → OpenAPI」创建应用，启用「查询数字员工」「临时登录链接」和
「用户身份委托」，取得客户端 ID 和密钥。

1. 业务系统后端使用客户端凭据换取 Bearer token。
2. 以当前业务用户身份查询其可访问的数字员工。
3. 调用员工 access 接口，选择普通打开、按业务实例恢复会话，或携带问题和上下文打开新会话。
4. 扩展请求返回 `login_url`，浏览器直接打开，由平台完成登录并进入 H5；仅传 `user` 的既有请求按第 6 节接入。

客户端密钥和系统 Bearer token 由业务后端使用。浏览器只接收临时 `login_url`；
不要自行拼接聊天路径，也不要把问题和上下文放入 URL。

本文用 `https://clawith.example.com` 表示平台公开地址，`https://business.example.com`
表示业务系统来源。示例中的 ID、手机号和数据均为占位值。

## 2. 系统认证

### 换取 token

`POST /api/openapi/v1/auth/token`

使用 HTTP Basic 客户端认证，Body 为表单，不能改成 JSON。

```bash
curl --fail-with-body "$BASE_URL/api/openapi/v1/auth/token" \
  --user "$CLIENT_ID:$CLIENT_SECRET" \
  --header 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode 'grant_type=client_credentials' \
  --data-urlencode 'scope=employees:read auth:login'
```

```json
{
  "access_token": "<system-access-token>",
  "token_type": "Bearer",
  "expires_in": 300,
  "scope": "auth:login employees:read"
}
```

省略 `scope` 时使用应用已开放的能力。按 `expires_in` 缓存 token；过期后重新获取，
没有 refresh token。后续接口使用 `Authorization: Bearer <system-access-token>`。
当前生成的客户端 ID 和密钥可直接用于上面的 curl；通用 OAuth 库应按标准分别对
ID 和密钥做表单编码，再构造 Basic 值。

应用停用、撤销、轮换密钥或修改配置后，旧系统 token 和未使用登录链接失效。
已经建立的普通用户登录态遵循平台现有登录规则。

### 查询能力

`GET /api/openapi/v1/capabilities`，携带 Bearer token。

新增能力上线后的返回示例：

```json
{
  "protocol_version": 1,
  "scopes": ["auth:login", "employees:read"],
  "trust_user_identity": true,
  "h5_launcher": {
    "interaction": {"supported": true, "version": 1},
    "instance_ref": true,
    "scenes": {"list": true, "activate": true}
  }
}
```

`supported` 表示服务支持此协议；调用方仍需具有两个 scope 且开启用户委托。
本轮不增加专用 scope，也不新增问题字数、上下文字节数或数据行数限制。

## 3. 委托用户

需要用户身份的请求包含以下 `user`：

```json
{
  "subject": "business-user-42",
  "phone": "+8613800000000",
  "asserted_at": 1788919200
}
```

| 字段 | 要求 |
| --- | --- |
| `subject` | 业务系统中稳定的用户标识，同一应用内不要随会话或登录变化 |
| `phone` | 业务系统已验证的当前用户手机号，用于匹配平台已有企业成员 |
| `asserted_at` | 本次请求生成时的 Unix 秒时间戳，允许与服务端相差 60 秒；示例时间不可直接复用 |

平台不通过此接口创建账号或授予员工权限。用户必须是应用所属企业的有效成员，
并有权访问对应员工。已有 subject 绑定不能通过更换手机号切换成另一个人。
平台管理员身份不能通过企业应用委托。

## 4. 查询数字员工

`POST /api/openapi/v1/digital-employees/search`，需要 `employees:read`。

```json
{
  "user": {"subject": "business-user-42", "phone": "+8613800000000", "asserted_at": 1788919200},
  "search": "经营",
  "page": 1,
  "page_size": 20
}
```

`search` 可省略，`page` 默认 1，`page_size` 默认 20、最大 100。

```json
{
  "items": [{
    "id": "11111111-1111-4111-8111-111111111111",
    "name": "经营分析",
    "avatar_url": null,
    "description": "协助分析经营数据",
    "access_url": "https://clawith.example.com/h5/agents/11111111-1111-4111-8111-111111111111/chat"
  }],
  "total": 1,
  "page": 1,
  "page_size": 20,
  "has_more": false
}
```

只返回委托用户可见的员工。`access_url` 是普通入口，**本身不包含登录授权**。

## 5. 打开 H5 与携带首问（本轮新增）

`POST /api/openapi/v1/digital-employees/{employee_id}/access`

### 请求字段

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `user` | object，必填 | 上述委托用户 |
| `instance_ref` | 非空 string，可选 | 业务侧稳定的会话实例引用，不能只有空白；由调用方自行命名，平台不解析其业务结构 |
| `scene_key` | string，可选 | 从有效场景列表取得的标识；打开登录链接时激活 |
| `interaction` | object，可选 | 本次首次提问；省略时不自动提问 |
| `interaction.request_id` | 非空 string，必填 | 本次业务动作的幂等键，不能只有空白；推荐 UUID，重试保持原值 |
| `interaction.message` | 非空 string，必填 | 用户问题；不能只有空白 |
| `interaction.context` | 任意 JSON，可选 | 本次数据快照；对象、数组、字符串、数字、布尔值均可，省略或 null 表示无附加资料 |
| `embed_origin` | string，可选 | 嵌入页面的来源，如 `https://business.example.com`，不含页面路径 |

只传 `user` 的既有请求仍只返回员工信息，需要 `employees:read`。
使用新字段请求登录入口时需要 `employees:read` 和 `auth:login`。
只增加有效 `embed_origin` 也可一次取得登录入口，此时不关联实例、不自动提问。
可选字段为 `null` 时视为未传；`interaction` 存在时才要求其 `request_id` 和 `message`。
`user`、请求顶层和 `interaction` 不接受未声明字段；`context` 内部字段由业务系统自由定义。

### A. 按业务实例普通打开

```json
{
  "user": {"subject": "business-user-42", "phone": "+8613800000000", "asserted_at": 1788919200},
  "instance_ref": "dashboard:12:assistant:456",
  "embed_origin": "https://business.example.com"
}
```

首次打开创建该实例会话，以后恢复该实例当前会话，不自动提问。
同一员工在不同业务位置需要独立会话时，使用不同 `instance_ref`。

### B. 携带问题与上下文

```json
{
  "user": {"subject": "business-user-42", "phone": "+8613800000000", "asserted_at": 1788919200},
  "instance_ref": "dashboard:12:assistant:456",
  "embed_origin": "https://business.example.com",
  "interaction": {
    "request_id": "bb4ce8b0-d97a-4c7f-b428-65960c4c095a",
    "message": "分析营业额同比下降的原因，并引用当前数据给出建议。",
    "context": {
      "version": 1,
      "captured_at": "2026-09-09T02:00:00Z",
      "source": {"system": "business", "label": "门店经营看板"},
      "location": {"application_id": "12", "page_id": "35", "element_id": "88"},
      "target": {"element_id": "456", "page_id": "36"},
      "page_state": {"parameters": {"store": "S01"}, "active_tab": "monthly"},
      "values": {"revenue": 128000, "yoy": -0.12},
      "datasets": [{
        "name": "daily_revenue",
        "references": {"table_id": "51", "data_source_id": "18"},
        "columns": [{"key": "amount", "label": "营业额", "field_id": "312", "unit": "元"}],
        "rows": [{"record_id": "901", "amount": 4200}],
        "query": {"filters": {"store": "S01"}, "page": 1, "page_size": 20},
        "selection": {"record_ids": ["901"]},
        "scope": "current_page",
        "returned_rows": 1,
        "has_more": true
      }]
    }
  }
}
```

`context` 结构由调用方定义，上例不是必填业务 schema。平台保留 JSON 结构和类型，
将其作为用户级参考资料提供给员工；H5 展示原问题和可展开的完整上下文。
来源页面与承载聊天的目标实例可以不同。建议明确快照时间、单位、筛选、分页和数据范围，
避免把当前页数据理解成全量数据。已有工具仍按原权限运行，资源 ID 不授予额外权限。

### 返回

```json
{
  "id": "11111111-1111-4111-8111-111111111111",
  "name": "经营分析",
  "avatar_url": null,
  "description": "协助分析经营数据",
  "access_url": "https://clawith.example.com/h5/agents/11111111-1111-4111-8111-111111111111/chat",
  "login_url": "https://clawith.example.com/openapi/login?code=<one-use-code>",
  "expires_in": 60,
  "request_id": "bb4ce8b0-d97a-4c7f-b428-65960c4c095a"
}
```

`request_id` 仅带 interaction 时返回。浏览器直接打开 `login_url`，
**不要再次调用 `/auth/links` 包装它**。此时 `access_url` 仍只是员工普通入口。

签发链接只保存待处理数据，不调用模型。用户打开链接、完成登录与员工权限检查后，
平台创建新会话并提交一条首问，后续发送、附件、流式回复、停止、确认和历史会话沿用原 H5。

### 幂等与会话规则

| 场景 | 结果 |
| --- | --- |
| 新用户动作使用新 `request_id` | 打开后创建新会话和首问 |
| 同动作重试，员工、实例、问题、上下文相同 | 返回新的临时链接；激活同一个会话和首问 |
| 同 `request_id` 改员工、实例、问题或上下文 | 409 `interaction_conflict` |
| 签发后未打开 | 不执行，不改变实例当前会话 |
| 激活新问题成功 | 该实例的当前会话切换到新会话；旧会话保留 |
| 重复激活、重签后再打开、多标签并发 | 不重复提交首问 |
| 原首问执行失败后重新打开 | 进入原会话，显示原执行状态，不自动重发 |
| 未激活交互超过 10 分钟 | 410 `interaction_expired`；同 ID 不会重新开始 |
| 已激活交互对应会话或首问被删除 | 410 `interaction_unavailable`；同 ID 不会重新创建 |

幂等范围是「OAuth 应用 + 委托用户 + request_id」。实例会话范围是
「OAuth 应用 + 委托用户 + 员工 + instance_ref」。不同应用或用户不会共享会话。
更新 token、`asserted_at` 或嵌入来源不改变业务内容；JSON 对象键的顺序不影响幂等，
数组顺序、值和类型按业务内容比较。省略 context 与 null 均表示无附加资料。

登录码有效期是 60 秒，待激活交互有效期是首次签发起 10 分钟。
登录码过期或已经使用时，后端用原 request_id 和原业务内容重新请求 access，
同时更新 `asserted_at`。重签不会延长交互的 10 分钟期限。
重复请求的关联和过期记录至少保留 24 小时；请为新动作始终生成新 ID。

## 6. 获取有效场景与激活

### 获取员工有效场景

`POST /api/openapi/v1/digital-employees/{employee_id}/scenes/search`

需要 `employees:read` 和可信用户委托，请求体只需 `user`：

```json
{
  "user": {"subject": "business-user-42", "phone": "+8613800000000", "asserted_at": 1788919200}
}
```

返回 `items` 和 `total`。每项沿用平台公开场景 manifest，主要字段如下：

| 字段 | 含义 |
| --- | --- |
| `scene_key` | 激活时传入的场景标识，例如 `business-analysis` |
| `name` | 已发布场景名称 |
| `enabled` | 有效列表中为 true |
| `revision` | 当前已发布版本号 |
| `welcome_message` | 场景欢迎语 |
| `quick_actions` | 允许用户看到的快捷操作 |

只返回已发布且启用的场景，不返回草稿、停用场景。员工未启用场景能力时返回
`{"items":[],"total":0}`。系统提示词、工具及 MCP 凭据不对外提供。
该接口不提供场景创建、编辑和发布权限。

### 使用 access 激活场景

在第 5 节 access 请求增加顶层 `scene_key`：

```json
{
  "user": {"subject": "business-user-42", "phone": "+8613800000000", "asserted_at": 1788919200},
  "instance_ref": "dashboard:12:assistant:456",
  "scene_key": "business-analysis"
}
```

返回原员工字段、`login_url`、`expires_in`，并追加 `scene_key` 和 `scene_revision`。
浏览器直接打开 `login_url` 后激活场景；签发本身不改变会话，也不调用模型。
需要 `employees:read` 和 `auth:login`，不新增专用 scope。

需要按场景自动提问时，在同一请求中加入原有 `interaction`：

```json
{
  "user": {"subject": "business-user-42", "phone": "+8613800000000", "asserted_at": 1788919200},
  "instance_ref": "dashboard:12:assistant:456",
  "scene_key": "business-analysis",
  "interaction": {
    "request_id": "b77954cf-0630-4a85-9a55-b887254d36d2",
    "message": "分析当前营业额变化。",
    "context": {"revenue": 128000, "yoy": -0.12}
  }
}
```

平台先选择场景，再提交首问。原生员工的系统提示、工具配置、菜单和后续 H5 对话
复用既有场景机制。OpenClaw 保持现有 H5 场景展示与网关协议，本轮不将原生场景
提示词和工具配置转换为远端网关协议。

- 有 `instance_ref`、无 `interaction`：在该实例当前会话激活场景；首次打开创建会话。
- 无 `instance_ref`、有 `scene_key`：打开独立会话，避免改变原 H5 主会话。
- 带新 `interaction`：创建新会话并按指定场景提交首问，沿用原有幂等规则。
- 同 `request_id` 改 `scene_key`：409 `interaction_conflict`；重复打开已激活交互不覆盖之后的场景选择。
- 省略 `scene_key`：保持原有会话、自动场景和默认场景规则。

`scene_revision` 是签发时的当前已发布版本。打开时重查可用性，使用当时有效的已发布版本；
首问记录实际版本，后续切换场景不会改写已开始的那一轮。
场景下架、未发布或员工关闭场景能力时返回 404 `scene_unavailable`，不静默回退。
标识沿用平台既有格式，直接使用列表返回的 `scene_key` 即可。

## 7. 既有普通入口与通用登录

不需要实例隔离或首问的既有接入可以继续：查询或 access 取得 `access_url`，然后调用
`POST /api/openapi/v1/auth/links`（需要 `auth:login`）。

```json
{
  "user": {"subject": "business-user-42", "phone": "+8613800000000", "asserted_at": 1788919200},
  "redirect_uri": "https://clawith.example.com/h5/agents/11111111-1111-4111-8111-111111111111/chat",
  "embed_origin": "https://business.example.com"
}
```

返回 `{"login_url":"<one-use-login-url>","expires_in":60}`。
`/auth/links` 不接受 interaction；新能力统一使用员工 access 接口。
`POST /api/openapi/v1/auth/link-exchange` 由平台登录页内部调用，对接方无需自行兑换。

## 8. 来源与地址

系统 API 通过客户端凭据和 Bearer 授权，不要求调用方域名与平台域名相同。
企业配置的嵌入白名单留空时允许任意有效嵌入来源；填写后按显式配置校验 `embed_origin`。
来源填写 origin，正式地址使用 HTTPS，本地开发支持 localhost 等已有开发来源。

通用登录的 `redirect_uri` 可以是平台相对路径、平台公开地址，或企业明确配置的跳转来源。
新增 access 流程由服务端生成跳转目标，对接方无需自行校验或改写为业务系统域名。
如果返回了错误的公开地址，应修正平台公开地址配置。iframe 是否可用也取决于业务站点的
嵌入策略与浏览器行为；这些不会改变系统 API 的 Bearer 认证方式。

## 9. 错误与重试

业务错误沿用以下结构；按 `detail.code` 分支，不依赖提示文案。

```json
{"detail":{"code":"interaction_conflict","message":"interaction_conflict"}}
```

| HTTP | code | 处理 |
| --- | --- | --- |
| 400 | `invalid_interaction` | 修正新字段格式、无效 scene_key、空 request_id 或空问题 |
| 400 | `invalid_user_assertion` | 用当前 Unix 秒时间戳更新用户断言 |
| 401 | `invalid_token` / `invalid_login_code` | 更新系统 token，或由业务后端重签登录链接 |
| 401 | `application_disabled` | 检查应用、企业启用状态及凭据是否因配置变更失效 |
| 403 | `identity_delegation_disabled` / `identity_delegation_denied` | 检查企业应用委托设置和用户身份 |
| 401 或 403 | `user_unavailable` | 检查企业成员状态；登录链接兑换时使用 401 |
| 403 | `access_denied` | 检查员工原有权限和交互归属 |
| 403 | `embed_origin_denied` | 检查企业显式配置的嵌入来源 |
| 403 | `quota_exceeded` | 按已有平台额度处理 |
| 404 或 403 | `employee_unavailable` | 检查员工可用状态；保留原员工访问检查的错误语义 |
| 404 | `scene_unavailable` | 重新获取有效场景，确认场景已发布且启用 |
| 409 | `identity_conflict` | 核对用户映射，不用换手机号绕过已有绑定 |
| 409 | `interaction_conflict` | 同动作保持原内容；明确新动作才使用新 ID |
| 410 | `interaction_expired` / `interaction_unavailable` | 原交互已不可用，需要用户发起新动作 |
| 422 | `invalid_request` | 修正既有字段格式 |
| 429 | `rate_limited` | 按企业应用已有请求额度退避重试 |
| 503 | `service_unavailable` | 暂时故障；保留原业务 ID 与内容重试 |

OAuth 端点与 Bearer scope 错误使用标准 `{"error":"…"}` 结构及相应认证挑战，
例如 `invalid_client`、`invalid_scope`、`invalid_token`、`insufficient_scope`。
平台既有员工权限检查可能返回原有的错误提示，而非上表业务 code；同时处理 HTTP 状态。
响应 `X-Request-ID` 用于排障，和 `interaction.request_id` 的业务幂等用途不同。
本轮没有新增 `context_too_large` 或 8000 字符／128 KiB／200 行硬限制；
平台已有请求容量、模型上下文容量和权限仍适用，不承诺无限输入。

## 10. 可执行请求示例

以下示例在业务后端环境运行，需 curl 和 jq。预先设置 `BASE_URL`、`CLIENT_ID`、
`CLIENT_SECRET`、`EMPLOYEE_ID`、`USER_SUBJECT`、`USER_PHONE`、`INSTANCE_REF`、
`INTERACTION_REQUEST_ID` 和 `QUESTION`。将实际上下文保存为 `context.json`。
`INTERACTION_REQUEST_ID` 在同一动作的网络重试中保持不变。

```bash
TOKEN=$(curl --silent --show-error --fail-with-body \
  --user "$CLIENT_ID:$CLIENT_SECRET" \
  --data-urlencode 'grant_type=client_credentials' \
  --data-urlencode 'scope=employees:read auth:login' \
  "$BASE_URL/api/openapi/v1/auth/token" | jq -er '.access_token')

jq -n --arg subject "$USER_SUBJECT" --arg phone "$USER_PHONE" \
  --argjson asserted_at "$(date +%s)" \
  --arg instance_ref "$INSTANCE_REF" \
  --arg request_id "$INTERACTION_REQUEST_ID" --arg message "$QUESTION" \
  --slurpfile context context.json \
  '{user:{subject:$subject,phone:$phone,asserted_at:$asserted_at},
    instance_ref:$instance_ref,
    interaction:{request_id:$request_id,message:$message,context:$context[0]}}' \
  | curl --silent --show-error --fail-with-body \
    --header "Authorization: Bearer $TOKEN" \
    --header 'Content-Type: application/json' \
    --data-binary @- \
    "$BASE_URL/api/openapi/v1/digital-employees/$EMPLOYEE_ID/access"
```

将成功响应中的 `login_url` 返回给当前用户的浏览器打开，不将它写入普通业务日志。
对接完成后验证三件事：首次打开只有一条首问、重签重开不会重复、不同实例恢复各自会话。

## 11. 其他接口与范围

- `POST /api/openapi/v1/auth/revoke`：Basic 客户端认证，表单字段 `token`，可选
  `token_type_hint`；成功返回 200，未知或已撤销 token 同样返回 200。
- `GET /.well-known/oauth-authorization-server`：OAuth 服务发现，需要平台配置有效的 HTTPS 公开地址。
- 本轮没有新增外部会话 CRUD、聊天 REST 流式接口、任务轮询、回调、自动同步用户或业务系统专用端点。
- 原有 H5 聊天能力继续由平台自身提供；业务系统负责自己的页面、数据授权和嵌入窗口生命周期。

内部实现约束见 [OpenAPI 架构文档](../.agents/architecture/openapi.md)。
