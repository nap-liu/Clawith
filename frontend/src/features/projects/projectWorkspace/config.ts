import {
  IconActivityHeartbeat,
  IconArchive,
  IconBolt,
  IconHistory,
  IconMessageCircle,
  IconTargetArrow,
  IconUsers,
} from "@tabler/icons-react";
import type { ProjectSummary } from "../types";
import type {
  ProjectWorkspaceTab as WorkspaceTab,
} from "../projectWorkspaceRouting";
import type {
  ProjectToolDefinition,
  RecordValue,
  WorkspaceDomainDefinition,
} from "./types";

export const WORKSPACE_DOMAINS: WorkspaceDomainDefinition[] = [
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
      { id: "milestones", labelKey: "projectWorkspaceNav.tabs.milestones" },
      { id: "git", labelKey: "projectWorkspaceNav.tabs.git" },
    ],
  },
  {
    id: "team",
    defaultTab: "members",
    labelKey: "projectWorkspaceNav.domains.team",
    icon: IconUsers,
    tabs: [
      { id: "members", labelKey: "projectWorkspaceNav.tabs.projectAgents" },
      { id: "capabilities", labelKey: "projectWorkspaceNav.tabs.capabilities" },
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

export const workspaceDomainForTab = (
  tab: WorkspaceTab,
): WorkspaceDomainDefinition | undefined =>
  WORKSPACE_DOMAINS.find((domain) =>
    domain.tabs.some((candidate) => candidate.id === tab),
  );

export const PROJECT_TOOL_REGISTRY: readonly ProjectToolDefinition[] = [
  {
    name: "project_get_context",
    label: "View project overview",
    description: "Review the project goal, plan, status, and current version.",
    participant: true,
  },
  {
    name: "project_list_work_items",
    label: "View tasks",
    description:
      "Review project tasks or work assigned to the current member.",
    participant: true,
  },
  {
    name: "project_update_work_item",
    label: "Update task",
    description: "Update the status, progress, or evidence of an existing task.",
    descriptionKey: "projectTerminology.workspace.ownerToolDescription",
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
    label: "Create task",
    description:
      "Create and assign a task. Available to the execution lead.",
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
