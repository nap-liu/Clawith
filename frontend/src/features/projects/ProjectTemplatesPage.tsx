import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  IconArrowLeft,
  IconArrowRight,
  IconBolt,
  IconBrandGit,
  IconCheck,
  IconChevronRight,
  IconCode,
  IconRefresh,
  IconSparkles,
  IconUsers,
  IconX,
} from "@tabler/icons-react";
import { projectsApi } from "../../services/projects";
import Pagination from "../../components/Pagination";
import type { ProjectTemplate } from "./types";
import {
  Button,
  ProjectCard,
  ProjectDialog,
  ProjectEmptyState,
  ProjectIconButton,
  SearchInput,
} from "./components/ProjectUI";
import { projectUserFacingCopy } from "./projectUserFacingCopy";
import "./projectPortfolio.css";

function TemplateDetail({
  template,
  onClose,
  onUse,
}: {
  template: ProjectTemplate;
  onClose: () => void;
  onUse: () => void;
}) {
  const { t } = useTranslation();
  return (
    <ProjectDialog
      open
      onClose={onClose}
      ariaLabel={t("projectTemplates.detailAria", {
        name: projectUserFacingCopy(template.name, t),
      })}
      className="pm-modal pm-template-detail"
    >
      <header>
        <div className="pm-template-mark">
          <IconSparkles size={21} />
        </div>
        <div>
          <span>
            {projectUserFacingCopy(template.category, t)} · {template.version}
          </span>
          <h2 id="template-detail-title">
            {projectUserFacingCopy(template.name, t)}
          </h2>
          <p>{projectUserFacingCopy(template.description, t)}</p>
        </div>
        <ProjectIconButton
          onClick={onClose}
          aria-label={t("projectTemplates.close")}
        >
          <IconX size={18} />
        </ProjectIconButton>
      </header>
      <div className="pm-template-detail-stats">
        <div>
          <span>{t("projectTemplates.author")}</span>
          <strong>
            {template.author_name
              ? projectUserFacingCopy(template.author_name, t)
              : t("projectTemplates.platformTemplate")}
          </strong>
        </div>
        <div>
          <span>{t("projectTemplates.used")}</span>
          <strong>
            {t("projectTemplates.usageCount", {
              count: template.usage_count,
            })}
          </strong>
        </div>
        <div>
          <span>{t("projectTemplates.snapshotVersion")}</span>
          <strong>{template.version}</strong>
        </div>
      </div>
      <section>
        <h3>{t("projectTemplates.teamRoles")}</h3>
        <div className="pm-role-list">
          {template.roles.map((role) => (
            <div key={role.key}>
              <IconUsers size={16} />
              <span>
                <strong>{projectUserFacingCopy(role.name, t)}</strong>
                <small>
                  {role.description
                    ? projectUserFacingCopy(role.description, t)
                    : t(
                        template.snapshot_backed
                          ? "projectTemplates.roleFallback"
                          : "projectTemplates.roleFallbackLegacy",
                      )}
                </small>
              </span>
              {role.required && <em>{t("projectTemplates.required")}</em>}
            </div>
          ))}
        </div>
      </section>
      <section className="pm-template-capabilities">
        <div>
          <h3>{t("projectTemplates.sharedSkill")}</h3>
          <div className="pm-chip-row">
            {template.skills.length ? (
              template.skills.map((item) => (
                <span key={item.id || item.name}>
                  <IconBolt size={14} />
                  {projectUserFacingCopy(item.name, t)}
                  {item.version && <small>{item.version}</small>}
                </span>
              ))
            ) : (
              <p>{t("projectTemplates.noSkill")}</p>
            )}
          </div>
        </div>
        <div>
          <h3>{t("projectTemplates.sharedMcp")}</h3>
          <div className="pm-chip-row">
            {template.mcp_servers.length ? (
              template.mcp_servers.map((item) => (
                <span key={item.id || item.name}>
                  <IconCode size={14} />
                  {projectUserFacingCopy(item.name, t)}
                  {item.version && <small>{item.version}</small>}
                </span>
              ))
            ) : (
              <p>{t("projectTemplates.noMcp")}</p>
            )}
          </div>
        </div>
      </section>
      <section>
        <h3>{t("projectTemplates.configuration")}</h3>
        <ul className="pm-check-list">
          {template.snapshot_backed ? (
            <>
              <li>
                <IconCheck size={15} />
                {t("projectTemplates.restoreFiles", {
                  count:
                    template.asset_summary?.total_file_count ??
                    template.asset_summary?.file_count ??
                    0,
                })}
              </li>
              <li>
                <IconCheck size={15} />
                {t("projectTemplates.restoreEmployees", {
                  count:
                    template.asset_summary?.digital_employee_count ??
                    template.roles.length,
                })}
              </li>
              <li>
                <IconCheck size={15} />
                {t("projectTemplates.restoreSettings")}
              </li>
            </>
          ) : (
            <>
              <li>
                <IconCheck size={15} />
                {t("projectTemplates.legacyConfiguration")}
              </li>
              <li>
                <IconCheck size={15} />
                {t("projectTemplates.membersAdjustable")}
              </li>
            </>
          )}
          <li>
            <IconCheck size={15} />
            {t("projectTemplates.versionRecovery")}
          </li>
        </ul>
      </section>
      <footer>
        <Button
          variant="secondary"
          className="pm-button pm-button-secondary"
          onClick={onClose}
        >
          {t("projectTemplates.cancel")}
        </Button>
        <Button
          variant="primary"
          className="pm-button pm-button-primary"
          onClick={onUse}
        >
          {t("projectTemplates.useThis")}
          <IconArrowRight size={16} />
        </Button>
      </footer>
    </ProjectDialog>
  );
}

export default function ProjectTemplatesPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const category = searchParams.get("category") || "all";
  const query = searchParams.get("q")?.trim() || "";
  const [queryDraft, setQueryDraft] = useState(query);
  const selectedId = searchParams.get("template");

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
  const templatesQuery = useQuery({
    queryKey: ["projects", "templates", "catalog"],
    queryFn: () => projectsApi.listTemplates(),
  });
  const detailQuery = useQuery({
    queryKey: ["projects", "template", selectedId],
    queryFn: () => projectsApi.getTemplate(selectedId || ""),
    enabled: Boolean(selectedId),
  });
  const allTemplates = templatesQuery.data ?? [];
  const categories = useMemo(
    () => [
      "all",
      ...Array.from(new Set(allTemplates.map((item) => item.category))),
    ],
    [allTemplates],
  );
  const templates = useMemo(() => {
    const normalizedQuery = query.toLocaleLowerCase();
    return allTemplates.filter((template) => {
      if (category !== "all" && template.category !== category) return false;
      if (!normalizedQuery) return true;
      return [
        projectUserFacingCopy(template.name, t),
        projectUserFacingCopy(template.category, t),
        projectUserFacingCopy(template.description, t),
        ...template.roles.map((role) => projectUserFacingCopy(role.name, t)),
        ...template.skills.map((skill) => projectUserFacingCopy(skill.name, t)),
        ...template.mcp_servers.map((mcp) =>
          projectUserFacingCopy(mcp.name, t),
        ),
      ]
        .join(" ")
        .toLocaleLowerCase()
        .includes(normalizedQuery);
    });
  }, [allTemplates, category, query, t]);
  const pageSize = 12;
  const requestedPage = Number.parseInt(searchParams.get("page") || "1", 10);
  const totalPages = Math.max(1, Math.ceil(templates.length / pageSize));
  const page = Math.min(
    Math.max(1, Number.isFinite(requestedPage) ? requestedPage : 1),
    totalPages,
  );
  const visibleTemplates = templates.slice(
    (page - 1) * pageSize,
    page * pageSize,
  );
  const setPage = (nextPage: number) =>
    updateUrlState({ page: nextPage > 1 ? String(nextPage) : undefined });
  const featured =
    allTemplates.find((item) => item.featured) || allTemplates[0];

  const useTemplate = (id: string) =>
    navigate(`/projects/new?template=${encodeURIComponent(id)}`);

  return (
    <main className="pm-page pm-template-market">
      <Button
        variant="ghost"
        className="pm-back-link"
        onClick={() => navigate("/projects")}
      >
        <IconArrowLeft size={16} />
        {t("projectTemplates.back")}
      </Button>
      <header className="pm-page-header">
        <div>
          <span className="pm-eyebrow">{t("projectTemplates.eyebrow")}</span>
          <h1>{t("projectTemplates.title")}</h1>
          <p>{t("projectTemplates.description")}</p>
        </div>
        <Button
          variant="primary"
          className="pm-button pm-button-primary"
          onClick={() => navigate("/projects/new")}
        >
          {t("projectTemplates.createBlank")}
          <IconArrowRight size={16} />
        </Button>
      </header>

      {featured && (
        <section className="pm-market-hero">
          <div className="pm-market-copy">
            <span>
              {t("projectTemplates.featuredLabel", {
                category: projectUserFacingCopy(featured.category, t),
              })}
            </span>
            <h2>
              {t("projectTemplates.heroTitleFirst")}
              <br />
              {t("projectTemplates.heroTitleSecond")}
            </h2>
            <p>
              {t("projectTemplates.heroDescription", {
                description: projectUserFacingCopy(featured.description, t),
              })}
            </p>
            <Button
              variant="primary"
              className="pm-button pm-button-primary"
              onClick={() => updateUrlState({ template: featured.id })}
            >
              {t("projectTemplates.viewFeatured")}
              <IconArrowRight size={16} />
            </Button>
          </div>
          <div
            className="pm-blueprint"
            aria-label={t("projectTemplates.blueprintAria")}
          >
            <div className="pm-blueprint-line" />
            {[
              [
                "01",
                t("projectTemplates.blueprint.goal"),
                t("projectTemplates.blueprint.success"),
              ],
              [
                "02",
                t("projectTemplates.blueprint.team"),
                t("projectTemplates.blueprint.employees"),
              ],
              [
                "03",
                t("projectTemplates.blueprint.capabilities"),
                t("projectTemplates.blueprint.skillMcp"),
              ],
              [
                "04",
                t("projectTemplates.blueprint.delivery"),
                t("projectTemplates.blueprint.gitOutputs"),
              ],
            ].map((item) => (
              <article key={item[0]}>
                <i>{item[0]}</i>
                <strong>{item[1]}</strong>
                <small>{item[2]}</small>
              </article>
            ))}
          </div>
        </section>
      )}

      <form
        className="pm-market-controls"
        onSubmit={(event) => {
          event.preventDefault();
          updateUrlState({
            q: queryDraft.trim() || undefined,
            page: undefined,
          });
        }}
      >
        <SearchInput
          className="pm-search"
          value={queryDraft}
          onChange={(event) => setQueryDraft(event.target.value)}
          placeholder={t("projectTemplates.searchPlaceholder")}
          aria-label={t("projectTemplates.searchAria")}
        />
        <div className="pm-category-row">
          {categories.map((item) => (
            <Button
              variant="ghost"
              type="button"
              key={item}
              className={category === item ? "is-active" : ""}
              onClick={() => {
                updateUrlState({
                  category: item === "all" ? undefined : item,
                  page: undefined,
                });
              }}
            >
              {item === "all"
                ? t("projectTemplates.all")
                : projectUserFacingCopy(item, t)}
            </Button>
          ))}
        </div>
      </form>

      <div className="pm-market-heading">
        <div>
          <strong>
            {category === "all"
              ? t("projectTemplates.all")
              : projectUserFacingCopy(category, t)}
          </strong>
          <span>
            {t("projectTemplates.results", { count: templates.length })}
          </span>
        </div>
      </div>
      {templatesQuery.isPending ? (
        <div className="pm-state">
          <span className="pm-spinner" />
          <strong>{t("projectTemplates.loading")}</strong>
        </div>
      ) : templatesQuery.isError ? (
        <ProjectEmptyState
          tone="error"
          title={t("projectTemplates.loadFailed")}
          description={
            templatesQuery.error instanceof Error
              ? templatesQuery.error.message
              : t("projectTemplates.serviceUnavailable")
          }
          action={
            <Button
              variant="secondary"
              className="pm-button pm-button-secondary"
              onClick={() => templatesQuery.refetch()}
            >
              <IconRefresh size={16} />
              {t("projectTemplates.reload")}
            </Button>
          }
        />
      ) : templates.length === 0 ? (
        <ProjectEmptyState
          icon={<IconSparkles size={23} />}
          title={t("projectTemplates.emptyTitle")}
          description={t("projectTemplates.emptyDescription")}
          action={
            <Button
              variant="secondary"
              className="pm-button pm-button-secondary"
              onClick={() => {
                setQueryDraft("");
                updateUrlState({
                  q: undefined,
                  category: undefined,
                  page: undefined,
                });
              }}
            >
              {t("projectTemplates.clearFilters")}
            </Button>
          }
        />
      ) : (
        <>
          <section className="pm-template-grid">
            {visibleTemplates.map((template) => (
              <ProjectCard
                className="pm-template-card"
                key={template.id}
                role="button"
                tabIndex={0}
                onClick={() => updateUrlState({ template: template.id })}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    updateUrlState({ template: template.id });
                  }
                }}
              >
                <header>
                  <span className="pm-template-mark">
                    <IconSparkles size={19} />
                  </span>
                  <div>
                    <span>{projectUserFacingCopy(template.category, t)}</span>
                    <small>{template.version}</small>
                  </div>
                  {template.featured && (
                    <em>{t("projectTemplates.featured")}</em>
                  )}
                </header>
                <h2>{projectUserFacingCopy(template.name, t)}</h2>
                <p>{projectUserFacingCopy(template.description, t)}</p>
                <div className="pm-template-roleline">
                  <IconUsers size={14} />
                  {template.roles
                    .slice(0, 3)
                    .map((role) => projectUserFacingCopy(role.name, t))
                    .join(" · ")}
                  {template.roles.length > 3 && (
                    <em>+{template.roles.length - 3}</em>
                  )}
                </div>
                <div className="pm-template-tags">
                  <span>
                    <IconBolt size={13} />
                    {template.skills.length} Skill
                  </span>
                  <span>
                    <IconBrandGit size={13} />
                    {template.mcp_servers.length} MCP
                  </span>
                  {template.snapshot_backed && template.asset_summary && (
                    <>
                      <span>
                        <IconBrandGit size={13} />
                        {t("projectTemplates.fileCount", {
                          count:
                            template.asset_summary.total_file_count ??
                            template.asset_summary.file_count,
                        })}
                      </span>
                      <span>
                        <IconUsers size={13} />
                        {t("projectTemplates.employeeCount", {
                          count: template.asset_summary.digital_employee_count,
                        })}
                      </span>
                    </>
                  )}
                </div>
                <footer>
                  <span>
                    {template.author_name
                      ? projectUserFacingCopy(template.author_name, t)
                      : t("projectTemplates.platformTemplate")}{" "}
                    {" · "}
                    {t("projectTemplates.usageCount", {
                      count: template.usage_count,
                    })}
                  </span>
                  <Button
                    variant="ghost"
                    onClick={(event) => {
                      event.stopPropagation();
                      useTemplate(template.id);
                    }}
                  >
                    {t("projectTemplates.use")}
                    <IconChevronRight size={14} />
                  </Button>
                </footer>
              </ProjectCard>
            ))}
          </section>
          <Pagination
            page={page}
            pageSize={pageSize}
            total={templates.length}
            onPageChange={setPage}
          />
        </>
      )}

      {selectedId && detailQuery.isPending && (
        <ProjectDialog
          open
          onClose={() => updateUrlState({ template: undefined })}
          ariaLabel={t("projectTemplates.loadingDetail")}
          className="pm-modal pm-modal-loading"
        >
          <span className="pm-spinner" />
          {t("projectTemplates.loadingDetail")}
        </ProjectDialog>
      )}
      {selectedId && detailQuery.isError && (
        <ProjectDialog
          open
          onClose={() => updateUrlState({ template: undefined })}
          ariaLabel={t("projectTemplates.detailLoadFailed")}
          className="pm-modal pm-modal-error"
        >
          <ProjectIconButton
            onClick={() => updateUrlState({ template: undefined })}
            aria-label={t("projectTemplates.close")}
          >
            <IconX size={18} />
          </ProjectIconButton>
          <strong>{t("projectTemplates.detailLoadFailed")}</strong>
          <p>
            {detailQuery.error instanceof Error
              ? detailQuery.error.message
              : t("projectTemplates.tryAgain")}
          </p>
          <Button
            variant="secondary"
            className="pm-button pm-button-secondary"
            onClick={() => detailQuery.refetch()}
          >
            <IconRefresh size={16} />
            {t("projectTemplates.reload")}
          </Button>
        </ProjectDialog>
      )}
      {selectedId && detailQuery.data && (
        <TemplateDetail
          template={detailQuery.data}
          onClose={() => updateUrlState({ template: undefined })}
          onUse={() => useTemplate(detailQuery.data.id)}
        />
      )}
    </main>
  );
}
