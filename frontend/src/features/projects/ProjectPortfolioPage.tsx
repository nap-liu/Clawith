import { useEffect, useMemo, useState } from "react";
import type { TFunction } from "i18next";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  IconArchive,
  IconArrowRight,
  IconFolder,
  IconLayoutGrid,
  IconList,
  IconLock,
  IconPlus,
  IconRefresh,
  IconTemplate,
  IconUsers,
} from "@tabler/icons-react";
import { projectsApi } from "../../services/projects";
import Pagination from "../../components/Pagination";
import type { ProjectScope, ProjectStatus, ProjectSummary } from "./types";
import {
  Button,
  ProjectCard,
  ProjectEmptyState,
  ProjectProgressBar,
  ProjectSegmentedControl,
  ProjectSelect,
  ProjectStatusBadge,
  SearchInput,
} from "./components/ProjectUI";
import "./projectPortfolio.css";

const scopeTabs: ProjectScope[] = ["mine", "shared", "running", "archived"];

const portfolioStatuses = new Set([
  "all",
  "running",
  "waiting",
  "paused",
  "completed",
  "failed",
]);

const statusTones: Record<
  ProjectStatus,
  "neutral" | "success" | "warning" | "error"
> = {
  planning: "warning",
  initializing: "neutral",
  running: "success",
  waiting: "warning",
  paused: "neutral",
  completed: "success",
  archived: "neutral",
  failed: "error",
};

function timeAgo(value: string, t: TFunction, language: string) {
  const delta = Date.now() - new Date(value).getTime();
  if (!Number.isFinite(delta) || delta < 60_000)
    return t("projectPortfolio.relative.justNow");
  if (delta < 3_600_000)
    return t("projectPortfolio.relative.minutesAgo", {
      count: Math.floor(delta / 60_000),
    });
  if (delta < 86_400_000)
    return t("projectPortfolio.relative.hoursAgo", {
      count: Math.floor(delta / 3_600_000),
    });
  return new Date(value).toLocaleDateString(language, {
    month: "short",
    day: "numeric",
  });
}

function AgentStack({
  project,
  ariaLabel,
}: {
  project: ProjectSummary;
  ariaLabel: string;
}) {
  const members = project.members || [];
  return (
    <div className="pm-agent-stack" aria-label={ariaLabel}>
      {members.slice(0, 4).map((member) =>
        member.avatar_url ? (
          <img
            key={member.agent_id}
            src={member.avatar_url}
            alt={member.agent_name}
          />
        ) : (
          <span key={member.agent_id} title={member.agent_name}>
            {member.agent_name.slice(0, 1)}
          </span>
        ),
      )}
      {members.length > 4 && <i>+{members.length - 4}</i>}
    </div>
  );
}

export default function ProjectPortfolioPage() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedScope = searchParams.get("scope");
  const scope = scopeTabs.some((item) => item === requestedScope)
    ? (requestedScope as ProjectScope)
    : "mine";
  const query = searchParams.get("q")?.trim() || "";
  const [queryDraft, setQueryDraft] = useState(query);
  const requestedStatus = searchParams.get("status") || "all";
  const status = portfolioStatuses.has(requestedStatus)
    ? requestedStatus
    : "all";
  const view = searchParams.get("view") === "grid" ? "grid" : "list";

  useEffect(() => setQueryDraft(query), [query]);

  const updateUrlState = (patch: Record<string, string | undefined>) => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      Object.entries(patch).forEach(([key, value]) => {
        if (value) next.set(key, value);
        else next.delete(key);
      });
      return next;
    });
  };

  const projectQuery = useQuery({
    queryKey: ["projects", "portfolio", scope, query, status],
    queryFn: () => projectsApi.list({ scope, query, status }),
  });
  const projects = projectQuery.data?.items ?? [];
  const pageSize = 10;
  const requestedPage = Number.parseInt(searchParams.get("page") || "1", 10);
  const totalPages = Math.max(1, Math.ceil(projects.length / pageSize));
  const page = Math.min(
    Math.max(1, Number.isFinite(requestedPage) ? requestedPage : 1),
    totalPages,
  );
  const visibleProjects = projects.slice(
    (page - 1) * pageSize,
    page * pageSize,
  );
  const setPage = (nextPage: number) =>
    updateUrlState({ page: nextPage > 1 ? String(nextPage) : undefined });
  const overview = useMemo(
    () => ({
      running: projects.filter((project) => project.status === "running")
        .length,
      activeAgents: projects.reduce(
        (sum, project) => sum + (project.active_agent_count ?? 0),
        0,
      ),
      waiting: projects.filter((project) => project.status === "waiting")
        .length,
      averageProgress: projects.length
        ? Math.round(
            projects.reduce((sum, project) => sum + project.progress, 0) /
              projects.length,
          )
        : 0,
    }),
    [projects],
  );

  const search = () => {
    updateUrlState({ q: queryDraft.trim() || undefined, page: undefined });
  };

  return (
    <main className="pm-page pm-portfolio">
      <header className="pm-page-header">
        <div>
          <span className="pm-eyebrow">{t("projectPortfolio.eyebrow")}</span>
          <h1>{t("projectPortfolio.title")}</h1>
          <p>{t("projectPortfolio.description")}</p>
        </div>
        <div className="pm-header-actions">
          <Button
            variant="secondary"
            className="pm-button pm-button-secondary"
            onClick={() => navigate("/projects/templates")}
          >
            <IconTemplate size={17} />
            {t("projectPortfolio.templateMarket")}
          </Button>
          <Button
            variant="primary"
            className="pm-button pm-button-primary"
            onClick={() => navigate("/projects/new")}
          >
            <IconPlus size={17} />
            {t("projectPortfolio.createProject")}
          </Button>
        </div>
      </header>

      <section
        className="pm-overview"
        aria-label={t("projectPortfolio.overviewAria")}
      >
        <div className="pm-overview-signal">
          <span className="pm-live-dot" />
          <div>
            <strong>
              {t("projectPortfolio.runningProjects", {
                count: overview.running,
              })}
            </strong>
            <small>
              {t("projectPortfolio.activeEmployees", {
                count: overview.activeAgents,
              })}
            </small>
          </div>
        </div>
        <dl>
          <div>
            <dt>{t("projectPortfolio.waitingForMe")}</dt>
            <dd>{overview.waiting}</dd>
          </div>
          <div>
            <dt>{t("projectPortfolio.currentProjects")}</dt>
            <dd>{projectQuery.data?.total ?? 0}</dd>
          </div>
          <div>
            <dt>{t("projectPortfolio.averageProgress")}</dt>
            <dd>{overview.averageProgress}%</dd>
          </div>
        </dl>
      </section>

      <nav className="pm-tabs" aria-label={t("projectPortfolio.scopeAria")}>
        {scopeTabs.map((tab) => (
          <Button
            variant="ghost"
            key={tab}
            className={scope === tab ? "is-active" : ""}
            onClick={() => {
              updateUrlState({
                scope: tab === "mine" ? undefined : tab,
                page: undefined,
              });
            }}
          >
            {tab === "archived" && <IconArchive size={15} />}
            {t(`projectPortfolio.scope.${tab}`)}
            {scope === tab && projectQuery.data && (
              <em>{projectQuery.data.total}</em>
            )}
          </Button>
        ))}
      </nav>

      <form
        className="pm-toolbar"
        onSubmit={(event) => {
          event.preventDefault();
          search();
        }}
      >
        <SearchInput
          className="pm-search"
          value={queryDraft}
          onChange={(event) => setQueryDraft(event.target.value)}
          placeholder={t("projectTerminology.portfolioSearch")}
          aria-label={t("projectPortfolio.searchAria")}
        />
        <ProjectSelect
          value={status}
          onChange={(value) => {
            updateUrlState({
              status: value === "all" ? undefined : value,
              page: undefined,
            });
          }}
          ariaLabel={t("projectPortfolio.statusAria")}
          options={[
            { value: "all", label: t("projectPortfolio.status.all") },
            { value: "running", label: t("projectPortfolio.status.running") },
            { value: "waiting", label: t("projectPortfolio.status.waiting") },
            { value: "paused", label: t("projectPortfolio.status.paused") },
            {
              value: "completed",
              label: t("projectPortfolio.status.completed"),
            },
            { value: "failed", label: t("projectPortfolio.status.failed") },
          ]}
        />
        <ProjectSegmentedControl
          className="pm-view-toggle"
          value={view}
          onChange={(value) =>
            updateUrlState({ view: value === "list" ? undefined : value })
          }
          ariaLabel={t("projectPortfolio.viewAria")}
          options={[
            {
              value: "list",
              label: (
                <IconList
                  size={16}
                  aria-label={t("projectPortfolio.listView")}
                />
              ),
            },
            {
              value: "grid",
              label: (
                <IconLayoutGrid
                  size={16}
                  aria-label={t("projectPortfolio.gridView")}
                />
              ),
            },
          ]}
        />
      </form>

      {projectQuery.isPending ? (
        <div className="pm-state">
          <span className="pm-spinner" />
          <strong>{t("projectPortfolio.loadingTitle")}</strong>
          <p>{t("projectPortfolio.loadingDescription")}</p>
        </div>
      ) : projectQuery.isError ? (
        <ProjectEmptyState
          tone="error"
          title={t("projectPortfolio.errorTitle")}
          description={
            projectQuery.error instanceof Error
              ? projectQuery.error.message
              : t("projectPortfolio.serviceError")
          }
          action={
            <Button
              variant="secondary"
              className="pm-button pm-button-secondary"
              onClick={() => projectQuery.refetch()}
            >
              <IconRefresh size={16} />
              {t("projectPortfolio.reload")}
            </Button>
          }
        />
      ) : projects.length === 0 ? (
        <ProjectEmptyState
          icon={<IconFolder size={24} />}
          title={t("projectPortfolio.emptyTitle")}
          description={
            query || status !== "all"
              ? t("projectPortfolio.filteredEmptyDescription")
              : t("projectPortfolio.emptyDescription")
          }
          action={
            query || status !== "all" ? (
              <Button
                variant="secondary"
                className="pm-button pm-button-secondary"
                onClick={() => {
                  setQueryDraft("");
                  updateUrlState({
                    q: undefined,
                    status: undefined,
                    page: undefined,
                  });
                }}
              >
                {t("projectPortfolio.clearFilters")}
              </Button>
            ) : (
              <Button
                variant="primary"
                className="pm-button pm-button-primary"
                onClick={() => navigate("/projects/new")}
              >
                <IconPlus size={16} />
                {t("projectPortfolio.createProject")}
              </Button>
            )
          }
        />
      ) : (
        <>
          <section className={`pm-projects pm-projects-${view}`}>
            {visibleProjects.map((project) => (
              <ProjectCard
                key={project.id}
                className="pm-project-card"
                role="link"
                onClick={() => navigate(`/projects/${project.id}`)}
                tabIndex={0}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    navigate(`/projects/${project.id}`);
                  }
                }}
              >
                <div className="pm-project-icon">
                  <IconFolder size={19} />
                </div>
                <div className="pm-project-main">
                  <div className="pm-project-title">
                    <h2>{project.name}</h2>
                    <ProjectStatusBadge
                      className={`pm-status pm-status-${project.status}`}
                      tone={statusTones[project.status]}
                    >
                      {t(`projectPortfolio.status.${project.status}`)}
                    </ProjectStatusBadge>
                  </div>
                  <p>{project.description || project.objective}</p>
                  <div className="pm-project-meta-mobile">
                    <span>
                      {project.visibility === "private"
                        ? t("projectPortfolio.private")
                        : t("projectPortfolio.shared")}
                    </span>
                    <span>{timeAgo(project.updated_at, t, i18n.language)}</span>
                  </div>
                </div>
                <div className="pm-project-team">
                  <AgentStack
                    project={project}
                    ariaLabel={t("projectPortfolio.digitalEmployeeCount", {
                      count: project.members?.length ?? 0,
                    })}
                  />
                  <small>
                    {t("projectPortfolio.digitalEmployeeCount", {
                      count: project.members?.length ?? 0,
                    })}
                  </small>
                </div>
                <div className="pm-project-signal">
                  <strong>
                    {project.current_signal ||
                      (project.status === "running"
                        ? t("projectTerminology.ownerDriving")
                        : t("projectPortfolio.noRunEvents"))}
                  </strong>
                  <small>
                    {project.next_action ||
                      t("projectTerminology.ownerSummary", {
                        name: project.leader_name || t("common.notSpecified"),
                      })}
                  </small>
                </div>
                <ProjectProgressBar
                  className="pm-project-progress"
                  value={project.progress}
                />
                <div className="pm-project-update">
                  <strong>
                    {timeAgo(project.updated_at, t, i18n.language)}
                  </strong>
                  <small>
                    {project.visibility === "private" ? (
                      <IconLock size={13} />
                    ) : (
                      <IconUsers size={13} />
                    )}
                    {project.visibility === "private"
                      ? t("projectPortfolio.private")
                      : project.shared_with_names?.join(
                          t("projectPortfolio.nameSeparator"),
                        ) || t("projectPortfolio.shared")}
                  </small>
                </div>
                <IconArrowRight className="pm-project-arrow" size={17} />
              </ProjectCard>
            ))}
          </section>
          <Pagination
            page={page}
            pageSize={pageSize}
            total={projects.length}
            onPageChange={setPage}
          />
        </>
      )}
    </main>
  );
}
