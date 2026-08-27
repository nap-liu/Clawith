import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const service = readFileSync(
  new URL("../src/services/projects.ts", import.meta.url),
  "utf8",
);
const workspace = readFileSync(
  new URL(
    "../src/features/projects/components/ProjectFileWorkspace.tsx",
    import.meta.url,
  ),
  "utf8",
);
const styles = readFileSync(
  new URL(
    "../src/features/projects/components/ProjectFileWorkspace.css",
    import.meta.url,
  ),
  "utf8",
);
const workspaceStyles = readFileSync(
  new URL("../src/features/projects/projectWorkspace.css", import.meta.url),
  "utf8",
);
const codeEditor = readFileSync(
  new URL(
    "../src/features/projects/components/ProjectCodeEditor.tsx",
    import.meta.url,
  ),
  "utf8",
);
const workspaceRouting = readFileSync(
  new URL(
    "../src/features/projects/projectWorkspaceRouting.ts",
    import.meta.url,
  ),
  "utf8",
);
const workspacePage = readFileSync(
  new URL("../src/features/projects/ProjectWorkspacePage.tsx", import.meta.url),
  "utf8",
);
const projectTypes = readFileSync(
  new URL("../src/features/projects/types.ts", import.meta.url),
  "utf8",
);

assert.match(
  service,
  /getDirectoryArchive:[\s\S]*\/files\/archive\?\$\{query\}/,
  "directory downloads must use the archive ticket endpoint",
);
assert.match(
  workspace,
  /node\.kind\s*===\s*["']folder["'][\s\S]*getDirectoryArchive\s*\(\s*projectId,\s*node\.path,?\s*\)/,
  "folder rows must download immutable server archives",
);
assert.match(
  workspace,
  /getFileContent\s*\(\s*projectId,\s*node\.path,\s*1\s*\)/,
  "file rows must reuse signed raw file downloads",
);
assert.match(
  workspace,
  /sandbox="allow-scripts"/,
  "HTML previews must run in an opaque sandbox",
);
assert.doesNotMatch(
  workspace,
  /allow-same-origin/,
  "HTML previews must never share the application origin",
);
assert.match(
  workspace,
  /content\.html_preview_url/,
  "HTML preview must use the server snapshot route",
);
assert.match(codeEditor, /md:\s*["']markdown["']/);
assert.match(codeEditor, /markdown:\s*["']markdown["']/);
assert.match(
  codeEditor,
  /language=\{languageForPath\(path\)\}/,
  "Markdown files must open in Monaco with the standard markdown language",
);
assert.match(
  workspace,
  /markdownPreview[\s\S]*ProjectCodeEditor[\s\S]*readOnly/,
  "Markdown preview and source modes must share Monaco instead of a second renderer",
);
assert.match(
  workspaceRouting,
  /files:\s*\[[^\]]*["']file["'][^\]]*["']fileView["']/,
  "Markdown preview state must survive refresh in the file tab URL",
);
assert.match(
  styles,
  /project-file-workspace__tree[\s\S]*overflow-y: auto/,
  "the directory tree must own vertical scrolling",
);
assert.match(
  workspaceStyles,
  /--project-navigation-font-size:\s*[^;]+;[\s\S]*project-workspace__nav section > button[\s\S]*font-size:\s*var\(--project-navigation-font-size\)/,
  "project navigation must define and consume one shared typography token",
);
assert.match(
  styles,
  /project-file-workspace__tree-row[\s\S]*font:\s*500 var\(--project-navigation-font-size\)/,
  "file rows must use the same typography token as project navigation",
);
assert.match(
  styles,
  /project-file-workspace__tree-row[\s\S]*height:\s*30px[\s\S]*grid-template-columns:\s*13px 16px minmax\(0, 1fr\)/,
  "file rows and icons must keep the compact workspace density",
);
assert.match(
  styles,
  /project-file-workspace__tree-download[\s\S]*opacity:\s*0[\s\S]*visibility:\s*hidden[\s\S]*project-file-workspace__tree-item:hover[\s\S]*opacity:\s*1[\s\S]*visibility:\s*visible/,
  "row download actions must stay hidden until hover or keyboard focus",
);
assert.match(
  workspace,
  /node\.path\s*===\s*["']\.agents["'][\s\S]*projectAgentNames\.get/,
  "the workspace tree must present project Agent assets by Agent name",
);
assert.match(
  workspacePage,
  /projectWorkspaceNav\.tabs\.workspace/,
  "the project file area must be exposed as the workspace",
);
assert.match(
  workspacePage,
  /id:\s*["']members["'][\s\S]{0,180}labelKey:\s*["']projectWorkspaceNav\.tabs\.projectAgents["']/,
  "the project member label must keep the compatible members tab key",
);
assert.match(
  workspacePage,
  /createProjectAgent[\s\S]*updateProjectAgent[\s\S]*promoteProjectAgent/,
  "the project Agent drawer must expose create, edit, and promotion actions",
);
assert.match(
  workspacePage,
  /openPromoteDialog[\s\S]*projectAgents\.actions\.promote/,
  "the selected Project Digital Employee must expose one direct upgrade action",
);
assert.match(
  workspacePage,
  /<ProjectDialog[\s\S]*open=\{promoteDialogOpen\}[\s\S]*projectAgents\.promotion\.confirmTitle/,
  "Project Digital Employee upgrades must use the shared dialog and overlay",
);
assert.match(
  workspacePage,
  /onOpenWorkspace\(`\$\{projectAgent\.agent_dir\}\/soul\.md`\)/,
  "project Agent assets must open directly in the project workspace",
);
assert.match(
  projectTypes,
  /interface ProjectOwnedAgent[\s\S]*agent_dir:\s*string/,
  "project Agent API data must include its workspace asset root",
);

console.log("project file workspace download and preview contract passed");
