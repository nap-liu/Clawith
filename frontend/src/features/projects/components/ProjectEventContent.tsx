import { createPortal } from "react-dom";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import MarkdownRenderer from "../../../components/MarkdownRenderer";
import Button from "../../../components/ui/Button";
import { useAnchoredPopoverPosition } from "../../../components/ui/useAnchoredPopoverPosition";

export function ProjectEventLabel({ eventType }: { eventType?: string }) {
  const { t } = useTranslation();
  const code = String(eventType || "").trim();
  return (
    <>
      {code
        ? t(`projectAudit.events.${code}`, {
            defaultValue: t("projectAudit.eventFallback"),
          })
        : t("projectAudit.eventFallback")}
    </>
  );
}

function markdownPreview(content: string, maxChars: number): string {
  const characters = Array.from(content.trim());
  if (characters.length <= maxChars) return content.trim();
  return `${characters.slice(0, maxChars).join("").trimEnd()}…`;
}

export default function ProjectEventContent({
  content,
  eventType,
  maxChars = 260,
  empty,
  className = "",
}: {
  content?: string;
  eventType?: string;
  maxChars?: number;
  empty?: string;
  className?: string;
}) {
  const { t } = useTranslation();
  const [previewOpen, setPreviewOpen] = useState(false);
  const anchorRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const closeTimerRef = useRef<number | null>(null);
  const translatedDetail = eventType
    ? t(`projectAudit.details.${eventType}`, { defaultValue: "" }).trim()
    : "";
  const value = String(
    translatedDetail || content || empty || t("projectAudit.noDetails"),
  ).trim();
  const long =
    Array.from(value).length > maxChars || value.split("\n").length > 6;
  const popoverPosition = useAnchoredPopoverPosition({
    open: previewOpen,
    anchorRef,
    popoverRef,
    width: 620,
    minWidth: 280,
    maxWidth: 720,
    maxHeight: 420,
    contentVersion: value,
  });

  const cancelClose = () => {
    if (closeTimerRef.current !== null)
      window.clearTimeout(closeTimerRef.current);
    closeTimerRef.current = null;
  };
  const scheduleClose = () => {
    cancelClose();
    closeTimerRef.current = window.setTimeout(() => setPreviewOpen(false), 120);
  };

  useEffect(() => () => cancelClose(), []);

  return (
    <div className={`project-event-content${className ? ` ${className}` : ""}`}>
      <MarkdownRenderer
        content={long ? markdownPreview(value, maxChars) : value}
      />
      {long && (
        <>
          <Button
            ref={anchorRef}
            type="button"
            variant="ghost"
            className="project-event-content__trigger"
            aria-expanded={previewOpen}
            onMouseEnter={() => {
              cancelClose();
              setPreviewOpen(true);
            }}
            onMouseLeave={scheduleClose}
            onFocus={() => {
              cancelClose();
              setPreviewOpen(true);
            }}
            onBlur={scheduleClose}
            onClick={() => setPreviewOpen((open) => !open)}
          >
            {t("projectAudit.expandDetails")}
          </Button>
          {previewOpen &&
            createPortal(
              <div
                ref={popoverRef}
                className="project-event-content__popover"
                style={popoverPosition.style}
                role="tooltip"
                data-placement={popoverPosition.placement}
                onMouseEnter={cancelClose}
                onMouseLeave={scheduleClose}
              >
                <MarkdownRenderer content={value} />
              </div>,
              document.body,
            )}
        </>
      )}
    </div>
  );
}
