import { type ConversationTimelineProps } from "../../features/conversation/web/ConversationTimeline";
import {
  type buildChatAttachmentPayload,
  type ChatPreviewImage,
} from "../../utils/chatAttachments";

export type SessionViewerTarget = {
  sessionId: string;
  /** Durable ChatMessage id for the exact project Run/event turn. */
  anchorMessageId?: string;
  /** Lets the viewer resolve a child-session anchor from durable message metadata. */
  projectRunId?: string;
  agentId?: string;
  title?: string;
  status?: string;
  mode?: string;
  model?: string;
  /** Force a historical or frozen session into view-only mode. */
  readOnly?: boolean;
};

export type SessionViewerGroupConfig = {
  members: Array<{
    agentId: string;
    name: string;
    isLeader?: boolean;
    isEnabled?: boolean;
  }>;
  currentAgentId?: string;
  maxMentions?: number;
  loadMessages: (
    sessionId: string,
    options?: { before?: string; limit?: number },
  ) => Promise<{
    items: unknown[];
    hasMore: boolean;
    nextCursor: string | null;
    turn?: Record<string, any>;
  }>;
  sendMessage: (
    sessionId: string,
    payload: {
      content: string;
      llm_content?: string;
      mentions: string[];
      attachments: ReturnType<typeof buildChatAttachmentPayload>["attachments"];
      client_message_id?: string;
    },
  ) => Promise<{
    message?: Record<string, any>;
    awakened_agent_ids?: string[];
    subagent_runs?: Array<{
      project_run_id?: string;
      run_id: string | null;
      session_id: string | null;
      agent_id: string;
      status: string;
      error?: string;
    }>;
    turn?: Record<string, any>;
  }>;
};

type GroupPendingRun = {
  projectRunId: string;
  agentId: string;
  status: string;
  createdAt: number;
  anchorMessageId: string;
};

export type GroupTurnState = {
  phase: "active" | "expired";
  anchorMessageId: string;
  agentIds: string[];
  runCount: number;
};

export const GROUP_RUN_ACTIVE_STATUSES = new Set([
  "queued",
  "pending",
  "waiting",
  "running",
  "processing",
]);
const GROUP_PENDING_TIMEOUT_MS = 45 * 60 * 1000;

function groupMessageMetadata(row: unknown): Record<string, any> {
  if (!row || typeof row !== "object") return {};
  const record = row as Record<string, any>;
  const metadata = record.metadata || record.message_meta;
  return metadata && typeof metadata === "object" && !Array.isArray(metadata)
    ? metadata
    : {};
}

/**
 * Rebuild the pending group turn from the durable group timeline. ProjectRun
 * ids are recorded on the Human anchor and echoed by the Agent reply, so this
 * survives a reload without treating ordinary group messages as a broadcast.
 */
export function deriveGroupTurnState(
  rows: unknown[],
  now = Date.now(),
  timeoutMs = GROUP_PENDING_TIMEOUT_MS,
): GroupTurnState | null {
  const completedProjectRunIds = new Set<string>();
  for (const row of rows) {
    const metadata = groupMessageMetadata(row);
    const sourceIds = Array.isArray(metadata.source_project_run_ids)
      ? metadata.source_project_run_ids
      : Array.isArray(metadata.project_run_ids)
        ? metadata.project_run_ids
        : [];
    sourceIds.forEach((id: unknown) => {
      const value = String(id || "").trim();
      if (value) completedProjectRunIds.add(value);
    });
  }

  const pending: GroupPendingRun[] = [];
  const expired: GroupPendingRun[] = [];
  let latestRunAnchorAt = 0;
  for (const row of rows) {
    if (!row || typeof row !== "object") continue;
    const record = row as Record<string, any>;
    const metadata = groupMessageMetadata(record);
    const subagentRuns = Array.isArray(metadata.subagent_runs)
      ? metadata.subagent_runs
      : [];
    if (!subagentRuns.length) continue;
    const createdAt = Date.parse(String(record.created_at || "")) || now;
    const anchorId = String(record.id || "");
    latestRunAnchorAt = Math.max(latestRunAnchorAt, createdAt);
    for (const rawRun of subagentRuns) {
      if (!rawRun || typeof rawRun !== "object") continue;
      const run = rawRun as Record<string, any>;
      const status = String(run.status || "").toLowerCase();
      const projectRunId = String(run.project_run_id || "").trim();
      const agentId = String(run.agent_id || "").trim();
      if (
        !projectRunId ||
        !agentId ||
        !GROUP_RUN_ACTIVE_STATUSES.has(status) ||
        completedProjectRunIds.has(projectRunId)
      )
        continue;
      const candidate = {
        projectRunId,
        agentId,
        status,
        createdAt,
        anchorMessageId: anchorId,
      };
      if (now - createdAt > timeoutMs) expired.push(candidate);
      else pending.push(candidate);
    }
  }

  // An old timed-out anchor should not keep warning forever once a newer
  // group turn has reached a reply. Active runs can span several anchors,
  // but an expiry notice only belongs to the latest durable turn.
  const source = pending.length
    ? pending
    : expired.filter((run) => run.createdAt === latestRunAnchorAt);
  if (!source.length) return null;
  const latestSource = source.reduce((latest, run) =>
    run.createdAt >= latest.createdAt ? run : latest,
  );
  return {
    phase: pending.length ? "active" : "expired",
    anchorMessageId: latestSource.anchorMessageId,
    agentIds: Array.from(new Set(source.map((run) => run.agentId))),
    runCount: source.length,
  };
}

export type SessionViewerDrawerProps = {
  agentId: string;
  agentName: string;
  target: SessionViewerTarget | null;
  routeMode?: "pc" | "h5";
  portalContainer?: HTMLElement | null;
  interactive?: boolean;
  /** Render the same timeline/composer inside the current page instead of an overlay drawer. */
  embedded?: boolean;
  groupConfig?: SessionViewerGroupConfig;
  onClose: () => void;
  onPreviewImages?: (images: ChatPreviewImage[], index: number) => void;
  unavailableAttachmentKeys?: ReadonlySet<string>;
  onAttachmentDownload?: ConversationTimelineProps["onAttachmentDownload"];
  onAttachmentUnavailable?: ConversationTimelineProps["onAttachmentUnavailable"];
};
