# 内部托管 Turn 统一恢复

## 目标与范围

内部托管数字员工的执行统一使用持久化 Turn。输入被接受后，发布关闭、
进程退出或执行节点丢失都保留原执行身份、工具记录和业务关联，由原执行核心接续。
用户停止、等待确认和明确失败分别保持 cancelled、suspended 和 failed 语义。

本轮覆盖平台内部执行链路。外部 OpenClaw 执行端及其恢复协议不在本轮范围，
不改其远端运行方式，也不将其执行转交本地模型。Native Gateway 的本地目标员工
属于内部托管范围。

复用 ChatSession、ChatMessage、现有业务记录、会话租约和模型工具循环。
不新增表、任务引擎或消息中间件，不增加恢复次数、恢复时长或来源限制。

## 当前实现

### 接收与执行身份

接收方先保存原始输入、真实 Session、anchor、generation 和业务关联，
提交后再返回执行 accepted 或调度执行。提交后尚未启动的执行也能被周期扫描发现。

- Trigger 关联原 TriggerExecution；异步唤醒同样先持久化再调度。
- Task 的每次运行使用 TaskLog.id，保留执行用户及原任务要求。
- AgentSchedule 的每个到期时刻生成稳定 occurrence 身份；anchor 与
  next_run_at、last_run_at、run_count 在同一事务提交。后续周期另建执行，恢复不回拨计划。
- manual、API 创建及工具创建任务入口先提交 anchor，再把 anchor ID 交给后台调度。
- 后台 Session 复用非人类 source_channel=trigger，user_id 为空；执行用户保存在
  anchor 上，后台类型通过 background_execution.kind 区分，不伪造人工消息发送者。

后台锚点的 background_execution 保存 kind、reference_id、settings、completion，
以及业务回写和投递完成标记。settings 沿用模型覆盖、推理、Soul/记忆开关及工具设置；
固定任务提示词通过 prepared_turn_context 保存。不存运行对象、回调或密钥。

### 同一执行和恢复核心

Trigger、Task、Schedule、heartbeat、oneshot 的入口只负责准备输入及业务结果处理。
后台首次执行和恢复都经 run_background_turn → resume_startup_anchor → resume_turn，
再进入已有 channel caller 和 call_llm / call_llm_with_failover。
call_agent_llm_with_tools、heartbeat、heartbeat_oneshot 不再维护独立工具循环。
Web、Feishu、API 和工具创建任务均在一个事务内保存 Task、TaskLog 和 anchor。
心跳同样原子保存本次输入、通知消费状态和触发时间，恢复不再次消费收件箱。

会话租约覆盖恢复读取、工具结果处理、模型执行和终态回写。Native Gateway 使用
同一租约入口，容量只由共享执行方领取一次。数据库事务不跨越容量等待和模型调用。

Web/H5、IM、MCP 保留正常交互入口，接收和异常处理与持久化 Turn 对齐；
恢复仍使用同一核心。Subagent 和项目协作保留原业务领取者，通用扫描器不重复领取
subagent Session。确定性督办提醒保留原投递流程，不增加模型调用。

### 中断、停止与业务完成

| 事实 | 处理 |
| --- | --- |
| 持久化执行的子进程丢失 | 以 TurnInterrupted 交回执行方，保留原 anchor，等待恢复 |
| 服务关闭或任务取消，但没有持久化 STOP | 保留未完成执行，不写失败结果 |
| 用户明确停止 | 保留 cancelled，不自动重开原 anchor |
| 等待确认或项目暂停 | 等待原条件恢复，不另建执行绕过条件 |
| 模型或配置明确失败 | 写入 typed failure 和业务失败结果，不当成进程中断 |
| 已有终态回复 | 只补业务回写及投递，不再调用模型 |

complete_background_turn 与 reconcile_background_turn 共用按 kind 分派的幂等 finalizer。
TriggerExecution 状态、webhook 消费位置、Task 结果、手动 Schedule 成功计数及
oneshot 通知，与对应完成标记在同一事务提交。投递使用原消息及 receipt，网络操作在事务外。
STOP 和服务中断不推进 webhook 队列。

Task 停止后可回到 pending，供用户显式再运行；新运行使用新的 TaskLog 身份，
原 cancelled anchor 不会被自动重做。已保存的 LLMFailure 返回时保留原内容和错误码，
不因返回字符串而误判成功，也不再触发 failover。

工具进度沿用共享工具记录与恢复处理：复用已完成结果；对可能已经产生副作用、
但没有确定结果的调用，保留现有恢复判断，不能把重新调用当作安全接续。

### 周期发现与数据变更

启动和现有 trigger worker tick 调用同一恢复 dispatcher。候选来自 Session 的
持久化当前 Turn、后台未完成回写/投递标记及待投递终态回复，不以近期消息窗口
排除仍未结束的持久化 Turn。已有会话租约和工作负载容量负责协调并发。

数据库仅增加三个 partial indexes，并同步模型声明；没有新业务字段或新表：

| 索引 | 对应候选 |
| --- | --- |
| ix_chat_sessions_active_turn | 当前 running / suspended Turn |
| ix_chat_messages_background_unfinished | 后台执行尚未完成投递 |
| ix_chat_messages_terminal_pending | 终态回复的 pending delivery |

索引迁移使用 concurrent 创建/删除，并修复中断遗留的无效索引。
隔离 PostgreSQL 已验证新库初始化、重复执行、旧版本升级及回退再升级；三个索引均有效。

## 验证状态与剩余验收

代码已实现，完成中立交叉审阅及隔离 Docker 定向集成验收，尚未发布生产。
已完成的定向检查包括：Task 输入和执行身份、重复领取、cron 并发 occurrence、
业务 finalizer 重入、Task STOP、oneshot 失败通知，以及 manual/API/工具入口提交后调度。
真实 provider 与隔离执行进程已验证后台调用；Native Gateway 在容量为 1 时与另一
恢复者并发，观察到一次模型调用和一条回复。

真实执行子进程在工具结果落库后被 SIGKILL，周期发现接续原 anchor，复用工具结果，
最终仅一条完成回复；模型配置失效则保存失败，不进入重复恢复。
完成回复先于投递中断、下一 Turn 已开始、通知已发送但后台完成标记未提交，
均验证只补原回复或完成标记，不再调用模型、不重复发送已确认通知。
六个核心恢复测试模块共 48 项检查通过；本次修改的源码均满足 800 行上限。
两个实际 worker 的两次 SIGKILL 和一次正常停止演练，验证原任务自动接管完成。
补充故障、并发及确认验证详见 [可靠性验证记录](turn-recovery-reliability-validation.md)。
按本轮验证授权使用独立 3010 环境，前端与 API 健康检查通过，保留原 3008 环境。
上述定向结果不等于所有供应商和全部中断时机的完整演练；生产发布仍需独立验收。
不向共享开发库或生产写入测试消息，不自动补录历史失败执行。

OpenAPI 数字员工查询增加创建者信息是独立待办，不与 Turn 生命周期耦合。
