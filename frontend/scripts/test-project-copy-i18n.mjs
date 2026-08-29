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
  "AI Native",
  "A2A Mesh",
  "enabled project owner",
  "Human group messages",
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
const projectKeys = [...new Set([...Object.keys(zh), ...Object.keys(en)])]
  .filter((key) => key.startsWith("project"))
  .sort();

for (const key of projectKeys) {
  assert.ok(zh[key], `zh translations are missing ${key}`);
  assert.ok(en[key], `en translations are missing ${key}`);
  assert.deepEqual(
    canonicalLeafKeys(zh[key]),
    canonicalLeafKeys(en[key]),
    `${key} translation keys must stay aligned`,
  );
}

for (const [locale, messages] of [
  ["zh", zh],
  ["en", en],
]) {
  const visibleCopy = projectKeys.flatMap((key) => leafValues(messages[key]));
  for (const copy of bannedVisibleCopy) {
    assert.equal(
      visibleCopy.some((value) => value.toLowerCase().includes(copy.toLowerCase())),
      false,
      `${locale} project translations contain internal or obsolete copy: ${copy}`,
    );
  }
}

const zhWorkItem = zh.projectWorkspacePage.workItems.detail.singlePage;
const enWorkItem = en.projectWorkspacePage.workItems.detail.singlePage;
for (const [locale, messages] of [
  ["zh", zhWorkItem],
  ["en", enWorkItem],
]) {
  assert.equal(
    leafValues(messages).some((copy) => /\b(?:Run|Event|Session|Commit|ID)\b/i.test(copy)),
    false,
    `${locale} work-item copy must not expose technical record terms`,
  );
}

const zhTeam = zh.projectAgents.teamPage;
const enTeam = en.projectAgents.teamPage;
for (const [locale, messages] of [
  ["zh", zhTeam],
  ["en", enTeam],
]) {
  assert.equal(
    leafValues(messages).some((copy) =>
      /\b(?:Agent|Run|Event|Session|Commit|ID)\b|快照代次/i.test(copy),
    ),
    false,
    `${locale} team copy must not expose technical implementation terms`,
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

assert.equal(zh.projectWorkspaceNav.tabs.projectAgents, "数字员工");
assert.equal(en.projectWorkspaceNav.tabs.projectAgents, "Digital Employees");
assert.match(zh.projectGraphs.sourceAgent, /数字员工/);
assert.match(en.projectGraphs.sourceAgent, /Digital Employee/);
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

console.log("project user-visible copy and i18n check passed");

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

function canonicalLeafKeys(value) {
  return [...new Set(leafKeys(value).map((key) => key.replace(/_(?:one|other)$/, "")))].sort();
}
