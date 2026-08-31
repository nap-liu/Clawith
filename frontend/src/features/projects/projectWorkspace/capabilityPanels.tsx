import { useContext, useMemo } from "react";
import { useTranslation } from "react-i18next";
import {
  IconCircleCheck,
  IconClock,
  IconCodeDots,
  IconTool,
  IconUsers,
  IconX,
} from "@tabler/icons-react";

import { projectsApi } from "../../../services/projects";
import {
  Button,
  ProjectCountBadge,
  ProjectDataTable,
  ProjectDataTableBody,
  ProjectDataTableCell,
  ProjectDataTableHead,
  ProjectDataTableHeader,
  ProjectDataTableRow,
  ProjectEmptyState,
  ProjectSegmentedControl,
  ProjectSelect,
  ProjectStatusBadge,
  ToggleSwitch,
} from "../components/ProjectUI";
import { PROJECT_TOOL_REGISTRY } from "./config";
import {
  bool,
  obj,
  projectToolResolution,
  stringList,
  text,
} from "./helpers";
import { WorkspaceNavigationContext, useWorkspacePagination } from "./navigation";
import { EmptyState, SectionHeading } from "./shared";
import type { ProjectToolDefinition, RecordValue } from "./types";

export function ProjectToolsControl({
  projectId,
  members,
  policies,
  runAction,
  busyAction,
  canManage,
  fixedMemberId,
  compact = false,
}: {
  projectId: string;
  members: RecordValue[];
  policies: RecordValue | null;
  runAction: (
    key: string,
    action: () => Promise<unknown>,
    success: string,
  ) => Promise<boolean>;
  busyAction: string;
  canManage: boolean;
  fixedMemberId?: string;
  compact?: boolean;
}) {
  const { t } = useTranslation();
  const { get, update } = useContext(WorkspaceNavigationContext);
  const activeMembers = useMemo(
    () => members.filter((entry) => entry.is_enabled !== false),
    [members],
  );
  const requestedMemberId = get("toolMember");
  const selectedMemberId = fixedMemberId
    ? fixedMemberId
    : activeMembers.some(
          (member) => text(member, "id", "member_id") === requestedMemberId,
        )
      ? requestedMemberId
      : text(activeMembers[0] || {}, "id", "member_id");
  const member =
    members.find(
      (entry) => text(entry, "id", "member_id") === selectedMemberId,
    ) || (fixedMemberId ? undefined : activeMembers[0]);
  const memberId = text(member || {}, "id", "member_id");
  const memberName =
    text(member || {}, "name_snapshot", "agent_name", "name") ||
    t("projectAgents.defaultRole");
  const effectiveCount = member
    ? PROJECT_TOOL_REGISTRY.filter(
        (tool) => projectToolResolution(tool, member, policies).effective,
      ).length
    : 0;
  const { pageItems: visibleTools, pagination: toolsPagination } =
    useWorkspacePagination(PROJECT_TOOL_REGISTRY, "projectTools", 6, [6, 12]);
  const toggleTool = (tool: ProjectToolDefinition, checked: boolean) => {
    if (!member || !memberId) return;
    const resolution = projectToolResolution(tool, member, policies);
    if (
      !resolution.roleCeiling ||
      resolution.policyBlocked ||
      resolution.snapshotBlocked
    )
      return;
    const config = obj(member.config_snapshot);
    const existingDisabled = stringList(config.disabled_project_tools);
    const disabled = new Set(existingDisabled);
    if (checked) disabled.delete(tool.name);
    else disabled.add(tool.name);
    const knownNames = new Set(
      PROJECT_TOOL_REGISTRY.map((entry) => entry.name),
    );
    const disabledProjectTools = [
      ...existingDisabled.filter((name) => !knownNames.has(name)),
      ...PROJECT_TOOL_REGISTRY.map((entry) => entry.name).filter((name) =>
        disabled.has(name),
      ),
    ];
    void runAction(
      `project-tool-${memberId}-${tool.name}`,
      () =>
        projectsApi.patchMember(projectId, memberId, {
          config_snapshot: {
            ...config,
            disabled_project_tools: disabledProjectTools,
          },
        }),
      t("projectManagementTools.updated", { name: memberName }),
    );
  };
  const renderedTools = compact ? PROJECT_TOOL_REGISTRY : visibleTools;
  return (
    <section
      className={`project-workspace__project-tools${compact ? " is-compact" : ""}`}
    >
      <header className="project-workspace__subsection-heading">
        <div>
          <h3>{t("projectManagementTools.title")}</h3>
          {!compact ? <p>{t("projectManagementTools.description")}</p> : null}
        </div>
        {member && !compact ? (
          <div className="project-workspace__project-tool-member">
            <ProjectSelect
              value={memberId}
              options={activeMembers.map((entry) => ({
                value: text(entry, "id", "member_id"),
                label: bool(entry, "is_leader")
                  ? t("projectTerminology.workspace.memberOwner", {
                      name: text(entry, "name_snapshot", "agent_name", "name"),
                    })
                  : text(entry, "name_snapshot", "agent_name", "name"),
              }))}
              onChange={(value) => update({ toolMember: value })}
              ariaLabel={t("projectManagementTools.selectMember")}
            />
            <ProjectCountBadge>
              {effectiveCount} / {PROJECT_TOOL_REGISTRY.length}
            </ProjectCountBadge>
          </div>
        ) : member ? (
          <ProjectCountBadge>
            {effectiveCount} / {PROJECT_TOOL_REGISTRY.length}
          </ProjectCountBadge>
        ) : null}
      </header>
      {member ? (
        <>
          <div className="project-workspace__project-tool-grid">
            {renderedTools.map((tool) => {
              const resolution = projectToolResolution(tool, member, policies);
              const blockedLabel = !resolution.roleCeiling
                ? t("projectTerminology.ownerDedicated")
                : resolution.policyBlocked
                  ? t("projectManagementTools.status.policyBlocked")
                  : resolution.snapshotBlocked
                    ? t("projectManagementTools.status.snapshotBlocked")
                    : resolution.memberDisabled
                      ? t("projectManagementTools.status.memberDisabled")
                      : t("projectManagementTools.status.available");
              const tone = resolution.effective
                ? "success"
                : resolution.memberDisabled
                  ? "neutral"
                  : "warning";
              const actionKey = `project-tool-${memberId}-${tool.name}`;
              return (
                <article
                  key={tool.name}
                  className={resolution.effective ? "is-effective" : ""}
                >
                  <header>
                    <span>
                      <IconTool size={16} />
                    </span>
                    <div>
                      <strong>
                        {t(
                          `projectManagementTools.registry.${tool.name}.label`,
                          {
                            defaultValue: tool.label,
                          },
                        )}
                      </strong>
                    </div>
                    {canManage ? (
                      <ToggleSwitch
                        checked={resolution.effective}
                        onChange={(checked) => toggleTool(tool, checked)}
                        ariaLabel={t(
                          resolution.effective
                            ? "projectManagementTools.disableTool"
                            : "projectManagementTools.enableTool",
                          {
                            name: t(
                              `projectManagementTools.registry.${tool.name}.label`,
                              { defaultValue: tool.label },
                            ),
                          },
                        )}
                        disabled={
                          !resolution.roleCeiling ||
                          resolution.policyBlocked ||
                          resolution.snapshotBlocked ||
                          busyAction === actionKey
                        }
                      />
                    ) : (
                      <ProjectStatusBadge tone={tone}>
                        {blockedLabel}
                      </ProjectStatusBadge>
                    )}
                  </header>
                  <p>
                    {tool.descriptionKey
                      ? t(tool.descriptionKey)
                      : t(
                          `projectManagementTools.registry.${tool.name}.description`,
                          { defaultValue: tool.description },
                        )}
                  </p>
                </article>
              );
            })}
          </div>
          {!compact ? toolsPagination : null}
        </>
      ) : (
        <ProjectEmptyState
          icon={<IconUsers size={22} />}
          title={t("projectManagementTools.emptyTitle")}
          description={t("projectManagementTools.emptyDescription")}
        />
      )}
    </section>
  );
}

export function CapabilitiesPanel({
  members,
  capabilities,
  policies,
}: {
  members: RecordValue[];
  capabilities: RecordValue[];
  policies: RecordValue | null;
}) {
  const { t } = useTranslation();
  const { get, update } = useContext(WorkspaceNavigationContext);
  const view = get("capView") === "matrix" ? "matrix" : "list";
  const requestedFilter = get("capFilter");
  const filter = ["all", "skill", "mcp", "project", "agent"].includes(
    requestedFilter,
  )
    ? requestedFilter
    : "all";
  const departedAgentIds = new Set(
    members
      .filter((member) => member.is_enabled === false)
      .map((member) => text(member, "agent_id"))
      .filter(Boolean),
  );
  const filteredCapabilities = capabilities.filter((cap) => {
    if (filter === "all") return true;
    if (["skill", "mcp"].includes(filter))
      return text(cap, "capability_type", "kind", "type") === filter;
    const inherited =
      Boolean(text(cap, "inherited_from_agent_id")) ||
      ["agent", "inherited"].includes(text(cap, "source"));
    return filter === "agent" ? inherited : !inherited;
  });
  const { pageItems: visibleCapabilities, pagination } = useWorkspacePagination(
    filteredCapabilities,
    "capabilities",
    12,
    [12, 24, 48],
  );
  return (
    <>
      <SectionHeading
        eyebrow="CAPABILITY CONTROL"
        title={t("projectAgents.teamPage.capabilities")}
        description={t("projectAgents.teamPage.capabilitiesDescription")}
        actions={
          <ProjectSegmentedControl
            value={view}
            options={[
              {
                value: "list",
                label: t("projectAgents.teamPage.listView"),
              },
              {
                value: "matrix",
                label: t("projectAgents.teamPage.matrixView"),
              },
            ]}
            onChange={(nextView) =>
              update({
                capView: nextView === "matrix" ? "matrix" : undefined,
              })
            }
            ariaLabel={t("projectAgents.teamPage.viewModeAria")}
          />
        }
      />
      {view === "matrix" ? (
        <CapabilityMatrix
          members={members}
          capabilities={capabilities}
          policies={policies}
        />
      ) : (
        <>
          {capabilities.length ? (
            <>
              <ProjectSegmentedControl
                className="project-workspace__filters"
                value={filter}
                options={[
                  { value: "all", label: t("common.all") },
                  { value: "skill", label: "Skill" },
                  { value: "mcp", label: "MCP" },
                  {
                    value: "project",
                    label: t("projectWorkspacePage.capabilities.projectShared"),
                  },
                  {
                    value: "agent",
                    label: t("projectWorkspacePage.capabilities.inherited"),
                  },
                ]}
                onChange={(value) =>
                  update({ capFilter: value, capabilitiesPage: undefined })
                }
                ariaLabel={t("projectWorkspacePage.capabilities.filterAria")}
              />
              {filteredCapabilities.length ? (
                <>
                  <div className="project-workspace__cap-grid">
                    {visibleCapabilities.map((cap) => {
                      const id = text(cap, "id", "binding_id", "capability_id");
                      const capabilityType = text(
                        cap,
                        "capability_type",
                        "kind",
                        "type",
                      );
                      const enabled = cap.is_enabled !== false;
                      const scopeCount = Object.keys(obj(cap.scope)).length;
                      const inheritedFromAgentId = text(
                        cap,
                        "inherited_from_agent_id",
                      );
                      const inheritedFromMember = members.find(
                        (member) => text(member, "agent_id") === inheritedFromAgentId,
                      );
                      const inheritedFromName = text(
                        inheritedFromMember || {},
                        "name_snapshot",
                        "agent_name",
                        "name",
                      );
                      const departedOwner = Boolean(
                        inheritedFromAgentId &&
                        departedAgentIds.has(inheritedFromAgentId),
                      );
                      return (
                        <article
                          key={id}
                          className={departedOwner ? "is-readonly" : ""}
                        >
                          <header>
                            <span className={`is-${capabilityType || "skill"}`}>
                              {capabilityType === "mcp" ? (
                                <IconCodeDots size={17} />
                              ) : (
                                <IconTool size={17} />
                              )}
                            </span>
                            <div>
                              <strong>
                                {text(cap, "name", "capability_name")}
                              </strong>
                              <small>
                                {text(cap, "version") ||
                                  capabilityType.toUpperCase() ||
                                  t(
                                    "projectWorkspacePage.capabilities.capability",
                                  )}{" "}
                                ·{" "}
                                {inheritedFromAgentId
                                  ? t(
                                      "projectWorkspacePage.capabilities.inheritedFrom",
                                      {
                                        name: inheritedFromName ||
                                          t("projectTerminology.dynamicCopy.digitalEmployee"),
                                        departed: departedOwner
                                          ? t(
                                              "projectWorkspacePage.capabilities.departedSuffix",
                                            )
                                          : "",
                                      },
                                    )
                                  : t(
                                      "projectWorkspacePage.capabilities.projectShared",
                                    )}
                              </small>
                            </div>
                            <ProjectStatusBadge
                              tone={
                                enabled && !departedOwner
                                  ? "success"
                                  : "neutral"
                              }
                            >
                              {t(
                                enabled && !departedOwner
                                  ? "projectManagementTools.status.available"
                                  : "projectManagementTools.status.memberDisabled",
                              )}
                            </ProjectStatusBadge>
                          </header>
                          <p>
                            {departedOwner
                              ? t(
                                  "projectWorkspacePage.capabilities.departedDescription",
                                )
                              : text(cap, "description") ||
                                t(
                                  "projectWorkspacePage.capabilities.noDescription",
                                )}
                          </p>
                          <footer>
                            <span>
                              {scopeCount
                                ? t(
                                    "projectWorkspacePage.capabilities.scopeCount",
                                    {
                                      count: scopeCount,
                                    },
                                  )
                                : t(
                                    "projectWorkspacePage.capabilities.noScope",
                                  )}
                            </span>
                            {departedOwner ? (
                              <ProjectStatusBadge tone="neutral">
                                {t(
                                  "projectWorkspacePage.members.historical.title",
                                )}
                              </ProjectStatusBadge>
                            ) : null}
                          </footer>
                        </article>
                      );
                    })}
                  </div>
                  {pagination}
                </>
              ) : (
                <EmptyState
                  icon={<IconTool size={22} />}
                  title={t("projectAgents.teamPage.capabilitiesEmptyTitle")}
                  description={t(
                    "projectAgents.teamPage.capabilitiesEmptyDescription",
                  )}
                />
              )}
            </>
          ) : (
            <EmptyState
              icon={<IconTool size={22} />}
              title={t("projectAgents.teamPage.capabilitiesEmptyTitle")}
              description={t(
                "projectAgents.teamPage.capabilitiesEmptyDescription",
              )}
            />
          )}
        </>
      )}
    </>
  );
}

export function CapabilityBindingsMatrix({
  members,
  capabilities,
}: {
  members: RecordValue[];
  capabilities: RecordValue[];
}) {
  const { t } = useTranslation();
  const { pageItems: visibleCapabilities, pagination } = useWorkspacePagination(
    capabilities,
    "capabilityMatrix",
    10,
    [10, 20, 50],
  );
  return (
    <>
      <SectionHeading
        eyebrow="EFFECTIVE CAPABILITIES"
        title={t("projectWorkspaceNav.tabs.matrix")}
        description={t(
          "projectTerminology.workspace.capabilityMatrixDescription",
        )}
      />
      {members.length && capabilities.length ? (
        <>
          <div className="project-workspace__matrix-wrap">
            <ProjectDataTable>
              <ProjectDataTableHead>
                <ProjectDataTableRow>
                  <ProjectDataTableHeader>
                    {t("projectWorkspacePage.capabilities.columns.capability")}
                  </ProjectDataTableHeader>
                  <ProjectDataTableHeader>
                    {t("projectWorkspacePage.capabilities.columns.source")}
                  </ProjectDataTableHeader>
                  {members.map((member) => (
                    <ProjectDataTableHeader
                      key={text(member, "agent_id", "id", "member_id")}
                    >
                      {text(member, "agent_name", "name_snapshot", "name")}
                      <small>
                        {member.is_enabled === false
                          ? t(
                              "projectWorkspacePage.capabilities.departedHistorical",
                            )
                          : bool(member, "is_leader")
                            ? t("projectTerminology.owner")
                            : t("projectAgents.status.active")}
                      </small>
                    </ProjectDataTableHeader>
                  ))}
                </ProjectDataTableRow>
              </ProjectDataTableHead>
              <ProjectDataTableBody>
                {visibleCapabilities.map((cap) => {
                  const inheritedAgentId = text(cap, "inherited_from_agent_id");
                  const shared = text(cap, "source") === "shared";
                  const inherited =
                    Boolean(inheritedAgentId) ||
                    ["agent", "inherited"].includes(text(cap, "source"));
                  return (
                    <ProjectDataTableRow
                      key={text(cap, "id", "binding_id", "capability_id")}
                    >
                      <ProjectDataTableCell>
                        <strong>{text(cap, "name", "capability_name")}</strong>
                        <small>
                          {text(cap, "capability_type", "kind", "type")}
                        </small>
                      </ProjectDataTableCell>
                      <ProjectDataTableCell>
                        {t(
                          inherited
                            ? "projectWorkspacePage.capabilities.inherited"
                            : "projectWorkspacePage.capabilities.projectShared",
                        )}
                      </ProjectDataTableCell>
                      {members.map((member) => {
                        const id = text(member, "agent_id", "id", "member_id");
                        const enabled = cap.is_enabled !== false;
                        const resolved: "yes" | "no" | "unknown" =
                          member.is_enabled === false || !enabled
                            ? "no"
                            : inheritedAgentId
                              ? inheritedAgentId === id
                                ? "yes"
                                : "no"
                              : shared
                                ? "yes"
                                : "no";
                        return (
                          <ProjectDataTableCell key={id}>
                            <span
                              className={`project-workspace__matrix-${resolved}`}
                            >
                              {resolved === "yes" ? (
                                <IconCircleCheck size={17} />
                              ) : resolved === "no" ? (
                                <IconX size={16} />
                              ) : (
                                <span aria-hidden="true">—</span>
                              )}
                              <small>
                                {member.is_enabled === false
                                  ? t(
                                      "projectWorkspacePage.members.historical.title",
                                    )
                                  : resolved === "yes"
                                    ? t(
                                        "projectManagementTools.status.available",
                                      )
                                    : resolved === "no"
                                      ? t(
                                          "projectManagementTools.status.unavailable",
                                        )
                                      : t(
                                          "projectWorkspacePage.capabilities.policyResolved",
                                        )}
                              </small>
                            </span>
                          </ProjectDataTableCell>
                        );
                      })}
                    </ProjectDataTableRow>
                  );
                })}
              </ProjectDataTableBody>
            </ProjectDataTable>
          </div>
          {pagination}
        </>
      ) : (
        <EmptyState
          icon={<IconCodeDots size={22} />}
          title={t("projectAgents.teamPage.matrixEmptyTitle")}
          description={t("projectAgents.teamPage.matrixEmptyDescription")}
        />
      )}
    </>
  );
}

export function ProjectToolsMatrix({
  members,
  policies,
}: {
  members: RecordValue[];
  policies: RecordValue | null;
}) {
  const { t } = useTranslation();
  const { pageItems: visibleTools, pagination } = useWorkspacePagination(
    PROJECT_TOOL_REGISTRY,
    "projectToolMatrix",
    10,
    [10, 20],
  );
  return (
    <section className="project-workspace__project-tool-matrix">
      <header className="project-workspace__subsection-heading">
        <div>
          <h3>{t("projectManagementTools.matrixTitle")}</h3>
          <p>{t("projectManagementTools.matrixDescription")}</p>
        </div>
        <ProjectCountBadge>
          {t("projectManagementTools.toolCount", {
            count: PROJECT_TOOL_REGISTRY.length,
          })}
        </ProjectCountBadge>
      </header>
      {members.length ? (
        <>
          <div className="project-workspace__matrix-wrap">
            <ProjectDataTable>
              <ProjectDataTableHead>
                <ProjectDataTableRow>
                  <ProjectDataTableHeader>
                    {t("projectManagementTools.toolColumn")}
                  </ProjectDataTableHeader>
                  <ProjectDataTableHeader>
                    {t("projectManagementTools.scopeColumn")}
                  </ProjectDataTableHeader>
                  {members.map((member) => (
                    <ProjectDataTableHeader
                      key={text(member, "id", "member_id", "agent_id")}
                    >
                      {text(member, "name_snapshot", "agent_name", "name")}
                      <small>
                        {member.is_enabled === false
                          ? t("projectAgents.status.departed")
                          : bool(member, "is_leader")
                            ? t("projectTerminology.owner")
                            : t("projectTerminology.workspace.projectMembers")}
                      </small>
                    </ProjectDataTableHeader>
                  ))}
                </ProjectDataTableRow>
              </ProjectDataTableHead>
              <ProjectDataTableBody>
                {visibleTools.map((tool) => (
                  <ProjectDataTableRow key={tool.name}>
                    <ProjectDataTableCell>
                      <strong>
                        {t(
                          `projectManagementTools.registry.${tool.name}.label`,
                          {
                            defaultValue: tool.label,
                          },
                        )}
                      </strong>
                    </ProjectDataTableCell>
                    <ProjectDataTableCell>
                      {tool.participant
                        ? t("projectTerminology.workspace.allMembers")
                        : t("projectTerminology.ownerOnly")}
                    </ProjectDataTableCell>
                    {members.map((member) => {
                      const resolution = projectToolResolution(
                        tool,
                        member,
                        policies,
                      );
                      const label = resolution.lifecycleBlocked
                        ? t("projectManagementTools.status.memberDeparted")
                        : resolution.effective
                          ? t("projectManagementTools.status.available")
                          : !resolution.roleCeiling
                            ? t("projectManagementTools.status.unavailable")
                            : resolution.memberDisabled
                              ? t(
                                  "projectManagementTools.status.memberDisabled",
                                )
                              : resolution.policyBlocked
                                ? t(
                                    "projectManagementTools.status.policyBlocked",
                                  )
                                : t("projectManagementTools.status.restricted");
                      return (
                        <ProjectDataTableCell
                          key={text(member, "id", "member_id", "agent_id")}
                        >
                          <span
                            className={
                              resolution.effective
                                ? "project-workspace__matrix-yes"
                                : resolution.lifecycleBlocked ||
                                    !resolution.roleCeiling
                                  ? "project-workspace__matrix-no"
                                  : "project-workspace__matrix-unknown"
                            }
                          >
                            {resolution.effective ? (
                              <IconCircleCheck size={17} />
                            ) : resolution.lifecycleBlocked ||
                              !resolution.roleCeiling ? (
                              <IconX size={16} />
                            ) : (
                              <IconClock size={16} />
                            )}
                            <small>{label}</small>
                          </span>
                        </ProjectDataTableCell>
                      );
                    })}
                  </ProjectDataTableRow>
                ))}
              </ProjectDataTableBody>
            </ProjectDataTable>
          </div>
          {pagination}
        </>
      ) : (
        <ProjectEmptyState
          icon={<IconUsers size={22} />}
          title={t("projectManagementTools.matrixEmptyTitle")}
          description={t("projectManagementTools.matrixEmptyDescription")}
        />
      )}
    </section>
  );
}

export function CapabilityMatrix({
  members,
  capabilities,
  policies,
}: {
  members: RecordValue[];
  capabilities: RecordValue[];
  policies: RecordValue | null;
}) {
  return (
    <>
      <CapabilityBindingsMatrix members={members} capabilities={capabilities} />
      <ProjectToolsMatrix members={members} policies={policies} />
    </>
  );
}
