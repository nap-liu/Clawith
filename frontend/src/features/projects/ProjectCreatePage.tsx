import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  IconAlertTriangle,
  IconArrowLeft,
  IconArrowRight,
  IconCheck,
  IconCrown,
  IconLock,
  IconMessageCircle,
  IconRefresh,
  IconShieldCheck,
  IconUsers,
} from "@tabler/icons-react";

import { projectsApi } from "../../services/projects";
import { enterpriseApi } from "../../services/api";
import { useDialog } from "../../components/Dialog/DialogProvider";
import Pagination from "../../components/Pagination";
import ToolsTab from "../../pages/agent-detail/tabs/ToolsTab";
import SkillsTab from "../../pages/agent-detail/tabs/SkillsTab";
import type {
  ProjectAgentOption,
  ProjectAgentSettingsDraft,
  ProjectAgentToolOption,
  ProjectCapabilityCreateOverride,
  ProjectCapabilityOption,
  ProjectCreatePayload,
  ProjectFromTemplatePayload,
  ProjectTemplate,
  ProjectVisibility,
} from "./types";
import {
  Button,
  ProjectEmptyState,
  ProjectField,
  SearchInput,
  TextInput,
} from "./components/ProjectUI";
import ProjectAgentSettingsPanel, {
  type ProjectAgentSettingsSection,
} from "./components/ProjectAgentSettingsPanel";
import { projectUserFacingCopy } from "./projectUserFacingCopy";
import "./projectPortfolio.css";

type Draft = {
  name: string;
  memberIds: string[];
  leaderId: string;
  sharedCapabilityIds: string[];
  inheritedCapabilityIds: string[];
  agentSettings: Record<string, ProjectAgentSettingsDraft>;
  visibility: ProjectVisibility;
  shareTargets: string[];
};

function defaultProjectName(t: TFunction) {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return t("projectCreate.defaultName", { date: `${month}-${day}` });
}

function initialDraft(t: TFunction): Draft {
  return {
    name: defaultProjectName(t),
    memberIds: [],
    leaderId: "",
    sharedCapabilityIds: [],
    inheritedCapabilityIds: [],
    agentSettings: {},
    visibility: "private",
    shareTargets: [],
  };
}

function initialAgentSettings(
  agent: ProjectAgentOption,
  tools: ProjectAgentToolOption[],
): ProjectAgentSettingsDraft {
  const visibleTools = tools.filter(
    (tool) =>
      !tool.installed_by_agent_id || tool.installed_by_agent_id === agent.id,
  );
  return {
    config_snapshot: {
      primary_model_id: agent.primary_model_id || null,
      fallback_model_id: agent.fallback_model_id || null,
      max_tool_rounds: agent.max_tool_rounds ?? 50,
      project_instruction: "",
    },
    tools: visibleTools.map((tool) => ({
      ...tool,
      agent_config: { ...(tool.agent_config || {}) },
    })),
    mcp_server_overrides: {},
    skill_capability_ids: [],
  };
}

function toggleItem(items: string[], id: string) {
  return items.includes(id)
    ? items.filter((item) => item !== id)
    : [...items, id];
}

function AgentAvatar({ agent }: { agent: ProjectAgentOption }) {
  const [failed, setFailed] = useState(false);
  const [authenticatedAvatarUrl, setAuthenticatedAvatarUrl] = useState<
    string | null
  >(null);
  const avatarUrl = agent.avatar_url || "";
  const requiresAuthentication = avatarUrl.startsWith("/api");

  useEffect(() => {
    setFailed(false);
    setAuthenticatedAvatarUrl(null);
    if (!requiresAuthentication) return;

    const token = localStorage.getItem("token") || "";
    if (!token) {
      setFailed(true);
      return;
    }

    const controller = new AbortController();
    let objectUrl: string | null = null;
    let cancelled = false;
    const requestUrl = new URL(avatarUrl, window.location.origin);
    // Authentication belongs in the request header. Never preserve credentials
    // from stored avatar URLs in a request URI where proxies can log them.
    requestUrl.searchParams.delete("token");
    requestUrl.searchParams.delete("access_token");

    void fetch(`${requestUrl.pathname}${requestUrl.search}`, {
      headers: { Authorization: `Bearer ${token}` },
      credentials: "same-origin",
      signal: controller.signal,
    })
      .then((response) => {
        if (!response.ok)
          throw new Error(`Avatar request failed: ${response.status}`);
        return response.blob();
      })
      .then((blob) => {
        objectUrl = URL.createObjectURL(blob);
        if (cancelled) {
          URL.revokeObjectURL(objectUrl);
          objectUrl = null;
          return;
        }
        setAuthenticatedAvatarUrl(objectUrl);
      })
      .catch((error) => {
        if (!cancelled && error instanceof Error && error.name !== "AbortError") {
          setFailed(true);
        }
      });

    return () => {
      cancelled = true;
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [avatarUrl, requiresAuthentication]);

  const displayUrl = requiresAuthentication ? authenticatedAvatarUrl : avatarUrl;
  return avatarUrl && !failed ? (
    displayUrl ? (
      <img
        className="pm-agent-avatar"
        src={displayUrl}
        alt=""
        onError={() => setFailed(true)}
      />
    ) : (
      <span className="pm-agent-avatar">{agent.name.slice(0, 1)}</span>
    )
  ) : (
    <span className="pm-agent-avatar">{agent.name.slice(0, 1)}</span>
  );
}

export default function ProjectCreatePage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const dialog = useDialog();
  const [searchParams] = useSearchParams();
  const templateId = searchParams.get("template");
  const seededTemplateId = useRef<string | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  const [step, setStep] = useState(1);
  const [draft, setDraft] = useState<Draft>(() => initialDraft(t));
  const [submitError, setSubmitError] = useState("");
  const steps = [
    [t("projectCreate.steps.team"), t("projectCreate.steps.teamDescription")],
    [
      t("projectCreate.steps.capabilities"),
      t("projectCreate.steps.capabilitiesDescription"),
    ],
    [
      t("projectCreate.steps.boundary"),
      t("projectCreate.steps.boundaryDescription"),
    ],
  ] as const;

  const bootstrapQuery = useQuery({
    queryKey: ["projects", "bootstrap-options"],
    queryFn: projectsApi.bootstrapOptions,
    staleTime: 30_000,
  });
  const templateQuery = useQuery({
    queryKey: ["projects", "template", templateId],
    queryFn: () => projectsApi.getTemplate(templateId || ""),
    enabled: Boolean(templateId),
  });
  const createMutation = useMutation({
    mutationFn: (
      request:
        | { mode: "blank"; payload: ProjectCreatePayload }
        | { mode: "template"; payload: ProjectFromTemplatePayload },
    ) =>
      request.mode === "template"
        ? projectsApi.createFromTemplate(request.payload)
        : projectsApi.create(request.payload),
    onSuccess: async (project) => {
      if (project.template_setup_summary) {
        const setup = project.template_setup_summary;
        await dialog.alert(t("projectCreate.templateSetup.message"), {
          title: t("projectCreate.templateSetup.title"),
          type: "success",
          details: t("projectCreate.templateSetup.details", {
            files: setup.restored_file_count,
            employees: setup.restored_digital_employee_count,
            skills: setup.restored_skill_count,
            connections: setup.restored_connection_count,
            tools: setup.restored_tool_count,
          }),
          confirmLabel: t("projectCreate.templateSetup.continue"),
        });
      }
      navigate(`/projects/${project.id}/planning`);
    },
    onError: () =>
      setSubmitError(
        t(
          templateId
            ? "projectCreate.templateFailed"
            : "projectCreate.failed",
        ),
      ),
  });

  useEffect(() => {
    const template = templateQuery.data;
    const availableCapabilities = bootstrapQuery.data?.capabilities;
    if (!template || seededTemplateId.current === template.id) return;
    if (template.snapshot_backed) {
      seededTemplateId.current = template.id;
      setDraft((current) => ({
        ...current,
        name: projectUserFacingCopy(template.name, t),
      }));
      return;
    }
    if (!availableCapabilities) return;
    seededTemplateId.current = template.id;
    const availableSharedCapabilityIds = new Set(
      availableCapabilities
        .filter((capability) => capability.source === "project")
        .map((capability) => capability.capability_id || capability.id),
    );
    const templateCapabilities = [...template.skills, ...template.mcp_servers];
    const sharedCapabilityIds = templateCapabilities
      .map((item) => item.id)
      .filter((id): id is string =>
        Boolean(id && availableSharedCapabilityIds.has(id)),
      );
    setDraft((current) => ({
      ...current,
      name: t("projectCreate.templateProjectName", {
        name: projectUserFacingCopy(template.name, t),
      }),
      sharedCapabilityIds,
    }));
  }, [bootstrapQuery.data?.capabilities, t, templateQuery.data]);

  const options = bootstrapQuery.data;
  const agents = options?.agents ?? [];
  const capabilities = options?.capabilities ?? [];
  const tools = options?.tools ?? [];
  const selectedAgents = agents.filter((agent) =>
    draft.memberIds.includes(agent.id),
  );
  const projectCapabilities = capabilities.filter(
    (capability) => capability.source === "project",
  );
  const inheritedCapabilities = capabilities.filter(
    (capability) =>
      capability.source === "agent" &&
      draft.memberIds.includes(capability.owner_agent_id || ""),
  );
  const leader = selectedAgents.find((agent) => agent.id === draft.leaderId);
  const template = templateQuery.data;
  const restoresSnapshot = Boolean(template?.snapshot_backed);
  const needsBootstrap = !templateId || !restoresSnapshot;

  useEffect(() => {
    if (!options || restoresSnapshot) return;
    setDraft((current) => {
      let changed = false;
      const nextSettings = { ...current.agentSettings };
      for (const agentId of current.memberIds) {
        if (nextSettings[agentId]) continue;
        const agent = options.agents.find((entry) => entry.id === agentId);
        if (!agent) continue;
        nextSettings[agentId] = initialAgentSettings(
          agent,
          options.tools,
        );
        changed = true;
      }
      for (const agentId of Object.keys(nextSettings)) {
        if (!current.memberIds.includes(agentId)) {
          delete nextSettings[agentId];
          changed = true;
        }
      }
      return changed ? { ...current, agentSettings: nextSettings } : current;
    });
  }, [draft.memberIds, options, restoresSnapshot]);

  const validation = useMemo(
    () => [
      Boolean(
        draft.name.trim() &&
        (restoresSnapshot ||
          (draft.memberIds.length &&
            draft.leaderId &&
            draft.memberIds.includes(draft.leaderId))),
      ),
      true,
      draft.visibility === "private" || draft.shareTargets.length > 0,
    ],
    [draft, restoresSnapshot],
  );

  const patchDraft = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((current) => ({ ...current, [key]: value }));

  useEffect(() => {
    contentRef.current?.scrollTo({ top: 0, behavior: "auto" });
  }, [step]);

  const goNext = () => {
    if (validation[step - 1]) setStep((current) => Math.min(3, current + 1));
  };
  const submit = () => {
    if (!validation.every(Boolean) || createMutation.isPending) return;
    setSubmitError("");
    const members = draft.memberIds.map((agentId) => ({
      agent_id: agentId,
      is_leader: agentId === draft.leaderId,
      enabled_inherited_capability_ids: inheritedCapabilities
        .filter(
          (capability) =>
            capability.owner_agent_id === agentId &&
            draft.inheritedCapabilityIds.includes(capability.id),
        )
        .map((capability) => capability.capability_id || capability.id),
      settings: draft.agentSettings[agentId]
        ? {
            config_snapshot: {
              ...draft.agentSettings[agentId].config_snapshot,
              max_tool_rounds: (() => {
                const raw = draft.agentSettings[agentId].config_snapshot.max_tool_rounds;
                if (raw === "" || raw === null || raw === undefined) return null;
                const parsed = Number(raw);
                return Number.isFinite(parsed) ? parsed : null;
              })(),
            },
            tools: draft.agentSettings[agentId].tools.map((tool) => ({
              tool_id: tool.id,
              enabled: tool.can_disable === false ? true : tool.enabled,
              config: tool.agent_config || {},
            })),
            mcp_server_overrides: Object.entries(
              draft.agentSettings[agentId].mcp_server_overrides,
            ).map(([server_id, override]) => {
              const { credential_state: _credentialState, ...persistedOverride } =
                override;
              return { server_id, ...persistedOverride };
            }),
            skill_capability_ids:
              draft.agentSettings[agentId].skill_capability_ids,
          }
        : undefined,
    }));
    const selectedCapabilities = [
      ...projectCapabilities.filter((capability) =>
        draft.sharedCapabilityIds.includes(capability.id),
      ),
      ...inheritedCapabilities.filter((capability) =>
        draft.inheritedCapabilityIds.includes(capability.id),
      ),
    ];
    const capabilityOverrides: ProjectCapabilityCreateOverride[] =
      selectedCapabilities.map((capability) => ({
        capability_type: capability.kind,
        capability_id: capability.capability_id || capability.id,
        capability_name: capability.name,
        source: capability.source === "agent" ? "inherited" : "shared",
        inherited_from_agent_id:
          capability.source === "agent"
            ? capability.owner_agent_id || null
            : null,
        is_enabled: true,
      }));
    const sharedWithUserIds =
      draft.visibility === "shared" ? draft.shareTargets : [];
    const name = draft.name.trim() || defaultProjectName(t);

    if (templateId) {
      const overrides = restoresSnapshot
        ? { shared_with_user_ids: sharedWithUserIds }
        : {
            members,
            capabilities: capabilityOverrides,
            shared_with_user_ids: sharedWithUserIds,
          };
      createMutation.mutate({
        mode: "template",
        payload: {
          template_id: templateId,
          name,
          visibility: draft.visibility,
          overrides,
        },
      });
      return;
    }

    createMutation.mutate({
      mode: "blank",
      payload: {
        name,
        description: "",
        objective: "",
        success_criteria: [],
        members,
        shared_capability_ids: draft.sharedCapabilityIds,
        git: {
          repository_mode: "managed",
          repository_url: null,
          branch_policy: "work_item",
        },
        runtime: {
          mode: "balanced",
          monthly_budget: 300,
          approval_policy: "risk",
        },
        visibility: draft.visibility,
        shared_with_user_ids: sharedWithUserIds,
      },
    });
  };

  if (
    (templateId && templateQuery.isPending) ||
    (needsBootstrap && bootstrapQuery.isPending)
  ) {
    return (
      <main className="pm-page">
        <div className="pm-state pm-state-full">
          <span className="pm-spinner" />
          <strong>{t("projectCreate.loadingTitle")}</strong>
          <p>{t("projectCreate.loadingDescription")}</p>
        </div>
      </main>
    );
  }
  if ((needsBootstrap && bootstrapQuery.isError) || templateQuery.isError) {
    return (
      <main className="pm-page">
        <ProjectEmptyState
          className="pm-state-full"
          tone="error"
          title={t("projectCreate.loadFailedTitle")}
          description={t("projectCreate.loadFailedDescription")}
          action={
            <Button
              variant="secondary"
              className="pm-button pm-button-secondary"
              onClick={() => {
                bootstrapQuery.refetch();
                templateQuery.refetch();
              }}
            >
              <IconRefresh size={16} />
              {t("projectCreate.reload")}
            </Button>
          }
        />
      </main>
    );
  }

  return (
    <main className="pm-create-page pm-create-page--planning">
      <aside className="pm-create-aside">
        <Button
          variant="ghost"
          className="pm-back-link"
          onClick={() => navigate("/projects")}
        >
          <IconArrowLeft size={16} />
          {t("projectCreate.exit")}
        </Button>
        <div className="pm-create-brand">
          <span>◆</span>
          <div>
            <strong>{t("projectCreate.brand")}</strong>
            <small>{t("projectTerminology.create.planningIntro")}</small>
          </div>
        </div>
        <ol>
          {steps.map((item, index) => {
            const number = index + 1;
            return (
              <li
                key={item[0]}
                className={`${step === number ? "is-active" : ""} ${step > number ? "is-done" : ""}`}
              >
                <Button
                  variant="ghost"
                  onClick={() => {
                    if (
                      number <= step ||
                      validation.slice(0, number - 1).every(Boolean)
                    )
                      setStep(number);
                  }}
                  aria-current={step === number ? "step" : undefined}
                >
                  <i>{step > number ? <IconCheck size={14} /> : number}</i>
                  <span>
                    <strong>{item[0]}</strong>
                    <small>{item[1]}</small>
                  </span>
                </Button>
              </li>
            );
          })}
        </ol>
      </aside>
      <section className="pm-create-main">
        <header>
          <div>
            <span>{t("projectCreate.progress", { step })}</span>
            <strong>{steps[step - 1][0]}</strong>
          </div>
          <small>
            {template
              ? t("projectCreate.basedOn", {
                  name: projectUserFacingCopy(template.name, t),
                  version: template.version,
                })
              : t("projectCreate.minimalSetup")}
          </small>
        </header>
        <div ref={contentRef} className="pm-create-content">
          {step === 1 && (
            <TeamStep
              draft={draft}
              template={template}
              agents={agents}
              patch={patchDraft}
            />
          )}
          {step === 2 && (
            <CapabilitiesStep
              agents={selectedAgents}
              projectCapabilities={projectCapabilities}
              inheritedCapabilities={inheritedCapabilities}
              tools={tools}
              template={template}
              draft={draft}
              patch={patchDraft}
            />
          )}
          {step === 3 && (
            <BoundaryStep
              draft={draft}
              selectedAgents={selectedAgents}
              leader={leader}
              shareTargets={options?.users ?? []}
              capabilityCount={
                Object.values(draft.agentSettings).reduce(
                  (count, settings) =>
                    count +
                    settings.tools.filter(
                      (tool) => tool.type === "mcp" && tool.enabled,
                    ).length +
                    settings.skill_capability_ids.length,
                  0,
                )
              }
              template={template}
              patch={patchDraft}
            />
          )}
        </div>
        <footer>
          <Button
            variant="secondary"
            className="pm-button pm-button-secondary"
            disabled={step === 1 || createMutation.isPending}
            onClick={() => setStep((current) => current - 1)}
          >
            {t("projectCreate.previous")}
          </Button>
          <div>
            {!validation[step - 1] && (
              <span>
                <IconAlertTriangle size={14} />
                {t("projectCreate.requiredHint")}
              </span>
            )}
            {submitError && (
              <span className="pm-submit-error">
                <IconAlertTriangle size={14} />
                {submitError}
              </span>
            )}
          </div>
          {step < 3 ? (
            <Button
              variant="primary"
              className="pm-button pm-button-primary"
              disabled={!validation[step - 1]}
              onClick={goNext}
            >
              {t("projectCreate.continue")}
              <IconArrowRight size={16} />
            </Button>
          ) : (
            <Button
              variant="primary"
              className="pm-button pm-button-primary"
              disabled={!validation.every(Boolean) || createMutation.isPending}
              onClick={submit}
            >
              {createMutation.isPending ? (
                <>
                  <span className="pm-spinner pm-spinner-small" />
                  {t(
                    templateId
                      ? "projectCreate.templateRestoring"
                      : "projectCreate.creating",
                  )}
                </>
              ) : (
                <>
                  {t(
                    templateId
                      ? "projectCreate.useTemplate"
                      : "projectTerminology.create.createAndPlan",
                  )}
                  <IconMessageCircle size={16} />
                </>
              )}
            </Button>
          )}
        </footer>
      </section>
    </main>
  );
}

type PatchDraft = <K extends keyof Draft>(key: K, value: Draft[K]) => void;

function StepTitle({
  number,
  title,
  description,
}: {
  number: string;
  title: string;
  description: string;
}) {
  return (
    <div className="pm-step-title">
      <span>{number}</span>
      <div>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
    </div>
  );
}

function TeamStep({
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

function CapabilitiesStep({
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
  const agentPageCount = Math.max(1, Math.ceil(matchingAgents.length / agentPageSize));
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

function BoundaryStep({
  draft,
  selectedAgents,
  leader,
  shareTargets,
  capabilityCount,
  template,
  patch,
}: {
  draft: Draft;
  selectedAgents: ProjectAgentOption[];
  leader?: ProjectAgentOption;
  shareTargets: Array<{ id: string; name: string; email?: string | null }>;
  capabilityCount: number;
  template?: ProjectTemplate;
  patch: PatchDraft;
}) {
  const { t } = useTranslation();
  const [shareSearch, setShareSearch] = useState("");
  const [sharePage, setSharePage] = useState(1);
  const sharePageSize = 10;
  const normalizedShareSearch = shareSearch.trim().toLocaleLowerCase();
  const filteredShareTargets = useMemo(
    () =>
      shareTargets.filter((target) =>
        `${target.name} ${target.email || ""}`
          .toLocaleLowerCase()
          .includes(normalizedShareSearch),
      ),
    [normalizedShareSearch, shareTargets],
  );
  const shareTotalPages = Math.max(
    1,
    Math.ceil(filteredShareTargets.length / sharePageSize),
  );
  const currentSharePage = Math.min(sharePage, shareTotalPages);
  const visibleShareTargets = filteredShareTargets.slice(
    (currentSharePage - 1) * sharePageSize,
    currentSharePage * sharePageSize,
  );

  useEffect(() => setSharePage(1), [normalizedShareSearch]);
  useEffect(() => {
    if (sharePage > shareTotalPages) setSharePage(shareTotalPages);
  }, [sharePage, shareTotalPages]);

  return (
    <div className="pm-step-section">
      <StepTitle
        number="03"
        title={t("projectCreate.boundary.title")}
        description={t("projectCreate.boundary.description")}
      />
      <div className="pm-visibility-options">
        <Button
          variant="ghost"
          type="button"
          className={draft.visibility === "private" ? "is-selected" : ""}
          onClick={() => patch("visibility", "private")}
          aria-pressed={draft.visibility === "private"}
        >
          <IconLock size={21} />
          <span>
            <strong>{t("projectCreate.boundary.private")}</strong>
            <small>{t("projectCreate.boundary.privateHint")}</small>
          </span>
          <i>{draft.visibility === "private" && <IconCheck size={14} />}</i>
        </Button>
        <Button
          variant="ghost"
          type="button"
          className={draft.visibility === "shared" ? "is-selected" : ""}
          onClick={() => patch("visibility", "shared")}
          aria-pressed={draft.visibility === "shared"}
        >
          <IconUsers size={21} />
          <span>
            <strong>{t("projectCreate.boundary.shared")}</strong>
            <small>{t("projectCreate.boundary.sharedHint")}</small>
          </span>
          <i>{draft.visibility === "shared" && <IconCheck size={14} />}</i>
        </Button>
      </div>
      {draft.visibility === "shared" && (
        <section className="pm-share-box">
          <header>
            <strong>{t("projectCreate.boundary.shareMembers")}</strong>
            <small>{t("projectCreate.boundary.shareHint")}</small>
          </header>
          <SearchInput
            className="pm-share-search"
            value={shareSearch}
            onChange={(event) => setShareSearch(event.target.value)}
            placeholder={t("projectCreate.boundary.searchMembers")}
            aria-label={t("projectCreate.boundary.searchMembersAria")}
          />
          {shareTargets.length ? (
            visibleShareTargets.length ? (
              <>
                <div className="pm-share-targets">
              {visibleShareTargets.map((target) => {
                const selected = draft.shareTargets.includes(target.id);
                return (
                  <Button
                    variant="ghost"
                    type="button"
                    key={target.id}
                    className={selected ? "is-selected" : ""}
                    onClick={() =>
                      patch(
                        "shareTargets",
                        toggleItem(draft.shareTargets, target.id),
                      )
                    }
                    aria-pressed={selected}
                  >
                    <i>{selected && <IconCheck size={12} />}</i>
                    <span>
                      <strong>{target.name}</strong>
                      <small>{target.email || target.id}</small>
                    </span>
                  </Button>
                );
              })}
                </div>
                {filteredShareTargets.length > sharePageSize ? (
                  <Pagination
                    className="pm-share-pagination"
                    page={currentSharePage}
                    pageSize={sharePageSize}
                    total={filteredShareTargets.length}
                    onPageChange={setSharePage}
                    showJump={false}
                    compact
                    ariaLabel={t("projectCreate.boundary.paginationAria")}
                  />
                ) : null}
              </>
            ) : (
              <div className="pm-inline-empty">
                <p>{t("projectCreate.boundary.noSearchResults")}</p>
              </div>
            )
          ) : (
            <div className="pm-inline-empty">
              <p>{t("projectCreate.boundary.noShareTargets")}</p>
            </div>
          )}
        </section>
      )}
      <section className="pm-final-review">
        <header>
          <strong>{t("projectCreate.boundary.review")}</strong>
          <small>{t("projectCreate.boundary.reviewHint")}</small>
        </header>
        <dl>
          <div>
            <dt>{t("projectCreate.boundary.project")}</dt>
            <dd>{draft.name}</dd>
          </div>
          <div>
            <dt>{t("projectCreate.boundary.team")}</dt>
            <dd>
              {template?.snapshot_backed
                ? t("projectCreate.boundary.templateTeam", {
                    count:
                      template.asset_summary?.digital_employee_count ??
                      template.roles.length,
                  })
                : `${t("projectTerminology.create.teamOwner", {
                    count: selectedAgents.length,
                  })}${leader?.name || t("projectCreate.boundary.unspecified")}`}
            </dd>
          </div>
          <div>
            <dt>{t("projectCreate.boundary.capabilities")}</dt>
            <dd>
              {template?.snapshot_backed
                ? t("projectCreate.boundary.templateCapabilities")
                : t("projectCreate.boundary.capabilityCount", {
                    count: capabilityCount,
                  })}
            </dd>
          </div>
          <div>
            <dt>{t("projectCreate.boundary.visibility")}</dt>
            <dd>
              {draft.visibility === "private"
                ? t("projectCreate.boundary.private")
                : t("projectCreate.boundary.sharedCount", {
                    count: draft.shareTargets.length,
                  })}
            </dd>
          </div>
          <div className="pm-review-planning">
            <dt>{t("projectCreate.boundary.next")}</dt>
            <dd>{t("projectTerminology.create.nextStep")}</dd>
          </div>
        </dl>
      </section>
      <div className="pm-policy-callout pm-policy-success">
        <IconMessageCircle size={18} />
        <div>
          <strong>{t("projectCreate.boundary.planningTitle")}</strong>
          <p>{t("projectTerminology.create.workspaceNext")}</p>
        </div>
      </div>
    </div>
  );
}
