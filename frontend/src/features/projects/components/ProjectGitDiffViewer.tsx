import { lazy, Suspense, useEffect, useState } from "react";
import {
  IconAlertTriangle,
  IconCodeDots,
  IconLoader2,
} from "@tabler/icons-react";
import { useTranslation } from "react-i18next";

import { projectsApi, type ProjectGitDiff } from "../../../services/projects";
import { Button } from "./ProjectUI";

const ProjectCodeDiffEditor = lazy(async () => {
  const module = await import("./ProjectCodeEditor");
  return { default: module.ProjectCodeDiffEditor };
});

type Props = {
  projectId: string;
  commit: string;
  path: string;
};

export default function ProjectGitDiffViewer({
  projectId,
  commit,
  path,
}: Props) {
  const { t } = useTranslation();
  const [diff, setDiff] = useState<ProjectGitDiff | null>(null);
  const [error, setError] = useState("");
  const [requestKey, setRequestKey] = useState(0);
  const compactCommit = (value: string | null | undefined) =>
    value ? value.slice(0, 10) : t("projectGit.diff.emptyTree");

  useEffect(() => {
    let disposed = false;
    setDiff(null);
    setError("");
    if (!commit || !path)
      return () => {
        disposed = true;
      };
    projectsApi
      .getGitDiff(projectId, { commit, path })
      .then((response) => {
        if (!disposed) setDiff(response);
      })
      .catch((reason: unknown) => {
        if (!disposed)
          setError(
            reason instanceof Error
              ? reason.message
              : t("projectGit.diff.loadFallback"),
          );
      });
    return () => {
      disposed = true;
    };
  }, [commit, path, projectId, requestKey, t]);

  if (!commit)
    return (
      <div className="project-git-diff__state">
        <IconCodeDots size={22} />
        <strong>{t("projectGit.diff.noCommitTitle")}</strong>
        <span>{t("projectGit.diff.noCommitDescription")}</span>
      </div>
    );
  if (error)
    return (
      <div className="project-git-diff__state is-error">
        <IconAlertTriangle size={22} />
        <strong>{t("projectGit.diff.loadFailed")}</strong>
        <span>{error}</span>
        <Button
          variant="secondary"
          onClick={() => setRequestKey((value) => value + 1)}
        >
          {t("projectGit.diff.retry")}
        </Button>
      </div>
    );
  if (!diff)
    return (
      <div className="project-git-diff__state">
        <IconLoader2 className="project-workspace__spinner" size={22} />
        <strong>{t("projectGit.diff.loading")}</strong>
        <span>
          {compactCommit(commit)} · {path}
        </span>
      </div>
    );

  const file = diff.files.find((entry) => entry.path === path) || diff.files[0];
  if (!file)
    return (
      <div className="project-git-diff__state">
        <IconCodeDots size={22} />
        <strong>{t("projectGit.diff.noFileChange")}</strong>
        <span>
          {compactCommit(diff.parent_commit)} → {compactCommit(diff.commit)}
        </span>
      </div>
    );

  return (
    <div className="project-git-diff">
      <div className="project-git-diff__meta">
        <span className={`project-git-diff__status is-${file.status}`}>
          {file.status === "added"
            ? t("projectGit.diff.status.added")
            : file.status === "deleted"
              ? t("projectGit.diff.status.deleted")
              : t("projectGit.diff.status.modified")}
        </span>
        <code>
          {compactCommit(diff.parent_commit)} → {compactCommit(diff.commit)}
        </code>
        {file.additions !== null && (
          <span className="project-git-diff__additions">+{file.additions}</span>
        )}
        {file.deletions !== null && (
          <span className="project-git-diff__deletions">−{file.deletions}</span>
        )}
        {diff.is_root && <span>{t("projectGit.diff.rootCommit")}</span>}
      </div>
      {file.binary ? (
        <div className="project-git-diff__state">
          <IconCodeDots size={22} />
          <strong>{t("projectGit.diff.binaryTitle")}</strong>
          <span>{t("projectGit.diff.binaryDescription")}</span>
        </div>
      ) : (
        <Suspense
          fallback={
            <div className="project-git-diff__state">
              <IconLoader2 className="project-workspace__spinner" size={22} />
              {t("projectGit.diff.loadingEditor")}
            </div>
          }
        >
          <ProjectCodeDiffEditor
            key={`${diff.commit}:${diff.parent_commit || "root"}:${file.path}`}
            path={file.path}
            original={file.original_content || ""}
            modified={file.modified_content || ""}
          />
        </Suspense>
      )}
      {(file.content_truncated || diff.patch_truncated) && (
        <div className="project-git-diff__notice">
          <IconAlertTriangle size={15} />
          {t("projectGit.diff.truncated")}
        </div>
      )}
    </div>
  );
}
