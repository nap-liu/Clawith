import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  IconCheck,
  IconCrown,
  IconMessageCircle,
  IconShieldCheck,
} from "@tabler/icons-react";

import Pagination from "../../../components/Pagination";
import {
  Button,
  ProjectEmptyState,
  ProjectField,
  SearchInput,
  TextInput,
} from "../components/ProjectUI";
import { projectUserFacingCopy } from "../projectUserFacingCopy";
import type { ProjectAgentOption, ProjectTemplate } from "../types";
import {
  AgentAvatar,
  defaultProjectName,
  type Draft,
  type PatchDraft,
  StepTitle,
  toggleItem,
} from "./shared";

export function TeamStep({
  draft,
  template,
  agents,
  patch,
}: {
  draft: Draft;
  template?: ProjectTemplate;
  agents: ProjectAgentOption[];
  patch: PatchDraft;
}) {
  const { t } = useTranslation();
  const [agentQuery, setAgentQuery] = useState("");
  const [agentPage, setAgentPage] = useState(1);
  const agentPageSize = 8;
  const restoresSnapshot = Boolean(template?.snapshot_backed);
  const selectedTeamAgents = useMemo(
    () => agents.filter((agent) => draft.memberIds.includes(agent.id)),
    [agents, draft.memberIds],
  );
  const matchingAvailableAgents = useMemo(() => {
    const query = agentQuery.trim().toLocaleLowerCase();
    return agents.filter(
      (agent) =>
        !draft.memberIds.includes(agent.id) &&
        (!query ||
          [agent.name, agent.role_description]
            .filter(Boolean)
            .join(" ")
            .toLocaleLowerCase()
            .includes(query)),
    );
  }, [agentQuery, agents, draft.memberIds]);
  const agentPageCount = Math.max(
    1,
    Math.ceil(matchingAvailableAgents.length / agentPageSize),
  );
  const visibleAgents = matchingAvailableAgents.slice(
    (agentPage - 1) * agentPageSize,
    agentPage * agentPageSize,
  );
  useEffect(() => setAgentPage(1), [agentQuery]);
  useEffect(() => {
    if (agentPage > agentPageCount) setAgentPage(agentPageCount);
  }, [agentPage, agentPageCount]);

  const renderAgentCard = (agent: ProjectAgentOption) => {
    const selected = draft.memberIds.includes(agent.id);
    const isLeader = draft.leaderId === agent.id;
    const toggleAgent = () => {
      const next = toggleItem(draft.memberIds, agent.id);
      patch("memberIds", next);
      if (!next.includes(draft.leaderId)) patch("leaderId", next[0] || "");
    };
    return (
      <article key={agent.id} className={selected ? "is-selected" : ""}>
        <Button
          variant="ghost"
          type="button"
          className="pm-agent-select"
          onClick={toggleAgent}
          aria-pressed={selected}
        >
          <i>{selected && <IconCheck size={13} />}</i>
          <AgentAvatar agent={agent} />
          <span className="pm-agent-select__copy">
            <strong>{agent.name}</strong>
            <small>{projectUserFacingCopy(agent.role_description, t)}</small>
          </span>
          <em>
            {t("projectCreate.team.capabilityCount", {
              count: (agent.skill_count || 0) + (agent.mcp_count || 0),
            })}
          </em>
        </Button>
        {selected && (
          <Button
            variant="ghost"
            type="button"
            className={`pm-leader-select ${isLeader ? "is-active" : ""}`}
            onClick={() => patch("leaderId", agent.id)}
            aria-pressed={isLeader}
          >
            <IconCrown size={14} />
            {isLeader
              ? t("projectTerminology.projectOwner")
              : t("projectTerminology.setOwner")}
          </Button>
        )}
      </article>
    );
  };
  return (
    <div className="pm-step-section">
      <StepTitle
        number="01"
        title={t(
          restoresSnapshot
            ? "projectCreate.team.snapshotTitle"
            : "projectCreate.team.title",
        )}
        description={
          restoresSnapshot
            ? t("projectCreate.team.snapshotDescription")
            : t("projectTerminology.create.teamDescription")
        }
      />
      <div className="pm-form-grid pm-create-minimal-name">
        <ProjectField
          className="pm-span-2"
          label={t("projectCreate.team.projectName")}
          labelFor="project-name"
          hint={t("projectCreate.team.projectNameHint")}
        >
          <TextInput
            id="project-name"
            value={draft.name}
            onChange={(event) => patch("name", event.target.value)}
            placeholder={defaultProjectName(t)}
          />
        </ProjectField>
        {template && (
          <ProjectField
            className="pm-span-2"
            label={t("projectCreate.team.startingTemplate")}
          >
            <div className="pm-static-field">
              <strong>{projectUserFacingCopy(template.name, t)}</strong>
              <small>
                {t(
                  template.snapshot_backed
                    ? "projectCreate.templateSnapshotHint"
                    : "projectCreate.templateLegacyHint",
                  {
                    version: template.version,
                  },
                )}
              </small>
            </div>
          </ProjectField>
        )}
      </div>
      {restoresSnapshot && template?.asset_summary ? (
        <div className="pm-policy-callout pm-policy-success">
          <IconShieldCheck size={18} />
          <div>
            <strong>
              {t("projectCreate.team.snapshotEmployees", {
                count: template.asset_summary.digital_employee_count,
              })}
            </strong>
            <p>
              {t("projectCreate.team.snapshotFiles", {
                count:
                  template.asset_summary.total_file_count ??
                  template.asset_summary.file_count,
              })}
            </p>
          </div>
        </div>
      ) : null}
      {!restoresSnapshot && (
        <>
          <div className="pm-planning-intro">
            <IconMessageCircle size={19} />
            <div>
              <strong>{t("projectCreate.team.planningTitle")}</strong>
              <p>{t("projectTerminology.create.planningDescription")}</p>
            </div>
          </div>
          {selectedTeamAgents.length ? (
            <section className="pm-selected-agent-section">
              <header>
                <strong>{t("projectCreate.team.selectedPinned")}</strong>
                <span>
                  <small>{t("projectCreate.team.selectedPinnedHint", {
                    count: selectedTeamAgents.length,
                  })}</small>
                  <i />
                  <small>{t("projectCreate.team.a2a")}</small>
                </span>
              </header>
              <div className="pm-agent-grid pm-agent-grid--selected">
                {selectedTeamAgents.map(renderAgentCard)}
              </div>
            </section>
          ) : null}
          <SearchInput
            className="pm-agent-search"
            value={agentQuery}
            onChange={(event) => setAgentQuery(event.target.value)}
            placeholder={t("projectCreate.team.searchPlaceholder")}
            aria-label={t("projectCreate.team.searchAria")}
          />
          {agents.length ? (
            visibleAgents.length ? (
              <>
                <div className="pm-agent-results">
                  <div className="pm-agent-grid">
                    {visibleAgents.map(renderAgentCard)}
                  </div>
                </div>
                {matchingAvailableAgents.length > agentPageSize ? (
                  <Pagination
                    className="pm-agent-pagination"
                    page={agentPage}
                    pageSize={agentPageSize}
                    total={matchingAvailableAgents.length}
                    onPageChange={setAgentPage}
                    showJump={false}
                    compact
                    ariaLabel={t("projectCreate.team.paginationAria")}
                  />
                ) : null}
              </>
            ) : (
              <ProjectEmptyState
                title={t("projectCreate.team.noSearchResults")}
                description={t("projectCreate.team.noSearchResultsHint")}
              />
            )
          ) : (
            <ProjectEmptyState
              title={t("projectCreate.team.emptyTitle")}
              description={t("projectCreate.team.emptyDescription")}
            />
          )}
        </>
      )}
    </div>
  );
}
