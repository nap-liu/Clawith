import { readFileSync, realpathSync } from 'node:fs';
import { dirname, resolve } from 'node:path';

const LOCAL_IMPORT = /@import\s+(['"])([^'"]+)\1\s*;/g;

export function loadCssEntry(entryPath, activePaths = new Set()) {
    const resolvedPath = realpathSync(entryPath);
    if (activePaths.has(resolvedPath)) {
        throw new Error(`circular CSS import: ${resolvedPath}`);
    }

    const nextActivePaths = new Set(activePaths);
    nextActivePaths.add(resolvedPath);
    return readFileSync(resolvedPath, 'utf8').replace(
        LOCAL_IMPORT,
        (_statement, _quote, importPath) => loadCssEntry(
            resolve(dirname(resolvedPath), importPath),
            nextActivePaths,
        ),
    );
}
