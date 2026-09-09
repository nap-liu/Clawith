# H5 宿主上下文接入

状态：已实现并通过独立审计。目标环境是否已更新，以 `capabilities` 返回的能力为准。

目标：用户直接在原 H5 输入框提问，每条消息自动携带发送时的宿主页面快照。
页面切换不重载 iframe、不重建会话、不自动提问。

## 1. OpenAPI 增量

既有 OAuth、用户委托、员工与场景权限不变。完整基础合同见
[OpenAPI 接入指南](openapi-integration.md)。

能力发现 `GET /api/openapi/v1/capabilities` 新增：

```json
{
  "h5_launcher": {
    "host_context": {"supported": true, "version": 1}
  }
}
```

在既有 `POST /api/openapi/v1/digital-employees/{employee_id}/access` 中开启：

```json
{
  "user": {"subject": "business-user-42", "phone": "+8613800000000", "asserted_at": 1788926400},
  "instance_ref": "global-assistant-window",
  "embed_origin": "https://business.example.com",
  "scene_key": "business-analysis",
  "host_context": {"enabled": true, "version": 1}
}
```

`asserted_at` 必须使用实际请求时的 Unix 秒时间戳。`scene_key` 可省略。
启用 `host_context` 时需提供 `instance_ref` 和 `embed_origin`；后者用于浏览器精确投递。
仍使用 `employees:read`、`auth:login`，不增加专用 scope。

响应保留原员工信息、`login_url`、`expires_in`，追加：

```json
{
  "host_context": {"version": 1, "frame_origin": "https://chat.example.com"}
}
```

以平台返回的 `frame_origin` 校验 H5 消息，不能用 API 地址或登录地址猜测。
设置 iframe 的 `src` 为返回的完整 `login_url`，不要自行拼接 H5 地址。
未开启或省略此配置时，原 H5 行为不变。

本配置只影响当前 iframe 的打开流程，不写入全局用户配置或改变其他普通 H5 窗口。
登录后配置由平台内部交给 H5，调用方无需管理额外 token。

## 2. 最小浏览器通信协议

H5 在发送前向其 `parent` 投递：

```json
{
  "type": "digital_employee.context.request",
  "version": 1,
  "request_id": "99016cc3-0f0b-4f29-b8cb-fdbe78fa3837",
  "attempt_id": "b63b352f-b3f3-4da8-81ad-1c939717dc46"
}
```

宿主返回当前用户有权访问的快照：

```json
{
  "type": "digital_employee.context.response",
  "version": 1,
  "request_id": "99016cc3-0f0b-4f29-b8cb-fdbe78fa3837",
  "attempt_id": "b63b352f-b3f3-4da8-81ad-1c939717dc46",
  "status": "ready",
  "context": {
    "captured_at": "2026-09-09T04:00:00Z",
    "source": {"system": "business", "label": "营业额看板"},
    "location": {"table_id": "33", "view_id": "67"},
    "selection": {"record_ids": ["1", "2"]},
    "coverage": "selected_records",
    "columns": [{"field_id": "148", "name": "营业额", "unit": "元"}],
    "rows": [{"record_id": "1", "营业额": 128000}, {"record_id": "2", "营业额": 96000}]
  }
}
```

`context` 为任意 JSON，上例不是必填业务 schema。保留字段类型、真实 ID、捕获时间和数据范围。
没有选中记录时，可以返回页面位置及真实覆盖范围，不要求整表数据。

获取失败：

```json
{
  "type": "digital_employee.context.response",
  "version": 1,
  "request_id": "99016cc3-0f0b-4f29-b8cb-fdbe78fa3837",
  "attempt_id": "b63b352f-b3f3-4da8-81ad-1c939717dc46",
  "status": "unavailable",
  "error": {"code": "context_unavailable"}
}
```

宿主必须确认 `event.source === iframe.contentWindow`，且 `event.origin` 与
`frame_origin` 精确一致。回复该窗口时使用这个精确 origin。H5 同样校验 parent 与
access 中的 `embed_origin`。双方均不使用 `*`。
这些检查用于防止浏览器窗口串消息，不增加系统 API 的调用域名限制。

`request_id` 固定本条消息与快照；`attempt_id` 只标识一次浏览器通信，每次重试更新。
回复必须原样回显两者，避免超时后的旧回复误配新等待。业务快照服务只使用 request_id
固定原页面状态，不把 attempt_id 当成新的问题或快照。

## 3. 可选轻量 SDK

系统自动托管原生 ES module：`/sdk/h5-host-context.js`，以及配套声明 `/sdk/h5-host-context.d.ts`。
可直接使用浏览器协议，也可使用这个通信封装；不需要 npm 发布或另建聊天客户端。

```js
import { createHostContextBridge } from 'https://chat.example.com/sdk/h5-host-context.js';

const bridge = createHostContextBridge({
  iframe: document.querySelector('#employee-chat'),
  frameOrigin: accessResult.host_context.frame_origin,
  getContext: async ({ requestId, signal }) => {
    // 接入你现有的授权快照服务；同 requestId 固定首次页面状态。
    return getFixedAuthorizedSnapshot({ requestId, signal });
  },
});

// 登录入口仍使用 OpenAPI 返回值。
document.querySelector('#employee-chat').src = accessResult.login_url;

// 关闭、替换 iframe 或切换业务身份时清理。
bridge.dispose();
```

示例 `getFixedAuthorizedSnapshot` 由业务系统实现，不是 SDK 的数据接口。
它必须在首次收到 requestId 时固定页面引用和选择；同 ID 重试仍使用该状态，通过
业务后端原有权限读取数据。不能在重试时改用已切换的新页面。

SDK 只封装来源校验、并发请求合并、成功/失败回复和销毁取消；不持有客户端密钥，
不调用业务 API，不管理窗口布局，不自动发送问题，也不持续同步页面状态。
将示例中的 `https://chat.example.com` 替换为实际平台地址即可直接跨域导入；
系统的公开 `/sdk/` 静态路径已允许跨域加载，并随系统版本更新，无需另行托管。
也可以将模块纳入宿主自身构建。静态模块可跨域读取，窗口通信仍须精确校验来源。

## 4. 发送与重试规则

- H5 为每次明确的新提问生成一个消息 UUID，同时用作快照 request_id。
- 取得快照前保留输入和附件；取得后随原 WebSocket 消息一起发送。
- 获取失败或超时不发送缺失快照的消息；原输入框的发送操作可重试同一动作。
- 相同动作重试保持原 ID、问题、附件和已固定快照；用户编辑草稿后再次发送是新动作。
- 断线重连不重新读取当前页面来替换已经取得的快照。
- 已发出但未收到持久化回执的消息保留完整发送内容，不能被后续输入覆盖；重连按原 ID 对账或重发。
- 相同消息 ID、相同内容只落一条消息；更换问题、附件或快照返回 `message_conflict`。
- 取消、会话/身份切换、关闭窗口后忽略迟到响应；多个 iframe 按各自窗口分别匹配。
- STOP 同时取消尚未发出的快照等待，防止终止后迟到回包启动新问题。
- STOP 后，当前会话中尚未确认的发送只与历史对账，不在重连时自动补发。
- 明确拒绝的消息退出自动重试；草稿未被编辑时恢复原问题和附件。
- 生成中的追加消息仍走原排队/接收流程，获取下一条快照不打断当前回答。
- 场景配置与快照同时生效，停止、确认、附件及历史浏览沿用原有能力。

`/new`、`/reset`、`/continue` 沿用原会话控制流程，不请求宿主快照。

原 `interaction` 首问继续使用其明确提供的快照，不再请求宿主；后续普通提问仍获取
新的宿主快照。开启本模式的普通打开不触发自动开场消息。

## 5. 错误和数据边界

OpenAPI 启用配置缺少实例、来源或版本无效时返回 400 `invalid_host_context`。
浏览器获取失败/超时在原聊天区域给出本地化错误，不显示任意宿主 HTML。
WebSocket 冲突沿现有错误事件返回 `message_conflict` 和关联消息 ID。

快照属于用户级参考资料，不是系统指令或权限凭据。平台继续执行已有员工、会话、模型、
工具和审批规则。不要在 context 或 postMessage 中放置 OpenAPI 密钥、登录 token 或数据库凭据。
不增加字数、字节数或行数专用限制，继续遵守已有请求和模型容量。

机器可读接口定义见 [OpenAPI JSON](openapi-v1.json)。接入前先检查目标环境的能力发现响应。
