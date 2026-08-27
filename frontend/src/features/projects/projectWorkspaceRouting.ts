export type ProjectWorkspaceTab =
  | "cockpit"
  | "work"
  | "group"
  | "mesh"
  | "files"
  | "milestones"
  | "runs"
  | "members"
  | "capabilities"
  | "matrix"
  | "policies"
  | "git"
  | "audit";

export type ProjectWorkspaceUrlPatch = Record<string, string | undefined>;

export type ProjectWorkItemCompatibilityTarget = {
  workItemId: string;
  anchor:
    | "work-item-execution"
    | "work-item-conversation"
    | "work-item-changes"
    | "work-item-review";
};

const PROJECT_WORKSPACE_TABS = new Set<ProjectWorkspaceTab>([
  "cockpit",
  "work",
  "group",
  "mesh",
  "files",
  "milestones",
  "runs",
  "members",
  "capabilities",
  "matrix",
  "policies",
  "git",
  "audit",
]);

const WORK_ITEM_OBJECT_QUERY_KEYS = [
  "workItemTab",
  "evidence",
  "workItemRunsPage",
  "workItemRunsPageSize",
  "workItemEventsPage",
  "workItemEventsPageSize",
  "workItemSessionsPage",
  "workItemSessionsPageSize",
  "workItemFilesPage",
  "workItemCommitsPage",
  "workItemEvidencePage",
  "workItemEvidencePageSize",
] as const;

const LEGACY_WORK_ITEM_ANCHORS = {
  execution: "work-item-execution",
  conversation: "work-item-conversation",
  changes: "work-item-changes",
  review: "work-item-review",
} as const;

function legacyWorkItemAnchor(
  value: string | null,
): ProjectWorkItemCompatibilityTarget["anchor"] | undefined {
  if (
    !value ||
    !Object.prototype.hasOwnProperty.call(LEGACY_WORK_ITEM_ANCHORS, value)
  ) {
    return undefined;
  }
  return LEGACY_WORK_ITEM_ANCHORS[
    value as keyof typeof LEGACY_WORK_ITEM_ANCHORS
  ];
}

const CAPABILITY_QUERY_KEYS = [
  "capView",
  "capFilter",
  "capabilitiesPage",
  "capabilitiesPageSize",
  "toolMember",
  "projectToolsPage",
  "projectToolsPageSize",
  "capabilityMatrixPage",
  "capabilityMatrixPageSize",
  "projectToolMatrixPage",
  "projectToolMatrixPageSize",
] as const;

const TAB_QUERY_KEYS: Record<ProjectWorkspaceTab, readonly string[]> = {
  cockpit: [],
  work: [
    "workView",
    "workItemsPage",
    "workItemsPageSize",
    "workItem",
    ...WORK_ITEM_OBJECT_QUERY_KEYS,
  ],
  group: [],
  mesh: [],
  files: ["file", "fileView"],
  milestones: [
    "milestone",
    "milestonesPage",
    "milestonesPageSize",
    "milestoneRunsPage",
  ],
  runs: ["runMember", "runsPage", "runsPageSize"],
  members: ["member", "membersPage", "membersPageSize"],
  capabilities: CAPABILITY_QUERY_KEYS,
  matrix: CAPABILITY_QUERY_KEYS,
  policies: [],
  git: ["commit", "gitCommitsPage", "gitCommitsPageSize"],
  audit: [
    "auditEvent",
    "auditQ",
    "auditScope",
    "auditActor",
    "auditType",
    "auditEventsPage",
    "auditEventsPageSize",
  ],
};

const TAB_SCOPED_QUERY_KEYS = Array.from(
  new Set(Object.values(TAB_QUERY_KEYS).flat()),
);

const RETIRED_QUERY_KEYS = [
  "cockpitWorkItemsPage",
  "cockpitRisksPage",
  "meshEventsPage",
  "meshEventsPageSize",
  "memberRunsPage",
  "memberRunsPageSize",
] as const;

const UUID_QUERY_VALUE_PATTERN =
  /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i;

function applyUrlPatch(
  params: URLSearchParams,
  patch: ProjectWorkspaceUrlPatch,
): URLSearchParams {
  const next = new URLSearchParams(params);
  Object.entries(patch).forEach(([key, value]) => {
    if (value) next.set(key, value);
    else next.delete(key);
  });
  return next;
}

export function projectWorkspaceTabFromUrl(
  params: URLSearchParams,
): ProjectWorkspaceTab {
  const requested = params.get("tab");
  if (requested === "detail") return "work";
  if (requested === "matrix") return "capabilities";
  return PROJECT_WORKSPACE_TABS.has(requested as ProjectWorkspaceTab)
    ? (requested as ProjectWorkspaceTab)
    : "cockpit";
}

/**
 * Canonicalize an inbound workspace URL in one atomic operation.
 *
 * `detail` remains an inbound-only compatibility alias. Legacy collection
 * links open the list view, while legacy object links preserve their detail
 * state. Query parameters owned by other top-level tabs are removed.
 */
export function normalizeProjectWorkspaceUrl(
  params: URLSearchParams,
): URLSearchParams {
  const requested = params.get("tab");
  const tab = projectWorkspaceTabFromUrl(params);
  const legacyCollectionPatch =
    requested === "detail" && !params.get("workItem")
      ? { workView: "list" }
      : {};
  const legacyAuditQuery = params.get("auditQ") || "";
  const legacyAuditEventPatch =
    tab === "audit" &&
    !params.get("auditEvent") &&
    UUID_QUERY_VALUE_PATTERN.test(legacyAuditQuery)
      ? { auditEvent: legacyAuditQuery, auditQ: undefined }
      : {};
  const legacyWorkItemTab = params.get("workItemTab");
  const legacyWorkItemTabPatch = legacyWorkItemAnchor(legacyWorkItemTab)
    ? {}
    : { workItemTab: undefined };
  const capabilityViewPatch =
    tab === "capabilities"
      ? {
          capView:
            requested === "matrix" || params.get("capView") === "matrix"
              ? "matrix"
              : "list",
        }
      : {};
  return applyUrlPatch(
    params,
    projectWorkspaceTabUrlPatch(tab, {
      ...legacyCollectionPatch,
      ...legacyAuditEventPatch,
      ...legacyWorkItemTabPatch,
      ...capabilityViewPatch,
    }),
  );
}

/** Resolve an inbound tabbed detail link without adding a new query key. */
export function projectWorkItemCompatibilityTargetFromUrl(
  params: URLSearchParams,
): ProjectWorkItemCompatibilityTarget | null {
  const workItemId = params.get("workItem");
  const anchor = legacyWorkItemAnchor(params.get("workItemTab"));
  if (!workItemId || !anchor) return null;
  return {
    workItemId,
    anchor,
  };
}

/**
 * Build one atomic route patch for a top-level workspace tab transition.
 *
 * Parameters owned by the destination tab survive; parameters owned by every
 * other tab are removed. Global parameters, including an open session route,
 * are intentionally left untouched.
 */
export function projectWorkspaceTabUrlPatch(
  nextTab: ProjectWorkspaceTab,
  patch: ProjectWorkspaceUrlPatch = {},
): ProjectWorkspaceUrlPatch {
  const canonicalTab = nextTab === "matrix" ? "capabilities" : nextTab;
  const destinationKeys = new Set(TAB_QUERY_KEYS[canonicalTab]);
  const cleanup = Object.fromEntries(
    [
      ...TAB_SCOPED_QUERY_KEYS.filter((key) => !destinationKeys.has(key)),
      ...RETIRED_QUERY_KEYS,
    ].map((key) => [key, undefined]),
  );
  if (canonicalTab !== "capabilities") {
    return { ...cleanup, tab: canonicalTab, ...patch };
  }
  const { capView, ...capabilityPatch } = patch;
  return {
    ...cleanup,
    tab: canonicalTab,
    ...capabilityPatch,
    capView: nextTab === "matrix" || capView === "matrix" ? "matrix" : "list",
  };
}

/**
 * Transition between work-item objects without leaking object-scoped state.
 * Re-selecting the same object retains its active detail state.
 */
export function projectWorkItemUrlPatch(
  nextWorkItemId: string | undefined,
  currentWorkItemId: string | undefined,
): ProjectWorkspaceUrlPatch {
  if (nextWorkItemId && nextWorkItemId === currentWorkItemId) {
    return { workItem: nextWorkItemId };
  }
  return {
    ...Object.fromEntries(
      WORK_ITEM_OBJECT_QUERY_KEYS.map((key) => [key, undefined]),
    ),
    workItem: nextWorkItemId,
  };
}
