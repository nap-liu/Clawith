import { lazy, Suspense, useEffect, useState } from "react";
import {
  IconAlertTriangle,
  IconCodeDots,
  IconLoader2,
} from "@tabler/icons-react";

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

const compactCommit = (value: string | null | undefined) =>
  value ? value.slice(0, 10) : "空树";

export default function ProjectGitDiffViewer({
  projectId,
  commit,
  path,
}: Props) {
  const [diff, setDiff] = useState<ProjectGitDiff | null>(null);
  const [error, setError] = useState("");
  const [requestKey, setRequestKey] = useState(0);

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
            reason instanceof Error ? reason.message : "无法读取 Git 变更",
          );
      });
    return () => {
      disposed = true;
    };
  }, [commit, path, projectId, requestKey]);

  if (!commit)
    return (
      <div className="project-git-diff__state">
        <IconCodeDots size={22} />
        <strong>没有关联版本</strong>
        <span>该文件尚未关联版本记录。</span>
      </div>
    );
  if (error)
    return (
      <div className="project-git-diff__state is-error">
        <IconAlertTriangle size={22} />
        <strong>Git 变更加载失败</strong>
        <span>{error}</span>
        <Button
          variant="secondary"
          onClick={() => setRequestKey((value) => value + 1)}
        >
          重试
        </Button>
      </div>
    );
  if (!diff)
    return (
      <div className="project-git-diff__state">
        <IconLoader2 className="project-workspace__spinner" size={22} />
        <strong>正在读取文件变更</strong>
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
        <strong>该 Commit 没有修改此文件</strong>
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
            ? "新增"
            : file.status === "deleted"
              ? "删除"
              : "修改"}
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
        {diff.is_root && <span>根提交</span>}
      </div>
      {file.binary ? (
        <div className="project-git-diff__state">
          <IconCodeDots size={22} />
          <strong>二进制文件变更</strong>
          <span>二进制文件不支持在线对比。</span>
        </div>
      ) : (
        <Suspense
          fallback={
            <div className="project-git-diff__state">
              <IconLoader2 className="project-workspace__spinner" size={22} />
              正在载入差异视图…
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
          内容较大，当前仅展示部分差异。
        </div>
      )}
    </div>
  );
}
