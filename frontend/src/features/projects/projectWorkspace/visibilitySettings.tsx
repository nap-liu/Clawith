import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  IconChevronRight,
  IconLoader2,
  IconLock,
  IconUsers,
} from "@tabler/icons-react";

import { useToast } from "../../../components/Toast/ToastProvider";
import OrgMemberAccessPicker, {
  type AgentAccessUser,
} from "../../../components/OrgMemberAccessPicker";
import { projectsApi } from "../../../services/projects";
import {
  Button,
  ProjectField,
  ProjectSelect,
  ProjectSegmentedControl,
  ProjectStatusBadge,
} from "../components/ProjectUI";
import type { ProjectSummary } from "../types";
import { errorMessage, obj } from "./helpers";

export function ProjectVisibilitySettings({
  projectId,
  project,
  onReload,
}: {
  projectId: string;
  project: ProjectSummary;
  onReload: () => Promise<void>;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const canManageAccess = project.can_manage_sharing === true;
  const canManageExecutionUser = project.can_manage_execution_user === true;
  const [visibility, setVisibility] = useState<"private" | "shared">(
    project.visibility,
  );
  const [sharedUserIds, setSharedUserIds] = useState<string[]>(
    project.shared_with_user_ids || [],
  );
  const [sharedUsers, setSharedUsers] = useState<AgentAccessUser[]>(() =>
    (project.shared_with_user_ids || []).map((userId, index) => ({
      id: userId,
      name: project.shared_with_names?.[index] || userId,
      access_level: "use",
    })),
  );
  const [executionUserId, setExecutionUserId] = useState(
    project.execution_user_id || "",
  );
  const [memberPickerOpen, setMemberPickerOpen] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [saving, setSaving] = useState(false);
  const [permissionDenied, setPermissionDenied] = useState(
    project.access_role !== "owner",
  );
  const sharedUserIdsVersion = (project.shared_with_user_ids || []).join("\u0000");
  const sharedUserNamesVersion = (project.shared_with_names || []).join("\u0000");

  useEffect(() => {
    setVisibility(project.visibility);
    setSharedUserIds(project.shared_with_user_ids || []);
    setSharedUsers(
      (project.shared_with_user_ids || []).map((userId, index) => ({
        id: userId,
        name: project.shared_with_names?.[index] || userId,
        access_level: "use",
      })),
    );
    setExecutionUserId(project.execution_user_id || "");
    setPermissionDenied(!canManageAccess);
    setSaveError("");
  }, [
    canManageAccess,
    project.execution_user_id,
    project.updated_at,
    project.visibility,
    sharedUserIdsVersion,
    sharedUserNamesVersion,
  ]);

  const readOnly = permissionDenied || !canManageAccess;
  const sharedWithoutMembers =
    visibility === "shared" && sharedUserIds.length === 0;
  const sharedWithoutExecutionUser =
    canManageExecutionUser &&
    visibility === "shared" &&
    (!executionUserId ||
      (executionUserId !== project.owner_id &&
        !sharedUserIds.includes(executionUserId)));
  const save = async () => {
    if (readOnly) return;
    if (sharedWithoutMembers) {
      setSaveError(t("projectWorkspacePage.visibility.memberRequired"));
      return;
    }
    if (sharedWithoutExecutionUser) {
      setSaveError(t("projectWorkspacePage.visibility.executionUserRequired"));
      return;
    }
    setSaving(true);
    setSaveError("");
    try {
      await projectsApi.update(projectId, {
        visibility,
        shared_with_user_ids: visibility === "shared" ? sharedUserIds : [],
        ...(canManageExecutionUser
          ? {
              execution_user_id:
                visibility === "shared" ? executionUserId : null,
            }
          : {}),
      });
      toast.success(
        t(
          visibility === "shared"
            ? "projectWorkspacePage.visibility.feedback.shared"
            : "projectWorkspacePage.visibility.feedback.private",
        ),
      );
      await onReload();
    } catch (error) {
      const status = Number(obj(error).status);
      const message = errorMessage(
        error,
        t("projectWorkspacePage.errors.requestFailed"),
      );
      if (status === 403 || status === 404) setPermissionDenied(true);
      setSaveError(
        status === 403 || status === 404
          ? t("projectWorkspacePage.visibility.permissionDeniedDraft")
          : message,
      );
      toast.error(message);
    } finally {
      setSaving(false);
    }
  };
  const selectedUsers = sharedUsers.filter((user) =>
    sharedUserIds.includes(user.id),
  );

  return (
    <section className="project-workspace__visibility-settings">
      <header className="project-workspace__settings-section-heading">
        <div>
          <h3>{t("projectWorkspacePage.visibility.title")}</h3>
          <p>{t("projectWorkspacePage.visibility.description")}</p>
        </div>
      </header>
      <div className="project-workspace__visibility-body">
        <ProjectField
          label={
            <span className="project-workspace__visibility-label">
              {t("projectWorkspacePage.visibility.label")}{" "}
              <ProjectStatusBadge
                tone={visibility === "shared" ? "info" : "neutral"}
              >
                {t(
                  visibility === "shared"
                    ? "projectWorkspacePage.visibility.shared"
                    : "projectWorkspacePage.visibility.private",
                )}
              </ProjectStatusBadge>
            </span>
          }
          hint={t("projectWorkspacePage.visibility.privateHint")}
        >
          <ProjectSegmentedControl
            value={visibility}
            options={[
              {
                value: "private",
                label: t("projectWorkspacePage.visibility.private"),
              },
              {
                value: "shared",
                label: t("projectWorkspacePage.visibility.sharedWithMembers"),
              },
            ]}
            onChange={setVisibility}
            ariaLabel={t("projectWorkspacePage.visibility.aria")}
            disabled={readOnly}
          />
        </ProjectField>
        <ProjectField
          label={t("projectWorkspacePage.visibility.members")}
          hint={
            visibility === "shared"
              ? t("projectTerminology.workspace.shareOwnerExcluded")
              : t("projectWorkspacePage.visibility.membersHint")
          }
          error={
            sharedWithoutMembers
              ? t("projectWorkspacePage.visibility.memberRequiredShort")
              : undefined
          }
        >
          <button
            type="button"
            className="project-workspace__member-picker-trigger"
            onClick={() => setMemberPickerOpen(true)}
            disabled={readOnly || visibility !== "shared"}
            aria-label={t("projectWorkspacePage.visibility.selectMembersAria")}
          >
            <span>
              <IconUsers size={17} />
              {selectedUsers.length
                ? t("projectWorkspacePage.visibility.selectedMembers", {
                    count: selectedUsers.length,
                  })
                : t("projectWorkspacePage.visibility.selectMembers")}
            </span>
            <IconChevronRight size={17} />
          </button>
          {selectedUsers.length > 0 && (
            <div className="project-workspace__selected-members" aria-live="polite">
              {selectedUsers.slice(0, 4).map((user) => (
                <span key={user.id}>{user.name}</span>
              ))}
              {selectedUsers.length > 4 && <span>+{selectedUsers.length - 4}</span>}
            </div>
          )}
        </ProjectField>
        {canManageExecutionUser && (
          <ProjectField
            label={t("projectWorkspacePage.visibility.executionUser")}
            hint={t("projectWorkspacePage.visibility.executionUserHint")}
            error={
              sharedWithoutExecutionUser
                ? t(
                    "projectWorkspacePage.visibility.executionUserRequiredShort",
                  )
                : undefined
            }
          >
            <ProjectSelect
              value={visibility === "shared" ? executionUserId : project.owner_id || ""}
              options={[
                {
                  value: project.owner_id || "",
                  label:
                    project.owner_name ||
                    t("projectWorkspacePage.visibility.executionUserOwner"),
                },
                ...selectedUsers.map((user) => ({
                  value: user.id,
                  label: user.name,
                })),
              ].filter((option) => Boolean(option.value))}
              onChange={setExecutionUserId}
              ariaLabel={t("projectWorkspacePage.visibility.executionUser")}
              disabled={readOnly || visibility !== "shared"}
              placeholder={t(
                "projectWorkspacePage.visibility.selectExecutionUser",
              )}
            />
          </ProjectField>
        )}
      </div>
      <footer>
        <div>
          {readOnly && (
            <small className="is-warning">
              {t("projectWorkspacePage.visibility.readOnly")}
            </small>
          )}
          {saveError && (
            <small className="is-error" role="alert">
              {saveError}
            </small>
          )}
        </div>
        {!readOnly && (
          <Button
            variant="secondary"
            onClick={() => void save()}
            disabled={
              readOnly ||
              saving ||
              sharedWithoutMembers ||
              sharedWithoutExecutionUser
            }
          >
            {saving ? (
              <IconLoader2 className="project-workspace__spinner" size={16} />
            ) : (
              <IconLock size={16} />
            )}
            {t("projectWorkspacePage.visibility.save")}
          </Button>
        )}
      </footer>
      <OrgMemberAccessPicker
        open={memberPickerOpen}
        agentId={projectId}
        directoryBaseUrl={`/projects/${projectId}/directory`}
        membersOnly
        users={selectedUsers}
        departments={[]}
        onClose={() => setMemberPickerOpen(false)}
        onSave={async (users) => {
          setSharedUsers(users);
          const userIds = users.map((user) => user.id);
          setSharedUserIds(userIds);
          if (
            canManageExecutionUser &&
            executionUserId !== project.owner_id &&
            !userIds.includes(executionUserId)
          ) {
            setExecutionUserId("");
          }
        }}
      />
    </section>
  );
}
