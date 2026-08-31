import type { TFunction } from "i18next";
import {
  IconAlertTriangle,
  IconDeviceFloppy,
  IconLoader2,
  IconLock,
  IconPlayerPause,
  IconPlayerPlay,
  IconRefresh,
  IconSettings,
  IconSparkles,
  IconX,
} from "@tabler/icons-react";

import SessionViewerDrawer, {
  type SessionViewerGroupConfig,
} from "../../../components/SessionViewerDrawer";
import { projectsApi } from "../../../services/projects";
import { projectUserFacingCopy } from "../projectUserFacingCopy";
import {
  Button,
  ProjectCountBadge,
  ProjectDialog,
  ProjectIconButton,
  ProjectSegmentedControl,
} from "../components/ProjectUI";
import { inferProjectSessionIntent as inferredSessionIntent } from "../projectSessionRouting";
import type {
  ProjectWorkspaceTab as WorkspaceTab,
  ProjectWorkspaceUrlPatch as WorkspaceUrlPatch,
} from "../projectWorkspaceRouting";
import { WORKSPACE_DOMAINS } from "./config";
import { WorkspaceNavigationContext } from "./navigation";
import { StatusPill } from "./shared";
import { Cockpit, GroupChatPanel, MeshPanel } from "./overviewPanels";
import { WorkBoard } from "./workPanels";
import { FilesPanel, MilestonesPanel, RunsPanel } from "./deliveryPanels";
import { CapabilitiesPanel } from "./capabilityPanels";
import { PoliciesPanel } from "./policiesPanel";
import { AuditPanel, GitActionDialog, GitPanel } from "./gitPanels";
import { WorkItemDetail } from "./workItemDetail";
import { MembersPanel } from "./membersPanel";
import type {
  OpenSession,
  ProjectSessionTarget,
  RecordValue,
  WorkspaceData,
  WorkspaceDomainDefinition,
  WorkspaceNavigationState,
} from "./types";

export function ProjectWorkspacePageShell({
  t,
  projectId,
  data,
  loading,
  error,
  tab,
  activeDomain,
  isOwner,
  canEdit,
  editingTemplateId,
  busyAction,
  resourceWarnings,
  selectedWorkItemId,
  selectedMemberId,
  selectedCommit,
  gitDialog,
  runtimeDialog,
  groupConfig,
  sessionTarget,
  workspaceNavigation,
  onNavigateWorkspace,
  onOpenSession,
  onRunAction,
  onReload,
  onSaveTemplate,
  onCancelTemplateEditing,
  onSelectWorkItem,
  onSelectMember,
  onSelectCommit,
  onSetGitDialog,
  onSetRuntimeDialog,
  onChangeRuntimeStatus,
  onCloseSessionTarget,
}: {
  t: TFunction;
  projectId: string;
  data: WorkspaceData | null;
  loading: boolean;
  error: string;
  tab: WorkspaceTab;
  activeDomain: WorkspaceDomainDefinition | undefined;
  isOwner: boolean;
  canEdit: boolean;
  editingTemplateId: string;
  busyAction: string;
  resourceWarnings: string[];
  selectedWorkItemId: string;
  selectedMemberId: string;
  selectedCommit: RecordValue | null;
  gitDialog: "restore" | "branch" | null;
  runtimeDialog: "pause" | "resume" | null;
  groupConfig: SessionViewerGroupConfig;
  sessionTarget: ProjectSessionTarget | null;
  workspaceNavigation: WorkspaceNavigationState;
  onNavigateWorkspace: (
    nextTab: WorkspaceTab,
    patch?: WorkspaceUrlPatch,
  ) => void;
  onOpenSession: OpenSession;
  onRunAction: (
    key: string,
    action: () => Promise<unknown>,
    success: string,
  ) => Promise<boolean>;
  onReload: () => void;
  onSaveTemplate: () => void;
  onCancelTemplateEditing: () => void;
  onSelectWorkItem: (id: string) => void;
  onSelectMember: (id: string) => void;
  onSelectCommit: (commit: RecordValue) => void;
  onSetGitDialog: (value: "restore" | "branch" | null) => void;
  onSetRuntimeDialog: (value: "pause" | "resume" | null) => void;
  onChangeRuntimeStatus: () => void;
  onCloseSessionTarget: () => void;
}) {
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
        <Button variant="secondary" onClick={onReload}>
          <IconRefresh size={16} />
          {t("projectWorkspacePage.actions.reload")}
        </Button>
      </main>
    );
  }

  const renderContent = () => {
    switch (tab) {
      case "cockpit":
        return (
          <Cockpit
            data={data}
            onNavigate={onNavigateWorkspace}
            onOpenSession={onOpenSession}
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
            onSelect={onSelectWorkItem}
            onNavigate={onNavigateWorkspace}
            onOpenSession={onOpenSession}
            runAction={onRunAction}
            busyAction={busyAction}
            canManage={canEdit}
          />
        ) : (
          <WorkBoard
            projectId={projectId}
            items={data.workItems}
            members={data.members}
            runs={data.runs}
            onSelect={onSelectWorkItem}
            runAction={onRunAction}
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
              canEdit &&
              ["planning", "running", "paused", "waiting", "completed"].includes(
                data.project.status,
              )
            }
          />
        );
      case "mesh":
        return (
          <MeshPanel
            members={data.members}
            events={data.events}
            runs={data.runs}
            onNavigate={onNavigateWorkspace}
            onOpenSession={onOpenSession}
          />
        );
      case "files":
        return (
          <FilesPanel
            projectId={projectId}
            files={data.files}
            members={data.members}
            projectAgents={data.projectAgents}
            runAction={onRunAction}
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
            onOpenWorkItem={(id) => onNavigateWorkspace("work", { workItem: id })}
            onOpenSession={onOpenSession}
            runAction={onRunAction}
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
            onOpenSession={onOpenSession}
            runAction={onRunAction}
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
            onSelect={onSelectMember}
            onOpenWorkspace={(path) =>
              onNavigateWorkspace("files", { file: path })
            }
            onNavigate={onNavigateWorkspace}
            runAction={onRunAction}
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
            onReload={async () => onReload()}
            runAction={onRunAction}
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
            onSelect={onSelectCommit}
            onDialog={onSetGitDialog}
            onOpenSession={onOpenSession}
            onReload={async () => onReload()}
          />
        );
      case "audit":
        return (
          <AuditPanel
            events={data.events}
            members={data.members}
            onRefresh={async () => onReload()}
            onOpenSession={onOpenSession}
          />
        );
    }
  };

  return (
    <main
      className={`project-workspace${tab === "files" ? " project-workspace--files" : ""}${tab === "group" ? " project-workspace--chat" : ""}`}
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
        {editingTemplateId && (
          <div className="project-workspace__template-actions">
            <Button
              variant="secondary"
              disabled={busyAction.startsWith("template-editor-")}
              onClick={onCancelTemplateEditing}
            >
              {busyAction === "template-editor-cancel" ? (
                <IconLoader2 className="project-workspace__spinner" size={16} />
              ) : (
                <IconX size={16} />
              )}
              {t("projectTemplates.management.cancelEdit")}
            </Button>
            <Button
              variant="primary"
              disabled={busyAction.startsWith("template-editor-")}
              onClick={onSaveTemplate}
            >
              {busyAction === "template-editor-save" ? (
                <IconLoader2 className="project-workspace__spinner" size={16} />
              ) : (
                <IconDeviceFloppy size={16} />
              )}
              {t("projectTemplates.management.save")}
            </Button>
          </div>
        )}
        {isOwner && ["running", "paused", "waiting"].includes(data.project.status) && (
          <Button
            variant={data.project.status === "running" ? "secondary" : "primary"}
            disabled={busyAction === "project-runtime"}
            onClick={() =>
              onSetRuntimeDialog(
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
          onClick={() => onNavigateWorkspace("policies")}
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
                  onClick={() => onNavigateWorkspace(domain.defaultTab)}
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
                onChange={(nextTab) => onNavigateWorkspace(nextTab)}
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
                    void onRunAction(
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
                <Button variant="ghost" onClick={onReload}>
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
          onClose={() => onSetGitDialog(null)}
          runAction={onRunAction}
        />
      )}
      <ProjectDialog
        open={runtimeDialog !== null}
        onClose={() => {
          if (busyAction !== "project-runtime") onSetRuntimeDialog(null);
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
              onClick={() => onSetRuntimeDialog(null)}
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
              onClick={() => onSetRuntimeDialog(null)}
            >
              {t("common.cancel")}
            </Button>
            <Button
              variant={runtimeDialog === "pause" ? "danger" : "primary"}
              disabled={busyAction === "project-runtime"}
              onClick={onChangeRuntimeStatus}
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
        onClose={onCloseSessionTarget}
      />
    </main>
  );
}
