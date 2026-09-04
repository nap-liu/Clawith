import type { ReactNode } from "react";
import {
  IconBolt,
  IconDeviceFloppy,
  IconLoader2,
  IconSettings,
  IconTool,
} from "@tabler/icons-react";
import { useTranslation } from "react-i18next";

import DivergenceSlider from "../../../components/DivergenceSlider";
import ReasoningEffortSelect, { type ReasoningEffortValue } from "../../../components/ReasoningEffortSelect";
import type { ProjectAgentSettingsDraft } from "../types";
import {
  Button,
  ProjectField,
  ProjectSelect,
  ProjectTextarea,
  TextInput,
} from "./ProjectUI";
import ProjectAgentCapabilityPanel from "./ProjectAgentCapabilityPanel";
import "./ProjectAgentSettingsPanel.css";

export type ProjectAgentSettingsSection = "config" | "tools" | "skill";

type ModelOption = { value: string; label: string };

export default function ProjectAgentSettingsPanel({
  title,
  eyebrow,
  headerActions,
  value,
  onChange,
  config,
  onConfigChange,
  modelOptions,
  counts,
  tools,
  skill,
  canManage = true,
  isReadonly = false,
  onSave,
  saving = false,
  saveLabel,
  className = "",
}: {
  title: string;
  eyebrow?: string;
  headerActions?: ReactNode;
  value: ProjectAgentSettingsSection;
  onChange: (value: ProjectAgentSettingsSection) => void;
  config: ProjectAgentSettingsDraft["config_snapshot"];
  onConfigChange: (
    key: keyof ProjectAgentSettingsDraft["config_snapshot"],
    value: string | number | null,
  ) => void;
  modelOptions: ModelOption[];
  counts: Record<ProjectAgentSettingsSection, number>;
  tools: ReactNode;
  skill: ReactNode;
  canManage?: boolean;
  isReadonly?: boolean;
  onSave?: () => void;
  saving?: boolean;
  saveLabel?: string;
  className?: string;
}) {
  const { t } = useTranslation();
  const disabled = isReadonly || !canManage;

  return (
    <section
      className={`project-agent-settings${isReadonly ? " is-readonly" : ""}${className ? ` ${className}` : ""}`}
    >
      <header className="project-agent-settings__header">
        <div>
          {eyebrow ? <span>{eyebrow}</span> : null}
          <h3>{title}</h3>
        </div>
        {headerActions ? (
          <div className="project-agent-settings__actions">{headerActions}</div>
        ) : null}
      </header>

      <ProjectAgentCapabilityPanel
        value={value}
        onChange={onChange}
        ariaLabel={t("projectAgents.capabilityPackage.title")}
        tabs={[
          {
            value: "config",
            icon: <IconSettings size={16} />,
            label: t("projectAgents.capabilityPackage.sections.config"),
            count: t("projectAgents.capabilityPackage.count", {
              count: counts.config,
            }),
          },
          {
            value: "tools",
            icon: <IconTool size={16} />,
            label: t("projectAgents.capabilityPackage.sections.tools"),
            count: t("projectAgents.capabilityPackage.count", {
              count: counts.tools,
            }),
          },
          {
            value: "skill",
            icon: <IconBolt size={16} />,
            label: t("projectAgents.capabilityPackage.sections.skill"),
            count: t("projectAgents.capabilityPackage.count", {
              count: counts.skill,
            }),
          },
        ] as const}
      >
        {value === "config" ? (
          <>
            <div className="project-agent-settings__config-form">
              <ProjectField
                label={t("projectWorkspacePage.members.fields.primaryModel")}
                hint={t("projectWorkspacePage.members.fields.primaryModelHint")}
              >
                <ProjectSelect
                  value={String(config.primary_model_id || "")}
                  options={modelOptions}
                  onChange={(next) =>
                    onConfigChange("primary_model_id", next || null)
                  }
                  ariaLabel={t(
                    "projectWorkspacePage.members.fields.primaryModelAria",
                  )}
                  disabled={disabled}
                />
              </ProjectField>
              <ProjectField
                label={t("projectWorkspacePage.members.fields.fallbackModel")}
              >
                <ProjectSelect
                  value={String(config.fallback_model_id || "")}
                  options={modelOptions}
                  onChange={(next) =>
                    onConfigChange("fallback_model_id", next || null)
                  }
                  ariaLabel={t(
                    "projectWorkspacePage.members.fields.fallbackModelAria",
                  )}
                  disabled={disabled}
                />
              </ProjectField>
              <div className="project-agent-settings__temperature">
                <DivergenceSlider
                  value={config.temperature ?? null}
                  onChange={(temperature) =>
                    onConfigChange("temperature", temperature)
                  }
                  label={t("projectWorkspacePage.members.fields.temperature")}
                  inheritedLabel={t(
                    "projectWorkspacePage.members.fields.temperatureInherited",
                  )}
                  lowLabel={t("projectWorkspacePage.members.fields.temperatureLow")}
                  middleLabel={t(
                    "projectWorkspacePage.members.fields.temperatureMiddle",
                  )}
                  highLabel={t("projectWorkspacePage.members.fields.temperatureHigh")}
                  disabled={disabled}
                />
              </div>
              <ProjectField label={t("reasoning.label")}>
                <ReasoningEffortSelect
                  value={(config.reasoning_effort || "") as ReasoningEffortValue}
                  onChange={(reasoningEffort) =>
                    onConfigChange("reasoning_effort", reasoningEffort || null)
                  }
                  disabled={disabled}
                />
              </ProjectField>
              <ProjectField
                label={t("projectWorkspacePage.members.fields.maxToolRounds")}
              >
                <TextInput
                  type="number"
                  min="1"
                  max="200"
                  value={String(config.max_tool_rounds ?? "")}
                  onChange={(event) =>
                    onConfigChange("max_tool_rounds", event.target.value)
                  }
                  disabled={disabled}
                />
              </ProjectField>
              <ProjectField
                className="is-wide"
                label={t("projectWorkspacePage.members.fields.instructions")}
                hint={t(
                  isReadonly
                    ? "projectWorkspacePage.members.fields.departedHint"
                    : "projectWorkspacePage.members.fields.instructionsHint",
                )}
              >
                <ProjectTextarea
                  value={String(config.project_instruction || "")}
                  onChange={(event) =>
                    onConfigChange("project_instruction", event.target.value)
                  }
                  rows={3}
                  disabled={disabled}
                />
              </ProjectField>
            </div>
            {onSave && canManage && !isReadonly ? (
              <footer>
                <Button variant="primary" onClick={onSave} disabled={saving}>
                  {saving ? (
                    <IconLoader2 className="project-workspace__spinner" size={16} />
                  ) : (
                    <IconDeviceFloppy size={16} />
                  )}
                  {saveLabel ||
                    t("projectWorkspacePage.members.actions.saveSnapshot")}
                </Button>
              </footer>
            ) : null}
          </>
        ) : value === "tools" ? (
          tools
        ) : (
          skill
        )}
      </ProjectAgentCapabilityPanel>
    </section>
  );
}
