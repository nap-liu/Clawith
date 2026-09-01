import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  IconAlertTriangle,
  IconArrowLeft,
  IconArrowRight,
  IconCheck,
  IconMessageCircle,
  IconRefresh,
} from "@tabler/icons-react";

import { projectsApi } from "../../services/projects";
import { useDialog } from "../../components/Dialog/DialogProvider";
import type {
  ProjectCapabilityCreateOverride,
  ProjectCreatePayload,
  ProjectFromTemplatePayload,
} from "./types";
import {
  Button,
  ProjectEmptyState,
} from "./components/ProjectUI";
import { BoundaryStep } from "./projectCreatePage/BoundaryStep";
import { CapabilitiesStep } from "./projectCreatePage/CapabilitiesStep";
import {
  defaultProjectName,
  initialAgentSettings,
  initialDraft,
  type Draft,
} from "./projectCreatePage/shared";
import { TeamStep } from "./projectCreatePage/TeamStep";
import { projectUserFacingCopy } from "./projectUserFacingCopy";
import "./projectPortfolio.css";

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
