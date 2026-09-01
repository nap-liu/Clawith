import type { ComponentType } from "react";
import type { SessionViewerTarget } from "../../../components/SessionViewerDrawer";
import type {
  ProjectCapabilityOption,
  ProjectOwnedAgent,
  ProjectSummary,
} from "../types";
import type {
  ProjectSessionIntent as SessionIntent,
} from "../projectSessionRouting";
import type {
  ProjectWorkspaceTab as WorkspaceTab,
  ProjectWorkspaceUrlPatch as WorkspaceUrlPatch,
} from "../projectWorkspaceRouting";

export type RecordValue = Record<string, unknown>;

export type ProjectSessionTarget = SessionViewerTarget & {
  agentId: string;
  agentName: string;
  kind?: "group" | "session";
};

export type OpenSession = (
  source: RecordValue,
  title?: string,
  intent?: SessionIntent,
) => void;

export type WorkspaceNavigationState = {
  navigate: (tab: WorkspaceTab, patch?: WorkspaceUrlPatch) => void;
  get: (key: string) => string;
  update: (patch: WorkspaceUrlPatch, options?: { replace?: boolean }) => void;
};

export type WorkspaceData = {
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

export type WorkspaceDomain =
  "overview" | "work" | "collaboration" | "delivery" | "team" | "activity";

export type WorkspaceDomainDefinition = {
  id: WorkspaceDomain;
  defaultTab: WorkspaceTab;
  labelKey: string;
  icon: ComponentType<{ size?: number | string }>;
  tabs: Array<{ id: WorkspaceTab; labelKey: string }>;
};

export type ProjectToolDefinition = {
  name: string;
  label: string;
  description: string;
  descriptionKey?: string;
  participant: boolean;
};

export type MemberSkillOption = ProjectCapabilityOption;
