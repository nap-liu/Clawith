import { useTranslation } from "react-i18next";
import i18n from "../../../i18n";
import {
  closestProjectTraceValue as closestTraceValue,
  projectSessionTargetFromUrl,
  resolveProjectSessionRoute as sessionRouteOf,
  projectTraceRecords as traceRecords,
} from "../projectSessionRouting";
import type { ProjectSummary } from "../types";
import type {
  ProjectSessionIntent as SessionIntent,
} from "../projectSessionRouting";
import type {
  ProjectToolDefinition,
  RecordValue,
} from "./types";

export const obj = (value: unknown): RecordValue =>
  value && typeof value === "object" ? (value as RecordValue) : {};

export const arr = (value: unknown): RecordValue[] =>
  Array.isArray(value)
    ? value.map(obj)
    : Array.isArray(obj(value).items)
      ? (obj(value).items as unknown[]).map(obj)
      : [];

export const text = (source: RecordValue, ...keys: string[]): string => {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "string" || typeof value === "number") {
      return String(value);
    }
  }
  return "";
};

export const templateEditorId = (
  project: ProjectSummary,
  policies: RecordValue | null,
): string => {
  const sources = [obj(project.settings), obj(policies)];
  for (const source of sources) {
    const marker = source.template_editor;
    if (typeof marker === "string" && marker) return marker;
    const id = text(obj(marker), "template_id");
    if (id) return id;
  }
  return "";
};

export const bool = (source: RecordValue, ...keys: string[]): boolean =>
  keys.some((key) => source[key] === true);

export const num = (source: RecordValue, ...keys: string[]): number => {
  for (const key of keys) {
    const value = Number(source[key]);
    if (Number.isFinite(value)) return value;
  }
  return 0;
};

export const listText = (source: RecordValue, key: string): string =>
  Array.isArray(source[key])
    ? (source[key] as unknown[]).map((value) => String(value)).join("；")
    : text(source, key);

export const dateLabel = (value: unknown): string => {
  if (!value) return "—";
  const date = new Date(String(value));
  return Number.isNaN(date.getTime())
    ? String(value)
    : new Intl.DateTimeFormat(i18n.language, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      }).format(date);
};

export const compactId = (value: string): string =>
  value.length > 12 ? value.slice(0, 8) : value;

export const fileSizeLabel = (bytes: number): string => {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
};

export const statusLabel = (
  status: string,
  t: ReturnType<typeof useTranslation>["t"],
): string =>
  t(`projectGraphs.status.${status || "unset"}`, {
    defaultValue: t("projectGraphs.status.unknown"),
  });

export const stringList = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.map((entry) => String(entry)).filter(Boolean)
    : [];

export function projectToolResolution(
  tool: ProjectToolDefinition,
  member: RecordValue,
  policies: RecordValue | null,
) {
  const role = bool(member, "is_leader") ? "leader" : "participant";
  const lifecycleBlocked = member.is_enabled === false;
  const roleCeiling = role === "leader" || tool.participant;
  const projectPolicy = obj(
    obj(policies?.policies).project_tools || obj(policies).project_tools,
  );
  const policyDisabled = new Set([
    ...stringList(projectPolicy.disabled),
    ...stringList(projectPolicy[`${role}_disabled`]),
  ]);
  const roleAllowed = Array.isArray(projectPolicy[`${role}_allowed`])
    ? new Set(stringList(projectPolicy[`${role}_allowed`]))
    : null;
  const config = obj(member.config_snapshot);
  const memberDisabled = new Set(stringList(config.disabled_project_tools));
  const memberAllowed = Array.isArray(config.enabled_project_tools)
    ? new Set(stringList(config.enabled_project_tools))
    : null;
  const policyBlocked =
    policyDisabled.has(tool.name) ||
    Boolean(roleAllowed && !roleAllowed.has(tool.name));
  const snapshotBlocked = Boolean(
    memberAllowed && !memberAllowed.has(tool.name),
  );
  return {
    role,
    roleCeiling,
    policyBlocked,
    snapshotBlocked,
    lifecycleBlocked,
    memberDisabled: memberDisabled.has(tool.name),
    effective:
      !lifecycleBlocked &&
      roleCeiling &&
      !policyBlocked &&
      !snapshotBlocked &&
      !memberDisabled.has(tool.name),
  };
}

export const errorMessage = (
  _error: unknown,
  fallback = "Request failed",
): string => fallback;

export function sessionIdOf(source: RecordValue): string {
  return sessionRouteOf(source)?.sessionId || "";
}

export function traceStringValues(
  source: RecordValue,
  ...keys: string[]
): string[] {
  const values: string[] = [];
  for (const record of traceRecords(source)) {
    for (const key of keys) {
      const value = record[key];
      if (Array.isArray(value)) {
        value.forEach((entry) => {
          if (typeof entry === "string" || typeof entry === "number") {
            values.push(String(entry));
          }
        });
      } else if (typeof value === "string" || typeof value === "number") {
        values.push(String(value));
      }
    }
  }
  return Array.from(
    new Set(values.map((value) => value.trim()).filter(Boolean)),
  );
}

export function runAgentId(run: RecordValue): string {
  return closestTraceValue(
    traceRecords(run),
    "execution_agent_id",
    "subagent_agent_id",
    "agent_id",
    "to_agent_id",
    "assignee_agent_id",
  );
}

export function runAgentName(
  run: RecordValue,
  members: RecordValue[],
  fallback = "Digital Employee not recorded",
): string {
  const records = traceRecords(run);
  const snapshotName = closestTraceValue(
    records,
    "agent_name_snapshot",
    "name_snapshot",
    "execution_agent_name",
    "subagent_agent_name",
    "agent_name",
    "to_agent_name",
  );
  if (snapshotName) return snapshotName;
  const agentId = runAgentId(run);
  const member = members.find((entry) => text(entry, "agent_id") === agentId);
  return text(member || {}, "name_snapshot", "agent_name", "name") || fallback;
}

export function sameGitCommit(left: string, right: string): boolean {
  return Boolean(
    left &&
      right &&
      (left === right || left.startsWith(right) || right.startsWith(left)),
  );
}

export function pickCollection(
  payload: RecordValue,
  ...keys: string[]
): RecordValue[] {
  for (const key of keys) {
    const value = payload[key];
    const items = arr(value);
    if (items.length || Array.isArray(value) || Array.isArray(obj(value).items)) {
      return items;
    }
  }
  return [];
}

export function reportedPercent(...sources: RecordValue[]): number | null {
  for (const source of sources) {
    const direct = num(source, "reported_percent", "progress_percent", "percent");
    if (direct) return Math.max(0, Math.min(100, direct));
    const raw = source.progress;
    if (typeof raw === "number" && Number.isFinite(raw)) {
      return Math.max(0, Math.min(100, raw <= 1 ? raw * 100 : raw));
    }
  }
  return null;
}

export function isProjectRiskEvent(event: RecordValue): boolean {
  const severity = text(event, "severity", "level", "tone").toLowerCase();
  const kind = text(event, "event_type", "type").toLowerCase();
  return (
    ["warning", "error", "critical"].includes(severity) ||
    /(risk|issue|block|fail|error|timeout|reject)/.test(kind)
  );
}

const INTERNAL_UUID_PATTERN =
  /\b[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\b/gi;

export function redactInternalUuids(value: string, replacement: string): string {
  return value.replace(INTERNAL_UUID_PATTERN, replacement);
}
