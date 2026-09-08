import assert from "node:assert/strict";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { loadTypeScriptModule } from "./load-typescript-module.mjs";

const __dirname = dirname(fileURLToPath(import.meta.url));
const {
  inferProjectSessionIntent,
  projectSessionTargetFromUrl,
  projectSessionUrlPatch,
  resolveProjectSessionRoute,
} = loadTypeScriptModule(
  resolve(__dirname, "../src/features/projects/projectSessionRouting.ts"),
);
const {
  normalizeProjectWorkspaceUrl,
  projectWorkItemCompatibilityTargetFromUrl,
  projectWorkItemUrlPatch,
  projectWorkspaceTabFromUrl,
  projectWorkspaceTabUrlPatch,
} = loadTypeScriptModule(
  resolve(__dirname, "../src/features/projects/projectWorkspaceRouting.ts"),
);
const { resolveConversationMessageAnchor } = loadTypeScriptModule(
  resolve(
    __dirname,
    "../src/features/conversation/core/conversationAnchoring.ts",
  ),
);
const plain = (value) => JSON.parse(JSON.stringify(value));

{
  const target = {
    sessionId: "session-1",
    agentId: "agent-1",
    anchorMessageId: "message-1",
    projectRunId: "run-1",
    kind: "session",
    mode: "a2a",
    readOnly: true,
  };
  const patch = projectSessionUrlPatch(target);
  const params = new URLSearchParams();
  Object.entries(patch).forEach(([key, value]) => {
    if (value) params.set(key, value);
  });
  assert.deepEqual(plain(projectSessionTargetFromUrl(params)), target);
  assert.equal(projectSessionUrlPatch(null).sessionId, undefined);
  assert.equal(projectSessionTargetFromUrl(new URLSearchParams()), null);
}

{
  const legacyCollection = normalizeProjectWorkspaceUrl(
    new URLSearchParams(
      "tab=detail&runsPage=4&sessionId=session-1&sessionAgentId=agent-1",
    ),
  );
  assert.equal(legacyCollection.get("tab"), "work");
  assert.equal(legacyCollection.get("workView"), "list");
  assert.equal(legacyCollection.get("runsPage"), null);
  assert.equal(legacyCollection.get("sessionId"), "session-1");
  assert.equal(legacyCollection.get("sessionAgentId"), "agent-1");

  const legacyDetail = normalizeProjectWorkspaceUrl(
    new URLSearchParams(
      "tab=detail&workItem=work-1&workItemTab=review&evidence=file.md&auditEventsPage=3",
    ),
  );
  assert.equal(legacyDetail.get("tab"), "work");
  assert.equal(legacyDetail.get("workItem"), "work-1");
  assert.equal(legacyDetail.get("workItemTab"), "review");
  assert.equal(legacyDetail.get("evidence"), "file.md");
  assert.equal(legacyDetail.get("auditEventsPage"), null);
  assert.equal(projectWorkspaceTabFromUrl(legacyDetail), "work");
  assert.deepEqual(
    plain(projectWorkItemCompatibilityTargetFromUrl(legacyDetail)),
    { workItemId: "work-1", anchor: "work-item-review" },
  );

  const legacyContext = normalizeProjectWorkspaceUrl(
    new URLSearchParams("tab=detail&workItem=work-context&workItemTab=context"),
  );
  assert.equal(legacyContext.get("workItem"), "work-context");
  assert.equal(legacyContext.get("workItemTab"), null);
  assert.equal(projectWorkItemCompatibilityTargetFromUrl(legacyContext), null);

  for (const section of ["execution", "conversation", "changes", "review"]) {
    const legacySection = normalizeProjectWorkspaceUrl(
      new URLSearchParams(
        `tab=detail&workItem=work-${section}&workItemTab=${section}`,
      ),
    );
    assert.deepEqual(
      plain(projectWorkItemCompatibilityTargetFromUrl(legacySection)),
      {
        workItemId: `work-${section}`,
        anchor: `work-item-${section}`,
      },
      `legacy ${section} links must keep their exact work item`,
    );
  }

  const invalidTab = normalizeProjectWorkspaceUrl(
    new URLSearchParams("tab=unknown&workItem=work-1&runsPage=2"),
  );
  assert.equal(invalidTab.get("tab"), "cockpit");
  assert.equal(invalidTab.get("workItem"), null);
  assert.equal(invalidTab.get("runsPage"), null);

  const legacyMatrix = normalizeProjectWorkspaceUrl(
    new URLSearchParams(
      "tab=matrix&capFilter=enabled&capabilityMatrixPage=2&projectToolMatrixPage=3&membersPage=4",
    ),
  );
  assert.equal(legacyMatrix.get("tab"), "capabilities");
  assert.equal(legacyMatrix.get("capView"), "matrix");
  assert.equal(legacyMatrix.get("capFilter"), "enabled");
  assert.equal(legacyMatrix.get("capabilityMatrixPage"), "2");
  assert.equal(legacyMatrix.get("projectToolMatrixPage"), "3");
  assert.equal(legacyMatrix.get("membersPage"), null);
  assert.equal(projectWorkspaceTabFromUrl(legacyMatrix), "capabilities");

  const unknownCapabilityView = normalizeProjectWorkspaceUrl(
    new URLSearchParams(
      "tab=capabilities&capView=unknown&toolMember=member-1&projectToolsPage=2",
    ),
  );
  assert.equal(unknownCapabilityView.get("capView"), "list");
  assert.equal(unknownCapabilityView.get("toolMember"), "member-1");
  assert.equal(unknownCapabilityView.get("projectToolsPage"), "2");

  const legacyMatrixPatch = projectWorkspaceTabUrlPatch("matrix", {
    capabilityMatrixPage: "2",
  });
  assert.equal(legacyMatrixPatch.tab, "capabilities");
  assert.equal(legacyMatrixPatch.capView, "matrix");
  assert.equal(legacyMatrixPatch.capabilityMatrixPage, "2");

  const capabilityListPatch = projectWorkspaceTabUrlPatch("capabilities", {
    capView: "list",
    capFilter: "enabled",
    capabilitiesPage: "3",
  });
  assert.equal(capabilityListPatch.tab, "capabilities");
  assert.equal(capabilityListPatch.capView, "list");
  assert.equal(capabilityListPatch.capFilter, "enabled");
  assert.equal(capabilityListPatch.capabilitiesPage, "3");

  const membersPatch = projectWorkspaceTabUrlPatch("members", {
    member: "member-2",
    membersPage: "2",
  });
  assert.equal(membersPatch.tab, "members");
  assert.equal(membersPatch.member, "member-2");
  assert.equal(membersPatch.membersPage, "2");
  assert.equal(membersPatch.capView, undefined);
  assert.equal(membersPatch.capabilityMatrixPage, undefined);

  const sameTabPatch = projectWorkspaceTabUrlPatch("runs", {});
  assert.ok(
    !("runsPage" in sameTabPatch),
    "destination tab pagination must be retained",
  );
  const workPatch = projectWorkspaceTabUrlPatch("work", {
    workItem: "work-2",
  });
  assert.equal(workPatch.workItem, "work-2");
  assert.ok(
    !("workItemTab" in workPatch),
    "new work-item links must not generate the retired tab query",
  );

  const switchWorkItem = projectWorkItemUrlPatch("work-2", "work-1");
  assert.equal(switchWorkItem.workItem, "work-2");
  assert.equal(switchWorkItem.workItemTab, undefined);
  assert.equal(switchWorkItem.evidence, undefined);
  assert.equal(switchWorkItem.workItemRunsPage, undefined);

  const retainCurrentWorkItem = projectWorkItemUrlPatch("work-2", "work-2");
  assert.deepEqual(plain(retainCurrentWorkItem), { workItem: "work-2" });

  const closeWorkItem = projectWorkItemUrlPatch(undefined, "work-2");
  assert.equal(closeWorkItem.workItem, undefined);
  assert.equal(closeWorkItem.workItemTab, undefined);
  assert.equal(closeWorkItem.evidence, undefined);
}

{
  const route = resolveProjectSessionRoute(
    {
      id: "project-run-domain-id",
      agent_id: "leader-agent",
      trigger_type: "manual",
      input: { dispatch: { turn_anchor_id: "run-turn-anchor" } },
      output: { subagent_session_id: "leader-child-session" },
    },
    "run",
  );
  assert.deepEqual(plain(route), {
    sessionId: "leader-child-session",
    kind: "session",
    agentId: "leader-agent",
    intent: "run",
    anchorMessageId: "run-turn-anchor",
  });
}

{
  const first = resolveProjectSessionRoute(
    {
      subagent_session_id: "shared-child-session",
      agent_id: "worker-agent",
      input: { dispatch: { turn_anchor_id: "first-run-turn" } },
    },
    "run",
  );
  const second = resolveProjectSessionRoute(
    {
      subagent_session_id: "shared-child-session",
      agent_id: "worker-agent",
      input: { dispatch: { turn_anchor_id: "second-run-turn" } },
    },
    "run",
  );
  assert.equal(first?.sessionId, second?.sessionId);
  assert.equal(first?.anchorMessageId, "first-run-turn");
  assert.equal(second?.anchorMessageId, "second-run-turn");
}

assert.equal(
  resolveProjectSessionRoute(
    { id: "project-run-domain-id", agent_id: "leader-agent" },
    "run",
  ),
  null,
  "a ProjectRun id must never be treated as a ChatSession id",
);

{
  const event = {
    id: "event-domain-id",
    event_type: "a2a.delivered",
    from_agent_id: "worker-z",
    to_agent_id: "reviewer-a",
    event_metadata: {
      session_id: "exact-a2a-session",
      session_access_agent_id: "reviewer-a",
      group_session_id: "project-group-session",
    },
  };
  assert.equal(inferProjectSessionIntent(event), "a2a");
  assert.deepEqual(plain(resolveProjectSessionRoute(event)), {
    sessionId: "exact-a2a-session",
    kind: "session",
    agentId: "reviewer-a",
    intent: "a2a",
  });
  assert.equal(
    resolveProjectSessionRoute(event, "group")?.sessionId,
    "project-group-session",
    "an explicit group route must not accidentally open the A2A thread",
  );
}

{
  const a2aRun = {
    id: "a2a-project-run-id",
    trigger_type: "a2a",
    from_agent_id: "worker-z",
    to_agent_id: "reviewer-a",
    output: {
      session_id: "a2a-run-session",
      session_agent_id: "reviewer-a",
    },
  };
  assert.equal(inferProjectSessionIntent(a2aRun), "a2a");
  assert.equal(
    resolveProjectSessionRoute(a2aRun)?.sessionId,
    "a2a-run-session",
  );
}

{
  const enrichedA2ARun = {
    id: "a2a-project-run-id",
    trigger_type: "a2a",
    agent_id: "reviewer-a",
    session_id: "visible-a2a-session",
    subagent_session_id: "worker-child-session",
  };
  assert.deepEqual(
    plain(resolveProjectSessionRoute(enrichedA2ARun)),
    {
      sessionId: "visible-a2a-session",
      kind: "session",
      agentId: "reviewer-a",
      intent: "a2a",
    },
    "the stable Run DTO must open the visible A2A conversation, not its worker child",
  );
}

{
  const workItemSession = {
    run_id: "project-run-id",
    source_channel: "subagent",
    session_id: "exact-worker-session",
    agent_id: "worker-agent",
  };
  assert.deepEqual(
    plain(resolveProjectSessionRoute(workItemSession)),
    {
      sessionId: "exact-worker-session",
      kind: "session",
      agentId: "worker-agent",
      intent: "run",
    },
    "a WorkItemDetailOut session record must route directly to its exact ChatSession",
  );
}

{
  const route = resolveProjectSessionRoute({
    event_type: "a2a.queued",
    from_agent_id: "z-agent",
    to_agent_id: "a-agent",
    metadata: JSON.stringify({ session_id: "a2a-json-session" }),
  });
  assert.equal(route?.sessionId, "a2a-json-session");
  assert.equal(
    route?.agentId,
    "a-agent",
    "A2A fallback access identity must match the backend canonical min id",
  );
}

assert.deepEqual(
  plain(
    resolveProjectSessionRoute({
      id: "group-chat-session",
      source_channel: "project",
      access_agent_id: "leader-agent",
    }),
  ),
  {
    sessionId: "group-chat-session",
    kind: "group",
    agentId: "leader-agent",
    intent: "group",
  },
);

assert.deepEqual(
  plain(
    resolveProjectSessionRoute({
      id: "a2a-chat-session",
      source_channel: "agent",
      agent_id: "canonical-access-agent",
    }),
  ),
  {
    sessionId: "a2a-chat-session",
    kind: "session",
    agentId: "canonical-access-agent",
    intent: "a2a",
  },
);

assert.equal(
  resolveProjectSessionRoute({
    id: "git-commit-hash",
    metadata: { run_id: "run-domain-id" },
  }),
  null,
  "Git and audit entities without an exact conversation anchor must not open an unrelated session",
);

{
  const sharedSessionRows = [
    {
      id: "durable-message-run-a",
      metadata: {
        project_run_id: "run-a",
        turn_anchor_id: "turn-a",
      },
    },
    {
      id: "durable-message-run-b",
      metadata: {
        project_run_id: "run-b",
        turn_anchor_id: "turn-b",
      },
    },
  ];
  assert.equal(
    resolveConversationMessageAnchor(sharedSessionRows, "turn-b", "run-b"),
    "durable-message-run-b",
    "a reused session must resolve to the exact durable message for its Run",
  );
  assert.equal(
    resolveConversationMessageAnchor(sharedSessionRows, undefined, "run-a"),
    "durable-message-run-a",
    "projectRunId must remain a precise fallback when no turn anchor is present",
  );
}

{
  const requestedAnchor = "turn-anchor-shared-in-metadata";
  assert.equal(
    resolveConversationMessageAnchor(
      [
        {
          id: "earlier-message",
          metadata: { turn_anchor_id: requestedAnchor },
        },
        {
          id: requestedAnchor,
          metadata: { turn_anchor_id: requestedAnchor },
        },
      ],
      requestedAnchor,
    ),
    requestedAnchor,
    "the durable message id must outrank an earlier nested metadata match",
  );
}

{
  assert.equal(
    resolveConversationMessageAnchor(
      [
        {
          id: "tool-row",
          role: "tool_call",
          toolCallId: "tool-call-42",
          metadata: { project_run_id: "run-tool" },
        },
      ],
      "tool-call-42",
      "run-tool",
    ),
    "tool-call-42",
    "tool anchors must focus the grouped tool row in the standard timeline",
  );
}

console.log("project exact-session routing tests passed");
