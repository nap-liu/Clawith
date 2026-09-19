# 身份、目录与通道绑定架构

本文定义平台人员主体、多 SSO 登录、SCIM/厂商目录同步、组织架构和 IM
通道绑定的稳定边界。涉及 `Identity`、`User`、`IdentityProvider`、
`OrgMember`、`OrgDepartment`、`ChannelUserBinding` 或组织同步的任务，必须先
阅读本文。

## 1. 目标与基本原则

平台以 SCIM User/Group/membership 语义作为统一用户与组织架构基石，外部系统
只是身份、目录或通道信息的适配来源。

1. `PlatformUser` 是全平台唯一人员锚点。当前兼容模型由 `Identity` 承担这个
   物理身份；手机号在全平台唯一，并保存全局启停状态。
2. `TenantUserMembership` 表示 PlatformUser 与租户的关系。当前兼容模型由
   `User` 承担，保存 tenant、角色、租户资料和租户级启停状态。
3. 一个租户可以同时启用多个 SSO 登录方式。每个登录方式对应一个独立的
   provider connection，并可带有自己独立的目录同步能力和组织架构。
4. 同一个 TenantUserMembership 可以关联多个目录来源账号；同一个 PlatformUser
   也可以通过不同 membership 属于多个租户。跨租户只共享全局人员锚点，不共享
   组织、角色、权限、来源账号或通道绑定。
5. SCIM 是平台内部统一目录领域协议，不是只有 SCIM provider 才具备的特性。
   钉钉、飞书、企微和其他 provider 都必须适配为同一种 SCIM User、Group、
   membership、active 和 enterprise extension 规范化快照。
6. OAuth 2.0/OIDC 是登录协议，SCIM 2.0 是目录协议；对于标准身份源，它们是同一个
   ProviderConnection 的固定组合入口，由一条连接共同承载登录和目录同步。两种协议
   可以共享安全凭据配置，但不能共享或混淆 subject 命名空间。
7. 钉钉、飞书、企微等 IM 身份是租户成员在特定安装范围内的补充路由信息，
   不单独定义一个人。unionid、openid、staff_id 等只属于对应通道和安装范围。
8. 姓名、昵称、头像、职位和部门名称不是人员归一证据。
9. 所有来源 ID 都视为 opaque string。禁止推断其格式，禁止把 SCIM id 冒充
   钉钉 unionid，也禁止截断或跨 provider 比较来源 ID。

## 2. 事实模型

目标关系如下：

```text
Tenant
  ├─ ProviderConnection A (登录能力 + 目录能力)
  │    ├─ DirectoryGroup graph
  │    └─ DirectoryAccount ───────┐
  ├─ ProviderConnection B         │
  │    ├─ DirectoryGroup graph    ├──> TenantUserMembership ──> PlatformUser
  │    └─ DirectoryAccount ───────┘               │
  └─ ChannelSubject / Binding ────────────────────┘
```

### 2.1 PlatformUser 与 TenantUserMembership

- `PlatformUser` 是跨租户稳定锚点，保存规范化手机号、邮箱、登录凭据和
  `is_active`。当前数据库中的对应实体是 `Identity`。
- 手机号和邮箱是首次归一时可配置顺序的匹配属性，不是外部账号的稳定主键；二者都会
  随人员资料变化。唯一性必须基于规范化值，而不只是原始字符串唯一，数据库约束与
  应用规范化必须使用同一规则。
- `TenantUserMembership` 表示一个 PlatformUser 在某租户中的成员身份；同一个
  PlatformUser 可以有多个 tenant membership，同一 `(tenant_id, platform_user_id)`
  最多一条。当前数据库中的对应实体是 `User`。
- tenant membership 保存租户角色、显示资料、配额和 `is_active`；任何租户业务
  权限都通过 membership 判断，不能仅凭 PlatformUser 身份跨租户访问。
- 可信目录创建人员时必须同时得到 PlatformUser 锚点与当前租户 membership；不再
  长期创建没有全局锚点的 tenant User。
- 未经验证的 IM sender subject 或只有历史缓存、没有 fresh claim 的兼容记录不得
  直接成为新的 PlatformUser；它们可在迁移期保留 identityless tenant User，直到
  可信目录/登录证据补齐。完整可信目录快照中的账号即使暂无手机号和邮箱，也要创建
  opaque PlatformUser 锚点，后续再按 provider policy 补齐联系方式。

状态必须分层：

- `PlatformUser.is_active`：全平台账号是否启用；禁用后所有租户均不可登录；
- `TenantUserMembership.is_active`：该用户在某租户是否启用，不影响其他租户；
- `DirectoryAccount.status/active`：某个来源账号的独立状态事实；成功同步后只对本次
  provider 影响到的成员重新计算当前租户的 `TenantUserMembership.is_active`：同租户
  任一已启用 provider 的已绑定来源账号为 active 时，租户成员保持启用；没有任何
  active 来源时才停用。该聚合必须与 provider 同步顺序无关，且不得修改其他租户或
  `PlatformUser.is_active`；
- provider、tenant 或来源账号 inactive 时，统一授权取相关状态的交集。
- `TenantUserMembership.is_login_suspended` 表示租户管理员明确暂停登录，独立于
  来源账号聚合结果；目录同步和登录归一不得清除此状态。所有登录入口共享同一认证
  主体编排：已有会话、委托凭据和临时登录链接只执行只读门禁；只有通过完整凭据校验
  的密码登录或新鲜可信 provider 登录，才可把当前精确来源账号标记为 active，再按
  全部启用 provider 的来源状态重算 `TenantUserMembership.is_active`。新来源恢复不得
  修改其他 provider 的 deleted/inactive 事实，也不得绕过 PlatformUser、tenant 或
  `is_login_suspended` 的停用状态。
- 已验证签名的 provider 入站事件若同时通过 provider 用户详情接口确认该账号仍存在，
  这是比旧同步快照更新的 active 事实；共享解析器必须先恢复该 DirectoryAccount 和
  当前 tenant membership，再执行绑定校验。详情接口失败不得被解释为 active，也不得
  绕过已停用的 PlatformUser。

### 2.2 ProviderConnection

当前代码中的 `IdentityProvider` 是 provider connection 的兼容实现。目标上一个
connection 应显式声明能力，而不是只靠 `provider_type` 推导全部行为：

- `login_protocol`: `oauth2`、`oidc`、`dingtalk`、`feishu` 等；
- `directory_protocol`: `scim`、`dingtalk`、`feishu`、`wecom` 等；
- `channel_protocols`: 此 connection 可接收或发送的 IM 通道；
- 服务端凭据配置：沿用现有加密/脱敏配置机制；独立 secret 引用属于后续安全迁移，
  不作为第一轮目录统一的前置条件；
- `identity_match_policy`: 此来源的人员归一策略；
- `sync_policy`: 调度、范围、分页、安全上限和停用策略。

一个 connection 可以只有登录能力、只有目录能力，或同时具备两者。登录和目录
共享凭据时复用同一份服务端配置，不得在新表、同步 run 或前端响应中复制明文
secret。

标准身份源是上述通用能力模型中的固定组合：一个 ProviderConnection 同时声明
OAuth 2.0/OIDC `login_protocol` 与 SCIM 2.0 `directory_protocol`，其产品入口、配置、
启停、审计和同步 run 都归属于同一条连接。这与钉钉、飞书、企微和 Google 等单个
连接同时承载登录与目录能力的模型一致。前端不得把“OAuth/OIDC 登录”和“SCIM
目录”拆成两个新增 provider，也不得要求管理员分别创建后再人工关联。后端可以继续
读取、运行和迁移历史 `provider_type=scim` 等 standalone SCIM 数据，但该兼容形态
不是新建连接的产品模型。

### 2.3 DirectoryAccount

当前 `OrgMember` 是 DirectoryAccount 的兼容实现。目标语义为“某目录源中的一个
账号/人员记录”，至少包含：

- `tenant_id`、`provider_id`、`external_id`；
- `tenant_user_id`，可空，表示归一后关联的 TenantUserMembership；通过 membership
  可稳定到唯一 PlatformUser；
- 来源展示属性，如姓名、邮箱、手机号、职位、头像和 active 状态；
- `link_status`、`matched_by`、`linked_at` 和最后观察时间；
- 来源原始版本或 ETag 的安全摘要，不持久化不必要的敏感原始响应。

必须满足：

- `(provider_id, external_id)` 在非空时唯一；
- 同一 TenantUserMembership 可以有多个 provider 的 DirectoryAccount；
- 一个 DirectoryAccount 同时只能绑定一个 TenantUserMembership，且 tenant 必须一致；
- 来源状态 inactive、来源缺失 tombstone、租户成员禁用和 PlatformUser 禁用是
  四个不同状态。

### 2.4 DirectoryGroup 与组织图

当前 `OrgDepartment` 是 DirectoryGroup 的兼容实现。平台统一按 SCIM Group 图
建模；无论上游是标准 SCIM、钉钉、飞书还是企微，都不能被限制为单父部门树。

目标需要两个关系表：

- `directory_group_edges(parent_group_id, child_group_id)`：保存 Group 嵌套；
- `directory_account_groups(account_id, group_id, is_primary)`：保存人员多 Group
  归属。

约束如下：

- 所有关联必须属于同一 tenant 和 provider；
- Group 图允许多个根和多父节点；同步写入前必须检测循环；
- 统一快照 `Group.members` 中的 User 与 Group 引用分别落入上述两类关系；
- 没有 Group 的用户仍然同步，数据库保存“零 membership”。“未分组”是查询层
  的虚拟视图，不伪造一个上游 Group；
- `OrgDepartment.parent_id`、`OrgMember.department_id` 和 `department_path` 只作为
  迁移期的主路径兼容投影，不能再作为完整事实来源；
- 当图存在多父节点时，兼容主路径按稳定规则选择，但 API 必须同时返回完整
  membership/edge 信息。

每个具备目录能力的 provider 都使用同一份企业根映射配置
`directory.root_mapping.root_name`，其默认值是当前租户企业名称。适配器必须先保留并
解析上游真实根：钉钉读取根部门详情取得企业名称，SCIM 使用 Group 图的结构根，其他
provider 使用各自协议的结构根。统一内核随后执行以下最小归一规则：

- 上游恰好一个结构根时，保留它的 provider-scoped external ID，仅把展示名称映射为
  平台企业根名称；
- 上游有多个结构根或没有唯一业务根时，在规范化快照中增加一个 external ID 稳定的
  平台企业根，并把各上游根挂到该节点；
- 技术占位名称 `Root` 不是业务组织，不得由新同步写入或展示；历史钉钉/SCIM `Root`
  仅作为读取兼容被隐藏，其有效子节点提升到标准树顶层，直到下一次成功同步修正；
- 根映射只改变当前 provider 在当前租户下的组织投影，不改变来源 ID、人员归一证据、
  其他租户或其他 provider 的同步事实。

### 2.5 ChannelSubject 与 ChannelUserBinding

通道 subject 的完整身份至少由以下维度组成：

```text
(tenant_id, provider_id, installation_scope, channel_type, id_type, subject)
```

- `ChannelUserBinding` 只表示一个通道 subject 已绑定到哪个 tenant membership；
  通过 membership 才能关联唯一 PlatformUser。
- 企业目录 ProviderConnection 属于租户级上游身份边界，可以被同一企业下的多个机器人
  安装共享；机器人或应用安装范围只写入 `ChannelUserBinding.installation_scope`，不得反向
  占用或改写租户级 ProviderConnection。没有目录能力的纯通道 provider 仍按安装隔离。
  同一企业的稳定 provider 用户 ID 必须跨所有机器人复用，不以接收入站事件的 Agent 或
  机器人凭据划分人员。用户资料统一通过 ProviderConnection 配置的企业应用查询，不依赖
  入站机器人的通讯录权限；同租户存在多个同类型企业 provider 时，通道必须显式选择
  ProviderConnection，禁止按机器人安装范围猜测。
- 每个通道适配器都必须输出同一标准 subject 集合：`provider_id`、
  `installation_scope`、`channel_type`、`id_type`、`subject`。适配器只处理原始字段语义，
  绑定、目录匹配、联系方式匹配和自动注册必须经过共享解析器。
- 未归一的 sender 不能为了消息投递自动制造新的全局人员身份；共享解析器
  可以创建 identityless tenant User 以保留现有通道自动注册能力，待可信目录或登录
  证据补齐后再归一到 PlatformUser。
- 适配器暂时只能取得弱 subject 时，必须按安装范围绑定；后续同一事件或回调
  取得更强 subject 时，由共享解析器补齐绑定并收敛到原 tenant User。例如钉钉
  缺少 `senderStaffId` 时的 opaque `senderId` 只是 `sender_id` 绑定，不得写成目录
  `external_id`；后续获得 staff ID 时补齐 `staff_id`/`union_id` 而不新建用户。
- 共享解析器必须先查当前精确安装范围；仅当精确范围未命中时，才可兼容查询同一
  tenant、同一 ProviderConnection 下由已归一事件学习的 provider-scoped alias，或旧
  installation scope 中的 provider 稳定 subject。机器人凭据或应用 ID 轮换不能把同一
  上游账号注册为第二人。相同 subject 在历史 scope 中必须唯一指向一个 tenant User，
  否则拒绝自动选择并进入修复；存在冲突标记的 alias 禁止参与回退。命中后必须为当前
  安装范围补齐绑定。该规则适用于所有 provider，不在通道适配器中复制实现。
- unionid、openid、staff_id、chat_id 等不能写入 SCIM DirectoryAccount 的 ID
  字段；它们以多个绑定或通道属性存在。
- 已绑定通道必须可从 PlatformUser 和当前 tenant membership 查询，界面显示
  provider、channel、安装范围、id_type、状态和最后观察时间，但不展示敏感
  subject 全值。

## 3. 可配置人员归一策略

每个 provider connection 都必须保存有版本的 `identity_match_policy`。`ordered_fields`
必须是非空、去重、按用户配置顺序排列的受支持字段列表；管理员可以动态增加、删除和
调整字段顺序，不要求固定包含手机号和邮箱。默认策略为：

```json
{
  "version": 1,
  "ordered_fields": ["phone", "email"],
  "match_mode": "normalized_exact",
  "on_lower_priority_conflict": "bind_highest_priority_and_flag",
  "allow_name_match": false
}
```

### 3.1 匹配顺序

对每条可信来源记录执行：

1. 先用完整 provider-scoped 来源键查找既有绑定：目录使用
   `(tenant_id, provider_id, external_id)`，登录和 IM 使用各自明确的
   `(installation_scope, channel_type, id_type, subject)` 命名空间。Provider 返回的
   用户 ID 是该来源内的稳定主键；已有绑定不因手机号或邮箱变化而漂移。
2. 未绑定时，严格按照 `ordered_fields` 顺序逐项匹配。默认列表是手机号、邮箱；
   provider 可以动态增删受支持字段并调整顺序。保存和执行前必须拒绝空列表、重复项
   和不受支持字段；核心代码不得写死优先级，也不得强制策略始终包含 phone 或 email。
3. 手机号执行统一 E.164/国家区号规范化后精确匹配；在规范化规则完成迁移前，
   至少保持现有去空格、连接符和前导 `+` 的兼容行为。禁止尾号、模糊或前缀匹配。
4. 邮箱 trim、lowercase 后精确匹配；占位域名不是可信证据。
5. 命中最高优先级字段后即得到候选 PlatformUser，再获取或创建当前租户的
   TenantUserMembership。低优先级字段若指向另一个 PlatformUser，保留高优先级
   结果并创建冲突审计，不合并两个已有 PlatformUser，也不覆盖对方联系方式。
6. 没有任何命中时，可信目录可以创建新的 PlatformUser 与当前租户 membership；
   IM 通道通过共享解析器自动创建 identityless tenant User 和精确 ChannelSubject
   绑定，不独立创建标准全局人员主体。

### 3.2 可信度与更新

- 只有当前认证成功的目录响应或登录响应可作为本次 fresh claims。
- 数据库中历史 OrgMember 联系方式是展示缓存，不自动升级为 fresh claims。
- 只有尚未绑定稳定来源 ID 的首次接入才执行手机号/邮箱匹配策略。已有精确来源绑定时，
  始终命中原 PlatformUser，并把 fresh claims 中未被其他身份占用的最新手机号、邮箱
  自动更新到该身份；不得重新识别人、创建第二个 User 或因联系方式变化拒绝登录。
- 新联系方式已被另一个 PlatformUser 占用时，保留稳定来源绑定并只把真正冲突的字段
  进入 `review_required`；其他未冲突字段仍自动刷新。同步和登录不得按联系方式把来源
  账号静默改绑到另一个 PlatformUser。
- 每次决策记录 policy version、matched field、候选数、冲突类型和 source account；
  日志与 API 不记录明文 secret，也不输出完整敏感标识。

### 3.3 冲突人工处理

- 冲突处理只能由租户管理员在当前 tenant 和 provider 范围内执行；平台管理员必须显式
  选择 tenant。接口从当前 `OrgMember`、租户 `User` 及其 `Identity` 动态读取双方字段供
  管理员核对，审计日志只保存脱敏快照、证据摘要和租户内可复核的引用，不保存明文
  联系方式。
- 只有来源账号、当前绑定、最高优先级候选和当前证据仍与冲突快照一致时才允许修复。
  证据已变化或历史记录缺少证据时必须重新同步，不得按过期记录改绑定。
- “重绑到最高优先级候选”只移动该 DirectoryAccount、其精确 ChannelSubject 绑定和
  明确指向该 DirectoryAccount 的关系；不合并或删除 PlatformUser。
- “合并用户”必须由租户管理员显式选择当前租户内的保留用户。操作在单个事务中把
  冲突涉及用户的全部 DirectoryAccount、ChannelUserBinding 和 AgentRelationship
  归一到保留用户，重复关系按 `(agent_id, user_id)` 去重，并停用当前租户内被合并的
  TenantUserMembership。被停用记录、其 PlatformIdentity 及历史业务外键继续保留，
  不删除全局身份、不改其他租户 membership；合并结果和各类迁移数量必须进入审计。
- 合并时手机号和邮箱独立选择。候选必须覆盖冲突涉及的所有租户用户字段值，以及当前
  DirectoryAccount 中经过本次证据校验、尚未归属其他身份的真实字段值；不得因为来源值
  暂无租户用户关系而从选项中隐藏。已被本次合并范围外身份占用的值不得迁移。
- 冲突界面必须列出当前字段值、其指向用户和所有可保留候选，提供“合并用户”“仅重绑
  当前来源”“确认人员保持分离”三种真实可执行操作。历史审计仅在当前 provider 下恰好
  一个 DirectoryAccount 能按当前策略重现同一冲突时动态恢复操作证据；无法唯一复核时
  继续拒绝变更，不能按模糊猜测合并。
- “确认人员保持分离”以 `(tenant, provider, source account, evidence digest)` 保存持久
  决策。相同证据不重复产生冲突；任一来源字段或候选归属变化时重新进入冲突队列。

## 4. SCIM 领域内核与适配器协议

平台内部唯一目录模型直接采用 SCIM 语义：User、Group、Group.members、active、
enterprise extension 和 provider-scoped resource id。这个模型适用于所有目录来源，
不是标准 SCIM transport 的专属能力。

所有目录适配器实现统一接口：

```text
discover_capabilities()
fetch_snapshot_or_changes(cursor)
validate_snapshot()
normalize_groups()
normalize_accounts()
```

适配器的输出必须是统一 `ScimDirectorySnapshot`，其中包含 Users、Groups 和 typed
memberships。适配器只负责远端协议和字段映射，不负责创建 PlatformUser、合并
身份、写业务权限或发送消息。统一 SCIM 领域内核负责校验、持久化、归一、
reconcile 和审计。

现有厂商 SDK 可以在 transport 内部继续产生 `ExternalDepartment/ExternalUser`
兼容记录，但统一 lifecycle 必须先把整批记录组装为 `ScimDirectorySnapshot`，完成
ID、引用和 Group 环校验后才允许写目录事实；这些兼容记录不能越过快照边界直接
触发 reconcile。

厂商调用预算属于 adapter 边界：钉钉组织接口有配额/限流，开发与回归默认只使用
固定夹具、mock 和既有只读快照，不得把每次代码验证都变成真实全量钉钉同步。自有
OAuth 与 dev SCIM 可在隔离测试租户内反复联调，但同样必须使用有界分页和请求预算。

### 4.1 原生 SCIM 2.0 transport 适配器

原生标准身份源在产品和前端中以一条“OAuth 2.0/OIDC 登录 + SCIM 2.0 目录”的
ProviderConnection 创建和管理；SCIM transport 是这条连接的目录能力，不是第二个
需要单独新增的 provider。后端对历史 standalone SCIM 记录保留兼容解析和迁移能力。

SCIM 实现遵守 RFC 7643/7644 的资源和分页语义：

- 首先读取 `ServiceProviderConfig`、`ResourceTypes` 和 `Schemas`，校验服务能力；
- 分页读取 `/Users` 和 `/Groups`，使用 `startIndex`、`count`、`itemsPerPage` 和
  `totalResults`，不能假设服务接受请求的 page size；
- User `id` 和 Group `id` 作为 provider-scoped opaque external ID；
- 支持 core User、enterprise User extension、Group.members 的 User/Group 引用；
- 未声明的扩展字段必须经过配置映射，不能靠字段名猜测；
- 不从 `organization` 字符串反推 Group ID；Group.members 才是成员关系事实；
- 支持 active/inactive 用户、零 Group 用户、多个 Group、多个根和 Group 嵌套；
- 校验 ID 唯一、引用完整、分页完整、响应 schema、资源上限和 Group 环无循环；
- 远端快照在分页中变化、引用悬空或响应不完整时，整次同步失败且不得 reconcile；
- Basic/OAuth client 凭据仅从服务端 provider 配置读取，不进入前端、不写日志或
  同步运行记录；后续可在不改变适配器接口的情况下迁移为 secret 引用。

内部 SCIM 当前必需兼容的最小字段集是：

- User: `id`、`userName`、`displayName`、`active`、`emails`、`phoneNumbers`；
- EnterpriseUser: `employeeNumber`、`organization`；
- Group: `id`、`displayName`、`members`，member type 支持 User 和 Group。

这些字段不能被解释为任何 IM 厂商标识。若未来服务增加扩展 schema，必须通过
schema URI 和配置映射显式接入。

### 4.2 厂商 transport 适配器

钉钉、飞书、企微适配器继续保留各自的授权范围、分页、限流和 ID 语义，但必须
转换成与原生 SCIM 相同的 User、Group 和 typed membership 快照。多根组织、
多 Group 归属和 Group 嵌套是统一领域能力，所有 provider 都必须支持表达，不能
只在 SCIM adapter 中实现。适配器必须：

- 使用明确超时、有界重试、Retry-After 和全局请求预算；
- 不在部门循环中重复写同一人员；
- 完整保留多部门归属；
- 区分“无权限看见”“来源已删除”“来源 inactive”；
- 禁止在授权范围接口失败时静默扩大到根部门并执行删除 reconcile。

## 5. 同步状态机与一致性

每个 provider connection 独立调度和写入，不允许一个来源的快照删除另一个来源
的账号或组织。

### 5.1 调度与手动触发

每个具备 directory capability 的 provider 都使用同一份最小调度配置：

```json
{
  "enabled": true,
  "interval_value": 1,
  "interval_unit": "day"
}
```

- `interval_unit` 只允许 `hour`、`day`、`week`、`month`，`interval_value` 必须为
  正整数并设置合理上限；核心调度器不得按 provider 类型分叉。
- 每个 provider 持久化 `next_sync_at`。小时/天/周按 UTC 时长计算；月按 UTC
  日历月计算，目标月没有同一日时取该月最后一天。
- 自动调度和手动触发必须进入同一个 `request -> run -> execute` 服务。手动触发
  不修改下一次自动执行时间；关闭自动调度也不禁止管理员手动触发。
- 同一 provider 同时最多一个 `pending/running` run；重复触发返回已有 run，避免
  重复拉取和并发 reconcile。
- scheduler 只领取到期 provider 并创建 run，实际同步在独立短任务中执行；领取时
  使用数据库条件更新/锁，保证多副本部署不重复执行。

第一轮进度采用持久化 run + HTTP 轮询，不为此另建消息队列或 WebSocket 状态机。
API 至少返回 `status`、`stage`、`processed_items`、`total_items`、`progress_percent`、
有限统计、错误摘要和开始/结束时间。进度只能单调递增，未知总量时百分比可为空。

### 5.2 同步步骤

1. 获取 `(tenant_id, provider_id)` 单实例锁；并发请求返回已有 run 状态。
2. 在没有数据库长事务的情况下读取远端分页数据。
3. 在内存或 staging 表中构建完整快照并做结构校验。
4. 更新触发时已经创建的 `directory_sync_run`，记录来源、模式、阶段、开始时间、
   能力摘要和预期数量。
5. 短事务批量 upsert Group、Group edge、DirectoryAccount 和 account membership。
6. 对需要归一的 account 使用小事务和同一 subject 锁调用统一用户归一服务。
7. 只有完整快照全部成功时才 reconcile 本 provider 未出现的记录。
8. 提交成功后更新 `last_success_at`、cursor/checksum 和结果数量。

### 5.3 状态与审计

`directory_sync_run.stage` 至少区分 `fetching`、`validating`、`applying`；
`directory_sync_run.status` 至少支持：

- `pending`、`running`、`succeeded`；
- `partial_failed`、`failed`、`cancelled`、`needs_review`。

`last_attempt_at` 与 `last_success_at` 必须分开。部分失败、跳过部门、分页不完整或
身份冲突不能更新最后成功时间。每次 run 保存：

- 远端/规范化/新增/更新/inactive/tombstone 数量；
- Group、edge、account、membership 数量；
- 新建 PlatformUser、已有 PlatformUser 绑定、未绑定、冲突数量；
- 错误分类和有限样例，不保存 secret 或大段 PII。

### 5.4 删除与停用

- 来源 `active=false` 必须忠实更新 DirectoryAccount 来源状态，并按 2.1 的确定性聚合
  规则更新 tenant membership；单一来源永远不能禁用全局 PlatformUser。
- 完整快照中缺失的账号标记为 source tombstone；只有当所有权威来源均失效且满足
  tenant policy 时，才允许禁用相应 tenant membership；全局账号停用必须走独立
  平台管理流程。
- 删除 provider connection 默认软停用并保留来源、绑定和同步审计。物理删除必须
  有专门迁移和可恢复备份，不能先把 provider_id 置空而丢失来源。

## 6. 查询与产品表现

### 6.1 管理 API

需要提供以下稳定查询：

- provider connection 列表及 login/directory/channel capabilities；
- 每个 provider 的同步状态、最近 runs、可安全展示的配置摘要；
- 按 provider 查询完整 Group 图、账号和 membership；
- 按 PlatformUser/tenant membership 查询全部 `directory_sources[]` 和
  `channel_bindings[]`；
- 按 Group 查询成员时基于 membership 表，并支持是否递归包含子 Group；
- 冲突队列和人工确认/解绑操作，全部带租户权限和审计。

API 不应在跨 provider 用户列表中丢弃来源。可以返回一个首选展示 profile，但必须
同时返回全部来源摘要；首选 profile 策略也应配置且可解释。

### 6.2 管理界面

- 同时展示所有已启用的 SSO 登录方式，不以 provider type 的 `find()` 隐藏连接；
- 每个登录方式单独显示目录能力、同步按钮、run 状态和自己的组织浏览器；
- 标准身份源只提供一个组合新增入口，一次创建同一 ProviderConnection 的
  OAuth 2.0/OIDC 登录与 SCIM 2.0 目录配置；不得拆成两个 provider 表单或两条连接；
- 用户详情显示全局状态、当前租户成员状态、“来源”标记和“已绑定通道”标记；
- 通用组织浏览器和人员选择器按平台内部 SCIM Group/User 统一视角呈现；组织树只
  显示业务组织名称与层级，不显示 provider 来源，也不暴露适配器生成的技术 Root；
- 平台标准组织树优先读取租户已启用的 SCIM 目录投影，不把各 provider 原始树直接
  拼接为一棵树；没有标准 SCIM 投影的迁移期租户才读取归一后的 provider 树；
- 组织树只返回包含有效成员或包含非空后代的节点，空分支不进入浏览器或人员选择器；
  有子节点的条目必须支持展开和收起；
- 通用人员选择器和 provider 目录浏览器必须按根节点、直属子节点懒加载，默认收起；
  空分支判断和直属成员数量读取成功同步持久化的递归 `member_count`，不得在每次展开
  节点时递归扫描整个租户目录；搜索必须有明确结果上限；
- 人员条目用轻量标签展示已关联 provider 的产品名称，来源与通道合并去重，不显示
  provider type、subject、安装标识或内部 UUID；
- provider 目录浏览和通用人员选择器统一支持按姓名、手机号、邮箱精确或模糊搜索；
  搜索在数据库内执行、精确命中优先，并保持租户范围和结果上限；
- 同一用户在不同来源的职位、部门等并列展示，不静默覆盖；
- 多根组织和 Group 图使用 forest/DAG 视图；无 Group 用户显示在计算得到的“未分组”
  视图中；
- secret 字段只支持“已配置/替换”，后端响应永不回传明文。

## 7. 配置结构

推荐的 provider connection 配置示意如下。字段是平台通用命名，不硬编码租户、
域名或厂商应用名：

```json
{
  "capabilities": {
    "login_protocol": "oauth2",
    "directory_protocol": "scim",
    "channel_protocols": []
  },
  "credential_ref": "secret://provider-credential-id",
  "login": {
    "authorize_url": "<configured>",
    "token_url": "<configured>",
    "userinfo_url": "<configured>",
    "scopes": ["openid", "profile", "email"]
  },
  "directory": {
    "base_url": "<configured>",
    "page_size": 500,
    "max_resources": 200000,
    "field_mapping": {},
    "root_mapping": {
      "root_name": "<tenant enterprise name>"
    }
  },
  "identity_match_policy": {
    "version": 1,
    "ordered_fields": ["phone", "email"],
    "match_mode": "normalized_exact",
    "on_lower_priority_conflict": "bind_highest_priority_and_flag"
  },
  "sync_policy": {
    "enabled": true,
    "interval_value": 1,
    "interval_unit": "day"
  }
}
```

环境 URL 和 secret 只进入部署配置或安全存储；本文和产品默认值不得写死生产或
测试域名。

字段映射属于 provider connection 配置。平台目标字段集合固定，管理员配置来源
字段路径；运行时仅在缺少覆盖值时使用对应协议的标准默认路径。OAuth/OIDC 登录映射
与 SCIM 目录映射使用独立命名空间，不能相互覆盖。SCIM 的 `schemas`、资源 `id`、
`active` 和 Group 成员引用是协议及绑定锚点，不开放映射；姓名、邮箱、手机号、头像、
职位、员工编号、组织、事业部和部门等资料字段允许配置 RFC 6901 JSON Pointer，并在进入
平台归一化链路前完成解析。OAuth/OIDC 使用无前导斜杠的完整点路径，保留非标准响应
包装层，例如 `data.userId`；SCIM 使用标准属性 JSON Pointer，头像默认映射到标准
`/photos` 多值属性。头像只更新目录账号与租户展示资料，不作为人员归一证据。

字段映射界面必须提供只读探测：OAuth/OIDC 优先直接读取真实 userinfo；provider
不支持服务端直接探测时，管理员可以直接输入上游响应的完整字段路径，不追加授权
跳转或登录前置步骤；SCIM 读取 `/Schemas` 与最多一个 User 资源。探测结果必须来自
真实响应的
完整字段路径，并以“样本值（字段路径）”展示，保存时只保存字段路径。OAuth 路径
不得抹平或自动拼接上游包装层，例如真实响应中的 `data.userId` 必须由探测结果或
管理员输入原样提供；没有真实样本时
不得用协议元数据或默认字段伪装成探测结果。探测结果不得包含 token、secret、
authorization 等凭据字段，样本值必须有长度上限且只短期缓存。管理员从真实探测
结果中选择来源字段，不能依靠猜测或产品内写死厂商字段。探测不得触发目录同步或
远端写操作。

### 7.1 平台认证入口开关

平台级认证策略与任何单一 tenant/provider 分离，默认保持兼容开启：

- `password_login_enabled`：关闭后，后端密码登录接口直接拒绝，登录页隐藏账号密码
  表单与忘记密码入口；已启用的 SSO 登录不受影响。
- `account_registration_enabled`：关闭后，后端自助注册接口直接拒绝，登录页隐藏注册
  入口；现有 PlatformUser 登录不受影响。由租户已启用 ProviderConnection 返回 fresh
  claims 的 SSO 登录属于可信企业身份接入，仍允许 JIT 创建 PlatformUser 和当前租户
  membership，不得被公共自助注册开关误伤。

前端隐藏只用于产品表现，后端开关才是授权边界。两个开关是平台级全局配置，不允许
provider 或 tenant 绕过；管理员创建/邀请成员属于管理操作，继续由其独立权限与策略
控制，不能误用“自助注册”开关替代管理员授权。

## 8. 从当前模型迁移

切换前必须可灰度、可中止并按 provider connection 分批验证；本能力一旦完成正式
切换即按 forward-only 运行，不把“回滚到旧归一链路”作为发布后的恢复手段。

### 阶段 A：基线与约束

1. 只读审计 provider、OrgMember 重复 ID、PlatformUser/tenant membership 多重
   关联、Group/人员数量、无部门和 inactive 数据。
2. 建立同步 run 和冲突审计表；分离 last attempt 与 last success。
3. 在加唯一约束前生成重复修复清单，不能直接删除历史记录。
4. 将当前 `Identity` 明确为 PlatformUser 兼容实体、当前 `User` 明确为 tenant
   membership 兼容实体；先提供领域服务，禁止直接大范围翻转表义。

### 阶段 B：兼容事实表

1. 新增 DirectoryAccount-Group membership 和 Group edge 表，作为所有 provider
   共用的 SCIM 领域事实表。
2. 从现有 `department_id`、`parent_id` 回填一条兼容关系。
3. 保留现有字段作为读兼容投影；新同步先双写关系表与兼容字段。
4. 将 ChannelUserBinding 查询统一到完整安装范围键；pending ChannelSubject 不纳入
   第一轮，避免引入当前产品尚不需要的状态机。

### 阶段 C：统一归一服务

1. 把匹配顺序移到 provider policy，默认 phone 再 email。
2. 所有目录适配器、SSO 回调和可信通道联系人都调用同一归一服务。
3. 增加来源绑定、低优先级冲突、既有绑定冲突和人工 review 流程。
4. 清除通过姓名、subject 前缀或跨 provider external ID 的隐式匹配。
5. 手机号按规范化值建立全局唯一约束；命中已有 PlatformUser 后只创建当前租户的
   membership，不复制 PlatformUser。

### 阶段 D：SCIM 领域内核与适配器

1. 先实现统一 ScimDirectorySnapshot、User/Group/membership 校验和 apply 内核。
2. 原生 SCIM 实现 capability/schema discovery 与完整分页；其他 provider 实现
   各自 transport 到相同 snapshot 的映射。
3. 在测试环境执行 shadow run，只生成 diff 和归一候选，不写正式表。
4. 比较各来源的人员覆盖、联系方式、active 状态、Group 图和用户归一
   结果；冲突必须有清单和规则解释。
5. shadow 通过后，仅对选定 provider 开启正式 apply；其他登录方式不受影响。

### 阶段 E：产品切换

1. 每个 SSO connection 独立显示并调度自己的目录同步。
2. 用户页展示多来源和已绑定通道，组织浏览器改读关系表。
3. 钉钉可继续作为独立登录/目录/IM connection；若某租户决定以内部 SCIM 替代
   钉钉目录，只关闭该 connection 的 directory capability，不删除钉钉通道绑定。
4. 稳定观察至少一个完整同步周期后，才移除对应的旧目录读取路径。

### 正式切换与故障处置

- 正式切换前允许停止候选 provider、修正配置并重新执行 shadow/验收；
- 正式切换后不恢复旧归一链路。故障时暂停受影响 provider 的后续调度，保留已写
  来源事实和审计，以向前修复恢复；其他 provider、登录与 IM 通道继续独立运行；
- 禁止用恢复脚本合并/拆分 PlatformUser、清空来源绑定或把 provider_id 置空；
- 发布门禁必须证明新架构完整覆盖后才允许切换，不能以“上线后再回滚”替代验证。

## 9. 验收标准

实现完成至少需要以下可观察证据：

1. 同一手机号在 SCIM、钉钉和飞书三个来源中只产生一个 PlatformUser；同租户只
   产生一个 TenantUserMembership，并保留三个 DirectoryAccount 来源。跨租户复用
   PlatformUser、分别建立 membership。
2. 默认 phone 优先；配置为 email 优先后行为随 policy 改变。低优先级字段冲突必须
   停止自动合并并进入审计；租户管理员可选择保留用户执行租户内软合并，完成后只剩
   一个活跃 TenantUserMembership 承载相关目录账号、通道绑定和关系。
3. 同一租户多个 SSO 登录方式同时可用，各自组织同步、状态和失败互不覆盖。
4. 原生 SCIM、钉钉和其他适配器产出的统一快照都能包含 active、inactive、无
   Group、多 Group、多根和嵌套 Group。
5. 多父 Group 和多 Group 用户不会因兼容 `parent_id/department_id` 投影丢失关系。
6. 分页中断、totalResults 变化、引用悬空、Group 循环或部分写失败时不 reconcile，
   last success 不更新。
7. 一个来源 tombstone 不会删除其他来源或通道绑定，也不会直接禁用仍有权威来源的
   tenant membership，更不能禁用全局 PlatformUser。
8. 用户 API/UI 能看到平台状态、租户状态、所有来源与已绑定通道；敏感 subject
   和 secret 不泄漏。
9. 所有写入和查询严格 tenant/provider scoped，并有跨租户负向测试。
10. 测试通过真实 API/数据库可观察行为验证，全部在隔离 Docker 环境运行。
11. 每个 provider 可独立配置小时/天/周/月定时同步，也可手动触发；重复触发不产生
    并发 run，管理端轮询能看到单调进度与最终统计。
12. 回归矩阵覆盖每个 provider policy 在目录、SSO 和 IM 入站三类入口的一致结果，
    覆盖钉钉 inbound 自动创建/既有 subject 稳定绑定、全部组织人员选择器、多来源与
    通道标记，以及 PlatformUser、tenant membership、provider、来源账号各层 inactive。

## 10. 第一轮最小落地边界

为保持方案简单高效，第一轮继续复用 `Identity`、`User`、`IdentityProvider`、
`OrgMember`、`OrgDepartment` 和 `ChannelUserBinding`，只新增不可由现有字段可靠表达
的持久化事实：

1. `directory_sync_runs`：provider 独立运行、触发来源、进度和结果；
2. `directory_group_edges`：同 provider 的 Group 嵌套与多父关系；
3. `directory_account_groups`：DirectoryAccount 的多 Group 归属；
4. `IdentityProvider` 上的调度列：启用、间隔值/单位、下一次执行以及最后尝试/成功。

第一轮不另建 PlatformUser、TenantUserMembership、DirectoryAccount 或
DirectoryGroup 表；这些名称是领域语义，分别由现有模型兼容承担。也不引入工作流
引擎、事件总线、分布式队列、通用规则 DSL 或新的凭据系统。适配器统一输出 SCIM
快照，核心服务负责一次校验、一次归一和一次 provider-scoped reconcile。

## 11. 当前实现的已知迁移点

实现前必须重新核对当前代码，但以下边界是迁移重点：

- `IdentityProvider.provider_type` 当前同时承担登录、目录和通道分类，需要拆成显式
  capabilities 或兼容解析层；
- 当前 `Identity` 对应目标 PlatformUser，当前 `User` 对应目标 tenant membership；
  大量调用仍直接读取 `User.tenant_id`，迁移必须先加兼容服务，不能一次性翻转表义；
- 当前允许 identityless User；目标要求可信目录创建全局 opaque anchor，并逐步清理
  identityless tenant membership；
- `OrgMember.department_id` 和 `OrgDepartment.parent_id` 当前只能表达单归属树；
- 部分目录同步逐部门重复拉取用户，member 计数可能是出现次数而非唯一账号数；
- 部分失败仍可能写 last synced，且缺少独立同步 run；
- 管理端当前可能按 provider type 只取第一条 connection；
- provider 配置响应中的 secret 需要改成只写不读和安全引用；
- 目录 profile、SSO shell 和 IM sender shell 当前都可能复用 OrgMember，需要按
  DirectoryAccount、登录 subject 和 ChannelSubject 的事实边界逐步解耦。

这些是目标架构与当前兼容层之间的迁移清单，不是允许新代码继续复制旧行为的理由。

### OpenAPI 可信手机号的存量兼容查询

标准 OpenAPI 的用户委托通过现有 `CanonicalUserResolver` 显式开启
`phone_equivalence` 查询。中国大陆完整 11 位手机号（`1[3-9]` 开头）与
`+86`、`86`、`0086` 加同一完整号码视为精确等价候选。此规则不采用尾号、
模糊或前缀匹配；其他明确国际区号保持原精确规范化行为。等价候选命中多个
Identity 时必须拒绝并进入冲突处置，不能取第一条或自动合并。

这是查询兼容，不更新存量手机号、不迁移全库；其他目录/通道调用默认保持
原行为。应用作用域 subject 绑定和既有全局/租户启停检查继续独立执行。
