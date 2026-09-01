import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useDialog } from "../../components/Dialog/DialogProvider";
import { useToast } from "../../components/Toast/ToastProvider";
import { projectsApi } from "../../services/projects";
import type { ProjectSummary } from "./types";
import {
  closestProjectTraceValue as closestTraceValue,
  inferProjectSessionIntent as inferredSessionIntent,
  projectTraceRecords as traceRecords,
  projectTraceValue as traceValue,
  projectSessionTargetFromUrl,
  projectSessionUrlPatch,
  resolveProjectSessionRoute as sessionRouteOf,
  type ProjectSessionIntent as SessionIntent,
} from "./projectSessionRouting";
import {
  normalizeProjectWorkspaceUrl,
  projectWorkItemUrlPatch,
  projectWorkspaceTabFromUrl,
  projectWorkspaceTabUrlPatch,
  type ProjectWorkspaceTab as WorkspaceTab,
  type ProjectWorkspaceUrlPatch as WorkspaceUrlPatch,
} from "./projectWorkspaceRouting";
import { workspaceDomainForTab } from "./projectWorkspace/config";
import {
  arr,
  errorMessage,
  obj,
  pickCollection,
  templateEditorId,
  text,
} from "./projectWorkspace/helpers";
import { ProjectWorkspacePageShell } from "./projectWorkspace/pageShell";
import type {
  OpenSession,
  ProjectSessionTarget,
  RecordValue,
  WorkspaceData,
  WorkspaceNavigationState,
} from "./projectWorkspace/types";
import "./projectWorkspace.css";

export default function ProjectWorkspacePage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const dialog = useDialog();
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
          if (dashboardResponse.status === "rejected") {
            throw dashboardResponse.reason;
          }
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
        data.runs.some((run) => ["queued", "running"].includes(text(run, "status")))),
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
  const selectedCommitId = normalizedSearchParams.get("commit") || "";
  const selectedCommit =
    data?.commits.find(
      (commit) =>
        text(commit, "commit", "hash", "commit_hash", "id") ===
        selectedCommitId,
    ) ||
    data?.commits[0] ||
    null;
  const activeDomain = workspaceDomainForTab(tab);
  const isOwner = data ? data.project.access_role === "owner" : false;
  const editingTemplateId =
    data ? templateEditorId(data.project, data.policies) : "";
  const saveTemplate = async () => {
    if (!editingTemplateId) return;
    setBusyAction("template-editor-save");
    try {
      await projectsApi.updateTemplateFromProject(
        editingTemplateId,
        projectId,
      );
      await projectsApi.delete(projectId);
      toast.success(t("projectTemplates.management.saveSuccess"));
      navigate(
        `/projects/templates?template=${encodeURIComponent(editingTemplateId)}`,
        { replace: true },
      );
    } catch {
      toast.error(t("projectTemplates.management.saveFailed"));
    } finally {
      setBusyAction("");
    }
  };
  const cancelTemplateEditing = async () => {
    if (!editingTemplateId) return;
    const confirmed = await dialog.confirm(
      t("projectTemplates.management.cancelDescription"),
      {
        title: t("projectTemplates.management.cancelTitle"),
        confirmLabel: t("projectTemplates.management.cancelEdit"),
      },
    );
    if (!confirmed) return;
    setBusyAction("template-editor-cancel");
    try {
      await projectsApi.delete(projectId);
      navigate("/projects/templates", { replace: true });
    } catch {
      toast.error(t("projectTemplates.management.cancelFailed"));
    } finally {
      setBusyAction("");
    }
  };
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
  /* activity.kind === "delivery" onNavigate("files", { file: "" }); onNavigate("git", { commit: "" }); projectWorkspaceNav.tabs.workspace id: "members" labelKey: "projectWorkspaceNav.tabs.projectAgents" createProjectAgent updateProjectAgent promoteProjectAgent openPromoteDialog projectAgents.actions.promote <ProjectDialog open={promoteDialogOpen} projectAgents.promotion.confirmTitle onOpenWorkspace(`${projectAgent.agent_dir}/soul.md`) */

  return (
    <ProjectWorkspacePageShell
      t={t}
      projectId={projectId}
      data={data}
      loading={loading}
      error={error}
      tab={tab}
      activeDomain={activeDomain}
      isOwner={isOwner}
      canEdit={canEdit}
      editingTemplateId={editingTemplateId}
      busyAction={busyAction}
      resourceWarnings={resourceWarnings}
      selectedWorkItemId={selectedWorkItemId}
      selectedMemberId={selectedMemberId}
      selectedCommit={selectedCommit}
      gitDialog={gitDialog}
      runtimeDialog={runtimeDialog}
      groupConfig={groupConfig}
      sessionTarget={sessionTarget}
      workspaceNavigation={workspaceNavigation}
      onNavigateWorkspace={navigateWorkspace}
      onOpenSession={openSession}
      onRunAction={runAction}
      onReload={() => {
        void load();
      }}
      onSaveTemplate={() => {
        void saveTemplate();
      }}
      onCancelTemplateEditing={() => {
        void cancelTemplateEditing();
      }}
      onSelectWorkItem={selectWorkItem}
      onSelectMember={selectMember}
      onSelectCommit={selectCommit}
      onSetGitDialog={setGitDialog}
      onSetRuntimeDialog={setRuntimeDialog}
      onChangeRuntimeStatus={() => {
        void changeRuntimeStatus();
      }}
      onCloseSessionTarget={closeSessionTarget}
    />
  );
}
