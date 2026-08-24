type TraceRecord = Record<string, unknown>;

const ANCHOR_KEYS = [
  "anchor_message_id",
  "message_id",
  "message_anchor_id",
  "assistant_message_id",
  "response_message_id",
  "tool_message_id",
  "tool_call_id",
  "source_message_id",
  "request_message_id",
  "group_message_id",
  "subagent_turn_anchor_id",
  "turn_anchor_id",
  "origin_turn_anchor_id",
] as const;

const RUN_KEYS = ["project_run_id", "run_id", "subagent_run_id"] as const;

function recordsOf(value: unknown): TraceRecord[] {
  const records: TraceRecord[] = [];
  const seen = new Set<TraceRecord>();
  const visit = (candidate: unknown, depth: number) => {
    if (depth > 4 || !candidate) return;
    if (typeof candidate === "string") {
      const start = candidate.indexOf("{");
      if (start < 0) return;
      try {
        visit(JSON.parse(candidate.slice(start)), depth + 1);
      } catch {
        return;
      }
      return;
    }
    if (Array.isArray(candidate)) {
      candidate.forEach((item) => visit(item, depth + 1));
      return;
    }
    if (typeof candidate !== "object") return;
    const record = candidate as TraceRecord;
    if (seen.has(record)) return;
    seen.add(record);
    records.push(record);
    Object.values(record).forEach((item) => visit(item, depth + 1));
  };
  visit(value, 0);
  return records;
}

function values(records: TraceRecord[], keys: readonly string[]): string[] {
  return records.flatMap((record) =>
    keys.flatMap((key) => {
      const value = record[key];
      if (Array.isArray(value)) {
        return value
          .filter(
            (item) => typeof item === "string" || typeof item === "number",
          )
          .map(String);
      }
      return typeof value === "string" || typeof value === "number"
        ? [String(value)]
        : [];
    }),
  );
}

function durableMessageId(row: TraceRecord): string {
  const value =
    row.role === "tool_call"
      ? row.toolCallId || row.tool_call_id || row.id
      : row.id || row.message_id;
  return typeof value === "string" || typeof value === "number"
    ? String(value)
    : "";
}

/**
 * Maps a project turn/run anchor to the durable message rendered by the
 * standard conversation timeline. This is required when several Runs share
 * one session and the project DTO stores a turn id in nested metadata.
 */
export function resolveConversationMessageAnchor(
  rows: TraceRecord[],
  requestedAnchor?: string,
  projectRunId?: string,
): string {
  const anchor = String(requestedAnchor || "").trim();
  const runId = String(projectRunId || "").trim();
  return (
    rows
      .map((row, index) => {
        const records = recordsOf(row);
        const messageId = durableMessageId(row);
        const anchorValues = values(records, ANCHOR_KEYS);
        const directRuns = values(records, RUN_KEYS);
        const relatedRuns = values(records, [
          "project_run_ids",
          "source_project_run_ids",
          "related_run_ids",
        ]);
        const directAnchor = Boolean(anchor && messageId === anchor);
        const metadataAnchor = Boolean(
          anchor && !directAnchor && anchorValues.includes(anchor),
        );
        const exactRun = Boolean(runId && directRuns.includes(runId));
        const relatedRun = Boolean(runId && relatedRuns.includes(runId));
        return {
          messageId,
          index,
          score: directAnchor
            ? 125
            : metadataAnchor
              ? 100
              : exactRun
                ? 50
                : relatedRun
                  ? 25
                  : 0,
        };
      })
      .filter((candidate) => candidate.messageId && candidate.score > 0)
      .sort(
        (left, right) => right.score - left.score || left.index - right.index,
      )[0]?.messageId || ""
  );
}
