import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { IconShieldCheck } from "@tabler/icons-react";

import { enterpriseApi } from "../../../services/api";
import Pagination from "../../../components/Pagination";
import ToolsTab from "../../../pages/agent-detail/tabs/ToolsTab";
import SkillsTab from "../../../pages/agent-detail/tabs/SkillsTab";
import ProjectAgentSettingsPanel, {
  type ProjectAgentSettingsSection,
} from "../components/ProjectAgentSettingsPanel";
import { Button, SearchInput } from "../components/ProjectUI";
import { projectUserFacingCopy } from "../projectUserFacingCopy";
import type {
  ProjectAgentOption,
  ProjectAgentSettingsDraft,
  ProjectAgentToolOption,
  ProjectCapabilityOption,
  ProjectTemplate,
} from "../types";
import {
  AgentAvatar,
  initialAgentSettings,
  type Draft,
  type PatchDraft,
  StepTitle,
} from "./shared";

export function CapabilitiesStep({
  agents,
  projectCapabilities,
  inheritedCapabilities,
  tools,
  template,
  draft,
  patch,
}: {
  agents: ProjectAgentOption[];
  projectCapabilities: ProjectCapabilityOption[];
  inheritedCapabilities: ProjectCapabilityOption[];
  tools: ProjectAgentToolOption[];
  template?: ProjectTemplate;
  draft: Draft;
  patch: PatchDraft;
}) {
  const { t } = useTranslation();
  const [activeAgentId, setActiveAgentId] = useState(
    () => draft.leaderId || agents[0]?.id || "",
  );
  const [capabilityTab, setCapabilityTab] =
    useState<ProjectAgentSettingsSection>("config");
  const [agentQuery, setAgentQuery] = useState("");
  const [agentPage, setAgentPage] = useState(1);
  const agentPageSize = 6;
  const modelsQuery = useQuery({
    queryKey: ["llm-models", "project-create"],
    queryFn: enterpriseApi.llmModels,
  });
  useEffect(() => {
    if (!agents.some((agent) => agent.id === activeAgentId)) {
      setActiveAgentId(draft.leaderId || agents[0]?.id || "");
    }
  }, [activeAgentId, agents, draft.leaderId]);
  const allCapabilities = [...projectCapabilities, ...inheritedCapabilities];
  const activeAgent = agents.find((agent) => agent.id === activeAgentId);
  const activeSettings = activeAgent
    ? draft.agentSettings[activeAgent.id] ||
      initialAgentSettings(activeAgent, tools)
    : null;
  const visibleCapabilities = allCapabilities.filter(
    (capability) =>
      capability.source === "project" ||
      capability.owner_agent_id === activeAgentId,
  );
  const updateActiveSettings = (next: ProjectAgentSettingsDraft) => {
    if (!activeAgent) return;
    patch("agentSettings", {
      ...draft.agentSettings,
      [activeAgent.id]: next,
    });
  };
  const matchingAgents = agents.filter((agent) => {
    const query = agentQuery.trim().toLocaleLowerCase();
    return !query || [agent.name, agent.role_description]
      .filter(Boolean)
      .join(" ")
      .toLocaleLowerCase()
      .includes(query);
  });
  const agentPageCount = Math.max(
    1,
    Math.ceil(matchingAgents.length / agentPageSize),
  );
  const currentAgentPage = Math.min(agentPage, agentPageCount);
  const visibleAgents = matchingAgents.slice(
    (currentAgentPage - 1) * agentPageSize,
    currentAgentPage * agentPageSize,
  );
  useEffect(() => setAgentPage(1), [agentQuery]);
  useEffect(() => {
    if (agentPage > agentPageCount) setAgentPage(agentPageCount);
  }, [agentPage, agentPageCount]);
  const modelOptions = [
    { value: "", label: t("projectSnapshot.followSourceAgent") },
    ...((modelsQuery.data || []) as Array<{
      id: string;
      provider: string;
      model: string;
      label?: string;
      enabled?: boolean;
    }>)
      .filter((model) => model.enabled !== false)
      .map((model) => ({
        value: model.id,
        label: model.label || `${model.provider} · ${model.model}`,
      })),
  ];
  if (template?.snapshot_backed) {
    return (
      <div className="pm-step-section">
        <StepTitle
          number="02"
          title={t("projectCreate.capabilities.snapshotTitle")}
          description={t("projectCreate.capabilities.snapshotDescription")}
        />
        <div className="pm-policy-callout pm-policy-success">
          <IconShieldCheck size={18} />
          <div>
            <strong>{t("projectTemplates.restoreSettings")}</strong>
            <p>{t("projectCreate.capabilities.snapshotDetail")}</p>
          </div>
        </div>
      </div>
    );
  }
  return (
    <div className="pm-step-section">
      <StepTitle
        number="02"
        title={t("projectCreate.capabilities.title")}
        description={t("projectCreate.capabilities.description")}
      />
      <section className="pm-capability-picker">
        <SearchInput
          className="pm-capability-agent-search"
          value={agentQuery}
          onChange={(event) => setAgentQuery(event.target.value)}
          placeholder={t("projectCreate.team.searchPlaceholder")}
          aria-label={t("projectCreate.team.searchAria")}
        />
        <div className="pm-selected-agent-list" role="list">
          {visibleAgents.map((agent) => {
            const active = agent.id === activeAgentId;
            return (
              <Button
                key={agent.id}
                type="button"
                variant="ghost"
                className={active ? "is-active" : ""}
                onClick={() => setActiveAgentId(agent.id)}
                aria-pressed={active}
                role="listitem"
              >
                <AgentAvatar agent={agent} />
                <span className="pm-selected-agent-copy">
                  <strong>{agent.name}</strong>
                  <small>{projectUserFacingCopy(agent.role_description, t)}</small>
                </span>
              </Button>
            );
          })}
        </div>
        {matchingAgents.length > agentPageSize ? (
          <Pagination
            className="pm-capability-agent-pagination"
            page={currentAgentPage}
            pageSize={agentPageSize}
            total={matchingAgents.length}
            onPageChange={setAgentPage}
            showJump={false}
            compact
            ariaLabel={t("projectCreate.team.paginationAria")}
          />
        ) : null}
        {activeAgent && activeSettings ? (
          <ProjectAgentSettingsPanel
            className="pm-capability-panel"
            title={activeAgent.name}
            eyebrow={t("projectAgents.teamPage.workSettings")}
            value={capabilityTab}
            onChange={(nextTab) => {
              setCapabilityTab(nextTab);
            }}
            config={activeSettings.config_snapshot}
            onConfigChange={(key, value) =>
              updateActiveSettings({
                ...activeSettings,
                config_snapshot: {
                  ...activeSettings.config_snapshot,
                  [key]: value,
                },
              })
            }
            modelOptions={modelOptions}
            counts={{
              config: Object.values(activeSettings.config_snapshot).filter(Boolean)
                .length,
              tools: activeSettings.tools.length,
              skill: activeSettings.skill_capability_ids.length,
            }}
            tools={
              <ToolsTab
                agentId={activeAgent.id}
                agentName={activeAgent.name}
                canManage
                canConfigure
                draftTools={activeSettings.tools}
                onDraftToolsChange={(nextTools) =>
                  updateActiveSettings({
                    ...activeSettings,
                    tools: nextTools,
                  })
                }
                draftMcpOverrides={activeSettings.mcp_server_overrides}
                onDraftMcpOverridesChange={(mcp_server_overrides) =>
                  updateActiveSettings({
                    ...activeSettings,
                    mcp_server_overrides,
                  })
                }
              />
            }
            skill={
              <SkillsTab
                key={activeAgent.id}
                agentId={activeAgent.id}
                canManage
                scope="project"
                draftCapabilities={visibleCapabilities.filter(
                  (capability) => capability.kind === "skill",
                )}
                selectedCapabilityIds={activeSettings.skill_capability_ids}
                onSelectedCapabilityIdsChange={(skill_capability_ids) =>
                  updateActiveSettings({
                    ...activeSettings,
                    skill_capability_ids,
                  })
                }
              />
            }
          />
        ) : null}
      </section>
    </div>
  );
}
