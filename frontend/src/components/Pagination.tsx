import { useEffect, useMemo, useState } from "react";
import { IconChevronLeft, IconChevronRight } from "@tabler/icons-react";
import { useTranslation } from "react-i18next";

import SelectDropdown from "./SelectDropdown";
import Button from "./ui/Button";
import "./Pagination.css";

export type PaginationProps = {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
  onPageSizeChange?: (pageSize: number) => void;
  pageSizeOptions?: readonly number[];
  className?: string;
  ariaLabel?: string;
  showJump?: boolean;
  showRange?: boolean;
  compact?: boolean;
};

type PaginationItem = number | "gap-left" | "gap-right";

function buildPaginationItems(
  currentPage: number,
  totalPages: number,
): PaginationItem[] {
  if (totalPages <= 7) {
    return Array.from({ length: totalPages }, (_, index) => index + 1);
  }

  const items: PaginationItem[] = [1];
  const start = Math.max(2, currentPage - 1);
  const end = Math.min(totalPages - 1, currentPage + 1);
  if (start > 2) items.push("gap-left");
  for (let page = start; page <= end; page += 1) items.push(page);
  if (end < totalPages - 1) items.push("gap-right");
  items.push(totalPages);
  return items;
}

export default function Pagination({
  page,
  pageSize,
  total,
  onPageChange,
  onPageSizeChange,
  pageSizeOptions = [10, 20, 50],
  className = "",
  ariaLabel,
  showJump = true,
  showRange = true,
  compact = false,
}: PaginationProps) {
  const { t } = useTranslation();
  const totalPages = Math.max(
    1,
    Math.ceil(Math.max(0, total) / Math.max(1, pageSize)),
  );
  const currentPage = Math.min(totalPages, Math.max(1, Math.trunc(page)));
  const [jumpPage, setJumpPage] = useState(String(currentPage));
  const paginationItems = useMemo(
    () => buildPaginationItems(currentPage, totalPages),
    [currentPage, totalPages],
  );
  const pageSizeSelectOptions = useMemo(
    () =>
      pageSizeOptions.map((option) => ({
        value: String(option),
        label: t("common.pagination.pageSize", { size: option }),
      })),
    [pageSizeOptions, t],
  );

  useEffect(() => {
    setJumpPage(String(currentPage));
  }, [currentPage]);

  if (total <= 0 || pageSize <= 0) return null;

  const start = (currentPage - 1) * pageSize + 1;
  const end = Math.min(total, currentPage * pageSize);
  const goToPage = (nextPage: number) => {
    onPageChange(Math.min(totalPages, Math.max(1, Math.trunc(nextPage))));
  };
  const commitJumpPage = () => {
    const parsed = Number(jumpPage);
    if (Number.isFinite(parsed)) goToPage(parsed);
    else setJumpPage(String(currentPage));
  };

  return (
    <nav
      className={`pagination${compact ? " pagination--compact" : ""}${className ? ` ${className}` : ""}`}
      aria-label={ariaLabel || t("common.pagination.label")}
    >
      {showRange && (
        <span className="pagination__total">
          {t("common.pagination.range", { start, end, total })}
        </span>
      )}
      <div className="pagination__pages">
        <Button
          type="button"
          variant="ghost"
          className="pagination__button"
          aria-label={t("common.pagination.previous")}
          disabled={currentPage <= 1}
          onClick={() => goToPage(currentPage - 1)}
        >
          <IconChevronLeft size={16} />
        </Button>
        {paginationItems.map((item) =>
          typeof item === "number" ? (
            <Button
              key={item}
              type="button"
              variant="ghost"
              className={`pagination__button${item === currentPage ? " is-active" : ""}`}
              aria-label={t("common.pagination.page", { page: item })}
              aria-current={item === currentPage ? "page" : undefined}
              onClick={() => goToPage(item)}
            >
              {item}
            </Button>
          ) : (
            <span key={item} className="pagination__gap" aria-hidden="true">
              …
            </span>
          ),
        )}
        <Button
          type="button"
          variant="ghost"
          className="pagination__button"
          aria-label={t("common.pagination.next")}
          disabled={currentPage >= totalPages}
          onClick={() => goToPage(currentPage + 1)}
        >
          <IconChevronRight size={16} />
        </Button>
      </div>
      {onPageSizeChange && (
        <SelectDropdown
          ariaLabel={t("common.pagination.pageSizeLabel")}
          value={String(pageSize)}
          options={pageSizeSelectOptions}
          onChange={(value) => onPageSizeChange(Number(value))}
          className="pagination__page-size"
        />
      )}
      {showJump && totalPages > 1 && (
        <label className="pagination__jump">
          <span>{t("common.pagination.jump")}</span>
          <input
            type="number"
            min={1}
            max={totalPages}
            value={jumpPage}
            aria-label={t("common.pagination.jumpLabel")}
            onChange={(event) => setJumpPage(event.target.value)}
            onBlur={commitJumpPage}
            onKeyDown={(event) => {
              if (event.key !== "Enter") return;
              event.preventDefault();
              commitJumpPage();
              event.currentTarget.blur();
            }}
          />
          <span>{t("common.pagination.pageUnit")}</span>
        </label>
      )}
    </nav>
  );
}
