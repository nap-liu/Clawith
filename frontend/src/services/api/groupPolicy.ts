import { request } from './core';

export type GroupTarget = {
    id: string;
    name: string;
    channel: string;
    available: boolean;
    rule_count: number;
    enabled_count: number;
    target_ref?: string;
};
export type GroupMember = { id: string; name: string };
export type GroupRule = {
    id: string;
    name: string;
    effect: 'allow' | 'deny';
    enabled: boolean;
    all_members: boolean;
    member_ids: string[];
    user_ids: string[];
};
export type GroupPolicy = { group: GroupTarget; revision: number; rules: GroupRule[]; members: GroupMember[]; users: GroupMember[] };
type Page<T> = { items: T[]; next_offset: number | null };
const base = (agentId: string) => `/agents/${agentId}/group-policy/groups`;
export const groupPolicyApi = {
    get: (agentId: string, groupId: string, targetRef = '') => request<GroupPolicy>(`${base(agentId)}/${groupId}?${new URLSearchParams({ target_ref: targetRef })}`),
    save: (agentId: string, groupId: string, rules: GroupRule[], revision: number, targetRef = '') =>
        request<GroupPolicy>(`${base(agentId)}/${groupId}`, {
            method: 'PUT', body: JSON.stringify({ rules, expected_revision: revision, target_ref: targetRef }),
        }),
    groups: (agentId: string, q: string, channel: string, offset = 0) =>
        request<Page<GroupTarget> & { total: number; channels: string[] }>(
            `${base(agentId)}?${new URLSearchParams({ q, channel, offset: String(offset) })}`,
        ),
};
