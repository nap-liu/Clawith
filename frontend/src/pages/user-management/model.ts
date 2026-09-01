export interface UserInfo {
  id: string;
  username: string;
  email: string;
  display_name: string;
  nickname?: string;
  role: string;
  is_active: boolean;
  quota_message_limit: number;
  quota_message_period: string;
  quota_messages_used: number;
  quota_max_agents: number;
  quota_agent_ttl_hours: number;
  agents_count: number;
  primary_mobile?: string;
  created_at?: string;
  source?: string;
}

export interface UserListResponse {
  items: UserInfo[];
  total: number;
  page: number;
  page_size: number;
}

export const PERIOD_OPTIONS = [
  { value: "permanent", label: "Permanent" },
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" },
  { value: "monthly", label: "Monthly" },
];

export const PAGE_SIZE = 15;
