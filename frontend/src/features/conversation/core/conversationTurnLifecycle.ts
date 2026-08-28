export type ConversationTurnPhase = "idle" | "active" | "suspended";
export type ConversationTurnPresentation =
  | "idle"
  | "waiting"
  | "streaming"
  | "suspended";

export type ConversationTurnSnapshot = {
  turnAnchorId: string | null;
  generation: number;
  revision: number;
  status: string;
  phase: ConversationTurnPhase;
};

export type ConversationTurnRuntime = {
  snapshot: ConversationTurnSnapshot;
  presentation: ConversationTurnPresentation;
  canonical: boolean;
};

export type ConversationTurnReduction = {
  accepted: boolean;
  runtime: ConversationTurnRuntime;
  hasSnapshot: boolean;
  controlsLifecycle: boolean;
  deliversTimeline: boolean;
  deliversTransport: boolean;
};

export const IDLE_CONVERSATION_TURN: ConversationTurnRuntime = {
  snapshot: {
    turnAnchorId: null,
    generation: 0,
    revision: 0,
    status: "idle",
    phase: "idle",
  },
  presentation: "idle",
  canonical: false,
};

const STREAM_EVENT_TYPES = new Set([
  "thinking",
  "chunk",
  "workspace_draft",
  "tool_call",
]);

function finiteNonNegativeInteger(value: unknown): number | null {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 0) return null;
  return parsed;
}

export function readConversationTurnSnapshot(
  payload: Record<string, any>,
): ConversationTurnSnapshot | null {
  const raw = payload.turn;
  if (!raw || typeof raw !== "object") return null;
  const generation = finiteNonNegativeInteger(raw.generation);
  const revision = finiteNonNegativeInteger(raw.revision);
  const phase = raw.phase;
  if (
    generation === null ||
    revision === null ||
    !["idle", "active", "suspended"].includes(phase)
  ) {
    return null;
  }
  return {
    turnAnchorId: raw.turn_anchor_id ? String(raw.turn_anchor_id) : null,
    generation,
    revision,
    status: String(raw.status || "idle"),
    phase,
  };
}

function compareSnapshot(
  incoming: ConversationTurnSnapshot,
  current: ConversationTurnSnapshot,
): number {
  if (incoming.generation !== current.generation) {
    return incoming.generation > current.generation ? 1 : -1;
  }
  if (
    incoming.turnAnchorId &&
    current.turnAnchorId &&
    incoming.turnAnchorId !== current.turnAnchorId
  ) {
    return -1;
  }
  if (incoming.revision !== current.revision) {
    return incoming.revision > current.revision ? 1 : -1;
  }
  return 0;
}

function presentationFor(
  snapshot: ConversationTurnSnapshot,
  payload: Record<string, any>,
  current: ConversationTurnRuntime,
): ConversationTurnPresentation {
  if (snapshot.phase === "idle") return "idle";
  if (snapshot.phase === "suspended") return "suspended";
  if (
    String(payload.type || "") === "tool_call" &&
    String(payload.status || "") === "done"
  ) {
    return "waiting";
  }
  if (STREAM_EVENT_TYPES.has(String(payload.type || ""))) return "streaming";
  if (
    snapshot.generation === current.snapshot.generation &&
    snapshot.revision === current.snapshot.revision &&
    current.presentation === "streaming"
  ) {
    return "streaming";
  }
  return "waiting";
}

/**
 * Fold every chat transport event through one ordering rule.
 *
 * During the producer migration, legacy events without an envelope remain
 * accepted but cannot replace the durable snapshot. Once all producers emit
 * the contract, that compatibility branch can be removed in one place.
 */
export function reduceConversationTurnEvent(
  current: ConversationTurnRuntime,
  payload: Record<string, any>,
): ConversationTurnReduction {
  const eventType = String(payload.type || "");
  const eventKind = String(payload.event_kind || "");
  const incoming = readConversationTurnSnapshot(payload);
  const isReceipt = eventType === "turn_receipt" || eventKind === "turn_rejected";
  const deliversTransport = eventType === "connected" || isReceipt;
  if (isReceipt) {
    const advancesSnapshot = Boolean(
      incoming && compareSnapshot(incoming, current.snapshot) > 0,
    );
    return {
      accepted: true,
      runtime:
        incoming && advancesSnapshot
          ? {
              snapshot: incoming,
              presentation: presentationFor(
                incoming,
                { ...payload, type: "connected" },
                current,
              ),
              canonical: true,
            }
          : current,
      hasSnapshot: incoming !== null,
      controlsLifecycle: advancesSnapshot,
      deliversTimeline: false,
      deliversTransport: true,
    };
  }
  if (!incoming) {
    return {
      accepted: true,
      runtime: current,
      hasSnapshot: false,
      controlsLifecycle: !current.canonical,
      deliversTimeline: !isReceipt,
      deliversTransport,
    };
  }

  const comparison = compareSnapshot(incoming, current.snapshot);
  if (comparison < 0) {
    const staleActiveFrame = Boolean(
      incoming.generation === current.snapshot.generation &&
        Boolean(payload.producer_scope) &&
        incoming.turnAnchorId &&
        incoming.turnAnchorId === current.snapshot.turnAnchorId &&
        incoming.phase === "active" &&
        current.snapshot.phase === "active" &&
        (STREAM_EVENT_TYPES.has(eventType) || eventType === "workspace_draft"),
    );
    const deliversTimeline =
      staleActiveFrame ||
      eventType === "done" ||
      eventType === "assistant_message_committed" ||
      (eventType === "tool_call" && String(payload.status || "") === "done");
    return {
      // Lifecycle ordering and message delivery are separate concerns. An
      // older terminal cannot replace the current snapshot, but its durable
      // message must still reach the timeline and fold only its own anchor.
      accepted: deliversTimeline,
      runtime: current,
      hasSnapshot: true,
      controlsLifecycle: false,
      deliversTimeline,
      deliversTransport,
    };
  }
  return {
    accepted: true,
    runtime: {
      snapshot: incoming,
      presentation: presentationFor(incoming, payload, current),
      canonical: true,
    },
    hasSnapshot: true,
    controlsLifecycle: true,
    deliversTimeline: !isReceipt,
    deliversTransport,
  };
}

export function conversationTurnEventShouldBeHandled(
  reduction: ConversationTurnReduction,
): boolean {
  return reduction.deliversTimeline || reduction.deliversTransport;
}

export function conversationTurnEventClosesStream(
  reduction: ConversationTurnReduction,
  payload: Record<string, any>,
): boolean {
  if (String(payload.event_kind || "") === "turn_rejected") return false;
  if (
    !["done", "error", "quota_exceeded", "confirmation_required"].includes(
      String(payload.type || ""),
    )
  ) {
    return false;
  }
  if (!reduction.hasSnapshot) return reduction.controlsLifecycle;
  return (
    reduction.controlsLifecycle && reduction.runtime.snapshot.phase !== "active"
  );
}

export function conversationTurnIsWaiting(runtime: ConversationTurnRuntime): boolean {
  return runtime.presentation === "waiting";
}

export function conversationTurnIsStreaming(runtime: ConversationTurnRuntime): boolean {
  return runtime.presentation === "streaming";
}

export function conversationTurnIsRunning(runtime: ConversationTurnRuntime): boolean {
  return runtime.snapshot.phase === "active";
}

export function beginConversationTurnRecovery(
  runtime: ConversationTurnRuntime,
): ConversationTurnRuntime {
  if (runtime.snapshot.phase !== "active" || runtime.presentation === "waiting") {
    return runtime;
  }
  return { ...runtime, presentation: "waiting" };
}
