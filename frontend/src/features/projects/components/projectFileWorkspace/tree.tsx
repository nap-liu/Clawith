import {
  IconChevronRight,
  IconChevronsDown,
  IconChevronsUp,
  IconDownload,
  IconFile,
  IconFileCode,
  IconFolder,
  IconFolderOpen,
  IconLoader2,
  IconMusic,
  IconPhoto,
  IconPlayerPlay,
} from "@tabler/icons-react";

import { type ProjectFileKind } from "../../../../services/projects";

export type RecordValue = Record<string, unknown>;

export type Props = {
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
  canWrite?: boolean;
};

export type TreeNode = {
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

export const number = (source: RecordValue, ...keys: string[]) => {
  for (const key of keys) {
    const value = Number(source[key]);
    if (Number.isFinite(value)) return value;
  }
  return 0;
};

export const filePath = (entry: RecordValue) => text(entry, "path", "id");

export function treeFromFiles(files: RecordValue[]): TreeNode[] {
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

export function folderPaths(nodes: TreeNode[]): string[] {
  return nodes.flatMap((node) =>
    node.kind === "folder" ? [node.path, ...folderPaths(node.children)] : [],
  );
}

export function folderPathsForNode(node: TreeNode): string[] {
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

export function TreeRows({
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
