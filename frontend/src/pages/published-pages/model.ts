import type { PublishedPageActor } from "../../components/PublishedPageAttribution";

export type AccessMode = "public" | "authenticated" | "restricted";
export type AccessUser = {
  id: string;
  display_name: string;
  email?: string;
  status: "pending" | "approved" | "rejected";
  requested_at?: string;
};
export type Visitor = {
  id: string;
  display_name: string;
  email?: string;
  visitor_type: "authenticated" | "anonymous";
  view_count: number;
  first_viewed_at?: string;
  last_viewed_at?: string;
};
export type PublishedPage = {
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
export type PublishedPageDetail = Omit<PublishedPage, "visitors"> & {
  access_users: AccessUser[];
};
export type Paged<T> = {
  items: T[];
  total: number;
  page: number;
  page_size: number;
};

export const DEFAULT_PAGE_SIZE = 20;
export const MAX_BULK_PAGE_SELECTION = 100;
export const PAGE_SIZE_OPTIONS = [10, 20, 50, 100] as const;
export const VISITOR_PAGE_SIZE = 20;
export const ACCESS_MODE_OPTIONS = [
  { value: "public", label: "公开" },
  { value: "authenticated", label: "仅登录" },
  { value: "restricted", label: "指定人员" },
] as const;
export const modeLabels: Record<AccessMode, string> = {
  public: "公开",
  authenticated: "仅登录",
  restricted: "指定人员",
};

export function formatTime(value?: string) {
  return value ? new Date(value).toLocaleString() : "—";
}

export function readPageSize(value: string | null) {
  const parsed = Number(value);
  return PAGE_SIZE_OPTIONS.includes(
    parsed as (typeof PAGE_SIZE_OPTIONS)[number],
  )
    ? parsed
    : DEFAULT_PAGE_SIZE;
}

export function readPageNumber(value: string | null) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.max(1, Math.trunc(parsed)) : 1;
}
