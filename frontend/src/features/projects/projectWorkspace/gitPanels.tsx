import {
  useCallback,
  useContext,
  useMemo,
  useState,
  type FormEvent,
} from "react";
import { useTranslation } from "react-i18next";
import {
  IconBrandGit,
  IconCircleCheck,
  IconFilter,
  IconGitBranch,
  IconHistory,
  IconLoader2,
  IconRefresh,
  IconRestore,
  IconX,
} from "@tabler/icons-react";

import { projectsApi } from "../../../services/projects";
import {
  Button,
  ProjectCountBadge,
  ProjectDataTable,
  ProjectDataTableBody,
  ProjectDataTableCell,
  ProjectDataTableHead,
  ProjectDataTableHeader,
  ProjectDataTableRow,
  ProjectDialog,
  ProjectField,
  ProjectIconButton,
  ProjectSelect,
  ProjectStatusBadge,
  SearchInput,
  TextInput,
} from "../components/ProjectUI";
import ProjectEventContent from "../components/ProjectEventContent";
import {
  closestProjectTraceValue as closestTraceValue,
  inferProjectSessionIntent as inferredSessionIntent,
  isProjectA2ARecord,
  projectTraceRecords as traceRecords,
  projectTraceValue as traceValue,
  resolveProjectSessionRoute as sessionRouteOf,
} from "../projectSessionRouting";
import type { ProjectSummary } from "../types";
import {
  bool,
  compactId,
  dateLabel,
  isProjectRiskEvent,
  redactInternalUuids,
  sessionIdOf,
  text,
} from "./helpers";
import { GitRepositoryControls } from "./gitRepositoryControls";
import { WorkspaceNavigationContext, useWorkspacePagination } from "./navigation";
import { EmptyState, SectionHeading, SessionButton } from "./shared";
import type { OpenSession, RecordValue } from "./types";

export function GitPanel({
  projectId,
  project,
  repository,
  commits,
  events,
  selected,
  onSelect,
  onDialog,
  onOpenSession,
  onReload,
}: {
  projectId: string;
  project: ProjectSummary;
  repository: RecordValue;
  commits: RecordValue[];
  events: RecordValue[];
  selected: RecordValue | null;
  onSelect: (commit: RecordValue) => void;
  onDialog: (mode: "restore" | "branch") => void;
  onOpenSession: OpenSession;
  onReload: () => Promise<void>;
}) {
  const { t } = useTranslation();
  const selectedId = text(
    selected || {},
    "commit",
    "hash",
    "commit_hash",
    "id",
  );
  const { pageItems: visibleCommits, pagination } = useWorkspacePagination(
    commits,
    "gitCommits",
    20,
  );
  const eventsForCommit = (commit: RecordValue) => {
    const commitId = text(commit, "commit", "hash", "commit_hash", "id");
    return events.filter(
      (event) =>
        traceValue(
          traceRecords(event),
          "commit_hash",
          "commit",
          "hash",
          "from_commit",
          "source_commit",
        ) === commitId,
    );
  };
  const responsibleAgentForCommit = (commit: RecordValue) => {
    for (const event of eventsForCommit(commit)) {
      const responsibleAgent = traceValue(
        traceRecords(event),
        "agent_name",
        "actor_name",
        "member_name",
        "name_snapshot",
      );
      if (responsibleAgent) return responsibleAgent;
    }

    const author = text(commit, "author", "author_name", "actor_name").trim();
    const normalizedAuthor = author.toLocaleLowerCase();
    return author &&
      normalizedAuthor !== "clawith" &&
      normalizedAuthor !== "clawith project"
      ? author
      : t("projectGit.projectMember");
  };
  const refsForCommit = (commit: RecordValue, index: number) => {
    const refs = new Set<string>();
    if (index === 0) refs.add("HEAD");
    [commit.branch, commit.branches, commit.refs].forEach((value) => {
      if (Array.isArray(value))
        value.forEach((entry) => {
          if (entry) refs.add(String(entry));
        });
      else if (typeof value === "string" && value.trim())
        value.split(",").forEach((entry) => refs.add(entry.trim()));
    });
    eventsForCommit(commit).forEach((event) => {
      if (text(event, "event_type", "type") !== "git.branch.created") return;
      const branch = traceValue(traceRecords(event), "branch", "branch_name");
      if (branch) refs.add(branch);
    });
    return [...refs];
  };
  const isMilestone = (commit: RecordValue) =>
    bool(commit, "milestone") ||
    eventsForCommit(commit).some(
      (event) =>
        text(event, "event_type", "type") === "git.milestone.created" ||
        traceRecords(event).some((record) => record.milestone === true),
    );
  const selectedIndex = Math.max(
    0,
    commits.findIndex(
      (commit) =>
        text(commit, "commit", "hash", "commit_hash", "id") === selectedId,
    ),
  );
  const selectedRefs = selected ? refsForCommit(selected, selectedIndex) : [];
  const selectedEvents = selected ? eventsForCommit(selected) : [];
  const linkedEvent =
    selectedEvents.find((event) => sessionIdOf(event)) || selectedEvents[0];
  const sessionSource = sessionIdOf(selected || {})
    ? selected || {}
    : linkedEvent || selected || {};
  return (
    <>
      <SectionHeading
        eyebrow="GIT / HISTORY"
        title={t("projectGit.title")}
        description=""
      />
      <GitRepositoryControls
        projectId={projectId}
        project={project}
        repository={repository}
        commits={commits}
        onReload={onReload}
      />
      {commits.length ? (
        <div className="project-workspace__git-layout">
          <section
            className="project-workspace__card project-workspace__git-log"
            aria-label={t("projectGit.historyAria")}
          >
            <header>
              <div>
                <h3>{t("projectGit.history")}</h3>
              </div>
              <ProjectCountBadge>{commits.length}</ProjectCountBadge>
            </header>
            <div className="project-workspace__git-log-head" aria-hidden="true">
              <span>{t("projectGit.commit")}</span>
              <span>{t("projectGit.message")}</span>
              <span>{t("projectGit.author")}</span>
              <span>{t("projectGit.time")}</span>
              <span>{t("projectGit.references")}</span>
            </div>
            <ol>
              {visibleCommits.map((commit) => {
                const index = commits.indexOf(commit);
                const hash = text(
                  commit,
                  "commit",
                  "hash",
                  "commit_hash",
                  "id",
                );
                const refs = refsForCommit(commit, index);
                const milestone = isMilestone(commit);
                return (
                  <li key={hash}>
                    <Button
                      type="button"
                      variant="ghost"
                      className={hash === selectedId ? "is-active" : ""}
                      aria-pressed={hash === selectedId}
                      onClick={() => onSelect(commit)}
                    >
                      <code title={hash}>
                        {text(commit, "short_commit") || hash.slice(0, 12)}
                      </code>
                      <span
                        className="project-workspace__git-log-message"
                        title={
                          text(commit, "message", "subject", "title") ||
                          t("projectGit.unnamedCommit")
                        }
                      >
                        <strong>
                          {text(commit, "message", "subject", "title") ||
                            t("projectGit.unnamedCommit")}
                        </strong>
                        <small>{hash}</small>
                      </span>
                      <span
                        className="project-workspace__git-log-author"
                        title={responsibleAgentForCommit(commit)}
                      >
                        {responsibleAgentForCommit(commit)}
                      </span>
                      <time dateTime={text(commit, "created_at", "timestamp")}>
                        {dateLabel(commit.created_at || commit.timestamp)}
                      </time>
                      <span className="project-workspace__git-refs">
                        {refs.map((ref) => (
                          <ProjectStatusBadge
                            key={ref}
                            tone={ref === "HEAD" ? "success" : "info"}
                          >
                            {ref}
                          </ProjectStatusBadge>
                        ))}
                        {milestone && (
                          <ProjectStatusBadge tone="warning">
                            {t("projectGit.milestone")}
                          </ProjectStatusBadge>
                        )}
                        {!refs.length && !milestone && <small>—</small>}
                      </span>
                    </Button>
                  </li>
                );
              })}
            </ol>
            {pagination}
          </section>
          <aside className="project-workspace__card project-workspace__commit-detail">
            <header>
              <div>
                <span>{t("projectGit.detail")}</span>
                <h3>
                  {text(selected || {}, "message", "subject", "title") ||
                    t("projectGit.unnamedCommit")}
                </h3>
              </div>
            </header>
            <div className="project-workspace__repro">
              <IconCircleCheck size={18} />
              <span>
                <strong>{t("projectGit.recoverable")}</strong>
              </span>
            </div>
            <dl className="project-workspace__definition-list">
              <div>
                <dt>{t("projectGit.commit")}</dt>
                <dd>
                  <code>{selectedId ? compactId(selectedId) : "—"}</code>
                </dd>
              </div>
              <div>
                <dt>{t("projectGit.author")}</dt>
                <dd>{responsibleAgentForCommit(selected || {})}</dd>
              </div>
              <div>
                <dt>{t("projectGit.time")}</dt>
                <dd>
                  {dateLabel(selected?.created_at || selected?.timestamp)}
                </dd>
              </div>
              <div>
                <dt>{t("projectGit.references")}</dt>
                <dd className="project-workspace__git-detail-refs">
                  {selectedRefs.filter((ref) => ref !== "HEAD").map((ref) => (
                    <ProjectStatusBadge key={ref} tone="info">
                      {ref}
                    </ProjectStatusBadge>
                  ))}
                  {selected && isMilestone(selected) && (
                    <ProjectStatusBadge tone="warning">
                      {t("projectGit.milestone")}
                    </ProjectStatusBadge>
                  )}
                  {!selectedRefs.filter((ref) => ref !== "HEAD").length &&
                    !(selected && isMilestone(selected)) &&
                    "—"}
                </dd>
              </div>
              <div>
                <dt>{t("projectGit.relatedRun")}</dt>
                <dd>
                  {traceValue(
                    traceRecords(linkedEvent || selected || {}),
                    "run_id",
                  )
                    ? t("projectGit.linked")
                    : "—"}
                </dd>
              </div>
              <div>
                <dt>{t("projectGit.workItem")}</dt>
                <dd>
                  {traceValue(
                    traceRecords(linkedEvent || selected || {}),
                    "work_item_id",
                  )
                    ? t("projectGit.linked")
                    : "—"}
                </dd>
              </div>
              <div>
                <dt>{t("projectGit.conversation")}</dt>
                <dd>
                  {sessionIdOf(sessionSource) ? t("projectGit.linked") : "—"}
                  <SessionButton
                    source={sessionSource}
                    onOpen={onOpenSession}
                  />
                </dd>
              </div>
              <div>
                <dt>{t("projectGit.changedFiles")}</dt>
                <dd>
                  {text(selected || {}, "file_count", "files_changed") || "—"}
                </dd>
              </div>
            </dl>
            {project.access_role === "owner" && (
              <div className="project-workspace__git-actions">
                <Button variant="secondary" onClick={() => onDialog("branch")}>
                  <IconGitBranch size={16} />
                  {t("projectGit.createBranch")}
                </Button>
                <Button variant="danger" onClick={() => onDialog("restore")}>
                  <IconRestore size={16} />
                  {t("projectGit.restoreVersion")}
                </Button>
              </div>
            )}
          </aside>
        </div>
      ) : (
        <EmptyState
          icon={<IconBrandGit size={22} />}
          title={t("projectGit.emptyTitle")}
          description={t("projectGit.emptyDescription")}
        />
      )}
    </>
  );
}

export function AuditPanel({
  events,
  members,
  onRefresh,
  onOpenSession,
}: {
  events: RecordValue[];
  members: RecordValue[];
  onRefresh: () => Promise<void>;
  onOpenSession: OpenSession;
}) {
  const { t } = useTranslation();
  const { get, update } = useContext(WorkspaceNavigationContext);
  const selectedEventId = get("auditEvent");
  const query = get("auditQ");
  const scope = get("auditScope");
  const actor = get("auditActor");
  const kind = get("auditType");
  const memberNames = useMemo(
    () =>
      new Map(
        members.map((member) => [
          text(member, "agent_id"),
          text(member, "name_snapshot", "agent_name", "name") ||
            t("projectAudit.projectAgent"),
        ]),
      ),
    [members, t],
  );
  const actorLabel = useCallback(
    (event: RecordValue) => {
      const agentId = text(event, "actor_agent_id");
      if (agentId)
        return memberNames.get(agentId) || t("projectAudit.projectAgent");
      if (text(event, "actor_user_id")) return t("projectAudit.projectUser");
      return t("projectAudit.projectSystem");
    },
    [memberNames, t],
  );
  const eventLabel = useCallback(
    (event: RecordValue) => {
      const code = text(event, "type", "event_type");
      return t(`projectAudit.events.${code}`, {
        defaultValue: t("projectAudit.eventFallback"),
      });
    },
    [t],
  );
  const actors = useMemo(
    () => Array.from(new Set(events.map(actorLabel))),
    [actorLabel, events],
  );
  const kinds = useMemo(
    () =>
      Array.from(
        new Set(
          events
            .map((event) => text(event, "type", "event_type"))
            .filter(Boolean),
        ),
      ),
    [events],
  );
  const visible = events.filter(
    (event) =>
      (!selectedEventId || text(event, "id", "event_id") === selectedEventId) &&
      (!scope ||
        (scope === "a2a" && isProjectA2ARecord(event)) ||
        (scope === "risks" && isProjectRiskEvent(event))) &&
      (!query ||
        `${JSON.stringify(event)} ${eventLabel(event)} ${actorLabel(event)}`
          .toLowerCase()
          .includes(query.toLowerCase())) &&
      (!actor || actorLabel(event) === actor) &&
      (!kind || text(event, "type", "event_type") === kind),
  );
  const { pageItems: visibleEvents, pagination } = useWorkspacePagination(
    visible,
    "auditEvents",
    20,
  );
  return (
    <section className="project-workspace__audit-shell">
      <div className="project-workspace__audit-filters">
        <SearchInput
          value={query}
          onChange={(event) =>
            update(
              {
                auditEvent: undefined,
                auditQ: event.target.value || undefined,
                auditEventsPage: undefined,
              },
              { replace: true },
            )
          }
          placeholder={t("projectWorkspacePage.audit.searchPlaceholder")}
          aria-label={t("projectWorkspacePage.audit.searchAria")}
        />
        <ProjectSelect
          value={actor}
          options={actors.map((value) => ({ value, label: value }))}
          onChange={(value) =>
            update({
              auditActor: value || undefined,
              auditEventsPage: undefined,
            })
          }
          ariaLabel={t("projectAudit.filterActor")}
          placeholder={t("projectAudit.allActors")}
        />
        <ProjectSelect
          value={kind}
          options={kinds.map((value) => ({
            value,
            label: eventLabel({ event_type: value }),
          }))}
          onChange={(value) =>
            update({
              auditType: value || undefined,
              auditEventsPage: undefined,
            })
          }
          ariaLabel={t("projectWorkspacePage.audit.typeFilterAria")}
          placeholder={t("projectWorkspacePage.audit.allTypes")}
        />
        <Button
          variant="ghost"
          onClick={() =>
            update({
              auditEvent: undefined,
              auditQ: undefined,
              auditScope: undefined,
              auditActor: undefined,
              auditType: undefined,
              auditEventsPage: undefined,
            })
          }
        >
          <IconFilter size={15} />
          {t("projectWorkspacePage.audit.clearFilters")}
        </Button>
        <Button variant="secondary" onClick={() => void onRefresh()}>
          <IconRefresh size={16} />
          {t("projectWorkspacePage.audit.refresh")}
        </Button>
      </div>

      {visible.length ? (
        <>
          <div className="project-workspace__audit-results">
            <ProjectDataTable className="project-workspace__audit-table">
              <ProjectDataTableHead>
                <ProjectDataTableRow>
                  <ProjectDataTableHeader>
                    {t("projectAudit.timeColumn")}
                  </ProjectDataTableHeader>
                  <ProjectDataTableHeader>
                    {t("projectAudit.actorColumn")}
                  </ProjectDataTableHeader>
                  <ProjectDataTableHeader>
                    {t("projectAudit.eventColumn")}
                  </ProjectDataTableHeader>
                  <ProjectDataTableHeader>
                    {t("projectAudit.detailsColumn")}
                  </ProjectDataTableHeader>
                  <ProjectDataTableHeader>
                    {t("projectAudit.relationsColumn")}
                  </ProjectDataTableHeader>
                </ProjectDataTableRow>
              </ProjectDataTableHead>
              <ProjectDataTableBody>
                {visibleEvents.map((event) => {
                  const eventCode = text(event, "type", "event_type");
                  const eventId = text(event, "id", "event_id");
                  const eventRecords = traceRecords(event);
                  const relationLabels = [
                    closestTraceValue(
                      eventRecords,
                      "work_item_id",
                      "project_work_item_id",
                    )
                      ? t("projectAudit.relatedWorkItem")
                      : "",
                    closestTraceValue(eventRecords, "run_id", "project_run_id")
                      ? t("projectAudit.relatedRun")
                      : "",
                    closestTraceValue(eventRecords, "commit_hash", "commit")
                      ? t("projectAudit.relatedCodeChange")
                      : "",
                  ].filter(Boolean);
                  const sessionIntent = inferredSessionIntent(event);
                  const hasSession = Boolean(
                    sessionRouteOf(event, sessionIntent),
                  );
                  const eventContent = redactInternalUuids(
                    text(event, "message", "summary", "detail"),
                    t("projectAudit.internalReferenceHidden"),
                  );

                  return (
                    <ProjectDataTableRow
                      key={
                        eventId || `${text(event, "created_at")}-${eventCode}`
                      }
                    >
                      <ProjectDataTableCell>
                        {dateLabel(event.created_at)}
                      </ProjectDataTableCell>
                      <ProjectDataTableCell>
                        {actorLabel(event)}
                      </ProjectDataTableCell>
                      <ProjectDataTableCell>
                        <span>{eventLabel(event)}</span>
                      </ProjectDataTableCell>
                      <ProjectDataTableCell>
                        <ProjectEventContent
                          eventType={eventCode}
                          content={eventContent}
                          maxChars={180}
                        />
                      </ProjectDataTableCell>
                      <ProjectDataTableCell>
                        <div className="project-workspace__audit-relations">
                          {relationLabels.map((label) => (
                            <span
                              className="project-workspace__audit-relation-label"
                              key={label}
                            >
                              {label}
                            </span>
                          ))}
                          {!relationLabels.length && !hasSession ? (
                            <span className="project-workspace__audit-relation-label">
                              {t("projectAudit.projectScope")}
                            </span>
                          ) : null}
                          <SessionButton
                            source={event}
                            onOpen={onOpenSession}
                            intent={sessionIntent}
                            label={t("projectAudit.viewSession")}
                          />
                        </div>
                      </ProjectDataTableCell>
                    </ProjectDataTableRow>
                  );
                })}
              </ProjectDataTableBody>
            </ProjectDataTable>
          </div>
          {pagination}
        </>
      ) : (
        <EmptyState
          icon={<IconHistory size={22} />}
          title={
            events.length
              ? t("projectWorkspacePage.audit.noMatchesTitle")
              : t("projectWorkspacePage.audit.emptyTitle")
          }
          description={
            events.length
              ? t("projectWorkspacePage.audit.noMatchesDescription")
              : t("projectWorkspacePage.audit.emptyDescription")
          }
        />
      )}
    </section>
  );
}

export function GitActionDialog({
  projectId,
  mode,
  commit,
  busy,
  onClose,
  runAction,
}: {
  projectId: string;
  mode: "restore" | "branch";
  commit: RecordValue;
  busy: string;
  onClose: () => void;
  runAction: (
    key: string,
    action: () => Promise<unknown>,
    success: string,
  ) => Promise<boolean>;
}) {
  const { t } = useTranslation();
  const hash = text(commit, "commit", "hash", "commit_hash", "id");
  const [branchName, setBranchName] = useState(
    `restore/${new Date().toISOString().slice(0, 10)}`,
  );
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const promise =
      mode === "restore"
        ? () => projectsApi.restoreCommit(projectId, { commit: hash })
        : () =>
            projectsApi.createBranch(projectId, {
              from_commit: hash,
              name: branchName,
            });
    void runAction(
      `git-${mode}`,
      promise,
      mode === "restore"
        ? t("projectGit.dialog.restoreSuccess")
        : t("projectGit.dialog.branchSuccess"),
    ).then((succeeded) => {
      if (succeeded) onClose();
    });
  };
  return (
    <ProjectDialog
      open
      onClose={onClose}
      ariaLabel={
        mode === "restore"
          ? t("projectGit.dialog.restoreTitle")
          : t("projectGit.dialog.branchTitle")
      }
      className="project-workspace__git-dialog"
    >
      <form className="project-workspace__modal" onSubmit={submit}>
        <header>
          <div>
            <span>{t("projectGit.dialog.eyebrow")}</span>
            <h2 id="project-git-dialog-title">
              {mode === "restore"
                ? t("projectGit.dialog.restoreTitle")
                : t("projectGit.dialog.branchTitle")}
            </h2>
          </div>
          <ProjectIconButton
            aria-label={t("projectGit.dialog.close")}
            onClick={onClose}
          >
            <IconX size={18} />
          </ProjectIconButton>
        </header>
        <p>
          {mode === "restore" ? (
            <>
              {t("projectGit.dialog.restoreDescriptionBefore")}
              <code>{hash}</code>
              {t("projectGit.dialog.restoreDescriptionAfter")}
            </>
          ) : (
            <>
              {t("projectGit.dialog.branchDescriptionBefore")}
              <code>{hash}</code>
              {t("projectGit.dialog.branchDescriptionAfter")}
            </>
          )}
        </p>
        {mode === "branch" && (
          <ProjectField
            label={t("projectGit.dialog.branchName")}
            labelFor="project-git-branch"
            required
          >
            <TextInput
              id="project-git-branch"
              value={branchName}
              onChange={(e) => setBranchName(e.target.value)}
              required
              pattern="[A-Za-z0-9._/-]+"
            />
          </ProjectField>
        )}
        <footer>
          <Button type="button" variant="secondary" onClick={onClose}>
            {t("projectGit.dialog.cancel")}
          </Button>
          <Button
            type="submit"
            variant={mode === "restore" ? "danger" : "primary"}
            disabled={busy === `git-${mode}`}
          >
            {busy === `git-${mode}` && (
              <IconLoader2 className="project-workspace__spinner" size={16} />
            )}
            {mode === "restore"
              ? t("projectGit.dialog.restoreSubmit")
              : t("projectGit.dialog.branchSubmit")}
          </Button>
        </footer>
      </form>
    </ProjectDialog>
  );
}
