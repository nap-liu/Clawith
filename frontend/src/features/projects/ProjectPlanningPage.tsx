import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { useNavigate, useParams } from "react-router-dom";
import {
  IconAlertTriangle,
  IconArrowLeft,
  IconArrowRight,
  IconCheck,
  IconCrown,
  IconGitCommit,
  IconLoader2,
  IconMessageCircle,
  IconRefresh,
  IconShieldCheck,
  IconSparkles,
  IconTargetArrow,
  IconTool,
  IconUsers,
} from "@tabler/icons-react";

import SessionViewerDrawer, {
  type SessionViewerGroupConfig,
  type SessionViewerTarget,
} from "../../components/SessionViewerDrawer";
import { projectsApi } from "../../services/projects";
import { Button, ProjectStatusBadge } from "./components/ProjectUI";
import "./projectPortfolio.css";
import "./projectPlanning.css";

type RecordValue = Record<string, unknown>;

const text = (source: RecordValue, ...keys: string[]) => {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "string" || typeof value === "number")
      return String(value);
  }
  return "";
};

export default function ProjectPlanningPage() {
  const { t } = useTranslation();
  const { projectId = "" } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const [drawerOpen, setDrawerOpen] = useState(true);
  const projectQuery = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => projectsApi.get(projectId),
    enabled: Boolean(projectId),
  });
  const membersQuery = useQuery({
    queryKey: ["project", projectId, "members"],
    queryFn: () => projectsApi.listMembers(projectId),
    enabled: Boolean(projectId),
  });
  const groupSessionQuery = useQuery({
    queryKey: ["project", projectId, "group-session"],
    queryFn: () => projectsApi.getGroupSession(projectId),
    enabled: Boolean(projectId),
  });
  const groupSessionId = text(
    groupSessionQuery.data || {},
    "id",
    "group_session_id",
  );
  const planningMessagesQuery = useQuery({
    queryKey: ["project", projectId, "group-session", groupSessionId, "messages"],
    queryFn: () => projectsApi.listGroupMessages(projectId, groupSessionId),
    enabled: Boolean(projectId && groupSessionId),
  });
  const confirmMutation = useMutation({
    mutationFn: () => projectsApi.confirmKickoff(projectId),
    onSuccess: () => navigate(`/projects/${projectId}`, { replace: true }),
  });

  const members = (
    Array.isArray(membersQuery.data) ? membersQuery.data : []
  ) as RecordValue[];
  const leader = members.find((member) => member.is_leader === true);
  const leaderName =
    text(leader || {}, "name_snapshot", "agent_name", "name") ||
    t("projectTerminology.projectOwner");
  const project = projectQuery.data;
  const session = groupSessionQuery.data;
  const groupMembers = useMemo(
    () =>
      members
        .map((member) => ({
          agentId: text(member, "agent_id"),
          name:
            text(member, "name_snapshot", "agent_name", "name") ||
            t("projectTerminology.dynamicCopy.digitalEmployee"),
          isLeader: member.is_leader === true,
          isEnabled: member.is_enabled !== false,
        }))
        .filter((member) => member.agentId),
    [members, t],
  );
  const groupConfig = useMemo<SessionViewerGroupConfig>(
    () => ({
      members: groupMembers,
      maxMentions: 0,
      loadMessages: (sessionId, options) =>
        projectsApi.listGroupMessages(projectId, sessionId, options),
      sendMessage: (sessionId, payload) =>
        projectsApi.sendGroupMessage(projectId, sessionId, payload),
    }),
    [groupMembers, projectId],
  );
  const sessionTarget = useMemo<SessionViewerTarget | null>(
    () =>
      session
        ? {
            sessionId: text(session, "id", "group_session_id"),
            agentId: text(session, "access_agent_id", "agent_id"),
            title:
              text(session, "title", "group_name") ||
              t("projectTerminology.planning.sessionTitle", {
                name: leaderName,
              }),
            mode: "group",
            status: "planning",
          }
        : null,
    [leaderName, session, t],
  );
  const loading =
    projectQuery.isPending ||
    membersQuery.isPending ||
    groupSessionQuery.isPending ||
    planningMessagesQuery.isPending;
  const failed =
    projectQuery.isError ||
    membersQuery.isError ||
    groupSessionQuery.isError ||
    planningMessagesQuery.isError;
  const discussionMessages = planningMessagesQuery.data?.items || [];
  const discussionRoles = new Set(
    discussionMessages.map((message) => text(message, "role")),
  );
  const discussionCount = discussionMessages.filter((message) =>
    ["user", "assistant"].includes(text(message, "role")),
  ).length;
  const canConfirm =
    discussionRoles.has("user") && discussionRoles.has("assistant");

  if (loading) {
    return (
      <main className="pm-planning-page pm-planning-state">
        <IconLoader2 className="pm-spinner-icon" size={26} />
        <strong>{t("projectTerminology.planningConnection")}</strong>
        <p>{t("projectTerminology.planning.loadingDescription")}</p>
      </main>
    );
  }

  if (failed || !project || !sessionTarget) {
    const error =
      projectQuery.error ||
      membersQuery.error ||
      groupSessionQuery.error ||
      planningMessagesQuery.error;
    return (
      <main className="pm-planning-page pm-planning-state" role="alert">
        <IconAlertTriangle size={26} />
        <strong>{t("projectTerminology.planning.unavailableTitle")}</strong>
        <p>
          {error instanceof Error
            ? error.message
            : t("projectTerminology.planningUnavailable")}
        </p>
        <Button
          variant="secondary"
          onClick={() => {
            void projectQuery.refetch();
            void membersQuery.refetch();
            void groupSessionQuery.refetch();
            void planningMessagesQuery.refetch();
          }}
        >
          <IconRefresh size={16} />
          {t("projectTerminology.planning.reload")}
        </Button>
      </main>
    );
  }

  const hasStarted = project.status !== "planning";

  return (
    <main className="pm-planning-page">
      <header className="pm-planning-header">
        <Button variant="ghost" onClick={() => navigate("/projects")}>
          <IconArrowLeft size={16} />
          {t("projectTerminology.planning.backToProjects")}
        </Button>
        <div>
          <span>{t("projectTerminology.planning.eyebrow")}</span>
          <h1>{project.name}</h1>
          <p>{t("projectTerminology.planning.description")}</p>
        </div>
        <ProjectStatusBadge tone={hasStarted ? "success" : "warning"}>
          {hasStarted
            ? t("projectTerminology.planning.started")
            : t("projectTerminology.planning.planning")}
        </ProjectStatusBadge>
      </header>

      <div className="pm-planning-layout">
        <section className="pm-planning-conversation">
          <div className="pm-planning-leader">
            <span>{leaderName.slice(0, 1)}</span>
            <div>
              <small>{t("projectTerminology.projectOwner")}</small>
              <h2>{leaderName}</h2>
              <p>{t("projectTerminology.planning.ownerDescription")}</p>
            </div>
            <IconCrown size={22} />
          </div>
          <div className="pm-planning-dialogue-card">
            <IconMessageCircle size={22} />
            <div>
              <strong>{t("projectTerminology.planningTogether")}</strong>
              <p>{t("projectTerminology.planningPrompt")}</p>
              <small>
                {discussionCount > 0
                  ? t("projectTerminology.planning.messageCount", {
                      count: discussionCount,
                    })
                  : t("projectTerminology.planning.startMessage")}
              </small>
            </div>
            <Button variant="primary" onClick={() => setDrawerOpen(true)}>
              <IconMessageCircle size={16} />
              {discussionCount > 0
                ? t("projectTerminology.planning.continue")
                : t("projectTerminology.planning.start")}
            </Button>
          </div>

          <div
            className="pm-planning-outcomes"
            aria-label={t("projectTerminology.planning.outcomesAria")}
          >
            <article>
              <IconTargetArrow size={18} />
              <span>
                <strong>{t("projectTerminology.planning.goalTitle")}</strong>
                <small>
                  {t("projectTerminology.planning.goalDescription")}
                </small>
              </span>
            </article>
            <article>
              <IconCheck size={18} />
              <span>
                <strong>
                  {t("projectTerminology.planning.successTitle")}
                </strong>
                <small>
                  {t("projectTerminology.planning.successDescription")}
                </small>
              </span>
            </article>
            <article>
              <IconSparkles size={18} />
              <span>
                <strong>{t("projectTerminology.planning.planTitle")}</strong>
                <small>
                  {t("projectTerminology.planning.planDescription")}
                </small>
              </span>
            </article>
          </div>
        </section>

        <aside className="pm-planning-governance">
          <header>
            <IconShieldCheck size={20} />
            <div>
              <strong>{t("projectTerminology.planning.members")}</strong>
            </div>
          </header>
          <div className="pm-planning-role-scope pm-planning-role-scope--leader">
            <IconCrown size={17} />
            <span>
              <strong>{t("projectTerminology.ownerRole")}</strong>
              <small>{t("projectTerminology.planning.ownerScope")}</small>
            </span>
          </div>
          <div className="pm-planning-role-scope">
            <IconUsers size={17} />
            <span>
              <strong>{t("projectTerminology.planning.members")}</strong>
              <small>{t("projectTerminology.planning.memberScope")}</small>
            </span>
          </div>
          <ul>
            <li>
              <IconGitCommit size={15} />
              {t("projectTerminology.planning.recordPreserved")}
            </li>
            <li>
              <IconTool size={15} />
              {t("projectTerminology.planning.capabilitiesConfirmed")}
            </li>
            <li>
              <IconShieldCheck size={15} />
              {t("projectTerminology.planning.approvalRequired")}
            </li>
          </ul>
          {confirmMutation.isError && (
            <div className="pm-planning-error" role="alert">
              <IconAlertTriangle size={15} />
              {confirmMutation.error instanceof Error
                ? confirmMutation.error.message
                : t("projectTerminology.planning.startFailed")}
            </div>
          )}
          {hasStarted ? (
            <Button
              variant="primary"
              onClick={() => navigate(`/projects/${projectId}`)}
            >
              {t("projectTerminology.planning.enterWorkspace")}
              <IconArrowRight size={16} />
            </Button>
          ) : (
            <Button
              variant="primary"
              disabled={confirmMutation.isPending || !canConfirm}
              onClick={() => confirmMutation.mutate()}
            >
              {confirmMutation.isPending ? (
                <IconLoader2 className="pm-spinner-icon" size={16} />
              ) : (
                <IconCheck size={16} />
              )}
              {t("projectTerminology.confirmAndStart")}
            </Button>
          )}
          {!hasStarted && (
            <small className="pm-planning-confirm-hint">
              {canConfirm
                ? t("projectTerminology.confirmHintReady")
                : t("projectTerminology.confirmHintWaiting")}
            </small>
          )}
        </aside>
      </div>

      <SessionViewerDrawer
        agentId={text(session || {}, "access_agent_id", "agent_id")}
        agentName={leaderName}
        target={drawerOpen ? sessionTarget : null}
        interactive
        groupConfig={groupConfig}
        onClose={() => {
          setDrawerOpen(false);
          void planningMessagesQuery.refetch();
        }}
      />
    </main>
  );
}
