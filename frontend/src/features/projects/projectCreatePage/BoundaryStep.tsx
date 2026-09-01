import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  IconCheck,
  IconLock,
  IconMessageCircle,
  IconUsers,
} from "@tabler/icons-react";

import Pagination from "../../../components/Pagination";
import { Button, SearchInput } from "../components/ProjectUI";
import type { ProjectAgentOption, ProjectTemplate } from "../types";
import { type Draft, type PatchDraft, StepTitle, toggleItem } from "./shared";

export function BoundaryStep({
  draft,
  selectedAgents,
  leader,
  shareTargets,
  capabilityCount,
  template,
  patch,
}: {
  draft: Draft;
  selectedAgents: ProjectAgentOption[];
  leader?: ProjectAgentOption;
  shareTargets: Array<{ id: string; name: string; email?: string | null }>;
  capabilityCount: number;
  template?: ProjectTemplate;
  patch: PatchDraft;
}) {
  const { t } = useTranslation();
  const [shareSearch, setShareSearch] = useState("");
  const [sharePage, setSharePage] = useState(1);
  const sharePageSize = 10;
  const normalizedShareSearch = shareSearch.trim().toLocaleLowerCase();
  const filteredShareTargets = useMemo(
    () =>
      shareTargets.filter((target) =>
        `${target.name} ${target.email || ""}`
          .toLocaleLowerCase()
          .includes(normalizedShareSearch),
      ),
    [normalizedShareSearch, shareTargets],
  );
  const shareTotalPages = Math.max(
    1,
    Math.ceil(filteredShareTargets.length / sharePageSize),
  );
  const currentSharePage = Math.min(sharePage, shareTotalPages);
  const visibleShareTargets = filteredShareTargets.slice(
    (currentSharePage - 1) * sharePageSize,
    currentSharePage * sharePageSize,
  );

  useEffect(() => setSharePage(1), [normalizedShareSearch]);
  useEffect(() => {
    if (sharePage > shareTotalPages) setSharePage(shareTotalPages);
  }, [sharePage, shareTotalPages]);

  return (
    <div className="pm-step-section">
      <StepTitle
        number="03"
        title={t("projectCreate.boundary.title")}
        description={t("projectCreate.boundary.description")}
      />
      <div className="pm-visibility-options">
        <Button
          variant="ghost"
          type="button"
          className={draft.visibility === "private" ? "is-selected" : ""}
          onClick={() => patch("visibility", "private")}
          aria-pressed={draft.visibility === "private"}
        >
          <IconLock size={21} />
          <span>
            <strong>{t("projectCreate.boundary.private")}</strong>
            <small>{t("projectCreate.boundary.privateHint")}</small>
          </span>
          <i>{draft.visibility === "private" && <IconCheck size={14} />}</i>
        </Button>
        <Button
          variant="ghost"
          type="button"
          className={draft.visibility === "shared" ? "is-selected" : ""}
          onClick={() => patch("visibility", "shared")}
          aria-pressed={draft.visibility === "shared"}
        >
          <IconUsers size={21} />
          <span>
            <strong>{t("projectCreate.boundary.shared")}</strong>
            <small>{t("projectCreate.boundary.sharedHint")}</small>
          </span>
          <i>{draft.visibility === "shared" && <IconCheck size={14} />}</i>
        </Button>
      </div>
      {draft.visibility === "shared" && (
        <section className="pm-share-box">
          <header>
            <strong>{t("projectCreate.boundary.shareMembers")}</strong>
            <small>{t("projectCreate.boundary.shareHint")}</small>
          </header>
          <SearchInput
            className="pm-share-search"
            value={shareSearch}
            onChange={(event) => setShareSearch(event.target.value)}
            placeholder={t("projectCreate.boundary.searchMembers")}
            aria-label={t("projectCreate.boundary.searchMembersAria")}
          />
          {shareTargets.length ? (
            visibleShareTargets.length ? (
              <>
                <div className="pm-share-targets">
                  {visibleShareTargets.map((target) => {
                    const selected = draft.shareTargets.includes(target.id);
                    return (
                      <Button
                        variant="ghost"
                        type="button"
                        key={target.id}
                        className={selected ? "is-selected" : ""}
                        onClick={() =>
                          patch(
                            "shareTargets",
                            toggleItem(draft.shareTargets, target.id),
                          )
                        }
                        aria-pressed={selected}
                      >
                        <i>{selected && <IconCheck size={12} />}</i>
                        <span>
                          <strong>{target.name}</strong>
                          <small>{target.email || target.id}</small>
                        </span>
                      </Button>
                    );
                  })}
                </div>
                {filteredShareTargets.length > sharePageSize ? (
                  <Pagination
                    className="pm-share-pagination"
                    page={currentSharePage}
                    pageSize={sharePageSize}
                    total={filteredShareTargets.length}
                    onPageChange={setSharePage}
                    showJump={false}
                    compact
                    ariaLabel={t("projectCreate.boundary.paginationAria")}
                  />
                ) : null}
              </>
            ) : (
              <div className="pm-inline-empty">
                <p>{t("projectCreate.boundary.noSearchResults")}</p>
              </div>
            )
          ) : (
            <div className="pm-inline-empty">
              <p>{t("projectCreate.boundary.noShareTargets")}</p>
            </div>
          )}
        </section>
      )}
      <section className="pm-final-review">
        <header>
          <strong>{t("projectCreate.boundary.review")}</strong>
          <small>{t("projectCreate.boundary.reviewHint")}</small>
        </header>
        <dl>
          <div>
            <dt>{t("projectCreate.boundary.project")}</dt>
            <dd>{draft.name}</dd>
          </div>
          <div>
            <dt>{t("projectCreate.boundary.team")}</dt>
            <dd>
              {template?.snapshot_backed
                ? t("projectCreate.boundary.templateTeam", {
                    count:
                      template.asset_summary?.digital_employee_count ??
                      template.roles.length,
                  })
                : `${t("projectTerminology.create.teamOwner", {
                    count: selectedAgents.length,
                  })}${leader?.name || t("projectCreate.boundary.unspecified")}`}
            </dd>
          </div>
          <div>
            <dt>{t("projectCreate.boundary.capabilities")}</dt>
            <dd>
              {template?.snapshot_backed
                ? t("projectCreate.boundary.templateCapabilities")
                : t("projectCreate.boundary.capabilityCount", {
                    count: capabilityCount,
                  })}
            </dd>
          </div>
          <div>
            <dt>{t("projectCreate.boundary.visibility")}</dt>
            <dd>
              {draft.visibility === "private"
                ? t("projectCreate.boundary.private")
                : t("projectCreate.boundary.sharedCount", {
                    count: draft.shareTargets.length,
                  })}
            </dd>
          </div>
          <div className="pm-review-planning">
            <dt>{t("projectCreate.boundary.next")}</dt>
            <dd>{t("projectTerminology.create.nextStep")}</dd>
          </div>
        </dl>
      </section>
      <div className="pm-policy-callout pm-policy-success">
        <IconMessageCircle size={18} />
        <div>
          <strong>{t("projectCreate.boundary.planningTitle")}</strong>
          <p>{t("projectTerminology.create.workspaceNext")}</p>
        </div>
      </div>
    </div>
  );
}
