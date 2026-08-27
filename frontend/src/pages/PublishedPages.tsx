import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import {
  IconCopy,
  IconExternalLink,
  IconEye,
  IconHistory,
  IconLock,
  IconSettings,
  IconTrash,
  IconUsers,
  IconWorld,
  IconX,
} from "@tabler/icons-react";

import OrgMemberAccessPicker, {
  type AgentAccessUser,
} from "../components/OrgMemberAccessPicker";
import Pagination from "../components/Pagination";
import ConfirmModal from "../components/ConfirmModal";
import PublishedPageAttribution, {
  type PublishedPageActor,
} from "../components/PublishedPageAttribution";
import PublishedPageFilters, {
  type PublishedPageAgentOption,
} from "../components/PublishedPageFilters";
import SelectDropdown from "../components/SelectDropdown";
import { useToast } from "../components/Toast/ToastProvider";
import Button from "../components/ui/Button";
import Checkbox from "../components/ui/Checkbox";
import { fetchJson } from "../services/api";
import { copyToClipboard } from "../utils/clipboard";
import "./PublishedPages.css";

type AccessMode = "public" | "authenticated" | "restricted";
type AccessUser = {
  id: string;
  display_name: string;
  email?: string;
  status: "pending" | "approved" | "rejected";
  requested_at?: string;
};
type Visitor = {
  id: string;
  display_name: string;
  email?: string;
  visitor_type: "authenticated" | "anonymous";
  view_count: number;
  first_viewed_at?: string;
  last_viewed_at?: string;
};
type PublishedPage = {
  id: string;
  short_id: string;
  agent_id: string;
  title: string;
  source_path: string;
  agent_name: string;
  access_mode: AccessMode;
  view_count: number;
  url: string;
  created_at?: string;
  created_by?: PublishedPageActor | null;
  last_published_by?: PublishedPageActor | null;
  last_published_at?: string | null;
  visitor_count: number;
  pending_request_count: number;
  visitors: Visitor[];
};
type PublishedPageDetail = Omit<PublishedPage, "visitors"> & {
  access_users: AccessUser[];
};
type Paged<T> = { items: T[]; total: number; page: number; page_size: number };

const DEFAULT_PAGE_SIZE = 20;
const MAX_BULK_PAGE_SELECTION = 100;
const PAGE_SIZE_OPTIONS = [10, 20, 50, 100] as const;
const VISITOR_PAGE_SIZE = 20;
const ACCESS_MODE_OPTIONS = [
  { value: "public", label: "公开" },
  { value: "authenticated", label: "仅登录" },
  { value: "restricted", label: "指定人员" },
] as const;
const modeLabels: Record<AccessMode, string> = {
  public: "公开",
  authenticated: "仅登录",
  restricted: "指定人员",
};

function formatTime(value?: string) {
  return value ? new Date(value).toLocaleString() : "—";
}

function absolutePageUrl(url: string) {
  try {
    return new URL(url, window.location.origin).toString();
  } catch {
    return url;
  }
}

function readPageSize(value: string | null) {
  const parsed = Number(value);
  return PAGE_SIZE_OPTIONS.includes(
    parsed as (typeof PAGE_SIZE_OPTIONS)[number],
  )
    ? parsed
    : DEFAULT_PAGE_SIZE;
}

function readPageNumber(value: string | null) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.max(1, Math.trunc(parsed)) : 1;
}

export default function PublishedPages() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedPageId = searchParams.get("page");
  const agentId = searchParams.get("agent_id") || "";
  const selectedAgentIds = Array.from(
    new Set([
      ...searchParams.getAll("agent_ids"),
      ...(agentId ? [agentId] : []),
    ]),
  ).sort();
  const selectedAgentIdsKey = selectedAgentIds.join(",");
  const searchQuery = searchParams.get("q") || "";
  const pageNo = readPageNumber(searchParams.get("page_no"));
  const pageSize = readPageSize(searchParams.get("page_size"));
  const [activeTab, setActiveTab] = useState<"permissions" | "visitors">(
    "permissions",
  );
  const [visitorPage, setVisitorPage] = useState(1);
  const [mode, setMode] = useState<AccessMode>("public");
  const [selectedPeople, setSelectedPeople] = useState<AgentAccessUser[]>([]);
  const [showMemberPicker, setShowMemberPicker] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [searchDraft, setSearchDraft] = useState(searchQuery);
  const [selectedPages, setSelectedPages] = useState<
    Record<string, PublishedPage>
  >({});
  const selectedPagesRef = useRef<Record<string, PublishedPage>>({});
  const [bulkMode, setBulkMode] = useState<AccessMode>("authenticated");
  const [bulkPeople, setBulkPeople] = useState<AgentAccessUser[]>([]);
  const [showBulkMemberPicker, setShowBulkMemberPicker] = useState(false);
  const [showBulkConfirm, setShowBulkConfirm] = useState(false);
  const [bulkSaving, setBulkSaving] = useState(false);

  const listParams = new URLSearchParams({
    page: String(pageNo),
    page_size: String(pageSize),
  });
  selectedAgentIds.forEach((id) => listParams.append("agent_ids", id));
  if (searchQuery) listParams.set("q", searchQuery);
  const { data: agentOptions = [] } = useQuery({
    queryKey: ["published-pages", "agent-options"],
    queryFn: () =>
      fetchJson<PublishedPageAgentOption[]>("/pages/agent-options"),
  });
  const { data: pageData, isLoading } = useQuery({
    queryKey: [
      "published-pages",
      "list",
      pageNo,
      pageSize,
      selectedAgentIdsKey,
      searchQuery,
    ],
    queryFn: () => fetchJson<Paged<PublishedPage>>(`/pages/mine?${listParams}`),
  });
  const pages = pageData?.items || [];
  const { data: selected } = useQuery({
    queryKey: ["published-pages", "detail", selectedPageId],
    queryFn: () =>
      fetchJson<PublishedPageDetail>(`/pages/${selectedPageId}/detail`),
    enabled: Boolean(selectedPageId),
  });
  const { data: visitorData, isLoading: visitorsLoading } = useQuery({
    queryKey: ["published-pages", "visitors", selectedPageId, visitorPage],
    queryFn: () =>
      fetchJson<Paged<Visitor>>(
        `/pages/${selectedPageId}/visitors?page=${visitorPage}&page_size=${VISITOR_PAGE_SIZE}`,
      ),
    enabled: Boolean(selectedPageId) && activeTab === "visitors",
  });

  useEffect(() => {
    setSearchDraft(searchQuery);
  }, [searchQuery]);

  useEffect(() => {
    selectedPagesRef.current = {};
    setSelectedPages({});
    setBulkPeople([]);
    setShowBulkConfirm(false);
  }, [selectedAgentIdsKey, searchQuery]);

  useEffect(() => {
    if (!selected) return;
    setMode(selected.access_mode);
    setSelectedPeople(
      selected.access_users
        .filter((user) => user.status === "approved")
        .map((user) => ({
          id: user.id,
          name: user.display_name,
          email: user.email,
          access_level: "use",
        })),
    );
    setActiveTab("permissions");
    setVisitorPage(1);
    setShowDeleteConfirm(false);
  }, [selectedPageId, selected]);

  const originalApprovedIds = useMemo(
    () =>
      selected?.access_users
        .filter((user) => user.status === "approved")
        .map((user) => user.id)
        .sort() || [],
    [selected],
  );
  const selectedIds = useMemo(
    () => selectedPeople.map((user) => user.id).sort(),
    [selectedPeople],
  );
  const dirty =
    Boolean(selected) &&
    (mode !== selected?.access_mode ||
      JSON.stringify(selectedIds) !== JSON.stringify(originalApprovedIds));

  const updateSearch = (updates: Record<string, string | null>) => {
    const next = new URLSearchParams(searchParams);
    next.delete("token");
    Object.entries(updates).forEach(([key, value]) =>
      value === null ? next.delete(key) : next.set(key, value),
    );
    setSearchParams(next);
  };
  const updateAgentFilter = (ids: string[]) => {
    const next = new URLSearchParams(searchParams);
    next.delete("token");
    next.delete("agent_id");
    next.delete("agent_ids");
    ids
      .slice()
      .sort()
      .forEach((id) => next.append("agent_ids", id));
    next.delete("page_no");
    next.delete("page");
    setSearchParams(next);
  };
  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ["published-pages"] });
  };
  const copyPageUrl = async (url: string) => {
    if (await copyToClipboard(absolutePageUrl(url)))
      toast.success("发布地址已复制");
    else toast.error("复制失败，请手动复制地址");
  };
  const searchPages = () => {
    const normalized = searchDraft.trim();
    updateSearch({ q: normalized || null, page_no: null, page: null });
  };

  const totalPages = Math.max(1, Math.ceil((pageData?.total || 0) / pageSize));
  const goToPage = (nextPage: number) => {
    const clamped = Math.min(totalPages, Math.max(1, Math.trunc(nextPage)));
    updateSearch({ page_no: String(clamped), page: null });
  };
  useEffect(() => {
    if (pageData && pageNo > totalPages) goToPage(totalPages);
  }, [pageData, pageNo, totalPages]);

  const selectedPageList = Object.values(selectedPages);
  const selectedPageIds = Object.keys(selectedPages);
  const allCurrentPageSelected =
    pages.length > 0 && pages.every((page) => Boolean(selectedPages[page.id]));
  const togglePageSelection = (page: PublishedPage) => {
    const current = selectedPagesRef.current;
    if (
      !current[page.id] &&
      Object.keys(current).length >= MAX_BULK_PAGE_SELECTION
    ) {
      toast.error(`单次最多选择 ${MAX_BULK_PAGE_SELECTION} 个页面`);
      return;
    }
    const next = { ...current };
    if (next[page.id]) delete next[page.id];
    else next[page.id] = page;
    selectedPagesRef.current = next;
    setSelectedPages(next);
  };
  const toggleCurrentPage = () => {
    const current = selectedPagesRef.current;
    const currentPageSelected =
      pages.length > 0 && pages.every((page) => Boolean(current[page.id]));
    const unselectedOnPage = pages.filter((page) => !current[page.id]);
    if (
      !currentPageSelected &&
      Object.keys(current).length + unselectedOnPage.length >
        MAX_BULK_PAGE_SELECTION
    ) {
      toast.error(
        `单次最多选择 ${MAX_BULK_PAGE_SELECTION} 个页面，已按上限选择`,
      );
    }
    const next = { ...current };
    if (currentPageSelected)
      pages.forEach((page) => {
        delete next[page.id];
      });
    else {
      let remaining = MAX_BULK_PAGE_SELECTION - Object.keys(next).length;
      pages.forEach((page) => {
        if (!next[page.id] && remaining > 0) {
          next[page.id] = page;
          remaining -= 1;
        }
      });
    }
    selectedPagesRef.current = next;
    setSelectedPages(next);
  };
  const clearBulkSelection = () => {
    selectedPagesRef.current = {};
    setSelectedPages({});
    setBulkPeople([]);
    setShowBulkMemberPicker(false);
    setShowBulkConfirm(false);
  };

  useEffect(() => {
    if (selectedPageIds.length === 0) {
      setBulkPeople([]);
      setShowBulkMemberPicker(false);
      setShowBulkConfirm(false);
    }
  }, [selectedPageIds.length]);
  const applyBulkAccess = async () => {
    if (selectedPageIds.length === 0 || bulkSaving) return;
    setBulkSaving(true);
    try {
      await fetchJson("/pages/batch/access", {
        method: "PUT",
        body: JSON.stringify({
          page_ids: selectedPageIds,
          access_mode: bulkMode,
          allowed_user_ids:
            bulkMode === "restricted" ? bulkPeople.map((user) => user.id) : [],
        }),
      });
      setShowBulkConfirm(false);
      clearBulkSelection();
      await refresh();
      toast.success(`已更新 ${selectedPageIds.length} 个页面的访问权限`);
    } catch (error: any) {
      toast.error("批量权限修改失败", {
        details: error?.message || String(error),
      });
    } finally {
      setBulkSaving(false);
    }
  };

  const save = async () => {
    if (!selected || !dirty) return;
    setSaving(true);
    try {
      await fetchJson(`/pages/${selected.id}/access`, {
        method: "PUT",
        body: JSON.stringify({
          access_mode: mode,
          allowed_user_ids: mode === "restricted" ? selectedIds : [],
        }),
      });
      await refresh();
      toast.success("权限设置已保存");
    } catch (error: any) {
      toast.error("权限设置保存失败", {
        details: error?.message || String(error),
      });
    } finally {
      setSaving(false);
    }
  };

  const resolveRequest = async (
    user: AccessUser,
    status: "approved" | "rejected",
  ) => {
    if (!selected) return;
    try {
      await fetchJson(`/pages/${selected.id}/requests/${user.id}`, {
        method: "PUT",
        body: JSON.stringify({ status }),
      });
      await refresh();
      toast.success(
        status === "approved"
          ? `已允许 ${user.display_name} 访问`
          : `已拒绝 ${user.display_name} 的访问申请`,
      );
    } catch (error: any) {
      toast.error("申请处理失败", { details: error?.message || String(error) });
    }
  };

  const deletePage = async () => {
    if (!selected || deleting) return;
    setDeleting(true);
    try {
      await fetchJson(`/pages/${selected.id}`, { method: "DELETE" });
      setShowDeleteConfirm(false);
      updateSearch({ page: null, page_no: null });
      await refresh();
      toast.success("发布地址已删除");
    } catch (error: any) {
      toast.error("删除发布地址失败", {
        details: error?.message || String(error),
      });
    } finally {
      setDeleting(false);
    }
  };

  useEffect(() => {
    const lastPage = Math.max(
      1,
      Math.ceil((visitorData?.total || 0) / VISITOR_PAGE_SIZE),
    );
    if (visitorPage > lastPage) setVisitorPage(lastPage);
  }, [visitorData?.total, visitorPage]);
  const pendingUsers =
    selected?.access_users.filter((user) => user.status === "pending") || [];
  const hasAppliedFilters = Boolean(searchQuery || selectedAgentIds.length);
  const hasResettableFilters = Boolean(
    searchDraft.trim() || selectedAgentIds.length,
  );

  return (
    <div className="published-pages">
      <div style={{ marginBottom: 24 }}>
        <h1 style={{ fontSize: 24, margin: 0 }}>发布管理</h1>
        <p style={{ color: "var(--text-tertiary)", fontSize: 13 }}>
          管理数字员工发布的网页、访问权限和访问记录
        </p>
      </div>

      <PublishedPageFilters
        agents={agentOptions}
        selectedAgentIds={selectedAgentIds}
        query={searchDraft}
        hasActiveFilters={hasResettableFilters}
        onAgentChange={updateAgentFilter}
        onQueryChange={setSearchDraft}
        onSearch={searchPages}
        onReset={() => {
          setSearchDraft("");
          updateSearch({
            q: null,
            agent_id: null,
            agent_ids: null,
            page_no: null,
            page: null,
          });
        }}
      />

      {isLoading ? (
        <p>加载中…</p>
      ) : pages.length === 0 ? (
        <div
          style={{
            padding: 40,
            textAlign: "center",
            border: "1px solid var(--border-subtle)",
            borderRadius: 10,
            color: "var(--text-tertiary)",
          }}
        >
          {hasAppliedFilters
            ? "没有匹配的发布页面"
            : "暂无已发布内容，可让数字员工使用 Publish Page 工具发布网页"}
        </div>
      ) : (
        <>
          <div
            className="published-pages-bulk"
            role="toolbar"
            aria-label="批量修改页面权限"
          >
            <label className="published-pages-bulk__select-all">
              <Checkbox
                checked={allCurrentPageSelected}
                onChange={toggleCurrentPage}
                aria-label={
                  allCurrentPageSelected ? "取消选择本页" : "选择本页"
                }
              />
              <span>
                {selectedPageIds.length > 0
                  ? `已选 ${selectedPageIds.length} 个页面`
                  : "选择本页"}
              </span>
            </label>
            {selectedPageIds.length > 0 && (
              <>
                <SelectDropdown
                  value={bulkMode}
                  options={ACCESS_MODE_OPTIONS}
                  onChange={setBulkMode}
                  ariaLabel="批量访问权限"
                  className="published-pages-bulk__mode"
                />
                {bulkMode === "restricted" && (
                  <Button
                    type="button"
                    variant="secondary"
                    onClick={() => setShowBulkMemberPicker(true)}
                  >
                    {bulkPeople.length > 0
                      ? `已选 ${bulkPeople.length} 人`
                      : "选择可访问人员"}
                  </Button>
                )}
                <Button
                  type="button"
                  variant="primary"
                  onClick={() => setShowBulkConfirm(true)}
                >
                  批量修改
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  onClick={clearBulkSelection}
                >
                  取消选择
                </Button>
              </>
            )}
          </div>
          <div
            style={{
              border: "1px solid var(--border-subtle)",
              borderRadius: 10,
              overflow: "hidden",
            }}
          >
            {pages.map((publishedPage, index) => (
              <div
                key={publishedPage.id}
                role="button"
                tabIndex={0}
                onClick={() => updateSearch({ page: publishedPage.id })}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ")
                    updateSearch({ page: publishedPage.id });
                }}
                className="published-pages-row"
                style={{
                  borderTop: index ? "1px solid var(--border-subtle)" : 0,
                  background: "var(--bg-primary)",
                  color: "inherit",
                  padding: "10px 14px",
                  cursor: "pointer",
                  display: "grid",
                  alignItems: "center",
                  gap: 14,
                }}
              >
                <div className="published-pages-row__identity">
                  <span
                    onClick={(event) => event.stopPropagation()}
                    onKeyDown={(event) => event.stopPropagation()}
                  >
                    <Checkbox
                      checked={Boolean(selectedPages[publishedPage.id])}
                      onChange={() => togglePageSelection(publishedPage)}
                      aria-label={`选择页面 ${publishedPage.title || publishedPage.source_path}`}
                    />
                  </span>
                  <div style={{ minWidth: 0 }}>
                    <div
                      style={{
                        fontWeight: 600,
                        fontSize: 14,
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {publishedPage.title || publishedPage.source_path}
                    </div>
                    <div
                      style={{
                        fontSize: 11,
                        color: "var(--text-tertiary)",
                        marginTop: 2,
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {publishedPage.agent_name} · {publishedPage.source_path}
                    </div>
                    <PublishedPageAttribution
                      createdBy={publishedPage.created_by}
                      createdAt={publishedPage.created_at}
                      lastPublishedBy={publishedPage.last_published_by}
                      lastPublishedAt={publishedPage.last_published_at}
                    />
                  </div>
                </div>
                <div
                  className="published-pages-row__url"
                  style={{
                    minWidth: 0,
                    display: "flex",
                    alignItems: "center",
                    gap: 6,
                  }}
                >
                  <a
                    href={absolutePageUrl(publishedPage.url)}
                    target="_blank"
                    rel="noreferrer"
                    onClick={(event) => event.stopPropagation()}
                    title={absolutePageUrl(publishedPage.url)}
                    style={{
                      minWidth: 0,
                      flex: 1,
                      fontSize: 11,
                      color: "var(--text-secondary)",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                      textDecoration: "none",
                    }}
                  >
                    {absolutePageUrl(publishedPage.url)}
                  </a>
                  <button
                    type="button"
                    aria-label="复制发布地址"
                    title="复制发布地址"
                    onClick={(event) => {
                      event.stopPropagation();
                      void copyPageUrl(publishedPage.url);
                    }}
                    style={{
                      border: 0,
                      background: "transparent",
                      color: "var(--text-tertiary)",
                      padding: 3,
                      cursor: "pointer",
                      display: "inline-flex",
                    }}
                  >
                    <IconCopy size={14} />
                  </button>
                </div>
                <span
                  className="published-pages-row__mode"
                  style={{ fontSize: 12 }}
                >
                  {modeLabels[publishedPage.access_mode]}
                </span>
                <span
                  className="published-pages-row__metrics"
                  style={{
                    fontSize: 12,
                    color: "var(--text-secondary)",
                    display: "flex",
                    gap: 12,
                    alignItems: "center",
                  }}
                >
                  <span
                    style={{
                      display: "inline-flex",
                      gap: 4,
                      alignItems: "center",
                    }}
                  >
                    <IconEye size={14} /> {publishedPage.view_count}
                  </span>
                  <span>{publishedPage.visitor_count} 人</span>
                  {publishedPage.pending_request_count > 0 && (
                    <span style={{ color: "var(--warning)" }}>
                      待处理 {publishedPage.pending_request_count}
                    </span>
                  )}
                </span>
              </div>
            ))}
          </div>
          <Pagination
            page={pageNo}
            pageSize={pageSize}
            total={pageData?.total || 0}
            onPageChange={goToPage}
            onPageSizeChange={(nextPageSize) =>
              updateSearch({
                page_size: String(nextPageSize),
                page_no: "1",
                page: null,
              })
            }
            pageSizeOptions={PAGE_SIZE_OPTIONS}
          />
        </>
      )}

      {selected && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 2000,
            background: "rgba(0,0,0,.28)",
          }}
          onClick={() => updateSearch({ page: null })}
        >
          <aside
            style={{
              position: "absolute",
              right: 0,
              top: 0,
              bottom: 0,
              width: "min(560px, 94vw)",
              background: "var(--bg-primary)",
              padding: 24,
              overflowY: "auto",
              boxShadow: "-12px 0 40px rgba(0,0,0,.16)",
            }}
            onClick={(event) => event.stopPropagation()}
          >
            <button
              aria-label="关闭"
              onClick={() => updateSearch({ page: null })}
              style={{
                float: "right",
                border: 0,
                background: "none",
                color: "inherit",
                cursor: "pointer",
              }}
            >
              <IconX size={20} />
            </button>
            <h2 style={{ fontSize: 19, margin: "0 0 10px" }}>
              {selected.title || selected.source_path}
            </h2>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                padding: "8px 10px",
                border: "1px solid var(--border-subtle)",
                borderRadius: 7,
                background: "var(--bg-secondary)",
              }}
            >
              <a
                href={absolutePageUrl(selected.url)}
                target="_blank"
                rel="noreferrer"
                title={absolutePageUrl(selected.url)}
                style={{
                  minWidth: 0,
                  flex: 1,
                  fontSize: 12,
                  color: "var(--text-secondary)",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                  textDecoration: "none",
                }}
              >
                {absolutePageUrl(selected.url)}
              </a>
              <button
                type="button"
                aria-label="复制发布地址"
                title="复制发布地址"
                onClick={() => void copyPageUrl(selected.url)}
                style={{
                  border: 0,
                  background: "transparent",
                  color: "var(--text-secondary)",
                  padding: 3,
                  cursor: "pointer",
                  display: "inline-flex",
                }}
              >
                <IconCopy size={15} />
              </button>
              <a
                href={absolutePageUrl(selected.url)}
                target="_blank"
                rel="noreferrer"
                aria-label="打开发布页面"
                title="打开发布页面"
                style={{
                  color: "var(--text-secondary)",
                  display: "inline-flex",
                }}
              >
                <IconExternalLink size={15} />
              </a>
            </div>
            <PublishedPageAttribution
              createdBy={selected.created_by}
              createdAt={selected.created_at}
              lastPublishedBy={selected.last_published_by}
              lastPublishedAt={selected.last_published_at}
              variant="detail"
            />
            <div
              style={{
                display: "flex",
                justifyContent: "flex-end",
                marginTop: 10,
              }}
            >
              <button
                type="button"
                className="btn btn-danger btn-sm"
                onClick={() => setShowDeleteConfirm(true)}
              >
                <IconTrash size={14} /> 删除发布地址
              </button>
            </div>

            <div
              style={{
                display: "flex",
                gap: 4,
                borderBottom: "1px solid var(--border-subtle)",
                marginTop: 24,
              }}
            >
              {(
                [
                  ["permissions", "权限设置", IconSettings],
                  [
                    "visitors",
                    `访问记录 ${selected.visitor_count}`,
                    IconHistory,
                  ],
                ] as const
              ).map(([value, label, Icon]) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => setActiveTab(value)}
                  style={{
                    border: 0,
                    borderBottom:
                      activeTab === value
                        ? "2px solid var(--accent-primary)"
                        : "2px solid transparent",
                    background: "none",
                    color:
                      activeTab === value
                        ? "var(--text-primary)"
                        : "var(--text-secondary)",
                    padding: "10px 12px",
                    cursor: "pointer",
                    display: "inline-flex",
                    gap: 6,
                    alignItems: "center",
                    fontWeight: 600,
                  }}
                >
                  <Icon size={15} /> {label}
                </button>
              ))}
            </div>

            {activeTab === "permissions" ? (
              <>
                <div style={{ display: "grid", gap: 8, marginTop: 18 }}>
                  {(
                    [
                      ["public", "公开", "任何人无需登录即可访问", IconWorld],
                      [
                        "authenticated",
                        "仅登录",
                        "公司内已登录用户可访问",
                        IconLock,
                      ],
                      [
                        "restricted",
                        "指定人员",
                        "仅发布者和指定人员可访问",
                        IconUsers,
                      ],
                    ] as const
                  ).map(([value, title, description, Icon]) => (
                    <label
                      key={value}
                      style={{
                        padding: 12,
                        border: `1px solid ${mode === value ? "var(--accent-primary)" : "var(--border-subtle)"}`,
                        borderRadius: 8,
                        display: "flex",
                        gap: 10,
                        cursor: "pointer",
                      }}
                    >
                      <input
                        type="radio"
                        checked={mode === value}
                        onChange={() => setMode(value)}
                      />
                      <Icon size={18} />
                      <span>
                        <strong style={{ display: "block", fontSize: 13 }}>
                          {title}
                        </strong>
                        <span
                          style={{
                            fontSize: 12,
                            color: "var(--text-tertiary)",
                          }}
                        >
                          {description}
                        </span>
                      </span>
                    </label>
                  ))}
                </div>

                {mode === "restricted" && (
                  <div
                    style={{
                      marginTop: 16,
                      padding: 14,
                      border: "1px solid var(--border-subtle)",
                      borderRadius: 8,
                      display: "flex",
                      alignItems: "center",
                      gap: 12,
                    }}
                  >
                    <div style={{ flex: 1 }}>
                      <strong style={{ fontSize: 13 }}>
                        已选择 {selectedPeople.length} 人
                      </strong>
                      <div
                        style={{
                          color: "var(--text-tertiary)",
                          fontSize: 11,
                          marginTop: 3,
                        }}
                      >
                        {selectedPeople
                          .slice(0, 3)
                          .map((user) => user.name)
                          .join("、") || "尚未选择人员"}
                        {selectedPeople.length > 3
                          ? ` 等 ${selectedPeople.length} 人`
                          : ""}
                      </div>
                    </div>
                    <button
                      className="btn btn-secondary btn-sm"
                      onClick={() => setShowMemberPicker(true)}
                    >
                      选择可访问人员
                    </button>
                  </div>
                )}

                {pendingUsers.length > 0 && (
                  <section style={{ marginTop: 24 }}>
                    <h3 style={{ fontSize: 14 }}>待处理申请</h3>
                    {pendingUsers.map((user) => (
                      <div
                        key={user.id}
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 8,
                          padding: "10px 0",
                          borderBottom: "1px solid var(--border-subtle)",
                        }}
                      >
                        <span style={{ flex: 1, fontSize: 13 }}>
                          {user.display_name}
                          {user.email ? ` · ${user.email}` : ""}
                        </span>
                        <button
                          className="btn"
                          onClick={() => void resolveRequest(user, "rejected")}
                        >
                          拒绝
                        </button>
                        <button
                          className="btn btn-primary"
                          onClick={() => void resolveRequest(user, "approved")}
                        >
                          允许
                        </button>
                      </div>
                    ))}
                  </section>
                )}

                <div
                  style={{
                    position: "sticky",
                    bottom: -24,
                    background: "var(--bg-primary)",
                    padding: "18px 0 24px",
                    marginTop: 20,
                    textAlign: "right",
                  }}
                >
                  <button
                    className="btn btn-primary"
                    disabled={saving || !dirty}
                    onClick={() => void save()}
                  >
                    {saving ? "保存中…" : dirty ? "保存权限" : "已保存"}
                  </button>
                </div>
              </>
            ) : (
              <section style={{ marginTop: 18 }}>
                {visitorsLoading ? (
                  <p>加载中…</p>
                ) : !visitorData?.items.length ? (
                  <p style={{ color: "var(--text-tertiary)", fontSize: 13 }}>
                    暂无访问记录
                  </p>
                ) : (
                  visitorData.items.map((visitor) => (
                    <div
                      key={visitor.id}
                      style={{
                        padding: "10px 0",
                        borderBottom: "1px solid var(--border-subtle)",
                        display: "grid",
                        gridTemplateColumns: "minmax(0, 1fr) auto",
                        gap: 10,
                      }}
                    >
                      <div style={{ minWidth: 0 }}>
                        <div
                          style={{
                            display: "flex",
                            alignItems: "baseline",
                            gap: 8,
                            minWidth: 0,
                          }}
                        >
                          <strong
                            title={visitor.display_name}
                            style={{
                              minWidth: 0,
                              maxWidth: visitor.email ? "42%" : "75%",
                              fontSize: 13,
                              overflow: "hidden",
                              textOverflow: "ellipsis",
                              whiteSpace: "nowrap",
                            }}
                          >
                            {visitor.display_name}
                          </strong>
                          {visitor.email && (
                            <span
                              title={visitor.email}
                              style={{
                                minWidth: 0,
                                color: "var(--text-tertiary)",
                                fontSize: 11,
                                overflow: "hidden",
                                textOverflow: "ellipsis",
                                whiteSpace: "nowrap",
                              }}
                            >
                              {visitor.email}
                            </span>
                          )}
                          {visitor.visitor_type === "anonymous" && (
                            <span
                              style={{
                                color: "var(--text-tertiary)",
                                fontSize: 10,
                                flexShrink: 0,
                              }}
                            >
                              未登录
                            </span>
                          )}
                        </div>
                        <div
                          style={{
                            color: "var(--text-tertiary)",
                            fontSize: 11,
                            marginTop: 4,
                          }}
                        >
                          首次：{formatTime(visitor.first_viewed_at)} · 最近：
                          {formatTime(visitor.last_viewed_at)}
                        </div>
                      </div>
                      <span
                        style={{ fontSize: 12, color: "var(--text-secondary)" }}
                      >
                        {visitor.view_count} 次
                      </span>
                    </div>
                  ))
                )}
                {(visitorData?.total || 0) > VISITOR_PAGE_SIZE && (
                  <Pagination
                    page={visitorPage}
                    pageSize={VISITOR_PAGE_SIZE}
                    total={visitorData?.total || 0}
                    onPageChange={setVisitorPage}
                  />
                )}
              </section>
            )}

            <OrgMemberAccessPicker
              open={showMemberPicker}
              agentId={selected.agent_id}
              directoryBaseUrl={`/pages/${selected.id}/directory`}
              membersOnly
              users={selectedPeople}
              departments={[]}
              onClose={() => setShowMemberPicker(false)}
              onSave={async (users) => setSelectedPeople(users)}
            />
          </aside>
        </div>
      )}
      <ConfirmModal
        open={showBulkConfirm && selectedPageIds.length > 0}
        title="批量修改访问权限"
        message={`将 ${selectedPageIds.length} 个页面统一设为“${modeLabels[bulkMode]}”${bulkMode === "restricted" ? `，允许 ${bulkPeople.length} 名已选人员访问` : ""}。确认继续吗？`}
        confirmLabel={bulkSaving ? "修改中…" : "确认修改"}
        cancelLabel="取消"
        onConfirm={() => void applyBulkAccess()}
        onCancel={() => {
          if (!bulkSaving) setShowBulkConfirm(false);
        }}
      />
      {showBulkMemberPicker && selectedPageList[0] && (
        <OrgMemberAccessPicker
          open
          agentId={selectedPageList[0].agent_id}
          directoryBaseUrl={`/pages/${selectedPageList[0].id}/directory`}
          membersOnly
          users={bulkPeople}
          departments={[]}
          onClose={() => setShowBulkMemberPicker(false)}
          onSave={async (users) => setBulkPeople(users)}
        />
      )}
      <ConfirmModal
        open={showDeleteConfirm && Boolean(selected)}
        title="删除发布地址"
        message={`删除后，“${selected?.title || selected?.source_path || "此页面"}”的发布地址将立即失效，权限和访问记录也会一并删除。数字员工工作区中的源文件会保留。`}
        confirmLabel={deleting ? "删除中…" : "确认删除"}
        cancelLabel="取消"
        danger
        onConfirm={() => void deletePage()}
        onCancel={() => {
          if (!deleting) setShowDeleteConfirm(false);
        }}
      />
    </div>
  );
}
