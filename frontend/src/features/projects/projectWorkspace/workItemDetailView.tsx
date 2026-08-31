import { type Dispatch, type ReactNode, type SetStateAction } from "react";
import { useTranslation } from "react-i18next";
import {
  IconArrowRight,
  IconBolt,
  IconCircleCheck,
  IconDeviceFloppy,
  IconFile,
  IconLoader2,
  IconMessageCircle,
  IconSettings,
} from "@tabler/icons-react";

import ProjectEventContent from "../components/ProjectEventContent";
import { projectUserFacingCopy } from "../projectUserFacingCopy";
import {
  Button,
  ProjectField,
  ProjectSelect,
} from "../components/ProjectUI";
import {
  inferProjectSessionIntent as inferredSessionIntent,
  projectTraceRecords as traceRecords,
  projectTraceValue as traceValue,
  resolveProjectSessionRoute as sessionRouteOf,
} from "../projectSessionRouting";
import type {
  ProjectWorkspaceTab as WorkspaceTab,
  ProjectWorkspaceUrlPatch as WorkspaceUrlPatch,
} from "../projectWorkspaceRouting";
import { dateLabel, obj, statusLabel, text } from "./helpers";
import { SessionButton, StatusPill } from "./shared";
import type { OpenSession, RecordValue } from "./types";

export function WorkItemDetailView({
  item,
  items,
  onSelect,
  onNavigate,
  onOpenSession,
  canManage,
  editing,
  setEditing,
  assignee,
  setAssignee,
  status,
  setStatus,
  priority,
  setPriority,
  memberOptions,
  statusOptions,
  priorityOptions,
  busyAction,
  save,
  startRun,
  approve,
  returnForChanges,
  cancelEdit,
  activeAssigneeName,
  workStatus,
  acceptanceCriteria,
  dependencyIds,
  displayEvidenceRecords,
  recentActivity,
  nextKey,
}: {
  item: RecordValue;
  items: RecordValue[];
  onSelect: (id: string) => void;
  onNavigate: (tab: WorkspaceTab, patch?: WorkspaceUrlPatch) => void;
  onOpenSession: OpenSession;
  canManage: boolean;
  editing: boolean;
  setEditing: Dispatch<SetStateAction<boolean>>;
  assignee: string;
  setAssignee: Dispatch<SetStateAction<string>>;
  status: string;
  setStatus: Dispatch<SetStateAction<string>>;
  priority: string;
  setPriority: Dispatch<SetStateAction<string>>;
  memberOptions: Array<{ value: string; label: string; disabled?: boolean }>;
  statusOptions: Array<{ value: string; label: string }>;
  priorityOptions: Array<{ value: string; label: string }>;
  busyAction: string;
  save: () => Promise<void>;
  startRun: () => void;
  approve: () => void;
  returnForChanges: () => void;
  cancelEdit: () => void;
  activeAssigneeName: string;
  workStatus: string;
  acceptanceCriteria: string[];
  dependencyIds: string[];
  displayEvidenceRecords: RecordValue[];
  recentActivity: Array<{
    kind: "execution" | "discussion" | "delivery";
    source: RecordValue;
    createdAt: string;
    key: string;
  }>;
  nextKey:
    | "unassigned"
    | "inProgress"
    | "blocked"
    | "review"
    | "done"
    | "todo";
}) {
  const { t } = useTranslation();
  const openActivity = (activity: (typeof recentActivity)[number]) => {
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
  const currentAssigneeId = text(item || {}, "assignee_agent_id");
  const nextActions: ReactNode = (() => {
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
                          <IconArrowRight size={15} />
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
              className="project-workspace__item-section project-workspace__item-progress"
            >
              <header>
                <h3>
                  {t(
                    "projectWorkspacePage.workItems.detail.singlePage.progressTitle",
                  )}
                </h3>
                {recentActivity.length > 0 && <span>{recentActivity.length}</span>}
              </header>
              {recentActivity.length ? (
                <>
                  <div className="project-workspace__item-activity-list">
                    {recentActivity.map((activity) => {
                      const activityLabel = t(
                        "projectWorkspacePage.workItems.detail.singlePage.activity." +
                          activity.kind,
                      );
                      const executionStatus = text(activity.source, "status");
                      const activityContent =
                        activity.kind === "execution"
                          ? ["failed", "cancelled"].includes(executionStatus)
                            ? t(
                                "projectWorkspacePage.workItems.detail.singlePage.executionUnavailable",
                              )
                            : text(
                                obj(activity.source.output),
                                "summary",
                                "result",
                                "message",
                              ) || statusLabel(executionStatus, t)
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
                        <article
                          className={`project-workspace__item-activity is-${activity.kind}`}
                          key={activity.key}
                        >
                          <span
                            className="project-workspace__item-activity-marker"
                            aria-hidden="true"
                          >
                            <ActivityIcon size={17} />
                          </span>
                          <div className="project-workspace__item-activity-body">
                            <div className="project-workspace__item-activity-meta">
                              <strong>{activityLabel}</strong>
                              {activity.createdAt && (
                                <time>{dateLabel(activity.createdAt)}</time>
                              )}
                            </div>
                            <div
                              className="project-workspace__item-activity-summary"
                              title={activityContent}
                            >
                              <ProjectEventContent
                                content={activityContent}
                                maxChars={100}
                              />
                            </div>
                          </div>
                          <Button
                            variant="ghost"
                            className="project-workspace__item-activity-action"
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
                            <IconArrowRight size={14} />
                          </Button>
                        </article>
                      );
                    })}
                  </div>
                  <Button variant="secondary" onClick={() => onNavigate("runs")}>
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
