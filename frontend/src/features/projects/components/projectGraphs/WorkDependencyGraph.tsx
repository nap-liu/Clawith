import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import type { Edge } from "@xyflow/react";

import {
  makeEdge,
  ProjectGraphCanvas,
  type ProjectGraphNode,
  type ProjectGraphRecord,
  statusLabel,
  statusTone,
  valueList,
  valueText,
} from "./shared";

export interface WorkDependencyGraphProps {
  items: ProjectGraphRecord[];
  selectedWorkItemId?: string;
  onWorkItemSelect?: (workItemId: string, item: ProjectGraphRecord) => void;
}

function dependencyRanks(items: ProjectGraphRecord[]): Map<string, number> {
  const ids = new Set(
    items.map((item) => valueText(item, "id", "work_item_id")).filter(Boolean),
  );
  const dependencies = new Map(
    items.map((item) => {
      const id = valueText(item, "id", "work_item_id");
      const refs = [...new Set(valueList(item, "dependency_ids"))].filter(
        (ref) => ref && ref !== id && ids.has(ref),
      );
      return [id, refs] as const;
    }),
  );
  const ranks = new Map<string, number>();
  const unresolved = new Set(ids);
  let changed = true;
  while (unresolved.size && changed) {
    changed = false;
    for (const id of [...unresolved]) {
      const refs = dependencies.get(id) || [];
      if (refs.some((dependency) => !ranks.has(dependency))) continue;
      ranks.set(
        id,
        refs.length
          ? Math.max(...refs.map((dependency) => ranks.get(dependency) || 0)) +
              1
          : 0,
      );
      unresolved.delete(id);
      changed = true;
    }
  }
  // Cycles are invalid project data, but the graph must remain inspectable.
  // Keep every unresolved node together after the valid DAG instead of recursing forever.
  const cycleRank = Math.max(-1, ...ranks.values()) + 1;
  [...unresolved].sort().forEach((id) => ranks.set(id, cycleRank));
  return ranks;
}

export function WorkDependencyGraph({
  items,
  selectedWorkItemId,
  onWorkItemSelect,
}: WorkDependencyGraphProps) {
  const { t, i18n } = useTranslation();
  const graph = useMemo(() => {
    const ranks = dependencyRanks(items);
    const grouped = new Map<number, ProjectGraphRecord[]>();
    for (const item of items) {
      const rank = ranks.get(valueText(item, "id", "work_item_id")) || 0;
      grouped.set(rank, [...(grouped.get(rank) || []), item]);
    }
    for (const [rank, entries] of grouped) {
      grouped.set(
        rank,
        entries.sort((left, right) =>
          valueText(left, "title", "name").localeCompare(
            valueText(right, "title", "name"),
            i18n.resolvedLanguage || i18n.language,
          ),
        ),
      );
    }
    const nodes: ProjectGraphNode[] = [];
    const records = new Map<string, ProjectGraphRecord>();
    for (const [rank, entries] of [...grouped.entries()].sort(
      ([left], [right]) => left - right,
    )) {
      const gap = 118;
      const firstY = -((entries.length - 1) * gap) / 2;
      entries.forEach((item, index) => {
        const id = valueText(item, "id", "work_item_id");
        const status = valueText(item, "status", "state");
        nodes.push({
          id,
          type: "projectGraph",
          position: { x: rank * 310, y: firstY + index * gap },
          data: {
            kind: "work",
            label:
              valueText(item, "title", "name") ||
              t("projectGraphs.unnamedWorkItem"),
            caption:
              valueText(
                item,
                "assignee_name",
                "agent_name",
                "assignee_agent_id",
              ) || t("projectGraphs.unassigned"),
            meta: "",
            badge:
              selectedWorkItemId === id
                ? t("projectGraphs.selected")
                : statusLabel(status, t),
            tone: statusTone(status),
          },
        });
        records.set(id, item);
      });
    }
    const edges: Edge[] = [];
    const knownIds = new Set(nodes.map((node) => node.id));
    for (const item of items) {
      const target = valueText(item, "id", "work_item_id");
      const dependencyIds = [...new Set(valueList(item, "dependency_ids"))];
      for (const dependency of dependencyIds) {
        if (dependency !== target && knownIds.has(dependency)) {
          edges.push(
            makeEdge(`dependency-${dependency}-${target}`, dependency, target, {
              label: t("projectGraphs.dependency"),
            }),
          );
        }
      }
    }
    const activeStatuses = new Set([
      "running",
      "doing",
      "in_progress",
      "review",
      "blocked",
      "waiting",
      "paused",
    ]);
    let focusIndex = nodes.findIndex((node) =>
      activeStatuses.has(
        valueText(records.get(node.id) || {}, "status", "state"),
      ),
    );
    if (focusIndex < 0)
      focusIndex = nodes.findIndex(
        (node) =>
          !["done", "completed", "success", "succeeded"].includes(
            valueText(records.get(node.id) || {}, "status", "state"),
          ),
      );
    if (focusIndex < 0) focusIndex = Math.max(0, nodes.length - 1);
    const focusStart = Math.min(
      Math.max(0, focusIndex - 1),
      Math.max(0, nodes.length - 3),
    );
    const focusNodeIds = nodes
      .slice(focusStart, focusStart + 3)
      .map((node) => node.id);
    return { nodes, edges, records, focusNodeIds };
  }, [i18n.language, i18n.resolvedLanguage, items, selectedWorkItemId, t]);

  return (
    <ProjectGraphCanvas
      ariaLabel={t("projectGraphs.workAria", {
        nodes: graph.nodes.length,
        edges: graph.edges.length,
      })}
      nodes={graph.nodes}
      edges={graph.edges}
      records={graph.records}
      miniMap={graph.nodes.length > 4}
      fitViewMinZoom={0.35}
      fitViewMaxZoom={1}
      fitViewPadding={0.16}
      onSelect={({ id, record }) => onWorkItemSelect?.(id, record)}
    />
  );
}
