import { useContext, useMemo, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import {
  IconBolt,
  IconChecklist,
  IconFlag,
  IconLoader2,
  IconPlayerPause,
  IconPlayerPlay,
  IconRestore,
  IconCircleCheck,
  IconX,
} from "@tabler/icons-react";

import { Drawer } from "../../../components/Dialog/DialogProvider";
import { projectsApi } from "../../../services/projects";
import ProjectFileWorkspace from "../components/ProjectFileWorkspace";
import {
  Button,
  ProjectField,
  ProjectIconButton,
  ProjectStatusBadge,
  TextInput,
} from "../components/ProjectUI";
import {
  closestProjectTraceValue as closestTraceValue,
  inferProjectSessionIntent as inferredSessionIntent,
  projectTraceRecords as traceRecords,
  projectTraceValue as traceValue,
  resolveProjectSessionRoute as sessionRouteOf,
} from "../projectSessionRouting";
import {
  compactId,
  dateLabel,
  runAgentId,
  runAgentName,
  sameGitCommit,
  text,
  traceStringValues,
  obj,
} from "./helpers";
import { WorkspaceNavigationContext, useWorkspacePagination } from "./navigation";
import { EmptyState, SectionHeading, SessionButton, StatusPill } from "./shared";
import type { OpenSession, RecordValue } from "./types";
import type { ProjectOwnedAgent as ProjectOwnedAgentType } from "../types";

export function FilesPanel({
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
  projectAgents: ProjectOwnedAgentType[];
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

export function MilestonesPanel({
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
    const title = t("projectAudit.events.git.milestone.created");
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

export function RunsPanel({
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
        eyebrow={t("projectWorkspacePage.runs.eyebrow")}
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
