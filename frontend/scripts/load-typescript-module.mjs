import { createRequire } from 'node:module';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, extname, resolve } from 'node:path';
import vm from 'node:vm';

const require = createRequire(import.meta.url);
const ts = require('typescript');

export function loadTypeScriptModule(sourcePath, globals = {}) {
    const cache = new Map();

    function load(modulePath) {
        const absolutePath = resolve(modulePath);
        if (cache.has(absolutePath)) return cache.get(absolutePath).exports;

        const source = readFileSync(absolutePath, 'utf8');
        const compiled = ts.transpileModule(source, {
            compilerOptions: {
                module: ts.ModuleKind.CommonJS,
                target: ts.ScriptTarget.ES2020,
                esModuleInterop: true,
            },
        }).outputText;

        const module = { exports: {} };
        cache.set(absolutePath, module);

        const localRequire = (specifier) => {
            if (!specifier.startsWith('.')) return require(specifier);
            const unresolved = resolve(dirname(absolutePath), specifier);
            const candidates = extname(unresolved)
                ? [unresolved]
                : [`${unresolved}.ts`, `${unresolved}.tsx`, `${unresolved}.js`, unresolved];
            const resolvedPath = candidates.find(existsSync);
            if (!resolvedPath) throw new Error(`Cannot resolve ${specifier} from ${absolutePath}`);
            if (resolvedPath.endsWith('.ts') || resolvedPath.endsWith('.tsx')) return load(resolvedPath);
            return require(resolvedPath);
        };

        vm.runInNewContext(compiled, {
            module,
            exports: module.exports,
            require: localRequire,
            console,
            URL,
            URLSearchParams,
            ...globals,
        }, { filename: absolutePath });
        return module.exports;
    }

    return load(sourcePath);
}
