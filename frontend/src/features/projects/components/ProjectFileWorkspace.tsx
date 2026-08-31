import {
  IconAlertTriangle,
  IconBrandGit,
  IconCode,
  IconDeviceFloppy,
  IconDownload,
  IconEye,
  IconFileUnknown,
  IconFolder,
  IconLoader2,
  IconMusic,
  IconPlus,
  IconRefresh,
} from "@tabler/icons-react";
import {
  lazy,
  Suspense,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { useTranslation } from "react-i18next";

import ResizableSplitPane from "../../../components/ui/ResizableSplitPane";
import MarkdownRenderer from "../../../components/MarkdownRenderer";
import {
  projectsApi,
  type ProjectFileContent,
} from "../../../services/projects";
import { formatFileSize } from "../../../utils/formatFileSize";
import { Button, TextInput } from "./ProjectUI";
import {
  TreeRows,
  filePath,
  folderPaths,
  folderPathsForNode,
  number,
  treeFromFiles,
  type Props,
  type TreeNode,
} from "./projectFileWorkspace/tree";
import "./ProjectFileWorkspace.css";

const ProjectCodeEditor = lazy(() => import("./ProjectCodeEditor"));

function MetaLine({ content }: { content: ProjectFileContent }) {
  const { t } = useTranslation();
  return (
    <div
      className="project-file-workspace__meta"
      aria-label={t("projectWorkspaceFiles.metadataAria")}
    >
      <span>{formatFileSize(content.size) || "0 B"}</span>
      <span>{content.mime_type || "application/octet-stream"}</span>
      <span title={content.commit}>
        {content.commit
          ? `${t("projectGit.commit")} ${content.commit.slice(0, 10)}`
          : t("projectWorkspaceFiles.notCommitted")}
      </span>
    </div>
  );
}

function MediaPreview({
  content,
  onUnavailable,
}: {
  content: ProjectFileContent;
  onUnavailable: () => void;
}) {
  const { t } = useTranslation();
  if (content.kind === "image") {
    return (
      <div className="project-file-workspace__image-stage">
        <img src={content.raw_url} alt={content.name} onError={onUnavailable} />
      </div>
    );
  }
  if (content.kind === "video") {
    return (
      <div className="project-file-workspace__media-stage">
        <video
          key={content.raw_url}
          src={content.raw_url}
          controls
          playsInline
          preload="metadata"
          onError={onUnavailable}
        />
      </div>
    );
  }
  if (content.kind === "audio") {
    return (
      <div className="project-file-workspace__media-stage project-file-workspace__media-stage--audio">
        <div className="project-file-workspace__audio-art">
          <IconMusic size={34} />
        </div>
        <strong>{content.name}</strong>
        <audio
          key={content.raw_url}
          src={content.raw_url}
          controls
          preload="metadata"
          onError={onUnavailable}
        />
      </div>
    );
  }
  return (
    <div className="project-file-workspace__binary-state">
      <span>
        <IconFileUnknown size={30} />
      </span>
      <h3>{t("projectWorkspaceFiles.unavailableTitle")}</h3>
      <p>{t("projectWorkspaceFiles.unavailableDescription")}</p>
      <a
        className="btn btn-secondary"
        href={content.download_url}
        download={content.name}
      >
        <IconDownload size={16} />
        {t("projectWorkspaceFiles.downloadFile")}
      </a>
    </div>
  );
}

export default function ProjectFileWorkspace({
  projectId,
  files,
  projectAgents = [],
  selectedPath: requestedPath = "",
  onSelectedPathChange,
  selectedView = "preview",
  onSelectedViewChange,
  runAction,
  busyAction,
  canWrite = true,
}: Props) {
  const { t } = useTranslation();
  const firstPath = filePath(files[0] || {});
  const initialPath =
    requestedPath && files.some((entry) => filePath(entry) === requestedPath)
      ? requestedPath
      : firstPath;
  const [selectedPath, setSelectedPath] = useState(initialPath);
  const [creating, setCreating] = useState(canWrite && !firstPath);
  const [draftPath, setDraftPath] = useState(firstPath);
  const [content, setContent] = useState<ProjectFileContent | null>(null);
  const [draftContent, setDraftContent] = useState("");
  const [loading, setLoading] = useState(Boolean(firstPath));
  const [loadError, setLoadError] = useState("");
  const [reloadKey, setReloadKey] = useState(0);
  const [htmlPreview, setHtmlPreview] = useState(false);
  const [downloadingPath, setDownloadingPath] = useState("");
  const [downloadError, setDownloadError] = useState("");
  const tree = useMemo(() => treeFromFiles(files), [files]);
  const projectAgentNames = useMemo(
    () => new Map(projectAgents.map((agent) => [agent.id, agent.name])),
    [projectAgents],
  );
  const treeLabel = (node: TreeNode) => {
    if (node.virtualRoot === "project")
      return t("projectWorkspaceFiles.projectFilesRoot");
    if (node.path === ".agents")
      return t("projectWorkspaceFiles.employeeFilesRoot");
    const segments = node.path.split("/");
    if (segments[0] === ".agents" && segments.length === 2) {
      return projectAgentNames.get(segments[1]) || node.name;
    }
    return node.name;
  };
  const allFolders = useMemo(() => folderPaths(tree), [tree]);
  const knownFoldersRef = useRef(new Set(allFolders));
  const mediaRecoveryPathRef = useRef("");
  const [expanded, setExpanded] = useState<Set<string>>(
    () => new Set(allFolders),
  );

  useEffect(() => {
    const nextFolders = allFolders.filter(
      (path) => !knownFoldersRef.current.has(path),
    );
    if (nextFolders.length)
      setExpanded((current) => new Set([...current, ...nextFolders]));
    knownFoldersRef.current = new Set(allFolders);
  }, [allFolders]);

  useEffect(() => {
    if (creating) return;
    if (selectedPath && files.some((entry) => filePath(entry) === selectedPath))
      return;
    const next = filePath(files[0] || {});
    setSelectedPath(next);
    setDraftPath(next);
    if (!next) setCreating(true);
  }, [creating, files, selectedPath]);

  useEffect(() => {
    if (creating || !selectedPath) return;
    let disposed = false;
    setLoading(true);
    setLoadError("");
    setContent(null);
    void projectsApi
      .getFileContent(projectId, selectedPath)
      .then((next) => {
        if (disposed) return;
        setContent(next);
        setDraftPath(next.path);
        setDraftContent(next.content || "");
      })
      .catch(() => {
        if (disposed) return;
        setLoadError(t("projectWorkspaceFiles.loadFailed"));
      })
      .finally(() => {
        if (!disposed) setLoading(false);
      });
    return () => {
      disposed = true;
    };
  }, [creating, projectId, reloadKey, selectedPath, t]);

  useEffect(() => {
    mediaRecoveryPathRef.current = "";
    setHtmlPreview(false);
  }, [selectedPath]);

  const selectFile = (path: string) => {
    setCreating(false);
    setSelectedPath(path);
    onSelectedPathChange?.(path);
    setDraftPath(path);
    setLoadError("");
    const segments = path.split("/");
    setExpanded(
      (current) =>
        new Set([
          ...current,
          ...segments
            .slice(0, -1)
            .map((_, index) => segments.slice(0, index + 1).join("/")),
        ]),
    );
  };

  useEffect(() => {
    if (
      !requestedPath ||
      requestedPath === selectedPath ||
      !files.some((entry) => filePath(entry) === requestedPath)
    )
      return;
    setCreating(false);
    setSelectedPath(requestedPath);
    setDraftPath(requestedPath);
    setLoadError("");
    const segments = requestedPath.split("/");
    setExpanded(
      (current) =>
        new Set([
          ...current,
          ...segments
            .slice(0, -1)
            .map((_, index) => segments.slice(0, index + 1).join("/")),
        ]),
    );
  }, [files, requestedPath, selectedPath]);

  const startNewFile = () => {
    if (!canWrite) return;
    setCreating(true);
    setSelectedPath("");
    onSelectedPathChange?.("");
    setDraftPath("");
    setDraftContent("");
    setContent(null);
    setLoadError("");
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    const path = draftPath.trim().replace(/^\/+/, "");
    if (!path) return;
    const ok = await runAction(
      "save-file",
      () => projectsApi.writeFile(projectId, { path, content: draftContent }),
      t("projectWorkspaceFiles.saved"),
    );
    if (ok) {
      setCreating(false);
      setSelectedPath(path);
      onSelectedPathChange?.(path);
      setDraftPath(path);
      setReloadKey((value) => value + 1);
    }
  };

  const textReadOnly = Boolean(
    content && (!content.is_editable || content.truncated),
  );
  const canEditText = creating || Boolean(content?.is_text);
  const isMarkdown = !creating && /\.(md|markdown)$/i.test(draftPath);
  const markdownPreview = isMarkdown && selectedView !== "source";
  const editorReadOnly = textReadOnly || !canWrite;
  const activeEntry = files.find((entry) => filePath(entry) === selectedPath);
  const refreshMediaTicket = () => {
    if (!selectedPath || mediaRecoveryPathRef.current === selectedPath) return;
    mediaRecoveryPathRef.current = selectedPath;
    setReloadKey((value) => value + 1);
  };

  const openDownload = (url: string, name: string) => {
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    link.rel = "noopener";
    document.body.appendChild(link);
    link.click();
    link.remove();
  };

  const downloadNode = async (node: TreeNode) => {
    if (downloadingPath) return;
    setDownloadingPath(node.path);
    setDownloadError("");
    try {
      if (node.kind === "folder") {
        const archive = node.virtualRoot
          ? await projectsApi.getDirectoryArchive(projectId)
          : await projectsApi.getDirectoryArchive(projectId, node.path);
        openDownload(archive.download_url, archive.name);
      } else {
        const file =
          content?.path === node.path
            ? content
            : await projectsApi.getFileContent(projectId, node.path, 1);
        openDownload(file.download_url, file.name);
      }
    } catch {
      setDownloadError(t("projectWorkspaceFiles.downloadFailed"));
    } finally {
      setDownloadingPath("");
    }
  };

  const downloadProject = async () => {
    if (downloadingPath) return;
    setDownloadingPath("__root__");
    setDownloadError("");
    try {
      const archive = await projectsApi.getDirectoryArchive(projectId);
      openDownload(archive.download_url, archive.name);
    } catch {
      setDownloadError(t("projectWorkspaceFiles.downloadFailed"));
    } finally {
      setDownloadingPath("");
    }
  };

  return (
    <ResizableSplitPane
      className="project-file-workspace"
      ariaLabel={t("projectWorkspaceFiles.resizeDirectory")}
      defaultSize={280}
      minSize={220}
      maxSize={520}
      storageKey="project-workspace-file-tree-width"
      first={
        <aside
          className="project-file-workspace__sidebar"
          aria-label={t("projectWorkspaceFiles.directoryAria")}
        >
          <header>
            <IconBrandGit size={16} />
            <span title={t("projectWorkspaceFiles.treeTitle")}>
              {t("projectWorkspaceFiles.treeTitle")}
            </span>
            <em>{files.length}</em>
            <Button
              type="button"
              variant="ghost"
              onClick={() => void downloadProject()}
              title={t("projectWorkspaceFiles.downloadWorkspace")}
              aria-label={t("projectWorkspaceFiles.downloadWorkspace")}
            >
              {downloadingPath === "__root__" ? (
                <IconLoader2 className="project-workspace__spinner" size={15} />
              ) : (
                <IconDownload size={15} />
              )}
            </Button>
            {canWrite && (
              <Button
                type="button"
                variant="ghost"
                onClick={startNewFile}
                title={t("projectWorkspaceFiles.newFile")}
                aria-label={t("projectWorkspaceFiles.newFile")}
              >
                <IconPlus size={15} />
              </Button>
            )}
          </header>
          <div
            className="project-file-workspace__tree"
            role="tree"
            aria-label={t("projectWorkspaceFiles.treeAria")}
          >
            {tree.length ? (
              <TreeRows
                nodes={tree}
                depth={0}
                expanded={expanded}
                selectedPath={selectedPath}
                onToggle={(path) =>
                  setExpanded((current) => {
                    const next = new Set(current);
                    if (next.has(path)) next.delete(path);
                    else next.add(path);
                    return next;
                  })
                }
                onSelect={selectFile}
                onExpandAll={(node) =>
                  setExpanded(
                    (current) =>
                      new Set([...current, ...folderPathsForNode(node)]),
                  )
                }
                onCollapseAll={(node) =>
                  setExpanded((current) => {
                    const next = new Set(current);
                    folderPathsForNode(node).forEach((path) =>
                      next.delete(path),
                    );
                    return next;
                  })
                }
                onDownload={(node) => void downloadNode(node)}
                downloadingPath={downloadingPath}
                labelFor={treeLabel}
                downloadLabel={(label) =>
                  t("projectWorkspaceFiles.downloadItem", { name: label })
                }
                expandAllLabel={(label) =>
                  t("projectWorkspaceFiles.expandAll", { name: label })
                }
                collapseAllLabel={(label) =>
                  t("projectWorkspaceFiles.collapseAll", { name: label })
                }
              />
            ) : (
              <div className="project-file-workspace__tree-empty">
                <IconFolder size={22} />
                <span>{t("projectWorkspaceFiles.empty")}</span>
              </div>
            )}
          </div>
          {downloadError ? (
            <div
              className="project-file-workspace__download-error"
              role="alert"
            >
              <IconAlertTriangle size={14} />
              <span>{downloadError}</span>
            </div>
          ) : null}
        </aside>
      }
      second={
        <section className="project-file-workspace__viewer">
          <header className="project-file-workspace__viewer-header">
            <div>
              <span>
                {creating
                  ? t("projectWorkspaceFiles.newFileBadge")
                  : htmlPreview
                    ? t("projectWorkspaceFiles.htmlPreviewBadge")
                    : isMarkdown && markdownPreview
                      ? t("projectWorkspaceFiles.markdownPreviewBadge")
                      : content?.is_text
                        ? t("projectWorkspaceFiles.editFileBadge")
                        : t("projectWorkspaceFiles.filePreviewBadge")}
              </span>
              {creating ? (
                <TextInput
                  form="project-file-editor-form"
                  value={draftPath}
                  onChange={(event) => setDraftPath(event.target.value)}
                  placeholder="docs/deliverable.md"
                  aria-label={t("projectWorkspaceFiles.pathAria")}
                  required
                  autoFocus
                />
              ) : (
                <h3 title={draftPath || selectedPath}>
                  {draftPath ||
                    selectedPath ||
                    t("projectWorkspaceFiles.newFile")}
                </h3>
              )}
            </div>
            <div className="project-file-workspace__viewer-actions">
              {content ? (
                <MetaLine content={content} />
              ) : activeEntry ? (
                <span>{formatFileSize(number(activeEntry, "size"))}</span>
              ) : null}
              {content?.html_preview_url ? (
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => setHtmlPreview((value) => !value)}
                  title={
                    htmlPreview
                      ? t("projectWorkspaceFiles.backToCode")
                      : t("projectWorkspaceFiles.previewHtml")
                  }
                  aria-label={
                    htmlPreview
                      ? t("projectWorkspaceFiles.backToCode")
                      : t("projectWorkspaceFiles.previewHtml")
                  }
                >
                  {htmlPreview ? <IconCode size={17} /> : <IconEye size={17} />}
                </Button>
              ) : null}
              {canWrite && isMarkdown && !textReadOnly ? (
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() =>
                    onSelectedViewChange?.(
                      markdownPreview ? "source" : "preview",
                    )
                  }
                  title={
                    markdownPreview
                      ? t("projectWorkspaceFiles.editMarkdown")
                      : t("projectWorkspaceFiles.previewMarkdown")
                  }
                  aria-label={
                    markdownPreview
                      ? t("projectWorkspaceFiles.editMarkdown")
                      : t("projectWorkspaceFiles.previewMarkdown")
                  }
                >
                  {markdownPreview ? (
                    <IconCode size={17} />
                  ) : (
                    <IconEye size={17} />
                  )}
                </Button>
              ) : null}
              {content?.download_url ? (
                <a
                  className="btn btn-ghost"
                  href={content.download_url}
                  download={content.name}
                  title={t("projectWorkspaceFiles.downloadFile")}
                  aria-label={t("projectWorkspaceFiles.downloadItem", {
                    name: content.name,
                  })}
                >
                  <IconDownload size={17} />
                </a>
              ) : null}
              {canEditText && !editorReadOnly && !markdownPreview ? (
                <Button
                  type="submit"
                  form="project-file-editor-form"
                  variant="primary"
                  disabled={!draftPath.trim() || busyAction === "save-file"}
                >
                  {busyAction === "save-file" ? (
                    <IconLoader2
                      className="project-workspace__spinner"
                      size={16}
                    />
                  ) : (
                    <IconDeviceFloppy size={16} />
                  )}
                  {t("projectWorkspaceFiles.save")}
                </Button>
              ) : null}
            </div>
          </header>

          {loading ? (
            <div className="project-file-workspace__state">
              <IconLoader2 className="project-workspace__spinner" size={24} />
              <span>{t("projectWorkspaceFiles.loadingHead")}</span>
            </div>
          ) : loadError ? (
            <div className="project-file-workspace__state is-error">
              <IconAlertTriangle size={24} />
              <strong>{t("projectWorkspaceFiles.loadFailed")}</strong>
              <span>{loadError}</span>
              <Button
                variant="secondary"
                onClick={() => setReloadKey((value) => value + 1)}
              >
                <IconRefresh size={15} />
                {t("projectWorkspaceFiles.retry")}
              </Button>
            </div>
          ) : htmlPreview && content?.html_preview_url ? (
            <div className="project-file-workspace__html-preview">
              <iframe
                key={content.html_preview_url}
                src={content.html_preview_url}
                title={t("projectWorkspaceFiles.htmlPreviewTitle", {
                  name: content.name,
                })}
                sandbox="allow-scripts"
                referrerPolicy="no-referrer"
              />
            </div>
          ) : markdownPreview ? (
            <MarkdownRenderer
              className="project-file-workspace__markdown-preview"
              content={draftContent}
            />
          ) : canEditText ? (
            <form
              id="project-file-editor-form"
              className="project-file-workspace__editor"
              onSubmit={(event) => void save(event)}
            >
              {textReadOnly ? (
                <div
                  className="project-file-workspace__readonly-note"
                  role="status"
                >
                  <IconAlertTriangle size={16} />
                  <div>
                    <strong>
                      {content?.truncated
                        ? t("projectWorkspaceFiles.truncatedTitle")
                        : t("projectWorkspaceFiles.readOnlyTitle")}
                    </strong>
                    <p>{t("projectWorkspaceFiles.readOnlyDescription")}</p>
                  </div>
                </div>
              ) : null}
              <Suspense
                fallback={
                  <div className="project-file-workspace__editor-loading">
                    <IconLoader2
                      className="project-workspace__spinner"
                      size={20}
                    />
                    <span>{t("projectWorkspaceFiles.loadingEditor")}</span>
                  </div>
                }
              >
                <ProjectCodeEditor
                  path={draftPath}
                  value={draftContent}
                  readOnly={editorReadOnly}
                  onChange={setDraftContent}
                />
              </Suspense>
              <footer>
                <span>
                  {creating
                    ? t("projectWorkspaceFiles.saveVersion")
                    : textReadOnly
                      ? t("projectWorkspaceFiles.readOnlyPreview")
                      : t("projectWorkspaceFiles.saveVersion")}
                </span>
              </footer>
            </form>
          ) : content ? (
            <div className="project-file-workspace__preview">
              <MediaPreview
                content={content}
                onUnavailable={refreshMediaTicket}
              />
            </div>
          ) : (
            <div className="project-file-workspace__state">
              <IconCode size={24} />
              <span>{t("projectWorkspaceFiles.selectFile")}</span>
            </div>
          )}
        </section>
      }
    />
  );
}
