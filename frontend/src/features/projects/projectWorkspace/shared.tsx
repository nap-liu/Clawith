import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { IconMessageCircle } from "@tabler/icons-react";
import {
  Button,
  ProjectEmptyState,
  ProjectStatusBadge,
} from "../components/ProjectUI";
import { resolveProjectSessionRoute as sessionRouteOf } from "../projectSessionRouting";
import { statusLabel } from "./helpers";
import type {
  OpenSession,
  RecordValue,
} from "./types";
import type {
  ProjectSessionIntent as SessionIntent,
} from "../projectSessionRouting";

export function SessionButton({
  source,
  onOpen,
  label,
  intent = "auto",
}: {
  source: RecordValue;
  onOpen: OpenSession;
  label?: string;
  intent?: SessionIntent;
}) {
  const { t } = useTranslation();
  if (!sessionRouteOf(source, intent)) return null;
  return (
    <Button
      type="button"
      variant="ghost"
      className="project-workspace__session-link"
      onClick={() => onOpen(source, undefined, intent)}
    >
      <IconMessageCircle size={14} />
      {label || t("projectWorkspacePage.session.view")}
    </Button>
  );
}

export function StatusPill({ status }: { status: string }) {
  const { t } = useTranslation();
  const tone = ["running", "success", "completed", "done"].includes(status)
    ? "success"
    : ["failed", "blocked"].includes(status)
      ? "error"
      : ["paused", "waiting", "review"].includes(status)
        ? "warning"
        : "neutral";
  return (
    <ProjectStatusBadge tone={tone} className="project-workspace__status">
      {statusLabel(status, t)}
    </ProjectStatusBadge>
  );
}

export function EmptyState(props: Parameters<typeof ProjectEmptyState>[0]) {
  return <ProjectEmptyState {...props} />;
}

export function SectionHeading({
  title,
  actions,
  className = "",
}: {
  eyebrow: string;
  title: string;
  description: string;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <header
      className={`project-workspace__section-heading${actions ? "" : " is-title-only"}${className ? ` ${className}` : ""}`}
      aria-label={title}
    >
      <h2 className="project-workspace__visually-hidden">{title}</h2>
      {actions && (
        <div className="project-workspace__heading-actions">{actions}</div>
      )}
    </header>
  );
}
