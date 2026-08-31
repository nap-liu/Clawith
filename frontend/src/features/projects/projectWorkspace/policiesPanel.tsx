import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import {
  IconCodeDots,
  IconDeviceFloppy,
  IconLoader2,
  IconRefresh,
  IconSparkles,
  IconTool,
  IconX,
} from "@tabler/icons-react";

import { useToast } from "../../../components/Toast/ToastProvider";
import { enterpriseApi } from "../../../services/api";
import { projectsApi } from "../../../services/projects";
import {
  Button,
  ProjectDialog,
  ProjectField,
  ProjectIconButton,
  ProjectSelect,
  ProjectStatusBadge,
  TextInput,
  ToggleSwitch,
} from "../components/ProjectUI";
import type { ProjectSummary, ProjectTemplateManifest } from "../types";
import { errorMessage, fileSizeLabel, num, obj, text } from "./helpers";
import { SectionHeading } from "./shared";
import { ProjectVisibilitySettings } from "./visibilitySettings";
import type { RecordValue } from "./types";

export function PoliciesPanel({
  projectId,
  project,
  policies,
  onReload,
  runAction,
  busyAction,
}: {
  projectId: string;
  project: ProjectSummary;
  policies: RecordValue | null;
  onReload: () => Promise<void>;
  runAction: (
    key: string,
    action: () => Promise<unknown>,
    success: string,
  ) => Promise<boolean>;
  busyAction: string;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const isOwner = project.access_role === "owner";
  const canManageSettings = isOwner;
  const governance = obj(policies?.policies);
  const [model, setModel] = useState("default");
  const [models, setModels] = useState<
    Array<{
      id: string;
      provider: string;
      model: string;
      label?: string;
      enabled?: boolean;
    }>
  >([]);
  const [modelsLoading, setModelsLoading] = useState(true);
  const [modelsError, setModelsError] = useState("");
  const [parallel, setParallel] = useState("4");
  const [a2aLimit, setA2aLimit] = useState("12");
  const [loopGuard, setLoopGuard] = useState(true);
  const [templateDialogOpen, setTemplateDialogOpen] = useState(false);
  const [templateName, setTemplateName] = useState(project.name);
  const [templateNameError, setTemplateNameError] = useState("");
  const [publishingTemplate, setPublishingTemplate] = useState(false);
  const [templateManifest, setTemplateManifest] =
    useState<ProjectTemplateManifest | null>(null);
  const [templateManifestLoading, setTemplateManifestLoading] = useState(false);
  const [templateManifestError, setTemplateManifestError] = useState("");
  const [includedTemplateSkillIds, setIncludedTemplateSkillIds] = useState<
    string[]
  >([]);
  useEffect(() => {
    const nextRuntime = obj(policies?.runtime);
    const nextGovernance = obj(policies?.policies);
    setModel(text(nextRuntime, "model", "default_model") || "default");
    setParallel(text(nextRuntime, "max_parallel_runs") || "4");
    setA2aLimit(text(nextGovernance, "max_a2a_wakes") || "12");
    setLoopGuard(nextGovernance.loop_guard !== false);
  }, [policies]);
  useEffect(() => {
    setTemplateName(project.name);
    setTemplateNameError("");
  }, [project.id, project.name]);
  const loadTemplateManifest = useCallback(async () => {
    if (!isOwner) return;
    setTemplateManifestLoading(true);
    setTemplateManifestError("");
    setIncludedTemplateSkillIds([]);
    try {
      setTemplateManifest(await projectsApi.getTemplateManifest(projectId));
    } catch (error) {
      setTemplateManifest(null);
      setTemplateManifestError(
        errorMessage(error, t("projectWorkspacePage.errors.requestFailed")),
      );
    } finally {
      setTemplateManifestLoading(false);
    }
  }, [isOwner, projectId, t]);
  useEffect(() => {
    if (templateDialogOpen) void loadTemplateManifest();
  }, [loadTemplateManifest, templateDialogOpen]);
  useEffect(() => {
    let active = true;
    setModelsLoading(true);
    setModelsError("");
    void enterpriseApi
      .llmModels()
      .then((items) => {
        if (!active) return;
        setModels(
          (Array.isArray(items) ? items : []).filter(
            (item) => item?.enabled !== false,
          ),
        );
      })
      .catch((error) => {
        if (active)
          setModelsError(
            errorMessage(error, t("projectWorkspacePage.errors.requestFailed")),
          );
      })
      .finally(() => {
        if (active) setModelsLoading(false);
      });
    return () => {
      active = false;
    };
  }, [t]);
  const modelOptions = [
    {
      value: "default",
      label: t("projectWorkspacePage.policies.defaultTenantModel"),
    },
    ...models.map((item) => ({
      value: item.id,
      label: item.label || `${item.provider} · ${item.model}`,
    })),
  ];
  useEffect(() => {
    if (modelsLoading || modelsError || model === "default") return;
    if (!models.some((item) => item.id === model)) setModel("default");
  }, [model, models, modelsError, modelsLoading]);
  const save = () => {
    if (!canManageSettings) return;
    void runAction(
      "save-policies",
      () =>
        projectsApi.updateSettings(projectId, {
          runtime: { model, max_parallel_runs: Number(parallel) },
          policies: {
            ...governance,
            max_a2a_wakes: Number(a2aLimit),
            loop_guard: loopGuard,
          },
        }),
      t("projectWorkspacePage.policies.feedback.saved"),
    );
  };
  const publishTemplate = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const name = templateName.trim();
    if (!name) {
      setTemplateNameError(t("projectTemplatePublish.nameRequired"));
      return;
    }
    if (!isOwner || publishingTemplate) return;

    setPublishingTemplate(true);
    setTemplateNameError("");
    try {
      await projectsApi.createTemplateFromProject(projectId, {
        name,
        is_published: true,
        included_skill_binding_ids: includedTemplateSkillIds,
      });
      toast.success(t("projectTemplatePublish.success"));
      setTemplateDialogOpen(false);
    } catch (error) {
      toast.error(
        errorMessage(error, t("projectWorkspacePage.errors.requestFailed")),
      );
    } finally {
      setPublishingTemplate(false);
    }
  };
  return (
    <>
      <SectionHeading
        eyebrow={t("projectWorkspacePage.policies.eyebrow")}
        title={t("projectWorkspaceNav.tabs.policies")}
        description=""
        actions={
          <>
            {isOwner && (
              <Button
                variant="secondary"
                onClick={() => {
                  setTemplateName(project.name);
                  setTemplateNameError("");
                  setTemplateDialogOpen(true);
                }}
              >
                <IconSparkles size={16} />
                {t("projectTemplatePublish.action")}
              </Button>
            )}
            {canManageSettings && (
              <Button
                variant="primary"
                onClick={save}
                disabled={busyAction === "save-policies"}
              >
                {busyAction === "save-policies" ? (
                  <IconLoader2
                    className="project-workspace__spinner"
                    size={16}
                  />
                ) : (
                  <IconDeviceFloppy size={16} />
                )}
                {t("projectWorkspacePage.policies.actions.save")}
              </Button>
            )}
          </>
        }
      />
      <ProjectVisibilitySettings
        projectId={projectId}
        project={project}
        onReload={onReload}
      />
      <section className="project-workspace__runtime-settings">
        <header className="project-workspace__settings-section-heading">
          <div>
            <h3>{t("projectWorkspacePage.policies.runtimeTitle")}</h3>
            <p>{t("projectWorkspacePage.policies.runtimeDescription")}</p>
          </div>
        </header>
        <div className="project-workspace__settings-grid">
          <ProjectField
            label={t("projectWorkspacePage.policies.fields.defaultModel")}
            hint={
              modelsError
                ? t("projectWorkspacePage.policies.modelsLoadFailed", {
                    error: modelsError,
                  })
                : t("projectWorkspacePage.policies.fields.defaultModelHint")
            }
          >
            <ProjectSelect
              value={model}
              options={modelOptions}
              onChange={setModel}
              ariaLabel={t("projectWorkspacePage.policies.fields.defaultModel")}
              disabled={
                !canManageSettings || modelsLoading || Boolean(modelsError)
              }
              placeholder={t(
                modelsLoading
                  ? "projectWorkspacePage.policies.loadingModels"
                  : "projectWorkspacePage.policies.selectModel",
              )}
            />
          </ProjectField>
          <ProjectField
            label={t("projectWorkspacePage.policies.fields.maxParallelRuns")}
            labelFor="project-policy-parallel"
          >
            <TextInput
              id="project-policy-parallel"
              type="number"
              min="1"
              max="32"
              value={parallel}
              onChange={(event) => setParallel(event.target.value)}
              disabled={!canManageSettings}
            />
          </ProjectField>
          <ProjectField
            label={t("projectWorkspacePage.policies.fields.maxA2AWakes")}
            labelFor="project-policy-a2a-limit"
          >
            <TextInput
              id="project-policy-a2a-limit"
              type="number"
              min="1"
              max="100"
              value={a2aLimit}
              onChange={(event) => setA2aLimit(event.target.value)}
              disabled={!canManageSettings}
            />
          </ProjectField>
          <ProjectField
            label={t("projectWorkspacePage.policies.fields.loopGuard")}
            hint={t("projectWorkspacePage.policies.fields.loopGuardHint")}
          >
            <div className="project-workspace__toggle-field">
              <span>
                {t(
                  loopGuard
                    ? "projectWorkspacePage.policies.fields.enabled"
                    : "projectWorkspacePage.policies.fields.disabled",
                )}
              </span>
              <ToggleSwitch
                checked={loopGuard}
                onChange={setLoopGuard}
                ariaLabel={t("projectWorkspacePage.policies.fields.loopGuard")}
                disabled={!canManageSettings}
              />
            </div>
          </ProjectField>
        </div>
      </section>
      <ProjectDialog
        open={templateDialogOpen}
        onClose={() => {
          if (!publishingTemplate) setTemplateDialogOpen(false);
        }}
        ariaLabel={t("projectTemplatePublish.title")}
        className="project-workspace__git-dialog"
      >
        <form className="project-workspace__modal" onSubmit={publishTemplate}>
          <header>
            <div>
              <span>{t("projectTemplatePublish.eyebrow")}</span>
              <h2>{t("projectTemplatePublish.title")}</h2>
            </div>
            <ProjectIconButton
              aria-label={t("projectTemplatePublish.close")}
              disabled={publishingTemplate}
              onClick={() => setTemplateDialogOpen(false)}
            >
              <IconX size={18} />
            </ProjectIconButton>
          </header>
          <p>{t("projectTemplatePublish.description")}</p>
          <section
            className="project-workspace__template-manifest"
            aria-label={t("projectTemplatePublish.manifest.title")}
          >
            <header>
              <strong>{t("projectTemplatePublish.manifest.title")}</strong>
              {templateManifestLoading && (
                <span>
                  <IconLoader2
                    className="project-workspace__spinner"
                    size={14}
                  />
                  {t("projectTemplatePublish.manifest.loading")}
                </span>
              )}
            </header>
            {templateManifestError ? (
              <div className="project-workspace__template-manifest-error">
                <span>{t("projectTemplatePublish.manifest.loadFailed")}</span>
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => void loadTemplateManifest()}
                >
                  <IconRefresh size={14} />
                  {t("projectTemplatePublish.manifest.retry")}
                </Button>
              </div>
            ) : templateManifest ? (
              <>
                <div className="project-workspace__template-manifest-grid">
                  <span>
                    <strong>
                      {templateManifest.asset_summary.total_file_count}
                    </strong>
                    <small>{t("projectTemplatePublish.manifest.files")}</small>
                  </span>
                  <span>
                    <strong>
                      {templateManifest.asset_summary.digital_employee_count}
                    </strong>
                    <small>
                      {t("projectTemplatePublish.manifest.employees")}
                    </small>
                  </span>
                  <span>
                    <strong>
                      {templateManifest.asset_summary.skill_count}
                    </strong>
                    <small>{t("projectTemplatePublish.manifest.skills")}</small>
                  </span>
                  <span>
                    <strong>
                      {templateManifest.asset_summary.capability_count}
                    </strong>
                    <small>
                      {t("projectTemplatePublish.manifest.platformDependencies")}
                    </small>
                  </span>
                </div>
                <dl className="project-workspace__template-manifest-details">
                  <div>
                    <dt>{t("projectTemplatePublish.manifest.employees")}</dt>
                    <dd>
                      {templateManifest.roles.length
                        ? templateManifest.roles
                            .map((role) => role.name)
                            .join(
                              t(
                                "projectTemplatePublish.manifest.memberSeparator",
                              ),
                            )
                        : t("projectTemplatePublish.manifest.none")}
                    </dd>
                  </div>
                </dl>
                <section className="project-workspace__template-capability-list">
                  <header>
                    <strong>
                      {t("projectTemplatePublish.manifest.capabilityListTitle")}
                    </strong>
                    <span>
                      {t("projectTemplatePublish.manifest.selectedCount", {
                        count: includedTemplateSkillIds.length,
                      })}
                    </span>
                  </header>
                  <p>
                    {t("projectTemplatePublish.manifest.capabilityListHint")}
                  </p>
                  {templateManifest.capabilities.length ? (
                    <div>
                      {templateManifest.capabilities.map((capability, index) => {
                        const capabilityType = capability.type || "tool";
                        const isSkill = capabilityType === "skill";
                        const selectionId =
                          capability.binding_id ||
                          capability.id ||
                          `${capabilityType}-${capability.key || capability.name}-${index}`;
                        const checked = isSkill
                          ? includedTemplateSkillIds.includes(selectionId)
                          : capability.selected !== false;
                        const affectedNames = [
                          ...(capability.affected_members || []).map(
                            (affectedMember) => affectedMember.name,
                          ),
                          capability.member_name || "",
                        ].filter(
                          (name, nameIndex, names) =>
                            Boolean(name) && names.indexOf(name) === nameIndex,
                        );
                        const affectedCount = Math.max(
                          capability.affected_member_count || 0,
                          affectedNames.length,
                        );
                        const availability = [
                          "available",
                          "missing",
                          "restricted",
                        ].includes(capability.availability || "")
                          ? capability.availability
                          : "available";
                        return (
                          <div
                            className="project-workspace__template-capability-row"
                            key={selectionId}
                          >
                            <span className="project-workspace__template-capability-selector">
                              {isSkill ? (
                                <input
                                  type="checkbox"
                                  checked={checked}
                                  disabled={publishingTemplate}
                                  aria-label={t(
                                    "projectTemplatePublish.manifest.selectSkill",
                                    {
                                      name:
                                        capability.name ||
                                        t(
                                          "projectTemplatePublish.manifest.unnamedCapability",
                                        ),
                                    },
                                  )}
                                  onChange={(event) =>
                                    setIncludedTemplateSkillIds((current) =>
                                      event.target.checked
                                        ? [...current, selectionId]
                                        : current.filter(
                                            (id) => id !== selectionId,
                                          ),
                                    )
                                  }
                                />
                              ) : capabilityType === "mcp" ? (
                                <IconCodeDots size={17} />
                              ) : (
                                <IconTool size={17} />
                              )}
                            </span>
                            <span>
                              <strong>
                                {capability.name ||
                                  t(
                                    "projectTemplatePublish.manifest.unnamedCapability",
                                  )}
                              </strong>
                              <small>
                                {t(
                                  `projectTemplatePublish.manifest.types.${capabilityType}`,
                                  {
                                    defaultValue: capabilityType,
                                  },
                                )}
                                {" · "}
                                {isSkill
                                  ? t(
                                      "projectTemplatePublish.manifest.filesAndSize",
                                      {
                                        count: capability.file_count || 0,
                                        size: fileSizeLabel(
                                          capability.size_bytes || 0,
                                        ),
                                      },
                                    )
                                  : t(
                                      "projectTemplatePublish.manifest.platformDependency",
                                    )}
                              </small>
                            </span>
                            <span>
                              <ProjectStatusBadge
                                tone={
                                  isSkill
                                    ? checked
                                      ? "info"
                                      : "neutral"
                                    : availability === "available"
                                      ? "success"
                                      : availability === "restricted"
                                        ? "warning"
                                        : "error"
                                }
                              >
                                {t(
                                  isSkill
                                    ? checked
                                      ? "projectTemplatePublish.manifest.included"
                                      : "projectTemplatePublish.manifest.notIncluded"
                                    : `projectTemplatePublish.manifest.availability.${availability}`,
                                )}
                              </ProjectStatusBadge>
                              <small>
                                {affectedCount
                                  ? t(
                                      "projectTemplatePublish.manifest.affectedMembers",
                                      {
                                        count: affectedCount,
                                        names:
                                          affectedNames.join(
                                            t(
                                              "projectTemplatePublish.manifest.memberSeparator",
                                            ),
                                          ) ||
                                          t(
                                            "projectTemplatePublish.manifest.memberDetailsUnavailable",
                                          ),
                                      },
                                    )
                                  : t(
                                      "projectTemplatePublish.manifest.noAffectedMembers",
                                    )}
                              </small>
                            </span>
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    <div className="project-workspace__template-capability-empty">
                      {t("projectTemplatePublish.manifest.noCapabilities")}
                    </div>
                  )}
                </section>
                <p>{t("projectTemplatePublish.manifest.exclusions")}</p>
              </>
            ) : null}
          </section>
          <ProjectField
            label={t("projectTemplatePublish.name")}
            labelFor="project-template-name"
            error={templateNameError}
            required
          >
            <TextInput
              id="project-template-name"
              value={templateName}
              maxLength={200}
              placeholder={t("projectTemplatePublish.namePlaceholder")}
              disabled={publishingTemplate}
              autoFocus
              onChange={(event) => {
                setTemplateName(event.target.value);
                if (templateNameError) setTemplateNameError("");
              }}
            />
          </ProjectField>
          <footer>
            <Button
              type="button"
              variant="secondary"
              disabled={publishingTemplate}
              onClick={() => setTemplateDialogOpen(false)}
            >
              {t("projectTemplatePublish.cancel")}
            </Button>
            <Button
              type="submit"
              variant="primary"
              disabled={
                publishingTemplate ||
                templateManifestLoading ||
                !templateManifest ||
                Boolean(templateManifestError) ||
                !templateName.trim()
              }
            >
              {publishingTemplate ? (
                <IconLoader2 className="project-workspace__spinner" size={16} />
              ) : (
                <IconSparkles size={16} />
              )}
              {t("projectTemplatePublish.publish")}
            </Button>
          </footer>
        </form>
      </ProjectDialog>
    </>
  );
}
