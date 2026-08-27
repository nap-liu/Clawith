import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  IconAlertTriangle,
  IconArrowLeft,
  IconArrowRight,
  IconBolt,
  IconCheck,
  IconCode,
  IconCrown,
  IconLock,
  IconMessageCircle,
  IconRefresh,
  IconShieldCheck,
  IconUsers,
} from "@tabler/icons-react";

import { projectsApi } from "../../services/projects";
import { useDialog } from "../../components/Dialog/DialogProvider";
import ToolCatalogPanel from "../../components/tools/ToolCatalogPanel";
import { getLocalizedToolPresentation } from "../../utils/toolPresentation";
import type {
  ProjectAgentOption,
  ProjectCapabilityCreateOverride,
  ProjectCapabilityOption,
  ProjectCreatePayload,
  ProjectFromTemplatePayload,
  ProjectTemplate,
  ProjectVisibility,
} from "./types";
import {
  Button,
  ProjectCountBadge,
  ProjectEmptyState,
  ProjectField,
  ProjectSegmentedControl,
  SearchInput,
  TextInput,
} from "./components/ProjectUI";
import { projectUserFacingCopy } from "./projectUserFacingCopy";
import "./projectPortfolio.css";

type Draft = {
  name: string;
  memberIds: string[];
  leaderId: string;
  sharedCapabilityIds: string[];
  inheritedCapabilityIds: string[];
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
    visibility: "private",
    shareTargets: [],
  };
}

function toggleItem(items: string[], id: string) {
  return items.includes(id)
    ? items.filter((item) => item !== id)
    : [...items, id];
}

function AgentAvatar({ agent }: { agent: ProjectAgentOption }) {
  const [failed, setFailed] = useState(false);
  const token = typeof window === "undefined" ? "" : localStorage.getItem("token") || "";
  const avatarUrl = agent.avatar_url?.startsWith("/api") && token
    ? `${agent.avatar_url}${agent.avatar_url.includes("?") ? "&" : "?"}token=${encodeURIComponent(token)}`
    : agent.avatar_url;
  useEffect(() => setFailed(false), [avatarUrl]);
  return avatarUrl && !failed ? (
    <img
      className="pm-agent-avatar"
      src={avatarUrl}
      alt=""
      onError={() => setFailed(true)}
    />
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
    onError: (error) =>
      setSubmitError(
        error instanceof Error
          ? error.message
          : t(
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
    const error = templateQuery.error || bootstrapQuery.error;
    return (
      <main className="pm-page">
        <ProjectEmptyState
          className="pm-state-full"
          tone="error"
          title={t("projectCreate.loadFailedTitle")}
          description={
            error instanceof Error
              ? error.message
              : t("projectCreate.loadFailedDescription")
          }
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
        <div className="pm-create-content">
          {step === 1 && (
            <TeamStep
              draft={draft}
              template={template}
              agents={agents}
              capabilities={capabilities}
              patch={patchDraft}
            />
          )}
          {step === 2 && (
            <CapabilitiesStep
              agents={selectedAgents}
              projectCapabilities={projectCapabilities}
              inheritedCapabilities={inheritedCapabilities}
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
                draft.sharedCapabilityIds.length +
                draft.inheritedCapabilityIds.length
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
  capabilities,
  patch,
}: {
  draft: Draft;
  template?: ProjectTemplate;
  agents: ProjectAgentOption[];
  capabilities: ProjectCapabilityOption[];
  patch: PatchDraft;
}) {
  const { t } = useTranslation();
  const [agentQuery, setAgentQuery] = useState("");
  const restoresSnapshot = Boolean(template?.snapshot_backed);
  const visibleAgents = useMemo(() => {
    const query = agentQuery.trim().toLocaleLowerCase();
    if (!query) return agents;
    return agents.filter((agent) =>
      [agent.name, agent.role_description]
        .filter(Boolean)
        .join(" ")
        .toLocaleLowerCase()
        .includes(query),
    );
  }, [agentQuery, agents]);
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
          <div className="pm-team-summary">
            <div className="pm-agent-stack">
              {agents
                .filter((agent) => draft.memberIds.includes(agent.id))
                .slice(0, 5)
                .map((agent) => (
                  <AgentAvatar key={agent.id} agent={agent} />
                ))}
            </div>
            <div>
              <strong>
                {t("projectCreate.team.selected", {
                  count: draft.memberIds.length,
                })}
              </strong>
              <small>{t("projectCreate.team.snapshotCreated")}</small>
            </div>
            <span>
              <i />
              {t("projectCreate.team.a2a")}
            </span>
          </div>
          <SearchInput
            className="pm-agent-search"
            value={agentQuery}
            onChange={(event) => setAgentQuery(event.target.value)}
            placeholder={t("projectCreate.team.searchPlaceholder")}
            aria-label={t("projectCreate.team.searchAria")}
          />
          {agents.length ? (
            visibleAgents.length ? (
              <div className="pm-agent-grid">
              {visibleAgents.map((agent) => {
                const selected = draft.memberIds.includes(agent.id);
                const isLeader = draft.leaderId === agent.id;
                const toggleAgent = () => {
                  const next = toggleItem(draft.memberIds, agent.id);
                  patch("memberIds", next);
                  if (!next.includes(draft.leaderId))
                    patch("leaderId", next[0] || "");
                  const ownedCapabilities = capabilities.filter(
                    (capability) =>
                      capability.source === "agent" &&
                      capability.owner_agent_id === agent.id,
                  );
                  const ownedCapabilityIds = ownedCapabilities.map(
                    (capability) => capability.id,
                  );
                  const ownedDefaultIds = ownedCapabilities
                    .filter(
                      (capability) => capability.enabled_by_default !== false,
                    )
                    .map((capability) => capability.id);
                  patch(
                    "inheritedCapabilityIds",
                    selected
                      ? draft.inheritedCapabilityIds.filter(
                          (id) => !ownedCapabilityIds.includes(id),
                        )
                      : Array.from(
                          new Set([
                            ...draft.inheritedCapabilityIds,
                            ...ownedDefaultIds,
                          ]),
                        ),
                  );
                };
                return (
                  <article
                    key={agent.id}
                    className={selected ? "is-selected" : ""}
                  >
                    <Button
                      variant="ghost"
                      type="button"
                      className="pm-agent-select"
                      onClick={toggleAgent}
                      aria-pressed={selected}
                    >
                      <i>{selected && <IconCheck size={13} />}</i>
                      <AgentAvatar agent={agent} />
                      <span>
                        <strong>{agent.name}</strong>
                        <small>
                          {projectUserFacingCopy(agent.role_description, t)}
                        </small>
                      </span>
                      <em>
                        {t("projectCreate.team.capabilityCount", {
                          count:
                            (agent.skill_count || 0) + (agent.mcp_count || 0),
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
              })}
              </div>
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
  template,
  draft,
  patch,
}: {
  agents: ProjectAgentOption[];
  projectCapabilities: ProjectCapabilityOption[];
  inheritedCapabilities: ProjectCapabilityOption[];
  template?: ProjectTemplate;
  draft: Draft;
  patch: PatchDraft;
}) {
  const { t } = useTranslation();
  const [activeAgentId, setActiveAgentId] = useState(
    () => draft.leaderId || agents[0]?.id || "",
  );
  const [capabilityTab, setCapabilityTab] = useState<"tools" | "skills">("tools");
  const [toolSearch, setToolSearch] = useState("");
  const [skillSearch, setSkillSearch] = useState("");
  const [expandedToolGroups, setExpandedToolGroups] = useState<Set<string>>(
    () => new Set(["general"]),
  );
  const [expandedSkillGroups, setExpandedSkillGroups] = useState<Set<string>>(
    () => new Set(["general"]),
  );
  useEffect(() => {
    if (!agents.some((agent) => agent.id === activeAgentId)) {
      setActiveAgentId(draft.leaderId || agents[0]?.id || "");
    }
  }, [activeAgentId, agents, draft.leaderId]);
  const allCapabilities = [...projectCapabilities, ...inheritedCapabilities];
  const visibleCapabilities = allCapabilities.filter(
    (capability) =>
      capability.source === "project" ||
      capability.owner_agent_id === activeAgentId,
  );
  const toolCapabilities = visibleCapabilities.filter(
    (capability) => capability.kind !== "skill",
  );
  const skillCapabilities = visibleCapabilities.filter(
    (capability) => capability.kind === "skill",
  );
  const selectedCapabilityIds = useMemo(
    () =>
      new Set([
        ...draft.sharedCapabilityIds,
        ...draft.inheritedCapabilityIds,
      ]),
    [draft.inheritedCapabilityIds, draft.sharedCapabilityIds],
  );
  const toggleCapability = (capabilityId: string) => {
    const capability = allCapabilities.find((item) => item.id === capabilityId);
    if (!capability) return;
    const key = capability.source === "agent"
      ? "inheritedCapabilityIds"
      : "sharedCapabilityIds";
    patch(key, toggleItem(draft[key], capabilityId));
  };
  const getPresentation = (capability: ProjectCapabilityOption) =>
    getLocalizedToolPresentation(t, {
      key: capability.internal_name,
      name: projectUserFacingCopy(capability.name, t),
      description: capability.description,
      category: capability.category,
      type: capability.kind,
      mcp_server_name: capability.mcp_server_name,
    });
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
        <div className="pm-selected-agent-list" role="list">
          {agents.map((agent) => {
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
                <span>
                  <strong>{agent.name}</strong>
                  <small>{projectUserFacingCopy(agent.role_description, t)}</small>
                </span>
              </Button>
            );
          })}
        </div>
        <div className="pm-capability-picker__header">
          <ProjectSegmentedControl
            className="pm-capability-tabs"
            value={capabilityTab}
            onChange={setCapabilityTab}
            ariaLabel={t("projectCreate.capabilities.tabsAria")}
            options={[
              {
                value: "tools",
                icon: <IconCode size={15} />,
                label: t("projectCreate.capabilities.toolsTab"),
                count: toolCapabilities.length,
              },
              {
                value: "skills",
                icon: <IconBolt size={15} />,
                label: t("projectCreate.capabilities.skillsTab"),
                count: skillCapabilities.length,
              },
            ]}
          />
          <ProjectCountBadge>
            {t("projectCreate.capabilities.count", {
              count: selectedCapabilityIds.size,
            })}
          </ProjectCountBadge>
        </div>
        <ToolCatalogPanel
          items={capabilityTab === "tools" ? toolCapabilities : skillCapabilities}
          getKey={(capability) => capability.id}
          getPresentation={getPresentation}
          searchValue={capabilityTab === "tools" ? toolSearch : skillSearch}
          onSearchChange={capabilityTab === "tools" ? setToolSearch : setSkillSearch}
          searchPlaceholder={t(
            capabilityTab === "tools"
              ? "projectCreate.capabilities.searchTools"
              : "projectCreate.capabilities.searchSkills",
          )}
          emptyLabel={t(
            capabilityTab === "tools"
              ? "projectCreate.capabilities.noTools"
              : "projectCreate.capabilities.noSkills",
          )}
          ariaLabel={t(
            capabilityTab === "tools"
              ? "projectCreate.capabilities.toolsAria"
              : "projectCreate.capabilities.skillsAria",
          )}
          expandedGroups={
            capabilityTab === "tools" ? expandedToolGroups : expandedSkillGroups
          }
          onExpandedGroupsChange={
            capabilityTab === "tools" ? setExpandedToolGroups : setExpandedSkillGroups
          }
          selectedKeys={selectedCapabilityIds}
          onToggle={toggleCapability}
          renderGroupIcon={() =>
            capabilityTab === "tools" ? <IconCode size={15} /> : <IconBolt size={15} />
          }
          renderItemBadges={(capability) => (
            <span className="tool-catalog-panel__badge">
              {capability.source === "agent"
                ? t("projectCreate.capabilities.fromEmployee", {
                    name: capability.owner_agent_name || t("projectCreate.capabilities.employeeShort"),
                  })
                : t("projectCreate.capabilities.projectShared")}
            </span>
          )}
          maxHeight={520}
        />
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
          {shareTargets.length ? (
            <div className="pm-share-targets">
              {shareTargets.map((target) => {
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
