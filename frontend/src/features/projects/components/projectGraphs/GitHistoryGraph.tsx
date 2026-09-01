import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import type { Edge } from "@xyflow/react";

import {
  compactId,
  dateLabel,
  makeEdge,
  ProjectGraphCanvas,
  type ProjectGraphNode,
  type ProjectGraphRecord,
  valueList,
  valueText,
} from "./shared";

export interface GitHistoryGraphProps {
  commits: ProjectGraphRecord[];
  selectedCommitId?: string;
  onCommitSelect?: (commitId: string, commit: ProjectGraphRecord) => void;
}

export function GitHistoryGraph({
  commits,
  selectedCommitId,
  onCommitSelect,
}: GitHistoryGraphProps) {
  const { t, i18n } = useTranslation();
  const graph = useMemo(() => {
    const ids = commits
      .map((commit) => valueText(commit, "commit", "hash", "commit_hash", "id"))
      .filter(Boolean);
    const idSet = new Set(ids);
    const branchLanes = new Map<string, number>();
    commits.forEach((commit) => {
      const branch = valueText(commit, "branch", "branch_name") || "main";
      if (!branchLanes.has(branch)) branchLanes.set(branch, branchLanes.size);
    });
    const nodes: ProjectGraphNode[] = commits.map((commit, index) => {
      const id = ids[index];
      const branch = valueText(commit, "branch", "branch_name") || "main";
      const isHead =
        index === 0 || commit.is_head === true || commit.current === true;
      return {
        id,
        type: "projectGraph",
        position: { x: (branchLanes.get(branch) || 0) * 270, y: index * 112 },
        data: {
          kind: "commit",
          label:
            valueText(commit, "message", "title") ||
            t("projectGraphs.commitNoMessage"),
          caption: `${valueText(commit, "author", "author_name", "agent_name") || t("projectGraphs.projectMember")} · ${dateLabel(commit.created_at, t, i18n.resolvedLanguage || i18n.language)}`,
          meta: valueText(commit, "short_commit") || compactId(id),
          badge:
            selectedCommitId === id
              ? t("projectGraphs.selected")
              : isHead
                ? "HEAD"
                : branch !== "main"
                  ? branch
                  : undefined,
          tone: isHead ? "success" : "neutral",
        },
      };
    });
    const edges: Edge[] = [];
    commits.forEach((commit, index) => {
      const child = ids[index];
      let parents = valueList(
        commit,
        "parents",
        "parent_hashes",
        "parent_commits",
      );
      const singleParent = valueText(
        commit,
        "parent",
        "parent_hash",
        "parent_commit",
      );
      if (singleParent) parents = [...parents, singleParent];
      if (!parents.length && ids[index + 1]) parents = [ids[index + 1]];
      for (const parent of [...new Set(parents)]) {
        if (idSet.has(parent)) {
          edges.push(
            makeEdge(`git-${parent}-${child}`, parent, child, {
              label: parents.length > 1 ? "merge" : undefined,
            }),
          );
        }
      }
    });
    return {
      nodes,
      edges,
      records: new Map(commits.map((commit, index) => [ids[index], commit])),
    };
  }, [commits, i18n.language, i18n.resolvedLanguage, selectedCommitId, t]);

  return (
    <ProjectGraphCanvas
      ariaLabel={t("projectGraphs.gitAria", { count: graph.nodes.length })}
      nodes={graph.nodes}
      edges={graph.edges}
      records={graph.records}
      direction="vertical"
      miniMap={graph.nodes.length > 10}
      onSelect={({ id, record }) => onCommitSelect?.(id, record)}
    />
  );
}
