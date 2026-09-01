import { useContext, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import {
  IconChecklist,
  IconChevronRight,
  IconLoader2,
  IconPlus,
  IconX,
} from "@tabler/icons-react";

import { projectsApi } from "../../../services/projects";
import {
  Button,
  ProjectCountBadge,
  ProjectField,
  ProjectIconButton,
  ProjectProgressBar,
  ProjectSegmentedControl,
  ProjectSelect,
  ProjectTextarea,
  TextInput,
} from "../components/ProjectUI";
import { ProjectGraphLegend, WorkDependencyGraph } from "../components/ProjectGraphs";
import {
  projectTraceRecords as traceRecords,
  projectTraceValue as traceValue,
} from "../projectSessionRouting";
import {
  dateLabel,
  listText,
  obj,
  reportedPercent,
  text,
} from "./helpers";
import { WorkspaceNavigationContext, useWorkspacePagination } from "./navigation";
import { EmptyState, SectionHeading, StatusPill } from "./shared";
import type { RecordValue } from "./types";

export function WorkBoard({
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
              <span>{t("projectWorkspacePage.workItems.eyebrow")}</span>
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

export function WorkItemList({
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
                            ? t("projectWorkspacePage.workItems.latestExecution")
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
