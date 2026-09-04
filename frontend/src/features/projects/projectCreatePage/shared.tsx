import { useEffect, useState } from "react";
import type { TFunction } from "i18next";

import type {
  ProjectAgentOption,
  ProjectAgentSettingsDraft,
  ProjectAgentToolOption,
  ProjectTemplate,
  ProjectVisibility,
} from "../types";

export type Draft = {
  name: string;
  memberIds: string[];
  leaderId: string;
  sharedCapabilityIds: string[];
  inheritedCapabilityIds: string[];
  agentSettings: Record<string, ProjectAgentSettingsDraft>;
  visibility: ProjectVisibility;
  shareTargets: string[];
};

export type PatchDraft = <K extends keyof Draft>(key: K, value: Draft[K]) => void;

export function defaultProjectName(t: TFunction) {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return t("projectCreate.defaultName", { date: `${month}-${day}` });
}

export function initialDraft(t: TFunction): Draft {
  return {
    name: defaultProjectName(t),
    memberIds: [],
    leaderId: "",
    sharedCapabilityIds: [],
    inheritedCapabilityIds: [],
    agentSettings: {},
    visibility: "private",
    shareTargets: [],
  };
}

export function initialAgentSettings(
  agent: ProjectAgentOption,
  tools: ProjectAgentToolOption[],
): ProjectAgentSettingsDraft {
  const visibleTools = tools.filter(
    (tool) =>
      !tool.installed_by_agent_id || tool.installed_by_agent_id === agent.id,
  );
  return {
    config_snapshot: {
      primary_model_id: agent.primary_model_id || null,
      fallback_model_id: agent.fallback_model_id || null,
      temperature: agent.temperature ?? null,
      reasoning_effort: agent.reasoning_effort ?? null,
      max_tool_rounds: agent.max_tool_rounds ?? 50,
      project_instruction: "",
    },
    tools: visibleTools.map((tool) => ({
      ...tool,
      agent_config: { ...(tool.agent_config || {}) },
    })),
    mcp_server_overrides: {},
    skill_capability_ids: [],
  };
}

export function toggleItem(items: string[], id: string) {
  return items.includes(id)
    ? items.filter((item) => item !== id)
    : [...items, id];
}

export function StepTitle({
  number,
  title,
  description,
}: {
  number: string;
  title: string;
  description: string;
}) {
  return (
    <div className="pm-step-title">
      <span>{number}</span>
      <div>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
    </div>
  );
}

export function AgentAvatar({ agent }: { agent: ProjectAgentOption }) {
  const [failed, setFailed] = useState(false);
  const [authenticatedAvatarUrl, setAuthenticatedAvatarUrl] = useState<
    string | null
  >(null);
  const avatarUrl = agent.avatar_url || "";
  const requiresAuthentication = avatarUrl.startsWith("/api");

  useEffect(() => {
    setFailed(false);
    setAuthenticatedAvatarUrl(null);
    if (!requiresAuthentication) return;

    const token = localStorage.getItem("token") || "";
    if (!token) {
      setFailed(true);
      return;
    }

    const controller = new AbortController();
    let objectUrl: string | null = null;
    let cancelled = false;
    const requestUrl = new URL(avatarUrl, window.location.origin);
    requestUrl.searchParams.delete("token");
    requestUrl.searchParams.delete("access_token");

    void fetch(`${requestUrl.pathname}${requestUrl.search}`, {
      headers: { Authorization: `Bearer ${token}` },
      credentials: "same-origin",
      signal: controller.signal,
    })
      .then((response) => {
        if (!response.ok)
          throw new Error(`Avatar request failed: ${response.status}`);
        return response.blob();
      })
      .then((blob) => {
        objectUrl = URL.createObjectURL(blob);
        if (cancelled) {
          URL.revokeObjectURL(objectUrl);
          objectUrl = null;
          return;
        }
        setAuthenticatedAvatarUrl(objectUrl);
      })
      .catch((error) => {
        if (!cancelled && error instanceof Error && error.name !== "AbortError") {
          setFailed(true);
        }
      });

    return () => {
      cancelled = true;
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [avatarUrl, requiresAuthentication]);

  const displayUrl = requiresAuthentication ? authenticatedAvatarUrl : avatarUrl;
  return avatarUrl && !failed ? (
    displayUrl ? (
      <img
        className="pm-agent-avatar"
        src={displayUrl}
        alt=""
        onError={() => setFailed(true)}
      />
    ) : (
      <span className="pm-agent-avatar">{agent.name.slice(0, 1)}</span>
    )
  ) : (
    <span className="pm-agent-avatar">{agent.name.slice(0, 1)}</span>
  );
}

export type ProjectCreateStepProps = {
  draft: Draft;
  template?: ProjectTemplate;
  patch: PatchDraft;
};
