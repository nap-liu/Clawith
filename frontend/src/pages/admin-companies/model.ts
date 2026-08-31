export type SortKey =
  | "name"
  | "sso_enabled"
  | "org_admin_email"
  | "user_count"
  | "agent_count"
  | "total_tokens"
  | "created_at"
  | "is_active";

export type SortDir = "asc" | "desc";

export const PAGE_SIZE = 15;
