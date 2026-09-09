# 内部托管 Turn 恢复可靠性验证

## 范围

本轮使用独立 Docker 数据库、Redis 分库、HTTP 模型服务夹具和真实执行子进程。
容器故障演练运行两个实际应用 worker，保留数据库、工作目录和模型服务，
终止或正常停止其中的 worker，观察另一实例接管。没有操作生产或 3008。

验证覆盖当前启用的内部执行入口。OpenClaw 外部执行不在本轮范围；
停用的 OKR 入口不代表当前运行链路，未重新启用。

## 已发现并修复的问题

| 问题 | 修复 |
| --- | --- |
| 旧心跳残留失效导入及独立模型循环 | 心跳原子保存输入、通知消费及触发时间，复用同一执行和完成机制 |
| 创建 Task 后、建立 Turn 前退出可遗留无执行记录的任务 | Web、Feishu、API、工具创建共用原子 admission |
| 后台确认续跑绕过业务完成流程，可能重复发送结果 | 复用原 background finalizer 和消息回执 |
| A2A 确认把执行 Agent 当成会话存储 Agent | 卡片归规范会话，执行身份从原 anchor 验证；保留真实 sender 和 Gateway 回执 |
| 项目后台任务确认后续跑缺少工作目录 | 复用已有项目状态及目录解析；暂停时保留原 Turn，恢复后正常完成 |
| Web 结果已发布成功，恢复却再次投递并返回失败 | 识别原本地消息已确认的回执，直接完成 |
| 丢失会话租约可能被当作业务失败 | 有持久 anchor 的模型调用使用统一 TurnInterrupted 信号 |

## 故障与并发证据

| 场景 | 观察结果 | 可复用验证入口 |
| --- | --- | --- |
| 执行子进程在工具结果提交后被 SIGKILL | 原 anchor 接续，已完成工具不重跑，仅一个终态 | `backend/tests/test_unified_turn_process_recovery.py` |
| 真实 Redis 执行租约被其他 owner 占用 | 旧执行中断，不能删除新 owner 的租约；原 Turn 可完成 | `backend/tests/test_turn_lease_recovery.py` |
| 两次 worker 容器 SIGKILL，加一次正常停止 | 同一 Task / Session / anchor 完成；一个工具结果、一个最终回复、两条 TaskLog（开始及完成） | `backend/tests/turn_container_fault_fixture.py` |
| 长任务等待模型时启动第二个 worker | 第二实例没有重复执行活跃租约；另一个独立任务正常完成 | 同上 |
| 四个独立进程并发领取 cron / interval / once | 各 occurrence 仅一个领取者及一次实际模型执行 | `backend/tests/test_trigger_durable_recovery.py` |
| webhook 连续两次中断 | 队列不提前消费；同一工具结果接续；完成时仅消费一次 | 同上 |
| 通知已发送、完成标记尚未提交时退出 | 重领仅补标记，不再次发送已确认通知 | 同上 |
| Task / Schedule 模型等待时 STOP，与重复恢复竞争 | 保持 cancelled，不重开；计划计数和任务日志一致 | `backend/tests/test_background_turn_recovery.py` |
| 创建任务提交后尚未 dispatch | 扫描找到已提交的 anchor 并完成任务 | 同上 |
| 心跳 tick 重入、接收通知后中断 | 只建立一次执行；保留原通知文本；最终活动不重复 | 同上 |
| 等待确认、点击后连续两次中断 | 等待时不执行模型；恢复仍为原 anchor，仅一个终态 | `backend/tests/test_turn_ingress_recovery_boundaries.py` |
| 原子代理 owner 过期后被重新领取 | 旧 owner 无法写入终态或覆盖新 owner | 同上 |
| 会话已进入下一轮，旧回复尚未完成投递 | 补原消息回执，不再执行模型、不向新来源误投递 | `backend/tests/test_turn_recovery_delivery.py` |

容器演练的 Task 验收以固定 Task / Session / anchor 及各自模型请求核对。
首次演练中，系统默认开启的旧心跳另行触发，暴露了上述遗漏；该心跳随后独立
完成归一和真实 tick 回归。夹具已明确关闭自动心跳，便于单独复现目标 Task。

## 检查范围

- Trigger 故障专项及既有 webhook / fired-state：34 项通过。
- 后台 Task / Schedule / oneshot / heartbeat 与任务创建：9 项通过；相关旧行为回归 19 项通过。
- Native Gateway / A2A 容量、幂等及身份：20 项通过。
- 真实 Redis owner 丢失及既有租约：20 项通过。
- 六个核心恢复测试模块：48 项通过，包含真实执行子进程终止后接续。
- 确认、A2A、项目目录等接收边界：6 项通过；项目确认续跑已完成独立只读复审。
- Web、MCP、Subagent、Project、确认及租户权限均运行对应行为回归；
  测试分组有重叠，不将多次运行累加为一个总数。
- 626 个服务与 API 模块实际导入通过，避免只在启动后的懒加载路径发现失效依赖。
- 三个局部索引在隔离 PostgreSQL 验证初始化、升级、回退后再升级和有效状态。
- 新增源码和验证夹具 Ruff 检查通过；所有修改及新增源码符合 800 行上限，diff 检查通过。
- 3010 独立开发环境已加载最终代码，应用启动、恢复扫描及前端/API HTTP 200 检查通过。

## 尚未覆盖

没有向外部 IM 供应商发送真实业务消息，没有对生产执行故障注入，
没有模拟所有数据库、网络和供应商故障组合。上述结果是具体故障场景的验证证据，
不能等同于对所有外部副作用的无条件 exactly-once 保证。

已经发出但无法确认结果的外部操作沿用既有 unknown / partial 语义，
不为通过恢复验收而伪造成功或盲目重放。生产发布仍需单独准备和验收。
