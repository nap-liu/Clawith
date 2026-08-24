import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const service = readFileSync(
  new URL("../src/services/projects.ts", import.meta.url),
  "utf8",
);
const workspace = readFileSync(
  new URL("../src/features/projects/ProjectWorkspacePage.tsx", import.meta.url),
  "utf8",
);
const viewer = readFileSync(
  new URL(
    "../src/features/projects/components/ProjectGitDiffViewer.tsx",
    import.meta.url,
  ),
  "utf8",
);
const editor = readFileSync(
  new URL(
    "../src/features/projects/components/ProjectCodeEditor.tsx",
    import.meta.url,
  ),
  "utf8",
);

assert.match(
  service,
  /getGitDiff:[\s\S]*\/git\/diff\?\$\{query\}/,
  "projectsApi must expose the provider-neutral Git diff endpoint",
);
assert.match(
  service,
  /if \(params\.parent\) query\.set\(["']parent["']/,
  "explicit merge parent must be supported",
);
assert.match(
  service,
  /if \(params\.path\) query\.set\(["']path["']/,
  "one-file diff must be supported",
);
assert.match(
  workspace,
  /<ProjectGitDiffViewer[\s\S]*commit=/,
  "work-item changes must render the real Git diff viewer",
);
assert.match(
  viewer,
  /projectsApi\s*\.\s*getGitDiff\s*\(\s*projectId,\s*\{\s*commit,\s*path\s*\}\s*\)/,
  "viewer must load immutable commit/path data",
);
assert.match(
  viewer,
  /ProjectCodeDiffEditor/,
  "viewer must reuse the project Monaco diff editor",
);
assert.match(
  editor,
  /<DiffEditor/,
  "project editor must provide Monaco DiffEditor",
);

console.log("project Git diff frontend contract passed");
