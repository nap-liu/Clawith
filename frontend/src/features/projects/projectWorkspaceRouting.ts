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
  capabilities: [
    "capFilter",
    "capabilitiesPage",
    "capabilitiesPageSize",
    "toolMember",
    "projectToolsPage",
    "projectToolsPageSize",
  ],
  matrix: [
    "capabilityMatrixPage",
    "capabilityMatrixPageSize",
    "projectToolMatrixPage",
    "projectToolMatrixPageSize",
  ],
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
  return applyUrlPatch(
    params,
    projectWorkspaceTabUrlPatch(tab, {
      ...legacyCollectionPatch,
      ...legacyAuditEventPatch,
    }),
  );
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
  const destinationKeys = new Set(TAB_QUERY_KEYS[nextTab]);
  const cleanup = Object.fromEntries(
    [
      ...TAB_SCOPED_QUERY_KEYS.filter((key) => !destinationKeys.has(key)),
      ...RETIRED_QUERY_KEYS,
    ].map((key) => [key, undefined]),
  );
  return { ...cleanup, tab: nextTab, ...patch };
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
