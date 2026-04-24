/**
 * Parser for the `<persisted-output>` block emitted by the backend's
 * tool-output materialization layer (P0).
 *
 * When a tool result exceeds the per-tool size threshold the backend replaces
 * the raw result with an envelope like:
 *
 *     <persisted-output>
 *     Output too large (80.4 KB). Full output saved to: .tool_results/<sid>/<tool>_<call_id>.json
 *
 *     Preview (first 2,000 chars):
 *     {"chunks":[{"content":"..."
 *     ...
 *
 *     Use read_file to access full content, or grep/search_files to find specific content.
 *     </persisted-output>
 *
 * This module recognises and decomposes that envelope so the UI can render
 * a proper "persisted tool output" card (with a read-file CTA) instead of a
 * truncated raw dump.
 */

export interface PersistedOutput {
    /** Human-readable size label, e.g. "80.4 KB" */
    sizeLabel: string;
    /** Relative path under the agent workspace, e.g. `.tool_results/<sid>/<tool>_<call_id>.json` */
    filePath: string;
    /** Preview body extracted between the `Preview (...)` header and the trailing hint. */
    preview: string;
}

/**
 * Try to parse a tool-result string as a `<persisted-output>` envelope.
 * Returns null when the string is not an envelope or any required field is missing.
 *
 * The parser is intentionally lenient:
 *   - leading/trailing whitespace is allowed around the outer tag
 *   - the preview body may itself contain XML-like tokens ("<tag>", etc.)
 *   - the file path may contain UUIDs, dots, and non-ASCII characters
 *   - the trailing "..." after the preview (indicating backend truncation) is
 *     stripped from the returned `preview` field
 */
export function parsePersistedOutput(raw: string): PersistedOutput | null {
    if (typeof raw !== 'string' || raw.length === 0) return null;

    const trimmed = raw.trim();
    if (!trimmed.startsWith('<persisted-output>') || !trimmed.endsWith('</persisted-output>')) {
        return null;
    }

    // Strip the outer tags. Use slice with known lengths so nested "<...>" tokens
    // inside the preview do not confuse the extractor.
    const open = '<persisted-output>';
    const close = '</persisted-output>';
    const inner = trimmed.slice(open.length, trimmed.length - close.length).trim();
    if (!inner) return null;

    // Size label: "Output too large (<size>)."
    const sizeMatch = inner.match(/Output too large \(([^)]+)\)\./);
    if (!sizeMatch) return null;
    const sizeLabel = sizeMatch[1].trim();
    if (!sizeLabel) return null;

    // File path: "Full output saved to: <path>" — path runs until end-of-line.
    const pathMatch = inner.match(/Full output saved to:\s*([^\r\n]+)/);
    if (!pathMatch) return null;
    const filePath = pathMatch[1].trim();
    if (!filePath) return null;

    // Preview body: between `Preview (...)` header line and the trailing
    // "Use read_file" hint. Use non-greedy match with a lookahead so any
    // XML-like tokens inside the preview do not break extraction.
    const previewMatch = inner.match(/Preview \([^)]*\):\s*\n([\s\S]*?)\n\s*Use read_file\b/);
    if (!previewMatch) return null;

    let preview = previewMatch[1];
    // Drop a single trailing "..." line if present (backend indicates truncation).
    preview = preview.replace(/\n\s*\.{3,}\s*$/, '');
    // Do not trim internal whitespace — preserve formatting — but strip trailing newlines only.
    preview = preview.replace(/\s+$/, '');

    if (!preview) return null;

    return { sizeLabel, filePath, preview };
}

/** Extract the basename of a path (platform-agnostic, forward-slash based). */
export function persistedOutputBasename(filePath: string): string {
    if (!filePath) return '';
    const idx = filePath.lastIndexOf('/');
    return idx === -1 ? filePath : filePath.slice(idx + 1);
}

// ─── Dev-only smoke assertions ────────────────────────────
// These run once on module import in dev builds. They document the parser's
// contract and guard against accidental regressions. Stripped in prod.
if (import.meta.env?.DEV) {
    const valid = `<persisted-output>
Output too large (80.4 KB). Full output saved to: .tool_results/bc8272aa-1234-5678-9abc-def012345678/mcp_ragflow_retrieval_call_abc.json

Preview (first 2,000 chars):
{"chunks":[{"content":"9-1、杯贴\n问题:..."
...

Use read_file to access full content, or grep/search_files to find specific content.
</persisted-output>`;

    const parsed = parsePersistedOutput(valid);
    console.assert(parsed !== null, '[persistedOutput] valid envelope should parse');
    console.assert(parsed?.sizeLabel === '80.4 KB', '[persistedOutput] sizeLabel mismatch');
    console.assert(
        parsed?.filePath === '.tool_results/bc8272aa-1234-5678-9abc-def012345678/mcp_ragflow_retrieval_call_abc.json',
        '[persistedOutput] filePath mismatch',
    );
    console.assert(
        parsed?.preview.startsWith('{"chunks":'),
        '[persistedOutput] preview should start with chunk payload',
    );
    console.assert(!parsed?.preview.endsWith('...'), '[persistedOutput] trailing ellipsis not stripped');

    const noClose = `<persisted-output>
Output too large (10 KB). Full output saved to: .tool_results/x.json

Preview (first 2,000 chars):
body
...

Use read_file to access full content.`;
    console.assert(parsePersistedOutput(noClose) === null, '[persistedOutput] missing close tag must return null');

    console.assert(parsePersistedOutput('just a plain string without tags') === null, '[persistedOutput] plain text must return null');
    console.assert(parsePersistedOutput('') === null, '[persistedOutput] empty string must return null');

    console.assert(
        persistedOutputBasename('.tool_results/sid/tool_call.json') === 'tool_call.json',
        '[persistedOutput] basename mismatch',
    );
}
