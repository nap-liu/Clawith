import {
  createContext,
  useContext,
  useEffect,
  useMemo,
} from "react";
import Pagination from "../../../components/Pagination";
import type {
  ProjectWorkspaceUrlPatch as WorkspaceUrlPatch,
} from "../projectWorkspaceRouting";
import type {
  WorkspaceNavigationState,
} from "./types";

export const WorkspaceNavigationContext = createContext<WorkspaceNavigationState>({
  navigate: () => undefined,
  get: () => "",
  update: () => undefined,
});

export function useWorkspacePagination<T>(
  items: readonly T[],
  key: string,
  defaultPageSize = 10,
  pageSizeOptions: readonly number[] = [10, 20, 50],
  allowPageSizeChange = true,
  showJump = true,
  compact = false,
) {
  const { get, update } = useContext(WorkspaceNavigationContext);
  const requestedPage = Number.parseInt(get(`${key}Page`), 10);
  const requestedPageSize = Number.parseInt(get(`${key}PageSize`), 10);
  const pageSize = pageSizeOptions.includes(requestedPageSize)
    ? requestedPageSize
    : defaultPageSize;
  const totalPages = Math.max(1, Math.ceil(items.length / pageSize));
  const page = Math.min(
    Math.max(1, Number.isFinite(requestedPage) ? requestedPage : 1),
    totalPages,
  );

  useEffect(() => {
    if (Number.isFinite(requestedPage) && requestedPage > totalPages) {
      update(
        { [`${key}Page`]: totalPages > 1 ? String(totalPages) : undefined } as WorkspaceUrlPatch,
        { replace: true },
      );
    }
  }, [key, requestedPage, totalPages, update]);

  const pageItems = useMemo(
    () => items.slice((page - 1) * pageSize, page * pageSize),
    [items, page, pageSize],
  );

  return {
    pageItems,
    pagination:
      items.length > pageSize ? (
        <Pagination
          page={page}
          pageSize={pageSize}
          total={items.length}
          onPageChange={(nextPage) =>
            update({
              [`${key}Page`]: nextPage > 1 ? String(nextPage) : undefined,
            } as WorkspaceUrlPatch)
          }
          onPageSizeChange={
            allowPageSizeChange
              ? (nextPageSize) =>
                  update({
                    [`${key}Page`]: undefined,
                    [`${key}PageSize`]:
                      nextPageSize === defaultPageSize
                        ? undefined
                        : String(nextPageSize),
                  } as WorkspaceUrlPatch)
              : undefined
          }
          pageSizeOptions={pageSizeOptions}
          showJump={showJump}
          showRange={!compact}
          compact={compact}
        />
      ) : null,
  };
}
