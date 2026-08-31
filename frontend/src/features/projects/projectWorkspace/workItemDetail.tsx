import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { projectsApi } from "../../../services/projects";
import {
  closestProjectTraceValue as closestTraceValue,
  inferProjectSessionIntent as inferredSessionIntent,
  projectTraceRecords as traceRecords,
  projectTraceValue as traceValue,
  resolveProjectSessionRoute as sessionRouteOf,
} from "../projectSessionRouting";
import {
  projectWorkItemCompatibilityTargetFromUrl,
  type ProjectWorkspaceTab as WorkspaceTab,
  type ProjectWorkspaceUrlPatch as WorkspaceUrlPatch,
} from "../projectWorkspaceRouting";
import {
  arr,
  obj,
  pickCollection,
  sameGitCommit,
  text,
  traceStringValues,
} from "./helpers";
import type { OpenSession, RecordValue } from "./types";
import { WorkItemList } from "./workPanels";
import { WorkItemDetailView } from "./workItemDetailView";

export function WorkItemDetail({
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
  const sessionSources = [...dtoSessions, ...relatedRuns, ...relatedEvents].filter(
    (entry, index, source) => {
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
    },
  );
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
  const deliverySources = relatedFiles.length ? relatedFiles : relatedCommits;
  const recentActivity = [
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

  return (
    <WorkItemDetailView
      item={item}
      items={items}
      onSelect={onSelect}
      onNavigate={onNavigate}
      onOpenSession={onOpenSession}
      canManage={canManage}
      editing={editing}
      setEditing={setEditing}
      assignee={assignee}
      setAssignee={setAssignee}
      status={status}
      setStatus={setStatus}
      priority={priority}
      setPriority={setPriority}
      memberOptions={memberOptions}
      statusOptions={statusOptions}
      priorityOptions={priorityOptions}
      busyAction={busyAction}
      save={save}
      startRun={startRun}
      approve={approve}
      returnForChanges={returnForChanges}
      cancelEdit={cancelEdit}
      activeAssigneeName={activeAssigneeName}
      workStatus={workStatus}
      acceptanceCriteria={acceptanceCriteria}
      dependencyIds={dependencyIds}
      displayEvidenceRecords={displayEvidenceRecords}
      recentActivity={recentActivity}
      nextKey={nextKey}
    />
  );
}
