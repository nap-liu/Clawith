import { type Dispatch, type SetStateAction } from "react";
import { useTranslation } from "react-i18next";
import {
  IconArchive,
  IconArrowRight,
  IconFlag,
  IconLoader2,
  IconPlus,
  IconRestore,
  IconSettings,
  IconSparkles,
  IconTrash,
  IconUsers,
} from "@tabler/icons-react";

import ToolsTab from "../../../pages/agent-detail/tabs/ToolsTab";
import SkillsTab from "../../../pages/agent-detail/tabs/SkillsTab";
import {
  Button,
  ProjectStatusBadge,
} from "../components/ProjectUI";
import { A2AMeshGraph } from "../components/ProjectGraphs";
import ProjectAgentSettingsPanel from "../components/ProjectAgentSettingsPanel";
import type {
  ProjectCapabilityOption,
  ProjectOwnedAgent,
} from "../types";
import {
  bool,
  text,
} from "./helpers";
import { EmptyState, SectionHeading } from "./shared";
import type {
  RecordValue,
} from "./types";
import type {
  ProjectWorkspaceTab as WorkspaceTab,
  ProjectWorkspaceUrlPatch as WorkspaceUrlPatch,
} from "../projectWorkspaceRouting";

export function MembersPanelView({
  projectId,
  members,
  collaborationEvents,
  member,
  agentId,
  projectAgent,
  memberId,
  memberName,
  canManage,
  departed,
  settingsOpen,
  setSettingsOpen,
  capabilitySection,
  setCapabilitySection,
  configDraft,
  updateConfigField,
  modelOptions,
  configCount,
  effectiveProjectToolCount,
  platformTools,
  memberMcps,
  memberSkills,
  busyAction,
  saveSnapshot,
  onSelect,
  onNavigate,
  openCreateAgent,
  openEditAgent,
  openPromoteDialog,
  openRemoveDialog,
  restoreMember,
  setLeader,
  memberSkillOptions,
  selectedMemberSkillIds,
  updateMemberSkills,
}: {
  projectId: string;
  members: RecordValue[];
  collaborationEvents: RecordValue[];
  member: RecordValue | undefined;
  agentId: string;
  projectAgent: ProjectOwnedAgent | null;
  memberId: string;
  memberName: string;
  canManage: boolean;
  departed: boolean;
  settingsOpen: boolean;
  setSettingsOpen: Dispatch<SetStateAction<boolean>>;
  capabilitySection: "config" | "tools" | "skill";
  setCapabilitySection: Dispatch<SetStateAction<"config" | "tools" | "skill">>;
  configDraft: RecordValue;
  updateConfigField: (key: string, value: unknown) => void;
  modelOptions: Array<{ value: string; label: string }>;
  configCount: number;
  effectiveProjectToolCount: number;
  platformTools: RecordValue[];
  memberMcps: RecordValue[];
  memberSkills: RecordValue[];
  busyAction: string;
  saveSnapshot: () => void;
  onSelect: (id: string) => void;
  onNavigate: (tab: WorkspaceTab, patch?: WorkspaceUrlPatch) => void;
  openCreateAgent: () => void;
  openEditAgent: () => void;
  openPromoteDialog: () => void;
  openRemoveDialog: () => void;
  restoreMember: () => void;
  setLeader: () => void;
  memberSkillOptions: ProjectCapabilityOption[];
  selectedMemberSkillIds: string[];
  updateMemberSkills: (nextIds: string[]) => void;
}) {
  const { t } = useTranslation();

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
                <Button variant="primary" onClick={openCreateAgent}>
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
            <>
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
              <ProjectAgentSettingsPanel
                className="project-workspace__action-panel project-workspace__member-editor"
                title={memberName}
                eyebrow={t("projectAgents.teamPage.workSettings")}
                value={capabilitySection}
                onChange={setCapabilitySection}
                config={{
                  primary_model_id: text(configDraft, "primary_model_id") || null,
                  fallback_model_id: text(configDraft, "fallback_model_id") || null,
                  max_tool_rounds: text(configDraft, "max_tool_rounds"),
                  project_instruction: text(configDraft, "project_instruction"),
                }}
                onConfigChange={(key, value) => updateConfigField(key, value)}
                modelOptions={modelOptions}
                counts={{
                  config: configCount,
                  tools:
                    effectiveProjectToolCount +
                    platformTools.length +
                    memberMcps.length,
                  skill: memberSkills.length,
                }}
                canManage={canManage}
                isReadonly={departed}
                onSave={saveSnapshot}
                saving={busyAction === "save-member"}
                headerActions={
                  <>
                    {projectAgent && (
                      <Button variant="secondary" onClick={openEditAgent}>
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
                            onClick={setLeader}
                          >
                            <IconFlag size={15} />
                            {t("projectTerminology.setOwner")}
                          </Button>
                          <Button variant="danger" onClick={openRemoveDialog}>
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
                  </>
                }
                tools={
                  <ToolsTab
                    agentId={projectAgent?.id || agentId}
                    agentName={memberName}
                    canManage={canManage && !departed}
                    canConfigure={
                      Boolean(projectAgent) && canManage && !departed
                    }
                    scope={projectAgent ? "agent" : "project"}
                    projectContext={
                      projectAgent ? undefined : { projectId, memberId }
                    }
                  />
                }
                skill={
                  <SkillsTab
                    agentId={projectAgent?.id || agentId}
                    canManage={canManage && !departed}
                    scope="project"
                    draftCapabilities={memberSkillOptions}
                    selectedCapabilityIds={selectedMemberSkillIds}
                    onSelectedCapabilityIdsChange={updateMemberSkills}
                  />
                }
              />
            </>
          )}
        </>
      ) : (
        <EmptyState
          icon={<IconUsers size={22} />}
          title={t("projectAgents.teamPage.noMembersTitle")}
          description={t("projectAgents.teamPage.noMembersDescription")}
          action={
            canManage ? (
              <Button variant="primary" onClick={openCreateAgent}>
                <IconPlus size={16} />
                {t("projectAgents.actions.create")}
              </Button>
            ) : undefined
          }
        />
      )}
    </>
  );
}
