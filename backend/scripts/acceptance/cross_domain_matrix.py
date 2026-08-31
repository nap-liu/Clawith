"""Business-domain matrix for the public API acceptance runner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Role:
    key: str
    name: str
    description: str


@dataclass(frozen=True)
class DeliverableContract:
    path: str
    owner_role: str
    purpose: str
    concepts: tuple[str, ...]
    review_role: str | None = None


@dataclass(frozen=True)
class Scenario:
    key: str
    name: str
    description: str
    goal: str
    criteria: tuple[str, ...]
    roles: tuple[str, ...]
    input_path: str
    input_content: str
    collaboration: str
    outputs: tuple[str, ...]
    deliverables: tuple[DeliverableContract, ...] = ()
    decision_signals: tuple[tuple[str, ...], ...] = (
        ("证据", "数据", "来源", "evidence", "source", "baseline"),
        ("决策", "建议", "结论", "decision", "recommend", "selected"),
        ("下一步", "负责人", "截止", "next step", "owner", "due"),
    )


ROLES = {
    role.key: role
    for role in (
        Role("marketing_owner", "营销项目负责人", "负责市场研究、发布策略、范围、预算、交付质量与跨岗位推进。"),
        Role("research", "用户研究员", "负责受众研究、用户分群、需求证据与研究局限说明。"),
        Role("content", "内容策略师", "负责品牌叙事、落地页、邮件与社媒内容，保持声明有据可查。"),
        Role("channel", "渠道运营", "负责渠道组合、发布日历、UTM、预算分配与执行清单。"),
        Role("visual", "视觉设计师", "负责视觉方向、信息层级、素材规格、无障碍和品牌一致性。"),
        Role("risk", "风险与合规评审", "独立检查事实、隐私、合规、风险与审批门禁，不为交付方背书。"),
        Role("analytics_owner", "数据项目负责人", "负责业务问题、指标口径、分析计划、结论质量与行动闭环。"),
        Role("data_engineer", "数据工程师", "负责数据字典、质量校验、清洗转换和可重复执行的数据管道。"),
        Role("data_analyst", "数据分析师", "负责指标计算、分群、趋势分析、异常解释和可复核结论。"),
        Role("bi_designer", "BI 设计师", "负责仪表盘信息架构、图表选择、筛选器和可访问性规范。"),
        Role("stat_reviewer", "统计评审", "独立复核指标公式、样本局限、数字一致性和推断边界。"),
        Role("hiring_owner", "招聘项目负责人", "负责岗位成果、统一标准、面试流程、公平性和人工决策门禁。"),
        Role("job_analyst", "岗位分析师", "把业务目标转化为职责、能力模型、职级和结构化评分标准。"),
        Role("sourcer", "人才寻访顾问", "负责合规渠道、候选池策略、触达话术和来源质量。"),
        Role("interviewer", "结构化面试官", "按统一题库和行为证据评分，不使用与岗位无关的个人信息。"),
        Role("hr_compliance", "人力合规评审", "检查公平招聘、隐私最小化、敏感信息处理和人工录用门禁。"),
        Role("ops_owner", "营运项目负责人", "负责补货目标、服务水平、跨门店协调、例外处理和执行收口。"),
        Role("demand_planner", "需求计划员", "负责销量趋势、提前期、MOQ 与安全库存计算。"),
        Role("inventory", "库存分析师", "负责库存健康、缺货风险、数量对账和异常识别。"),
        Role("supplier", "供应商协调员", "负责供应约束、到货窗口、缺口确认和替代方案。"),
        Role("store_ops", "门店运营协调", "负责门店日历、现场例外、优先级和执行确认。"),
        Role("support_owner", "客服项目负责人", "负责工单治理、SLA、重大升级、知识闭环与服务质量。"),
        Role("triage", "工单分诊专员", "负责分类、优先级、SLA 判断、去重和升级路由。"),
        Role("kb", "知识库运营", "负责把已验证解决方案沉淀为可检索、可维护的知识条目。"),
        Role("escalation", "重大问题升级专家", "负责 P0/P1 升级、跨团队协同、时间线与恢复证据。"),
        Role("support_qa", "客服质量与 VOC 分析", "负责抽样质检、根因聚类、趋势验证和改进建议。"),
        Role("procurement_owner", "采购项目负责人", "负责需求、供应商评估、预算、安全法务协同和人工审批包。"),
        Role("finance", "财务分析师", "负责报价核对、TCO、币种、预算差异与敏感性分析。"),
        Role("security", "信息安全评审", "负责安全问卷、数据处理、访问控制、风险与整改条件。"),
        Role("legal", "法务评审", "负责合同条款、责任边界、退出安排与待人工确认事项。"),
        Role("vendor", "供应商分析师", "负责能力对比、使用量适配、加权评分和证据索引。"),
        Role("compliance_owner", "合规项目负责人", "负责范围、控制框架、证据、差距、整改计划和独立复核。"),
        Role("policy", "政策研究员", "负责权威要求、适用性、版本和引用依据研究。"),
        Role("evidence", "证据管理员", "负责证据索引、来源、时效、完整性和保留要求。"),
        Role("control_mapper", "控制映射分析师", "负责要求、控制、证据与责任人的多对多映射。"),
        Role("tech_assessor", "技术控制评估员", "负责技术抽测、差距验证、风险等级和整改可行性。"),
        Role("independent", "独立复核员", "独立挑战方法、证据充分性和结论，不能兼任交付负责人。"),
        Role("development_owner", "研发负责人", "负责技术范围、依赖顺序、工程质量和可运行交付的最终收口。"),
        Role("backend_engineer", "后端研发工程师", "负责领域模型、API 契约、持久化、错误边界和服务端测试。"),
        Role("frontend_engineer", "前端研发工程师", "负责交互实现、状态管理、可访问性和浏览器端测试。"),
        Role("code_reviewer", "代码审查工程师", "独立检查正确性、安全性、可维护性和回归风险并提出修改意见。"),
        Role("qa_engineer", "测试工程师", "依据验收标准设计分层测试、复现缺陷并核验修复证据。"),
        Role("product_owner", "产品负责人", "负责问题定义、用户价值、范围取舍、成功指标和产品决策。"),
        Role("ux_researcher", "体验研究员", "负责研究问题、样本、用户任务、证据局限和洞察优先级。"),
        Role("interaction_designer", "交互设计师", "负责信息架构、关键任务流、状态、异常路径和交互规格。"),
        Role("content_designer", "内容设计师", "负责界面语言、提示、错误恢复和术语一致性。"),
        Role("accessibility_reviewer", "无障碍评审", "独立检查键盘、焦点、语义、对比度和辅助技术使用路径。"),
        Role("release_owner", "发布负责人", "负责变更范围、发布窗口、审批门禁、回滚决策和发布结果。"),
        Role("sre_engineer", "SRE 工程师", "负责容量、可观测性、SLO、告警、故障演练和恢复验证。"),
        Role("deployment_engineer", "部署工程师", "负责构建制品、环境差异、部署步骤和可重复执行脚本。"),
        Role("security_reviewer", "发布安全评审", "独立复核依赖、凭证、权限、漏洞和高风险变更。"),
        Role("change_manager", "变更经理", "负责变更记录、业务通知、责任人、时间窗和人工批准留痕。"),
    )
}


SCENARIOS = (
    Scenario(
        "development",
        "企业反馈平台工程交付",
        "从 API 契约开始完成可运行的反馈提交、分诊和审计能力，并由独立审查与测试把关。",
        "交付可在 Docker 中运行、具备前后端测试和可追溯审计的反馈平台增量。",
        ("API 契约与实现一致", "前端覆盖失败与空状态", "关键路径自动化测试通过", "审查问题闭环"),
        (
            "development_owner",
            "backend_engineer",
            "frontend_engineer",
            "code_reviewer",
            "qa_engineer",
        ),
        "inputs/engineering-brief.md",
        """# 工程输入\n\n能力：客户反馈提交、状态流转、负责人指派与审计时间线\n技术边界：FastAPI + PostgreSQL；React + TypeScript；Docker Compose 验证\n接口：POST /feedbacks、PATCH /feedbacks/{id}、GET /feedbacks/{id}/events\n质量门禁：服务端状态机测试、浏览器关键路径、独立代码审查均通过。\n""",
        "研发负责人拆分契约与依赖；后端先固化 API；前端依据契约实现；代码审查独立提出问题；测试工程师依据风险复验。",
        (
            "engineering/api-contract.md",
            "backend/app/api/feedbacks.py",
            "frontend/src/features/feedbacks/FeedbackForm.tsx",
            "backend/tests/test_feedbacks_api.py",
            "compose.yaml",
            "review/code-review.md",
        ),
        deliverables=(
            DeliverableContract(
                "engineering/api-contract.md",
                "development_owner",
                "状态机、接口和错误契约",
                ("POST /feedbacks", "状态", "错误"),
                "code_reviewer",
            ),
            DeliverableContract(
                "backend/app/api/feedbacks.py",
                "backend_engineer",
                "服务端实现与数据库验证",
                ("FastAPI", "feedback", "status"),
                "code_reviewer",
            ),
            DeliverableContract(
                "frontend/src/features/feedbacks/FeedbackForm.tsx",
                "frontend_engineer",
                "浏览器交互、状态和无障碍",
                ("React", "aria", "error"),
                "qa_engineer",
            ),
            DeliverableContract(
                "backend/tests/test_feedbacks_api.py",
                "qa_engineer",
                "状态流转、错误和审计的自动化回归测试",
                ("pytest", "audit", "status"),
                "code_reviewer",
            ),
            DeliverableContract(
                "compose.yaml",
                "development_owner",
                "可重复构建、健康检查和端到端运行入口",
                ("services", "build", "healthcheck"),
                "qa_engineer",
            ),
            DeliverableContract(
                "review/code-review.md",
                "code_reviewer",
                "独立审查、质疑与修改结论",
                ("风险", "问题", "修复"),
            ),
        ),
    ),
    Scenario(
        "product_ux",
        "移动端报销体验重构",
        "依据真实任务研究重构拍票、补充字段、审批和失败恢复流程，而不是只画静态页面。",
        "交付有研究证据、状态完整、可访问且可实施的移动报销体验规格。",
        ("覆盖四类关键任务", "包含异常与恢复路径", "文案可执行", "无障碍问题完成独立复核"),
        (
            "product_owner",
            "ux_researcher",
            "interaction_designer",
            "content_designer",
            "accessibility_reviewer",
        ),
        "inputs/product-research.md",
        """# 产品研究输入\n\n访谈样本：差旅员工 8 人、审批人 4 人、财务 3 人\n主要失败：弱网拍票丢失、费用类型难选、退回原因不可见、重复提交\n业务约束：单据字段由财务规则动态控制；提交前必须明确缺失项；移动端单手操作优先\n成功指标：提交完成率、首次通过率、平均补单次数、无障碍关键任务成功率。\n""",
        "研究员先形成证据和局限；产品负责人明确取舍；交互与内容并行后互审；无障碍评审独立挑战关键路径。",
        (
            "research/task-findings.md",
            "product/decision-log.md",
            "prototype/expense-flow.html",
            "design/content-spec.md",
            "review/accessibility-audit.md",
        ),
        deliverables=(
            DeliverableContract(
                "research/task-findings.md",
                "ux_researcher",
                "用户任务证据、样本和研究局限",
                ("样本", "任务", "局限"),
            ),
            DeliverableContract(
                "product/decision-log.md",
                "product_owner",
                "范围取舍、指标和决策依据",
                ("取舍", "指标", "决策"),
                "accessibility_reviewer",
            ),
            DeliverableContract(
                "prototype/expense-flow.html",
                "interaction_designer",
                "可交互主路径、异常状态与恢复原型",
                ("offline", "returned", "recovery"),
                "accessibility_reviewer",
            ),
            DeliverableContract(
                "design/content-spec.md",
                "content_designer",
                "字段、提示和错误恢复文案",
                ("字段", "提示", "错误"),
            ),
            DeliverableContract(
                "review/accessibility-audit.md",
                "accessibility_reviewer",
                "独立无障碍审查和整改要求",
                ("键盘", "焦点", "语义"),
            ),
        ),
    ),
    Scenario(
        "marketing",
        "B2B SaaS 春季产品发布活动",
        "基于真实产品发布案例方法，完成从 ICP 到多渠道发布与度量的可执行方案。",
        "交付一套可执行、可审核、可度量的 B2B SaaS 产品发布活动包。",
        ("至少三个 ICP 分群", "预算合计等于 50 万元", "所有声明完成合规复核", "UTM 唯一且可追踪"),
        ("marketing_owner", "research", "content", "channel", "visual", "risk"),
        "inputs/marketing-brief.md",
        """# 发布输入\n\n产品：企业级 AI 协作平台 3.0\n发布日期：2026-10-15\n总预算：500,000 CNY\n目标：获得 1,200 个有效线索和 80 个销售机会\n市场：中国大陆中大型科技、制造与专业服务企业\n约束：不得承诺不可验证的效率提升；所有客户引用需有证据编号。\n""",
        "用户研究先行；策略收敛后内容、视觉和渠道并行；风险评审独立把关；负责人最终收口。",
        (
            "research/icp.md",
            "strategy/campaign-plan.md",
            "content/landing-page.md",
            "design/creative-spec.md",
            "analytics/utm-plan.csv",
            "review/claims-review.md",
        ),
    ),
    Scenario(
        "data",
        "客服需求与 SLA 周报分析",
        "用脱敏工单样例建立可重复的数据质量、指标、洞察和 BI 规格闭环。",
        "交付可重复运行且数字可交叉核对的客服 SLA 分析包。",
        ("数据质量可复核", "指标公式有明确口径", "报告数字与 CSV 一致", "行动建议引用具体数据"),
        ("analytics_owner", "data_engineer", "data_analyst", "stat_reviewer", "bi_designer"),
        "inputs/tickets.csv",
        """ticket_id,channel,category,priority,created_at,first_response_min,resolution_min,sla_first_response_min,status\nT001,email,billing,P2,2026-08-01T09:00:00Z,42,380,60,closed\nT002,chat,outage,P0,2026-08-01T10:00:00Z,4,78,10,closed\nT003,email,account,P1,2026-08-02T11:00:00Z,95,620,30,closed\nT004,phone,outage,P1,2026-08-03T08:30:00Z,18,240,30,closed\nT005,chat,how-to,P3,2026-08-03T12:00:00Z,30,90,120,closed\nT006,email,billing,P2,2026-08-04T15:00:00Z,72,510,60,closed\nT007,chat,account,P2,2026-08-05T09:30:00Z,22,180,60,closed\nT008,phone,outage,P0,2026-08-05T10:15:00Z,7,120,10,closed\n""",
        "数据工程先做质量门禁；分析与 BI 以同一口径并行；统计评审独立复算；负责人形成行动建议。",
        (
            "data/quality-report.md",
            "analysis/metrics.sql",
            "outputs/kpis.csv",
            "reports/weekly-insight.md",
            "reports/dashboard-spec.md",
        ),
    ),
    Scenario(
        "hr",
        "高级客户成功经理招聘",
        "以结构化面试和公平性审查完成岗位定义、评分表、面试计划与人工决策材料。",
        "交付不含敏感个人信息、标准一致且保留人工录用门禁的招聘包。",
        ("统一评分标准", "不写入候选人敏感信息", "推荐引用行为证据", "录用决定保持人工门禁"),
        ("hiring_owner", "job_analyst", "sourcer", "interviewer", "hr_compliance"),
        "inputs/hiring-brief.md",
        """# 招聘输入\n\n岗位：高级客户成功经理\n目标：90 天内建立 30 家重点客户健康度体系并把续约预测准确率提升到 85%\n地点：上海/远程混合\n薪酬：35k-50k/月\n候选人材料：仅使用 C01-C05 匿名编号，不保存姓名、性别、年龄、照片、家庭状态。\n最终录用由人类招聘委员会决定。\n""",
        "岗位分析定义统一量尺；寻访和面试执行分离；合规独立复核；负责人只给证据化建议，不做自动录用。",
        (
            "hiring/job-description.md",
            "hiring/sourcing-plan.md",
            "hiring/scorecard.csv",
            "hiring/interview-plan.md",
            "review/fairness-audit.md",
            "hiring/decision-memo.md",
        ),
    ),
    Scenario(
        "operations",
        "多门店库存补货计划",
        "围绕销量、库存、MOQ、提前期和闭店日历生成可对账补货计划与例外台账。",
        "交付数量可对账、约束可解释、例外有负责人和日期的门店补货包。",
        ("不出现负库存", "满足 MOQ 与提前期", "每个短缺有方案或例外", "汇总与明细一致"),
        ("ops_owner", "demand_planner", "inventory", "supplier", "store_ops", "risk"),
        "inputs/inventory.csv",
        """store,sku,on_hand,avg_daily_sales,lead_days,moq,service_days,closed_dates\nS01,SKU-A,18,6,4,24,14,2026-09-03\nS01,SKU-B,65,5,7,20,14,\nS02,SKU-A,8,4,4,24,14,2026-09-04\nS02,SKU-C,120,7,10,50,21,\nS03,SKU-B,10,3,7,20,14,2026-09-05\nS03,SKU-C,20,8,10,50,21,\n""",
        "计划员计算需求；库存分析交叉对账；供应商与门店分别确认约束；风险评审检查例外；负责人发布日运行手册。",
        (
            "ops/inventory-health.csv",
            "ops/replenishment-plan.csv",
            "ops/exception-log.md",
            "ops/vendor-calloff.csv",
            "ops/daily-runbook.md",
        ),
    ),
    Scenario(
        "support",
        "积压工单治理与重大问题升级",
        "对脱敏积压工单完成分诊、SLA 识别、P0/P1 升级、质检、知识库和 VOC 闭环。",
        "交付可执行的客服积压治理包并让每个重大问题具有精确协作记录。",
        ("所有工单有分类和优先级", "所有 SLA 违约被标记", "P0/P1 有点对点升级记录", "PII 全部脱敏"),
        ("support_owner", "triage", "kb", "escalation", "support_qa"),
        "inputs/backlog.csv",
        """ticket_id,age_hours,category,priority,summary,sla_hours\nK101,30,login,P1,SSO redirect loop for tenant A,4\nK102,8,billing,P2,Invoice currency mismatch,24\nK103,3,outage,P0,EU webhook delivery stopped,1\nK104,72,how-to,P3,Dashboard export instructions,48\nK105,26,privacy,P1,Deletion request pending,8\nK106,12,integration,P2,CRM sync duplicate records,24\n""",
        "分诊建立队列；重大升级专家只处理 P0/P1；知识库接收已验证方案；质量与 VOC 独立抽查；负责人收口。",
        (
            "support/triage.csv",
            "support/escalation-register.csv",
            "support/response-playbook.md",
            "quality/qa-sample.csv",
            "insights/voc-report.md",
        ),
    ),
    Scenario(
        "procurement",
        "软件供应商年度续约评估",
        "对三家供应商报价、使用量、安全问卷与合同条款完成并行评审和人工审批包。",
        "交付不超预算、权重可核对且保留人工签署门禁的软件续约建议。",
        ("金额币种核对一致", "评分权重合计 100%", "推荐不超预算", "付款与签署保持人工门禁"),
        ("procurement_owner", "finance", "security", "legal", "vendor", "risk"),
        "inputs/vendor-quotes.csv",
        """vendor,currency,annual_license,implementation,support,active_users,sso,export_sla_days\nAlpha,CNY,420000,30000,20000,680,yes,7\nBeta,CNY,360000,80000,40000,680,yes,14\nGamma,USD,52000,5000,3000,680,no,30\n\n预算上限：500,000 CNY；评估汇率：1 USD = 7.20 CNY；安全与法务未通过不得推荐。\n""",
        "财务、安全、法务并行独立评审；供应商分析做加权评分；风险复核挑战假设；负责人形成审批包但不签署。",
        (
            "finance/tco.csv",
            "review/security.md",
            "review/legal.md",
            "procurement/vendor-scorecard.csv",
            "procurement/approval-pack.md",
        ),
    ),
    Scenario(
        "compliance",
        "NIST CSF 2.0 控制差距评估",
        "以 NIST CSF 2.0 Profile 方法完成范围、证据、控制映射、差距处置和独立复核。",
        "交付控制—证据—责任—整改可追溯、无证据不宣称合规的差距评估包。",
        ("每个控制有状态证据和负责人", "无证据不得合规", "每个差距有等级和期限", "独立复核通过"),
        ("compliance_owner", "policy", "evidence", "control_mapper", "tech_assessor", "independent"),
        "inputs/control-scope.md",
        """# 评估范围\n\n系统：多租户 AI 协作 SaaS 的身份、会话、项目文件与审计服务\n框架：NIST CSF 2.0，重点覆盖 GV、ID、PR、DE、RS、RC\n证据样例：访问控制策略、资产清单、备份记录、事件响应流程、日志保留配置\n风险偏好：高风险 30 天、中风险 90 天、低风险 180 天完成整改\n所有例外必须由人类风险委员会批准。\n""",
        "政策研究定义要求；证据管理员建立索引；映射与技术抽测协作；独立复核员挑战结论；负责人形成治理报告。",
        (
            "scope/system-scope.md",
            "governance/remediation-plan.md",
            "controls/control-matrix.csv",
            "evidence/evidence-index.csv",
            "assessment/gap-register.csv",
            "review/independent-review.md",
        ),
    ),
    Scenario(
        "release_operations",
        "多租户平台高峰期灰度发布",
        "在高峰业务流量下完成制品验证、容量门禁、灰度、回滚演练和变更审批。",
        "交付可重复、可观测、可回滚且保留人工放行门禁的生产发布包。",
        ("制品可追溯", "容量与 SLO 达标", "回滚演练成功", "安全和变更审批有明确结论"),
        (
            "release_owner",
            "deployment_engineer",
            "sre_engineer",
            "qa_engineer",
            "security_reviewer",
            "change_manager",
        ),
        "inputs/release-window.md",
        """# 发布窗口输入\n\n版本：v3.8.0；制品摘要：sha256:8f5d...c21a\n窗口：2026-09-12 01:00-03:00 CST；冻结点：00:30\n基线：500 并发 Turn，p95 首包 < 4s，错误率 < 1%，数据库连接利用率 < 75%\n灰度：5% -> 25% -> 100%；每阶段观察 15 分钟\n回滚条件：错误率连续 5 分钟 > 2% 或关键数据校验失败；最终放行由人类变更经理批准。\n""",
        "部署工程师验证制品和步骤；SRE 独立执行容量与回滚演练；测试与安全并行门禁；变更经理核对证据；发布负责人执行灰度决策。",
        (
            "release/artifact-manifest.md",
            "deploy/canary.yaml",
            "release/deployment-runbook.md",
            "reliability/capacity-report.md",
            "reliability/rollback-drill.md",
            "review/security-gate.md",
            "release/change-record.md",
        ),
        deliverables=(
            DeliverableContract(
                "release/artifact-manifest.md",
                "deployment_engineer",
                "制品、配置和来源校验",
                ("sha256", "版本", "配置"),
                "security_reviewer",
            ),
            DeliverableContract(
                "release/deployment-runbook.md",
                "deployment_engineer",
                "灰度步骤、检查点和停止条件",
                ("5%", "25%", "停止"),
                "release_owner",
            ),
            DeliverableContract(
                "deploy/canary.yaml",
                "deployment_engineer",
                "机器可执行的灰度阶段、权重和分析门禁",
                ("stages", "weight", "analysis"),
                "sre_engineer",
            ),
            DeliverableContract(
                "reliability/capacity-report.md",
                "sre_engineer",
                "容量、SLO 和瓶颈证据",
                ("500", "p95", "错误率"),
                "qa_engineer",
            ),
            DeliverableContract(
                "reliability/rollback-drill.md",
                "sre_engineer",
                "回滚演练、RTO 和数据校验",
                ("回滚", "RTO", "校验"),
                "change_manager",
            ),
            DeliverableContract(
                "review/security-gate.md",
                "security_reviewer",
                "发布安全风险和放行条件",
                ("漏洞", "凭证", "风险"),
                "change_manager",
            ),
            DeliverableContract(
                "release/change-record.md",
                "change_manager",
                "发布窗口、责任人和人工审批",
                ("窗口", "负责人", "审批"),
            ),
        ),
    ),
)


LEGACY_DELIVERABLE_CONTRACTS: dict[str, tuple[DeliverableContract, ...]] = {
    "marketing": (
        DeliverableContract("research/icp.md", "research", "受众分群证据与局限", ("ICP", "分群", "证据")),
        DeliverableContract(
            "strategy/campaign-plan.md",
            "marketing_owner",
            "预算、目标和渠道决策",
            ("500,000", "1,200", "预算"),
            "risk",
        ),
        DeliverableContract(
            "content/landing-page.md", "content", "有据可查的落地页内容", ("价值", "证据编号", "行动"), "risk"
        ),
        DeliverableContract(
            "design/creative-spec.md", "visual", "信息层级、素材规格与无障碍", ("层级", "规格", "无障碍"), "risk"
        ),
        DeliverableContract("analytics/utm-plan.csv", "channel", "唯一可追踪渠道参数", ("utm_", "渠道", "预算")),
        DeliverableContract("review/claims-review.md", "risk", "声明和合规独立复核", ("声明", "风险", "结论")),
    ),
    "data": (
        DeliverableContract(
            "data/quality-report.md", "data_engineer", "数据质量门禁", ("缺失", "重复", "校验"), "stat_reviewer"
        ),
        DeliverableContract(
            "analysis/metrics.sql",
            "data_analyst",
            "可重复指标计算",
            ("SELECT", "sla", "first_response"),
            "stat_reviewer",
        ),
        DeliverableContract(
            "outputs/kpis.csv", "data_analyst", "可交叉核对 KPI", ("sla", "rate", "count"), "stat_reviewer"
        ),
        DeliverableContract(
            "reports/weekly-insight.md",
            "analytics_owner",
            "基于数字的行动结论",
            ("洞察", "数据", "行动"),
            "stat_reviewer",
        ),
        DeliverableContract(
            "reports/dashboard-spec.md", "bi_designer", "仪表盘图表和筛选规格", ("图表", "筛选", "指标")
        ),
    ),
    "hr": (
        DeliverableContract(
            "hiring/job-description.md", "job_analyst", "岗位成果和能力模型", ("90 天", "职责", "能力"), "hr_compliance"
        ),
        DeliverableContract(
            "hiring/sourcing-plan.md",
            "sourcer",
            "合规来源、人才池与触达策略",
            ("渠道", "人才池", "触达"),
            "hr_compliance",
        ),
        DeliverableContract(
            "hiring/scorecard.csv", "job_analyst", "统一行为证据评分标准", ("行为", "评分", "证据"), "hr_compliance"
        ),
        DeliverableContract(
            "hiring/interview-plan.md", "interviewer", "结构化面试流程", ("题目", "面试", "评分"), "hr_compliance"
        ),
        DeliverableContract(
            "review/fairness-audit.md", "hr_compliance", "公平性和隐私独立审查", ("公平", "隐私", "敏感")
        ),
        DeliverableContract(
            "hiring/decision-memo.md", "hiring_owner", "保留人工门禁的建议", ("建议", "证据", "人工"), "hr_compliance"
        ),
    ),
    "operations": (
        DeliverableContract(
            "ops/inventory-health.csv", "inventory", "库存健康与缺口核对", ("on_hand", "shortage", "sku")
        ),
        DeliverableContract(
            "ops/replenishment-plan.csv",
            "demand_planner",
            "满足 MOQ 和提前期的补货量",
            ("moq", "lead", "quantity"),
            "inventory",
        ),
        DeliverableContract("ops/exception-log.md", "risk", "例外风险、责任人与期限", ("例外", "负责人", "期限")),
        DeliverableContract("ops/vendor-calloff.csv", "supplier", "供应约束与到货安排", ("供应", "到货", "SKU")),
        DeliverableContract(
            "ops/daily-runbook.md", "store_ops", "门店日历和执行检查点", ("门店", "闭店", "检查"), "ops_owner"
        ),
    ),
    "support": (
        DeliverableContract(
            "support/triage.csv", "triage", "分类、优先级与 SLA 标记", ("priority", "sla", "category"), "support_qa"
        ),
        DeliverableContract(
            "support/escalation-register.csv",
            "escalation",
            "P0/P1 精确升级记录",
            ("P0", "P1", "owner"),
            "support_owner",
        ),
        DeliverableContract(
            "support/response-playbook.md", "kb", "已验证响应与知识复用", ("响应", "验证", "知识"), "support_qa"
        ),
        DeliverableContract("quality/qa-sample.csv", "support_qa", "客服质检抽样与结论", ("sample", "score", "result")),
        DeliverableContract(
            "insights/voc-report.md", "support_qa", "根因趋势与改进建议", ("VOC", "根因", "建议"), "support_owner"
        ),
    ),
    "procurement": (
        DeliverableContract(
            "finance/tco.csv", "finance", "报价、汇率和预算核对", ("CNY", "TCO", "500000"), "procurement_owner"
        ),
        DeliverableContract("review/security.md", "security", "安全风险与整改条件", ("访问控制", "风险", "条件")),
        DeliverableContract("review/legal.md", "legal", "合同责任与退出条款", ("责任", "退出", "条款")),
        DeliverableContract(
            "procurement/vendor-scorecard.csv",
            "vendor",
            "权重为 100% 的供应商评分",
            ("weight", "score", "vendor"),
            "risk",
        ),
        DeliverableContract(
            "procurement/approval-pack.md",
            "procurement_owner",
            "保留人工签署的推荐包",
            ("推荐", "预算", "人工"),
            "risk",
        ),
    ),
    "compliance": (
        DeliverableContract(
            "scope/system-scope.md", "policy", "适用框架和系统边界", ("NIST", "范围", "适用"), "independent"
        ),
        DeliverableContract(
            "governance/remediation-plan.md",
            "compliance_owner",
            "差距处置、治理责任和期限决策",
            ("处置", "责任", "期限"),
            "independent",
        ),
        DeliverableContract(
            "controls/control-matrix.csv",
            "control_mapper",
            "控制、证据和责任映射",
            ("control", "evidence", "owner"),
            "independent",
        ),
        DeliverableContract(
            "evidence/evidence-index.csv",
            "evidence",
            "证据来源、时效和完整性",
            ("source", "date", "status"),
            "independent",
        ),
        DeliverableContract(
            "assessment/gap-register.csv",
            "tech_assessor",
            "差距等级、负责人和期限",
            ("risk", "owner", "due"),
            "independent",
        ),
        DeliverableContract(
            "review/independent-review.md", "independent", "独立质疑和复核结论", ("质疑", "证据", "结论")
        ),
    ),
}


def scenario_deliverables(scenario: Scenario) -> tuple[DeliverableContract, ...]:
    """Return the explicit professional contract for every required output."""

    return scenario.deliverables or LEGACY_DELIVERABLE_CONTRACTS.get(scenario.key, ())


def validate_matrix(scenarios: tuple[Scenario, ...] = SCENARIOS) -> None:
    """Fail before launch when a domain contract is incomplete or ambiguous."""

    errors: list[str] = []
    scenario_keys: set[str] = set()
    for scenario in scenarios:
        prefix = scenario.key or "<missing-key>"
        if scenario.key in scenario_keys:
            errors.append(f"{prefix}: duplicate scenario key")
        scenario_keys.add(scenario.key)
        if not all((scenario.name, scenario.description, scenario.goal, scenario.input_path, scenario.input_content)):
            errors.append(f"{prefix}: name, description, goal, and input evidence are required")
        if len(scenario.roles) < 5 or len(set(scenario.roles)) != len(scenario.roles):
            errors.append(f"{prefix}: at least five distinct professional roles are required")
        unknown_roles = sorted(set(scenario.roles) - ROLES.keys())
        if unknown_roles:
            errors.append(f"{prefix}: unknown roles: {', '.join(unknown_roles)}")
        if len(scenario.outputs) != len(set(scenario.outputs)):
            errors.append(f"{prefix}: output paths must be unique")

        contracts = scenario_deliverables(scenario)
        contract_paths = tuple(contract.path for contract in contracts)
        if set(contract_paths) != set(scenario.outputs) or len(contract_paths) != len(scenario.outputs):
            errors.append(f"{prefix}: every output needs exactly one deliverable contract")
        for contract in contracts:
            if contract.owner_role not in scenario.roles:
                errors.append(f"{prefix}:{contract.path}: owner is not a project role")
            if contract.review_role and contract.review_role not in scenario.roles:
                errors.append(f"{prefix}:{contract.path}: reviewer is not a project role")
            if contract.review_role == contract.owner_role:
                errors.append(f"{prefix}:{contract.path}: independent reviewer cannot be the owner")
            if not contract.purpose.strip() or len(contract.concepts) < 3:
                errors.append(f"{prefix}:{contract.path}: purpose and three domain concepts are required")
        contracted_roles = {contract.owner_role for contract in contracts} | {
            contract.review_role for contract in contracts if contract.review_role
        }
        roles_without_contract = sorted(set(scenario.roles) - contracted_roles)
        if roles_without_contract:
            errors.append(f"{prefix}: roles without an output or review contract: {', '.join(roles_without_contract)}")
    if errors:
        raise ValueError("Invalid cross-domain acceptance matrix:\n- " + "\n- ".join(errors))


def describe_matrix(scenarios: tuple[Scenario, ...] = SCENARIOS) -> dict[str, Any]:
    """Return an auditable matrix without requiring credentials or API access."""

    validate_matrix(scenarios)
    return {
        "scenario_count": len(scenarios),
        "scenarios": [
            {
                "key": scenario.key,
                "name": scenario.name,
                "goal": scenario.goal,
                "input": {
                    "path": scenario.input_path,
                    "content": scenario.input_content,
                },
                "collaboration": scenario.collaboration,
                "roles": [
                    {
                        "key": role_key,
                        "name": ROLES[role_key].name,
                        "responsibility": ROLES[role_key].description,
                    }
                    for role_key in scenario.roles
                ],
                "deliverables": [
                    {
                        "path": contract.path,
                        "owner_role": contract.owner_role,
                        "review_role": contract.review_role,
                        "purpose": contract.purpose,
                        "concepts": list(contract.concepts),
                    }
                    for contract in scenario_deliverables(scenario)
                ],
                "criteria": list(scenario.criteria),
            }
            for scenario in scenarios
        ],
    }
