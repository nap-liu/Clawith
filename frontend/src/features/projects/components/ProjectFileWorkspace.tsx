import {
  IconAlertTriangle,
  IconBrandGit,
  IconChevronRight,
  IconChevronsDown,
  IconChevronsUp,
  IconCode,
  IconDeviceFloppy,
  IconDownload,
  IconEye,
  IconFile,
  IconFileCode,
  IconFileUnknown,
  IconFolder,
  IconFolderOpen,
  IconLoader2,
  IconMusic,
  IconPhoto,
  IconPlayerPlay,
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
import {
  projectsApi,
  type ProjectFileContent,
  type ProjectFileKind,
} from "../../../services/projects";
import { formatFileSize } from "../../../utils/formatFileSize";
import { Button, TextInput } from "./ProjectUI";
import "./ProjectFileWorkspace.css";

const ProjectCodeEditor = lazy(() => import("./ProjectCodeEditor"));

type RecordValue = Record<string, unknown>;

type Props = {
  projectId: string;
  files: RecordValue[];
  projectAgents?: Array<{ id: string; name: string }>;
  selectedPath?: string;
  onSelectedPathChange?: (path: string) => void;
  selectedView?: "preview" | "source";
  onSelectedViewChange?: (view: "preview" | "source") => void;
  runAction: (
    key: string,
    action: () => Promise<unknown>,
    success: string,
  ) => Promise<boolean>;
  busyAction: string;
};

type TreeNode = {
  name: string;
  path: string;
  kind: "folder" | "file";
  children: TreeNode[];
  entry?: RecordValue;
  virtualRoot?: "project";
};

const text = (source: RecordValue, ...keys: string[]) => {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "string" || typeof value === "number")
      return String(value);
  }
  return "";
};

const number = (source: RecordValue, ...keys: string[]) => {
  for (const key of keys) {
    const value = Number(source[key]);
    if (Number.isFinite(value)) return value;
  }
  return 0;
};

const filePath = (entry: RecordValue) => text(entry, "path", "id");

function treeFromFiles(files: RecordValue[]): TreeNode[] {
  const root: TreeNode = { name: "", path: "", kind: "folder", children: [] };
  for (const entry of files) {
    const path = filePath(entry).replace(/^\/+|\/+$/g, "");
    if (!path) continue;
    const segments = path.split("/").filter(Boolean);
    let parent = root;
    segments.forEach((segment, index) => {
      const currentPath = segments.slice(0, index + 1).join("/");
      const isFile = index === segments.length - 1;
      let child = parent.children.find(
        (candidate) =>
          candidate.name === segment &&
          candidate.kind === (isFile ? "file" : "folder"),
      );
      if (!child) {
        child = {
          name: segment,
          path: currentPath,
          kind: isFile ? "file" : "folder",
          children: [],
          entry: isFile ? entry : undefined,
        };
        parent.children.push(child);
      } else if (isFile) {
        child.entry = entry;
      }
      parent = child;
    });
  }
  const sort = (nodes: TreeNode[]) => {
    nodes.sort((left, right) =>
      left.kind === right.kind
        ? left.name.localeCompare(right.name, "zh-CN", {
            numeric: true,
            sensitivity: "base",
          })
        : left.kind === "folder"
          ? -1
          : 1,
    );
    nodes.forEach((node) => sort(node.children));
  };
  sort(root.children);
  const employeeRoot = root.children.find(
    (node) => node.kind === "folder" && node.path === ".agents",
  );
  const projectChildren = root.children.filter((node) => node !== employeeRoot);
  return [
    {
      name: "project",
      path: "__project_root__",
      kind: "folder",
      children: projectChildren,
      virtualRoot: "project",
    },
    employeeRoot || {
      name: ".agents",
      path: ".agents",
      kind: "folder",
      children: [],
    },
  ];
}

function folderPaths(nodes: TreeNode[]): string[] {
  return nodes.flatMap((node) =>
    node.kind === "folder" ? [node.path, ...folderPaths(node.children)] : [],
  );
}

function folderPathsForNode(node: TreeNode): string[] {
  return node.kind === "folder"
    ? [node.path, ...folderPaths(node.children)]
    : [];
}

function kindOf(entry: RecordValue | undefined): ProjectFileKind {
  const kind = text(entry || {}, "kind");
  return ["text", "image", "video", "audio", "binary"].includes(kind)
    ? (kind as ProjectFileKind)
    : "binary";
}

function FileKindIcon({
  kind,
  size = 16,
}: {
  kind: ProjectFileKind;
  size?: number;
}) {
  if (kind === "image") return <IconPhoto size={size} />;
  if (kind === "video") return <IconPlayerPlay size={size} />;
  if (kind === "audio") return <IconMusic size={size} />;
  if (kind === "text") return <IconFileCode size={size} />;
  return <IconFile size={size} />;
}

function TreeRows({
  nodes,
  depth,
  expanded,
  selectedPath,
  onToggle,
  onSelect,
  onExpandAll,
  onCollapseAll,
  onDownload,
  downloadingPath,
  labelFor,
  downloadLabel,
  expandAllLabel,
  collapseAllLabel,
}: {
  nodes: TreeNode[];
  depth: number;
  expanded: Set<string>;
  selectedPath: string;
  onToggle: (path: string) => void;
  onSelect: (path: string) => void;
  onExpandAll: (node: TreeNode) => void;
  onCollapseAll: (node: TreeNode) => void;
  onDownload: (node: TreeNode) => void;
  downloadingPath: string;
  labelFor: (node: TreeNode) => string;
  downloadLabel: (label: string) => string;
  expandAllLabel: (label: string) => string;
  collapseAllLabel: (label: string) => string;
}) {
  return (
    <>
      {nodes.map((node) => {
        const isFolder = node.kind === "folder";
        const isExpanded = isFolder && expanded.has(node.path);
        const subtreeFolders = isFolder ? folderPathsForNode(node) : [];
        const isSubtreeExpanded = subtreeFolders.every((path) =>
          expanded.has(path),
        );
        const label = labelFor(node);
        return (
          <div
            key={`${node.kind}:${node.path}`}
            role="treeitem"
            aria-expanded={isFolder ? isExpanded : undefined}
          >
            <div className="project-file-workspace__tree-item">
              <button
                type="button"
                className={[
                  "project-file-workspace__tree-row",
                  node.path === selectedPath ? "is-active" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                style={{ paddingInlineStart: `${8 + depth * 16}px` }}
                title={
                  node.virtualRoot
                    ? label
                    : label === node.name
                      ? node.path
                      : `${label} · ${node.path}`
                }
                onClick={() =>
                  isFolder ? onToggle(node.path) : onSelect(node.path)
                }
              >
                {isFolder ? (
                  <>
                    <IconChevronRight
                      className={isExpanded ? "is-expanded" : ""}
                      size={14}
                    />
                    <span className="project-file-workspace__folder-icon">
                      {isExpanded ? (
                        <IconFolderOpen size={16} />
                      ) : (
                        <IconFolder size={16} />
                      )}
                    </span>
                  </>
                ) : (
                  <>
                    <span className="project-file-workspace__tree-spacer" />
                    <FileKindIcon kind={kindOf(node.entry)} />
                  </>
                )}
                <span>{label}</span>
              </button>
              <div className="project-file-workspace__tree-actions project-file-workspace__tree-download">
                {isFolder && node.children.length ? (
                  <button
                    type="button"
                    className="project-file-workspace__tree-action"
                    title={
                      isSubtreeExpanded
                        ? collapseAllLabel(label)
                        : expandAllLabel(label)
                    }
                    aria-label={
                      isSubtreeExpanded
                        ? collapseAllLabel(label)
                        : expandAllLabel(label)
                    }
                    onClick={() =>
                      isSubtreeExpanded
                        ? onCollapseAll(node)
                        : onExpandAll(node)
                    }
                  >
                    {isSubtreeExpanded ? (
                      <IconChevronsUp size={14} />
                    ) : (
                      <IconChevronsDown size={14} />
                    )}
                  </button>
                ) : null}
                {!node.virtualRoot ? (
                  <button
                    type="button"
                    className={`project-file-workspace__tree-action${downloadingPath === node.path ? " is-loading" : ""}`}
                    title={downloadLabel(label)}
                    aria-label={downloadLabel(label)}
                    onClick={() => onDownload(node)}
                  >
                    {downloadingPath === node.path ? (
                      <IconLoader2
                        className="project-workspace__spinner"
                        size={14}
                      />
                    ) : (
                      <IconDownload size={14} />
                    )}
                  </button>
                ) : null}
              </div>
            </div>
            {isFolder && isExpanded ? (
              <div role="group">
                <TreeRows
                  nodes={node.children}
                  depth={depth + 1}
                  expanded={expanded}
                  selectedPath={selectedPath}
                  onToggle={onToggle}
                  onSelect={onSelect}
                  onExpandAll={onExpandAll}
                  onCollapseAll={onCollapseAll}
                  onDownload={onDownload}
                  downloadingPath={downloadingPath}
                  labelFor={labelFor}
                  downloadLabel={downloadLabel}
                  expandAllLabel={expandAllLabel}
                  collapseAllLabel={collapseAllLabel}
                />
              </div>
            ) : null}
          </div>
        );
      })}
    </>
  );
}

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
          ? `Commit ${content.commit.slice(0, 10)}`
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
}: Props) {
  const { t } = useTranslation();
  const firstPath = filePath(files[0] || {});
  const initialPath =
    requestedPath && files.some((entry) => filePath(entry) === requestedPath)
      ? requestedPath
      : firstPath;
  const [selectedPath, setSelectedPath] = useState(initialPath);
  const [creating, setCreating] = useState(!firstPath);
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
      .catch((error: unknown) => {
        if (disposed) return;
        setLoadError(
          error instanceof Error
            ? error.message
            : t("projectWorkspaceFiles.loadFailed"),
        );
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
  const editorReadOnly = textReadOnly;
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
    } catch (error: unknown) {
      setDownloadError(
        error instanceof Error
          ? error.message
          : t("projectWorkspaceFiles.downloadFailed"),
      );
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
    } catch (error: unknown) {
      setDownloadError(
        error instanceof Error
          ? error.message
          : t("projectWorkspaceFiles.downloadFailed"),
      );
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
            <Button
              type="button"
              variant="ghost"
              onClick={startNewFile}
              title={t("projectWorkspaceFiles.newFile")}
              aria-label={t("projectWorkspaceFiles.newFile")}
            >
              <IconPlus size={15} />
            </Button>
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
              {isMarkdown && !textReadOnly ? (
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
                path={draftPath || content?.path || "README.md"}
                value={draftContent}
                readOnly
                ariaLabel={t("projectWorkspaceFiles.markdownPreviewAria", {
                  name: content?.name || draftPath,
                })}
              />
            </Suspense>
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
