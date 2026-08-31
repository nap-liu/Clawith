import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import {
  IconAlertTriangle,
  IconBrandGit,
  IconDeviceFloppy,
  IconLoader2,
  IconLock,
  IconPlus,
  IconShieldCheck,
  IconTrash,
  IconX,
} from "@tabler/icons-react";

import { useToast } from "../../../components/Toast/ToastProvider";
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
  ProjectDialog,
  ProjectEmptyState,
  ProjectField,
  ProjectIconButton,
  ProjectStatusBadge,
  TextInput,
} from "../components/ProjectUI";
import type { ProjectSummary } from "../types";
import { errorMessage, text } from "./helpers";
import type { RecordValue } from "./types";

export function GitRepositoryControls({
  projectId,
  project,
  repository,
  commits,
  onReload,
}: {
  projectId: string;
  project: ProjectSummary;
  repository: RecordValue;
  commits: RecordValue[];
  onReload: () => Promise<void>;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const isOwner = project.access_role === "owner";
  const [remotes, setRemotes] = useState<Array<{ name: string; url: string }>>(
    [],
  );
  const [remotesLoading, setRemotesLoading] = useState(false);
  const [remoteError, setRemoteError] = useState("");
  const [remoteName, setRemoteName] = useState("");
  const [remoteUrl, setRemoteUrl] = useState("");
  const [editingRemote, setEditingRemote] = useState("");
  const [cloneUrl, setCloneUrl] = useState("");
  const [cloneBranch, setCloneBranch] = useState("");
  const [cloneConfirmOpen, setCloneConfirmOpen] = useState(false);
  const [busy, setBusy] = useState("");

  const loadRemotes = useCallback(async () => {
    if (!isOwner) {
      setRemotes([]);
      setRemoteError("");
      return;
    }
    setRemotesLoading(true);
    setRemoteError("");
    try {
      setRemotes(await projectsApi.listGitRemotes(projectId));
    } catch (error) {
      setRemoteError(
        errorMessage(error, t("projectWorkspacePage.errors.requestFailed")),
      );
    } finally {
      setRemotesLoading(false);
    }
  }, [isOwner, projectId, t]);
  useEffect(() => {
    void loadRemotes();
  }, [loadRemotes]);

  const repositoryFiles = Array.isArray(repository.files)
    ? repository.files.map(String).sort()
    : [];
  const source = text(repository, "source") === "cloned" ? "cloned" : "managed";
  const initializationOnly =
    source !== "cloned" &&
    commits.length === 1 &&
    [
      "Initialize project",
      "Initialize AI-native project",
      "创建项目初始版本",
    ].includes(text(commits[0], "message", "subject", "title")) &&
    repositoryFiles.length === 2 &&
    repositoryFiles[0] === "PROJECT.json" &&
    repositoryFiles[1] === "README.md";
  const canClone =
    isOwner && project.status === "planning" && initializationOnly;
  const cloneUnavailableReason = !isOwner
    ? t("projectTerminology.workspace.repositoryOwnerRequired")
    : project.status !== "planning"
      ? t("projectWorkspacePage.repository.notPlanning")
      : !initializationOnly
        ? t("projectWorkspacePage.repository.hasDeliverables")
        : "";

  const resetRemoteDraft = () => {
    setEditingRemote("");
    setRemoteName("");
    setRemoteUrl("");
  };
  const saveRemote = async (event: FormEvent) => {
    event.preventDefault();
    if (!isOwner || !remoteName.trim() || !remoteUrl.trim()) return;
    setBusy("remote-save");
    setRemoteError("");
    try {
      await projectsApi.putGitRemote(
        projectId,
        remoteName.trim(),
        remoteUrl.trim(),
      );
      toast.success(
        t(
          editingRemote
            ? "projectWorkspacePage.repository.feedback.remoteUpdated"
            : "projectWorkspacePage.repository.feedback.remoteAdded",
          { name: remoteName.trim() },
        ),
      );
      resetRemoteDraft();
      await loadRemotes();
      await onReload();
    } catch (error) {
      const message = errorMessage(
        error,
        t("projectWorkspacePage.errors.requestFailed"),
      );
      setRemoteError(message);
      toast.error(message);
    } finally {
      setBusy("");
    }
  };
  const deleteRemote = async (name: string) => {
    if (!isOwner) return;
    setBusy(`remote-delete-${name}`);
    setRemoteError("");
    try {
      await projectsApi.deleteGitRemote(projectId, name);
      toast.success(
        t("projectWorkspacePage.repository.feedback.remoteDeleted", { name }),
      );
      if (editingRemote === name) resetRemoteDraft();
      await loadRemotes();
      await onReload();
    } catch (error) {
      const message = errorMessage(
        error,
        t("projectWorkspacePage.errors.requestFailed"),
      );
      setRemoteError(message);
      toast.error(message);
    } finally {
      setBusy("");
    }
  };
  const cloneRepository = async (event: FormEvent) => {
    event.preventDefault();
    if (!canClone || !cloneUrl.trim()) return;
    setBusy("clone");
    try {
      await projectsApi.cloneGitRepository(projectId, {
        url: cloneUrl.trim(),
        branch: cloneBranch.trim() || undefined,
      });
      toast.success(t("projectWorkspacePage.repository.feedback.imported"));
      setCloneConfirmOpen(false);
      setCloneUrl("");
      setCloneBranch("");
      await loadRemotes();
      await onReload();
    } catch (error) {
      toast.error(
        errorMessage(error, t("projectWorkspacePage.errors.requestFailed")),
      );
    } finally {
      setBusy("");
    }
  };

  return (
    <section className="project-workspace__repository-control">
      <header>
        <div>
          <h3>{t("projectWorkspacePage.repository.title")}</h3>
        </div>
      </header>
      <div className="project-workspace__repository-summary">
        <article>
          <span>{t("projectWorkspacePage.repository.source")}</span>
          <strong>
            {t(
              source === "cloned"
                ? "projectWorkspacePage.repository.remoteRepository"
                : "projectWorkspacePage.repository.projectRepository",
            )}
          </strong>
          <small>
            {t(
              source === "cloned"
                ? "projectWorkspacePage.repository.remoteSourceHint"
                : "projectWorkspacePage.repository.projectSourceHint",
            )}
          </small>
        </article>
        <article>
          <span>{t("projectWorkspacePage.repository.defaultBranch")}</span>
          <strong>
            {text(repository, "default_branch") ||
              text(repository, "branch") ||
              t("projectWorkspacePage.notRecorded")}
          </strong>
          <small>
            HEAD <code>{text(repository, "head").slice(0, 12) || "—"}</code>
          </small>
        </article>
        <article>
          <span>{t("projectWorkspacePage.repository.remoteCount")}</span>
          <strong>
            {isOwner
              ? remotes.length
              : t("projectTerminology.workspace.repositoryOwnerVisibility")}
          </strong>
          <small>{t("projectWorkspacePage.repository.standardGitHint")}</small>
        </article>
      </div>
      <div className="project-workspace__repository-grid">
        <section>
          <header>
            <div>
              <span>{t("projectWorkspacePage.repository.remoteSection")}</span>
              <h4>{t("projectWorkspacePage.repository.remoteList")}</h4>
            </div>
            <ProjectCountBadge>
              {isOwner ? remotes.length : 0}
            </ProjectCountBadge>
          </header>
          {isOwner ? (
            remotes.length ? (
              <ProjectDataTable className="project-workspace__remote-table">
                <ProjectDataTableHead>
                  <ProjectDataTableRow>
                    <ProjectDataTableHeader>
                      {t("projectWorkspacePage.repository.columns.name")}
                    </ProjectDataTableHeader>
                    <ProjectDataTableHeader>
                      {t("projectWorkspacePage.repository.columns.url")}
                    </ProjectDataTableHeader>
                    <ProjectDataTableHeader>
                      {t("projectWorkspacePage.repository.columns.actions")}
                    </ProjectDataTableHeader>
                  </ProjectDataTableRow>
                </ProjectDataTableHead>
                <ProjectDataTableBody>
                  {remotes.map((remote) => (
                    <ProjectDataTableRow key={remote.name}>
                      <ProjectDataTableCell>
                        <code>{remote.name}</code>
                      </ProjectDataTableCell>
                      <ProjectDataTableCell>
                        <code title={remote.url}>{remote.url}</code>
                      </ProjectDataTableCell>
                      <ProjectDataTableCell>
                        <div className="project-workspace__remote-actions">
                          <Button
                            variant="ghost"
                            disabled={Boolean(busy)}
                            onClick={() => {
                              setEditingRemote(remote.name);
                              setRemoteName(remote.name);
                              setRemoteUrl(remote.url);
                            }}
                          >
                            {t("common.edit")}
                          </Button>
                          <ProjectIconButton
                            aria-label={t(
                              "projectWorkspacePage.repository.actions.deleteRemote",
                              { name: remote.name },
                            )}
                            disabled={Boolean(busy)}
                            onClick={() => void deleteRemote(remote.name)}
                          >
                            {busy === `remote-delete-${remote.name}` ? (
                              <IconLoader2
                                className="project-workspace__spinner"
                                size={15}
                              />
                            ) : (
                              <IconTrash size={15} />
                            )}
                          </ProjectIconButton>
                        </div>
                      </ProjectDataTableCell>
                    </ProjectDataTableRow>
                  ))}
                </ProjectDataTableBody>
              </ProjectDataTable>
            ) : (
              <ProjectEmptyState
                title={t(
                  remotesLoading
                    ? "projectWorkspacePage.repository.loadingRemotes"
                    : "projectWorkspacePage.repository.noRemotes",
                )}
                description={t(
                  "projectWorkspacePage.repository.noRemotesDescription",
                )}
              />
            )
          ) : (
            <ProjectEmptyState
              icon={<IconLock size={20} />}
              title={t(
                "projectTerminology.workspace.repositoryOwnerManagement",
              )}
              description={t(
                "projectWorkspacePage.repository.memberHistoryHint",
              )}
            />
          )}
          {isOwner && (
            <form
              className="project-workspace__remote-form"
              onSubmit={saveRemote}
            >
              <ProjectField
                label={t("projectWorkspacePage.repository.fields.remoteName")}
                labelFor="project-remote-name"
                required
              >
                <TextInput
                  id="project-remote-name"
                  value={remoteName}
                  onChange={(event) => setRemoteName(event.target.value)}
                  placeholder="origin"
                  pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
                  disabled={Boolean(editingRemote)}
                  required
                />
              </ProjectField>
              <ProjectField
                label={t("projectWorkspacePage.repository.fields.remoteUrl")}
                labelFor="project-remote-url"
                required
              >
                <TextInput
                  id="project-remote-url"
                  value={remoteUrl}
                  onChange={(event) => setRemoteUrl(event.target.value)}
                  placeholder="https://git.example.com/team/project.git"
                  required
                />
              </ProjectField>
              <footer>
                {editingRemote && (
                  <Button
                    type="button"
                    variant="ghost"
                    onClick={resetRemoteDraft}
                  >
                    {t("projectWorkspacePage.repository.actions.cancelEdit")}
                  </Button>
                )}
                <Button
                  type="submit"
                  variant="secondary"
                  disabled={
                    !remoteName.trim() || !remoteUrl.trim() || Boolean(busy)
                  }
                >
                  {busy === "remote-save" ? (
                    <IconLoader2
                      className="project-workspace__spinner"
                      size={15}
                    />
                  ) : editingRemote ? (
                    <IconDeviceFloppy size={15} />
                  ) : (
                    <IconPlus size={15} />
                  )}
                  {t(
                    editingRemote
                      ? "projectWorkspacePage.repository.actions.saveRemote"
                      : "projectWorkspacePage.repository.actions.addRemote",
                  )}
                </Button>
              </footer>
            </form>
          )}
        </section>
        <section>
          <header>
            <div>
              <span>{t("projectWorkspacePage.repository.initialSourceSection")}</span>
              <h4>{t("projectWorkspacePage.repository.initializeRemote")}</h4>
            </div>
            <ProjectStatusBadge tone={canClone ? "success" : "neutral"}>
              {t(
                canClone
                  ? "projectWorkspacePage.repository.cloneAvailable"
                  : "projectWorkspacePage.repository.cloneUnavailable",
              )}
            </ProjectStatusBadge>
          </header>
          <div className="project-workspace__clone-form">
            <ProjectField
              label={t("projectWorkspacePage.repository.fields.repositoryUrl")}
              labelFor="project-clone-url"
              required
            >
              <TextInput
                id="project-clone-url"
                value={cloneUrl}
                onChange={(event) => setCloneUrl(event.target.value)}
                placeholder={t(
                  "projectWorkspacePage.repository.fields.urlPlaceholder",
                )}
                disabled={!canClone || Boolean(busy)}
              />
            </ProjectField>
            <ProjectField
              label={t("projectWorkspacePage.repository.fields.optionalBranch")}
              labelFor="project-clone-branch"
            >
              <TextInput
                id="project-clone-branch"
                value={cloneBranch}
                onChange={(event) => setCloneBranch(event.target.value)}
                placeholder={t(
                  "projectWorkspacePage.repository.fields.branchPlaceholder",
                )}
                disabled={!canClone || Boolean(busy)}
              />
            </ProjectField>
            <div className="project-workspace__clone-note">
              <IconShieldCheck size={17} />
              <p>
                {t("projectTerminology.workspace.repositoryCredentialHint")}
              </p>
            </div>
            {cloneUnavailableReason && <small>{cloneUnavailableReason}</small>}
            <Button
              variant="danger"
              disabled={!canClone || !cloneUrl.trim() || Boolean(busy)}
              onClick={() => setCloneConfirmOpen(true)}
            >
              <IconBrandGit size={16} />
              {t("projectWorkspacePage.repository.actions.confirmSource")}
            </Button>
          </div>
        </section>
      </div>
      {remoteError && (
        <div className="project-workspace__repository-error" role="alert">
          <IconAlertTriangle size={16} />
          <span>{remoteError}</span>
        </div>
      )}
      <ProjectDialog
        open={cloneConfirmOpen}
        onClose={() => {
          if (busy !== "clone") setCloneConfirmOpen(false);
        }}
        ariaLabel={t("projectWorkspacePage.repository.confirmAria")}
        className="project-workspace__git-dialog"
      >
        <form className="project-workspace__modal" onSubmit={cloneRepository}>
          <header>
            <div>
              <span>{t("projectWorkspacePage.repository.importEyebrow")}</span>
              <h2>{t("projectWorkspacePage.repository.importTitle")}</h2>
            </div>
            <ProjectIconButton
              aria-label={t("common.close")}
              disabled={busy === "clone"}
              onClick={() => setCloneConfirmOpen(false)}
            >
              <IconX size={18} />
            </ProjectIconButton>
          </header>
          <p>{t("projectWorkspacePage.repository.importDescription")}</p>
          <dl className="project-workspace__definition-list">
            <div>
              <dt>{t("projectWorkspacePage.repository.remote")}</dt>
              <dd>
                <code>{cloneUrl}</code>
              </dd>
            </div>
            <div>
              <dt>{t("projectWorkspacePage.repository.branch")}</dt>
              <dd>
                <code>
                  {cloneBranch ||
                    t("projectWorkspacePage.repository.remoteDefaultBranch")}
                </code>
              </dd>
            </div>
          </dl>
          <div className="project-workspace__safe-note">
            <IconLock size={16} />
            <span>
              {t("projectWorkspacePage.repository.credentialsWarning")}
            </span>
          </div>
          <footer>
            <Button
              type="button"
              variant="secondary"
              disabled={busy === "clone"}
              onClick={() => setCloneConfirmOpen(false)}
            >
              {t("common.cancel")}
            </Button>
            <Button type="submit" variant="primary" disabled={busy === "clone"}>
              {busy === "clone" ? (
                <IconLoader2 className="project-workspace__spinner" size={16} />
              ) : (
                <IconBrandGit size={16} />
              )}
              {t("projectWorkspacePage.repository.actions.import")}
            </Button>
          </footer>
        </form>
      </ProjectDialog>
    </section>
  );
}
