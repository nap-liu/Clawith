import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import {
  IconAlertTriangle,
  IconArchive,
  IconArrowRight,
  IconChecklist,
  IconChevronRight,
  IconCircleCheck,
  IconHistory,
  IconMessageCircle,
  IconTargetArrow,
  IconUsers,
} from "@tabler/icons-react";

import SessionViewerDrawer, {
  type SessionViewerGroupConfig,
  type SessionViewerTarget,
} from "../../../components/SessionViewerDrawer";
import { projectUserFacingCopy } from "../projectUserFacingCopy";
import {
  Button,
  ProjectCountBadge,
  ProjectEmptyState,
  ProjectIconButton,
  ProjectStatusBadge,
} from "../components/ProjectUI";
import { A2AMeshGraph, ProjectGraphLegend } from "../components/ProjectGraphs";
import {
  inferProjectSessionIntent as inferredSessionIntent,
  isProjectA2ARecord,
  projectTraceRecords as traceRecords,
  projectTraceValue as traceValue,
  resolveProjectSessionRoute as sessionRouteOf,
} from "../projectSessionRouting";
import type { ProjectWorkspaceTab as WorkspaceTab, ProjectWorkspaceUrlPatch as WorkspaceUrlPatch } from "../projectWorkspaceRouting";
import {
  bool,
  compactId,
  dateLabel,
  isProjectRiskEvent,
  obj,
  text,
} from "./helpers";
import { EmptyState, SectionHeading, SessionButton, StatusPill } from "./shared";
import type {
  OpenSession,
  RecordValue,
  WorkspaceData,
} from "./types";
import type { ProjectSummary } from "../types";

export function Cockpit({
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
    [data.members, t],
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
      description: t("projectCockpit.blockedWorkItemHint"),
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

export function GroupChatPanel({
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

export function MeshPanel({
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
        eyebrow={t("projectMesh.eyebrow")}
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
