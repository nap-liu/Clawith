import { type Dispatch, type SetStateAction } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  IconAlertTriangle,
  IconArrowRight,
  IconCircleCheck,
  IconDeviceFloppy,
  IconLoader2,
  IconPlus,
  IconSparkles,
  IconTrash,
  IconUsers,
  IconX,
} from "@tabler/icons-react";

import { Drawer } from "../../../components/Dialog/DialogProvider";
import {
  Button,
  ProjectDialog,
  ProjectEmptyState,
  ProjectField,
  ProjectIconButton,
  ProjectSegmentedControl,
  ProjectSelect,
  ProjectTextarea,
  TextInput,
} from "../components/ProjectUI";
import { projectUserFacingCopy } from "../projectUserFacingCopy";
import type {
  ProjectCapabilityOption,
  ProjectOwnedAgent,
  ProjectOwnedAgentPromotion,
} from "../types";
import { num, text } from "./helpers";

export function MembersAgentDialogs({
  agentDrawerMode,
  setAgentDrawerMode,
  busyAction,
  projectAgent,
  onOpenWorkspace,
  promotedAgent,
  createKind,
  setCreateKind,
  agentsError,
  candidateOptions,
  selectedCandidateId,
  setCandidateAgentId,
  agentsLoading,
  selectedCandidate,
  candidateCapabilities,
  agentDraft,
  setAgentDraft,
  canManage,
  departed,
  saveProjectAgent,
  createOwnedAgent,
  promoteDialogOpen,
  setPromoteDialogOpen,
  memberName,
  promoteProjectAgent,
  removeDialogOpen,
  setRemoveDialogOpen,
  removeMember,
}: {
  agentDrawerMode: "create" | "edit" | null;
  setAgentDrawerMode: Dispatch<SetStateAction<"create" | "edit" | null>>;
  busyAction: string;
  projectAgent: ProjectOwnedAgent | null;
  onOpenWorkspace: (path: string) => void;
  promotedAgent: ProjectOwnedAgentPromotion | null;
  createKind: "copy" | "blank";
  setCreateKind: Dispatch<SetStateAction<"copy" | "blank">>;
  agentsError: string;
  candidateOptions: Array<{ value: string; label: string }>;
  selectedCandidateId: string;
  setCandidateAgentId: Dispatch<SetStateAction<string>>;
  agentsLoading: boolean;
  selectedCandidate: Record<string, unknown> | undefined;
  candidateCapabilities: ProjectCapabilityOption[];
  agentDraft: {
    name: string;
    roleDescription: string;
    soul: string;
    coreMemory: string;
  };
  setAgentDraft: Dispatch<
    SetStateAction<{
      name: string;
      roleDescription: string;
      soul: string;
      coreMemory: string;
    }>
  >;
  canManage: boolean;
  departed: boolean;
  saveProjectAgent: () => void;
  createOwnedAgent: () => void;
  promoteDialogOpen: boolean;
  setPromoteDialogOpen: Dispatch<SetStateAction<boolean>>;
  memberName: string;
  promoteProjectAgent: () => void;
  removeDialogOpen: boolean;
  setRemoveDialogOpen: Dispatch<SetStateAction<boolean>>;
  removeMember: () => void;
}) {
  const { t } = useTranslation();

  return (
    <>
      <Drawer
        open={Boolean(agentDrawerMode)}
        onClose={() => {
          if (!busyAction.includes("project-agent")) setAgentDrawerMode(null);
        }}
        ariaLabel={t(
          agentDrawerMode === "edit"
            ? "projectAgents.editor.title"
            : "projectAgents.create.title",
        )}
        className="project-workspace__agent-drawer"
      >
        <div className="project-workspace__agent-drawer-content">
          <header>
            <div>
              <span>{t("projectAgents.badge")}</span>
              <h2>
                {t(
                  agentDrawerMode === "edit"
                    ? "projectAgents.editor.title"
                    : "projectAgents.create.title",
                )}
              </h2>
              <p>
                {t(
                  agentDrawerMode === "edit"
                    ? "projectAgents.editor.description"
                    : "projectAgents.create.description",
                )}
              </p>
            </div>
            <ProjectIconButton
              aria-label={t("common.close")}
              disabled={busyAction.includes("project-agent")}
              onClick={() => setAgentDrawerMode(null)}
            >
              <IconX size={18} />
            </ProjectIconButton>
          </header>
          <div className="project-workspace__agent-drawer-body">
            {projectAgent ? (
              <div className="project-workspace__member-files-action">
                <Button
                  variant="ghost"
                  onClick={() => {
                    setAgentDrawerMode(null);
                    onOpenWorkspace(`${projectAgent.agent_dir}/soul.md`);
                  }}
                >
                  {t("projectAgents.actions.openWorkspace")}
                  <IconArrowRight size={14} />
                </Button>
              </div>
            ) : null}
            {promotedAgent ? (
              <div
                className="project-workspace__agent-promotion-success"
                role="status"
              >
                <IconCircleCheck size={20} />
                <div>
                  <strong>{t("projectAgents.promotion.title")}</strong>
                  <p>{t("projectAgents.promotion.description")}</p>
                  <Link to={`/agents/${promotedAgent.id}/chat`}>
                    {t("projectAgents.promotion.open")}
                    <IconArrowRight size={14} />
                  </Link>
                </div>
              </div>
            ) : null}
            {agentDrawerMode === "create" && (
              <>
                <ProjectSegmentedControl
                  value={createKind}
                  options={[
                    {
                      value: "copy",
                      label: t("projectAgents.create.copy"),
                    },
                    {
                      value: "blank",
                      label: t("projectAgents.create.blank"),
                    },
                  ]}
                  onChange={setCreateKind}
                  ariaLabel={t("projectAgents.create.mode")}
                />
                {createKind === "copy" &&
                  (agentsError ? (
                    <div
                      className="project-workspace__repository-error"
                      role="alert"
                    >
                      <IconAlertTriangle size={16} />
                      <span>{agentsError}</span>
                    </div>
                  ) : candidateOptions.length ? (
                    <>
                      <ProjectField label={t("projectAgents.create.source")}>
                        <ProjectSelect
                          value={selectedCandidateId}
                          options={candidateOptions}
                          onChange={setCandidateAgentId}
                          ariaLabel={t("projectAgents.create.source")}
                          disabled={
                            agentsLoading ||
                            busyAction === "create-project-agent"
                          }
                        />
                      </ProjectField>
                      {selectedCandidate && (
                        <>
                          <div className="project-workspace__member-candidate">
                            <span>
                              {(text(selectedCandidate, "name") || "A").slice(
                                0,
                                1,
                              )}
                            </span>
                            <div>
                              <strong>
                                {text(selectedCandidate, "name") ||
                                  t("projectAgents.unnamed")}
                              </strong>
                              <p>
                                {projectUserFacingCopy(
                                  text(
                                    selectedCandidate,
                                    "role_description",
                                  ) || t("projectAgents.defaultRole"),
                                  t,
                                )}
                              </p>
                            </div>
                          </div>
                          <section className="project-workspace__capability-preview">
                            <header>
                              <strong>
                                {t(
                                  "projectAgents.capabilityPackage.carryPreview",
                                )}
                              </strong>
                            </header>
                            <div>
                              {(["tool", "mcp", "skill"] as const).map(
                                (kind) => (
                                  <span key={kind}>
                                    <strong>
                                      {candidateCapabilities.filter(
                                        (capability) =>
                                          capability.kind === kind,
                                      ).length ||
                                        (kind === "skill"
                                          ? num(selectedCandidate, "skill_count")
                                          : kind === "mcp"
                                            ? num(
                                                selectedCandidate,
                                                "mcp_count",
                                              )
                                            : 0)}
                                    </strong>
                                    <small>
                                      {t(
                                        `projectAgents.capabilityPackage.sections.${kind === "tool" ? "tools" : kind}`,
                                      )}
                                    </small>
                                  </span>
                                ),
                              )}
                            </div>
                          </section>
                        </>
                      )}
                    </>
                  ) : (
                    <ProjectEmptyState
                      icon={
                        agentsLoading ? (
                          <IconLoader2
                            className="project-workspace__spinner"
                            size={20}
                          />
                        ) : (
                          <IconUsers size={20} />
                        )
                      }
                      title={t(
                        agentsLoading
                          ? "projectAgents.create.loading"
                          : "projectAgents.create.noSource",
                      )}
                      description={t(
                        agentsLoading
                          ? "projectAgents.create.loadingDescription"
                          : "projectAgents.create.noSourceDescription",
                      )}
                    />
                  ))}
              </>
            )}
            {(agentDrawerMode === "edit" || createKind === "blank") && (
              <div className="project-workspace__agent-form">
                <ProjectField
                  label={t("projectAgents.fields.name")}
                  labelFor="project-agent-name"
                >
                  <TextInput
                    id="project-agent-name"
                    value={agentDraft.name}
                    onChange={(event) =>
                      setAgentDraft((current) => ({
                        ...current,
                        name: event.target.value,
                      }))
                    }
                    disabled={
                      agentDrawerMode === "edit" && (!canManage || departed)
                    }
                  />
                </ProjectField>
                <ProjectField
                  label={t("projectAgents.fields.role")}
                  labelFor="project-agent-role"
                >
                  <ProjectTextarea
                    id="project-agent-role"
                    value={agentDraft.roleDescription}
                    rows={2}
                    onChange={(event) =>
                      setAgentDraft((current) => ({
                        ...current,
                        roleDescription: event.target.value,
                      }))
                    }
                    disabled={
                      agentDrawerMode === "edit" && (!canManage || departed)
                    }
                  />
                </ProjectField>
                <ProjectField
                  label={t("projectAgents.fields.soul")}
                  labelFor="project-agent-soul"
                  hint={t("projectAgents.fields.soulHint")}
                >
                  <ProjectTextarea
                    id="project-agent-soul"
                    value={agentDraft.soul}
                    rows={7}
                    onChange={(event) =>
                      setAgentDraft((current) => ({
                        ...current,
                        soul: event.target.value,
                      }))
                    }
                    disabled={
                      agentDrawerMode === "edit" && (!canManage || departed)
                    }
                  />
                </ProjectField>
                <ProjectField
                  label={t("projectAgents.fields.memory")}
                  labelFor="project-agent-memory"
                  hint={t("projectAgents.fields.memoryHint")}
                >
                  <ProjectTextarea
                    id="project-agent-memory"
                    value={agentDraft.coreMemory}
                    rows={7}
                    onChange={(event) =>
                      setAgentDraft((current) => ({
                        ...current,
                        coreMemory: event.target.value,
                      }))
                    }
                    disabled={
                      agentDrawerMode === "edit" && (!canManage || departed)
                    }
                  />
                </ProjectField>
              </div>
            )}
          </div>
          {(agentDrawerMode === "create" ||
            (agentDrawerMode === "edit" && canManage && !departed)) && (
            <footer>
              {agentDrawerMode === "edit" && projectAgent ? (
                <Button
                  variant="primary"
                  onClick={saveProjectAgent}
                  disabled={
                    agentDraft.name.trim().length < 2 ||
                    busyAction === "save-project-agent"
                  }
                >
                  <IconDeviceFloppy size={16} />
                  {t("projectAgents.actions.save")}
                </Button>
              ) : agentDrawerMode === "create" ? (
                <>
                  <Button
                    variant="secondary"
                    onClick={() => setAgentDrawerMode(null)}
                  >
                    {t("common.cancel")}
                  </Button>
                  <Button
                    variant="primary"
                    onClick={createOwnedAgent}
                    disabled={
                      busyAction === "create-project-agent" ||
                      (createKind === "copy"
                        ? !selectedCandidateId || agentsLoading
                        : agentDraft.name.trim().length < 2)
                    }
                  >
                    {busyAction === "create-project-agent" ? (
                      <IconLoader2
                        className="project-workspace__spinner"
                        size={16}
                      />
                    ) : (
                      <IconPlus size={16} />
                    )}
                    {t("projectAgents.actions.create")}
                  </Button>
                </>
              ) : null}
            </footer>
          )}
        </div>
      </Drawer>

      <ProjectDialog
        open={promoteDialogOpen}
        onClose={() => {
          if (busyAction !== "promote-project-agent") {
            setPromoteDialogOpen(false);
          }
        }}
        ariaLabel={t("projectAgents.promotion.confirmTitle", {
          name: projectAgent?.name || memberName,
        })}
        className="project-workspace__member-dialog"
      >
        <div className="project-workspace__modal">
          <header>
            <div>
              <span>{t("projectAgents.promotion.badge")}</span>
              <h2>
                {t("projectAgents.promotion.confirmTitle", {
                  name: projectAgent?.name || memberName,
                })}
              </h2>
            </div>
            <ProjectIconButton
              aria-label={t("common.close")}
              disabled={busyAction === "promote-project-agent"}
              onClick={() => setPromoteDialogOpen(false)}
            >
              <IconX size={18} />
            </ProjectIconButton>
          </header>
          <p>{t("projectAgents.promotion.confirmDescription")}</p>
          <footer>
            <Button
              variant="secondary"
              onClick={() => setPromoteDialogOpen(false)}
              disabled={busyAction === "promote-project-agent"}
            >
              {t("common.cancel")}
            </Button>
            <Button
              variant="primary"
              onClick={promoteProjectAgent}
              disabled={busyAction === "promote-project-agent"}
            >
              {busyAction === "promote-project-agent" ? (
                <IconLoader2 className="project-workspace__spinner" size={16} />
              ) : (
                <IconSparkles size={16} />
              )}
              {t("projectAgents.promotion.confirm")}
            </Button>
          </footer>
        </div>
      </ProjectDialog>

      <ProjectDialog
        open={removeDialogOpen}
        onClose={() => {
          if (busyAction !== "remove-member") setRemoveDialogOpen(false);
        }}
        ariaLabel={
          projectAgent
            ? t("projectAgents.deactivate.title", { name: memberName })
            : t("projectAgents.removeMember.title", { name: memberName })
        }
        className="project-workspace__member-dialog"
      >
        <div className="project-workspace__modal">
          <header>
            <div>
              <span>
                {projectAgent
                  ? t("projectAgents.badge")
                  : t("projectAgents.removeMember.badge")}
              </span>
              <h2>
                {projectAgent
                  ? t("projectAgents.deactivate.title", { name: memberName })
                  : t("projectAgents.removeMember.title", {
                      name: memberName,
                    })}
              </h2>
            </div>
            <ProjectIconButton
              aria-label={t("common.close")}
              disabled={busyAction === "remove-member"}
              onClick={() => setRemoveDialogOpen(false)}
            >
              <IconX size={18} />
            </ProjectIconButton>
          </header>
          <p>
            {projectAgent
              ? t("projectAgents.deactivate.description")
              : t("projectAgents.removeMember.description")}
          </p>
          <footer>
            <Button
              variant="secondary"
              onClick={() => setRemoveDialogOpen(false)}
              disabled={busyAction === "remove-member"}
            >
              {projectAgent
                ? t("projectAgents.actions.keepActive")
                : t("projectAgents.removeMember.keep")}
            </Button>
            <Button
              variant="danger"
              onClick={removeMember}
              disabled={busyAction === "remove-member"}
            >
              {busyAction === "remove-member" ? (
                <IconLoader2 className="project-workspace__spinner" size={16} />
              ) : (
                <IconTrash size={16} />
              )}
              {projectAgent
                ? t("projectAgents.actions.deactivate")
                : t("projectAgents.removeMember.confirm")}
            </Button>
          </footer>
        </div>
      </ProjectDialog>
    </>
  );
}
