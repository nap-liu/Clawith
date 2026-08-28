import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { useTranslation } from "react-i18next";
import { Link, useParams, useSearchParams } from "react-router-dom";
import {
  IconActivityHeartbeat,
  IconAlertTriangle,
  IconArchive,
  IconArrowRight,
  IconBolt,
  IconBrandGit,
  IconBroadcast,
  IconChecklist,
  IconChevronRight,
  IconCircleCheck,
  IconClock,
  IconCodeDots,
  IconFile,
  IconFileText,
  IconFilter,
  IconGitBranch,
  IconHistory,
  IconLoader2,
  IconLock,
  IconMessageCircle,
  IconPlayerPause,
  IconPlayerPlay,
  IconPlus,
  IconDeviceFloppy,
  IconFlag,
  IconRefresh,
  IconRestore,
  IconSettings,
  IconShieldCheck,
  IconSparkles,
  IconTargetArrow,
  IconTool,
  IconTrash,
  IconUsers,
  IconX,
} from "@tabler/icons-react";

import { useToast } from "../../components/Toast/ToastProvider";
import { Drawer } from "../../components/Dialog/DialogProvider";
import OrgMemberAccessPicker, {
  type AgentAccessUser,
} from "../../components/OrgMemberAccessPicker";
import Pagination from "../../components/Pagination";
import SessionViewerDrawer, {
  type SessionViewerGroupConfig,
  type SessionViewerTarget,
} from "../../components/SessionViewerDrawer";
import i18n from "../../i18n";
import { enterpriseApi } from "../../services/api";
import { projectsApi } from "../../services/projects";
import ToolsTab from "../../pages/agent-detail/tabs/ToolsTab";
import SkillsTab from "../../pages/agent-detail/tabs/SkillsTab";
import { projectUserFacingCopy } from "./projectUserFacingCopy";
import {
  Button,
  ProjectCountBadge,
  ProjectDataTable,
  ProjectDataTableBody,
  ProjectDataTableCell,
  ProjectDataTableHead,
  ProjectDataTableHeader,
  ProjectDataTableRow,
  ProjectDialog,
  ProjectEmptyState,
  ProjectField,
  ProjectIconButton,
  ProjectProgressBar,
  ProjectSegmentedControl,
  ProjectSelect,
  ProjectStatusBadge,
  ProjectTextarea,
  SearchInput,
  TextInput,
  ToggleSwitch,
} from "./components/ProjectUI";
import type {
  ProjectCapabilityOption,
  ProjectOwnedAgent,
  ProjectOwnedAgentPromotion,
  ProjectSummary,
  ProjectTemplateManifest,
} from "./types";
import {
  A2AMeshGraph,
  ProjectGraphLegend,
  WorkDependencyGraph,
} from "./components/ProjectGraphs";
import ProjectFileWorkspace from "./components/ProjectFileWorkspace";
import ProjectEventContent, {
  ProjectEventLabel,
} from "./components/ProjectEventContent";
import ProjectAgentCapabilityPanel from "./components/ProjectAgentCapabilityPanel";
import {
  closestProjectTraceValue as closestTraceValue,
  inferProjectSessionIntent as inferredSessionIntent,
  isProjectA2ARecord,
  projectTraceRecords as traceRecords,
  projectTraceValue as traceValue,
  projectSessionTargetFromUrl,
  projectSessionUrlPatch,
  resolveProjectSessionRoute as sessionRouteOf,
  type ProjectSessionIntent as SessionIntent,
} from "./projectSessionRouting";
import {
  normalizeProjectWorkspaceUrl,
  projectWorkItemCompatibilityTargetFromUrl,
  projectWorkItemUrlPatch,
  projectWorkspaceTabUrlPatch,
  projectWorkspaceTabFromUrl,
  type ProjectWorkspaceTab as WorkspaceTab,
  type ProjectWorkspaceUrlPatch as WorkspaceUrlPatch,
} from "./projectWorkspaceRouting";
import "./projectWorkspace.css";

type RecordValue = Record<string, unknown>;
type ProjectSessionTarget = SessionViewerTarget & {
  agentId: string;
  agentName: string;
  kind?: "group" | "session";
};
type OpenSession = (
  source: RecordValue,
  title?: string,
  intent?: SessionIntent,
) => void;
type WorkspaceNavigationState = {
  navigate: (tab: WorkspaceTab, patch?: WorkspaceUrlPatch) => void;
  get: (key: string) => string;
  update: (patch: WorkspaceUrlPatch, options?: { replace?: boolean }) => void;
};

const WorkspaceNavigationContext = createContext<WorkspaceNavigationState>({
  navigate: () => undefined,
  get: () => "",
  update: () => undefined,
});

function useWorkspacePagination<T>(
  items: readonly T[],
  key: string,
  defaultPageSize = 10,
  pageSizeOptions: readonly number[] = [10, 20, 50],
  allowPageSizeChange = true,
  showJump = true,
  compact = false,
) {
  const { get, update } = useContext(WorkspaceNavigationContext);
  const requestedPage = Number.parseInt(get(`${key}Page`), 10);
  const requestedPageSize = Number.parseInt(get(`${key}PageSize`), 10);
  const pageSize = pageSizeOptions.includes(requestedPageSize)
    ? requestedPageSize
    : defaultPageSize;
  const totalPages = Math.max(1, Math.ceil(items.length / pageSize));
  const page = Math.min(
    Math.max(1, Number.isFinite(requestedPage) ? requestedPage : 1),
    totalPages,
  );

  useEffect(() => {
    if (Number.isFinite(requestedPage) && requestedPage > totalPages) {
      update(
        { [`${key}Page`]: totalPages > 1 ? String(totalPages) : undefined },
        { replace: true },
      );
    }
  }, [key, requestedPage, totalPages, update]);

  const pageItems = useMemo(
    () => items.slice((page - 1) * pageSize, page * pageSize),
    [items, page, pageSize],
  );

  return {
    pageItems,
    pagination:
      items.length > pageSize ? (
        <Pagination
          page={page}
          pageSize={pageSize}
          total={items.length}
          onPageChange={(nextPage) =>
            update({
              [`${key}Page`]: nextPage > 1 ? String(nextPage) : undefined,
            })
          }
          onPageSizeChange={
            allowPageSizeChange
              ? (nextPageSize) =>
                  update({
                    [`${key}Page`]: undefined,
                    [`${key}PageSize`]:
                      nextPageSize === defaultPageSize
                        ? undefined
                        : String(nextPageSize),
                  })
              : undefined
          }
          pageSizeOptions={pageSizeOptions}
          showJump={showJump}
          showRange={!compact}
          compact={compact}
        />
      ) : null,
  };
}

type WorkspaceData = {
  project: ProjectSummary;
  members: RecordValue[];
  projectAgents: ProjectOwnedAgent[];
  capabilities: RecordValue[];
  workItems: RecordValue[];
  runs: RecordValue[];
  events: RecordValue[];
  milestones: RecordValue[];
  commits: RecordValue[];
  gitRepository: RecordValue;
  files: RecordValue[];
  policies: RecordValue | null;
  groupSession: RecordValue | null;
};

type WorkspaceDomain =
  "overview" | "work" | "collaboration" | "delivery" | "team" | "activity";

type WorkspaceDomainDefinition = {
  id: WorkspaceDomain;
  defaultTab: WorkspaceTab;
  labelKey: string;
  icon: typeof IconBolt;
  tabs: Array<{ id: WorkspaceTab; labelKey: string }>;
};

const WORKSPACE_DOMAINS: WorkspaceDomainDefinition[] = [
  {
    id: "overview",
    defaultTab: "cockpit",
    labelKey: "projectWorkspaceNav.domains.overview",
    icon: IconActivityHeartbeat,
    tabs: [{ id: "cockpit", labelKey: "projectWorkspaceNav.tabs.cockpit" }],
  },
  {
    id: "work",
    defaultTab: "work",
    labelKey: "projectWorkspaceNav.domains.work",
    icon: IconTargetArrow,
    tabs: [{ id: "work", labelKey: "projectWorkspaceNav.tabs.work" }],
  },
  {
    id: "collaboration",
    defaultTab: "group",
    labelKey: "projectWorkspaceNav.domains.collaboration",
    icon: IconMessageCircle,
    tabs: [
      { id: "group", labelKey: "projectWorkspaceNav.tabs.group" },
      { id: "mesh", labelKey: "projectWorkspaceNav.tabs.mesh" },
    ],
  },
  {
    id: "delivery",
    defaultTab: "files",
    labelKey: "projectWorkspaceNav.domains.delivery",
    icon: IconArchive,
    tabs: [
      { id: "files", labelKey: "projectWorkspaceNav.tabs.workspace" },
      {
        id: "milestones",
        labelKey: "projectWorkspaceNav.tabs.milestones",
      },
      { id: "git", labelKey: "projectWorkspaceNav.tabs.git" },
    ],
  },
  {
    id: "team",
    defaultTab: "members",
    labelKey: "projectWorkspaceNav.domains.team",
    icon: IconUsers,
    tabs: [
      {
        id: "members",
        labelKey: "projectWorkspaceNav.tabs.projectAgents",
      },
      {
        id: "capabilities",
        labelKey: "projectWorkspaceNav.tabs.capabilities",
      },
    ],
  },
  {
    id: "activity",
    defaultTab: "runs",
    labelKey: "projectWorkspaceNav.domains.activity",
    icon: IconHistory,
    tabs: [
      { id: "runs", labelKey: "projectWorkspaceNav.tabs.runs" },
      { id: "audit", labelKey: "projectWorkspaceNav.tabs.audit" },
    ],
  },
];

const workspaceDomainForTab = (
  tab: WorkspaceTab,
): WorkspaceDomainDefinition | undefined =>
  WORKSPACE_DOMAINS.find((domain) =>
    domain.tabs.some((candidate) => candidate.id === tab),
  );
const obj = (value: unknown): RecordValue =>
  value && typeof value === "object" ? (value as RecordValue) : {};
const arr = (value: unknown): RecordValue[] =>
  Array.isArray(value)
    ? value.map(obj)
    : Array.isArray(obj(value).items)
      ? (obj(value).items as unknown[]).map(obj)
      : [];
const text = (source: RecordValue, ...keys: string[]): string => {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "string" || typeof value === "number")
      return String(value);
  }
  return "";
};
const bool = (source: RecordValue, ...keys: string[]): boolean =>
  keys.some((key) => source[key] === true);
const num = (source: RecordValue, ...keys: string[]): number => {
  for (const key of keys) {
    const value = Number(source[key]);
    if (Number.isFinite(value)) return value;
  }
  return 0;
};
const listText = (source: RecordValue, key: string): string =>
  Array.isArray(source[key])
    ? (source[key] as unknown[]).map((value) => String(value)).join("；")
    : text(source, key);
const dateLabel = (value: unknown): string => {
  if (!value) return "—";
  const date = new Date(String(value));
  return Number.isNaN(date.getTime())
    ? String(value)
    : new Intl.DateTimeFormat(i18n.language, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      }).format(date);
};
const compactId = (value: string): string =>
  value.length > 12 ? value.slice(0, 8) : value;
const fileSizeLabel = (bytes: number): string => {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
};
const statusLabel = (
  status: string,
  t: ReturnType<typeof useTranslation>["t"],
): string =>
  t(`projectGraphs.status.${status || "unset"}`, {
    defaultValue: status || t("projectGraphs.status.unset"),
  });

type ProjectToolDefinition = {
  name: string;
  label: string;
  description: string;
  descriptionKey?: string;
  participant: boolean;
};

const PROJECT_TOOL_REGISTRY: readonly ProjectToolDefinition[] = [
  {
    name: "project_get_context",
    label: "View project overview",
    description: "Review the project goal, plan, status, and current version.",
    participant: true,
  },
  {
    name: "project_list_work_items",
    label: "View work items",
    description:
      "Review project work items or work assigned to the current member.",
    participant: true,
  },
  {
    name: "project_list_files",
    label: "View workspace",
    description: "Review project deliverables and their versions.",
    participant: true,
  },
  {
    name: "project_read_file",
    label: "Read workspace file",
    description: "Read a project text file. The file must already exist.",
    participant: true,
  },
  {
    name: "project_update_work_item",
    label: "Update work item",
    description: "Update the status, progress, or evidence of an existing work item.",
    descriptionKey: "projectTerminology.workspace.ownerToolDescription",
    participant: true,
  },
  {
    name: "project_write_file",
    label: "Write workspace file",
    description: "Save a file at a specified path in the project workspace.",
    participant: true,
  },
  {
    name: "project_message_agent",
    label: "Contact Digital Employee",
    description:
      "Start a collaboration request with one active member of the current project.",
    participant: true,
  },
  {
    name: "project_update_plan",
    label: "Update project plan",
    description:
      "Update the goal, success criteria, and current progress. Available to the execution lead.",
    participant: false,
  },
  {
    name: "project_create_work_item",
    label: "Create work item",
    description:
      "Create and assign a work item. Available to the execution lead.",
    participant: false,
  },
  {
    name: "project_set_member_enabled",
    label: "Manage project members",
    description:
      "Activate or deactivate a member other than the execution lead. Available to the execution lead.",
    descriptionKey:
      "projectTerminology.workspace.memberLifecycleToolDescription",
    participant: false,
  },
  {
    name: "project_set_capability_enabled",
    label: "Manage project capabilities",
    description:
      "Enable or disable a capability already added to the project. Available to the execution lead.",
    participant: false,
  },
  {
    name: "project_create_milestone",
    label: "Create delivery milestone",
    description:
      "Record the current delivery state for later review or recovery. Requires an existing project result.",
    participant: false,
  },
  {
    name: "project_set_status",
    label: "Update project status",
    description:
      "Set the project to waiting, paused, completed, or failed. Available to the execution lead.",
    participant: false,
  },
  {
    name: "project_restore_commit",
    label: "Restore project version",
    description:
      "Create a new current version from an earlier version while retaining existing history.",
    participant: false,
  },
] as const;

const stringList = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.map((entry) => String(entry)).filter(Boolean)
    : [];

function projectToolResolution(
  tool: ProjectToolDefinition,
  member: RecordValue,
  policies: RecordValue | null,
) {
  const role = bool(member, "is_leader") ? "leader" : "participant";
  const lifecycleBlocked = member.is_enabled === false;
  const roleCeiling = role === "leader" || tool.participant;
  const projectPolicy = obj(
    obj(policies?.policies).project_tools || obj(policies).project_tools,
  );
  const policyDisabled = new Set([
    ...stringList(projectPolicy.disabled),
    ...stringList(projectPolicy[`${role}_disabled`]),
  ]);
  const roleAllowed = Array.isArray(projectPolicy[`${role}_allowed`])
    ? new Set(stringList(projectPolicy[`${role}_allowed`]))
    : null;
  const config = obj(member.config_snapshot);
  const memberDisabled = new Set(stringList(config.disabled_project_tools));
  const memberAllowed = Array.isArray(config.enabled_project_tools)
    ? new Set(stringList(config.enabled_project_tools))
    : null;
  const policyBlocked =
    policyDisabled.has(tool.name) ||
    Boolean(roleAllowed && !roleAllowed.has(tool.name));
  const snapshotBlocked = Boolean(
    memberAllowed && !memberAllowed.has(tool.name),
  );
  return {
    role,
    roleCeiling,
    policyBlocked,
    snapshotBlocked,
    lifecycleBlocked,
    memberDisabled: memberDisabled.has(tool.name),
    effective:
      !lifecycleBlocked &&
      roleCeiling &&
      !policyBlocked &&
      !snapshotBlocked &&
      !memberDisabled.has(tool.name),
  };
}
const errorMessage = (error: unknown, fallback = "Request failed"): string =>
  error instanceof Error ? error.message : fallback;

function sessionIdOf(source: RecordValue): string {
  return sessionRouteOf(source)?.sessionId || "";
}

function traceStringValues(source: RecordValue, ...keys: string[]): string[] {
  const values: string[] = [];
  for (const record of traceRecords(source)) {
    for (const key of keys) {
      const value = record[key];
      if (Array.isArray(value)) {
        value.forEach((entry) => {
          if (typeof entry === "string" || typeof entry === "number")
            values.push(String(entry));
        });
      } else if (typeof value === "string" || typeof value === "number") {
        values.push(String(value));
      }
    }
  }
  return Array.from(
    new Set(values.map((value) => value.trim()).filter(Boolean)),
  );
}

function runAgentId(run: RecordValue): string {
  return closestTraceValue(
    traceRecords(run),
    "execution_agent_id",
    "subagent_agent_id",
    "agent_id",
    "to_agent_id",
    "assignee_agent_id",
  );
}

function runAgentName(
  run: RecordValue,
  members: RecordValue[],
  fallback = "Digital Employee not recorded",
): string {
  const records = traceRecords(run);
  const snapshotName = closestTraceValue(
    records,
    "agent_name_snapshot",
    "name_snapshot",
    "execution_agent_name",
    "subagent_agent_name",
    "agent_name",
    "to_agent_name",
  );
  if (snapshotName) return snapshotName;
  const agentId = runAgentId(run);
  const member = members.find((entry) => text(entry, "agent_id") === agentId);
  return text(member || {}, "name_snapshot", "agent_name", "name") || fallback;
}

function sameGitCommit(left: string, right: string): boolean {
  return Boolean(
    left &&
    right &&
    (left === right || left.startsWith(right) || right.startsWith(left)),
  );
}

function SessionButton({
  source,
  onOpen,
  label,
  intent = "auto",
}: {
  source: RecordValue;
  onOpen: OpenSession;
  label?: string;
  intent?: SessionIntent;
}) {
  const { t } = useTranslation();
  if (!sessionRouteOf(source, intent)) return null;
  return (
    <Button
      type="button"
      variant="ghost"
      className="project-workspace__session-link"
      onClick={() => onOpen(source, undefined, intent)}
    >
      <IconMessageCircle size={14} />
      {label || t("projectWorkspacePage.session.view")}
    </Button>
  );
}

function pickCollection(
  payload: RecordValue,
  ...keys: string[]
): RecordValue[] {
  for (const key of keys) {
    const value = payload[key];
    const items = arr(value);
    if (items.length || Array.isArray(value) || Array.isArray(obj(value).items))
      return items;
  }
  return [];
}

function StatusPill({ status }: { status: string }) {
  const { t } = useTranslation();
  const tone = ["running", "success", "completed", "done"].includes(status)
    ? "success"
    : ["failed", "blocked"].includes(status)
      ? "error"
      : ["paused", "waiting", "review"].includes(status)
        ? "warning"
        : "neutral";
  return (
    <ProjectStatusBadge tone={tone} className="project-workspace__status">
      {statusLabel(status, t)}
    </ProjectStatusBadge>
  );
}

function EmptyState(props: Parameters<typeof ProjectEmptyState>[0]) {
  return <ProjectEmptyState {...props} />;
}

function SectionHeading({
  title,
  actions,
  className = "",
}: {
  eyebrow: string;
  title: string;
  description: string;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <header
      className={`project-workspace__section-heading${actions ? "" : " is-title-only"}${className ? ` ${className}` : ""}`}
      aria-label={title}
    >
      <h2 className="project-workspace__visually-hidden">{title}</h2>
      {actions && (
        <div className="project-workspace__heading-actions">{actions}</div>
      )}
    </header>
  );
}

export default function ProjectWorkspacePage() {
  const { t } = useTranslation();
  const routeParams = useParams<{ projectId?: string; id?: string }>();
  const projectId = routeParams.projectId || routeParams.id || "";
  const [searchParams, setSearchParams] = useSearchParams();
  const normalizedSearchParams = useMemo(
    () => normalizeProjectWorkspaceUrl(searchParams),
    [searchParams],
  );
  const tab = projectWorkspaceTabFromUrl(normalizedSearchParams);
  const [data, setData] = useState<WorkspaceData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [resourceWarnings, setResourceWarnings] = useState<string[]>([]);
  const toast = useToast();
  const [busyAction, setBusyAction] = useState("");
  const selectedWorkItemId = normalizedSearchParams.get("workItem") || "";
  const selectedMemberId = normalizedSearchParams.get("member") || "";
  const [gitDialog, setGitDialog] = useState<"restore" | "branch" | null>(null);
  const [runtimeDialog, setRuntimeDialog] = useState<"pause" | "resume" | null>(
    null,
  );
  const [sessionTarget, setSessionTarget] =
    useState<ProjectSessionTarget | null>(null);
  const canEdit = data ? data.project.access_role !== "view" : false;
  const refreshingRef = useRef(0);
  useEffect(() => {
    if (normalizedSearchParams.toString() === searchParams.toString()) return;
    setSearchParams(normalizedSearchParams, { replace: true });
  }, [normalizedSearchParams, searchParams, setSearchParams]);
  const updateWorkspaceUrl = useCallback(
    (patch: WorkspaceUrlPatch, options: { replace?: boolean } = {}) => {
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current);
          Object.entries(patch).forEach(([key, value]) => {
            if (value) next.set(key, value);
            else next.delete(key);
          });
          return next;
        },
        { replace: options.replace === true },
      );
    },
    [setSearchParams],
  );
  const navigateWorkspace = useCallback(
    (nextTab: WorkspaceTab, patch: WorkspaceUrlPatch = {}) => {
      const nextPatch =
        nextTab === "work" &&
        Object.prototype.hasOwnProperty.call(patch, "workItem")
          ? {
              ...patch,
              ...projectWorkItemUrlPatch(
                patch.workItem,
                selectedWorkItemId || undefined,
              ),
            }
          : patch;
      updateWorkspaceUrl(projectWorkspaceTabUrlPatch(nextTab, nextPatch));
    },
    [selectedWorkItemId, updateWorkspaceUrl],
  );
  const openSessionTarget = useCallback(
    (target: ProjectSessionTarget) => {
      setSessionTarget(target);
      updateWorkspaceUrl(projectSessionUrlPatch(target));
    },
    [updateWorkspaceUrl],
  );
  const closeSessionTarget = useCallback(() => {
    setSessionTarget(null);
    updateWorkspaceUrl(projectSessionUrlPatch(null));
  }, [updateWorkspaceUrl]);
  const workspaceNavigation = useMemo<WorkspaceNavigationState>(
    () => ({
      navigate: navigateWorkspace,
      get: (key) => normalizedSearchParams.get(key) || "",
      update: updateWorkspaceUrl,
    }),
    [navigateWorkspace, normalizedSearchParams, updateWorkspaceUrl],
  );

  useEffect(() => {
    const routedTarget = projectSessionTargetFromUrl(normalizedSearchParams);
    if (!routedTarget) {
      setSessionTarget((current) => (current ? null : current));
      return;
    }
    if (!data) return;
    setSessionTarget((current) => {
      if (
        current?.sessionId === routedTarget.sessionId &&
        current.agentId === routedTarget.agentId &&
        current.anchorMessageId === routedTarget.anchorMessageId &&
        current.projectRunId === routedTarget.projectRunId &&
        current.kind === routedTarget.kind &&
        current.mode === routedTarget.mode &&
        current.readOnly === routedTarget.readOnly
      ) {
        return current;
      }
      const member = data.members.find(
        (entry) => text(entry, "agent_id") === routedTarget.agentId,
      );
      const group = routedTarget.kind === "group" ? data.groupSession : null;
      return {
        ...routedTarget,
        agentName:
          routedTarget.kind === "group"
            ? t("projectWorkspacePage.session.projectChat")
            : text(member || {}, "name_snapshot", "agent_name", "name") ||
              t("projectTerminology.dynamicCopy.digitalEmployee"),
        title:
          (group ? text(group, "title", "group_name") : "") ||
          (routedTarget.kind === "group"
            ? t("projectWorkspacePage.session.projectChat")
            : t("projectWorkspacePage.session.collaborationRecord")),
      };
    });
  }, [data, normalizedSearchParams, t]);

  useEffect(() => {
    if (!data || !selectedWorkItemId) return;
    const exists = data.workItems.some(
      (item) => text(item, "id", "work_item_id") === selectedWorkItemId,
    );
    if (exists) return;
    updateWorkspaceUrl(projectWorkItemUrlPatch(undefined, selectedWorkItemId), {
      replace: true,
    });
  }, [data, selectedWorkItemId, updateWorkspaceUrl]);

  const load = useCallback(
    async ({ silent = false }: { silent?: boolean } = {}) => {
      if (!projectId) {
        setError(t("projectWorkspacePage.errors.missingProjectId"));
        setLoading(false);
        return;
      }
      if (refreshingRef.current > 0 && silent) return;
      refreshingRef.current += 1;
      if (!silent) {
        setLoading(true);
        setError("");
        setResourceWarnings([]);
      }
      try {
        if (silent) {
          const [
            dashboardResponse,
            workItemsResponse,
            runsResponse,
            eventsResponse,
            milestonesResponse,
          ] = await Promise.allSettled([
            projectsApi.dashboard(projectId),
            projectsApi.listWorkItems(projectId),
            projectsApi.listRuns(projectId),
            projectsApi.listEvents(projectId),
            projectsApi.listMilestones(projectId),
          ]);
          if (dashboardResponse.status === "rejected")
            throw dashboardResponse.reason;
          const dashboard = obj(dashboardResponse.value);
          const dashboardProject = obj(dashboard.project);
          const dashboardGit = obj(dashboard.git);
          const dashboardFiles = Array.isArray(dashboard.files)
            ? dashboard.files
            : [];
          setData((current) =>
            current
              ? {
                  ...current,
                  project: {
                    ...current.project,
                    ...dashboardProject,
                  } as ProjectSummary,
                  workItems:
                    workItemsResponse.status === "fulfilled"
                      ? arr(workItemsResponse.value)
                      : pickCollection(dashboard, "work_items", "workItems"),
                  runs:
                    runsResponse.status === "fulfilled"
                      ? arr(runsResponse.value)
                      : pickCollection(dashboard, "runs"),
                  events:
                    eventsResponse.status === "fulfilled"
                      ? arr(eventsResponse.value)
                      : pickCollection(dashboard, "events", "audit_events"),
                  milestones:
                    milestonesResponse.status === "fulfilled"
                      ? arr(milestonesResponse.value)
                      : current.milestones,
                  commits: pickCollection(dashboardGit, "commits").length
                    ? pickCollection(dashboardGit, "commits")
                    : pickCollection(dashboard, "commits"),
                  gitRepository: { ...current.gitRepository, ...dashboardGit },
                  files: dashboardFiles.map((entry) =>
                    typeof entry === "string"
                      ? {
                          id: entry,
                          path: entry,
                          name: entry.split("/").pop() || entry,
                        }
                      : obj(entry),
                  ),
                }
              : current,
          );
          setResourceWarnings((current) =>
            current.filter(
              (warning) =>
                warning !== t("projectWorkspacePage.errors.refreshFailed"),
            ),
          );
          return;
        }
        const dashboard = await projectsApi.dashboard(projectId);
        const payload = obj(dashboard);
        const project = obj(payload.project) as unknown as ProjectSummary;
        if (!Object.keys(project).length) {
          throw new Error(
            t("projectWorkspacePage.errors.dashboardProjectMissing"),
          );
        }
        const resources = await Promise.allSettled([
          projectsApi.listMembers(projectId),
          projectsApi.listProjectAgents(projectId),
          projectsApi.listCapabilities(projectId),
          projectsApi.getGit(projectId),
          projectsApi.getSettings(projectId),
          projectsApi.listFiles(projectId),
          projectsApi.getGroupSession(projectId),
          projectsApi.listWorkItems(projectId),
          projectsApi.listRuns(projectId),
          projectsApi.listEvents(projectId),
          projectsApi.listMilestones(projectId),
        ]);
        const [
          membersResult,
          projectAgentsResult,
          capabilitiesResult,
          gitResult,
          settingsResult,
          filesResult,
          groupSessionResult,
          workItemsResult,
          runsResult,
          eventsResult,
          milestonesResult,
        ] = resources;
        const warningLabels = [
          t("projectWorkspacePage.resources.memberSnapshots"),
          t("projectAgents.badge"),
          t("projectWorkspacePage.resources.capabilityBindings"),
          t("projectWorkspacePage.resources.gitRepository"),
          t("projectWorkspacePage.resources.projectPolicies"),
          t("projectWorkspacePage.resources.workspace"),
          t("projectWorkspacePage.session.projectChat"),
          t("projectWorkspaceNav.tabs.detail"),
          t("projectWorkspacePage.resources.runRecords"),
          t("projectWorkspacePage.resources.projectEvents"),
          t("projectWorkspaceNav.tabs.milestones"),
        ];
        setResourceWarnings(
          resources.flatMap((result, index) =>
            result.status === "rejected"
              ? [
                  t("projectWorkspacePage.resources.loadFailed", {
                    resource: warningLabels[index],
                    error: errorMessage(
                      result.reason,
                      t("projectWorkspacePage.errors.requestFailed"),
                    ),
                  }),
                ]
              : [],
          ),
        );
        const members =
          membersResult.status === "fulfilled" ? membersResult.value : [];
        const projectAgents =
          projectAgentsResult.status === "fulfilled"
            ? projectAgentsResult.value
            : [];
        const capabilities =
          capabilitiesResult.status === "fulfilled"
            ? capabilitiesResult.value
            : [];
        const git = gitResult.status === "fulfilled" ? gitResult.value : {};
        const settings =
          settingsResult.status === "fulfilled" ? settingsResult.value : null;
        const serverFiles =
          filesResult.status === "fulfilled" ? filesResult.value : [];
        const gitPayload = obj(git);
        const rawFiles = Array.isArray(gitPayload.files)
          ? gitPayload.files
          : [];
        const projectSettings = obj(obj(payload.project).settings);
        const next: WorkspaceData = {
          project,
          members: arr(members),
          projectAgents,
          capabilities: arr(capabilities),
          workItems:
            workItemsResult.status === "fulfilled"
              ? arr(workItemsResult.value)
              : pickCollection(payload, "work_items", "workItems"),
          runs:
            runsResult.status === "fulfilled"
              ? arr(runsResult.value)
              : pickCollection(payload, "runs"),
          events:
            eventsResult.status === "fulfilled"
              ? arr(eventsResult.value)
              : pickCollection(payload, "events", "audit_events"),
          milestones:
            milestonesResult.status === "fulfilled"
              ? arr(milestonesResult.value)
              : [],
          commits: pickCollection(gitPayload, "commits"),
          gitRepository: { ...obj(obj(settings || {}).git), ...gitPayload },
          files: arr(serverFiles).length
            ? arr(serverFiles)
            : rawFiles.map((entry) =>
                typeof entry === "string"
                  ? {
                      id: entry,
                      path: entry,
                      name: entry.split("/").pop() || entry,
                    }
                  : obj(entry),
              ),
          policies: settings
            ? obj(settings)
            : payload.policies
              ? obj(payload.policies)
              : Object.keys(projectSettings).length
                ? projectSettings
                : null,
          groupSession:
            groupSessionResult.status === "fulfilled"
              ? obj(groupSessionResult.value)
              : null,
        };
        setData(next);
      } catch (loadError) {
        if (silent) {
          setResourceWarnings((current) =>
            current.includes(t("projectWorkspacePage.errors.refreshFailed"))
              ? current
              : [...current, t("projectWorkspacePage.errors.refreshFailed")],
          );
        } else {
          setError(
            errorMessage(
              loadError,
              t("projectWorkspacePage.errors.requestFailed"),
            ),
          );
          setData(null);
        }
      } finally {
        refreshingRef.current = Math.max(0, refreshingRef.current - 1);
        if (!silent) setLoading(false);
      }
    },
    [projectId, t],
  );

  useEffect(() => {
    void load();
  }, [load]);
  const fastRefresh = Boolean(
    data &&
    (["initializing", "running"].includes(data.project.status) ||
      data.runs.some((run) =>
        ["queued", "running"].includes(text(run, "status")),
      )),
  );
  useEffect(() => {
    const refresh = () => {
      if (document.visibilityState === "visible") void load({ silent: true });
    };
    const intervalId = window.setInterval(refresh, fastRefresh ? 3000 : 12000);
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") refresh();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.clearInterval(intervalId);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [fastRefresh, load]);
  const runAction = useCallback(
    async (key: string, action: () => Promise<unknown>, success: string) => {
      setBusyAction(key);
      try {
        await action();
        toast.success(success);
        await load();
        return true;
      } catch (actionError) {
        toast.error(
          errorMessage(
            actionError,
            t("projectWorkspacePage.errors.requestFailed"),
          ),
        );
        return false;
      } finally {
        setBusyAction("");
      }
    },
    [load, t, toast],
  );
  const changeRuntimeStatus = async () => {
    if (!runtimeDialog) return;
    const nextStatus = runtimeDialog === "pause" ? "paused" : "running";
    const succeeded = await runAction(
      "project-runtime",
      () => projectsApi.update(projectId, { status: nextStatus }),
      t(`projectRuntime.${runtimeDialog}Success`),
    );
    if (succeeded) setRuntimeDialog(null);
  };
  const openSession = useCallback<OpenSession>(
    (source, title, requestedIntent = "auto") => {
      if (!data) return;
      const sourceRecords = traceRecords(source);
      const sourceRunId = closestTraceValue(sourceRecords, "run_id");
      const sourceCommit = closestTraceValue(
        sourceRecords,
        "commit_hash",
        "commit",
        "hash",
      );
      const linkedRun = data.runs.find(
        (run) => text(run, "id", "run_id") === sourceRunId,
      );
      const linkedEvents = data.events.filter((event) => {
        const records = traceRecords(event);
        return (
          (sourceRunId &&
            closestTraceValue(records, "run_id") === sourceRunId) ||
          (sourceCommit &&
            closestTraceValue(records, "commit_hash", "commit", "hash") ===
              sourceCommit)
        );
      });
      const inferredIntent =
        requestedIntent === "auto"
          ? inferredSessionIntent(source)
          : requestedIntent;
      const intent =
        inferredIntent === "auto" &&
        linkedRun &&
        inferredSessionIntent(linkedRun) !== "auto"
          ? inferredSessionIntent(linkedRun)
          : inferredIntent;
      const sessionSources =
        intent === "a2a"
          ? [
              source,
              ...linkedEvents.filter(
                (event) => inferredSessionIntent(event) === "a2a",
              ),
              ...(linkedRun && inferredSessionIntent(linkedRun) === "a2a"
                ? [linkedRun]
                : []),
            ]
          : [source, ...(linkedRun ? [linkedRun] : []), ...linkedEvents];
      const routedSource = sessionSources
        .map((entry) => ({
          source: entry,
          route: sessionRouteOf(entry, intent),
        }))
        .find((entry) => entry.route);
      if (!routedSource?.route) {
        toast.warning(
          intent === "a2a"
            ? t("projectWorkspacePage.session.noDeliveredA2A")
            : t("projectWorkspacePage.session.noAnchor"),
        );
        return;
      }
      const { sessionId, kind } = routedSource.route;
      const routedCandidates = sessionSources
        .map((entry) => ({
          source: entry,
          route: sessionRouteOf(entry, intent),
          records: traceRecords(entry),
        }))
        .filter((entry) => entry.route?.sessionId === sessionId);
      const records = sessionSources.flatMap(traceRecords);
      const group = kind === "group" ? data.groupSession : null;
      const agentId =
        routedSource.route.agentId ||
        (group
          ? text(group, "access_agent_id", "session_agent_id", "agent_id")
          : "") ||
        closestTraceValue(
          records,
          "session_agent_id",
          "session_access_agent_id",
          "access_agent_id",
          "execution_agent_id",
          "subagent_agent_id",
          "agent_id",
        );
      if (!agentId) {
        toast.warning(t("projectWorkspacePage.session.missingEmployee"));
        return;
      }
      const member = data.members.find(
        (entry) => text(entry, "agent_id") === agentId,
      );
      const projectRunId =
        closestTraceValue(sourceRecords, "project_run_id", "run_id") ||
        sourceRunId ||
        (text(source, "trigger_type") ? text(source, "id") : "");
      const exactAnchor = routedCandidates.find((candidate) => {
        if (!candidate.route?.anchorMessageId || !projectRunId) return false;
        return (
          closestTraceValue(
            candidate.records,
            "project_run_id",
            "run_id",
            "subagent_run_id",
          ) === projectRunId
        );
      })?.route?.anchorMessageId;
      openSessionTarget({
        sessionId,
        anchorMessageId:
          exactAnchor ||
          routedCandidates.find((candidate) => candidate.route?.anchorMessageId)
            ?.route?.anchorMessageId,
        projectRunId: projectRunId || undefined,
        agentId,
        agentName:
          kind === "group"
            ? t("projectWorkspacePage.session.projectChat")
            : text(member || {}, "name_snapshot", "agent_name", "name") ||
              traceValue(
                records,
                "execution_agent_name",
                "agent_name",
                "actor_name",
                "to_agent_name",
                "from_agent_name",
              ) ||
              t("projectTerminology.dynamicCopy.digitalEmployee"),
        title:
          title ||
          (kind === "group" ? text(group || {}, "title", "group_name") : "") ||
          closestTraceValue(
            records,
            "session_title",
            "title",
            "task",
            "objective",
            "summary",
          ) ||
          t("projectWorkspacePage.session.collaborationRecord"),
        status: traceValue(records, "session_status", "status"),
        mode:
          kind === "group"
            ? "group"
            : traceValue(records, "session_mode", "mode"),
        kind,
        readOnly:
          !canEdit ||
          source.member_enabled === false ||
          source.is_enabled === false,
      });
    },
    [canEdit, data, openSessionTarget, t, toast],
  );
  const groupMembers = useMemo(() => {
    const sources = [
      ...(data?.members || []),
      ...arr(data?.groupSession ? obj(data.groupSession).members : []),
    ];
    const normalized = sources
      .map((member) => {
        const snapshot = obj(
          member.snapshot || member.member_snapshot || member.member,
        );
        const agent = obj(member.agent || snapshot.agent);
        return {
          agentId:
            text(member, "agent_id") ||
            text(snapshot, "agent_id") ||
            text(agent, "id", "agent_id"),
          name:
            text(member, "name_snapshot", "agent_name", "name") ||
            text(snapshot, "name_snapshot", "agent_name", "name") ||
            text(agent, "name", "agent_name") ||
            t("projectTerminology.dynamicCopy.digitalEmployee"),
          isLeader: member.is_leader === true || snapshot.is_leader === true,
          isEnabled:
            member.is_enabled !== false && snapshot.is_enabled !== false,
        };
      })
      .filter((member) => member.agentId);
    return Array.from(
      new Map(normalized.map((member) => [member.agentId, member])).values(),
    );
  }, [data?.groupSession, data?.members, t]);
  const groupConfig = useMemo(() => {
    const group = obj(data?.groupSession);
    const configuredLimit = Number(
      group.max_mentions ||
        group.mention_limit ||
        obj(data?.policies).group_max_mentions ||
        4,
    );
    const nonLeaderCount = groupMembers.filter(
      (member) => member.isEnabled !== false && !member.isLeader,
    ).length;
    return {
      members: groupMembers,
      maxMentions: Math.min(
        Number.isFinite(configuredLimit) ? Math.max(0, configuredLimit) : 4,
        nonLeaderCount,
      ),
      loadMessages: (
        sessionId: string,
        options?: { before?: string; limit?: number },
      ) => projectsApi.listGroupMessages(projectId, sessionId, options),
      sendMessage: (
        sessionId: string,
        payload: {
          content: string;
          llm_content?: string;
          mentions: string[];
          attachments: Array<Record<string, unknown>>;
          client_message_id?: string;
        },
      ) => projectsApi.sendGroupMessage(projectId, sessionId, payload),
    };
  }, [data?.groupSession, data?.policies, groupMembers, projectId]);

  if (loading) {
    return (
      <main
        className="project-workspace project-workspace__state"
        aria-live="polite"
      >
        <IconLoader2 className="project-workspace__spinner" size={28} />
        <strong>{t("projectWorkspacePage.loading.title")}</strong>
        <p>{t("projectWorkspacePage.loading.description")}</p>
      </main>
    );
  }
  if (error || !data) {
    return (
      <main className="project-workspace project-workspace__state" role="alert">
        <IconAlertTriangle size={28} />
        <strong>{t("projectWorkspacePage.errors.loadTitle")}</strong>
        <p>{error || t("projectWorkspacePage.errors.noProjectData")}</p>
        <Button variant="secondary" onClick={() => void load()}>
          <IconRefresh size={16} />
          {t("projectWorkspacePage.actions.reload")}
        </Button>
      </main>
    );
  }
  const selectedCommitId = normalizedSearchParams.get("commit") || "";
  const selectedCommit =
    data.commits.find(
      (commit) =>
        text(commit, "commit", "hash", "commit_hash", "id") ===
        selectedCommitId,
    ) ||
    data.commits[0] ||
    null;
  const activeDomain = workspaceDomainForTab(tab);
  const isOwner = data.project.access_role === "owner";
  const selectWorkItem = (id: string) =>
    updateWorkspaceUrl(
      projectWorkItemUrlPatch(id || undefined, selectedWorkItemId || undefined),
    );
  const selectMember = (id: string) =>
    updateWorkspaceUrl({ member: id || undefined });
  const selectCommit = (commit: RecordValue) =>
    updateWorkspaceUrl({
      commit: text(commit, "commit", "hash", "commit_hash", "id") || undefined,
    });

  const renderContent = () => {
    switch (tab) {
      case "cockpit":
        return (
          <Cockpit
            data={data}
            onNavigate={navigateWorkspace}
            onOpenSession={openSession}
          />
        );
      case "work":
        return selectedWorkItemId ? (
          <WorkItemDetail
            projectId={projectId}
            items={data.workItems}
            members={data.members}
            runs={data.runs}
            events={data.events}
            files={data.files}
            commits={data.commits}
            selectedId={selectedWorkItemId}
            onSelect={selectWorkItem}
            onNavigate={navigateWorkspace}
            onOpenSession={openSession}
            runAction={runAction}
            busyAction={busyAction}
            canManage={canEdit}
          />
        ) : (
          <WorkBoard
            projectId={projectId}
            items={data.workItems}
            members={data.members}
            runs={data.runs}
            onSelect={selectWorkItem}
            runAction={runAction}
            busyAction={busyAction}
            canManage={canEdit}
          />
        );
      case "group":
        return (
          <GroupChatPanel
            projectId={projectId}
            project={data.project}
            members={data.members}
            groupSession={data.groupSession}
            groupConfig={groupConfig}
            canSend={
              canEdit && ["planning", "running"].includes(data.project.status)
            }
          />
        );
      case "mesh":
        return (
          <MeshPanel
            members={data.members}
            events={data.events}
            runs={data.runs}
            onNavigate={navigateWorkspace}
            onOpenSession={openSession}
          />
        );
      case "files":
        return (
          <FilesPanel
            projectId={projectId}
            files={data.files}
            members={data.members}
            projectAgents={data.projectAgents}
            runAction={runAction}
            busyAction={busyAction}
            canWrite={canEdit}
          />
        );
      case "milestones":
        return (
          <MilestonesPanel
            projectId={projectId}
            milestones={data.milestones}
            commits={data.commits}
            events={data.events}
            workItems={data.workItems}
            runs={data.runs}
            files={data.files}
            members={data.members}
            onOpenWorkItem={(id) => navigateWorkspace("work", { workItem: id })}
            onOpenSession={openSession}
            runAction={runAction}
            busyAction={busyAction}
            canManage={canEdit}
          />
        );
      case "runs":
        return (
          <RunsPanel
            projectId={projectId}
            runs={data.runs}
            members={data.members}
            workItems={data.workItems}
            onOpenSession={openSession}
            runAction={runAction}
            busyAction={busyAction}
            canManage={canEdit}
          />
        );
      case "members":
        return (
          <MembersPanel
            projectId={projectId}
            projectAgents={data.projectAgents}
            members={data.members}
            capabilities={data.capabilities}
            policies={data.policies}
            events={data.events}
            canManage={isOwner}
            selectedId={selectedMemberId}
            onSelect={selectMember}
            onOpenWorkspace={(path) =>
              navigateWorkspace("files", { file: path })
            }
            onNavigate={navigateWorkspace}
            runAction={runAction}
            busyAction={busyAction}
          />
        );
      case "capabilities":
        return (
          <CapabilitiesPanel
            members={data.members}
            capabilities={data.capabilities}
            policies={data.policies}
          />
        );
      case "matrix":
        return (
          <CapabilitiesPanel
            members={data.members}
            capabilities={data.capabilities}
            policies={data.policies}
          />
        );
      case "policies":
        return (
          <PoliciesPanel
            projectId={projectId}
            project={data.project}
            policies={data.policies}
            onReload={load}
            runAction={runAction}
            busyAction={busyAction}
          />
        );
      case "git":
        return (
          <GitPanel
            projectId={projectId}
            project={data.project}
            repository={data.gitRepository}
            commits={data.commits}
            events={data.events}
            selected={selectedCommit}
            onSelect={selectCommit}
            onDialog={setGitDialog}
            onOpenSession={openSession}
            onReload={load}
          />
        );
      case "audit":
        return (
          <AuditPanel
            events={data.events}
            members={data.members}
            onRefresh={load}
            onOpenSession={openSession}
          />
        );
    }
  };

  return (
    <main
      className={`project-workspace${tab === "files" ? " project-workspace--files" : ""}`}
    >
      <header className="project-workspace__header">
        <div className="project-workspace__project-mark">
          {data.project.name.slice(0, 1).toUpperCase()}
        </div>
        <div className="project-workspace__project-copy">
          <div>
            <h1>{data.project.name}</h1>
            <StatusPill status={data.project.status} />
            <span className="project-workspace__visibility">
              <IconLock size={12} />
              {t(
                data.project.visibility === "shared"
                  ? "projectWorkspacePage.visibility.shared"
                  : "projectWorkspacePage.visibility.private",
              )}
            </span>
          </div>
          <p>
            {projectUserFacingCopy(
              data.project.objective ||
                data.project.description ||
                t("projectCockpit.objectiveFallback"),
              t,
            )}
          </p>
        </div>
        {isOwner && ["running", "paused", "waiting"].includes(data.project.status) && (
          <Button
            variant={data.project.status === "running" ? "secondary" : "primary"}
            disabled={busyAction === "project-runtime"}
            onClick={() =>
              setRuntimeDialog(
                data.project.status === "running" ? "pause" : "resume",
              )
            }
          >
            {busyAction === "project-runtime" ? (
              <IconLoader2 className="project-workspace__spinner" size={16} />
            ) : data.project.status === "running" ? (
              <IconPlayerPause size={16} />
            ) : (
              <IconPlayerPlay size={16} />
            )}
            {t(
              data.project.status === "running"
                ? "projectRuntime.pauseAction"
                : "projectRuntime.resumeAction",
            )}
          </Button>
        )}
        <Button
          variant="secondary"
          onClick={() => navigateWorkspace("policies")}
        >
          <IconSettings size={16} />
          {t("projectWorkspacePage.actions.projectSettings")}
        </Button>
      </header>

      <div className="project-workspace__body">
        <aside
          className="project-workspace__nav"
          aria-label={t("projectWorkspacePage.navigationAria")}
        >
          <section>
            <h2>{t("projectWorkspaceNav.groups.workspace")}</h2>
            {WORKSPACE_DOMAINS.map((domain) => {
              const Icon = domain.icon;
              const isActive = activeDomain?.id === domain.id;
              const a2aCount =
                domain.id === "collaboration"
                  ? data.events.filter(
                      (event) => inferredSessionIntent(event) === "a2a",
                    ).length
                  : 0;
              return (
                <Button
                  type="button"
                  variant="ghost"
                  key={domain.id}
                  className={isActive ? "is-active" : ""}
                  aria-current={isActive ? "page" : undefined}
                  title={t(domain.labelKey)}
                  onClick={() => navigateWorkspace(domain.defaultTab)}
                >
                  <Icon size={17} />
                  <span>{t(domain.labelKey)}</span>
                  {a2aCount > 0 && (
                    <ProjectCountBadge>{a2aCount}</ProjectCountBadge>
                  )}
                </Button>
              );
            })}
          </section>
        </aside>
        <WorkspaceNavigationContext.Provider value={workspaceNavigation}>
          <div
            className={`project-workspace__content${tab === "group" ? " project-workspace__content--chat" : ""}${tab === "cockpit" ? " project-workspace__content--cockpit" : ""}${tab === "files" ? " project-workspace__content--files" : ""}`}
          >
            {activeDomain && activeDomain.tabs.length > 1 && (
              <ProjectSegmentedControl
                value={tab}
                options={activeDomain.tabs.map((item) => ({
                  value: item.id,
                  label: t(item.labelKey),
                }))}
                onChange={(nextTab) => navigateWorkspace(nextTab)}
                ariaLabel={t("projectWorkspaceNav.secondaryAria", {
                  domain: t(activeDomain.labelKey),
                })}
                className="project-workspace__domain-tabs"
              />
            )}
            {data.project.status === "planning" && isOwner && (
              <section
                className="project-workspace__planning-banner"
                role="status"
              >
                <span>
                  <IconSparkles size={18} />
                </span>
                <div>
                  <strong>{t("projectWorkspacePage.planning.title")}</strong>
                  <p>{t("projectTerminology.workspace.planningBanner")}</p>
                </div>
                <Button
                  variant="primary"
                  disabled={busyAction === "kickoff"}
                  onClick={() =>
                    void runAction(
                      "kickoff",
                      () => projectsApi.confirmKickoff(projectId),
                      t("projectTerminology.workspace.planningStarted"),
                    )
                  }
                >
                  {busyAction === "kickoff" ? (
                    <IconLoader2
                      className="project-workspace__spinner"
                      size={16}
                    />
                  ) : (
                    <IconPlayerPlay size={16} />
                  )}
                  {t("projectTerminology.confirmAndStart")}
                </Button>
              </section>
            )}
            {resourceWarnings.length > 0 && (
              <div
                className="project-workspace__resource-warning"
                role="status"
              >
                <IconAlertTriangle size={17} />
                <div>
                  <strong>
                    {t("projectWorkspacePage.resources.warningTitle")}
                  </strong>
                  <p>{resourceWarnings.join("；")}</p>
                </div>
                <Button variant="ghost" onClick={() => void load()}>
                  <IconRefresh size={15} />
                  {t("common.retry")}
                </Button>
              </div>
            )}
            {renderContent()}
          </div>
        </WorkspaceNavigationContext.Provider>
      </div>

      {gitDialog && selectedCommit && (
        <GitActionDialog
          projectId={projectId}
          mode={gitDialog}
          commit={selectedCommit}
          busy={busyAction}
          onClose={() => setGitDialog(null)}
          runAction={runAction}
        />
      )}
      <ProjectDialog
        open={runtimeDialog !== null}
        onClose={() => {
          if (busyAction !== "project-runtime") setRuntimeDialog(null);
        }}
        ariaLabel={t(
          runtimeDialog === "resume"
            ? "projectRuntime.resumeTitle"
            : "projectRuntime.pauseTitle",
        )}
        className="project-workspace__member-dialog"
      >
        <div className="project-workspace__modal">
          <header>
            <div>
              <span>{t("projectRuntime.badge")}</span>
              <h2>
                {t(
                  runtimeDialog === "resume"
                    ? "projectRuntime.resumeTitle"
                    : "projectRuntime.pauseTitle",
                )}
              </h2>
            </div>
            <ProjectIconButton
              aria-label={t("common.close")}
              disabled={busyAction === "project-runtime"}
              onClick={() => setRuntimeDialog(null)}
            >
              <IconX size={18} />
            </ProjectIconButton>
          </header>
          <p>
            {t(
              runtimeDialog === "resume"
                ? "projectRuntime.resumeDescription"
                : "projectRuntime.pauseDescription",
            )}
          </p>
          <footer>
            <Button
              variant="secondary"
              disabled={busyAction === "project-runtime"}
              onClick={() => setRuntimeDialog(null)}
            >
              {t("common.cancel")}
            </Button>
            <Button
              variant={runtimeDialog === "pause" ? "danger" : "primary"}
              disabled={busyAction === "project-runtime"}
              onClick={() => void changeRuntimeStatus()}
            >
              {busyAction === "project-runtime" ? (
                <IconLoader2 className="project-workspace__spinner" size={16} />
              ) : runtimeDialog === "pause" ? (
                <IconPlayerPause size={16} />
              ) : (
                <IconPlayerPlay size={16} />
              )}
              {t(
                runtimeDialog === "pause"
                  ? "projectRuntime.confirmPause"
                  : "projectRuntime.confirmResume",
              )}
            </Button>
          </footer>
        </div>
      </ProjectDialog>
      <SessionViewerDrawer
        agentId={sessionTarget?.agentId || ""}
        agentName={
          sessionTarget?.agentName ||
          t("projectTerminology.dynamicCopy.digitalEmployee")
        }
        target={sessionTarget}
        interactive={canEdit}
        groupConfig={sessionTarget?.kind === "group" ? groupConfig : undefined}
        onClose={closeSessionTarget}
      />
    </main>
  );
}

function isProjectRiskEvent(event: RecordValue): boolean {
  const severity = text(event, "severity", "level", "tone").toLowerCase();
  const kind = text(event, "event_type", "type").toLowerCase();
  return (
    ["warning", "error", "critical"].includes(severity) ||
    /(risk|issue|block|fail|error|timeout|reject)/.test(kind)
  );
}

const INTERNAL_UUID_PATTERN =
  /\b[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\b/gi;

function redactInternalUuids(value: string, replacement: string): string {
  return value.replace(INTERNAL_UUID_PATTERN, replacement);
}

function Cockpit({
  data,
  onNavigate,
  onOpenSession,
}: {
  data: WorkspaceData;
  onNavigate: (tab: WorkspaceTab, patch?: WorkspaceUrlPatch) => void;
  onOpenSession: OpenSession;
}) {
  const { t } = useTranslation();
  const runningItems = data.workItems.filter((item) =>
    ["doing", "running", "in_progress"].includes(text(item, "status", "state")),
  );
  const completedItems = data.workItems.filter((item) =>
    ["done", "completed", "success", "succeeded"].includes(
      text(item, "status", "state"),
    ),
  );
  const blockedItems = data.workItems.filter((item) =>
    ["blocked", "failed"].includes(text(item, "status", "state")),
  );
  const activeMembers = data.members.filter(
    (member) => member.is_enabled !== false,
  );
  const leader = activeMembers.find((member) => bool(member, "is_leader"));
  const projectRecord = obj(data.project);
  const successCriteria = Array.isArray(projectRecord.success_criteria)
    ? (projectRecord.success_criteria as unknown[]).map(String).filter(Boolean)
    : [];
  const memberNameByAgentId = useMemo(
    () =>
      new Map(
        data.members.map((member) => [
          text(member, "agent_id"),
          text(member, "name_snapshot", "agent_name", "name") ||
            t("projectCockpit.unnamedMember"),
        ]),
      ),
    [data.members],
  );
  const assigneeName = (item: RecordValue) =>
    text(item, "assignee_name", "owner_name", "agent_name") ||
    memberNameByAgentId.get(text(item, "assignee_agent_id")) ||
    t("projectCockpit.unassigned");
  const workStateOrder = (item: RecordValue) => {
    const status = text(item, "status", "state");
    if (["doing", "running", "in_progress"].includes(status)) return 0;
    if (["review", "waiting_approval"].includes(status)) return 1;
    if (["blocked", "failed"].includes(status)) return 2;
    if (["todo", "backlog", "queued", "waiting"].includes(status)) return 3;
    return 4;
  };
  const actionableItems = data.workItems.filter((item) =>
    [
      "doing",
      "running",
      "in_progress",
      "review",
      "waiting_approval",
      "todo",
      "backlog",
      "queued",
      "waiting",
    ].includes(text(item, "status", "state")),
  );
  const updatedAtDescending = (left: RecordValue, right: RecordValue) =>
    new Date(String(right.updated_at || 0)).getTime() -
    new Date(String(left.updated_at || 0)).getTime();
  const displayedItems = actionableItems.length
    ? [...actionableItems].sort(
        (left, right) =>
          workStateOrder(left) - workStateOrder(right) ||
          updatedAtDescending(left, right),
      )
    : [...completedItems].sort(updatedAtDescending);
  const riskEvents = data.events.filter(isProjectRiskEvent);
  const riskEntries = [
    ...blockedItems.map((item) => ({
      id: `work:${text(item, "id", "work_item_id")}`,
      kind: "work" as const,
      title: text(item, "title", "name") || t("projectCockpit.blockedWorkItem"),
      description:
        text(item, "blocked_reason", "error", "description") ||
        t("projectCockpit.blockedWorkItemHint"),
      time: item.updated_at,
      source: item,
    })),
    ...riskEvents.map((event) => {
      const eventCode = text(event, "event_type", "type");
      return {
        id: `event:${text(event, "id", "event_id") || text(event, "created_at")}`,
        kind: "event" as const,
        title: t(`projectAudit.events.${eventCode}`, {
          defaultValue: t("projectAudit.eventFallback"),
        }),
        description: t("projectAudit.riskEventHint"),
        time: event.created_at,
        source: event,
      };
    }),
  ]
    .filter(
      (entry, index, entries) =>
        entries.findIndex((candidate) => candidate.id === entry.id) === index,
    )
    .sort(
      (left, right) =>
        new Date(String(right.time || 0)).getTime() -
        new Date(String(left.time || 0)).getTime(),
    );
  const openRiskEntry = (entry: (typeof riskEntries)[number]) => {
    if (entry.kind === "work") {
      onNavigate("work", {
        workItem: text(entry.source, "id", "work_item_id") || undefined,
      });
      return;
    }

    const eventRoute = sessionRouteOf(
      entry.source,
      inferredSessionIntent(entry.source),
    );
    if (eventRoute?.anchorMessageId) {
      onOpenSession(entry.source, entry.title);
      return;
    }
    const eventId = text(entry.source, "id", "event_id");
    onNavigate("audit", {
      auditScope: "risks",
      auditEvent: eventId || undefined,
      auditType:
        eventId || !text(entry.source, "event_type", "type")
          ? undefined
          : text(entry.source, "event_type", "type"),
    });
  };
  const visibleCockpitItems = displayedItems.slice(0, 5);
  const visibleRiskEntries = riskEntries.slice(0, 5);
  const projectObjective = projectUserFacingCopy(
    data.project.objective || t("projectCockpit.objectiveFallback"),
    t,
  );
  const projectDescription = projectUserFacingCopy(
    data.project.description ||
      t("projectTerminology.workspace.projectDescriptionFallback"),
    t,
  );
  return (
    <>
      <SectionHeading
        eyebrow="PROJECT COCKPIT"
        title={t("projectCockpit.title")}
        description={t("projectCockpit.description")}
        actions={
          <Button variant="primary" onClick={() => onNavigate("group")}>
            <IconMessageCircle size={16} />
            {t("projectCockpit.openGroupChat")}
          </Button>
        }
      />
      <section className="project-workspace__project-brief">
        <span className="project-workspace__project-brief-icon">
          <IconTargetArrow size={22} />
        </span>
        <div className="project-workspace__project-brief-copy">
          <span>{t("projectCockpit.objective")}</span>
          <h3 title={projectObjective}>{projectObjective}</h3>
          <p title={projectDescription}>{projectDescription}</p>
        </div>
        <dl className="project-workspace__project-brief-meta">
          <div>
            <dt>{t("projectTerminology.owner")}</dt>
            <dd>
              {text(leader || {}, "name_snapshot", "agent_name", "name") ||
                data.project.leader_name ||
                t("projectCockpit.pendingAssignment")}
            </dd>
          </div>
          <div>
            <dt>{t("projectCockpit.members")}</dt>
            <dd>
              {t("projectCockpit.activeMembers", {
                count: activeMembers.length,
              })}
            </dd>
          </div>
          <div>
            <dt>{t("projectCockpit.acceptanceCriteria")}</dt>
            <dd>
              {successCriteria.length
                ? t("projectCockpit.criteriaCount", {
                    count: successCriteria.length,
                  })
                : t("projectCockpit.criteriaMaintainedWithPlan")}
            </dd>
          </div>
          <div>
            <dt>{t("projectCockpit.projectProgress")}</dt>
            <dd>
              {Math.round(
                data.project.progress ||
                  (data.workItems.length
                    ? (completedItems.length / data.workItems.length) * 100
                    : 0),
              )}
              %
            </dd>
          </div>
        </dl>
      </section>
      <div className="project-workspace__metric-strip">
        <article>
          <span>{t("projectCockpit.workItems")}</span>
          <strong>{data.workItems.length}</strong>
          <small>
            {t("projectCockpit.workItemSummary", {
              running: runningItems.length,
              completed: completedItems.length,
            })}
          </small>
        </article>
        <article>
          <span>{t("projectCockpit.risks")}</span>
          <strong className={riskEntries.length ? "is-danger" : ""}>
            {riskEntries.length}
          </strong>
          <small>
            {riskEntries.length
              ? t("projectCockpit.needsFollowUp")
              : t("projectCockpit.noKnownIssues")}
          </small>
        </article>
        <article>
          <span>{t("projectCockpit.activeMemberMetric")}</span>
          <strong>{activeMembers.length}</strong>
          <small>
            {t("projectCockpit.runCount", { count: data.runs.length })}
          </small>
        </article>
        <article>
          <span>{t("projectCockpit.collaborationEvents")}</span>
          <strong>{data.events.length}</strong>
          <small>{t("projectCockpit.collaborationEventsHint")}</small>
        </article>
      </div>
      <div className="project-workspace__two-column">
        <section className="project-workspace__card">
          <header>
            <div>
              <span>{t("projectCockpit.executionOverview")}</span>
              <h3>{t("projectCockpit.workItemProgress")}</h3>
            </div>
            <Button variant="ghost" onClick={() => onNavigate("work")}>
              {t("projectCockpit.viewAll")} <IconArrowRight size={14} />
            </Button>
          </header>
          {displayedItems.length ? (
            <>
              <div className="project-workspace__compact-list">
                {visibleCockpitItems.map((item) => (
                  <Button
                    variant="ghost"
                    className="project-workspace__compact-item"
                    key={text(item, "id", "work_item_id")}
                    onClick={() =>
                      onNavigate("work", {
                        workItem: text(item, "id", "work_item_id"),
                      })
                    }
                  >
                    <StatusPill status={text(item, "status", "state")} />
                    <div>
                      <strong>{text(item, "title", "name")}</strong>
                      <small>
                        {assigneeName(item)} · {dateLabel(item.updated_at)}
                      </small>
                    </div>
                    <IconChevronRight size={15} />
                  </Button>
                ))}
              </div>
            </>
          ) : (
            <EmptyState
              icon={<IconArchive size={22} />}
              title={t("projectCockpit.noWorkItems")}
              description={t(
                "projectTerminology.workspace.workItemsAfterPlanning",
              )}
            />
          )}
        </section>
        <section className="project-workspace__card">
          <header>
            <div>
              <span>{t("projectCockpit.attentionNeeded")}</span>
              <h3>{t("projectCockpit.risks")}</h3>
            </div>
            <div className="project-workspace__card-actions">
              {blockedItems.length ? (
                <ProjectIconButton
                  onClick={() => onNavigate("work", { workView: "list" })}
                  aria-label={t("projectCockpit.viewBlockedWorkItems")}
                  title={t("projectCockpit.viewBlockedWorkItems")}
                >
                  <IconChecklist size={15} />
                </ProjectIconButton>
              ) : null}
              {riskEvents.length ? (
                <ProjectIconButton
                  onClick={() => onNavigate("audit", { auditScope: "risks" })}
                  aria-label={t("projectCockpit.viewRiskEvents")}
                  title={t("projectCockpit.viewRiskEvents")}
                >
                  <IconHistory size={15} />
                </ProjectIconButton>
              ) : null}
              <ProjectCountBadge className="project-workspace__count">
                {riskEntries.length}
              </ProjectCountBadge>
            </div>
          </header>
          {riskEntries.length ? (
            <>
              <div className="project-workspace__risk-list">
                {visibleRiskEntries.map((entry) => (
                  <Button
                    variant="ghost"
                    className="project-workspace__risk-entry"
                    key={entry.id}
                    onClick={() => openRiskEntry(entry)}
                  >
                    <IconAlertTriangle size={18} />
                    <div>
                      <strong>{entry.title}</strong>
                      <p>{entry.description}</p>
                      <time>{dateLabel(entry.time)}</time>
                    </div>
                    <IconChevronRight size={15} />
                  </Button>
                ))}
              </div>
            </>
          ) : (
            <EmptyState
              icon={<IconCircleCheck size={22} />}
              title={t("projectCockpit.noKnownRisks")}
              description={t("projectCockpit.noKnownRisksHint")}
            />
          )}
        </section>
      </div>
    </>
  );
}

function WorkBoard({
  projectId,
  items,
  members,
  runs,
  onSelect,
  runAction,
  busyAction,
  canManage,
}: {
  projectId: string;
  items: RecordValue[];
  members: RecordValue[];
  runs: RecordValue[];
  onSelect: (id: string) => void;
  runAction: (
    key: string,
    action: () => Promise<unknown>,
    success: string,
  ) => Promise<boolean>;
  busyAction: string;
  canManage: boolean;
}) {
  const { t } = useTranslation();
  const { get, update } = useContext(WorkspaceNavigationContext);
  const [showCreate, setShowCreate] = useState(false);
  const requestedView = get("workView");
  const view: "graph" | "board" | "list" = ["board", "list"].includes(
    requestedView,
  )
    ? (requestedView as "board" | "list")
    : "graph";
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [assignee, setAssignee] = useState("");
  const [priority, setPriority] = useState("medium");
  const [acceptance, setAcceptance] = useState("");
  const columns = [
    { key: "todo", label: t("projectGraphs.status.todo") },
    { key: "doing", label: t("projectGraphs.status.doing") },
    { key: "review", label: t("projectGraphs.status.review") },
    { key: "done", label: t("projectGraphs.status.done") },
  ] as const;
  const groupFor = (item: RecordValue) => {
    const status = text(item, "status", "state");
    if (["completed", "success", "done"].includes(status)) return "done";
    if (["running", "doing", "in_progress"].includes(status)) return "doing";
    if (["review", "waiting_approval"].includes(status)) return "review";
    return "todo";
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    void runAction(
      "create-work",
      () =>
        projectsApi.createWorkItem(projectId, {
          title: title.trim(),
          description: description.trim(),
          assignee_agent_id: assignee || null,
          priority,
          status: "todo",
          acceptance_criteria: acceptance
            .split("\n")
            .map((line) => line.trim())
            .filter(Boolean),
        }),
      t("projectWorkspacePage.workItems.feedback.created"),
    ).then((ok) => {
      if (ok) {
        setShowCreate(false);
        setTitle("");
        setDescription("");
        setAcceptance("");
      }
    });
  };
  const memberOptions = members
    .filter((member) => member.is_enabled !== false)
    .map((member) => ({
      value: text(member, "agent_id"),
      label: text(member, "name_snapshot", "agent_name"),
    }));
  const priorityOptions = [
    { value: "low", label: t("projectWorkspacePage.priority.low") },
    { value: "medium", label: t("projectWorkspacePage.priority.medium") },
    { value: "high", label: t("projectWorkspacePage.priority.high") },
    { value: "urgent", label: t("projectWorkspacePage.priority.urgent") },
  ];
  const priorityLabels = new Map(
    priorityOptions.map((option) => [option.value, option.label]),
  );
  const memberNameByAgentId = new Map(
    members.map((member) => [
      text(member, "agent_id"),
      text(member, "name_snapshot", "agent_name", "name"),
    ]),
  );
  const itemAssignee = (item: RecordValue) =>
    text(item, "assignee_name", "owner_name", "agent_name") ||
    memberNameByAgentId.get(text(item, "assignee_agent_id")) ||
    t("projectCockpit.unassigned");
  const graphItems = items.map((item) => ({
    ...item,
    assignee_name: itemAssignee(item),
  }));
  return (
    <>
      <SectionHeading
        eyebrow="OBJECTIVES / WORK GRAPH"
        title={t("projectWorkspacePage.workItems.title")}
        description={t("projectWorkspacePage.workItems.description")}
        actions={
          <>
            <ProjectSegmentedControl
              value={view}
              options={[
                {
                  value: "graph",
                  label: t("projectWorkspacePage.workItems.views.graph"),
                },
                {
                  value: "board",
                  label: t("projectWorkspacePage.workItems.views.board"),
                },
                {
                  value: "list",
                  label: t("projectWorkspacePage.workItems.views.list"),
                },
              ]}
              onChange={(next) => update({ workView: next })}
              ariaLabel={t("projectWorkspacePage.workItems.views.aria")}
            />
            {canManage && (
              <Button
                variant="primary"
                onClick={() => setShowCreate((value) => !value)}
              >
                <IconPlus size={16} />
                {t("projectWorkspacePage.workItems.actions.create")}
              </Button>
            )}
          </>
        }
      />
      {canManage && showCreate && (
        <form className="project-workspace__action-panel" onSubmit={submit}>
          <header>
            <div>
              <span>NEW WORK ITEM</span>
              <h3>{t("projectWorkspacePage.workItems.create.title")}</h3>
            </div>
            <ProjectIconButton
              aria-label={t("common.close")}
              onClick={() => setShowCreate(false)}
            >
              <IconX size={17} />
            </ProjectIconButton>
          </header>
          <div className="project-workspace__form-grid">
            <ProjectField
              className="is-wide"
              label={t("projectWorkspacePage.workItems.fields.title")}
              labelFor="project-work-title"
              required
            >
              <TextInput
                id="project-work-title"
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                required
                autoFocus
              />
            </ProjectField>
            <ProjectField
              className="is-wide"
              label={t("projectWorkspacePage.workItems.fields.description")}
              labelFor="project-work-description"
            >
              <ProjectTextarea
                id="project-work-description"
                value={description}
                onChange={(event) => setDescription(event.target.value)}
                rows={3}
              />
            </ProjectField>
            <ProjectField
              label={t("projectWorkspacePage.workItems.fields.assignee")}
            >
              <ProjectSelect
                value={assignee}
                options={memberOptions}
                onChange={setAssignee}
                ariaLabel={t("projectWorkspacePage.workItems.fields.assignee")}
                placeholder={t("projectCockpit.unassigned")}
              />
            </ProjectField>
            <ProjectField
              label={t("projectWorkspacePage.workItems.fields.priority")}
            >
              <ProjectSelect
                value={priority}
                options={priorityOptions}
                onChange={setPriority}
                ariaLabel={t("projectWorkspacePage.workItems.fields.priority")}
              />
            </ProjectField>
            <ProjectField
              className="is-wide"
              label={t("projectWorkspacePage.workItems.fields.acceptance")}
              labelFor="project-work-acceptance"
            >
              <ProjectTextarea
                id="project-work-acceptance"
                value={acceptance}
                onChange={(event) => setAcceptance(event.target.value)}
                rows={3}
              />
            </ProjectField>
          </div>
          <footer>
            <Button
              type="button"
              variant="secondary"
              onClick={() => setShowCreate(false)}
            >
              {t("common.cancel")}
            </Button>
            <Button
              type="submit"
              variant="primary"
              disabled={!title.trim() || busyAction === "create-work"}
            >
              {busyAction === "create-work" && (
                <IconLoader2 className="project-workspace__spinner" size={16} />
              )}
              {t("projectWorkspacePage.workItems.actions.create")}
            </Button>
          </footer>
        </form>
      )}
      {items.length ? (
        view === "graph" ? (
          <div className="project-workspace__graph-panel">
            <WorkDependencyGraph
              items={graphItems}
              selectedWorkItemId={undefined}
              onWorkItemSelect={(id) => onSelect(id)}
            />
            <ProjectGraphLegend />
          </div>
        ) : view === "board" ? (
          <div className="project-workspace__kanban">
            {columns.map((column) => {
              const list = items.filter(
                (item) => groupFor(item) === column.key,
              );
              return (
                <section key={column.key} data-column={column.key}>
                  <header>
                    <span>
                      <i />
                      {column.label}
                    </span>
                    <ProjectCountBadge>{list.length}</ProjectCountBadge>
                  </header>
                  <div>
                    {list.map((item) => {
                      const id = text(item, "id", "work_item_id");
                      const itemPriority = text(item, "priority") || "medium";
                      return (
                        <Button
                          variant="ghost"
                          className="project-workspace__kanban-card"
                          key={id}
                          onClick={() => onSelect(id)}
                        >
                          <div className="project-workspace__kanban-card-meta">
                            <code title={id}>{compactId(id)}</code>
                            <em data-priority={itemPriority}>
                              {priorityLabels.get(itemPriority) || itemPriority}
                            </em>
                          </div>
                          <h3
                            title={
                              text(item, "title", "name") ||
                              t("projectGraphs.unnamedWorkItem")
                            }
                          >
                            {text(item, "title", "name") ||
                              t("projectGraphs.unnamedWorkItem")}
                          </h3>
                          <p
                            title={
                              listText(item, "acceptance_criteria") ||
                              text(item, "description") ||
                              t("projectWorkspacePage.workItems.noAcceptance")
                            }
                          >
                            {listText(item, "acceptance_criteria") ||
                              text(item, "description") ||
                              t("projectWorkspacePage.workItems.noAcceptance")}
                          </p>
                          <footer>
                            <span title={itemAssignee(item)}>
                              {itemAssignee(item)}
                            </span>
                            <time>{dateLabel(item.updated_at)}</time>
                          </footer>
                        </Button>
                      );
                    })}
                    {!list.length && (
                      <div className="project-workspace__column-empty">
                        {t("projectCockpit.noWorkItems")}
                      </div>
                    )}
                  </div>
                </section>
              );
            })}
          </div>
        ) : (
          <WorkItemList
            items={items}
            members={members}
            runs={runs}
            onSelect={onSelect}
          />
        )
      ) : (
        <EmptyState
          icon={<IconChecklist size={22} />}
          title={t("projectWorkspacePage.workItems.empty.title")}
          description={t("projectWorkspacePage.workItems.empty.description")}
          action={
            canManage ? (
              <Button variant="primary" onClick={() => setShowCreate(true)}>
                {t("projectWorkspacePage.workItems.actions.createFirst")}
              </Button>
            ) : undefined
          }
        />
      )}
    </>
  );
}

function GroupChatPanel({
  project,
  groupSession,
  groupConfig,
  canSend,
}: {
  projectId: string;
  project: ProjectSummary;
  members: RecordValue[];
  groupSession: RecordValue | null;
  groupConfig: SessionViewerGroupConfig;
  canSend: boolean;
}) {
  const { t } = useTranslation();
  const sessionId = text(
    groupSession || {},
    "id",
    "group_session_id",
    "session_id",
  );
  const agentId = text(
    groupSession || {},
    "access_agent_id",
    "session_agent_id",
    "agent_id",
  );
  const target: SessionViewerTarget | null =
    sessionId && agentId
      ? {
          sessionId,
          agentId,
          title:
            text(groupSession || {}, "title", "group_name") ||
            t("projectWorkspacePage.session.projectChatTitle", {
              project: project.name,
            }),
          mode: "group",
          readOnly: !canSend,
        }
      : null;
  return target ? (
    <SessionViewerDrawer
      embedded
      agentId={agentId}
      agentName={t("projectWorkspacePage.session.projectChat")}
      target={target}
      interactive={canSend}
      groupConfig={groupConfig}
      onClose={() => undefined}
    />
  ) : (
    <ProjectEmptyState
      icon={<IconMessageCircle size={22} />}
      title={t("projectWorkspacePage.session.unavailableTitle")}
      description={t("projectWorkspacePage.session.unavailableDescription")}
    />
  );
}

function MeshPanel({
  members,
  events,
  runs,
  onNavigate,
  onOpenSession,
}: {
  members: RecordValue[];
  events: RecordValue[];
  runs: RecordValue[];
  onNavigate: (tab: WorkspaceTab, patch?: WorkspaceUrlPatch) => void;
  onOpenSession: OpenSession;
}) {
  const { t } = useTranslation();
  const a2aEvents = events.filter(isProjectA2ARecord);
  const latestAnomaly = [...a2aEvents]
    .filter((event) =>
      /(fail|error|timeout|reject|cancel)/.test(
        text(event, "event_type", "type").toLowerCase(),
      ),
    )
    .sort(
      (left, right) =>
        new Date(String(right.created_at || 0)).getTime() -
        new Date(String(left.created_at || 0)).getTime(),
    )[0];
  const sessionForAgent = (agentId: string) =>
    [...runs, ...events]
      .filter((entry) => {
        if (
          inferredSessionIntent(entry) !== "a2a" ||
          sessionRouteOf(entry, "a2a")?.kind !== "session"
        )
          return false;
        const records = traceRecords(entry);
        return [
          traceValue(
            records,
            "agent_id",
            "execution_agent_id",
            "subagent_agent_id",
          ),
          traceValue(records, "from_agent_id", "source_agent_id"),
          traceValue(records, "to_agent_id", "target_agent_id"),
        ].includes(agentId);
      })
      .sort(
        (left, right) =>
          new Date(
            String(right.updated_at || right.created_at || 0),
          ).getTime() -
          new Date(String(left.updated_at || left.created_at || 0)).getTime(),
      )[0];
  return (
    <>
      <SectionHeading
        eyebrow="A2A DIRECT MESH"
        title={t("projectWorkspacePage.mesh.title")}
        description={t("projectTerminology.workspace.meshDescription")}
        actions={
          <Button
            variant="secondary"
            onClick={() => onNavigate("audit", { auditScope: "a2a" })}
          >
            <IconHistory size={16} />
            {t("projectMesh.viewEvents")}
          </Button>
        }
      />
      {latestAnomaly ? (
        <Button
          variant="ghost"
          className="project-workspace__mesh-anomaly"
          onClick={() => {
            if (sessionRouteOf(latestAnomaly, "a2a")) {
              onOpenSession(latestAnomaly, t("projectMesh.recentIssue"), "a2a");
              return;
            }
            onNavigate("audit", {
              auditScope: "a2a",
              auditEvent: text(latestAnomaly, "id", "event_id") || undefined,
              auditType: text(latestAnomaly, "id", "event_id")
                ? undefined
                : text(latestAnomaly, "event_type", "type"),
            });
          }}
        >
          <IconAlertTriangle size={16} />
          <span>
            <strong>{t("projectMesh.recentIssue")}</strong>
            <small>{dateLabel(latestAnomaly.created_at)}</small>
          </span>
          <IconChevronRight size={15} />
        </Button>
      ) : null}
      <section className="project-workspace__mesh-graph">
        {members.length ? (
          <>
            <A2AMeshGraph
              members={members}
              events={a2aEvents}
              onAgentSelect={(agentId, member) => {
                const source = sessionForAgent(agentId);
                onOpenSession(
                  source || { ...member, agent_id: agentId },
                  t("projectWorkspacePage.session.a2aTitle", {
                    name:
                      text(member, "name_snapshot", "agent_name", "name") ||
                      t("projectTerminology.dynamicCopy.digitalEmployee"),
                  }),
                  "a2a",
                );
              }}
            />
            <ProjectGraphLegend />
          </>
        ) : (
          <EmptyState
            icon={<IconUsers size={22} />}
            title={t("projectWorkspacePage.mesh.emptyTitle")}
            description={t("projectWorkspacePage.mesh.emptyDescription")}
          />
        )}
      </section>
    </>
  );
}

function reportedPercent(...sources: RecordValue[]): number | null {
  for (const source of sources) {
    for (const key of ["progress", "progress_percent", "completion_percent"]) {
      if (
        source[key] === undefined ||
        source[key] === null ||
        source[key] === ""
      )
        continue;
      const value = Number(source[key]);
      if (Number.isFinite(value)) return Math.max(0, Math.min(100, value));
    }
  }
  return null;
}

function WorkItemList({
  items,
  members,
  runs,
  onSelect,
}: {
  items: RecordValue[];
  members: RecordValue[];
  runs: RecordValue[];
  onSelect: (id: string) => void;
}) {
  const { t } = useTranslation();
  const memberNameByAgentId = new Map(
    members.map((member) => [
      text(member, "agent_id"),
      text(member, "name_snapshot", "agent_name", "name") ||
        t("projectAgents.unnamed"),
    ]),
  );
  const priorityLabels = new Map([
    ["low", t("projectWorkspacePage.priority.low")],
    ["medium", t("projectWorkspacePage.priority.medium")],
    ["high", t("projectWorkspacePage.priority.high")],
    ["urgent", t("projectWorkspacePage.priority.urgent")],
  ]);
  const sorted = [...items].sort(
    (left, right) =>
      new Date(String(right.updated_at || 0)).getTime() -
      new Date(String(left.updated_at || 0)).getTime(),
  );
  const { pageItems: visibleItems, pagination } = useWorkspacePagination(
    sorted,
    "workItems",
  );
  return (
    <>
      {sorted.length ? (
        <section
          className="project-workspace__item-list"
          aria-label={t("projectWorkspacePage.workItems.listAria", {
            count: items.length,
          })}
        >
          <header aria-hidden="true">
            <span>
              {t("projectWorkspacePage.workItems.columns.work")}{" "}
              <ProjectCountBadge>{items.length}</ProjectCountBadge>
            </span>
            <span>
              {t("projectWorkspacePage.workItems.columns.statusPriority")}
            </span>
            <span>{t("projectTerminology.owner")}</span>
            <span>{t("projectWorkspacePage.workItems.columns.execution")}</span>
            <span>
              {t("projectWorkspacePage.workItems.columns.acceptance")}
            </span>
            <span>{t("projectWorkspacePage.workItems.columns.updated")}</span>
            <span />
          </header>
          <div>
            {visibleItems.map((item) => {
              const id = text(item, "id", "work_item_id");
              const itemStatus = text(item, "status", "state");
              const relatedRuns = runs
                .filter(
                  (run) =>
                    traceValue(
                      traceRecords(run),
                      "work_item_id",
                      "project_work_item_id",
                      "task_id",
                    ) === id,
                )
                .sort(
                  (left, right) =>
                    new Date(
                      String(right.updated_at || right.created_at || 0),
                    ).getTime() -
                    new Date(
                      String(left.updated_at || left.created_at || 0),
                    ).getTime(),
                );
              const latestRun = relatedRuns[0] || {};
              const progress = reportedPercent(
                item,
                latestRun,
                obj(latestRun.output),
              );
              const criteria = Array.isArray(item.acceptance_criteria)
                ? (item.acceptance_criteria as unknown[])
                    .map(String)
                    .filter(Boolean)
                : [];
              const rawResults = Array.isArray(item.acceptance_results)
                ? (item.acceptance_results as unknown[])
                : Array.isArray(obj(latestRun.output).acceptance_results)
                  ? (obj(latestRun.output).acceptance_results as unknown[])
                  : [];
              const acceptedCount =
                itemStatus === "done"
                  ? criteria.length
                  : rawResults.length
                    ? rawResults.filter(
                        (result) =>
                          [
                            "passed",
                            "accepted",
                            "done",
                            "completed",
                            "success",
                          ].includes(text(obj(result), "status", "result")) ||
                          result === true,
                      ).length
                    : null;
              const assignee =
                memberNameByAgentId.get(text(item, "assignee_agent_id")) ||
                text(item, "assignee_name", "agent_name") ||
                t("projectGraphs.unassigned");
              const priority = text(item, "priority") || "medium";
              return (
                <Button
                  type="button"
                  variant="ghost"
                  key={id}
                  className="project-workspace__item-list-row"
                  onClick={() => onSelect(id)}
                >
                  <span className="project-workspace__item-list-copy">
                    <code title={id}>{compactId(id)}</code>
                    <strong>
                      {text(item, "title", "name") ||
                        t("projectGraphs.unnamedWorkItem")}
                    </strong>
                    <small>
                      {text(item, "description") ||
                        t("projectWorkspacePage.workItems.noDescription")}
                    </small>
                  </span>
                  <span className="project-workspace__item-list-state">
                    <StatusPill status={itemStatus} />
                    <em data-priority={priority}>
                      {priorityLabels.get(priority) || priority}
                    </em>
                  </span>
                  <span
                    className="project-workspace__item-list-owner"
                    title={assignee}
                  >
                    {assignee}
                  </span>
                  <div className="project-workspace__item-list-progress">
                    {progress === null ? (
                      <>
                        <strong>
                          {[
                            "done",
                            "completed",
                            "succeeded",
                            "success",
                          ].includes(itemStatus)
                            ? t("projectGraphs.status.completed")
                            : t("projectWorkspacePage.workItems.notReported")}
                        </strong>
                        <small>
                          {text(latestRun, "id", "run_id")
                            ? `Run ${compactId(text(latestRun, "id", "run_id"))}`
                            : [
                                  "done",
                                  "completed",
                                  "succeeded",
                                  "success",
                                ].includes(itemStatus)
                              ? t(
                                  "projectWorkspacePage.workItems.statusCompleted",
                                )
                              : t(
                                  "projectWorkspacePage.workItems.noExecutionData",
                                )}
                        </small>
                      </>
                    ) : (
                      <>
                        <strong>{Math.round(progress)}%</strong>
                        <ProjectProgressBar
                          value={progress}
                          label={t(
                            "projectWorkspacePage.workItems.executionAria",
                            {
                              title: text(item, "title", "name"),
                            },
                          )}
                          showValue={false}
                        />
                      </>
                    )}
                  </div>
                  <span className="project-workspace__item-list-acceptance">
                    <strong>
                      {acceptedCount === null
                        ? t(
                            "projectWorkspacePage.workItems.acceptanceNotReported",
                          )
                        : `${acceptedCount} / ${criteria.length}`}
                    </strong>
                    <small>
                      {criteria.length
                        ? t("projectWorkspacePage.workItems.acceptanceCount", {
                            count: criteria.length,
                          })
                        : t("projectWorkspacePage.workItems.noAcceptance")}
                    </small>
                  </span>
                  <time dateTime={text(item, "updated_at")}>
                    {dateLabel(item.updated_at)}
                  </time>
                  <IconChevronRight size={16} />
                </Button>
              );
            })}
          </div>
          {pagination}
        </section>
      ) : (
        <EmptyState
          icon={<IconChecklist size={22} />}
          title={t("projectCockpit.noWorkItems")}
          description={t(
            "projectTerminology.workspace.workItemsAfterBreakdown",
          )}
        />
      )}
    </>
  );
}

function WorkItemDetail({
  projectId,
  items,
  members,
  runs,
  events,
  files,
  commits,
  selectedId,
  onSelect,
  onNavigate,
  onOpenSession,
  runAction,
  busyAction,
  canManage,
}: {
  projectId: string;
  items: RecordValue[];
  members: RecordValue[];
  runs: RecordValue[];
  events: RecordValue[];
  files: RecordValue[];
  commits: RecordValue[];
  selectedId: string;
  onSelect: (id: string) => void;
  onNavigate: (tab: WorkspaceTab, patch?: WorkspaceUrlPatch) => void;
  onOpenSession: OpenSession;
  runAction: (
    key: string,
    action: () => Promise<unknown>,
    success: string,
  ) => Promise<boolean>;
  busyAction: string;
  canManage: boolean;
}) {
  const { t } = useTranslation();
  const item = selectedId
    ? items.find((entry) => text(entry, "id", "work_item_id") === selectedId)
    : undefined;
  const itemId = text(item || {}, "id", "work_item_id");
  const [assignee, setAssignee] = useState("");
  const [status, setStatus] = useState("todo");
  const [priority, setPriority] = useState("medium");
  const [editing, setEditing] = useState(false);
  const [associationPayload, setAssociationPayload] =
    useState<RecordValue | null>(null);

  useEffect(() => {
    setAssignee(text(item || {}, "assignee_agent_id"));
    setStatus(text(item || {}, "status") || "todo");
    setPriority(text(item || {}, "priority") || "medium");
    setEditing(false);
  }, [item]);

  useEffect(() => {
    let active = true;
    setAssociationPayload(null);
    if (!itemId)
      return () => {
        active = false;
      };
    void projectsApi
      .getWorkItemDetail(projectId, itemId)
      .then((payload) => {
        if (active) setAssociationPayload(obj(payload));
      })
      .catch(() => {
        if (active) setAssociationPayload({});
      });
    return () => {
      active = false;
    };
  }, [itemId, projectId]);

  useEffect(() => {
    if (!itemId) return;
    const target = projectWorkItemCompatibilityTargetFromUrl(
      new URLSearchParams(window.location.search),
    );
    if (!target || target.workItemId !== itemId) return;
    const frame = window.requestAnimationFrame(() => {
      document.getElementById(target.anchor)?.scrollIntoView({
        block: "start",
      });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [associationPayload, itemId]);

  const save = async () => {
    const saved = await runAction(
      "save-work",
      () =>
        projectsApi.patchWorkItem(projectId, itemId, {
          assignee_agent_id: assignee || null,
          status,
          priority,
        }),
      t("projectWorkspacePage.workItems.feedback.saved"),
    );
    if (saved) setEditing(false);
  };

  const startRun = () =>
    void runAction(
      "run-work",
      () =>
        projectsApi.createRun(projectId, {
          work_item_id: itemId,
          agent_id: assignee || undefined,
          input: {
            objective: text(item || {}, "title"),
            task: text(item || {}, "description") || text(item || {}, "title"),
          },
        }),
      t("projectWorkspacePage.workItems.feedback.runCreated"),
    );

  const approve = () =>
    void runAction(
      "review-approve",
      () =>
        projectsApi.patchWorkItem(projectId, itemId, {
          status: "done",
        }),
      t("projectWorkspacePage.workItems.feedback.approved"),
    );

  const returnForChanges = () =>
    void runAction(
      "review-return",
      () =>
        projectsApi.patchWorkItem(projectId, itemId, {
          status: "blocked",
        }),
      t("projectWorkspacePage.workItems.feedback.returned"),
    );

  const cancelEdit = () => {
    setAssignee(text(item || {}, "assignee_agent_id"));
    setStatus(text(item || {}, "status") || "todo");
    setPriority(text(item || {}, "priority") || "medium");
    setEditing(false);
  };

  const currentAssigneeId = text(item || {}, "assignee_agent_id");
  const memberOptions = members
    .filter(
      (member) =>
        member.is_enabled !== false ||
        text(member, "agent_id") === currentAssigneeId,
    )
    .map((member) => ({
      value: text(member, "agent_id"),
      label:
        text(member, "name_snapshot", "agent_name") +
        (member.is_enabled === false
          ? t("projectWorkspacePage.workItems.historicalAssigneeSuffix")
          : ""),
      disabled: member.is_enabled === false,
    }));
  const statusOptions = [
    { value: "backlog", label: t("projectGraphs.status.backlog") },
    { value: "todo", label: t("projectGraphs.status.todo") },
    { value: "in_progress", label: t("projectGraphs.status.in_progress") },
    { value: "review", label: t("projectGraphs.status.review") },
    { value: "blocked", label: t("projectGraphs.status.blocked") },
  ];
  const priorityOptions = [
    { value: "low", label: t("projectWorkspacePage.priority.low") },
    { value: "medium", label: t("projectWorkspacePage.priority.medium") },
    { value: "high", label: t("projectWorkspacePage.priority.high") },
    { value: "urgent", label: t("projectWorkspacePage.priority.urgent") },
  ];
  const memberNameByAgentId = new Map(
    members.map((member) => [
      text(member, "agent_id"),
      text(member, "name_snapshot", "agent_name", "name") ||
        t("projectAgents.unnamed"),
    ]),
  );
  const matchesWorkItem = (entry: RecordValue) => {
    if (!itemId) return false;
    return (
      traceValue(
        traceRecords(entry),
        "work_item_id",
        "project_work_item_id",
        "task_id",
      ) === itemId
    );
  };
  const explicitlyRelatedRunIds = new Set([
    ...traceStringValues(item || {}, "run_id", "related_run_ids"),
    ...arr(obj(item || {}).related_runs)
      .map((run) => text(run, "id", "run_id"))
      .filter(Boolean),
  ]);
  const dtoRuns = pickCollection(associationPayload || {}, "runs");
  const relatedRuns = Array.isArray(associationPayload?.runs)
    ? dtoRuns
    : runs.filter(
        (run) =>
          matchesWorkItem(run) ||
          explicitlyRelatedRunIds.has(text(run, "id", "run_id")),
      );
  const relatedRunIds = new Set(
    relatedRuns.map((run) => text(run, "id", "run_id")).filter(Boolean),
  );
  const dtoEvents = pickCollection(associationPayload || {}, "events");
  const relatedEvents = Array.isArray(associationPayload?.events)
    ? dtoEvents
    : events.filter(
        (event) =>
          matchesWorkItem(event) ||
          relatedRunIds.has(
            traceValue(traceRecords(event), "run_id", "project_run_id"),
          ),
      );
  const relatedEventCommitIds = new Set(
    relatedEvents
      .map((event) =>
        traceValue(traceRecords(event), "commit_hash", "commit", "hash"),
      )
      .filter(Boolean),
  );
  const relatedEventPaths = new Set(
    relatedEvents
      .map((event) => traceValue(traceRecords(event), "path", "file_path"))
      .filter(Boolean),
  );
  const relatedCommits = Array.isArray(associationPayload?.commits)
    ? pickCollection(associationPayload || {}, "commits")
    : commits.filter((commit) => {
        if (matchesWorkItem(commit)) return true;
        const commitId = text(commit, "commit", "hash", "commit_hash", "id");
        return [...relatedEventCommitIds].some((eventCommitId) =>
          sameGitCommit(commitId, eventCommitId),
        );
      });
  const relatedFiles = Array.isArray(associationPayload?.files)
    ? pickCollection(associationPayload || {}, "files")
    : files.filter(
        (file) =>
          matchesWorkItem(file) ||
          relatedEventPaths.has(text(file, "path", "id", "name")),
      );
  const dtoSessions = pickCollection(associationPayload || {}, "sessions");
  const sessionSources = [
    ...dtoSessions,
    ...relatedRuns,
    ...relatedEvents,
  ].filter((entry, index, source) => {
    const route = sessionRouteOf(entry, inferredSessionIntent(entry));
    if (!route) return false;
    const identity = [route.sessionId, route.anchorMessageId || ""].join(":");
    return (
      source.findIndex((candidate) => {
        const candidateRoute = sessionRouteOf(
          candidate,
          inferredSessionIntent(candidate),
        );
        return (
          candidateRoute &&
          [candidateRoute.sessionId, candidateRoute.anchorMessageId || ""].join(
            ":",
          ) === identity
        );
      }) === index
    );
  });
  const dtoEvidenceRecords = Array.isArray(associationPayload?.evidence)
    ? (associationPayload.evidence as unknown[]).map((entry) =>
        typeof entry === "string" || typeof entry === "number"
          ? { value: String(entry) }
          : obj(entry),
      )
    : [];
  const derivedEvidenceRecords = [
    ...traceStringValues(item || {}, "evidence", "evidence_items").map(
      (value) => ({ value }),
    ),
    ...relatedEvents.flatMap((event) =>
      traceStringValues(event, "evidence", "evidence_items").map((value) => ({
        ...event,
        value,
      })),
    ),
    ...relatedRuns.flatMap((run) =>
      traceStringValues(run, "evidence", "evidence_items", "artifacts").map(
        (value) => ({ ...run, value }),
      ),
    ),
  ];
  const evidenceRecords =
    dtoEvidenceRecords.length || Array.isArray(associationPayload?.evidence)
      ? dtoEvidenceRecords
      : derivedEvidenceRecords;
  const displayEvidenceRecords = evidenceRecords.filter(
    (entry, index, source) => {
      const label = text(entry, "label", "description", "value", "path");
      const route = sessionRouteOf(entry, inferredSessionIntent(entry));
      return (
        Boolean(label) &&
        source.findIndex((candidate) => {
          const candidateRoute = sessionRouteOf(
            candidate,
            inferredSessionIntent(candidate),
          );
          return (
            text(candidate, "label", "description", "value", "path") ===
              label && candidateRoute?.sessionId === route?.sessionId
          );
        }) === index
      );
    },
  );
  const acceptanceCriteria = Array.isArray(item?.acceptance_criteria)
    ? (item.acceptance_criteria as unknown[]).map(String).filter(Boolean)
    : text(item || {}, "acceptance", "acceptance_criteria")
        .split("\n")
        .map((entry) => entry.trim())
        .filter(Boolean);
  const dependencyIds = Array.isArray(item?.dependency_ids)
    ? (item.dependency_ids as unknown[]).map(String)
    : [];
  const activeAssigneeName =
    memberNameByAgentId.get(currentAssigneeId) || t("projectGraphs.unassigned");
  const workStatus = text(item || {}, "status", "state") || "todo";

  const discussionSources = sessionSources.filter((source) => {
    const intent = inferredSessionIntent(source);
    const route = sessionRouteOf(source, intent);
    return intent === "a2a" || route?.kind === "group";
  });
  type ActivityPreview = {
    kind: "execution" | "discussion" | "delivery";
    source: RecordValue;
    createdAt: string;
    key: string;
  };
  const deliverySources = relatedFiles.length ? relatedFiles : relatedCommits;
  const recentActivity: ActivityPreview[] = [
    ...relatedRuns.slice(0, 1).map((source) => ({
      kind: "execution" as const,
      source,
      createdAt: text(source, "updated_at", "created_at", "finished_at"),
      key: ["execution", text(source, "id", "run_id")].join(":"),
    })),
    ...discussionSources.slice(0, 1).map((source) => {
      const route = sessionRouteOf(source, inferredSessionIntent(source));
      return {
        kind: "discussion" as const,
        source,
        createdAt: text(source, "updated_at", "created_at"),
        key: [
          "discussion",
          route?.sessionId || "",
          route?.anchorMessageId || "",
        ].join(":"),
      };
    }),
    ...deliverySources.slice(0, 1).map((source) => ({
      kind: "delivery" as const,
      source,
      createdAt: text(source, "updated_at", "created_at"),
      key: [
        "delivery",
        text(source, "path", "commit", "hash", "commit_hash", "id"),
      ].join(":"),
    })),
  ]
    .sort(
      (left, right) =>
        new Date(right.createdAt || 0).getTime() -
        new Date(left.createdAt || 0).getTime(),
    )
    .slice(0, 3);

  const nextKey = !currentAssigneeId
    ? "unassigned"
    : ["in_progress", "running"].includes(workStatus)
      ? "inProgress"
      : workStatus === "blocked"
        ? "blocked"
        : workStatus === "review"
          ? "review"
          : ["done", "completed"].includes(workStatus)
            ? "done"
            : "todo";

  if (!item)
    return (
      <WorkItemList
        items={items}
        members={members}
        runs={runs}
        onSelect={onSelect}
      />
    );

  const openActivity = (activity: ActivityPreview) => {
    if (activity.kind === "delivery") {
      const path = text(activity.source, "path");
      if (path) {
        onNavigate("files", { file: path });
        return;
      }
      onNavigate("git", {
        commit: text(activity.source, "commit", "hash", "commit_hash", "id"),
      });
      return;
    }
    const intent = inferredSessionIntent(activity.source);
    if (sessionRouteOf(activity.source, intent)) {
      onOpenSession(activity.source, text(item, "title", "name"), intent);
      return;
    }
    onNavigate("runs");
  };

  const nextActions = (() => {
    if (!canManage) {
      if (["done", "completed"].includes(workStatus))
        return (
          <Button variant="primary" onClick={() => onNavigate("files")}>
            {t(
              "projectWorkspacePage.workItems.detail.singlePage.actions.viewDelivery",
            )}
          </Button>
        );
      if (recentActivity.length)
        return (
          <Button variant="primary" onClick={() => onNavigate("runs")}>
            {t(
              "projectWorkspacePage.workItems.detail.singlePage.actions.viewProgress",
            )}
          </Button>
        );
      return null;
    }
    if (!currentAssigneeId)
      return (
        <Button variant="primary" onClick={() => setEditing(true)}>
          {t("projectWorkspacePage.workItems.detail.singlePage.actions.assign")}
        </Button>
      );
    if (workStatus === "review")
      return (
        <>
          <Button
            variant="secondary"
            disabled={busyAction === "review-return"}
            onClick={returnForChanges}
          >
            {busyAction === "review-return" && (
              <IconLoader2 className="project-workspace__spinner" size={16} />
            )}
            {t(
              "projectWorkspacePage.workItems.detail.singlePage.actions.return",
            )}
          </Button>
          <Button
            variant="primary"
            disabled={busyAction === "review-approve"}
            onClick={approve}
          >
            {busyAction === "review-approve" ? (
              <IconLoader2 className="project-workspace__spinner" size={16} />
            ) : (
              <IconCircleCheck size={16} />
            )}
            {t(
              "projectWorkspacePage.workItems.detail.singlePage.actions.approve",
            )}
          </Button>
        </>
      );
    if (["done", "completed"].includes(workStatus))
      return (
        <Button variant="primary" onClick={() => onNavigate("files")}>
          {t(
            "projectWorkspacePage.workItems.detail.singlePage.actions.viewDelivery",
          )}
        </Button>
      );
    if (["in_progress", "running"].includes(workStatus))
      return (
        <Button variant="primary" onClick={() => onNavigate("runs")}>
          {t(
            "projectWorkspacePage.workItems.detail.singlePage.actions.viewProgress",
          )}
        </Button>
      );
    if (workStatus === "blocked")
      return (
        <Button variant="primary" onClick={() => onNavigate("runs")}>
          {t(
            "projectWorkspacePage.workItems.detail.singlePage.actions.viewBlocker",
          )}
        </Button>
      );
    return (
      <Button
        variant="primary"
        disabled={busyAction === "run-work"}
        onClick={startRun}
      >
        {busyAction === "run-work" && (
          <IconLoader2 className="project-workspace__spinner" size={16} />
        )}
        {t("projectWorkspacePage.workItems.detail.singlePage.actions.start")}
      </Button>
    );
  })();

  return (
    <>
      <nav
        className="project-workspace__item-breadcrumb"
        aria-label={t("projectWorkspaceNav.tabs.work")}
      >
        <Button type="button" variant="ghost" onClick={() => onSelect("")}>
          {t("projectWorkspacePage.workItems.detail.singlePage.back")}
        </Button>
      </nav>
      <section className="project-workspace__item-shell">
        <header className="project-workspace__item-heading">
          <div>
            <h2>{text(item, "title", "name")}</h2>
          </div>
          {canManage && !editing && (
            <Button variant="secondary" onClick={() => setEditing(true)}>
              <IconSettings size={16} />
              {t("projectWorkspacePage.workItems.detail.singlePage.edit")}
            </Button>
          )}
        </header>

        <dl className="project-workspace__item-properties">
          <div>
            <dt>{t("projectWorkspacePage.workItems.fields.status")}</dt>
            <dd>
              <StatusPill status={workStatus} />
            </dd>
          </div>
          <div>
            <dt>{t("projectTerminology.owner")}</dt>
            <dd>{activeAssigneeName}</dd>
          </div>
          <div>
            <dt>{t("projectWorkspacePage.workItems.fields.priority")}</dt>
            <dd>
              {priorityOptions.find(
                (option) => option.value === text(item, "priority"),
              )?.label ||
                text(item, "priority") ||
                t("projectWorkspacePage.priority.medium")}
            </dd>
          </div>
          <div>
            <dt>{t("projectWorkspacePage.workItems.columns.updated")}</dt>
            <dd>{dateLabel(item.updated_at)}</dd>
          </div>
        </dl>

        {editing ? (
          <section className="project-workspace__item-edit-panel">
            <div className="project-workspace__item-edit-grid">
              <ProjectField label={t("projectTerminology.owner")}>
                <ProjectSelect
                  value={assignee}
                  options={memberOptions}
                  onChange={setAssignee}
                  ariaLabel={t("projectTerminology.projectOwner")}
                  placeholder={t("projectGraphs.unassigned")}
                />
              </ProjectField>
              <ProjectField
                label={t("projectWorkspacePage.workItems.fields.status")}
              >
                <ProjectSelect
                  value={status}
                  options={statusOptions}
                  onChange={setStatus}
                  ariaLabel={t("projectWorkspacePage.workItems.fields.status")}
                />
              </ProjectField>
              <ProjectField
                label={t("projectWorkspacePage.workItems.fields.priority")}
              >
                <ProjectSelect
                  value={priority}
                  options={priorityOptions}
                  onChange={setPriority}
                  ariaLabel={t(
                    "projectWorkspacePage.workItems.fields.priority",
                  )}
                />
              </ProjectField>
            </div>
            <footer>
              <Button variant="secondary" onClick={cancelEdit}>
                {t("projectWorkspacePage.workItems.detail.singlePage.cancel")}
              </Button>
              <Button
                variant="primary"
                disabled={busyAction === "save-work"}
                onClick={() => void save()}
              >
                {busyAction === "save-work" ? (
                  <IconLoader2
                    className="project-workspace__spinner"
                    size={16}
                  />
                ) : (
                  <IconDeviceFloppy size={16} />
                )}
                {t("projectWorkspacePage.workItems.detail.singlePage.save")}
              </Button>
            </footer>
          </section>
        ) : (
          <section className="project-workspace__item-next-step">
            <div>
              <strong>
                {t("projectWorkspacePage.workItems.detail.singlePage.nextStep")}
              </strong>
              <p>
                {t(
                  "projectWorkspacePage.workItems.detail.singlePage.next." +
                    nextKey,
                )}
              </p>
            </div>
            <div className="project-workspace__item-next-actions">
              {nextActions}
            </div>
          </section>
        )}

        <div className="project-workspace__item-layout">
          <div className="project-workspace__item-primary">
            <section className="project-workspace__item-section">
              <header>
                <h3>
                  {t("projectWorkspacePage.workItems.detail.singlePage.goal")}
                </h3>
              </header>
              <p>
                {projectUserFacingCopy(
                  text(item, "description", "context") ||
                    t("projectWorkspacePage.workItems.noDescription"),
                  t,
                )}
              </p>
            </section>

            <section className="project-workspace__item-section">
              <header>
                <h3>
                  {t(
                    "projectWorkspacePage.workItems.detail.singlePage.requirements",
                  )}
                </h3>
              </header>
              {dependencyIds.length ? (
                <ul className="project-workspace__item-dependencies">
                  {dependencyIds.map((id) => {
                    const dependency = items.find(
                      (entry) => text(entry, "id", "work_item_id") === id,
                    );
                    return (
                      <li key={id}>
                        <Button variant="ghost" onClick={() => onSelect(id)}>
                          <IconChevronRight size={15} />
                          {text(dependency || {}, "title", "name") ||
                            t(
                              "projectWorkspacePage.workItems.detail.context.relatedWorkItem",
                            )}
                        </Button>
                      </li>
                    );
                  })}
                </ul>
              ) : (
                <p>
                  {t(
                    "projectWorkspacePage.workItems.detail.singlePage.noDependencies",
                  )}
                </p>
              )}
            </section>

            <section
              id="work-item-review"
              className="project-workspace__item-section"
            >
              <header>
                <h3>
                  {t(
                    "projectWorkspacePage.workItems.detail.singlePage.acceptanceTitle",
                  )}
                </h3>
                <span>
                  {acceptanceCriteria.length
                    ? t(
                        "projectWorkspacePage.workItems.detail.singlePage.acceptanceCount",
                        { count: acceptanceCriteria.length },
                      )
                    : t("projectWorkspacePage.workItems.noAcceptance")}
                </span>
              </header>
              {acceptanceCriteria.length ? (
                <ul className="project-workspace__item-acceptance-list">
                  {acceptanceCriteria.map((criterion, index) => (
                    <li
                      className={
                        ["done", "completed"].includes(workStatus)
                          ? "is-passed"
                          : ""
                      }
                      key={criterion + "-" + index}
                    >
                      <IconCircleCheck size={17} />
                      <span>{criterion}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p>
                  {t(
                    "projectWorkspacePage.workItems.detail.context.defineAcceptance",
                  )}
                </p>
              )}
              <div className="project-workspace__item-evidence">
                <strong>
                  {displayEvidenceRecords.length
                    ? t(
                        "projectWorkspacePage.workItems.detail.singlePage.evidenceCount",
                        { count: displayEvidenceRecords.length },
                      )
                    : t(
                        "projectWorkspacePage.workItems.detail.singlePage.evidencePending",
                      )}
                </strong>
                {displayEvidenceRecords.slice(0, 3).map((evidence, index) => (
                  <article
                    key={
                      text(evidence, "label", "description", "value", "path") +
                      "-" +
                      index
                    }
                  >
                    <ProjectEventContent
                      content={text(
                        evidence,
                        "label",
                        "description",
                        "value",
                        "path",
                      )}
                      maxChars={180}
                    />
                    <SessionButton
                      source={evidence}
                      onOpen={onOpenSession}
                      intent={inferredSessionIntent(evidence)}
                    />
                  </article>
                ))}
              </div>
            </section>
          </div>

          <aside className="project-workspace__item-side">
            <span id="work-item-conversation" />
            <span id="work-item-changes" />
            <section
              id="work-item-execution"
              className="project-workspace__item-section"
            >
              <header>
                <h3>
                  {t(
                    "projectWorkspacePage.workItems.detail.singlePage.progressTitle",
                  )}
                </h3>
              </header>
              {recentActivity.length ? (
                <>
                  <div className="project-workspace__item-activity-list">
                    {recentActivity.map((activity) => {
                      const activityLabel = t(
                        "projectWorkspacePage.workItems.detail.singlePage.activity." +
                          activity.kind,
                      );
                      const activityContent =
                        activity.kind === "execution"
                          ? text(
                              obj(activity.source.output),
                              "summary",
                              "result",
                              "message",
                            ) ||
                            text(activity.source, "error") ||
                            statusLabel(text(activity.source, "status"), t)
                          : activity.kind === "discussion"
                            ? traceValue(
                                traceRecords(activity.source),
                                "objective",
                                "summary",
                                "message",
                                "task",
                              ) || activityLabel
                            : text(
                                activity.source,
                                "path",
                                "message",
                                "title",
                              ) || activityLabel;
                      const ActivityIcon =
                        activity.kind === "execution"
                          ? IconBolt
                          : activity.kind === "discussion"
                            ? IconMessageCircle
                            : IconFile;
                      return (
                        <article key={activity.key}>
                          <span>
                            <ActivityIcon size={17} />
                          </span>
                          <div>
                            <strong>{activityLabel}</strong>
                            <ProjectEventContent
                              content={activityContent}
                              maxChars={140}
                            />
                            {activity.createdAt && (
                              <time>{dateLabel(activity.createdAt)}</time>
                            )}
                          </div>
                          <Button
                            variant="secondary"
                            onClick={() => openActivity(activity)}
                          >
                            {t(
                              "projectWorkspacePage.workItems.detail.singlePage.activity." +
                                (activity.kind === "execution"
                                  ? "viewResult"
                                  : activity.kind === "discussion"
                                    ? "viewDiscussion"
                                    : "viewDelivery"),
                            )}
                          </Button>
                        </article>
                      );
                    })}
                  </div>
                  <Button
                    variant="secondary"
                    onClick={() => onNavigate("runs")}
                  >
                    {t(
                      "projectWorkspacePage.workItems.detail.singlePage.viewAllProgress",
                    )}
                    <IconArrowRight size={14} />
                  </Button>
                </>
              ) : (
                <p>
                  {t(
                    "projectWorkspacePage.workItems.detail.singlePage.progressEmpty",
                  )}
                </p>
              )}
            </section>
          </aside>
        </div>
      </section>
    </>
  );
}

function FilesPanel({
  projectId,
  files,
  members,
  projectAgents,
  runAction,
  busyAction,
  canWrite,
}: {
  projectId: string;
  files: RecordValue[];
  members: RecordValue[];
  projectAgents: ProjectOwnedAgent[];
  runAction: (
    key: string,
    action: () => Promise<unknown>,
    success: string,
  ) => Promise<boolean>;
  busyAction: string;
  canWrite: boolean;
}) {
  const { get, update } = useContext(WorkspaceNavigationContext);
  const workspaceEmployees = new Map<string, string>();
  members.forEach((member) => {
    const id = text(member, "agent_id", "agentId", "id");
    const name = text(member, "name_snapshot", "nameSnapshot", "name");
    if (id && name) workspaceEmployees.set(id, name);
  });
  projectAgents.forEach((employee) =>
    workspaceEmployees.set(employee.id, employee.name),
  );
  return (
    <ProjectFileWorkspace
      projectId={projectId}
      files={files}
      projectAgents={Array.from(workspaceEmployees, ([id, name]) => ({
        id,
        name,
      }))}
      selectedPath={get("file")}
      onSelectedPathChange={(path) => update({ file: path || undefined })}
      selectedView={get("fileView") === "source" ? "source" : "preview"}
      onSelectedViewChange={(view) => update({ fileView: view })}
      runAction={runAction}
      busyAction={busyAction}
      canWrite={canWrite}
    />
  );
}

function MilestonesPanel({
  projectId,
  milestones,
  commits,
  events,
  workItems,
  runs,
  files,
  members,
  onOpenWorkItem,
  onOpenSession,
  runAction,
  busyAction,
  canManage,
}: {
  projectId: string;
  milestones: RecordValue[];
  commits: RecordValue[];
  events: RecordValue[];
  workItems: RecordValue[];
  runs: RecordValue[];
  files: RecordValue[];
  members: RecordValue[];
  onOpenWorkItem: (id: string) => void;
  onOpenSession: OpenSession;
  runAction: (
    key: string,
    action: () => Promise<unknown>,
    success: string,
  ) => Promise<boolean>;
  busyAction: string;
  canManage: boolean;
}) {
  const { t, i18n } = useTranslation();
  const { get, update } = useContext(WorkspaceNavigationContext);
  const [message, setMessage] = useState("");
  const milestoneRecords = useMemo(
    () =>
      milestones.length
        ? milestones
        : events.filter(
            (event) =>
              text(event, "event_type", "type") === "git.milestone.created",
          ),
    [events, milestones],
  );
  const { pageItems: visibleMilestones, pagination } = useWorkspacePagination(
    milestoneRecords,
    "milestones",
  );
  const commitByHash = useMemo(
    () =>
      new Map(
        commits.map((commit) => [
          text(commit, "commit", "hash", "commit_hash", "id"),
          commit,
        ]),
      ),
    [commits],
  );
  const milestoneView = (event: RecordValue) => {
    const records = traceRecords(event);
    const hash = closestTraceValue(records, "commit_hash", "commit", "hash");
    const commit = commitByHash.get(hash);
    const title =
      text(event, "message", "summary", "detail") ||
      text(commit || {}, "message", "subject", "title") ||
      t("projectWorkspaceNav.tabs.milestones");
    const commitEvents = events.filter((candidate) => {
      const candidateHash = closestTraceValue(
        traceRecords(candidate),
        "commit_hash",
        "commit",
        "hash",
      );
      return candidate !== event && sameGitCommit(hash, candidateHash);
    });
    const linkedEventRecords = [event, ...commitEvents];
    const milestoneRunId = closestTraceValue(
      records,
      "run_id",
      "project_run_id",
    );
    const milestoneRun = runs.find(
      (run) => text(run, "id", "run_id") === milestoneRunId,
    );
    const businessRunIds = new Set(
      linkedEventRecords
        .flatMap((entry) => traceStringValues(entry, "related_run_ids"))
        .filter((runId) => runId !== milestoneRunId),
    );
    const businessRuns = runs.filter((run) =>
      businessRunIds.has(text(run, "id", "run_id")),
    );
    const linkedRuns = [
      ...(milestoneRun ? [milestoneRun] : []),
      ...businessRuns,
    ];
    const linkedWorkItemIds = new Set([
      ...linkedEventRecords.flatMap((entry) =>
        traceStringValues(
          entry,
          "work_item_id",
          "project_work_item_id",
          "related_work_item_ids",
        ),
      ),
      ...businessRuns.flatMap((run) =>
        traceStringValues(run, "work_item_id", "project_work_item_id"),
      ),
    ]);
    const linkedWorkItems = workItems.filter((item) =>
      linkedWorkItemIds.has(text(item, "id", "work_item_id")),
    );
    const linkedPaths = new Set(
      linkedEventRecords.flatMap((entry) =>
        traceStringValues(
          entry,
          "path",
          "file_path",
          "paths",
          "files",
          "changed",
        ),
      ),
    );
    const linkedFiles = files.filter((file) =>
      linkedPaths.has(text(file, "path", "id", "name")),
    );
    const sessionSource = [...linkedRuns, ...linkedEventRecords].find((entry) =>
      sessionRouteOf(entry, inferredSessionIntent(entry)),
    );
    const linkedAgentNames = Array.from(
      new Set(
        [
          ...linkedRuns.map((run) =>
            runAgentName(
              run,
              members,
              t("projectWorkspacePage.members.employeeNotRecorded"),
            ),
          ),
          text(event, "agent_name", "agent_name_snapshot"),
        ].filter(Boolean),
      ),
    );

    return {
      id: text(event, "id", "event_id") || hash,
      event,
      hash,
      commit,
      title: title.replace(/^Created delivery milestone:\s*/i, ""),
      businessRuns,
      linkedWorkItems,
      linkedFileCount: Math.max(linkedFiles.length, linkedPaths.size),
      sessionSource,
      linkedAgentNames,
    };
  };
  const selectedMilestoneId = get("milestone");
  const selectedMilestoneEvent = milestoneRecords.find(
    (event) =>
      (text(event, "id", "event_id") ||
        closestTraceValue(
          traceRecords(event),
          "commit_hash",
          "commit",
          "hash",
        )) === selectedMilestoneId,
  );
  const selectedMilestone = selectedMilestoneEvent
    ? milestoneView(selectedMilestoneEvent)
    : null;
  const {
    pageItems: visibleMilestoneRuns,
    pagination: milestoneRunsPagination,
  } = useWorkspacePagination(
    selectedMilestone?.businessRuns || [],
    "milestoneRuns",
    8,
    [8, 16],
    false,
  );
  const createMilestone = (event: FormEvent) => {
    event.preventDefault();
    const trimmed = message.trim();
    if (!trimmed) return;
    void runAction(
      "create-milestone",
      () =>
        projectsApi.commitFiles(projectId, {
          message: trimmed,
          milestone: true,
        }),
      t("projectWorkspacePage.milestones.feedback.created"),
    ).then((ok) => {
      if (ok) setMessage("");
    });
  };

  return (
    <>
      <SectionHeading
        eyebrow="MILESTONES / DELIVERY"
        title={t("projectWorkspaceNav.tabs.milestones")}
        description=""
      />
      {canManage && (
        <form
          className="project-workspace__inline-create"
          onSubmit={createMilestone}
        >
          <ProjectField
            label={t("projectWorkspacePage.milestones.fields.description")}
            labelFor="project-milestone-message"
            required
          >
            <TextInput
              id="project-milestone-message"
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              placeholder={t(
                "projectWorkspacePage.milestones.fields.placeholder",
              )}
              required
            />
          </ProjectField>
          <Button
            variant="primary"
            type="submit"
            disabled={!message.trim() || busyAction === "create-milestone"}
          >
            {busyAction === "create-milestone" ? (
              <IconLoader2 className="project-workspace__spinner" size={16} />
            ) : (
              <IconFlag size={16} />
            )}
            {t("projectWorkspacePage.milestones.actions.create")}
          </Button>
        </form>
      )}
      {milestoneRecords.length ? (
        <div className="project-workspace__milestone-list">
          {visibleMilestones.map((event) => {
            const view = milestoneView(event);
            return (
              <article key={view.id}>
                <span className="project-workspace__milestone-icon">
                  <IconFlag size={18} />
                </span>
                <div className="project-workspace__milestone-copy">
                  <div>
                    <ProjectStatusBadge tone="success">
                      {t("projectGraphs.status.completed")}
                    </ProjectStatusBadge>
                    <time>
                      {dateLabel(event.created_at || view.commit?.created_at)}
                    </time>
                  </div>
                  <h3 title={view.title}>{view.title}</h3>
                  <p>
                    {t("projectWorkspacePage.milestones.version")}{" "}
                    <code>{view.hash ? compactId(view.hash) : "—"}</code>
                  </p>
                  <dl className="project-workspace__milestone-stats">
                    <div>
                      <dt>{t("projectWorkspaceNav.tabs.detail")}</dt>
                      <dd>{view.linkedWorkItems.length}</dd>
                    </div>
                    <div>
                      <dt>{t("projectWorkspacePage.milestones.execution")}</dt>
                      <dd>{view.businessRuns.length}</dd>
                    </div>
                    <div>
                      <dt>
                        {t("projectWorkspacePage.milestones.deliveryFiles")}
                      </dt>
                      <dd>{view.linkedFileCount}</dd>
                    </div>
                    <div>
                      <dt>
                        {t("projectWorkspacePage.milestones.participants")}
                      </dt>
                      <dd>{view.linkedAgentNames.length}</dd>
                    </div>
                  </dl>
                  <div className="project-workspace__milestone-links">
                    {view.linkedWorkItems.map((item) => {
                      const id = text(item, "id", "work_item_id");
                      const label =
                        text(item, "title", "name") || compactId(id);
                      return (
                        <Button
                          key={id}
                          variant="ghost"
                          title={label}
                          onClick={() => onOpenWorkItem(id)}
                        >
                          <IconChecklist size={13} />
                          {label}
                        </Button>
                      );
                    })}
                  </div>
                </div>
                <div className="project-workspace__milestone-actions">
                  <small
                    title={view.linkedAgentNames.join(
                      i18n.language.startsWith("zh") ? "、" : ", ",
                    )}
                  >
                    {view.linkedAgentNames.join(
                      i18n.language.startsWith("zh") ? "、" : ", ",
                    )}
                  </small>
                  <div>
                    {view.businessRuns.length > 0 && (
                      <Button
                        variant="secondary"
                        onClick={() =>
                          update({
                            milestone: view.id,
                            milestoneRunsPage: undefined,
                          })
                        }
                      >
                        {t("projectWorkspacePage.milestones.actions.viewRuns")}
                      </Button>
                    )}
                    {view.sessionSource && (
                      <SessionButton
                        source={view.sessionSource}
                        onOpen={onOpenSession}
                        intent={inferredSessionIntent(view.sessionSource)}
                      />
                    )}
                  </div>
                </div>
              </article>
            );
          })}
          {pagination}
        </div>
      ) : (
        <EmptyState
          icon={<IconFlag size={22} />}
          title={t("projectWorkspacePage.milestones.empty.title")}
          description={t("projectWorkspacePage.milestones.empty.description")}
        />
      )}
      <Drawer
        open={Boolean(selectedMilestone)}
        onClose={() =>
          update({ milestone: undefined, milestoneRunsPage: undefined })
        }
        ariaLabel={t("projectWorkspacePage.milestones.drawerAria")}
        className="project-workspace__milestone-drawer"
      >
        {selectedMilestone && (
          <div className="project-workspace__milestone-drawer-content">
            <header>
              <div>
                <span>{t("projectWorkspacePage.milestones.execution")}</span>
                <h2>{selectedMilestone.title}</h2>
              </div>
              <ProjectIconButton
                aria-label={t("common.close")}
                onClick={() =>
                  update({ milestone: undefined, milestoneRunsPage: undefined })
                }
              >
                <IconX size={18} />
              </ProjectIconButton>
            </header>
            <div className="project-workspace__milestone-run-list">
              {visibleMilestoneRuns.map((run) => {
                const runId = text(run, "id", "run_id");
                return (
                  <article key={runId}>
                    <span className="project-workspace__run-icon">
                      <IconBolt size={16} />
                    </span>
                    <div>
                      <strong>
                        {text(obj(run.input), "objective", "task") ||
                          t("projectWorkspacePage.milestones.execution")}
                      </strong>
                      <small>
                        {runAgentName(
                          run,
                          members,
                          t("projectWorkspacePage.members.employeeNotRecorded"),
                        )}{" "}
                        · {dateLabel(run.started_at || run.created_at)}
                      </small>
                    </div>
                    <StatusPill status={text(run, "status")} />
                    <SessionButton
                      source={run}
                      onOpen={onOpenSession}
                      intent={inferredSessionIntent(run)}
                    />
                  </article>
                );
              })}
            </div>
            {milestoneRunsPagination}
          </div>
        )}
      </Drawer>
    </>
  );
}

function RunsPanel({
  projectId,
  runs,
  members,
  workItems,
  onOpenSession,
  runAction,
  busyAction,
  canManage,
}: {
  projectId: string;
  runs: RecordValue[];
  members: RecordValue[];
  workItems: RecordValue[];
  onOpenSession: OpenSession;
  runAction: (
    key: string,
    action: () => Promise<unknown>,
    success: string,
  ) => Promise<boolean>;
  busyAction: string;
  canManage: boolean;
}) {
  const { t } = useTranslation();
  const { get, update } = useContext(WorkspaceNavigationContext);
  const runMember = get("runMember");
  const filteredRuns = runMember
    ? runs.filter((run) => runAgentId(run) === runMember)
    : runs;
  const filteredMember = members.find(
    (member) => text(member, "agent_id") === runMember,
  );
  const { pageItems: visibleRuns, pagination } = useWorkspacePagination(
    filteredRuns,
    "runs",
  );
  return (
    <>
      <SectionHeading
        eyebrow="RUN CONTROL"
        title={t("projectWorkspaceNav.tabs.runs")}
        description=""
        actions={
          runMember ? (
            <Button
              variant="secondary"
              onClick={() =>
                update({ runMember: undefined, runsPage: undefined })
              }
            >
              {t("projectRuns.clearMemberFilter", {
                name:
                  text(
                    filteredMember || {},
                    "name_snapshot",
                    "agent_name",
                    "name",
                  ) || t("projectAgents.unnamed"),
              })}
            </Button>
          ) : null
        }
      />
      {filteredRuns.length ? (
        <div className="project-workspace__run-list">
          {visibleRuns.map((run) => {
            const runId = text(run, "id", "run_id");
            const runStatus = text(run, "status");
            const input = obj(run.input);
            const workItemId =
              traceValue(
                traceRecords(run),
                "work_item_id",
                "project_work_item_id",
                "task_id",
              ) || text(run, "work_item_id");
            const workItem = workItems.find(
              (entry) => text(entry, "id", "work_item_id") === workItemId,
            );
            const triggerTitle: Record<string, string> = {
              manual: t("projectWorkspacePage.runs.triggers.manual"),
              group_leader_message: t(
                "projectWorkspacePage.runs.triggers.groupMessage",
              ),
              leader_reply_batch: t(
                "projectWorkspacePage.runs.triggers.collaborationBatch",
              ),
              a2a: t("projectWorkspacePage.runs.triggers.a2a"),
              schedule: t("projectWorkspacePage.runs.triggers.schedule"),
              system: t("projectWorkspacePage.runs.triggers.system"),
            };
            const originalObjective =
              text(input, "objective", "title", "task") ||
              text(run, "title", "name", "objective") ||
              text(workItem || {}, "title", "name") ||
              text(obj(input.dispatch), "task")
                .split(/\r?\n/, 1)[0]
                .slice(0, 120) ||
              triggerTitle[text(run, "trigger_type")] ||
              t("projectWorkspacePage.runs.defaultObjective");
            const nextStatus =
              runStatus === "running"
                ? "waiting"
                : runStatus === "waiting" || runStatus === "queued"
                  ? "running"
                  : "";
            const retryKey = `retry-${runId}`;
            const sessionIntent = inferredSessionIntent(run);
            const agentId = runAgentId(run);
            const agentMember = members.find(
              (member) => text(member, "agent_id") === agentId,
            );
            const agentName = runAgentName(
              run,
              members,
              t("projectWorkspacePage.members.employeeNotRecorded"),
            );
            const sessionSource =
              agentMember?.is_enabled === false
                ? { ...run, member_enabled: false }
                : run;
            return (
              <article key={runId}>
                <div className="project-workspace__run-icon">
                  <IconBolt size={18} />
                </div>
                <div className="project-workspace__run-copy">
                  <div>
                    <StatusPill status={runStatus} />
                    <code>{runId}</code>
                  </div>
                  <h3>{originalObjective}</h3>
                  <p>{dateLabel(run.started_at || run.created_at)}</p>
                  <span className="project-workspace__run-agent">
                    <b>{agentName.slice(0, 1)}</b>
                    <span>
                      <strong>{agentName}</strong>
                      <small>
                        {agentMember?.is_enabled === false
                          ? t("projectWorkspacePage.runs.historicalMember")
                          : t(
                              "projectTerminology.workspace.currentExecutionOwner",
                            )}
                      </small>
                    </span>
                  </span>
                </div>
                <div className="project-workspace__run-controls">
                  <SessionButton
                    source={sessionSource}
                    onOpen={onOpenSession}
                    intent={sessionIntent}
                  />
                  {canManage && ["failed", "cancelled"].includes(runStatus) && (
                    <Button
                      variant="secondary"
                      disabled={busyAction === retryKey}
                      onClick={() =>
                        void runAction(
                          retryKey,
                          () =>
                            projectsApi.createRun(projectId, {
                              work_item_id:
                                text(run, "work_item_id") || undefined,
                              agent_id: agentId || undefined,
                              input: {
                                ...input,
                                objective: originalObjective,
                                retry_of_run_id: runId,
                              },
                            }),
                          t("projectWorkspacePage.runs.feedback.retried"),
                        )
                      }
                    >
                      {busyAction === retryKey ? (
                        <IconLoader2
                          className="project-workspace__spinner"
                          size={15}
                        />
                      ) : (
                        <IconRestore size={15} />
                      )}
                      {t("projectWorkspacePage.actions.retry")}
                    </Button>
                  )}
                  {canManage && nextStatus && (
                    <Button
                      variant="secondary"
                      disabled={busyAction === `run-${runId}`}
                      onClick={() =>
                        void runAction(
                          `run-${runId}`,
                          () =>
                            projectsApi.patchRun(projectId, runId, {
                              status: nextStatus,
                            }),
                          nextStatus === "waiting"
                            ? t("projectWorkspacePage.runs.feedback.waiting")
                            : t("projectWorkspacePage.runs.feedback.resumed"),
                        )
                      }
                    >
                      {busyAction === `run-${runId}` ? (
                        <IconLoader2
                          className="project-workspace__spinner"
                          size={15}
                        />
                      ) : nextStatus === "waiting" ? (
                        <IconPlayerPause size={15} />
                      ) : (
                        <IconPlayerPlay size={15} />
                      )}
                      {t(
                        nextStatus === "waiting"
                          ? "projectWorkspacePage.runs.actions.pause"
                          : "projectWorkspacePage.runs.actions.continue",
                      )}
                    </Button>
                  )}
                  {canManage &&
                    !["succeeded", "failed", "cancelled"].includes(
                      runStatus,
                    ) && (
                      <Button
                        variant="ghost"
                        disabled={busyAction === `finish-${runId}`}
                        onClick={() =>
                          void runAction(
                            `finish-${runId}`,
                            () =>
                              projectsApi.patchRun(projectId, runId, {
                                status: "succeeded",
                              }),
                            t("projectWorkspacePage.runs.feedback.completed"),
                          )
                        }
                      >
                        <IconCircleCheck size={15} />
                        {t("projectWorkspacePage.runs.actions.complete")}
                      </Button>
                    )}
                </div>
              </article>
            );
          })}
          {pagination}
        </div>
      ) : (
        <EmptyState
          title={t("projectWorkspacePage.runs.emptyTitle")}
          description={t("projectTerminology.workspace.runsAfterKickoff")}
        />
      )}
    </>
  );
}

function MembersPanel({
  projectId,
  projectAgents,
  members,
  capabilities,
  policies,
  events,
  canManage,
  selectedId,
  onSelect,
  onOpenWorkspace,
  onNavigate,
  runAction,
  busyAction,
}: {
  projectId: string;
  projectAgents: ProjectOwnedAgent[];
  members: RecordValue[];
  capabilities: RecordValue[];
  policies: RecordValue | null;
  events: RecordValue[];
  canManage: boolean;
  selectedId: string;
  onSelect: (id: string) => void;
  onOpenWorkspace: (path: string) => void;
  onNavigate: (tab: WorkspaceTab, patch?: WorkspaceUrlPatch) => void;
  runAction: (
    key: string,
    action: () => Promise<unknown>,
    success: string,
  ) => Promise<boolean>;
  busyAction: string;
}) {
  const { t } = useTranslation();
  const activeMembers = useMemo(
    () => members.filter((entry) => entry.is_enabled !== false),
    [members],
  );
  const collaborationEvents = useMemo(
    () => events.filter(isProjectA2ARecord),
    [events],
  );
  const member = selectedId
    ? members.find((entry) =>
        [
          text(entry, "id"),
          text(entry, "member_id"),
          text(entry, "agent_id"),
        ]
          .filter(Boolean)
          .includes(selectedId),
      )
    : undefined;
  const [configDraft, setConfigDraft] = useState<RecordValue>({});
  const [memberModels, setMemberModels] = useState<
    Array<{
      id: string;
      provider: string;
      model: string;
      label?: string;
      enabled?: boolean;
    }>
  >([]);
  const [availableAgents, setAvailableAgents] = useState<RecordValue[]>([]);
  const [availableCapabilities, setAvailableCapabilities] = useState<
    ProjectCapabilityOption[]
  >([]);
  const [agentsLoading, setAgentsLoading] = useState(true);
  const [agentsError, setAgentsError] = useState("");
  const [agentDrawerMode, setAgentDrawerMode] = useState<
    "create" | "edit" | null
  >(null);
  const [settingsOpen, setSettingsOpen] = useState(
    Boolean(selectedId && member),
  );
  const [capabilitySection, setCapabilitySection] = useState<
    "config" | "tools" | "mcp" | "skill"
  >("config");
  const [addingCapabilityKind, setAddingCapabilityKind] = useState<
    "mcp" | "skill" | null
  >(null);
  const [capabilityToAddId, setCapabilityToAddId] = useState("");
  const [createKind, setCreateKind] = useState<"copy" | "blank">("copy");
  const [removeDialogOpen, setRemoveDialogOpen] = useState(false);
  const [promoteDialogOpen, setPromoteDialogOpen] = useState(false);
  const [candidateAgentId, setCandidateAgentId] = useState("");
  const [agentDraft, setAgentDraft] = useState({
    name: "",
    roleDescription: "",
    soul: "",
    coreMemory: "",
  });
  const [promotedAgent, setPromotedAgent] =
    useState<ProjectOwnedAgentPromotion | null>(null);

  useEffect(() => {
    setConfigDraft(obj(member?.config_snapshot));
  }, [member]);

  useEffect(() => {
    if (!settingsOpen || !canManage) return;
    let active = true;
    void enterpriseApi
      .llmModels()
      .then((models) => {
        if (active)
          setMemberModels(
            (Array.isArray(models) ? models : []).filter(
              (model) => model?.enabled !== false,
            ),
          );
      })
      .catch(() => {
        if (active) setMemberModels([]);
      });
    return () => {
      active = false;
    };
  }, [canManage, settingsOpen]);

  useEffect(() => {
    if ((!settingsOpen && agentDrawerMode !== "create") || !canManage) return;
    let mounted = true;
    setAgentsLoading(true);
    setAgentsError("");
    void projectsApi
      .bootstrapOptions()
      .then((options) => {
        if (mounted) {
          setAvailableAgents(options.agents.map((agent) => ({ ...agent })));
          setAvailableCapabilities(options.capabilities);
        }
      })
      .catch((error) => {
        if (mounted)
          setAgentsError(
            errorMessage(error, t("projectWorkspacePage.errors.requestFailed")),
          );
      })
      .finally(() => {
        if (mounted) setAgentsLoading(false);
      });
    return () => {
      mounted = false;
    };
  }, [agentDrawerMode, canManage, settingsOpen, t]);

  const memberId = text(member || {}, "id", "member_id");
  const agentId = text(member || {}, "agent_id");
  const projectAgent = useMemo(
    () =>
      projectAgents.find(
        (entry) => entry.id === agentId || entry.member_id === memberId,
      ) || null,
    [agentId, memberId, projectAgents],
  );
  const memberName =
    text(member || {}, "name_snapshot", "agent_name", "name") ||
    t("projectAgents.defaultRole");
  const departed = member?.is_enabled === false;
  const candidates = availableAgents;
  const candidateOptions = candidates.map((agent) => ({
    value: text(agent, "id", "agent_id"),
    label: `${text(agent, "name", "agent_name") || t("projectAgents.unnamed")} · ${projectUserFacingCopy(
      text(agent, "role_description", "role") || t("projectAgents.defaultRole"),
      t,
    )}`,
  }));
  const selectedCandidate =
    candidates.find(
      (agent) => text(agent, "id", "agent_id") === candidateAgentId,
    ) || candidates[0];
  const selectedCandidateId = text(selectedCandidate || {}, "id", "agent_id");
  const candidateCapabilities = useMemo(
    () =>
      availableCapabilities.filter(
        (capability) =>
          capability.source === "agent" &&
          capability.owner_agent_id === selectedCandidateId,
      ),
    [availableCapabilities, selectedCandidateId],
  );

  useEffect(() => {
    setCapabilitySection("config");
    setAddingCapabilityKind(null);
    setCapabilityToAddId("");
  }, [memberId]);

  useEffect(() => {
    setSettingsOpen(Boolean(selectedId && memberId));
  }, [memberId, selectedId]);

  useEffect(() => {
    if (agentDrawerMode !== "edit" || !projectAgent) return;
    setAgentDraft({
      name: projectAgent.name,
      roleDescription: projectAgent.role_description,
      soul: projectAgent.soul,
      coreMemory: projectAgent.core_memory,
    });
  }, [agentDrawerMode, projectAgent]);
  const modelOptions = [
    { value: "", label: t("projectSnapshot.followSourceAgent") },
    ...memberModels.map((model) => ({
      value: model.id,
      label: model.label || `${model.provider} · ${model.model}`,
    })),
  ];
  const updateConfigField = (key: string, value: unknown) =>
    setConfigDraft((current) => ({ ...current, [key]: value }));

  const saveSnapshot = () => {
    if (!canManage || departed) return;
    const maxToolRoundsRaw = text(configDraft, "max_tool_rounds").trim();
    const maxToolRounds = maxToolRoundsRaw ? Number(maxToolRoundsRaw) : null;
    const payload = {
      ...configDraft,
      max_tool_rounds:
        maxToolRounds !== null && Number.isFinite(maxToolRounds)
          ? maxToolRounds
          : null,
    };
    void runAction(
      "save-member",
      () =>
        projectsApi.patchMember(projectId, memberId, {
          config_snapshot: payload,
        }),
      t("projectWorkspacePage.members.feedback.snapshotSaved"),
    );
  };
  const resetAgentDraft = () => {
    setAgentDraft({
      name: "",
      roleDescription: "",
      soul: "",
      coreMemory: "",
    });
    setCandidateAgentId("");
    setCreateKind("copy");
    setPromotedAgent(null);
  };
  const createOwnedAgent = () => {
    if (createKind === "copy" && !selectedCandidateId) return;
    if (createKind === "blank" && agentDraft.name.trim().length < 2) return;
    let created: ProjectOwnedAgent | null = null;
    void runAction(
      "create-project-agent",
      async () => {
        created = await projectsApi.createProjectAgent(projectId, {
          source_agent_id: createKind === "copy" ? selectedCandidateId : null,
          name: agentDraft.name.trim() || null,
          role_description: agentDraft.roleDescription.trim() || null,
          soul: agentDraft.soul || null,
          core_memory: agentDraft.coreMemory || null,
        });
        return created;
      },
      t("projectAgents.feedback.created"),
    ).then((succeeded) => {
      if (succeeded) {
        setAgentDrawerMode(null);
        resetAgentDraft();
        if (created) onSelect(created.member_id);
      }
    });
  };
  const saveProjectAgent = () => {
    if (!projectAgent || agentDraft.name.trim().length < 2) return;
    void runAction(
      "save-project-agent",
      () =>
        projectsApi.updateProjectAgent(projectId, projectAgent.id, {
          name: agentDraft.name.trim(),
          role_description: agentDraft.roleDescription,
          soul: agentDraft.soul,
          core_memory: agentDraft.coreMemory,
        }),
      t("projectAgents.feedback.saved"),
    ).then((succeeded) => {
      if (succeeded) setAgentDrawerMode(null);
    });
  };
  const promoteProjectAgent = () => {
    if (!projectAgent) return;
    let promoted: ProjectOwnedAgentPromotion | null = null;
    void runAction(
      "promote-project-agent",
      async () => {
        promoted = await projectsApi.promoteProjectAgent(
          projectId,
          projectAgent.id,
          agentDraft.name.trim(),
        );
        return promoted;
      },
      t("projectAgents.feedback.promoted"),
    ).then((succeeded) => {
      if (succeeded && promoted) {
        setPromoteDialogOpen(false);
        setPromotedAgent(promoted);
        setAgentDrawerMode("edit");
      }
    });
  };
  const openPromoteDialog = () => {
    if (!projectAgent) return;
    setPromotedAgent(null);
    if (agentDrawerMode !== "edit") {
      setAgentDraft({
        name: projectAgent.name,
        roleDescription: projectAgent.role_description,
        soul: projectAgent.soul,
        coreMemory: projectAgent.core_memory,
      });
    }
    setPromoteDialogOpen(true);
  };
  const removeMember = () => {
    if (!memberId || bool(member || {}, "is_leader")) return;
    void runAction(
      "remove-member",
      () =>
        projectAgent
          ? projectsApi.deactivateProjectAgent(projectId, projectAgent.id)
          : projectsApi.removeMember(
              projectId,
              memberId,
              "human_removed_from_project",
            ),
      projectAgent
        ? t("projectAgents.feedback.deactivated")
        : t("projectWorkspacePage.members.feedback.removed"),
    ).then((succeeded) => {
      if (succeeded) setRemoveDialogOpen(false);
    });
  };
  const restoreMember = () => {
    if (!memberId || !departed) return;
    void runAction(
      "restore-member",
      () =>
        projectAgent
          ? projectsApi.restoreProjectAgent(projectId, projectAgent.id)
          : projectsApi.restoreMember(
              projectId,
              memberId,
              "human_restored_to_project",
            ),
      projectAgent
        ? t("projectAgents.feedback.restored")
        : t("projectWorkspacePage.members.feedback.restored"),
    );
  };
  const memberCapabilities = capabilities.filter((capability) => {
    const inheritedAgentId = text(
      capability,
      "inherited_from_agent_id",
      "owner_agent_id",
    );
    if (inheritedAgentId) return inheritedAgentId === agentId;
    return text(capability, "source") === "shared";
  });
  const capabilitiesByKind = (kind: "tool" | "mcp" | "skill") =>
    memberCapabilities.filter(
      (capability) =>
        text(capability, "capability_type", "kind", "type") === kind,
    );
  const platformTools = capabilitiesByKind("tool");
  const memberMcps = capabilitiesByKind("mcp");
  const memberSkills = capabilitiesByKind("skill");
  const effectiveProjectToolCount = member
    ? PROJECT_TOOL_REGISTRY.filter(
        (tool) => projectToolResolution(tool, member, policies).effective,
      ).length
    : 0;
  const configCount = [
    text(configDraft, "primary_model_id"),
    text(configDraft, "fallback_model_id"),
    text(configDraft, "project_instruction"),
    text(configDraft, "max_tool_rounds"),
  ].filter(Boolean).length;
  const renderCapabilityRows = (
    items: RecordValue[],
    kind: "mcp" | "skill",
  ) => {
    if (!items.length) {
      return (
        <div className="project-workspace__member-capability-empty">
          <span>{t(`projectAgents.capabilityPackage.empty.${kind}`)}</span>
        </div>
      );
    }
    return (
      <div className="project-workspace__member-capability-list">
        {items.map((capability, index) => {
          const bindingEnabled = capability.is_enabled !== false;
          const availability = ["available", "missing", "restricted"].includes(
            text(capability, "availability"),
          )
            ? text(capability, "availability")
            : "available";
          const status =
            availability === "missing"
              ? "missing"
              : availability === "restricted" || !bindingEnabled || departed
                ? "restricted"
                : "available";
          const bindingId = text(
            capability,
            "id",
            "binding_id",
            "capability_id",
          );
          const actionKey = `member-capability-${memberId}-${bindingId}`;
          let capabilityName =
            text(capability, "name", "capability_name") ||
            t("projectWorkspacePage.capabilities.capability");
          let description = text(
            capability,
            "description",
            "purpose",
            "summary",
          );
          return (
            <div
              className="project-workspace__member-capability-row"
              key={
                bindingId || `${kind}-${capabilityName}-${index}`
              }
            >
              <span className={`is-${kind}`}>
                {kind === "mcp" ? (
                  <IconCodeDots size={16} />
                ) : kind === "skill" ? (
                  <IconBolt size={16} />
                ) : (
                  <IconTool size={16} />
                )}
              </span>
              <div>
                <strong>{capabilityName}</strong>
                {description ? <p>{description}</p> : null}
                {kind === "skill" ? (
                  <>
                    <small>
                      {t("projectAgents.capabilityPackage.projectAsset", {
                        name: memberName,
                      })}
                    </small>
                    <small>
                      {t("projectAgents.capabilityPackage.skillFiles", {
                        version:
                          text(capability, "version") ||
                          text(
                            obj(obj(capability.config).skill_asset),
                            "version",
                          ) ||
                          "—",
                        count:
                          num(capability, "file_count") ||
                          num(
                            obj(obj(capability.config).skill_asset),
                            "file_count",
                          ),
                        size: fileSizeLabel(
                          num(capability, "size_bytes") ||
                            num(
                              obj(obj(capability.config).skill_asset),
                              "size_bytes",
                            ),
                        ),
                      })}
                    </small>
                  </>
                ) : null}
              </div>
              <div className="project-workspace__member-capability-controls">
                <ProjectStatusBadge
                  tone={status === "available" ? "success" : "warning"}
                >
                  {t(`projectAgents.capabilityPackage.status.${status}`)}
                </ProjectStatusBadge>
                {canManage && !departed && bindingId ? (
                  <ToggleSwitch
                    checked={bindingEnabled}
                    disabled={busyAction === actionKey}
                    ariaLabel={t(
                      bindingEnabled
                        ? "projectWorkspacePage.capabilities.actions.disable"
                        : "projectWorkspacePage.capabilities.actions.enable",
                      { name: capabilityName },
                    )}
                    onChange={(checked) =>
                      void runAction(
                        actionKey,
                        () =>
                          projectsApi.patchCapability(projectId, bindingId, {
                            is_enabled: checked,
                          }),
                        t(
                          checked
                            ? "projectWorkspacePage.capabilities.feedback.enabled"
                            : "projectWorkspacePage.capabilities.feedback.disabled",
                        ),
                      )
                    }
                  />
                ) : null}
              </div>
            </div>
          );
        })}
      </div>
    );
  };
  const addableCapabilities = (kind: "mcp" | "skill") => {
    const boundIds = new Set(
      memberCapabilities
        .filter(
          (capability) =>
            text(capability, "capability_type", "kind", "type") === kind,
        )
        .map((capability) => text(capability, "capability_id"))
        .filter(Boolean),
    );
    const seen = new Set<string>();
    return availableCapabilities.filter((capability) => {
      const id = capability.capability_id || capability.id;
      if (
        capability.kind !== kind ||
        (kind === "mcp" && capability.source === "agent") ||
        !id ||
        boundIds.has(id) ||
        seen.has(id)
      )
        return false;
      seen.add(id);
      return true;
    });
  };
  const addMemberCapability = (kind: "mcp" | "skill") => {
    const candidates = addableCapabilities(kind);
    const candidate =
      candidates.find(
        (entry) =>
          (entry.capability_id || entry.id) === capabilityToAddId,
      ) || candidates[0];
    if (!candidate || !agentId) return;
    const capabilityId = candidate.capability_id || candidate.id;
    const actionKey = `add-member-capability-${memberId}-${kind}`;
    void runAction(
      actionKey,
      () =>
        projectsApi.createCapability(projectId, {
          capability_type: kind,
          capability_id: capabilityId,
          capability_name: candidate.name,
          source: "inherited",
          inherited_from_agent_id: agentId,
          is_enabled: true,
        }),
      t("projectAgents.capabilityPackage.added", { name: candidate.name }),
    ).then((succeeded) => {
      if (succeeded) {
        setAddingCapabilityKind(null);
        setCapabilityToAddId("");
      }
    });
  };
  const renderCapabilityGroup = (
    kind: "mcp" | "skill",
    items: RecordValue[],
    title: string,
  ) => {
    const candidates = addableCapabilities(kind);
    const actionKey = `add-member-capability-${memberId}-${kind}`;
    const selectedCapabilityId =
      capabilityToAddId ||
      (candidates[0]?.capability_id || candidates[0]?.id || "");
    const loadingCandidates = agentsLoading;
    return (
      <section className="project-workspace__member-capability-group">
        <header>
          <h4>{title}</h4>
          {canManage && !departed ? (
            <Button
              variant="ghost"
              onClick={() => {
                setAddingCapabilityKind((current) =>
                  current === kind ? null : kind,
                );
                setCapabilityToAddId("");
              }}
            >
              <IconPlus size={15} />
              {t("projectAgents.capabilityPackage.add")}
            </Button>
          ) : null}
        </header>
        {addingCapabilityKind === kind ? (
          <div className="project-workspace__member-capability-add">
            {loadingCandidates ? (
              <span>
                <IconLoader2
                  className="project-workspace__spinner"
                  size={15}
                />
                {t("projectAgents.capabilityPackage.loading")}
              </span>
            ) : candidates.length ? (
              <>
                <ProjectSelect
                  value={selectedCapabilityId}
                  options={candidates.map((capability) => ({
                    value: capability.capability_id || capability.id,
                    label: capability.name,
                  }))}
                  onChange={setCapabilityToAddId}
                  ariaLabel={t("projectAgents.capabilityPackage.select", {
                    kind: title,
                  })}
                />
                <Button
                  variant="primary"
                  disabled={busyAction === actionKey}
                  onClick={() => addMemberCapability(kind)}
                >
                  {busyAction === actionKey ? (
                    <IconLoader2
                      className="project-workspace__spinner"
                      size={15}
                    />
                  ) : (
                    <IconPlus size={15} />
                  )}
                  {t("projectAgents.capabilityPackage.confirmAdd")}
                </Button>
              </>
            ) : (
              <span>
                {agentsError ||
                  t("projectAgents.capabilityPackage.noCandidates")}
              </span>
            )}
          </div>
        ) : null}
        {renderCapabilityRows(items, kind)}
      </section>
    );
  };

  return (
    <>
      <SectionHeading
        eyebrow={t("projectAgents.eyebrow")}
        title={t("projectAgents.teamPage.title")}
        description={t("projectAgents.teamPage.description")}
        className="project-workspace__member-page-heading"
        actions={
          member || canManage ? (
            <>
              {member ? (
                <Button
                  variant="ghost"
                  onClick={() =>
                    onNavigate("runs", {
                      runMember: agentId,
                      runsPage: undefined,
                    })
                  }
                >
                  {t("projectAgents.viewActivity")}
                  <IconArrowRight size={14} />
                </Button>
              ) : null}
              {projectAgent && canManage && !departed ? (
                <Button
                  variant="secondary"
                  onClick={openPromoteDialog}
                  disabled={busyAction === "promote-project-agent"}
                >
                  <IconSparkles size={16} />
                  {t("projectAgents.actions.promote")}
                </Button>
              ) : null}
              {canManage ? (
                <Button
                  variant="primary"
                  onClick={() => {
                    resetAgentDraft();
                    setAgentDrawerMode("create");
                  }}
                >
                  <IconPlus size={16} />
                  {t("projectAgents.actions.create")}
                </Button>
              ) : null}
            </>
          ) : null
        }
      />
      {members.length ? (
        <>
          <section className="project-workspace__team-relationships">
            <header>
              <div>
                <h3>{t("projectWorkspacePage.mesh.title")}</h3>
                <p>{t("projectTerminology.workspace.meshDescription")}</p>
              </div>
              <Button
                variant="ghost"
                onClick={() => onNavigate("audit", { auditScope: "a2a" })}
              >
                {t("projectMesh.viewEvents")}
                <IconArrowRight size={14} />
              </Button>
            </header>
            <div className="project-workspace__team-relationships-graph">
              <A2AMeshGraph
                members={members}
                events={collaborationEvents}
                selectedAgentId={agentId}
                onAgentSelect={(_, selectedMember) => {
                  onSelect(
                    text(selectedMember, "id", "member_id", "agent_id"),
                  );
                  setSettingsOpen(true);
                }}
              />
            </div>
          </section>
          {settingsOpen && (
            <section
              id="project-member-work-settings"
              className={`project-workspace__action-panel project-workspace__member-editor${departed ? " is-readonly" : ""}`}
            >
              <header>
                <div>
                  <span>{t("projectAgents.teamPage.workSettings")}</span>
                  <h3>{memberName}</h3>
                </div>
                <div className="project-workspace__member-actions">
                  {projectAgent && (
                    <Button
                      variant="secondary"
                      onClick={() => {
                        setPromotedAgent(null);
                        setAgentDrawerMode("edit");
                      }}
                    >
                      <IconSettings size={15} />
                      {canManage && !departed
                        ? t("projectAgents.actions.edit")
                        : t("projectAgents.actions.view")}
                    </Button>
                  )}
                  {departed ? (
                    canManage ? (
                      <Button
                        variant="secondary"
                        disabled={busyAction === "restore-member"}
                        onClick={restoreMember}
                      >
                        {busyAction === "restore-member" ? (
                          <IconLoader2
                            className="project-workspace__spinner"
                            size={16}
                          />
                        ) : (
                          <IconRestore size={15} />
                        )}
                        {projectAgent
                          ? t("projectAgents.actions.restore")
                          : t("projectWorkspacePage.members.actions.restore")}
                      </Button>
                    ) : null
                  ) : !bool(member || {}, "is_leader") ? (
                    canManage ? (
                      <>
                        <Button
                          variant="secondary"
                          disabled={busyAction === "leader"}
                          onClick={() =>
                            void runAction(
                              "leader",
                              () => projectsApi.setLeader(projectId, agentId),
                              t("projectTerminology.workspace.ownerChanged"),
                            )
                          }
                        >
                          <IconFlag size={15} />
                          {t("projectTerminology.setOwner")}
                        </Button>
                        <Button
                          variant="danger"
                          onClick={() => setRemoveDialogOpen(true)}
                        >
                          <IconTrash size={15} />
                          {projectAgent
                            ? t("projectAgents.actions.deactivate")
                            : t("projectWorkspacePage.members.actions.remove")}
                        </Button>
                      </>
                    ) : null
                  ) : (
                    <ProjectStatusBadge tone="info">
                      {t("projectTerminology.currentOwner")}
                    </ProjectStatusBadge>
                  )}
                </div>
              </header>
              {departed && (
                <div className="project-workspace__member-history-note">
                  <IconArchive size={17} />
                  <div>
                    <strong>
                      {t("projectWorkspacePage.members.historical.title")}
                    </strong>
                    <p>
                      {t("projectWorkspacePage.members.historical.description")}
                    </p>
                  </div>
                </div>
              )}
              <ProjectAgentCapabilityPanel
                value={capabilitySection}
                onChange={setCapabilitySection}
                ariaLabel={t("projectAgents.capabilityPackage.title")}
                tabs={[
                  {
                    value: "config",
                    icon: <IconSettings size={16} />,
                    label: t(
                      "projectAgents.capabilityPackage.sections.config",
                    ),
                    count: t("projectAgents.capabilityPackage.count", {
                      count: configCount,
                    }),
                  },
                  {
                    value: "tools",
                    icon: <IconTool size={16} />,
                    label: t(
                      "projectAgents.capabilityPackage.sections.tools",
                    ),
                    count: t("projectAgents.capabilityPackage.count", {
                      count: effectiveProjectToolCount + platformTools.length,
                    }),
                  },
                  {
                    value: "mcp",
                    icon: <IconCodeDots size={16} />,
                    label: t(
                      "projectAgents.capabilityPackage.sections.mcp",
                    ),
                    count: t("projectAgents.capabilityPackage.count", {
                      count: memberMcps.length,
                    }),
                  },
                  {
                    value: "skill",
                    icon: <IconBolt size={16} />,
                    label: t(
                      "projectAgents.capabilityPackage.sections.skill",
                    ),
                    count: t("projectAgents.capabilityPackage.count", {
                      count: memberSkills.length,
                    }),
                  },
                ] as const}
              >
                {capabilitySection === "config" ? (
                  <>
                    <div className="project-workspace__snapshot-form">
                <ProjectField
                  label={t("projectWorkspacePage.members.fields.primaryModel")}
                  hint={t(
                    "projectWorkspacePage.members.fields.primaryModelHint",
                  )}
                >
                  <ProjectSelect
                    value={text(configDraft, "primary_model_id")}
                    options={modelOptions}
                    onChange={(value) =>
                      updateConfigField("primary_model_id", value || null)
                    }
                    ariaLabel={t(
                      "projectWorkspacePage.members.fields.primaryModelAria",
                    )}
                    disabled={departed || !canManage}
                  />
                </ProjectField>
                <ProjectField
                  label={t("projectWorkspacePage.members.fields.fallbackModel")}
                >
                  <ProjectSelect
                    value={text(configDraft, "fallback_model_id")}
                    options={modelOptions}
                    onChange={(value) =>
                      updateConfigField("fallback_model_id", value || null)
                    }
                    ariaLabel={t(
                      "projectWorkspacePage.members.fields.fallbackModelAria",
                    )}
                    disabled={departed || !canManage}
                  />
                </ProjectField>
                <ProjectField
                  label={t("projectWorkspacePage.members.fields.maxToolRounds")}
                  labelFor="project-member-max-tool-rounds"
                >
                  <TextInput
                    id="project-member-max-tool-rounds"
                    type="number"
                    min="1"
                    max="200"
                    value={text(configDraft, "max_tool_rounds")}
                    onChange={(event) =>
                      updateConfigField("max_tool_rounds", event.target.value)
                    }
                    disabled={departed || !canManage}
                  />
                </ProjectField>
                <ProjectField
                  className="is-wide"
                  label={t("projectWorkspacePage.members.fields.instructions")}
                  labelFor="project-member-instruction"
                  hint={
                    departed
                      ? t("projectWorkspacePage.members.fields.departedHint")
                      : t(
                          "projectWorkspacePage.members.fields.instructionsHint",
                        )
                  }
                >
                  <ProjectTextarea
                    id="project-member-instruction"
                    value={text(configDraft, "project_instruction")}
                    onChange={(event) =>
                      updateConfigField(
                        "project_instruction",
                        event.target.value,
                      )
                    }
                    rows={3}
                    disabled={departed || !canManage}
                  />
                </ProjectField>
                    </div>
                    {canManage && !departed && (
                      <footer>
                        <Button
                          variant="primary"
                          onClick={saveSnapshot}
                          disabled={busyAction === "save-member"}
                        >
                          {busyAction === "save-member" ? (
                            <IconLoader2
                              className="project-workspace__spinner"
                              size={16}
                            />
                          ) : (
                            <IconDeviceFloppy size={16} />
                          )}
                          {t(
                            "projectWorkspacePage.members.actions.saveSnapshot",
                          )}
                        </Button>
                      </footer>
                    )}
                  </>
                ) : capabilitySection === "tools" ? (
                  <ToolsTab
                    agentId={projectAgent?.id || agentId}
                    agentName={memberName}
                    canManage={canManage && !departed}
                    canConfigure={
                      Boolean(projectAgent) && canManage && !departed
                    }
                    scope="project"
                    projectContext={
                      projectAgent ? undefined : { projectId, memberId }
                    }
                  />
                ) : capabilitySection === "mcp" ? (
                  renderCapabilityGroup(
                    "mcp",
                    memberMcps,
                    t("projectAgents.capabilityPackage.sections.mcp"),
                  )
                ) : (
                  <SkillsTab
                    agentId={projectAgent?.id || agentId}
                    canManage={Boolean(projectAgent) && canManage && !departed}
                  />
                )}
              </ProjectAgentCapabilityPanel>
            </section>
          )}
        </>
      ) : (
        <EmptyState
          icon={<IconUsers size={22} />}
          title={t("projectAgents.teamPage.noMembersTitle")}
          description={t("projectAgents.teamPage.noMembersDescription")}
          action={
            canManage ? (
              <Button
                variant="primary"
                onClick={() => {
                  resetAgentDraft();
                  setAgentDrawerMode("create");
                }}
              >
                <IconPlus size={16} />
                {t("projectAgents.actions.create")}
              </Button>
            ) : undefined
          }
        />
      )}

      <Drawer
        open={Boolean(agentDrawerMode)}
        onClose={() => {
          if (!busyAction.includes("project-agent")) setAgentDrawerMode(null);
        }}
        ariaLabel={t(
          agentDrawerMode === "edit"
            ? "projectAgents.editor.title"
            : "projectAgents.create.title",
        )}
        className="project-workspace__agent-drawer"
      >
        <div className="project-workspace__agent-drawer-content">
          <header>
            <div>
              <span>{t("projectAgents.badge")}</span>
              <h2>
                {t(
                  agentDrawerMode === "edit"
                    ? "projectAgents.editor.title"
                    : "projectAgents.create.title",
                )}
              </h2>
              <p>
                {t(
                  agentDrawerMode === "edit"
                    ? "projectAgents.editor.description"
                    : "projectAgents.create.description",
                )}
              </p>
            </div>
            <ProjectIconButton
              aria-label={t("common.close")}
              disabled={busyAction.includes("project-agent")}
              onClick={() => setAgentDrawerMode(null)}
            >
              <IconX size={18} />
            </ProjectIconButton>
          </header>
          <div className="project-workspace__agent-drawer-body">
            {projectAgent ? (
              <div className="project-workspace__member-files-action">
                <Button
                  variant="ghost"
                  onClick={() => {
                    setAgentDrawerMode(null);
                    onOpenWorkspace(`${projectAgent.agent_dir}/soul.md`);
                  }}
                >
                  {t("projectAgents.actions.openWorkspace")}
                  <IconArrowRight size={14} />
                </Button>
              </div>
            ) : null}
            {promotedAgent ? (
              <div
                className="project-workspace__agent-promotion-success"
                role="status"
              >
                <IconCircleCheck size={20} />
                <div>
                  <strong>{t("projectAgents.promotion.title")}</strong>
                  <p>{t("projectAgents.promotion.description")}</p>
                  <Link to={`/agents/${promotedAgent.id}/chat`}>
                    {t("projectAgents.promotion.open")}
                    <IconArrowRight size={14} />
                  </Link>
                </div>
              </div>
            ) : null}
            {agentDrawerMode === "create" && (
              <>
                <ProjectSegmentedControl
                  value={createKind}
                  options={[
                    {
                      value: "copy",
                      label: t("projectAgents.create.copy"),
                    },
                    {
                      value: "blank",
                      label: t("projectAgents.create.blank"),
                    },
                  ]}
                  onChange={setCreateKind}
                  ariaLabel={t("projectAgents.create.mode")}
                />
                {createKind === "copy" &&
                  (agentsError ? (
                    <div
                      className="project-workspace__repository-error"
                      role="alert"
                    >
                      <IconAlertTriangle size={16} />
                      <span>{agentsError}</span>
                    </div>
                  ) : candidateOptions.length ? (
                    <>
                      <ProjectField label={t("projectAgents.create.source")}>
                        <ProjectSelect
                          value={selectedCandidateId}
                          options={candidateOptions}
                          onChange={setCandidateAgentId}
                          ariaLabel={t("projectAgents.create.source")}
                          disabled={
                            agentsLoading ||
                            busyAction === "create-project-agent"
                          }
                        />
                      </ProjectField>
                      {selectedCandidate && (
                        <>
                          <div className="project-workspace__member-candidate">
                            <span>
                              {(text(selectedCandidate, "name") || "A").slice(
                                0,
                                1,
                              )}
                            </span>
                            <div>
                              <strong>
                                {text(selectedCandidate, "name") ||
                                  t("projectAgents.unnamed")}
                              </strong>
                              <p>
                                {projectUserFacingCopy(
                                  text(
                                    selectedCandidate,
                                    "role_description",
                                  ) || t("projectAgents.defaultRole"),
                                  t,
                                )}
                              </p>
                            </div>
                          </div>
                          <section className="project-workspace__capability-preview">
                            <header>
                              <strong>
                                {t(
                                  "projectAgents.capabilityPackage.carryPreview",
                                )}
                              </strong>
                            </header>
                            <div>
                              {(["tool", "mcp", "skill"] as const).map(
                                (kind) => (
                                  <span key={kind}>
                                    <strong>
                                      {candidateCapabilities.filter(
                                        (capability) =>
                                          capability.kind === kind,
                                      ).length ||
                                        (kind === "skill"
                                          ? num(selectedCandidate, "skill_count")
                                          : kind === "mcp"
                                            ? num(
                                                selectedCandidate,
                                                "mcp_count",
                                              )
                                            : 0)}
                                    </strong>
                                    <small>
                                      {t(
                                        `projectAgents.capabilityPackage.sections.${kind === "tool" ? "tools" : kind}`,
                                      )}
                                    </small>
                                  </span>
                                ),
                              )}
                            </div>
                          </section>
                        </>
                      )}
                    </>
                  ) : (
                    <ProjectEmptyState
                      icon={
                        agentsLoading ? (
                          <IconLoader2
                            className="project-workspace__spinner"
                            size={20}
                          />
                        ) : (
                          <IconUsers size={20} />
                        )
                      }
                      title={t(
                        agentsLoading
                          ? "projectAgents.create.loading"
                          : "projectAgents.create.noSource",
                      )}
                      description={t(
                        agentsLoading
                          ? "projectAgents.create.loadingDescription"
                          : "projectAgents.create.noSourceDescription",
                      )}
                    />
                  ))}
              </>
            )}
            {(agentDrawerMode === "edit" || createKind === "blank") && (
              <div className="project-workspace__agent-form">
                <ProjectField
                  label={t("projectAgents.fields.name")}
                  labelFor="project-agent-name"
                >
                  <TextInput
                    id="project-agent-name"
                    value={agentDraft.name}
                    onChange={(event) =>
                      setAgentDraft((current) => ({
                        ...current,
                        name: event.target.value,
                      }))
                    }
                    disabled={
                      agentDrawerMode === "edit" && (!canManage || departed)
                    }
                  />
                </ProjectField>
                <ProjectField
                  label={t("projectAgents.fields.role")}
                  labelFor="project-agent-role"
                >
                  <ProjectTextarea
                    id="project-agent-role"
                    value={agentDraft.roleDescription}
                    rows={2}
                    onChange={(event) =>
                      setAgentDraft((current) => ({
                        ...current,
                        roleDescription: event.target.value,
                      }))
                    }
                    disabled={
                      agentDrawerMode === "edit" && (!canManage || departed)
                    }
                  />
                </ProjectField>
                <ProjectField
                  label={t("projectAgents.fields.soul")}
                  labelFor="project-agent-soul"
                  hint={t("projectAgents.fields.soulHint")}
                >
                  <ProjectTextarea
                    id="project-agent-soul"
                    value={agentDraft.soul}
                    rows={7}
                    onChange={(event) =>
                      setAgentDraft((current) => ({
                        ...current,
                        soul: event.target.value,
                      }))
                    }
                    disabled={
                      agentDrawerMode === "edit" && (!canManage || departed)
                    }
                  />
                </ProjectField>
                <ProjectField
                  label={t("projectAgents.fields.memory")}
                  labelFor="project-agent-memory"
                  hint={t("projectAgents.fields.memoryHint")}
                >
                  <ProjectTextarea
                    id="project-agent-memory"
                    value={agentDraft.coreMemory}
                    rows={7}
                    onChange={(event) =>
                      setAgentDraft((current) => ({
                        ...current,
                        coreMemory: event.target.value,
                      }))
                    }
                    disabled={
                      agentDrawerMode === "edit" && (!canManage || departed)
                    }
                  />
                </ProjectField>
              </div>
            )}
          </div>
          {(agentDrawerMode === "create" ||
            (agentDrawerMode === "edit" && canManage && !departed)) && (
            <footer>
              {agentDrawerMode === "edit" && projectAgent ? (
                <Button
                  variant="primary"
                  onClick={saveProjectAgent}
                  disabled={
                    agentDraft.name.trim().length < 2 ||
                    busyAction === "save-project-agent"
                  }
                >
                  <IconDeviceFloppy size={16} />
                  {t("projectAgents.actions.save")}
                </Button>
              ) : agentDrawerMode === "create" ? (
                <>
                  <Button
                    variant="secondary"
                    onClick={() => setAgentDrawerMode(null)}
                  >
                    {t("common.cancel")}
                  </Button>
                  <Button
                    variant="primary"
                    onClick={createOwnedAgent}
                    disabled={
                      busyAction === "create-project-agent" ||
                      (createKind === "copy"
                        ? !selectedCandidateId || agentsLoading
                        : agentDraft.name.trim().length < 2)
                    }
                  >
                    {busyAction === "create-project-agent" ? (
                      <IconLoader2
                        className="project-workspace__spinner"
                        size={16}
                      />
                    ) : (
                      <IconPlus size={16} />
                    )}
                    {t("projectAgents.actions.create")}
                  </Button>
                </>
              ) : null}
            </footer>
          )}
        </div>
      </Drawer>

      <ProjectDialog
        open={promoteDialogOpen}
        onClose={() => {
          if (busyAction !== "promote-project-agent") {
            setPromoteDialogOpen(false);
          }
        }}
        ariaLabel={t("projectAgents.promotion.confirmTitle", {
          name: projectAgent?.name || memberName,
        })}
        className="project-workspace__member-dialog"
      >
        <div className="project-workspace__modal">
          <header>
            <div>
              <span>{t("projectAgents.promotion.badge")}</span>
              <h2>
                {t("projectAgents.promotion.confirmTitle", {
                  name: projectAgent?.name || memberName,
                })}
              </h2>
            </div>
            <ProjectIconButton
              aria-label={t("common.close")}
              disabled={busyAction === "promote-project-agent"}
              onClick={() => setPromoteDialogOpen(false)}
            >
              <IconX size={18} />
            </ProjectIconButton>
          </header>
          <p>{t("projectAgents.promotion.confirmDescription")}</p>
          <footer>
            <Button
              variant="secondary"
              onClick={() => setPromoteDialogOpen(false)}
              disabled={busyAction === "promote-project-agent"}
            >
              {t("common.cancel")}
            </Button>
            <Button
              variant="primary"
              onClick={promoteProjectAgent}
              disabled={busyAction === "promote-project-agent"}
            >
              {busyAction === "promote-project-agent" ? (
                <IconLoader2 className="project-workspace__spinner" size={16} />
              ) : (
                <IconSparkles size={16} />
              )}
              {t("projectAgents.promotion.confirm")}
            </Button>
          </footer>
        </div>
      </ProjectDialog>

      <ProjectDialog
        open={removeDialogOpen}
        onClose={() => {
          if (busyAction !== "remove-member") setRemoveDialogOpen(false);
        }}
        ariaLabel={
          projectAgent
            ? t("projectAgents.deactivate.title", { name: memberName })
            : t("projectAgents.removeMember.title", { name: memberName })
        }
        className="project-workspace__member-dialog"
      >
        <div className="project-workspace__modal">
          <header>
            <div>
              <span>
                {projectAgent
                  ? t("projectAgents.badge")
                  : t("projectAgents.removeMember.badge")}
              </span>
              <h2>
                {projectAgent
                  ? t("projectAgents.deactivate.title", { name: memberName })
                  : t("projectAgents.removeMember.title", {
                      name: memberName,
                    })}
              </h2>
            </div>
            <ProjectIconButton
              aria-label={t("common.close")}
              disabled={busyAction === "remove-member"}
              onClick={() => setRemoveDialogOpen(false)}
            >
              <IconX size={18} />
            </ProjectIconButton>
          </header>
          <p>
            {projectAgent
              ? t("projectAgents.deactivate.description")
              : t("projectAgents.removeMember.description")}
          </p>
          <footer>
            <Button
              variant="secondary"
              onClick={() => setRemoveDialogOpen(false)}
              disabled={busyAction === "remove-member"}
            >
              {projectAgent
                ? t("projectAgents.actions.keepActive")
                : t("projectAgents.removeMember.keep")}
            </Button>
            <Button
              variant="danger"
              onClick={removeMember}
              disabled={busyAction === "remove-member"}
            >
              {busyAction === "remove-member" ? (
                <IconLoader2 className="project-workspace__spinner" size={16} />
              ) : (
                <IconTrash size={16} />
              )}
              {projectAgent
                ? t("projectAgents.actions.deactivate")
                : t("projectAgents.removeMember.confirm")}
            </Button>
          </footer>
        </div>
      </ProjectDialog>
    </>
  );
}

function ProjectToolsControl({
  projectId,
  members,
  policies,
  runAction,
  busyAction,
  canManage,
  fixedMemberId,
  compact = false,
}: {
  projectId: string;
  members: RecordValue[];
  policies: RecordValue | null;
  runAction: (
    key: string,
    action: () => Promise<unknown>,
    success: string,
  ) => Promise<boolean>;
  busyAction: string;
  canManage: boolean;
  fixedMemberId?: string;
  compact?: boolean;
}) {
  const { t } = useTranslation();
  const { get, update } = useContext(WorkspaceNavigationContext);
  const activeMembers = useMemo(
    () => members.filter((entry) => entry.is_enabled !== false),
    [members],
  );
  const requestedMemberId = get("toolMember");
  const selectedMemberId = fixedMemberId
    ? fixedMemberId
    : activeMembers.some(
          (member) => text(member, "id", "member_id") === requestedMemberId,
        )
      ? requestedMemberId
      : text(activeMembers[0] || {}, "id", "member_id");
  const member =
    members.find(
      (entry) => text(entry, "id", "member_id") === selectedMemberId,
    ) || (fixedMemberId ? undefined : activeMembers[0]);
  const memberId = text(member || {}, "id", "member_id");
  const memberName =
    text(member || {}, "name_snapshot", "agent_name", "name") ||
    t("projectAgents.defaultRole");
  const effectiveCount = member
    ? PROJECT_TOOL_REGISTRY.filter(
        (tool) => projectToolResolution(tool, member, policies).effective,
      ).length
    : 0;
  const { pageItems: visibleTools, pagination: toolsPagination } =
    useWorkspacePagination(PROJECT_TOOL_REGISTRY, "projectTools", 6, [6, 12]);
  const toggleTool = (tool: ProjectToolDefinition, checked: boolean) => {
    if (!member || !memberId) return;
    const resolution = projectToolResolution(tool, member, policies);
    if (
      !resolution.roleCeiling ||
      resolution.policyBlocked ||
      resolution.snapshotBlocked
    )
      return;
    const config = obj(member.config_snapshot);
    const existingDisabled = stringList(config.disabled_project_tools);
    const disabled = new Set(existingDisabled);
    if (checked) disabled.delete(tool.name);
    else disabled.add(tool.name);
    const knownNames = new Set(
      PROJECT_TOOL_REGISTRY.map((entry) => entry.name),
    );
    const disabledProjectTools = [
      ...existingDisabled.filter((name) => !knownNames.has(name)),
      ...PROJECT_TOOL_REGISTRY.map((entry) => entry.name).filter((name) =>
        disabled.has(name),
      ),
    ];
    void runAction(
      `project-tool-${memberId}-${tool.name}`,
      () =>
        projectsApi.patchMember(projectId, memberId, {
          config_snapshot: {
            ...config,
            disabled_project_tools: disabledProjectTools,
          },
        }),
      t("projectManagementTools.updated", { name: memberName }),
    );
  };
  const renderedTools = compact ? PROJECT_TOOL_REGISTRY : visibleTools;
  return (
    <section
      className={`project-workspace__project-tools${compact ? " is-compact" : ""}`}
    >
      <header className="project-workspace__subsection-heading">
        <div>
          <h3>{t("projectManagementTools.title")}</h3>
          {!compact ? <p>{t("projectManagementTools.description")}</p> : null}
        </div>
        {member && !compact ? (
          <div className="project-workspace__project-tool-member">
            <ProjectSelect
              value={memberId}
              options={activeMembers.map((entry) => ({
                value: text(entry, "id", "member_id"),
                label: bool(entry, "is_leader")
                  ? t("projectTerminology.workspace.memberOwner", {
                      name: text(entry, "name_snapshot", "agent_name", "name"),
                    })
                  : text(entry, "name_snapshot", "agent_name", "name"),
              }))}
              onChange={(value) => update({ toolMember: value })}
              ariaLabel={t("projectManagementTools.selectMember")}
            />
            <ProjectCountBadge>
              {effectiveCount} / {PROJECT_TOOL_REGISTRY.length}
            </ProjectCountBadge>
          </div>
        ) : member ? (
          <ProjectCountBadge>
            {effectiveCount} / {PROJECT_TOOL_REGISTRY.length}
          </ProjectCountBadge>
        ) : null}
      </header>
      {member ? (
        <>
          <div className="project-workspace__project-tool-grid">
            {renderedTools.map((tool) => {
              const resolution = projectToolResolution(tool, member, policies);
              const blockedLabel = !resolution.roleCeiling
                ? t("projectTerminology.ownerDedicated")
                : resolution.policyBlocked
                  ? t("projectManagementTools.status.policyBlocked")
                  : resolution.snapshotBlocked
                    ? t("projectManagementTools.status.snapshotBlocked")
                    : resolution.memberDisabled
                      ? t("projectManagementTools.status.memberDisabled")
                      : t("projectManagementTools.status.available");
              const tone = resolution.effective
                ? "success"
                : resolution.memberDisabled
                  ? "neutral"
                  : "warning";
              const actionKey = `project-tool-${memberId}-${tool.name}`;
              return (
                <article
                  key={tool.name}
                  className={resolution.effective ? "is-effective" : ""}
                >
                  <header>
                    <span>
                      <IconTool size={16} />
                    </span>
                    <div>
                      <strong>
                        {t(
                          `projectManagementTools.registry.${tool.name}.label`,
                          {
                            defaultValue: tool.label,
                          },
                        )}
                      </strong>
                    </div>
                    {canManage ? (
                      <ToggleSwitch
                        checked={resolution.effective}
                        onChange={(checked) => toggleTool(tool, checked)}
                        ariaLabel={t(
                          resolution.effective
                            ? "projectManagementTools.disableTool"
                            : "projectManagementTools.enableTool",
                          {
                            name: t(
                              `projectManagementTools.registry.${tool.name}.label`,
                              { defaultValue: tool.label },
                            ),
                          },
                        )}
                        disabled={
                          !resolution.roleCeiling ||
                          resolution.policyBlocked ||
                          resolution.snapshotBlocked ||
                          busyAction === actionKey
                        }
                      />
                    ) : (
                      <ProjectStatusBadge tone={tone}>
                        {blockedLabel}
                      </ProjectStatusBadge>
                    )}
                  </header>
                  <p>
                    {tool.descriptionKey
                      ? t(tool.descriptionKey)
                      : t(
                          `projectManagementTools.registry.${tool.name}.description`,
                          { defaultValue: tool.description },
                        )}
                  </p>
                </article>
              );
            })}
          </div>
          {!compact ? toolsPagination : null}
        </>
      ) : (
        <ProjectEmptyState
          icon={<IconUsers size={22} />}
          title={t("projectManagementTools.emptyTitle")}
          description={t("projectManagementTools.emptyDescription")}
        />
      )}
    </section>
  );
}

function CapabilitiesPanel({
  members,
  capabilities,
  policies,
}: {
  members: RecordValue[];
  capabilities: RecordValue[];
  policies: RecordValue | null;
}) {
  const { t } = useTranslation();
  const { get, update } = useContext(WorkspaceNavigationContext);
  const view = get("capView") === "matrix" ? "matrix" : "list";
  const requestedFilter = get("capFilter");
  const filter = ["all", "skill", "mcp", "project", "agent"].includes(
    requestedFilter,
  )
    ? requestedFilter
    : "all";
  const departedAgentIds = new Set(
    members
      .filter((member) => member.is_enabled === false)
      .map((member) => text(member, "agent_id"))
      .filter(Boolean),
  );
  const filteredCapabilities = capabilities.filter((cap) => {
    if (filter === "all") return true;
    if (["skill", "mcp"].includes(filter))
      return text(cap, "capability_type", "kind", "type") === filter;
    const inherited =
      Boolean(text(cap, "inherited_from_agent_id")) ||
      ["agent", "inherited"].includes(text(cap, "source"));
    return filter === "agent" ? inherited : !inherited;
  });
  const { pageItems: visibleCapabilities, pagination } = useWorkspacePagination(
    filteredCapabilities,
    "capabilities",
    12,
    [12, 24, 48],
  );
  return (
    <>
      <SectionHeading
        eyebrow="CAPABILITY CONTROL"
        title={t("projectAgents.teamPage.capabilities")}
        description={t("projectAgents.teamPage.capabilitiesDescription")}
        actions={
          <ProjectSegmentedControl
            value={view}
            options={[
              {
                value: "list",
                label: t("projectAgents.teamPage.listView"),
              },
              {
                value: "matrix",
                label: t("projectAgents.teamPage.matrixView"),
              },
            ]}
            onChange={(nextView) =>
              update({
                capView: nextView === "matrix" ? "matrix" : undefined,
              })
            }
            ariaLabel={t("projectAgents.teamPage.viewModeAria")}
          />
        }
      />
      {view === "matrix" ? (
        <CapabilityMatrix
          members={members}
          capabilities={capabilities}
          policies={policies}
        />
      ) : (
        <>
          {capabilities.length ? (
            <>
              <ProjectSegmentedControl
                className="project-workspace__filters"
                value={filter}
                options={[
                  { value: "all", label: t("common.all") },
                  { value: "skill", label: "Skill" },
                  { value: "mcp", label: "MCP" },
                  {
                    value: "project",
                    label: t("projectWorkspacePage.capabilities.projectShared"),
                  },
                  {
                    value: "agent",
                    label: t("projectWorkspacePage.capabilities.inherited"),
                  },
                ]}
                onChange={(value) =>
                  update({ capFilter: value, capabilitiesPage: undefined })
                }
                ariaLabel={t("projectWorkspacePage.capabilities.filterAria")}
              />
              {filteredCapabilities.length ? (
                <>
                  <div className="project-workspace__cap-grid">
                    {visibleCapabilities.map((cap) => {
                      const id = text(cap, "id", "binding_id", "capability_id");
                      const capabilityType = text(
                        cap,
                        "capability_type",
                        "kind",
                        "type",
                      );
                      const enabled = cap.is_enabled !== false;
                      const scopeCount = Object.keys(obj(cap.scope)).length;
                      const inheritedFromAgentId = text(
                        cap,
                        "inherited_from_agent_id",
                      );
                      const inheritedFromMember = members.find(
                        (member) => text(member, "agent_id") === inheritedFromAgentId,
                      );
                      const inheritedFromName = text(
                        inheritedFromMember || {},
                        "name_snapshot",
                        "agent_name",
                        "name",
                      );
                      const departedOwner = Boolean(
                        inheritedFromAgentId &&
                        departedAgentIds.has(inheritedFromAgentId),
                      );
                      return (
                        <article
                          key={id}
                          className={departedOwner ? "is-readonly" : ""}
                        >
                          <header>
                            <span className={`is-${capabilityType || "skill"}`}>
                              {capabilityType === "mcp" ? (
                                <IconCodeDots size={17} />
                              ) : (
                                <IconTool size={17} />
                              )}
                            </span>
                            <div>
                              <strong>
                                {text(cap, "name", "capability_name")}
                              </strong>
                              <small>
                                {text(cap, "version") ||
                                  capabilityType.toUpperCase() ||
                                  t(
                                    "projectWorkspacePage.capabilities.capability",
                                  )}{" "}
                                ·{" "}
                                {inheritedFromAgentId
                                  ? t(
                                      "projectWorkspacePage.capabilities.inheritedFrom",
                                      {
                                        name: inheritedFromName ||
                                          t("projectTerminology.dynamicCopy.digitalEmployee"),
                                        departed: departedOwner
                                          ? t(
                                              "projectWorkspacePage.capabilities.departedSuffix",
                                            )
                                          : "",
                                      },
                                    )
                                  : t(
                                      "projectWorkspacePage.capabilities.projectShared",
                                    )}
                              </small>
                            </div>
                            <ProjectStatusBadge
                              tone={
                                enabled && !departedOwner
                                  ? "success"
                                  : "neutral"
                              }
                            >
                              {t(
                                enabled && !departedOwner
                                  ? "projectManagementTools.status.available"
                                  : "projectManagementTools.status.memberDisabled",
                              )}
                            </ProjectStatusBadge>
                          </header>
                          <p>
                            {departedOwner
                              ? t(
                                  "projectWorkspacePage.capabilities.departedDescription",
                                )
                              : text(cap, "description") ||
                                t(
                                  "projectWorkspacePage.capabilities.noDescription",
                                )}
                          </p>
                          <footer>
                            <span>
                              {scopeCount
                                ? t(
                                    "projectWorkspacePage.capabilities.scopeCount",
                                    {
                                      count: scopeCount,
                                    },
                                  )
                                : t(
                                    "projectWorkspacePage.capabilities.noScope",
                                  )}
                            </span>
                            {departedOwner ? (
                              <ProjectStatusBadge tone="neutral">
                                {t(
                                  "projectWorkspacePage.members.historical.title",
                                )}
                              </ProjectStatusBadge>
                            ) : null}
                          </footer>
                        </article>
                      );
                    })}
                  </div>
                  {pagination}
                </>
              ) : (
                <EmptyState
                  icon={<IconTool size={22} />}
                  title={t("projectAgents.teamPage.capabilitiesEmptyTitle")}
                  description={t(
                    "projectAgents.teamPage.capabilitiesEmptyDescription",
                  )}
                />
              )}
            </>
          ) : (
            <EmptyState
              icon={<IconTool size={22} />}
              title={t("projectAgents.teamPage.capabilitiesEmptyTitle")}
              description={t(
                "projectAgents.teamPage.capabilitiesEmptyDescription",
              )}
            />
          )}
        </>
      )}
    </>
  );
}

function CapabilityBindingsMatrix({
  members,
  capabilities,
}: {
  members: RecordValue[];
  capabilities: RecordValue[];
}) {
  const { t } = useTranslation();
  const { pageItems: visibleCapabilities, pagination } = useWorkspacePagination(
    capabilities,
    "capabilityMatrix",
    10,
    [10, 20, 50],
  );
  return (
    <>
      <SectionHeading
        eyebrow="EFFECTIVE CAPABILITIES"
        title={t("projectWorkspaceNav.tabs.matrix")}
        description={t(
          "projectTerminology.workspace.capabilityMatrixDescription",
        )}
      />
      {members.length && capabilities.length ? (
        <>
          <div className="project-workspace__matrix-wrap">
            <ProjectDataTable>
              <ProjectDataTableHead>
                <ProjectDataTableRow>
                  <ProjectDataTableHeader>
                    {t("projectWorkspacePage.capabilities.columns.capability")}
                  </ProjectDataTableHeader>
                  <ProjectDataTableHeader>
                    {t("projectWorkspacePage.capabilities.columns.source")}
                  </ProjectDataTableHeader>
                  {members.map((member) => (
                    <ProjectDataTableHeader
                      key={text(member, "agent_id", "id", "member_id")}
                    >
                      {text(member, "agent_name", "name_snapshot", "name")}
                      <small>
                        {member.is_enabled === false
                          ? t(
                              "projectWorkspacePage.capabilities.departedHistorical",
                            )
                          : bool(member, "is_leader")
                            ? t("projectTerminology.owner")
                            : t("projectAgents.status.active")}
                      </small>
                    </ProjectDataTableHeader>
                  ))}
                </ProjectDataTableRow>
              </ProjectDataTableHead>
              <ProjectDataTableBody>
                {visibleCapabilities.map((cap) => {
                  const inheritedAgentId = text(cap, "inherited_from_agent_id");
                  const shared = text(cap, "source") === "shared";
                  const inherited =
                    Boolean(inheritedAgentId) ||
                    ["agent", "inherited"].includes(text(cap, "source"));
                  return (
                    <ProjectDataTableRow
                      key={text(cap, "id", "binding_id", "capability_id")}
                    >
                      <ProjectDataTableCell>
                        <strong>{text(cap, "name", "capability_name")}</strong>
                        <small>
                          {text(cap, "capability_type", "kind", "type")}
                        </small>
                      </ProjectDataTableCell>
                      <ProjectDataTableCell>
                        {t(
                          inherited
                            ? "projectWorkspacePage.capabilities.inherited"
                            : "projectWorkspacePage.capabilities.projectShared",
                        )}
                      </ProjectDataTableCell>
                      {members.map((member) => {
                        const id = text(member, "agent_id", "id", "member_id");
                        const enabled = cap.is_enabled !== false;
                        const resolved: "yes" | "no" | "unknown" =
                          member.is_enabled === false || !enabled
                            ? "no"
                            : inheritedAgentId
                              ? inheritedAgentId === id
                                ? "yes"
                                : "no"
                              : shared
                                ? "yes"
                                : "no";
                        return (
                          <ProjectDataTableCell key={id}>
                            <span
                              className={`project-workspace__matrix-${resolved}`}
                            >
                              {resolved === "yes" ? (
                                <IconCircleCheck size={17} />
                              ) : resolved === "no" ? (
                                <IconX size={16} />
                              ) : (
                                <span aria-hidden="true">—</span>
                              )}
                              <small>
                                {member.is_enabled === false
                                  ? t(
                                      "projectWorkspacePage.members.historical.title",
                                    )
                                  : resolved === "yes"
                                    ? t(
                                        "projectManagementTools.status.available",
                                      )
                                    : resolved === "no"
                                      ? t(
                                          "projectManagementTools.status.unavailable",
                                        )
                                      : t(
                                          "projectWorkspacePage.capabilities.policyResolved",
                                        )}
                              </small>
                            </span>
                          </ProjectDataTableCell>
                        );
                      })}
                    </ProjectDataTableRow>
                  );
                })}
              </ProjectDataTableBody>
            </ProjectDataTable>
          </div>
          {pagination}
        </>
      ) : (
        <EmptyState
          icon={<IconCodeDots size={22} />}
          title={t("projectAgents.teamPage.matrixEmptyTitle")}
          description={t("projectAgents.teamPage.matrixEmptyDescription")}
        />
      )}
    </>
  );
}

function ProjectToolsMatrix({
  members,
  policies,
}: {
  members: RecordValue[];
  policies: RecordValue | null;
}) {
  const { t } = useTranslation();
  const { pageItems: visibleTools, pagination } = useWorkspacePagination(
    PROJECT_TOOL_REGISTRY,
    "projectToolMatrix",
    10,
    [10, 20],
  );
  return (
    <section className="project-workspace__project-tool-matrix">
      <header className="project-workspace__subsection-heading">
        <div>
          <h3>{t("projectManagementTools.matrixTitle")}</h3>
          <p>{t("projectManagementTools.matrixDescription")}</p>
        </div>
        <ProjectCountBadge>
          {t("projectManagementTools.toolCount", {
            count: PROJECT_TOOL_REGISTRY.length,
          })}
        </ProjectCountBadge>
      </header>
      {members.length ? (
        <>
          <div className="project-workspace__matrix-wrap">
            <ProjectDataTable>
              <ProjectDataTableHead>
                <ProjectDataTableRow>
                  <ProjectDataTableHeader>
                    {t("projectManagementTools.toolColumn")}
                  </ProjectDataTableHeader>
                  <ProjectDataTableHeader>
                    {t("projectManagementTools.scopeColumn")}
                  </ProjectDataTableHeader>
                  {members.map((member) => (
                    <ProjectDataTableHeader
                      key={text(member, "id", "member_id", "agent_id")}
                    >
                      {text(member, "name_snapshot", "agent_name", "name")}
                      <small>
                        {member.is_enabled === false
                          ? t("projectAgents.status.departed")
                          : bool(member, "is_leader")
                            ? t("projectTerminology.owner")
                            : t("projectTerminology.workspace.projectMembers")}
                      </small>
                    </ProjectDataTableHeader>
                  ))}
                </ProjectDataTableRow>
              </ProjectDataTableHead>
              <ProjectDataTableBody>
                {visibleTools.map((tool) => (
                  <ProjectDataTableRow key={tool.name}>
                    <ProjectDataTableCell>
                      <strong>
                        {t(
                          `projectManagementTools.registry.${tool.name}.label`,
                          {
                            defaultValue: tool.label,
                          },
                        )}
                      </strong>
                    </ProjectDataTableCell>
                    <ProjectDataTableCell>
                      {tool.participant
                        ? t("projectTerminology.workspace.allMembers")
                        : t("projectTerminology.ownerOnly")}
                    </ProjectDataTableCell>
                    {members.map((member) => {
                      const resolution = projectToolResolution(
                        tool,
                        member,
                        policies,
                      );
                      const label = resolution.lifecycleBlocked
                        ? t("projectManagementTools.status.memberDeparted")
                        : resolution.effective
                          ? t("projectManagementTools.status.available")
                          : !resolution.roleCeiling
                            ? t("projectManagementTools.status.unavailable")
                            : resolution.memberDisabled
                              ? t(
                                  "projectManagementTools.status.memberDisabled",
                                )
                              : resolution.policyBlocked
                                ? t(
                                    "projectManagementTools.status.policyBlocked",
                                  )
                                : t("projectManagementTools.status.restricted");
                      return (
                        <ProjectDataTableCell
                          key={text(member, "id", "member_id", "agent_id")}
                        >
                          <span
                            className={
                              resolution.effective
                                ? "project-workspace__matrix-yes"
                                : resolution.lifecycleBlocked ||
                                    !resolution.roleCeiling
                                  ? "project-workspace__matrix-no"
                                  : "project-workspace__matrix-unknown"
                            }
                          >
                            {resolution.effective ? (
                              <IconCircleCheck size={17} />
                            ) : resolution.lifecycleBlocked ||
                              !resolution.roleCeiling ? (
                              <IconX size={16} />
                            ) : (
                              <IconClock size={16} />
                            )}
                            <small>{label}</small>
                          </span>
                        </ProjectDataTableCell>
                      );
                    })}
                  </ProjectDataTableRow>
                ))}
              </ProjectDataTableBody>
            </ProjectDataTable>
          </div>
          {pagination}
        </>
      ) : (
        <ProjectEmptyState
          icon={<IconUsers size={22} />}
          title={t("projectManagementTools.matrixEmptyTitle")}
          description={t("projectManagementTools.matrixEmptyDescription")}
        />
      )}
    </section>
  );
}

function CapabilityMatrix({
  members,
  capabilities,
  policies,
}: {
  members: RecordValue[];
  capabilities: RecordValue[];
  policies: RecordValue | null;
}) {
  return (
    <>
      <CapabilityBindingsMatrix members={members} capabilities={capabilities} />
      <ProjectToolsMatrix members={members} policies={policies} />
    </>
  );
}

function ProjectVisibilitySettings({
  projectId,
  project,
  onReload,
}: {
  projectId: string;
  project: ProjectSummary;
  onReload: () => Promise<void>;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const canManageAccess = project.access_role === "owner";
  const [visibility, setVisibility] = useState<"private" | "shared">(
    project.visibility,
  );
  const [sharedUserIds, setSharedUserIds] = useState<string[]>(
    project.shared_with_user_ids || [],
  );
  const [sharedUsers, setSharedUsers] = useState<AgentAccessUser[]>(() =>
    (project.shared_with_user_ids || []).map((userId, index) => ({
      id: userId,
      name: project.shared_with_names?.[index] || userId,
      access_level: "use",
    })),
  );
  const [executionUserId, setExecutionUserId] = useState(
    project.execution_user_id || "",
  );
  const [memberPickerOpen, setMemberPickerOpen] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [saving, setSaving] = useState(false);
  const [permissionDenied, setPermissionDenied] = useState(
    project.access_role !== "owner",
  );
  const sharedUserIdsVersion = (project.shared_with_user_ids || []).join("\u0000");
  const sharedUserNamesVersion = (project.shared_with_names || []).join("\u0000");

  useEffect(() => {
    setVisibility(project.visibility);
    setSharedUserIds(project.shared_with_user_ids || []);
    setSharedUsers(
      (project.shared_with_user_ids || []).map((userId, index) => ({
        id: userId,
        name: project.shared_with_names?.[index] || userId,
        access_level: "use",
      })),
    );
    setExecutionUserId(project.execution_user_id || "");
    setPermissionDenied(!canManageAccess);
    setSaveError("");
  }, [
    canManageAccess,
    project.execution_user_id,
    project.updated_at,
    project.visibility,
    sharedUserIdsVersion,
    sharedUserNamesVersion,
  ]);

  const readOnly = permissionDenied || !canManageAccess;
  const sharedWithoutMembers =
    visibility === "shared" && sharedUserIds.length === 0;
  const sharedWithoutExecutionUser =
    visibility === "shared" &&
    (!executionUserId || !sharedUserIds.includes(executionUserId));
  const save = async () => {
    if (readOnly) return;
    if (sharedWithoutMembers) {
      setSaveError(t("projectWorkspacePage.visibility.memberRequired"));
      return;
    }
    if (sharedWithoutExecutionUser) {
      setSaveError(t("projectWorkspacePage.visibility.executionUserRequired"));
      return;
    }
    setSaving(true);
    setSaveError("");
    try {
      await projectsApi.update(projectId, {
        visibility,
        shared_with_user_ids: visibility === "shared" ? sharedUserIds : [],
        execution_user_id:
          visibility === "shared" ? executionUserId : null,
      });
      toast.success(
        t(
          visibility === "shared"
            ? "projectWorkspacePage.visibility.feedback.shared"
            : "projectWorkspacePage.visibility.feedback.private",
        ),
      );
      await onReload();
    } catch (error) {
      const status = Number(obj(error).status);
      const message = errorMessage(
        error,
        t("projectWorkspacePage.errors.requestFailed"),
      );
      if (status === 403 || status === 404) setPermissionDenied(true);
      setSaveError(
        status === 403 || status === 404
          ? t("projectWorkspacePage.visibility.permissionDeniedDraft")
          : message,
      );
      toast.error(message);
    } finally {
      setSaving(false);
    }
  };
  const selectedUsers = sharedUsers.filter((user) =>
    sharedUserIds.includes(user.id),
  );

  return (
    <section className="project-workspace__visibility-settings">
      <header className="project-workspace__settings-section-heading">
        <div>
          <h3>{t("projectWorkspacePage.visibility.title")}</h3>
          <p>{t("projectWorkspacePage.visibility.description")}</p>
        </div>
      </header>
      <div className="project-workspace__visibility-body">
        <ProjectField
          label={
            <span className="project-workspace__visibility-label">
              {t("projectWorkspacePage.visibility.label")}{" "}
              <ProjectStatusBadge
                tone={visibility === "shared" ? "info" : "neutral"}
              >
                {t(
                  visibility === "shared"
                    ? "projectWorkspacePage.visibility.shared"
                    : "projectWorkspacePage.visibility.private",
                )}
              </ProjectStatusBadge>
            </span>
          }
          hint={t("projectWorkspacePage.visibility.privateHint")}
        >
          <ProjectSegmentedControl
            value={visibility}
            options={[
              {
                value: "private",
                label: t("projectWorkspacePage.visibility.private"),
              },
              {
                value: "shared",
                label: t("projectWorkspacePage.visibility.sharedWithMembers"),
              },
            ]}
            onChange={setVisibility}
            ariaLabel={t("projectWorkspacePage.visibility.aria")}
            disabled={readOnly}
          />
        </ProjectField>
        <ProjectField
          label={t("projectWorkspacePage.visibility.members")}
          hint={
            visibility === "shared"
              ? t("projectTerminology.workspace.shareOwnerExcluded")
              : t("projectWorkspacePage.visibility.membersHint")
          }
          error={
            sharedWithoutMembers
              ? t("projectWorkspacePage.visibility.memberRequiredShort")
              : undefined
          }
        >
          <button
            type="button"
            className="project-workspace__member-picker-trigger"
            onClick={() => setMemberPickerOpen(true)}
            disabled={readOnly || visibility !== "shared"}
            aria-label={t("projectWorkspacePage.visibility.selectMembersAria")}
          >
            <span>
              <IconUsers size={17} />
              {selectedUsers.length
                  ? t("projectWorkspacePage.visibility.selectedMembers", {
                      count: selectedUsers.length,
                    })
                  : t("projectWorkspacePage.visibility.selectMembers")}
            </span>
            <IconChevronRight size={17} />
          </button>
          {selectedUsers.length > 0 && (
            <div className="project-workspace__selected-members" aria-live="polite">
              {selectedUsers.slice(0, 4).map((user) => (
                <span key={user.id}>{user.name}</span>
              ))}
              {selectedUsers.length > 4 && <span>+{selectedUsers.length - 4}</span>}
            </div>
          )}
        </ProjectField>
        <ProjectField
          label={t("projectWorkspacePage.visibility.executionUser")}
          hint={t("projectWorkspacePage.visibility.executionUserHint")}
          error={
            sharedWithoutExecutionUser
              ? t(
                  "projectWorkspacePage.visibility.executionUserRequiredShort",
                )
              : undefined
          }
        >
          <ProjectSelect
            value={visibility === "shared" ? executionUserId : "owner"}
            options={
              visibility === "shared"
                ? selectedUsers.map((user) => ({
                    value: user.id,
                    label: user.name,
                  }))
                : [
                    {
                      value: "owner",
                      label:
                        project.owner_name ||
                        t("projectWorkspacePage.visibility.executionUserOwner"),
                    },
                  ]
            }
            onChange={setExecutionUserId}
            ariaLabel={t("projectWorkspacePage.visibility.executionUser")}
            disabled={
              readOnly || visibility !== "shared" || selectedUsers.length === 0
            }
            placeholder={t(
              "projectWorkspacePage.visibility.selectExecutionUser",
            )}
          />
        </ProjectField>
      </div>
      <footer>
        <div>
          {readOnly && (
            <small className="is-warning">
              {t("projectWorkspacePage.visibility.readOnly")}
            </small>
          )}
          {saveError && (
            <small className="is-error" role="alert">
              {saveError}
            </small>
          )}
        </div>
        {!readOnly && (
          <Button
            variant="secondary"
            onClick={() => void save()}
            disabled={
              readOnly ||
              saving ||
              sharedWithoutMembers ||
              sharedWithoutExecutionUser
            }
          >
            {saving ? (
              <IconLoader2 className="project-workspace__spinner" size={16} />
            ) : (
              <IconLock size={16} />
            )}
            {t("projectWorkspacePage.visibility.save")}
          </Button>
        )}
      </footer>
      <OrgMemberAccessPicker
        open={memberPickerOpen}
        agentId={projectId}
        directoryBaseUrl={`/projects/${projectId}/directory`}
        membersOnly
        users={selectedUsers}
        departments={[]}
        onClose={() => setMemberPickerOpen(false)}
        onSave={async (users) => {
          setSharedUsers(users);
          const userIds = users.map((user) => user.id);
          setSharedUserIds(userIds);
          if (!userIds.includes(executionUserId)) setExecutionUserId("");
        }}
      />
    </section>
  );
}

function PoliciesPanel({
  projectId,
  project,
  policies,
  onReload,
  runAction,
  busyAction,
}: {
  projectId: string;
  project: ProjectSummary;
  policies: RecordValue | null;
  onReload: () => Promise<void>;
  runAction: (
    key: string,
    action: () => Promise<unknown>,
    success: string,
  ) => Promise<boolean>;
  busyAction: string;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const isOwner = project.access_role === "owner";
  const canManageSettings = isOwner;
  const governance = obj(policies?.policies);
  const [model, setModel] = useState("default");
  const [models, setModels] = useState<
    Array<{
      id: string;
      provider: string;
      model: string;
      label?: string;
      enabled?: boolean;
    }>
  >([]);
  const [modelsLoading, setModelsLoading] = useState(true);
  const [modelsError, setModelsError] = useState("");
  const [parallel, setParallel] = useState("4");
  const [a2aLimit, setA2aLimit] = useState("12");
  const [loopGuard, setLoopGuard] = useState(true);
  const [templateDialogOpen, setTemplateDialogOpen] = useState(false);
  const [templateName, setTemplateName] = useState(project.name);
  const [templateNameError, setTemplateNameError] = useState("");
  const [publishingTemplate, setPublishingTemplate] = useState(false);
  const [templateManifest, setTemplateManifest] =
    useState<ProjectTemplateManifest | null>(null);
  const [templateManifestLoading, setTemplateManifestLoading] = useState(false);
  const [templateManifestError, setTemplateManifestError] = useState("");
  const [includedTemplateSkillIds, setIncludedTemplateSkillIds] = useState<
    string[]
  >([]);
  useEffect(() => {
    const nextRuntime = obj(policies?.runtime);
    const nextGovernance = obj(policies?.policies);
    setModel(text(nextRuntime, "model", "default_model") || "default");
    setParallel(text(nextRuntime, "max_parallel_runs") || "4");
    setA2aLimit(text(nextGovernance, "max_a2a_wakes") || "12");
    setLoopGuard(nextGovernance.loop_guard !== false);
  }, [policies]);
  useEffect(() => {
    setTemplateName(project.name);
    setTemplateNameError("");
  }, [project.id, project.name]);
  const loadTemplateManifest = useCallback(async () => {
    if (!isOwner) return;
    setTemplateManifestLoading(true);
    setTemplateManifestError("");
    setIncludedTemplateSkillIds([]);
    try {
      setTemplateManifest(await projectsApi.getTemplateManifest(projectId));
    } catch (error) {
      setTemplateManifest(null);
      setTemplateManifestError(
        errorMessage(error, t("projectWorkspacePage.errors.requestFailed")),
      );
    } finally {
      setTemplateManifestLoading(false);
    }
  }, [isOwner, projectId]);
  useEffect(() => {
    if (templateDialogOpen) void loadTemplateManifest();
  }, [loadTemplateManifest, templateDialogOpen]);
  useEffect(() => {
    let active = true;
    setModelsLoading(true);
    setModelsError("");
    void enterpriseApi
      .llmModels()
      .then((items) => {
        if (!active) return;
        setModels(
          (Array.isArray(items) ? items : []).filter(
            (item) => item?.enabled !== false,
          ),
        );
      })
      .catch((error) => {
        if (active)
          setModelsError(
            errorMessage(error, t("projectWorkspacePage.errors.requestFailed")),
          );
      })
      .finally(() => {
        if (active) setModelsLoading(false);
      });
    return () => {
      active = false;
    };
  }, [t]);
  const modelOptions = [
    {
      value: "default",
      label: t("projectWorkspacePage.policies.defaultTenantModel"),
    },
    ...models.map((item) => ({
      value: item.id,
      label: item.label || `${item.provider} · ${item.model}`,
    })),
  ];
  useEffect(() => {
    if (modelsLoading || modelsError || model === "default") return;
    if (!models.some((item) => item.id === model)) setModel("default");
  }, [model, models, modelsError, modelsLoading]);
  const save = () => {
    if (!canManageSettings) return;
    void runAction(
      "save-policies",
      () =>
        projectsApi.updateSettings(projectId, {
          runtime: { model, max_parallel_runs: Number(parallel) },
          policies: {
            ...governance,
            max_a2a_wakes: Number(a2aLimit),
            loop_guard: loopGuard,
          },
        }),
      t("projectWorkspacePage.policies.feedback.saved"),
    );
  };
  const publishTemplate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const name = templateName.trim();
    if (!name) {
      setTemplateNameError(t("projectTemplatePublish.nameRequired"));
      return;
    }
    if (!isOwner || publishingTemplate) return;

    setPublishingTemplate(true);
    setTemplateNameError("");
    try {
      await projectsApi.createTemplateFromProject(projectId, {
        name,
        is_published: true,
        included_skill_binding_ids: includedTemplateSkillIds,
      });
      toast.success(t("projectTemplatePublish.success"));
      setTemplateDialogOpen(false);
    } catch (error) {
      toast.error(
        errorMessage(error, t("projectWorkspacePage.errors.requestFailed")),
      );
    } finally {
      setPublishingTemplate(false);
    }
  };
  return (
    <>
      <SectionHeading
        eyebrow={t("projectWorkspacePage.policies.eyebrow")}
        title={t("projectWorkspaceNav.tabs.policies")}
        description=""
        actions={
          <>
            {isOwner && (
              <Button
                variant="secondary"
                onClick={() => {
                  setTemplateName(project.name);
                  setTemplateNameError("");
                  setTemplateDialogOpen(true);
                }}
              >
                <IconSparkles size={16} />
                {t("projectTemplatePublish.action")}
              </Button>
            )}
            {canManageSettings && (
              <Button
                variant="primary"
                onClick={save}
                disabled={busyAction === "save-policies"}
              >
                {busyAction === "save-policies" ? (
                  <IconLoader2
                    className="project-workspace__spinner"
                    size={16}
                  />
                ) : (
                  <IconDeviceFloppy size={16} />
                )}
                {t("projectWorkspacePage.policies.actions.save")}
              </Button>
            )}
          </>
        }
      />
      <ProjectVisibilitySettings
        projectId={projectId}
        project={project}
        onReload={onReload}
      />
      <section className="project-workspace__runtime-settings">
        <header className="project-workspace__settings-section-heading">
          <div>
            <h3>{t("projectWorkspacePage.policies.runtimeTitle")}</h3>
            <p>{t("projectWorkspacePage.policies.runtimeDescription")}</p>
          </div>
        </header>
        <div className="project-workspace__settings-grid">
        <ProjectField
          label={t("projectWorkspacePage.policies.fields.defaultModel")}
          hint={
            modelsError
              ? t("projectWorkspacePage.policies.modelsLoadFailed", {
                  error: modelsError,
                })
              : t("projectWorkspacePage.policies.fields.defaultModelHint")
          }
        >
          <ProjectSelect
            value={model}
            options={modelOptions}
            onChange={setModel}
            ariaLabel={t("projectWorkspacePage.policies.fields.defaultModel")}
            disabled={
              !canManageSettings || modelsLoading || Boolean(modelsError)
            }
            placeholder={t(
              modelsLoading
                ? "projectWorkspacePage.policies.loadingModels"
                : "projectWorkspacePage.policies.selectModel",
            )}
          />
        </ProjectField>
        <ProjectField
          label={t("projectWorkspacePage.policies.fields.maxParallelRuns")}
          labelFor="project-policy-parallel"
        >
          <TextInput
            id="project-policy-parallel"
            type="number"
            min="1"
            max="32"
            value={parallel}
            onChange={(event) => setParallel(event.target.value)}
            disabled={!canManageSettings}
          />
        </ProjectField>
        <ProjectField
          label={t("projectWorkspacePage.policies.fields.maxA2AWakes")}
          labelFor="project-policy-a2a-limit"
        >
          <TextInput
            id="project-policy-a2a-limit"
            type="number"
            min="1"
            max="100"
            value={a2aLimit}
            onChange={(event) => setA2aLimit(event.target.value)}
            disabled={!canManageSettings}
          />
        </ProjectField>
        <ProjectField
          label={t("projectWorkspacePage.policies.fields.loopGuard")}
          hint={t("projectWorkspacePage.policies.fields.loopGuardHint")}
        >
          <div className="project-workspace__toggle-field">
            <span>
              {t(
                loopGuard
                  ? "projectWorkspacePage.policies.fields.enabled"
                  : "projectWorkspacePage.policies.fields.disabled",
              )}
            </span>
            <ToggleSwitch
              checked={loopGuard}
              onChange={setLoopGuard}
              ariaLabel={t("projectWorkspacePage.policies.fields.loopGuard")}
              disabled={!canManageSettings}
            />
          </div>
        </ProjectField>
        </div>
      </section>
      <ProjectDialog
        open={templateDialogOpen}
        onClose={() => {
          if (!publishingTemplate) setTemplateDialogOpen(false);
        }}
        ariaLabel={t("projectTemplatePublish.title")}
        className="project-workspace__git-dialog"
      >
        <form className="project-workspace__modal" onSubmit={publishTemplate}>
          <header>
            <div>
              <span>{t("projectTemplatePublish.eyebrow")}</span>
              <h2>{t("projectTemplatePublish.title")}</h2>
            </div>
            <ProjectIconButton
              aria-label={t("projectTemplatePublish.close")}
              disabled={publishingTemplate}
              onClick={() => setTemplateDialogOpen(false)}
            >
              <IconX size={18} />
            </ProjectIconButton>
          </header>
          <p>{t("projectTemplatePublish.description")}</p>
          <section
            className="project-workspace__template-manifest"
            aria-label={t("projectTemplatePublish.manifest.title")}
          >
            <header>
              <strong>{t("projectTemplatePublish.manifest.title")}</strong>
              {templateManifestLoading && (
                <span>
                  <IconLoader2
                    className="project-workspace__spinner"
                    size={14}
                  />
                  {t("projectTemplatePublish.manifest.loading")}
                </span>
              )}
            </header>
            {templateManifestError ? (
              <div className="project-workspace__template-manifest-error">
                <span>{t("projectTemplatePublish.manifest.loadFailed")}</span>
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => void loadTemplateManifest()}
                >
                  <IconRefresh size={14} />
                  {t("projectTemplatePublish.manifest.retry")}
                </Button>
              </div>
            ) : templateManifest ? (
              <>
                <div className="project-workspace__template-manifest-grid">
                  <span>
                    <strong>
                      {templateManifest.asset_summary.total_file_count}
                    </strong>
                    <small>{t("projectTemplatePublish.manifest.files")}</small>
                  </span>
                  <span>
                    <strong>
                      {templateManifest.asset_summary.digital_employee_count}
                    </strong>
                    <small>
                      {t("projectTemplatePublish.manifest.employees")}
                    </small>
                  </span>
                  <span>
                    <strong>
                      {templateManifest.asset_summary.skill_count}
                    </strong>
                    <small>{t("projectTemplatePublish.manifest.skills")}</small>
                  </span>
                  <span>
                    <strong>
                      {templateManifest.asset_summary.capability_count}
                    </strong>
                    <small>
                      {t("projectTemplatePublish.manifest.platformDependencies")}
                    </small>
                  </span>
                </div>
                <dl className="project-workspace__template-manifest-details">
                  <div>
                    <dt>{t("projectTemplatePublish.manifest.employees")}</dt>
                    <dd>
                      {templateManifest.roles.length
                        ? templateManifest.roles
                            .map((role) => role.name)
                            .join(
                              t(
                                "projectTemplatePublish.manifest.memberSeparator",
                              ),
                            )
                        : t("projectTemplatePublish.manifest.none")}
                    </dd>
                  </div>
                </dl>
                <section className="project-workspace__template-capability-list">
                  <header>
                    <strong>
                      {t("projectTemplatePublish.manifest.capabilityListTitle")}
                    </strong>
                    <span>
                      {t("projectTemplatePublish.manifest.selectedCount", {
                        count: includedTemplateSkillIds.length,
                      })}
                    </span>
                  </header>
                  <p>
                    {t("projectTemplatePublish.manifest.capabilityListHint")}
                  </p>
                  {templateManifest.capabilities.length ? (
                    <div>
                      {templateManifest.capabilities.map((capability, index) => {
                        const capabilityType = capability.type || "tool";
                        const isSkill = capabilityType === "skill";
                        const selectionId =
                          capability.binding_id ||
                          capability.id ||
                          `${capabilityType}-${capability.key || capability.name}-${index}`;
                        const checked = isSkill
                          ? includedTemplateSkillIds.includes(selectionId)
                          : capability.selected !== false;
                        const affectedNames = [
                          ...(capability.affected_members || []).map(
                            (affectedMember) => affectedMember.name,
                          ),
                          capability.member_name || "",
                        ].filter(
                          (name, nameIndex, names) =>
                            Boolean(name) && names.indexOf(name) === nameIndex,
                        );
                        const affectedCount = Math.max(
                          capability.affected_member_count || 0,
                          affectedNames.length,
                        );
                        const availability = [
                          "available",
                          "missing",
                          "restricted",
                        ].includes(capability.availability || "")
                          ? capability.availability
                          : "available";
                        return (
                          <div
                            className="project-workspace__template-capability-row"
                            key={selectionId}
                          >
                            <span className="project-workspace__template-capability-selector">
                              {isSkill ? (
                                <input
                                  type="checkbox"
                                  checked={checked}
                                  disabled={publishingTemplate}
                                  aria-label={t(
                                    "projectTemplatePublish.manifest.selectSkill",
                                    {
                                      name:
                                        capability.name ||
                                        t(
                                          "projectTemplatePublish.manifest.unnamedCapability",
                                        ),
                                    },
                                  )}
                                  onChange={(event) =>
                                    setIncludedTemplateSkillIds((current) =>
                                      event.target.checked
                                        ? [...current, selectionId]
                                        : current.filter(
                                            (id) => id !== selectionId,
                                          ),
                                    )
                                  }
                                />
                              ) : capabilityType === "mcp" ? (
                                <IconCodeDots size={17} />
                              ) : (
                                <IconTool size={17} />
                              )}
                            </span>
                            <span>
                              <strong>
                                {capability.name ||
                                  t(
                                    "projectTemplatePublish.manifest.unnamedCapability",
                                  )}
                              </strong>
                              <small>
                                {t(
                                  `projectTemplatePublish.manifest.types.${capabilityType}`,
                                  {
                                    defaultValue: capabilityType,
                                  },
                                )}
                                {" · "}
                                {isSkill
                                  ? t(
                                      "projectTemplatePublish.manifest.filesAndSize",
                                      {
                                        count: capability.file_count || 0,
                                        size: fileSizeLabel(
                                          capability.size_bytes || 0,
                                        ),
                                      },
                                    )
                                  : t(
                                      "projectTemplatePublish.manifest.platformDependency",
                                    )}
                              </small>
                            </span>
                            <span>
                              <ProjectStatusBadge
                                tone={
                                  isSkill
                                    ? checked
                                      ? "info"
                                      : "neutral"
                                    : availability === "available"
                                      ? "success"
                                      : availability === "restricted"
                                        ? "warning"
                                        : "error"
                                }
                              >
                                {t(
                                  isSkill
                                    ? checked
                                      ? "projectTemplatePublish.manifest.included"
                                      : "projectTemplatePublish.manifest.notIncluded"
                                    : `projectTemplatePublish.manifest.availability.${availability}`,
                                )}
                              </ProjectStatusBadge>
                              <small>
                                {affectedCount
                                  ? t(
                                      "projectTemplatePublish.manifest.affectedMembers",
                                      {
                                        count: affectedCount,
                                        names:
                                          affectedNames.join(
                                            t(
                                              "projectTemplatePublish.manifest.memberSeparator",
                                            ),
                                          ) ||
                                          t(
                                            "projectTemplatePublish.manifest.memberDetailsUnavailable",
                                          ),
                                      },
                                    )
                                  : t(
                                      "projectTemplatePublish.manifest.noAffectedMembers",
                                    )}
                              </small>
                            </span>
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    <div className="project-workspace__template-capability-empty">
                      {t("projectTemplatePublish.manifest.noCapabilities")}
                    </div>
                  )}
                </section>
                <p>{t("projectTemplatePublish.manifest.exclusions")}</p>
              </>
            ) : null}
          </section>
          <ProjectField
            label={t("projectTemplatePublish.name")}
            labelFor="project-template-name"
            error={templateNameError}
            required
          >
            <TextInput
              id="project-template-name"
              value={templateName}
              maxLength={200}
              placeholder={t("projectTemplatePublish.namePlaceholder")}
              disabled={publishingTemplate}
              autoFocus
              onChange={(event) => {
                setTemplateName(event.target.value);
                if (templateNameError) setTemplateNameError("");
              }}
            />
          </ProjectField>
          <footer>
            <Button
              type="button"
              variant="secondary"
              disabled={publishingTemplate}
              onClick={() => setTemplateDialogOpen(false)}
            >
              {t("projectTemplatePublish.cancel")}
            </Button>
            <Button
              type="submit"
              variant="primary"
              disabled={
                publishingTemplate ||
                templateManifestLoading ||
                !templateManifest ||
                Boolean(templateManifestError) ||
                !templateName.trim()
              }
            >
              {publishingTemplate ? (
                <IconLoader2 className="project-workspace__spinner" size={16} />
              ) : (
                <IconSparkles size={16} />
              )}
              {t("projectTemplatePublish.publish")}
            </Button>
          </footer>
        </form>
      </ProjectDialog>
    </>
  );
}

function GitRepositoryControls({
  projectId,
  project,
  repository,
  commits,
  onReload,
}: {
  projectId: string;
  project: ProjectSummary;
  repository: RecordValue;
  commits: RecordValue[];
  onReload: () => Promise<void>;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const isOwner = project.access_role === "owner";
  const [remotes, setRemotes] = useState<Array<{ name: string; url: string }>>(
    [],
  );
  const [remotesLoading, setRemotesLoading] = useState(false);
  const [remoteError, setRemoteError] = useState("");
  const [remoteName, setRemoteName] = useState("");
  const [remoteUrl, setRemoteUrl] = useState("");
  const [editingRemote, setEditingRemote] = useState("");
  const [cloneUrl, setCloneUrl] = useState("");
  const [cloneBranch, setCloneBranch] = useState("");
  const [cloneConfirmOpen, setCloneConfirmOpen] = useState(false);
  const [busy, setBusy] = useState("");

  const loadRemotes = useCallback(async () => {
    if (!isOwner) {
      setRemotes([]);
      setRemoteError("");
      return;
    }
    setRemotesLoading(true);
    setRemoteError("");
    try {
      setRemotes(await projectsApi.listGitRemotes(projectId));
    } catch (error) {
      setRemoteError(
        errorMessage(error, t("projectWorkspacePage.errors.requestFailed")),
      );
    } finally {
      setRemotesLoading(false);
    }
  }, [isOwner, projectId, t]);
  useEffect(() => {
    void loadRemotes();
  }, [loadRemotes]);

  const repositoryFiles = Array.isArray(repository.files)
    ? repository.files.map(String).sort()
    : [];
  const source = text(repository, "source") === "cloned" ? "cloned" : "managed";
  const initializationOnly =
    source !== "cloned" &&
    commits.length === 1 &&
    text(commits[0], "message", "subject", "title") ===
      "Initialize AI-native project" &&
    repositoryFiles.length === 2 &&
    repositoryFiles[0] === "PROJECT.json" &&
    repositoryFiles[1] === "README.md";
  const canClone =
    isOwner && project.status === "planning" && initializationOnly;
  const cloneUnavailableReason = !isOwner
    ? t("projectTerminology.workspace.repositoryOwnerRequired")
    : project.status !== "planning"
      ? t("projectWorkspacePage.repository.notPlanning")
      : !initializationOnly
        ? t("projectWorkspacePage.repository.hasDeliverables")
        : "";

  const resetRemoteDraft = () => {
    setEditingRemote("");
    setRemoteName("");
    setRemoteUrl("");
  };
  const saveRemote = async (event: FormEvent) => {
    event.preventDefault();
    if (!isOwner || !remoteName.trim() || !remoteUrl.trim()) return;
    setBusy("remote-save");
    setRemoteError("");
    try {
      await projectsApi.putGitRemote(
        projectId,
        remoteName.trim(),
        remoteUrl.trim(),
      );
      toast.success(
        t(
          editingRemote
            ? "projectWorkspacePage.repository.feedback.remoteUpdated"
            : "projectWorkspacePage.repository.feedback.remoteAdded",
          { name: remoteName.trim() },
        ),
      );
      resetRemoteDraft();
      await loadRemotes();
      await onReload();
    } catch (error) {
      const message = errorMessage(
        error,
        t("projectWorkspacePage.errors.requestFailed"),
      );
      setRemoteError(message);
      toast.error(message);
    } finally {
      setBusy("");
    }
  };
  const deleteRemote = async (name: string) => {
    if (!isOwner) return;
    setBusy(`remote-delete-${name}`);
    setRemoteError("");
    try {
      await projectsApi.deleteGitRemote(projectId, name);
      toast.success(
        t("projectWorkspacePage.repository.feedback.remoteDeleted", { name }),
      );
      if (editingRemote === name) resetRemoteDraft();
      await loadRemotes();
      await onReload();
    } catch (error) {
      const message = errorMessage(
        error,
        t("projectWorkspacePage.errors.requestFailed"),
      );
      setRemoteError(message);
      toast.error(message);
    } finally {
      setBusy("");
    }
  };
  const cloneRepository = async (event: FormEvent) => {
    event.preventDefault();
    if (!canClone || !cloneUrl.trim()) return;
    setBusy("clone");
    try {
      await projectsApi.cloneGitRepository(projectId, {
        url: cloneUrl.trim(),
        branch: cloneBranch.trim() || undefined,
      });
      toast.success(t("projectWorkspacePage.repository.feedback.imported"));
      setCloneConfirmOpen(false);
      setCloneUrl("");
      setCloneBranch("");
      await loadRemotes();
      await onReload();
    } catch (error) {
      toast.error(
        errorMessage(error, t("projectWorkspacePage.errors.requestFailed")),
      );
    } finally {
      setBusy("");
    }
  };

  return (
    <section className="project-workspace__repository-control">
      <header>
        <div>
          <h3>{t("projectWorkspacePage.repository.title")}</h3>
        </div>
      </header>
      <div className="project-workspace__repository-summary">
        <article>
          <span>{t("projectWorkspacePage.repository.source")}</span>
          <strong>
            {t(
              source === "cloned"
                ? "projectWorkspacePage.repository.remoteRepository"
                : "projectWorkspacePage.repository.projectRepository",
            )}
          </strong>
          <small>
            {t(
              source === "cloned"
                ? "projectWorkspacePage.repository.remoteSourceHint"
                : "projectWorkspacePage.repository.projectSourceHint",
            )}
          </small>
        </article>
        <article>
          <span>{t("projectWorkspacePage.repository.defaultBranch")}</span>
          <strong>
            {text(repository, "default_branch") ||
              text(repository, "branch") ||
              t("projectWorkspacePage.notRecorded")}
          </strong>
          <small>
            HEAD <code>{text(repository, "head").slice(0, 12) || "—"}</code>
          </small>
        </article>
        <article>
          <span>{t("projectWorkspacePage.repository.remoteCount")}</span>
          <strong>
            {isOwner
              ? remotes.length
              : t("projectTerminology.workspace.repositoryOwnerVisibility")}
          </strong>
          <small>{t("projectWorkspacePage.repository.standardGitHint")}</small>
        </article>
      </div>
      <div className="project-workspace__repository-grid">
        <section>
          <header>
            <div>
              <span>REMOTES</span>
              <h4>{t("projectWorkspacePage.repository.remoteList")}</h4>
            </div>
            <ProjectCountBadge>
              {isOwner ? remotes.length : 0}
            </ProjectCountBadge>
          </header>
          {isOwner ? (
            remotes.length ? (
              <ProjectDataTable className="project-workspace__remote-table">
                <ProjectDataTableHead>
                  <ProjectDataTableRow>
                    <ProjectDataTableHeader>
                      {t("projectWorkspacePage.repository.columns.name")}
                    </ProjectDataTableHeader>
                    <ProjectDataTableHeader>
                      {t("projectWorkspacePage.repository.columns.url")}
                    </ProjectDataTableHeader>
                    <ProjectDataTableHeader>
                      {t("projectWorkspacePage.repository.columns.actions")}
                    </ProjectDataTableHeader>
                  </ProjectDataTableRow>
                </ProjectDataTableHead>
                <ProjectDataTableBody>
                  {remotes.map((remote) => (
                    <ProjectDataTableRow key={remote.name}>
                      <ProjectDataTableCell>
                        <code>{remote.name}</code>
                      </ProjectDataTableCell>
                      <ProjectDataTableCell>
                        <code title={remote.url}>{remote.url}</code>
                      </ProjectDataTableCell>
                      <ProjectDataTableCell>
                        <div className="project-workspace__remote-actions">
                          <Button
                            variant="ghost"
                            disabled={Boolean(busy)}
                            onClick={() => {
                              setEditingRemote(remote.name);
                              setRemoteName(remote.name);
                              setRemoteUrl(remote.url);
                            }}
                          >
                            {t("common.edit")}
                          </Button>
                          <ProjectIconButton
                            aria-label={t(
                              "projectWorkspacePage.repository.actions.deleteRemote",
                              { name: remote.name },
                            )}
                            disabled={Boolean(busy)}
                            onClick={() => void deleteRemote(remote.name)}
                          >
                            {busy === `remote-delete-${remote.name}` ? (
                              <IconLoader2
                                className="project-workspace__spinner"
                                size={15}
                              />
                            ) : (
                              <IconTrash size={15} />
                            )}
                          </ProjectIconButton>
                        </div>
                      </ProjectDataTableCell>
                    </ProjectDataTableRow>
                  ))}
                </ProjectDataTableBody>
              </ProjectDataTable>
            ) : (
              <ProjectEmptyState
                title={t(
                  remotesLoading
                    ? "projectWorkspacePage.repository.loadingRemotes"
                    : "projectWorkspacePage.repository.noRemotes",
                )}
                description={t(
                  "projectWorkspacePage.repository.noRemotesDescription",
                )}
              />
            )
          ) : (
            <ProjectEmptyState
              icon={<IconLock size={20} />}
              title={t(
                "projectTerminology.workspace.repositoryOwnerManagement",
              )}
              description={t(
                "projectWorkspacePage.repository.memberHistoryHint",
              )}
            />
          )}
          {isOwner && (
            <form
              className="project-workspace__remote-form"
              onSubmit={saveRemote}
            >
              <ProjectField
                label={t("projectWorkspacePage.repository.fields.remoteName")}
                labelFor="project-remote-name"
                required
              >
                <TextInput
                  id="project-remote-name"
                  value={remoteName}
                  onChange={(event) => setRemoteName(event.target.value)}
                  placeholder="origin"
                  pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
                  disabled={Boolean(editingRemote)}
                  required
                />
              </ProjectField>
              <ProjectField
                label={t("projectWorkspacePage.repository.fields.remoteUrl")}
                labelFor="project-remote-url"
                required
              >
                <TextInput
                  id="project-remote-url"
                  value={remoteUrl}
                  onChange={(event) => setRemoteUrl(event.target.value)}
                  placeholder="https://git.example.com/team/project.git"
                  required
                />
              </ProjectField>
              <footer>
                {editingRemote && (
                  <Button
                    type="button"
                    variant="ghost"
                    onClick={resetRemoteDraft}
                  >
                    {t("projectWorkspacePage.repository.actions.cancelEdit")}
                  </Button>
                )}
                <Button
                  type="submit"
                  variant="secondary"
                  disabled={
                    !remoteName.trim() || !remoteUrl.trim() || Boolean(busy)
                  }
                >
                  {busy === "remote-save" ? (
                    <IconLoader2
                      className="project-workspace__spinner"
                      size={15}
                    />
                  ) : editingRemote ? (
                    <IconDeviceFloppy size={15} />
                  ) : (
                    <IconPlus size={15} />
                  )}
                  {t(
                    editingRemote
                      ? "projectWorkspacePage.repository.actions.saveRemote"
                      : "projectWorkspacePage.repository.actions.addRemote",
                  )}
                </Button>
              </footer>
            </form>
          )}
        </section>
        <section>
          <header>
            <div>
              <span>INITIAL SOURCE</span>
              <h4>{t("projectWorkspacePage.repository.initializeRemote")}</h4>
            </div>
            <ProjectStatusBadge tone={canClone ? "success" : "neutral"}>
              {t(
                canClone
                  ? "projectWorkspacePage.repository.cloneAvailable"
                  : "projectWorkspacePage.repository.cloneUnavailable",
              )}
            </ProjectStatusBadge>
          </header>
          <div className="project-workspace__clone-form">
            <ProjectField
              label={t("projectWorkspacePage.repository.fields.repositoryUrl")}
              labelFor="project-clone-url"
              required
            >
              <TextInput
                id="project-clone-url"
                value={cloneUrl}
                onChange={(event) => setCloneUrl(event.target.value)}
                placeholder={t(
                  "projectWorkspacePage.repository.fields.urlPlaceholder",
                )}
                disabled={!canClone || Boolean(busy)}
              />
            </ProjectField>
            <ProjectField
              label={t("projectWorkspacePage.repository.fields.optionalBranch")}
              labelFor="project-clone-branch"
            >
              <TextInput
                id="project-clone-branch"
                value={cloneBranch}
                onChange={(event) => setCloneBranch(event.target.value)}
                placeholder={t(
                  "projectWorkspacePage.repository.fields.branchPlaceholder",
                )}
                disabled={!canClone || Boolean(busy)}
              />
            </ProjectField>
            <div className="project-workspace__clone-note">
              <IconShieldCheck size={17} />
              <p>
                {t("projectTerminology.workspace.repositoryCredentialHint")}
              </p>
            </div>
            {cloneUnavailableReason && <small>{cloneUnavailableReason}</small>}
            <Button
              variant="danger"
              disabled={!canClone || !cloneUrl.trim() || Boolean(busy)}
              onClick={() => setCloneConfirmOpen(true)}
            >
              <IconBrandGit size={16} />
              {t("projectWorkspacePage.repository.actions.confirmSource")}
            </Button>
          </div>
        </section>
      </div>
      {remoteError && (
        <div className="project-workspace__repository-error" role="alert">
          <IconAlertTriangle size={16} />
          <span>{remoteError}</span>
        </div>
      )}
      <ProjectDialog
        open={cloneConfirmOpen}
        onClose={() => {
          if (busy !== "clone") setCloneConfirmOpen(false);
        }}
        ariaLabel={t("projectWorkspacePage.repository.confirmAria")}
        className="project-workspace__git-dialog"
      >
        <form className="project-workspace__modal" onSubmit={cloneRepository}>
          <header>
            <div>
              <span>IMPORT REPOSITORY</span>
              <h2>{t("projectWorkspacePage.repository.importTitle")}</h2>
            </div>
            <ProjectIconButton
              aria-label={t("common.close")}
              disabled={busy === "clone"}
              onClick={() => setCloneConfirmOpen(false)}
            >
              <IconX size={18} />
            </ProjectIconButton>
          </header>
          <p>{t("projectWorkspacePage.repository.importDescription")}</p>
          <dl className="project-workspace__definition-list">
            <div>
              <dt>{t("projectWorkspacePage.repository.remote")}</dt>
              <dd>
                <code>{cloneUrl}</code>
              </dd>
            </div>
            <div>
              <dt>{t("projectWorkspacePage.repository.branch")}</dt>
              <dd>
                <code>
                  {cloneBranch ||
                    t("projectWorkspacePage.repository.remoteDefaultBranch")}
                </code>
              </dd>
            </div>
          </dl>
          <div className="project-workspace__safe-note">
            <IconLock size={16} />
            <span>
              {t("projectWorkspacePage.repository.credentialsWarning")}
            </span>
          </div>
          <footer>
            <Button
              type="button"
              variant="secondary"
              disabled={busy === "clone"}
              onClick={() => setCloneConfirmOpen(false)}
            >
              {t("common.cancel")}
            </Button>
            <Button type="submit" variant="primary" disabled={busy === "clone"}>
              {busy === "clone" ? (
                <IconLoader2 className="project-workspace__spinner" size={16} />
              ) : (
                <IconBrandGit size={16} />
              )}
              {t("projectWorkspacePage.repository.actions.import")}
            </Button>
          </footer>
        </form>
      </ProjectDialog>
    </section>
  );
}

function GitPanel({
  projectId,
  project,
  repository,
  commits,
  events,
  selected,
  onSelect,
  onDialog,
  onOpenSession,
  onReload,
}: {
  projectId: string;
  project: ProjectSummary;
  repository: RecordValue;
  commits: RecordValue[];
  events: RecordValue[];
  selected: RecordValue | null;
  onSelect: (commit: RecordValue) => void;
  onDialog: (mode: "restore" | "branch") => void;
  onOpenSession: OpenSession;
  onReload: () => Promise<void>;
}) {
  const { t } = useTranslation();
  const selectedId = text(
    selected || {},
    "commit",
    "hash",
    "commit_hash",
    "id",
  );
  const { pageItems: visibleCommits, pagination } = useWorkspacePagination(
    commits,
    "gitCommits",
    20,
  );
  const eventsForCommit = (commit: RecordValue) => {
    const commitId = text(commit, "commit", "hash", "commit_hash", "id");
    return events.filter(
      (event) =>
        traceValue(
          traceRecords(event),
          "commit_hash",
          "commit",
          "hash",
          "from_commit",
          "source_commit",
        ) === commitId,
    );
  };
  const responsibleAgentForCommit = (commit: RecordValue) => {
    for (const event of eventsForCommit(commit)) {
      const responsibleAgent = traceValue(
        traceRecords(event),
        "agent_name",
        "actor_name",
        "member_name",
        "name_snapshot",
      );
      if (responsibleAgent) return responsibleAgent;
    }

    const author = text(commit, "author", "author_name", "actor_name").trim();
    const normalizedAuthor = author.toLocaleLowerCase();
    return author &&
      normalizedAuthor !== "clawith" &&
      normalizedAuthor !== "clawith project"
      ? author
      : t("projectGit.projectMember");
  };
  const refsForCommit = (commit: RecordValue, index: number) => {
    const refs = new Set<string>();
    if (index === 0) refs.add("HEAD");
    [commit.branch, commit.branches, commit.refs].forEach((value) => {
      if (Array.isArray(value))
        value.forEach((entry) => {
          if (entry) refs.add(String(entry));
        });
      else if (typeof value === "string" && value.trim())
        value.split(",").forEach((entry) => refs.add(entry.trim()));
    });
    eventsForCommit(commit).forEach((event) => {
      if (text(event, "event_type", "type") !== "git.branch.created") return;
      const branch = traceValue(traceRecords(event), "branch", "branch_name");
      if (branch) refs.add(branch);
    });
    return [...refs];
  };
  const isMilestone = (commit: RecordValue) =>
    bool(commit, "milestone") ||
    eventsForCommit(commit).some(
      (event) =>
        text(event, "event_type", "type") === "git.milestone.created" ||
        traceRecords(event).some((record) => record.milestone === true),
    );
  const selectedIndex = Math.max(
    0,
    commits.findIndex(
      (commit) =>
        text(commit, "commit", "hash", "commit_hash", "id") === selectedId,
    ),
  );
  const selectedRefs = selected ? refsForCommit(selected, selectedIndex) : [];
  const selectedEvents = selected ? eventsForCommit(selected) : [];
  const linkedEvent =
    selectedEvents.find((event) => sessionIdOf(event)) || selectedEvents[0];
  const sessionSource = sessionIdOf(selected || {})
    ? selected || {}
    : linkedEvent || selected || {};
  return (
    <>
      <SectionHeading
        eyebrow="GIT / HISTORY"
        title={t("projectGit.title")}
        description=""
      />
      <GitRepositoryControls
        projectId={projectId}
        project={project}
        repository={repository}
        commits={commits}
        onReload={onReload}
      />
      {commits.length ? (
        <div className="project-workspace__git-layout">
          <section
            className="project-workspace__card project-workspace__git-log"
            aria-label={t("projectGit.historyAria")}
          >
            <header>
              <div>
                <h3>{t("projectGit.history")}</h3>
              </div>
              <ProjectCountBadge>{commits.length}</ProjectCountBadge>
            </header>
            <div className="project-workspace__git-log-head" aria-hidden="true">
              <span>{t("projectGit.commit")}</span>
              <span>{t("projectGit.message")}</span>
              <span>{t("projectGit.author")}</span>
              <span>{t("projectGit.time")}</span>
              <span>{t("projectGit.references")}</span>
            </div>
            <ol>
              {visibleCommits.map((commit) => {
                const index = commits.indexOf(commit);
                const hash = text(
                  commit,
                  "commit",
                  "hash",
                  "commit_hash",
                  "id",
                );
                const refs = refsForCommit(commit, index);
                const milestone = isMilestone(commit);
                return (
                  <li key={hash}>
                    <Button
                      type="button"
                      variant="ghost"
                      className={hash === selectedId ? "is-active" : ""}
                      aria-pressed={hash === selectedId}
                      onClick={() => onSelect(commit)}
                    >
                      <code title={hash}>
                        {text(commit, "short_commit") || hash.slice(0, 12)}
                      </code>
                      <span className="project-workspace__git-log-message">
                        <strong>
                          {text(commit, "message", "subject", "title") ||
                            t("projectGit.unnamedCommit")}
                        </strong>
                        <small>{hash}</small>
                      </span>
                      <span>{responsibleAgentForCommit(commit)}</span>
                      <time dateTime={text(commit, "created_at", "timestamp")}>
                        {dateLabel(commit.created_at || commit.timestamp)}
                      </time>
                      <span className="project-workspace__git-refs">
                        {refs.map((ref) => (
                          <ProjectStatusBadge
                            key={ref}
                            tone={ref === "HEAD" ? "success" : "info"}
                          >
                            {ref}
                          </ProjectStatusBadge>
                        ))}
                        {milestone && (
                          <ProjectStatusBadge tone="warning">
                            {t("projectGit.milestone")}
                          </ProjectStatusBadge>
                        )}
                        {!refs.length && !milestone && <small>—</small>}
                      </span>
                    </Button>
                  </li>
                );
              })}
            </ol>
            {pagination}
          </section>
          <aside className="project-workspace__card project-workspace__commit-detail">
            <header>
              <div>
                <span>{t("projectGit.detail")}</span>
                <h3>
                  {text(selected || {}, "message", "subject", "title") ||
                    t("projectGit.unnamedCommit")}
                </h3>
              </div>
            </header>
            <div className="project-workspace__repro">
              <IconCircleCheck size={18} />
              <span>
                <strong>{t("projectGit.recoverable")}</strong>
              </span>
            </div>
            <dl className="project-workspace__definition-list">
              <div>
                <dt>{t("projectGit.commit")}</dt>
                <dd>
                  <code>{selectedId || "—"}</code>
                </dd>
              </div>
              <div>
                <dt>{t("projectGit.author")}</dt>
                <dd>{responsibleAgentForCommit(selected || {})}</dd>
              </div>
              <div>
                <dt>{t("projectGit.time")}</dt>
                <dd>
                  {dateLabel(selected?.created_at || selected?.timestamp)}
                </dd>
              </div>
              <div>
                <dt>{t("projectGit.references")}</dt>
                <dd className="project-workspace__git-detail-refs">
                  {selectedRefs.map((ref) => (
                    <ProjectStatusBadge
                      key={ref}
                      tone={ref === "HEAD" ? "success" : "info"}
                    >
                      {ref}
                    </ProjectStatusBadge>
                  ))}
                  {selected && isMilestone(selected) && (
                    <ProjectStatusBadge tone="warning">
                      {t("projectGit.milestone")}
                    </ProjectStatusBadge>
                  )}
                  {!selectedRefs.length &&
                    !(selected && isMilestone(selected)) &&
                    "—"}
                </dd>
              </div>
              <div>
                <dt>{t("projectGit.relatedRun")}</dt>
                <dd>
                  {traceValue(
                    traceRecords(linkedEvent || selected || {}),
                    "run_id",
                  ) || "—"}
                </dd>
              </div>
              <div>
                <dt>{t("projectGit.workItem")}</dt>
                <dd>
                  {traceValue(
                    traceRecords(linkedEvent || selected || {}),
                    "work_item_id",
                  ) || "—"}
                </dd>
              </div>
              <div>
                <dt>{t("projectGit.conversation")}</dt>
                <dd>
                  {sessionIdOf(sessionSource) || "—"}
                  <SessionButton
                    source={sessionSource}
                    onOpen={onOpenSession}
                  />
                </dd>
              </div>
              <div>
                <dt>{t("projectGit.changedFiles")}</dt>
                <dd>
                  {text(selected || {}, "file_count", "files_changed") || "—"}
                </dd>
              </div>
            </dl>
            {project.access_role === "owner" && (
              <div className="project-workspace__git-actions">
                <Button variant="secondary" onClick={() => onDialog("branch")}>
                  <IconGitBranch size={16} />
                  {t("projectGit.createBranch")}
                </Button>
                <Button variant="danger" onClick={() => onDialog("restore")}>
                  <IconRestore size={16} />
                  {t("projectGit.restoreVersion")}
                </Button>
              </div>
            )}
          </aside>
        </div>
      ) : (
        <EmptyState
          icon={<IconBrandGit size={22} />}
          title={t("projectGit.emptyTitle")}
          description={t("projectGit.emptyDescription")}
        />
      )}
    </>
  );
}

function AuditPanel({
  events,
  members,
  onRefresh,
  onOpenSession,
}: {
  events: RecordValue[];
  members: RecordValue[];
  onRefresh: () => Promise<void>;
  onOpenSession: OpenSession;
}) {
  const { t } = useTranslation();
  const { get, update } = useContext(WorkspaceNavigationContext);
  const selectedEventId = get("auditEvent");
  const query = get("auditQ");
  const scope = get("auditScope");
  const actor = get("auditActor");
  const kind = get("auditType");
  const memberNames = useMemo(
    () =>
      new Map(
        members.map((member) => [
          text(member, "agent_id"),
          text(member, "name_snapshot", "agent_name", "name") ||
            t("projectAudit.projectAgent"),
        ]),
      ),
    [members, t],
  );
  const actorLabel = useCallback(
    (event: RecordValue) => {
      const agentId = text(event, "actor_agent_id");
      if (agentId)
        return memberNames.get(agentId) || t("projectAudit.projectAgent");
      if (text(event, "actor_user_id")) return t("projectAudit.projectUser");
      return t("projectAudit.projectSystem");
    },
    [memberNames, t],
  );
  const eventLabel = useCallback(
    (event: RecordValue) => {
      const code = text(event, "type", "event_type");
      return t(`projectAudit.events.${code}`, {
        defaultValue: t("projectAudit.eventFallback"),
      });
    },
    [t],
  );
  const actors = useMemo(
    () => Array.from(new Set(events.map(actorLabel))),
    [actorLabel, events],
  );
  const kinds = useMemo(
    () =>
      Array.from(
        new Set(
          events
            .map((event) => text(event, "type", "event_type"))
            .filter(Boolean),
        ),
      ),
    [events],
  );
  const visible = events.filter(
    (event) =>
      (!selectedEventId || text(event, "id", "event_id") === selectedEventId) &&
      (!scope ||
        (scope === "a2a" && isProjectA2ARecord(event)) ||
        (scope === "risks" && isProjectRiskEvent(event))) &&
      (!query ||
        `${JSON.stringify(event)} ${eventLabel(event)} ${actorLabel(event)}`
          .toLowerCase()
          .includes(query.toLowerCase())) &&
      (!actor || actorLabel(event) === actor) &&
      (!kind || text(event, "type", "event_type") === kind),
  );
  const { pageItems: visibleEvents, pagination } = useWorkspacePagination(
    visible,
    "auditEvents",
    20,
  );
  return (
    <section className="project-workspace__audit-shell">
      <div className="project-workspace__audit-filters">
        <SearchInput
          value={query}
          onChange={(event) =>
            update(
              {
                auditEvent: undefined,
                auditQ: event.target.value || undefined,
                auditEventsPage: undefined,
              },
              { replace: true },
            )
          }
          placeholder={t("projectWorkspacePage.audit.searchPlaceholder")}
          aria-label={t("projectWorkspacePage.audit.searchAria")}
        />
        <ProjectSelect
          value={actor}
          options={actors.map((value) => ({ value, label: value }))}
          onChange={(value) =>
            update({
              auditActor: value || undefined,
              auditEventsPage: undefined,
            })
          }
          ariaLabel={t("projectAudit.filterActor")}
          placeholder={t("projectAudit.allActors")}
        />
        <ProjectSelect
          value={kind}
          options={kinds.map((value) => ({
            value,
            label: eventLabel({ event_type: value }),
          }))}
          onChange={(value) =>
            update({
              auditType: value || undefined,
              auditEventsPage: undefined,
            })
          }
          ariaLabel={t("projectWorkspacePage.audit.typeFilterAria")}
          placeholder={t("projectWorkspacePage.audit.allTypes")}
        />
        <Button
          variant="ghost"
          onClick={() =>
            update({
              auditEvent: undefined,
              auditQ: undefined,
              auditScope: undefined,
              auditActor: undefined,
              auditType: undefined,
              auditEventsPage: undefined,
            })
          }
        >
          <IconFilter size={15} />
          {t("projectWorkspacePage.audit.clearFilters")}
        </Button>
        <Button variant="secondary" onClick={() => void onRefresh()}>
          <IconRefresh size={16} />
          {t("projectWorkspacePage.audit.refresh")}
        </Button>
      </div>

      {visible.length ? (
        <>
          <div className="project-workspace__audit-results">
            <ProjectDataTable className="project-workspace__audit-table">
              <ProjectDataTableHead>
                <ProjectDataTableRow>
                  <ProjectDataTableHeader>
                    {t("projectAudit.timeColumn")}
                  </ProjectDataTableHeader>
                  <ProjectDataTableHeader>
                    {t("projectAudit.actorColumn")}
                  </ProjectDataTableHeader>
                  <ProjectDataTableHeader>
                    {t("projectAudit.eventColumn")}
                  </ProjectDataTableHeader>
                  <ProjectDataTableHeader>
                    {t("projectAudit.detailsColumn")}
                  </ProjectDataTableHeader>
                  <ProjectDataTableHeader>
                    {t("projectAudit.relationsColumn")}
                  </ProjectDataTableHeader>
                </ProjectDataTableRow>
              </ProjectDataTableHead>
              <ProjectDataTableBody>
                {visibleEvents.map((event) => {
                  const eventCode = text(event, "type", "event_type");
                  const eventId = text(event, "id", "event_id");
                  const eventRecords = traceRecords(event);
                  const relationLabels = [
                    closestTraceValue(
                      eventRecords,
                      "work_item_id",
                      "project_work_item_id",
                    )
                      ? t("projectAudit.relatedWorkItem")
                      : "",
                    closestTraceValue(eventRecords, "run_id", "project_run_id")
                      ? t("projectAudit.relatedRun")
                      : "",
                    closestTraceValue(eventRecords, "commit_hash", "commit")
                      ? t("projectAudit.relatedCodeChange")
                      : "",
                  ].filter(Boolean);
                  const sessionIntent = inferredSessionIntent(event);
                  const hasSession = Boolean(
                    sessionRouteOf(event, sessionIntent),
                  );
                  const eventContent = redactInternalUuids(
                    text(event, "message", "summary", "detail"),
                    t("projectAudit.internalReferenceHidden"),
                  );

                  return (
                    <ProjectDataTableRow
                      key={
                        eventId || `${text(event, "created_at")}-${eventCode}`
                      }
                    >
                      <ProjectDataTableCell>
                        {dateLabel(event.created_at)}
                      </ProjectDataTableCell>
                      <ProjectDataTableCell>
                        {actorLabel(event)}
                      </ProjectDataTableCell>
                      <ProjectDataTableCell>
                        <span>{eventLabel(event)}</span>
                      </ProjectDataTableCell>
                      <ProjectDataTableCell>
                        <ProjectEventContent
                          eventType={eventCode}
                          content={eventContent}
                          maxChars={180}
                        />
                      </ProjectDataTableCell>
                      <ProjectDataTableCell>
                        <div className="project-workspace__audit-relations">
                          {relationLabels.map((label) => (
                            <span
                              className="project-workspace__audit-relation-label"
                              key={label}
                            >
                              {label}
                            </span>
                          ))}
                          {!relationLabels.length && !hasSession ? (
                            <span className="project-workspace__audit-relation-label">
                              {t("projectAudit.projectScope")}
                            </span>
                          ) : null}
                          <SessionButton
                            source={event}
                            onOpen={onOpenSession}
                            intent={sessionIntent}
                            label={t("projectAudit.viewSession")}
                          />
                        </div>
                      </ProjectDataTableCell>
                    </ProjectDataTableRow>
                  );
                })}
              </ProjectDataTableBody>
            </ProjectDataTable>
          </div>
          {pagination}
        </>
      ) : (
        <EmptyState
          icon={<IconHistory size={22} />}
          title={
            events.length
              ? t("projectWorkspacePage.audit.noMatchesTitle")
              : t("projectWorkspacePage.audit.emptyTitle")
          }
          description={
            events.length
              ? t("projectWorkspacePage.audit.noMatchesDescription")
              : t("projectWorkspacePage.audit.emptyDescription")
          }
        />
      )}
    </section>
  );
}

function GitActionDialog({
  projectId,
  mode,
  commit,
  busy,
  onClose,
  runAction,
}: {
  projectId: string;
  mode: "restore" | "branch";
  commit: RecordValue;
  busy: string;
  onClose: () => void;
  runAction: (
    key: string,
    action: () => Promise<unknown>,
    success: string,
  ) => Promise<boolean>;
}) {
  const { t } = useTranslation();
  const hash = text(commit, "commit", "hash", "commit_hash", "id");
  const [branchName, setBranchName] = useState(
    `restore/${new Date().toISOString().slice(0, 10)}`,
  );
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const promise =
      mode === "restore"
        ? () => projectsApi.restoreCommit(projectId, { commit: hash })
        : () =>
            projectsApi.createBranch(projectId, {
              from_commit: hash,
              name: branchName,
            });
    void runAction(
      `git-${mode}`,
      promise,
      mode === "restore"
        ? t("projectGit.dialog.restoreSuccess")
        : t("projectGit.dialog.branchSuccess"),
    ).then((succeeded) => {
      if (succeeded) onClose();
    });
  };
  return (
    <ProjectDialog
      open
      onClose={onClose}
      ariaLabel={
        mode === "restore"
          ? t("projectGit.dialog.restoreTitle")
          : t("projectGit.dialog.branchTitle")
      }
      className="project-workspace__git-dialog"
    >
      <form className="project-workspace__modal" onSubmit={submit}>
        <header>
          <div>
            <span>{t("projectGit.dialog.eyebrow")}</span>
            <h2 id="project-git-dialog-title">
              {mode === "restore"
                ? t("projectGit.dialog.restoreTitle")
                : t("projectGit.dialog.branchTitle")}
            </h2>
          </div>
          <ProjectIconButton
            aria-label={t("projectGit.dialog.close")}
            onClick={onClose}
          >
            <IconX size={18} />
          </ProjectIconButton>
        </header>
        <p>
          {mode === "restore" ? (
            <>
              {t("projectGit.dialog.restoreDescriptionBefore")}
              <code>{hash}</code>
              {t("projectGit.dialog.restoreDescriptionAfter")}
            </>
          ) : (
            <>
              {t("projectGit.dialog.branchDescriptionBefore")}
              <code>{hash}</code>
              {t("projectGit.dialog.branchDescriptionAfter")}
            </>
          )}
        </p>
        {mode === "branch" && (
          <ProjectField
            label={t("projectGit.dialog.branchName")}
            labelFor="project-git-branch"
            required
          >
            <TextInput
              id="project-git-branch"
              value={branchName}
              onChange={(e) => setBranchName(e.target.value)}
              required
              pattern="[A-Za-z0-9._/-]+"
            />
          </ProjectField>
        )}
        <footer>
          <Button type="button" variant="secondary" onClick={onClose}>
            {t("projectGit.dialog.cancel")}
          </Button>
          <Button
            type="submit"
            variant={mode === "restore" ? "danger" : "primary"}
            disabled={busy === `git-${mode}`}
          >
            {busy === `git-${mode}` && (
              <IconLoader2 className="project-workspace__spinner" size={16} />
            )}
            {mode === "restore"
              ? t("projectGit.dialog.restoreSubmit")
              : t("projectGit.dialog.branchSubmit")}
          </Button>
        </footer>
      </form>
    </ProjectDialog>
  );
}
