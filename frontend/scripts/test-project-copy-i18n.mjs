import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, extname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const projectRoot = resolve(root, "src/features/projects");
const checkedFiles = [
  ...collectSourceFiles(projectRoot),
  resolve(root, "src/components/SessionViewerDrawer.tsx"),
];

const bannedVisibleCopy = [
  "已连接标准 Web Chat",
  "标准 Web Chat",
  "Human-only",
  "Leader-only",
  "项目 Leader",
  "Project leader",
  "项目所有者",
  "仅所有者",
  "成员快照约束",
  "项目运行时",
  "Project runtime",
  "合并为一轮",
  "coalesced into one",
  "非交互模式",
  "Git 基线",
  "snapshot generation",
  "会话缺少归属 Agent",
  "源 Agent 未改变",
];

for (const path of checkedFiles) {
  const source = readFileSync(path, "utf8");
  for (const copy of bannedVisibleCopy) {
    assert.equal(
      source.includes(`"${copy}`) ||
        source.includes(`'${copy}`) ||
        source.includes(`>${copy}`),
      false,
      `${path} contains internal or obsolete visible copy: ${copy}`,
    );
  }
}

const zh = JSON.parse(readFileSync(resolve(root, "src/i18n/zh.json"), "utf8"));
const en = JSON.parse(readFileSync(resolve(root, "src/i18n/en.json"), "utf8"));
for (const [locale, messages] of [
  ["zh", zh],
  ["en", en],
]) {
  const visibleCopy = leafValues({
    projectTerminology: messages.projectTerminology,
    projectGraphs: messages.projectGraphs,
    projectSnapshot: messages.projectSnapshot,
    projectAudit: messages.projectAudit,
    projectWorkspaceNav: messages.projectWorkspaceNav,
    projectWorkspaceFiles: messages.projectWorkspaceFiles,
    projectAgents: messages.projectAgents,
  }).join("\n");
  for (const copy of bannedVisibleCopy) {
    assert.equal(
      visibleCopy.toLowerCase().includes(copy.toLowerCase()),
      false,
      `${locale} project translations contain internal or obsolete copy: ${copy}`,
    );
  }
}

assert.equal(zh.projectTerminology.owner, "负责人");
assert.equal(en.projectTerminology.owner, "Project owner");
assert.equal(
  zh.projectTerminology.planning.capabilitiesConfirmed,
  "项目能力按已确认的配置生效",
);
assert.equal(
  en.projectTerminology.planning.capabilitiesConfirmed,
  "Project capabilities follow the confirmed configuration",
);
assert.deepEqual(
  leafKeys(zh.projectTerminology),
  leafKeys(en.projectTerminology),
  "project terminology translation keys must stay aligned",
);
assert.deepEqual(
  leafKeys(zh.projectGraphs),
  leafKeys(en.projectGraphs),
  "project graph translation keys must stay aligned",
);
assert.deepEqual(
  leafKeys(zh.projectSnapshot),
  leafKeys(en.projectSnapshot),
  "project snapshot translation keys must stay aligned",
);
const workspaceSource = readFileSync(
  resolve(projectRoot, "ProjectWorkspacePage.tsx"),
  "utf8",
);
assert.doesNotMatch(
  workspaceSource,
  /projectSnapshot\.autonomy|autonomySelectOptions|updateAutonomyField/,
  "project member details must not expose the disconnected autonomy editor",
);
assert.deepEqual(
  leafKeys(zh.projectAudit),
  leafKeys(en.projectAudit),
  "project audit translation keys must stay aligned",
);
assert.deepEqual(
  leafKeys(zh.projectWorkspaceNav),
  leafKeys(en.projectWorkspaceNav),
  "project workspace navigation translation keys must stay aligned",
);
assert.equal(
  zh.projectWorkspaceNav.tabs.projectAgents,
  "数字员工",
  "the team navigation label must name the managed Digital Employees",
);
assert.equal(
  en.projectWorkspaceNav.tabs.projectAgents,
  "Digital Employees",
  "the team navigation label must name the managed Digital Employees",
);
assert.deepEqual(
  leafKeys(zh.projectWorkspaceFiles),
  leafKeys(en.projectWorkspaceFiles),
  "project workspace file translation keys must stay aligned",
);
const zhWorkItemSinglePage =
  zh.projectWorkspacePage.workItems.detail.singlePage;
const enWorkItemSinglePage =
  en.projectWorkspacePage.workItems.detail.singlePage;
assert.deepEqual(
  leafKeys(zhWorkItemSinglePage),
  leafKeys(enWorkItemSinglePage),
  "work-item single-page translation keys must stay aligned",
);
const requiredWorkItemSinglePageKeys = [
  "acceptanceCount",
  "acceptancePassed",
  "acceptancePending",
  "acceptanceTitle",
  "actions.approve",
  "actions.assign",
  "actions.reopen",
  "actions.retry",
  "actions.return",
  "actions.start",
  "actions.viewBlocker",
  "actions.viewDelivery",
  "actions.viewProgress",
  "activity.delivery",
  "activity.discussion",
  "activity.evidence",
  "activity.execution",
  "activity.viewDelivery",
  "activity.viewDiscussion",
  "activity.viewEvidence",
  "activity.viewResult",
  "back",
  "cancel",
  "edit",
  "evidenceCount",
  "evidencePending",
  "goal",
  "next.blocked",
  "next.done",
  "next.inProgress",
  "next.review",
  "next.todo",
  "next.unassigned",
  "nextStep",
  "noDependencies",
  "progressEmpty",
  "progressTitle",
  "requirements",
  "save",
  "viewAllProgress",
].sort();
assert.deepEqual(
  leafKeys(zhWorkItemSinglePage),
  requiredWorkItemSinglePageKeys,
  "work-item single-page copy must expose the complete product contract",
);
for (const [locale, messages] of [
  ["zh", zhWorkItemSinglePage],
  ["en", enWorkItemSinglePage],
]) {
  assert.equal(
    leafValues(messages).some((copy) =>
      /\b(?:Run|Event|Session|Commit|ID)\b/i.test(copy),
    ),
    false,
    `${locale} work-item single-page copy must not expose technical record terms`,
  );
}
assert.deepEqual(
  leafKeys(zh.projectAgents),
  leafKeys(en.projectAgents),
  "project Digital Employee translation keys must stay aligned",
);
assert.equal(zh.projectAgents.badge, "项目专用数字员工");
assert.equal(en.projectAgents.badge, "Project Digital Employee");
assert.equal(zh.projectAgents.eyebrow, "项目数字员工 / 成员");
assert.equal(en.projectAgents.eyebrow, "PROJECT DIGITAL EMPLOYEES / MEMBERS");
assert.equal(zh.projectAgents.create.noSource, "没有可复制的数字员工");
assert.equal(
  en.projectAgents.create.noSource,
  "No Digital Employee is available to copy",
);
const zhTeamPage = zh.projectAgents.teamPage;
const enTeamPage = en.projectAgents.teamPage;
const requiredTeamPageKeys = [
  "active",
  "activeEmptyDescription",
  "activeEmptyTitle",
  "capabilities",
  "capabilitiesDescription",
  "capabilitiesEmptyDescription",
  "capabilitiesEmptyTitle",
  "clearFilters",
  "count",
  "departed",
  "departedEmptyDescription",
  "departedEmptyTitle",
  "description",
  "executionOwner",
  "listView",
  "matrixEmptyDescription",
  "matrixEmptyTitle",
  "matrixView",
  "memberType",
  "noMatchesDescription",
  "noMatchesTitle",
  "noMembersDescription",
  "noMembersTitle",
  "openSettings",
  "projectOwned",
  "readOnly",
  "responsibility",
  "standard",
  "title",
  "viewModeAria",
  "workSettings",
  "workSettingsDescription",
].sort();
assert.deepEqual(
  leafKeys(zhTeamPage),
  leafKeys(enTeamPage),
  "team-page translation keys must stay aligned",
);
assert.deepEqual(
  leafKeys(zhTeamPage),
  requiredTeamPageKeys,
  "team-page copy must expose the complete product contract",
);
for (const [locale, messages] of [
  ["zh", zhTeamPage],
  ["en", enTeamPage],
]) {
  assert.equal(
    leafValues(messages).some((copy) =>
      /\b(?:Agent|Run|Event|Session|Commit|ID)\b|快照代次/i.test(copy),
    ),
    false,
    `${locale} team-page copy must not expose technical implementation terms`,
  );
}
for (const [locale, messages] of [
  ["zh", zh.projectAgents],
  ["en", en.projectAgents],
]) {
  assert.equal(
    leafValues(messages).some((copy) => /\bAgents?\b/.test(copy)),
    false,
    `${locale} project Digital Employee dialogs must not expose the Agent term`,
  );
}
assert.equal(zh.wizard.errors.nameRequired, "数字员工名称不能为空");
assert.equal(
  en.wizard.errors.nameRequired,
  "Digital Employee name is required",
);
assert.equal(zh.projectGraphs.sourceAgent, "源数字员工");
assert.equal(en.projectGraphs.sourceAgent, "Source Digital Employee");
assert.equal(zh.projectWorkspaceFiles.agentRoot, "项目数字员工");
assert.equal(en.projectWorkspaceFiles.agentRoot, "Project Digital Employees");

const planningSource = readFileSync(
  resolve(projectRoot, "ProjectPlanningPage.tsx"),
  "utf8",
);
assert.equal(
  /[\u3400-\u9fff]/u.test(planningSource),
  false,
  "ProjectPlanningPage user copy must use i18n keys",
);

const graphSource = readFileSync(
  resolve(projectRoot, "components/ProjectGraphs.tsx"),
  "utf8",
);
assert.match(
  graphSource,
  /t\("projectGraphs\.meshAria",\s*\{\s*members:[\s\S]*?edges:/,
  "the collaboration graph must pass every meshAria interpolation parameter",
);
assert.match(
  graphSource,
  /t\("projectGraphs\.snapshotAria",\s*\{\s*name:[\s\S]*?runs:/,
  "the member snapshot graph must pass every snapshotAria interpolation parameter",
);
assert.match(
  graphSource,
  /t\("projectGraphs\.snapshotSummary",\s*\{\s*shown:[\s\S]*?total:/,
  "the member snapshot summary must pass every snapshotSummary interpolation parameter",
);

console.log("project copy i18n contract tests passed");

function collectSourceFiles(directory) {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name);
    if (statSync(path).isDirectory()) return collectSourceFiles(path);
    return [".ts", ".tsx"].includes(extname(path)) ? [path] : [];
  });
}

function leafKeys(value, prefix = "") {
  return Object.entries(value)
    .flatMap(([key, child]) => {
      const path = prefix ? `${prefix}.${key}` : key;
      return child && typeof child === "object"
        ? leafKeys(child, path)
        : [path];
    })
    .sort();
}

function leafValues(value) {
  return Object.values(value).flatMap((child) =>
    child && typeof child === "object" ? leafValues(child) : [String(child)],
  );
}
