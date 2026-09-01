const WORKSPACE_ROOT = 'workspace';
const SKILLS_ROOT = 'skills';
const MEMORY_ROOT = 'memory';
const ENTERPRISE_ROOT = 'enterprise_info';
const DEFAULT_UPLOAD_DIR = 'workspace/uploads';
const EDITABLE_EXTS = new Set(['.md', '.markdown', '.csv']);
const IMAGE_EXTS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg']);
const PREVIEW_EXTS = new Set(['.md', '.markdown', '.csv', '.html', '.htm', '.pdf', '.xlsx', '.xls', '.docx', '.doc', '.pptx', '.ppt', '.txt', '.log', '.json', ...IMAGE_EXTS]);
const MIN_SAVING_VISIBLE_MS = 650;
const SAVED_VISIBLE_MS = 1600;
const DEFAULT_TREE_WIDTH = 240;
const DEFAULT_HISTORY_WIDTH = 320;
const MIN_SIDE_WIDTH = 220;
const MAX_SIDE_WIDTH = 520;

function extOf(path: string): string {
    const idx = path.lastIndexOf('.');
    return idx >= 0 ? path.slice(idx).toLowerCase() : '';
}

function parseCsv(text: string): string[][] {
    const nonEmpty = text
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter(Boolean)
        .slice(0, 10);
    const delimiters = [',', '，', ';', '\t', '|'];
    const delimiter = delimiters
        .map((candidate) => ({
            candidate,
            score: nonEmpty.reduce((total, line) => total + (line.split(candidate).length - 1), 0),
        }))
        .sort((a, b) => b.score - a.score)[0]?.candidate || ',';
    const rows: string[][] = [];
    let row: string[] = [];
    let cell = '';
    let quoted = false;
    for (let i = 0; i < text.length; i += 1) {
        const ch = text[i];
        const next = text[i + 1];
        if (ch === '"' && quoted && next === '"') {
            cell += '"';
            i += 1;
        } else if (ch === '"') {
            quoted = !quoted;
        } else if (ch === delimiter && !quoted) {
            row.push(cell);
            cell = '';
        } else if ((ch === '\n' || ch === '\r') && !quoted) {
            if (ch === '\r' && next === '\n') i += 1;
            row.push(cell);
            rows.push(row);
            row = [];
            cell = '';
        } else {
            cell += ch;
        }
    }
    if (cell || row.length) {
        row.push(cell);
        rows.push(row);
    }
    return rows;
}

function fileName(path: string): string {
    return path.split('/').pop() || path;
}

function isPreviewable(path: string): boolean {
    return PREVIEW_EXTS.has(extOf(path));
}

function parentDirs(path?: string | null): string[] {
    if (!path) return [WORKSPACE_ROOT];
    const parts = path.split('/').filter(Boolean);
    const dirs: string[] = [];
    for (let i = 0; i < parts.length - 1; i += 1) {
        dirs.push(parts.slice(0, i + 1).join('/'));
    }
    if (!dirs.length) dirs.push(WORKSPACE_ROOT);
    return dirs;
}

function isWorkspacePath(path?: string | null): boolean {
    return !!path && (path === WORKSPACE_ROOT || path.startsWith(`${WORKSPACE_ROOT}/`));
}

function removeWorkspaceExpansion(dirs: Set<string>): Set<string> {
    const next = new Set(dirs);
    Array.from(next).forEach((dir) => {
        if (isWorkspacePath(dir)) next.delete(dir);
    });
    return next;
}

function parentDir(path?: string | null): string {
    if (!path || !path.startsWith(`${WORKSPACE_ROOT}/`)) return WORKSPACE_ROOT;
    const parts = path.split('/');
    return parts.length > 1 ? parts.slice(0, -1).join('/') : WORKSPACE_ROOT;
}

function directoryOf(path?: string | null): string {
    if (!path) return WORKSPACE_ROOT;
    const parts = path.split('/').filter(Boolean);
    return parts.length > 1 ? parts.slice(0, -1).join('/') : WORKSPACE_ROOT;
}

function isWritableDir(path?: string | null): boolean {
    if (!path) return false;
    return path === WORKSPACE_ROOT
        || path === SKILLS_ROOT
        || path.startsWith(`${WORKSPACE_ROOT}/`)
        || path.startsWith(`${SKILLS_ROOT}/`);
}

function isEnterprisePath(path?: string | null): boolean {
    return !!path && (path === ENTERPRISE_ROOT || path.startsWith(`${ENTERPRISE_ROOT}/`));
}

function normalizeWritableDir(path?: string | null): string {
    if (isWritableDir(path)) return path as string;
    return DEFAULT_UPLOAD_DIR;
}

function formatRevisionTime(value?: string | null): string {
    if (!value) return '';
    const dt = new Date(value);
    if (Number.isNaN(dt.getTime())) return '';
    const mm = String(dt.getMonth() + 1).padStart(2, '0');
    const dd = String(dt.getDate()).padStart(2, '0');
    const hh = String(dt.getHours()).padStart(2, '0');
    const min = String(dt.getMinutes()).padStart(2, '0');
    return `${mm}-${dd} ${hh}:${min}`;
}

function buildPreviewVersion(content: string): string {
    let hash = 0;
    for (let i = 0; i < content.length; i += 1) {
        hash = (hash * 31 + content.charCodeAt(i)) >>> 0;
    }
    return `${content.length}-${hash}`;
}

function trimTrailingEmpty(row: string[]): string[] {
    const next = [...row];
    while (next.length && !String(next[next.length - 1] || '').trim()) next.pop();
    return next;
}

function buildRevisionDiff(revision: any): string {
    const before = revision.before_content ?? '';
    const after = revision.after_content ?? '';
    if (!before && !after) return 'No preview available for this revision.';
    if (before === after) {
        if (revision.operation === 'restore') return 'Restored this snapshot.';
        if (revision.operation === 'autosave') return 'Autosaved with no textual changes.';
        return 'No textual changes in this revision.';
    }

    const beforeLines = before.split('\n');
    const afterLines = after.split('\n');

    let prefix = 0;
    while (
        prefix < beforeLines.length &&
        prefix < afterLines.length &&
        beforeLines[prefix] === afterLines[prefix]
    ) {
        prefix += 1;
    }

    let suffix = 0;
    while (
        suffix < beforeLines.length - prefix &&
        suffix < afterLines.length - prefix &&
        beforeLines[beforeLines.length - 1 - suffix] === afterLines[afterLines.length - 1 - suffix]
    ) {
        suffix += 1;
    }

    const removed = beforeLines.slice(prefix, beforeLines.length - suffix);
    const added = afterLines.slice(prefix, afterLines.length - suffix);
    const chunks: string[] = [];

    if (removed.length) {
        chunks.push(...removed.map((line: string) => `- ${line}`));
    }
    if (added.length) {
        chunks.push(...added.map((line: string) => `+ ${line}`));
    }

    if (!chunks.length) {
        chunks.push(`Before:\n${before || '(empty)'}`, `After:\n${after || '(empty)'}`);
    }

    return chunks.join('\n');
}

export {
    WORKSPACE_ROOT,
    SKILLS_ROOT,
    MEMORY_ROOT,
    ENTERPRISE_ROOT,
    DEFAULT_UPLOAD_DIR,
    EDITABLE_EXTS,
    IMAGE_EXTS,
    PREVIEW_EXTS,
    MIN_SAVING_VISIBLE_MS,
    SAVED_VISIBLE_MS,
    DEFAULT_TREE_WIDTH,
    DEFAULT_HISTORY_WIDTH,
    MIN_SIDE_WIDTH,
    MAX_SIDE_WIDTH,
    extOf,
    parseCsv,
    fileName,
    isPreviewable,
    parentDirs,
    isWorkspacePath,
    removeWorkspaceExpansion,
    parentDir,
    directoryOf,
    isWritableDir,
    isEnterprisePath,
    normalizeWritableDir,
    formatRevisionTime,
    buildPreviewVersion,
    trimTrailingEmpty,
    buildRevisionDiff,
};
