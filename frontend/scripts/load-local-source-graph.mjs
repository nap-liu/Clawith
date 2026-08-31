import { existsSync, readFileSync, realpathSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";

const sourceExtensions = [".ts", ".tsx", ".js", ".jsx"];

function resolveLocalImport(importerPath, specifier) {
  const basePath = resolve(dirname(importerPath), specifier);
  const candidates = [
    basePath,
    ...sourceExtensions.map((extension) => `${basePath}${extension}`),
    ...sourceExtensions.map((extension) => resolve(basePath, `index${extension}`)),
  ];
  return candidates.find(
    (candidate) => existsSync(candidate) && statSync(candidate).isFile(),
  );
}

export function loadLocalSourceGraph(entryPath) {
  const visited = new Set();
  const sources = [];

  function visit(sourcePath) {
    const canonicalPath = realpathSync(sourcePath);
    if (visited.has(canonicalPath)) return;
    visited.add(canonicalPath);

    const source = readFileSync(canonicalPath, "utf8");
    sources.push(source);
    const importPattern = /(?:import|export)\s+(?:[\s\S]*?\s+from\s+)?["']([^"']+)["']/g;
    for (const match of source.matchAll(importPattern)) {
      if (!match[1].startsWith(".")) continue;
      const dependencyPath = resolveLocalImport(canonicalPath, match[1]);
      if (dependencyPath) visit(dependencyPath);
    }
  }

  visit(entryPath);
  return sources.join("\n");
}
