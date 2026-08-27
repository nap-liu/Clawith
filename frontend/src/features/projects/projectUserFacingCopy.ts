import type { TFunction } from "i18next";

const BUILTIN_TEMPLATE_COPY_KEYS: Record<string, string> = {
  产品研发冲刺: "projectTemplates.builtin.productSprint.name",
  "由负责人驱动需求、研发、测试与交付闭环。":
    "projectTemplates.builtin.productSprint.description",
  市场洞察研究: "projectTemplates.builtin.marketResearch.name",
  "并行采集、交叉验证并形成有证据链的研究报告。":
    "projectTemplates.builtin.marketResearch.description",
  内容发布流水线: "projectTemplates.builtin.contentPipeline.name",
  "从选题、创作、审校到多渠道发布的协作模板。":
    "projectTemplates.builtin.contentPipeline.description",
  研发: "projectTemplates.builtin.categories.development",
  研究: "projectTemplates.builtin.categories.research",
  内容: "projectTemplates.builtin.categories.content",
  产品设计: "projectTemplates.builtin.roles.productDesign",
  前端开发: "projectTemplates.builtin.roles.frontendDevelopment",
  后端开发: "projectTemplates.builtin.roles.backendDevelopment",
  质量工程: "projectTemplates.builtin.roles.qualityEngineering",
  研究负责人: "projectTemplates.builtin.roles.researchOwner",
  情报分析: "projectTemplates.builtin.roles.intelligenceAnalysis",
  事实核查: "projectTemplates.builtin.roles.factChecking",
  报告编辑: "projectTemplates.builtin.roles.reportEditing",
  内容负责人: "projectTemplates.builtin.roles.contentOwner",
  作者: "projectTemplates.builtin.roles.author",
  审校: "projectTemplates.builtin.roles.review",
  发布运营: "projectTemplates.builtin.roles.publishingOperations",
  需求拆解: "projectTemplates.builtin.capabilities.requirementBreakdown",
  代码评审: "projectTemplates.builtin.capabilities.codeReview",
  深度研究: "projectTemplates.builtin.capabilities.deepResearch",
  内容创作: "projectTemplates.builtin.capabilities.contentCreation",
  品牌审校: "projectTemplates.builtin.capabilities.brandReview",
  平台模板: "projectTemplates.platformTemplate",
  负责人: "projectTerminology.projectOwner",
  通用: "projectTemplates.builtin.categories.general",
  未命名模板: "projectTemplates.unnamedTemplate",
  未命名能力: "projectTemplates.unnamedCapability",
};

/**
 * Normalizes legacy project terminology at the presentation boundary.
 *
 * Templates and agent role snapshots can outlive the seed copy that created
 * them, so existing records may still contain the former Leader / Agent
 * wording. Keep the persisted values intact and translate only what users see.
 */
export function projectUserFacingCopy(value: string, t: TFunction): string {
  const localizedValue = BUILTIN_TEMPLATE_COPY_KEYS[value]
    ? t(BUILTIN_TEMPLATE_COPY_KEYS[value])
    : value;
  return localizedValue
    .replace(/项目负责人/g, t("projectTerminology.dynamicCopy.projectOwner"))
    .replace(
      /项目专用\s*Agent/gi,
      t("projectTerminology.dynamicCopy.projectDigitalEmployee"),
    )
    .replace(
      /项目\s*Agent/gi,
      t("projectTerminology.dynamicCopy.projectDigitalEmployee"),
    )
    .replace(
      /(?:源|来源)\s*Agent/gi,
      t("projectTerminology.dynamicCopy.sourceDigitalEmployee"),
    )
    .replace(
      /项目\s*Leader/gi,
      t("projectTerminology.dynamicCopy.projectOwner"),
    )
    .replace(
      /\bProject\s+Agent\b/gi,
      t("projectTerminology.dynamicCopy.projectDigitalEmployee"),
    )
    .replace(
      /\bSource\s+Agent\b/gi,
      t("projectTerminology.dynamicCopy.sourceDigitalEmployee"),
    )
    .replace(
      /\bProject\s+Leader\b/gi,
      t("projectTerminology.dynamicCopy.projectOwner"),
    )
    .replace(/\bLeader\b/gi, t("projectTerminology.dynamicCopy.owner"))
    .replace(/\bClawith\b/gi, t("projectTerminology.dynamicCopy.platform"))
    .replace(
      /\bAgents\b/gi,
      t("projectTerminology.dynamicCopy.digitalEmployees"),
    )
    .replace(
      /\bAgent\b/gi,
      t("projectTerminology.dynamicCopy.digitalEmployee"),
    );
}
