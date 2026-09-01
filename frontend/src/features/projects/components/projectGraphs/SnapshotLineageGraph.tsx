import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import type { Edge } from "@xyflow/react";

import { projectUserFacingCopy } from "../../projectUserFacingCopy";
import { resolveProjectSessionRoute } from "../../projectSessionRouting";
import {
  dateLabel,
  makeEdge,
  ProjectGraphCanvas,
  type ProjectGraphNode,
  type ProjectGraphRecord,
  type Selection,
  statusLabel,
  statusTone,
  valueText,
} from "./shared";

export interface SnapshotLineageGraphProps {
  member: ProjectGraphRecord | null;
  runs?: ProjectGraphRecord[];
  runSnapshots?: ProjectGraphRecord[];
  onNodeSelect?: (selection: Selection) => void;
}

export function SnapshotLineageGraph({
  member,
  runs = [],
  runSnapshots = [],
  onNodeSelect,
}: SnapshotLineageGraphProps) {
  const { t, i18n } = useTranslation();
  const graph = useMemo(() => {
    if (!member)
      return {
        nodes: [] as ProjectGraphNode[],
        edges: [] as Edge[],
        records: new Map<string, ProjectGraphRecord>(),
        visibleRunCount: 0,
        totalRunCount: 0,
      };
    const memberId = valueText(member, "id", "member_id");
    const agentId = valueText(member, "agent_id");
    const sourceId = `source:${agentId || memberId}`;
    const projectId = `project:${memberId || agentId}`;
    const matchingSnapshots = runSnapshots.filter((snapshot) => {
      const snapshotMemberId = valueText(
        snapshot,
        "project_member_id",
        "member_id",
      );
      const snapshotAgentId = valueText(snapshot, "agent_id");
      return (
        (!snapshotMemberId && !snapshotAgentId) ||
        snapshotMemberId === memberId ||
        snapshotAgentId === agentId
      );
    });
    const matchingRuns = matchingSnapshots.length
      ? matchingSnapshots
      : runs.filter(
          (run) =>
            !valueText(run, "agent_id") ||
            valueText(run, "agent_id") === agentId,
        );
    const traceableRuns = matchingRuns
      .map((entry) => {
        const runRecord = matchingSnapshots.length
          ? runs.find(
              (run) =>
                valueText(run, "id", "run_id") === valueText(entry, "run_id"),
            ) || entry
          : entry;
        return { entry, runRecord };
      })
      .filter(({ runRecord }) =>
        Boolean(resolveProjectSessionRoute(runRecord, "run")),
      )
      .sort((left, right) => {
        const timestamp = (record: ProjectGraphRecord) => {
          const raw = valueText(
            record,
            "finished_at",
            "started_at",
            "updated_at",
            "created_at",
          );
          const parsed = raw ? new Date(raw).getTime() : 0;
          return Number.isFinite(parsed) ? parsed : 0;
        };
        return (
          timestamp(right.runRecord) - timestamp(left.runRecord) ||
          valueText(right.runRecord, "id", "run_id").localeCompare(
            valueText(left.runRecord, "id", "run_id"),
          )
        );
      });
    const visibleRuns = traceableRuns.slice(0, 8);
    const runsPerColumn = Math.min(4, Math.max(1, visibleRuns.length));
    const runGap = 112;
    const firstRunY = -((runsPerColumn - 1) * runGap) / 2;
    const nodes: ProjectGraphNode[] = [
      {
        id: sourceId,
        type: "projectGraph",
        position: { x: 0, y: 0 },
        data: {
          kind: "source",
          label:
            valueText(member, "name_snapshot", "agent_name", "name") ||
            t("projectGraphs.sourceAgent"),
          caption: t("projectGraphs.sourceCaption"),
          meta: "",
          badge: t("projectGraphs.readOnlySource"),
          tone: "neutral",
          interactive: false,
        },
      },
      {
        id: projectId,
        type: "projectGraph",
        position: { x: 340, y: 0 },
        data: {
          kind: "snapshot",
          label: t("projectGraphs.isolatedSnapshot"),
          caption: projectUserFacingCopy(
            valueText(member, "role_snapshot", "role") ||
              t("projectGraphs.memberConfig"),
            t,
          ),
          meta: "",
          badge:
            member.is_leader === true
              ? t("projectTerminology.owner")
              : member.is_enabled === false
                ? t("projectGraphs.disabled")
                : t("projectGraphs.active"),
          tone: member.is_enabled === false ? "neutral" : "info",
          interactive: false,
        },
      },
    ];
    const records = new Map<string, ProjectGraphRecord>([
      [sourceId, member],
      [projectId, member],
    ]);
    const edges: Edge[] = [
      makeEdge("source-to-project", sourceId, projectId, {
        label: t("projectGraphs.snapshotCreated"),
      }),
    ];
    visibleRuns.forEach(({ entry, runRecord }, index) => {
      const runId = valueText(entry, "run_id", "id") || `${index}`;
      const nodeId = `run:${runId}`;
      const status = valueText(runRecord, "status") || "frozen";
      const column = Math.floor(index / runsPerColumn);
      const row = index % runsPerColumn;
      nodes.push({
        id: nodeId,
        type: "projectGraph",
        position: {
          x: 680 + column * 300,
          y: firstRunY + row * runGap,
        },
        data: {
          kind: "run",
          label:
            valueText(runRecord, "name", "title") ||
            t("projectGraphs.execution"),
          caption: `${
            status === "frozen"
              ? t("projectGraphs.frozen")
              : statusLabel(status, t)
          } · ${dateLabel(runRecord.created_at, t, i18n.resolvedLanguage || i18n.language)}`,
          meta: "",
          badge: t("projectGraphs.frozen"),
          tone: statusTone(status),
          interactive: true,
        },
      });
      records.set(nodeId, runRecord);
      edges.push(
        makeEdge(`project-to-${nodeId}`, projectId, nodeId, {
          label: t("projectGraphs.runtimeFrozen"),
        }),
      );
    });
    return {
      nodes,
      edges,
      records,
      visibleRunCount: visibleRuns.length,
      totalRunCount: matchingRuns.length,
    };
  }, [i18n.language, i18n.resolvedLanguage, member, runSnapshots, runs, t]);

  return (
    <ProjectGraphCanvas
      ariaLabel={t("projectGraphs.snapshotAria", {
        name:
          valueText(member || {}, "name_snapshot", "agent_name", "name") ||
          t("projectGraphs.unnamedAgent"),
        runs: graph.totalRunCount,
      })}
      nodes={graph.nodes}
      edges={graph.edges}
      records={graph.records}
      variant="lineage"
      fitViewMinZoom={0.35}
      fitViewMaxZoom={0.95}
      fitViewPadding={0.2}
      panOnScroll
      summary={t("projectGraphs.snapshotSummary", {
        shown: graph.visibleRunCount,
        total: graph.totalRunCount,
      })}
      onSelect={onNodeSelect}
    />
  );
}
