import { useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import {
  IconBolt,
  IconCodeDots,
  IconLoader2,
  IconPlus,
  IconTool,
} from "@tabler/icons-react";

import { enterpriseApi } from "../../../services/api";
import { sortLlmModels } from "../../../utils/llmModels";
import { projectsApi } from "../../../services/projects";
import type {
  ProjectCapabilityOption,
  ProjectOwnedAgent,
  ProjectOwnedAgentPromotion,
} from "../types";
import { PROJECT_TOOL_REGISTRY } from "./config";
import {
  errorMessage,
  fileSizeLabel,
  num,
  obj,
  projectToolResolution,
  text,
  bool,
} from "./helpers";
import type {
  OpenSession,
  RecordValue,
} from "./types";
import type {
  ProjectWorkspaceTab as WorkspaceTab,
  ProjectWorkspaceUrlPatch as WorkspaceUrlPatch,
} from "../projectWorkspaceRouting";
import { isProjectA2ARecord } from "../projectSessionRouting";
import { MembersPanelView } from "./membersPanelView";
import { MembersAgentDialogs } from "./membersAgentDialogs";

export function MembersPanel({
  projectId,
  projectAgents,
  members,
  capabilities,
  policies,
  events,
  canManage,
  selectedId,
  onSelect,
  onOpenWorkspace,
  onNavigate,
  runAction,
  busyAction,
}: {
  projectId: string;
  projectAgents: ProjectOwnedAgent[];
  members: RecordValue[];
  capabilities: RecordValue[];
  policies: RecordValue | null;
  events: RecordValue[];
  canManage: boolean;
  selectedId: string;
  onSelect: (id: string) => void;
  onOpenWorkspace: (path: string) => void;
  onNavigate: (tab: WorkspaceTab, patch?: WorkspaceUrlPatch) => void;
  runAction: (
    key: string,
    action: () => Promise<unknown>,
    success: string,
  ) => Promise<boolean>;
  busyAction: string;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const activeMembers = useMemo(
    () => members.filter((entry) => entry.is_enabled !== false),
    [members],
  );
  const collaborationEvents = useMemo(
    () => events.filter(isProjectA2ARecord),
    [events],
  );
  const member = selectedId
    ? members.find((entry) =>
        [text(entry, "id"), text(entry, "member_id"), text(entry, "agent_id")]
          .filter(Boolean)
          .includes(selectedId),
      )
    : undefined;
  const [configDraft, setConfigDraft] = useState<RecordValue>({});
  const [memberModels, setMemberModels] = useState<
    Array<{
      id: string;
      provider: string;
      model: string;
      label?: string;
      enabled?: boolean;
    }>
  >([]);
  const [availableAgents, setAvailableAgents] = useState<RecordValue[]>([]);
  const [availableCapabilities, setAvailableCapabilities] = useState<
    ProjectCapabilityOption[]
  >([]);
  const [agentsLoading, setAgentsLoading] = useState(true);
  const [agentsError, setAgentsError] = useState("");
  const [agentDrawerMode, setAgentDrawerMode] = useState<
    "create" | "edit" | null
  >(null);
  const [settingsOpen, setSettingsOpen] = useState(
    Boolean(selectedId && member),
  );
  const [capabilitySection, setCapabilitySection] = useState<
    "config" | "tools" | "skill"
  >("config");
  const [addingCapabilityKind, setAddingCapabilityKind] = useState<
    "mcp" | "skill" | null
  >(null);
  const [capabilityToAddId, setCapabilityToAddId] = useState("");
  const [createKind, setCreateKind] = useState<"copy" | "blank">("copy");
  const [removeDialogOpen, setRemoveDialogOpen] = useState(false);
  const [promoteDialogOpen, setPromoteDialogOpen] = useState(false);
  const [candidateAgentId, setCandidateAgentId] = useState("");
  const [agentDraft, setAgentDraft] = useState({
    name: "",
    roleDescription: "",
    soul: "",
    coreMemory: "",
  });
  const [promotedAgent, setPromotedAgent] =
    useState<ProjectOwnedAgentPromotion | null>(null);

  useEffect(() => {
    setConfigDraft(obj(member?.config_snapshot));
  }, [member]);

  useEffect(() => {
    if (!settingsOpen || !canManage) return;
    let active = true;
    void enterpriseApi
      .llmModels()
      .then((models) => {
        if (active)
          setMemberModels(
            (Array.isArray(models) ? models : []).filter(
              (model) => model?.enabled !== false,
            ),
          );
      })
      .catch(() => {
        if (active) setMemberModels([]);
      });
    return () => {
      active = false;
    };
  }, [canManage, settingsOpen]);

  useEffect(() => {
    if ((!settingsOpen && agentDrawerMode !== "create") || !canManage) return;
    let mounted = true;
    setAgentsLoading(true);
    setAgentsError("");
    void queryClient
      .fetchQuery({
        queryKey: ["projects", "bootstrap-options"],
        queryFn: projectsApi.bootstrapOptions,
        staleTime: 30_000,
      })
      .then((options) => {
        if (mounted) {
          setAvailableAgents(options.agents.map((agent) => ({ ...agent })));
          setAvailableCapabilities(options.capabilities);
        }
      })
      .catch((error) => {
        if (mounted)
          setAgentsError(
            errorMessage(error, t("projectWorkspacePage.errors.requestFailed")),
          );
      })
      .finally(() => {
        if (mounted) setAgentsLoading(false);
      });
    return () => {
      mounted = false;
    };
  }, [agentDrawerMode, canManage, queryClient, settingsOpen, t]);

  const memberId = text(member || {}, "id", "member_id");
  const agentId = text(member || {}, "agent_id");
  const projectAgent = useMemo(
    () =>
      projectAgents.find(
        (entry) => entry.id === agentId || entry.member_id === memberId,
      ) || null,
    [agentId, memberId, projectAgents],
  );
  const memberName =
    text(member || {}, "name_snapshot", "agent_name", "name") ||
    t("projectAgents.defaultRole");
  const departed = member?.is_enabled === false;
  const candidates = availableAgents;
  const candidateOptions = candidates.map((agent) => ({
    value: text(agent, "id", "agent_id"),
    label: `${text(agent, "name", "agent_name") || t("projectAgents.unnamed")} · ${text(agent, "role_description", "role") || t("projectAgents.defaultRole")}`,
  }));
  const selectedCandidate =
    candidates.find(
      (agent) => text(agent, "id", "agent_id") === candidateAgentId,
    ) || candidates[0];
  const selectedCandidateId = text(selectedCandidate || {}, "id", "agent_id");
  const candidateCapabilities = useMemo(
    () =>
      availableCapabilities.filter(
        (capability) =>
          capability.source === "agent" &&
          capability.owner_agent_id === selectedCandidateId,
      ),
    [availableCapabilities, selectedCandidateId],
  );

  useEffect(() => {
    setCapabilitySection("config");
    setAddingCapabilityKind(null);
    setCapabilityToAddId("");
  }, [memberId]);

  useEffect(() => {
    setSettingsOpen(Boolean(selectedId && memberId));
  }, [memberId, selectedId]);

  useEffect(() => {
    if (agentDrawerMode !== "edit" || !projectAgent) return;
    setAgentDraft({
      name: projectAgent.name,
      roleDescription: projectAgent.role_description,
      soul: projectAgent.soul,
      coreMemory: projectAgent.core_memory,
    });
  }, [agentDrawerMode, projectAgent]);
  const modelOptions = [
    { value: "", label: t("projectSnapshot.followSourceAgent") },
    ...sortLlmModels(memberModels).map((model) => ({
      value: model.id,
      label: model.label || `${model.provider} · ${model.model}`,
    })),
  ];
  const updateConfigField = (key: string, value: unknown) =>
    setConfigDraft((current) => ({ ...current, [key]: value }));

  const saveSnapshot = () => {
    if (!canManage || departed) return;
    const maxToolRoundsRaw = text(configDraft, "max_tool_rounds").trim();
    const maxToolRounds = maxToolRoundsRaw ? Number(maxToolRoundsRaw) : null;
    const payload = {
      ...configDraft,
      max_tool_rounds:
        maxToolRounds !== null && Number.isFinite(maxToolRounds)
          ? maxToolRounds
          : null,
    };
    void runAction(
      "save-member",
      () =>
        projectsApi.patchMember(projectId, memberId, {
          config_snapshot: payload,
        }),
      t("projectWorkspacePage.members.feedback.snapshotSaved"),
    );
  };
  const resetAgentDraft = () => {
    setAgentDraft({
      name: "",
      roleDescription: "",
      soul: "",
      coreMemory: "",
    });
    setCandidateAgentId("");
    setCreateKind("copy");
    setPromotedAgent(null);
  };
  const createOwnedAgent = () => {
    if (createKind === "copy" && !selectedCandidateId) return;
    if (createKind === "blank" && agentDraft.name.trim().length < 2) return;
    let created: ProjectOwnedAgent | null = null;
    void runAction(
      "create-project-agent",
      async () => {
        created = await projectsApi.createProjectAgent(projectId, {
          source_agent_id: createKind === "copy" ? selectedCandidateId : null,
          name: agentDraft.name.trim() || null,
          role_description: agentDraft.roleDescription.trim() || null,
          soul: agentDraft.soul || null,
          core_memory: agentDraft.coreMemory || null,
        });
        return created;
      },
      t("projectAgents.feedback.created"),
    ).then((succeeded) => {
      if (succeeded) {
        setAgentDrawerMode(null);
        resetAgentDraft();
        if (created) onSelect(created.member_id);
      }
    });
  };
  const saveProjectAgent = () => {
    if (!projectAgent || agentDraft.name.trim().length < 2) return;
    void runAction(
      "save-project-agent",
      () =>
        projectsApi.updateProjectAgent(projectId, projectAgent.id, {
          name: agentDraft.name.trim(),
          role_description: agentDraft.roleDescription,
          soul: agentDraft.soul,
          core_memory: agentDraft.coreMemory,
        }),
      t("projectAgents.feedback.saved"),
    ).then((succeeded) => {
      if (succeeded) setAgentDrawerMode(null);
    });
  };
  const promoteProjectAgent = () => {
    if (!projectAgent) return;
    let promoted: ProjectOwnedAgentPromotion | null = null;
    void runAction(
      "promote-project-agent",
      async () => {
        promoted = await projectsApi.promoteProjectAgent(
          projectId,
          projectAgent.id,
          agentDraft.name.trim(),
        );
        return promoted;
      },
      t("projectAgents.feedback.promoted"),
    ).then((succeeded) => {
      if (succeeded && promoted) {
        setPromoteDialogOpen(false);
        setPromotedAgent(promoted);
        setAgentDrawerMode("edit");
      }
    });
  };
  const openPromoteDialog = () => {
    if (!projectAgent) return;
    setPromotedAgent(null);
    if (agentDrawerMode !== "edit") {
      setAgentDraft({
        name: projectAgent.name,
        roleDescription: projectAgent.role_description,
        soul: projectAgent.soul,
        coreMemory: projectAgent.core_memory,
      });
    }
    setPromoteDialogOpen(true);
  };
  const removeMember = () => {
    if (!memberId || bool(member || {}, "is_leader")) return;
    void runAction(
      "remove-member",
      () =>
        projectAgent
          ? projectsApi.deactivateProjectAgent(projectId, projectAgent.id)
          : projectsApi.removeMember(
              projectId,
              memberId,
              "human_removed_from_project",
            ),
      projectAgent
        ? t("projectAgents.feedback.deactivated")
        : t("projectWorkspacePage.members.feedback.removed"),
    ).then((succeeded) => {
      if (succeeded) setRemoveDialogOpen(false);
    });
  };
  const restoreMember = () => {
    if (!memberId || !departed) return;
    void runAction(
      "restore-member",
      () =>
        projectAgent
          ? projectsApi.restoreProjectAgent(projectId, projectAgent.id)
          : projectsApi.restoreMember(
              projectId,
              memberId,
              "human_restored_to_project",
            ),
      projectAgent
        ? t("projectAgents.feedback.restored")
        : t("projectWorkspacePage.members.feedback.restored"),
    );
  };
  const memberCapabilities = capabilities.filter((capability) => {
    const inheritedAgentId = text(
      capability,
      "inherited_from_agent_id",
      "owner_agent_id",
    );
    if (inheritedAgentId) return inheritedAgentId === agentId;
    return text(capability, "source") === "shared";
  });
  const capabilitiesByKind = (kind: "tool" | "mcp" | "skill") =>
    memberCapabilities.filter(
      (capability) =>
        text(capability, "capability_type", "kind", "type") === kind,
    );
  const platformTools = capabilitiesByKind("tool");
  const memberMcps = capabilitiesByKind("mcp");
  const memberSkills = capabilitiesByKind("skill");
  const memberSkillOptions = (() => {
    const options = new Map<string, ProjectCapabilityOption>();
    availableCapabilities
      .filter(
        (capability) =>
          capability.kind === "skill" &&
          (capability.source === "project" ||
            capability.owner_agent_id === agentId),
      )
      .forEach((capability) => {
        const id = capability.capability_id || capability.id;
        if (id) options.set(id, capability);
      });
    memberSkills.forEach((capability) => {
      const id = text(capability, "capability_id", "id");
      if (!id || options.has(id)) return;
      options.set(id, {
        id,
        capability_id: id,
        name:
          text(capability, "name", "capability_name") ||
          t("projectWorkspacePage.capabilities.capability"),
        description: text(capability, "description", "purpose", "summary"),
        kind: "skill",
        source: text(capability, "source") === "shared" ? "project" : "agent",
        origin:
          text(capability, "source") === "shared"
            ? "market"
            : "digital_employee",
        owner_agent_id: text(
          capability,
          "inherited_from_agent_id",
          "owner_agent_id",
        ),
      });
    });
    return [...options.values()];
  })();
  const selectedMemberSkillIds = memberSkills
    .filter((capability) => capability.is_enabled !== false)
    .map((capability) => text(capability, "capability_id", "id"))
    .filter(Boolean);
  const updateMemberSkills = (nextIds: string[]) => {
    if (!canManage || departed || !agentId) return;
    const selected = new Set(nextIds);
    const existingByCapability = new Map(
      memberSkills.map((capability) => [
        text(capability, "capability_id", "id"),
        capability,
      ]),
    );
    void runAction(
      `member-skills-${memberId}`,
      () =>
        Promise.all([
          ...memberSkills
            .filter((capability) => {
              const capabilityId = text(capability, "capability_id", "id");
              return (
                Boolean(capabilityId) &&
                selected.has(capabilityId) !==
                  (capability.is_enabled !== false)
              );
            })
            .map((capability) =>
              projectsApi.patchCapability(
                projectId,
                text(capability, "id", "binding_id"),
                {
                  is_enabled: selected.has(
                    text(capability, "capability_id", "id"),
                  ),
                },
              ),
            ),
          ...nextIds
            .filter((capabilityId) => !existingByCapability.has(capabilityId))
            .map((capabilityId) => {
              const option = memberSkillOptions.find(
                (candidate) =>
                  (candidate.capability_id || candidate.id) === capabilityId,
              );
              return projectsApi.createCapability(projectId, {
                capability_type: "skill",
                capability_id: capabilityId,
                capability_name:
                  option?.name ||
                  t("projectWorkspacePage.capabilities.capability"),
                source: "inherited",
                inherited_from_agent_id: agentId,
                is_enabled: true,
              });
            }),
        ]),
      t("projectWorkspacePage.capabilities.feedback.enabled"),
    );
  };
  const effectiveProjectToolCount = member
    ? PROJECT_TOOL_REGISTRY.filter(
        (tool) => projectToolResolution(tool, member, policies).effective,
      ).length
    : 0;
  const configCount = [
    text(configDraft, "primary_model_id"),
    text(configDraft, "fallback_model_id"),
    typeof configDraft.temperature === "number" ? "temperature" : "",
    text(configDraft, "reasoning_effort"),
    text(configDraft, "project_instruction"),
    text(configDraft, "max_tool_rounds"),
  ].filter(Boolean).length;

  return (
    <>
      <MembersPanelView
        projectId={projectId}
        members={members}
        collaborationEvents={collaborationEvents}
        member={member}
        agentId={agentId}
        projectAgent={projectAgent}
        memberId={memberId}
        memberName={memberName}
        canManage={canManage}
        departed={Boolean(departed)}
        settingsOpen={settingsOpen}
        setSettingsOpen={setSettingsOpen}
        capabilitySection={capabilitySection}
        setCapabilitySection={setCapabilitySection}
        configDraft={configDraft}
        updateConfigField={updateConfigField}
        modelOptions={modelOptions}
        configCount={configCount}
        effectiveProjectToolCount={effectiveProjectToolCount}
        platformTools={platformTools}
        memberMcps={memberMcps}
        memberSkills={memberSkills}
        busyAction={busyAction}
        saveSnapshot={saveSnapshot}
        onSelect={onSelect}
        onNavigate={onNavigate}
        openCreateAgent={() => {
          resetAgentDraft();
          setAgentDrawerMode("create");
        }}
        openEditAgent={() => {
          setPromotedAgent(null);
          setAgentDrawerMode("edit");
        }}
        openPromoteDialog={openPromoteDialog}
        openRemoveDialog={() => setRemoveDialogOpen(true)}
        restoreMember={restoreMember}
        setLeader={() =>
          void runAction(
            "leader",
            () => projectsApi.setLeader(projectId, agentId),
            t("projectTerminology.workspace.ownerChanged"),
          )
        }
        memberSkillOptions={memberSkillOptions}
        selectedMemberSkillIds={selectedMemberSkillIds}
        updateMemberSkills={updateMemberSkills}
      />
      <MembersAgentDialogs
        agentDrawerMode={agentDrawerMode}
        setAgentDrawerMode={setAgentDrawerMode}
        busyAction={busyAction}
        projectAgent={projectAgent}
        onOpenWorkspace={onOpenWorkspace}
        promotedAgent={promotedAgent}
        createKind={createKind}
        setCreateKind={setCreateKind}
        agentsError={agentsError}
        candidateOptions={candidateOptions}
        selectedCandidateId={selectedCandidateId}
        setCandidateAgentId={setCandidateAgentId}
        agentsLoading={agentsLoading}
        selectedCandidate={selectedCandidate}
        candidateCapabilities={candidateCapabilities}
        agentDraft={agentDraft}
        setAgentDraft={setAgentDraft}
        canManage={canManage}
        departed={Boolean(departed)}
        saveProjectAgent={saveProjectAgent}
        createOwnedAgent={createOwnedAgent}
        promoteDialogOpen={promoteDialogOpen}
        setPromoteDialogOpen={setPromoteDialogOpen}
        memberName={memberName}
        promoteProjectAgent={promoteProjectAgent}
        removeDialogOpen={removeDialogOpen}
        setRemoveDialogOpen={setRemoveDialogOpen}
        removeMember={removeMember}
      />
    </>
  );
}
