import { useTranslation } from "react-i18next";
import {
  IconBrain,
  IconBrowser,
  IconChevronDown,
  IconClock,
  IconFileText,
  IconMessageCircle,
  IconSearch,
  IconTerminal2,
  IconTools,
} from "@tabler/icons-react";

import type { ConversationAnalysisItem } from "../../core/chatTimeline";

type AnalysisToolMeta = {
  title: string;
  label: string;
  target?: string;
  kind: "command" | "file" | "search" | "browser" | "message" | "agent" | "mcp";
};

function firstString(...values: any[]): string | undefined {
  return values
    .find((value) => typeof value === "string" && value.trim())
    ?.trim();
}

function basename(path?: string): string {
  if (!path) return "";
  const clean = String(path).split("?")[0].replace(/\\/g, "/");
  return clean.split("/").filter(Boolean).pop() || clean;
}

function titleCaseToolName(name: string): string {
  return (name || "tool")
    .replace(/^mcp[_:-]/i, "")
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function getToolMeta(
  item: Extract<ConversationAnalysisItem, { type: "tool" }>,
): AnalysisToolMeta {
  const name = item.name || "tool";
  const args =
    item.args && typeof item.args === "object" && !Array.isArray(item.args)
      ? item.args
      : {};
  const path = firstString(
    args.output_path,
    args.path,
    args.file_path,
    args.filename,
    args.name,
  );
  const url = firstString(args.url, args.link, args.uri);
  const query = firstString(args.query, args.q, args.keyword, args.search);
  const recipient = firstString(
    args.to,
    args.recipient,
    args.user,
    args.channel,
    args.agent_name,
  );
  const lower = name.toLowerCase();
  if (lower.includes("write_file") || lower.includes("create_file"))
    return {
      title: path ? `Created ${basename(path)}` : "Created a file",
      label: "Workspace",
      target: path,
      kind: "file",
    };
  if (lower.includes("edit_file") || lower.includes("update_file"))
    return {
      title: path ? `Updated ${basename(path)}` : "Updated a file",
      label: "Workspace",
      target: path,
      kind: "file",
    };
  if (lower.includes("move_file"))
    return {
      title: path ? `Moved ${basename(path)}` : "Moved a file",
      label: "Workspace",
      target: path,
      kind: "file",
    };
  if (lower.includes("delete_file"))
    return {
      title: path ? `Deleted ${basename(path)}` : "Deleted a file",
      label: "Workspace",
      target: path,
      kind: "file",
    };
  if (lower.startsWith("convert_"))
    return {
      title: path ? `Converted ${basename(path)}` : titleCaseToolName(name),
      label: "Workspace",
      target: path,
      kind: "file",
    };
  if (
    lower.includes("read_webpage") ||
    lower.includes("browser") ||
    lower.includes("webpage")
  )
    return {
      title: url
        ? `Read ${url.replace(/^https?:\/\//, "").split("/")[0]}`
        : titleCaseToolName(name),
      label: "Browser",
      target: url,
      kind: "browser",
    };
  if (lower.includes("search"))
    return {
      title: query ? `Searched ${query}` : titleCaseToolName(name),
      label: "Search",
      target: query,
      kind: "search",
    };
  if (lower.includes("send_") || lower.includes("message"))
    return {
      title: recipient
        ? `Sent message to ${recipient}`
        : titleCaseToolName(name),
      label: "Message",
      target: recipient,
      kind: "message",
    };
  if (lower.includes("agent"))
    return {
      title: titleCaseToolName(name),
      label: "Agent",
      target: recipient,
      kind: "agent",
    };
  if (lower.includes("mcp") || lower.includes(":"))
    return {
      title: titleCaseToolName(name),
      label: "MCP",
      target: path || url || query,
      kind: "mcp",
    };
  return {
    title: titleCaseToolName(name),
    label: "Tool",
    target: path || url || query || recipient,
    kind: "command",
  };
}

function getToolIcon(kind: AnalysisToolMeta["kind"]) {
  if (kind === "file") return IconFileText;
  if (kind === "search") return IconSearch;
  if (kind === "browser") return IconBrowser;
  if (kind === "message") return IconMessageCircle;
  if (kind === "agent") return IconBrain;
  if (kind === "mcp") return IconTools;
  return IconTerminal2;
}

function describeAnalysis(
  items: ConversationAnalysisItem[],
  t: (key: string, options?: any) => string,
) {
  const tools = items.filter(
    (item): item is Extract<ConversationAnalysisItem, { type: "tool" }> =>
      item.type === "tool",
  );
  if (!tools.length) return t("agent.chat.thoughtProcess");
  const counts = { created: 0, updated: 0, deleted: 0, commands: 0, agents: 0 };
  for (const tool of tools) {
    const name = tool.name.toLowerCase();
    if (name.includes("write_file") || name.includes("create_file"))
      counts.created += 1;
    else if (
      name.includes("edit_file") ||
      name.includes("update_file") ||
      name.includes("move_file") ||
      name.startsWith("convert_")
    )
      counts.updated += 1;
    else if (name.includes("delete_file")) counts.deleted += 1;
    else if (name === "send_message_to_agent" || name === "send_file_to_agent")
      counts.agents += 1;
    else counts.commands += 1;
  }
  const parts: string[] = [];
  if (counts.created)
    parts.push(t("agent.chat.createdFiles", { count: counts.created }));
  if (counts.updated)
    parts.push(t("agent.chat.updatedFiles", { count: counts.updated }));
  if (counts.deleted)
    parts.push(t("agent.chat.deletedFiles", { count: counts.deleted }));
  if (counts.commands)
    parts.push(t("agent.chat.ranCommands", { count: counts.commands }));
  if (counts.agents)
    parts.push(t("agent.chat.ranAgents", { count: counts.agents }));
  return parts.join(", ") || t("agent.chat.ranCommands", { count: tools.length });
}

export function AnalysisCard({
  items,
  running,
  expanded,
  onToggle,
}: {
  items: ConversationAnalysisItem[];
  running: boolean;
  expanded: boolean;
  onToggle: () => void;
}) {
  const { t } = useTranslation();
  const runningTool = [...items]
    .reverse()
    .find((item) => item.type === "tool" && item.status === "running");
  const title =
    runningTool?.type === "tool"
      ? getToolMeta(runningTool).title
      : describeAnalysis(items, t);
  return (
    <div
      className={`analysis-trace${expanded ? " analysis-trace--open" : ""}${running ? " analysis-trace--running" : ""}`}
    >
      <div className="analysis-trace-shell">
        <button className="analysis-trace-header" onClick={onToggle}>
          <span className="analysis-trace-signal" aria-hidden="true">
            <span />
            <span />
            <span />
          </span>
          <span className="analysis-trace-title">{title}</span>
          <IconChevronDown
            className="analysis-trace-chevron"
            size={15}
            stroke={1.8}
          />
        </button>
        {expanded && (
          <div className="analysis-trace-body">
            {items.map((item, index) => {
              const last = index === items.length - 1;
              if (item.type === "thinking") {
                return (
                  <div key={`${item.type}-${index}`} className="analysis-trace-row">
                    <div className="analysis-trace-node-wrap">
                      <div className="analysis-trace-node analysis-trace-node--thought">
                        <IconClock size={18} stroke={1.65} />
                      </div>
                      {!last && <div className="analysis-trace-rail" />}
                    </div>
                    <div
                      className="analysis-trace-row-content"
                      style={{
                        paddingBottom: last ? 0 : 18,
                        fontSize: 13,
                        lineHeight: 1.5,
                        whiteSpace: "pre-wrap",
                      }}
                    >
                      {item.content}
                    </div>
                  </div>
                );
              }
              const meta = getToolMeta(item);
              const ToolIcon = getToolIcon(meta.kind);
              const args =
                item.args && Object.keys(item.args).length
                  ? JSON.stringify(item.args, null, 2)
                  : "";
              return (
                <div
                  key={`${item.name}-${index}`}
                  className={`analysis-trace-row${item.status === "running" ? " analysis-trace-row--running" : ""}`}
                >
                  <div className="analysis-trace-node-wrap">
                    <div
                      className={`analysis-trace-node analysis-trace-node--tool analysis-tool-icon${item.status === "running" ? " analysis-tool-icon--running" : ""}`}
                    >
                      <ToolIcon size={18} stroke={1.65} />
                    </div>
                    {!last && <div className="analysis-trace-rail" />}
                  </div>
                  <div
                    className="analysis-trace-row-content"
                    style={{ paddingBottom: last ? 0 : 18 }}
                  >
                    <div style={{ color: "var(--text-secondary)", fontSize: 13 }}>
                      {meta.title}
                      {item.status === "running"
                        ? ` · ${t("common.loading")}`
                        : ""}
                    </div>
                    <div
                      style={{
                        display: "flex",
                        gap: 6,
                        marginTop: 8,
                        flexWrap: "wrap",
                      }}
                    >
                      <span className="conversation-tool-chip">{meta.label}</span>
                      {meta.target && (
                        <span className="conversation-tool-chip conversation-tool-chip--target">
                          {meta.target}
                        </span>
                      )}
                    </div>
                    {(args || item.result) && (
                      <details className="conversation-tool-details">
                        <summary>{t("agent.chat.viewDetails")}</summary>
                        {args && <pre>{args}</pre>}
                        {item.result && <pre>{item.result}</pre>}
                      </details>
                    )}
                  </div>
                </div>
              );
            })}
            {running && (
              <div className="analysis-trace-row">
                <div className="analysis-trace-node-wrap">
                  <div className="analysis-trace-node analysis-trace-node--pending">
                    <IconClock size={18} stroke={1.65} />
                  </div>
                </div>
                <div style={{ color: "var(--text-tertiary)", fontSize: 13 }}>
                  {t("agent.chat.inProgress")}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
