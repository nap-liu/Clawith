/**
 * User Management — admin page to view and manage user quotas and roles.
 */
import { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { useAuthStore } from "../stores";
import LinearCopyButton from "../components/LinearCopyButton";
import Pagination from "../components/Pagination";
import { useDialog } from "../components/Dialog/DialogProvider";
import { IconEdit } from "@tabler/icons-react";

import { Pencil } from "lucide-react";
import {
  PAGE_SIZE,
  PERIOD_OPTIONS,
  type UserInfo,
  type UserListResponse,
} from "./user-management/model";
import InviteUsersModal from "./user-management/InviteUsersModal";
import UserListPanel from "./user-management/UserListPanel";

const API_PREFIX = "/api";

async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
  const token = localStorage.getItem("token");
  const res = await fetch(`${API_PREFIX}${url}`, {
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    ...options,
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export default function UserManagement() {
  const { t, i18n } = useTranslation();
  const isChinese = i18n.language?.startsWith("zh");
  const { user: currentUser, setUser } = useAuthStore();
  const dialog = useDialog();

  const [users, setUsers] = useState<UserInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [editingUserId, setEditingUserId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState({
    quota_message_limit: 50,
    quota_message_period: "permanent",
    quota_max_agents: 2,
    quota_agent_ttl_hours: 0,
  });
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState("");
  const [changingRoleUserId, setChangingRoleUserId] = useState<string | null>(
    null,
  );
  const [editingProfileUser, setEditingProfileUser] = useState<UserInfo | null>(
    null,
  );
  const [totalUsers, setTotalUsers] = useState(0);
  const [reloadToken, setReloadToken] = useState(0);
  const [hasLoadedUsersOnce, setHasLoadedUsersOnce] = useState(false);

  // Invite modal state
  const [showInviteModal, setShowInviteModal] = useState(false);
  const [inviteEmails, setInviteEmails] = useState("");
  const [inviting, setInviting] = useState(false);
  const [inviteResult, setInviteResult] = useState<{
    invited: number;
    message: string;
  } | null>(null);

  // Search, sort & pagination
  const [searchQuery, setSearchQuery] = useState("");
  const [debouncedSearchQuery, setDebouncedSearchQuery] = useState("");
  const [sortOrder, setSortOrder] = useState<"asc" | "desc">("desc");
  const [page, setPage] = useState(1);

  const refreshUsers = (resetToFirstPage = false) => {
    if (resetToFirstPage && page !== 1) {
      setPage(1);
      return;
    }
    setReloadToken((v) => v + 1);
  };

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedSearchQuery(searchQuery.trim());
      setPage(1);
    }, 300);
    return () => window.clearTimeout(timer);
  }, [searchQuery]);

  useEffect(() => {
    let cancelled = false;

    const loadUsersPage = async () => {
      setLoading(true);
      try {
        const tenantId = localStorage.getItem("current_tenant_id") || "";
        const params = new URLSearchParams({
          page: String(page),
          page_size: String(PAGE_SIZE),
          sort_order: sortOrder,
        });
        if (tenantId) params.set("tenant_id", tenantId);
        if (debouncedSearchQuery) params.set("search", debouncedSearchQuery);

        const data = await fetchJson<UserListResponse>(
          `/users/?${params.toString()}`,
        );
        if (cancelled) return;

        const responseTotalPages = Math.max(
          1,
          Math.ceil(data.total / PAGE_SIZE),
        );
        if (page > responseTotalPages) {
          setPage(responseTotalPages);
          return;
        }

        setUsers(data.items);
        setTotalUsers(data.total);
      } catch (e: any) {
        if (!cancelled) {
          console.error("Failed to load users", e);
          setToast(`Error: ${e.message}`);
          setTimeout(() => setToast(""), 3000);
        }
      } finally {
        if (!cancelled) {
          setHasLoadedUsersOnce(true);
          setLoading(false);
        }
      }
    };

    loadUsersPage();

    return () => {
      cancelled = true;
    };
  }, [page, debouncedSearchQuery, sortOrder, reloadToken]);

  const startEdit = (user: UserInfo) => {
    setEditingUserId(user.id);
    setEditForm({
      quota_message_limit: user.quota_message_limit,
      quota_message_period: user.quota_message_period,
      quota_max_agents: user.quota_max_agents,
      quota_agent_ttl_hours: user.quota_agent_ttl_hours,
    });
  };

  const handleSave = async () => {
    if (!editingUserId) return;
    setSaving(true);
    try {
      await fetchJson(`/users/${editingUserId}/quota`, {
        method: "PATCH",
        body: JSON.stringify(editForm),
      });
      setToast(isChinese ? "配额已更新" : "Quota updated");
      setTimeout(() => setToast(""), 2000);
      setEditingUserId(null);
      refreshUsers();
    } catch (e: any) {
      setToast(`Error: ${e.message}`);
      setTimeout(() => setToast(""), 3000);
    }
    setSaving(false);
  };

  // ── Role change handler ──
  const handleRoleChange = async (userId: string, newRole: string) => {
    setChangingRoleUserId(userId);
    try {
      await fetchJson(`/users/${userId}/role`, {
        method: "PATCH",
        body: JSON.stringify({ role: newRole }),
      });
      setToast(isChinese ? "Role updated" : "Role updated");
      setTimeout(() => setToast(""), 2000);
      // If changed own role, update auth store
      if (userId === currentUser?.id) {
        setUser({ ...currentUser, role: newRole as any });
      }
      refreshUsers();
    } catch (e: any) {
      const detail = (() => {
        try {
          return JSON.parse(e.message)?.detail;
        } catch {
          return e.message;
        }
      })();
      setToast(`Error: ${detail || e.message}`);
      setTimeout(() => setToast(""), 4000);
    }
    setChangingRoleUserId(null);
  };

  // ── Handlers ──

  const handleSendInvites = async () => {
    const emails = inviteEmails
      .split(/[\n,]+/)
      .map((e) => e.trim())
      .filter(Boolean);
    if (emails.length === 0) return;
    setInviting(true);
    setInviteResult(null);
    try {
      const res = await fetchJson<any>("/enterprise/invite-users", {
        method: "POST",
        body: JSON.stringify({ emails }),
      });
      setInviteResult({ invited: res.invited, message: res.message });
      setInviteEmails("");
      // Refresh user list after invite
      refreshUsers(true);
    } catch (e: any) {
      setToast(`Error: ${e.message}`);
      setTimeout(() => setToast(""), 3000);
    }
    setInviting(false);
  };

  const periodLabel = (period: string) => {
    if (isChinese) {
      const map: Record<string, string> = {
        permanent: "永久",
        daily: "每天",
        weekly: "每周",
        monthly: "每月",
      };
      return map[period] || period;
    }
    return PERIOD_OPTIONS.find((p) => p.value === period)?.label || period;
  };

  // Role label & styling helpers
  const roleBadge = (role: string) => {
    const styles: Record<
      string,
      { bg: string; color: string; label: string; labelZh: string }
    > = {
      platform_admin: {
        bg: "rgba(239,68,68,0.12)",
        color: "#ef4444",
        label: "Platform Admin",
        labelZh: "Platform Admin",
      },
      org_admin: {
        bg: "rgba(168,85,247,0.12)",
        color: "#a855f7",
        label: "Admin",
        labelZh: "Admin",
      },
    };
    const s = styles[role];
    if (!s) return null;
    return (
      <span
        style={{
          marginLeft: "6px",
          fontSize: "10px",
          background: s.bg,
          color: s.color,
          borderRadius: "4px",
          padding: "1px 6px",
          fontWeight: 500,
        }}
      >
        {isChinese ? s.labelZh : s.label}
      </span>
    );
  };

  const formatDate = (iso?: string) => {
    if (!iso) return "-";
    const d = new Date(iso);
    return d.toLocaleString(isChinese ? "zh-CN" : "en-US", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  };

  const resultCountLabel = debouncedSearchQuery
    ? isChinese
      ? `${totalUsers} 位匹配用户`
      : `${totalUsers} matching users`
    : isChinese
      ? `${totalUsers} 位用户`
      : `${totalUsers} users`;
  const showInitialLoading = loading && !hasLoadedUsersOnce;
  const showRefreshing = loading && hasLoadedUsersOnce;
  const userGridTemplateColumns =
    "1.4fr 1.4fr 0.8fr 0.7fr 0.7fr 0.8fr 0.8fr 0.8fr 0.8fr 120px";

  const toggleSort = () => {
    setSortOrder((o) => (o === "asc" ? "desc" : "asc"));
    setPage(1);
  };

  return (
    <div>
      {toast && (
        <div
          style={{
            position: "fixed",
            top: "20px",
            right: "20px",
            padding: "10px 20px",
            borderRadius: "8px",
            background: !toast.startsWith("Error:")
              ? "var(--success)"
              : "var(--error)",
            color: "#fff",
            fontSize: "13px",
            zIndex: 9999,
            transition: "all 0.3s",
          }}
        >
          {toast}
        </div>
      )}

      {editingProfileUser && (
        <EditUserModal
          user={editingProfileUser}
          onClose={() => setEditingProfileUser(null)}
          onUpdated={() => {
            refreshUsers();
            setEditingProfileUser(null);
          }}
        />
      )}

      <UserListPanel
        isChinese={isChinese}
        searchQuery={searchQuery}
        setSearchQuery={setSearchQuery}
        resultCountLabel={resultCountLabel}
        setShowInviteModal={setShowInviteModal}
        setInviteEmails={setInviteEmails}
        setInviteResult={setInviteResult}
        showInitialLoading={showInitialLoading}
        showRefreshing={showRefreshing}
        t={t}
        userGridTemplateColumns={userGridTemplateColumns}
        toggleSort={toggleSort}
        sortOrder={sortOrder}
        users={users}
        roleBadge={roleBadge}
        formatDate={formatDate}
        currentUser={currentUser}
        changingRoleUserId={changingRoleUserId}
        dialog={dialog}
        handleRoleChange={handleRoleChange}
        setEditingProfileUser={setEditingProfileUser}
        editingUserId={editingUserId}
        setEditingUserId={setEditingUserId}
        startEdit={startEdit}
        editForm={editForm}
        setEditForm={setEditForm}
        periodLabel={periodLabel}
        handleSave={handleSave}
        saving={saving}
        totalUsers={totalUsers}
        page={page}
        setPage={setPage}
      />

      {/* Invite Users Modal */}
      {showInviteModal && (
        <InviteUsersModal
          isChinese={isChinese}
          inviteEmails={inviteEmails}
          setInviteEmails={setInviteEmails}
          inviteResult={inviteResult}
          inviting={inviting}
          setShowInviteModal={setShowInviteModal}
          handleSendInvites={handleSendInvites}
        />
      )}
    </div>
  );
}

// ─── Edit User Profile Modal ───────────────────────────────
interface EditUserModalProps {
  user: UserInfo;
  onClose: () => void;
  onUpdated: () => void;
}

function EditUserModal({ user, onClose, onUpdated }: EditUserModalProps) {
  const { t, i18n } = useTranslation();
  const isChinese = i18n.language?.startsWith("zh");
  const { user: currentUser } = useAuthStore();

  const [form, setForm] = useState({
    display_name: user.display_name || "",
    email: user.email || "",
    primary_mobile: user.primary_mobile || "",
    is_active: user.is_active,
    new_password: "",
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const handleSave = async () => {
    if (!form.display_name.trim()) {
      setError(isChinese ? "显示名称不能为空" : "Display name is required");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const payload: any = {
        display_name: form.display_name.trim(),
        email: form.email.trim(),
        primary_mobile: form.primary_mobile.trim() || null,
        is_active: form.is_active,
      };
      if (form.new_password.trim()) {
        payload.new_password = form.new_password.trim();
      }
      await fetchJson(`/users/${user.id}/profile`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      });
      onUpdated();
      onClose();
    } catch (e: any) {
      const detail = (() => {
        try {
          return JSON.parse(e.message)?.detail;
        } catch {
          return e.message;
        }
      })();
      setError(detail || (isChinese ? "保存失败" : "Save failed"));
    }
    setSaving(false);
  };

  const isReadOnly =
    currentUser?.role === "org_admin" && user.role === "platform_admin";

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.5)",
        zIndex: 10001,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        backdropFilter: "blur(4px)",
      }}
      onClick={onClose}
    >
      <div
        className="card"
        style={{
          padding: "24px",
          maxWidth: "440px",
          width: "90%",
          boxShadow: "0 20px 60px rgba(0,0,0,0.3)",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginBottom: "20px",
          }}
        >
          <h2 style={{ fontSize: "16px", fontWeight: 600 }}>
            {isChinese ? "编辑用户信息" : "Edit User"}
          </h2>
          <button
            onClick={onClose}
            style={{
              background: "none",
              border: "none",
              cursor: "pointer",
              color: "var(--text-tertiary)",
            }}
          >
            <svg
              width="20"
              height="20"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        <div
          style={{
            fontSize: "12px",
            color: "var(--text-tertiary)",
            marginBottom: "16px",
          }}
        >
          {isChinese ? "用户名" : "Username"}:{" "}
          <span
            style={{ fontFamily: "monospace", color: "var(--text-secondary)" }}
          >
            @{user.username}
          </span>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
          <div className="form-group">
            <label className="form-label" style={{ fontSize: "12px" }}>
              {isChinese ? "显示名称" : "Display Name"}{" "}
              <span style={{ color: "var(--error)" }}>*</span>
            </label>
            <input
              className="form-input"
              value={form.display_name}
              onChange={(e) =>
                setForm({ ...form, display_name: e.target.value })
              }
              disabled={isReadOnly}
              style={{ fontSize: "13px" }}
            />
          </div>

          <div className="form-group">
            <label className="form-label" style={{ fontSize: "12px" }}>
              {isChinese ? "邮箱" : "Email"}
            </label>
            <input
              className="form-input"
              type="email"
              value={form.email}
              onChange={(e) => setForm({ ...form, email: e.target.value })}
              disabled={isReadOnly}
              style={{ fontSize: "13px" }}
            />
          </div>

          <div className="form-group">
            <label className="form-label" style={{ fontSize: "12px" }}>
              {isChinese ? "手机号" : "Mobile"}
            </label>
            <input
              className="form-input"
              type="tel"
              value={form.primary_mobile}
              onChange={(e) =>
                setForm({ ...form, primary_mobile: e.target.value })
              }
              disabled={isReadOnly}
              placeholder={isChinese ? "可选" : "Optional"}
              style={{ fontSize: "13px" }}
            />
          </div>

          <div className="form-group">
            <label className="form-label" style={{ fontSize: "12px" }}>
              {t("users.newPassword", isChinese ? "新密码" : "New Password")}
            </label>
            <input
              className="form-input"
              type="password"
              value={form.new_password}
              onChange={(e) =>
                setForm({ ...form, new_password: e.target.value })
              }
              disabled={isReadOnly}
              placeholder={t(
                "users.newPasswordPlaceholder",
                isChinese
                  ? "留空则不修改密码"
                  : "Leave blank to keep current password",
              )}
              style={{ fontSize: "13px" }}
              autoComplete="new-password"
            />
            <div
              style={{
                fontSize: "11px",
                color: "var(--text-tertiary)",
                marginTop: "4px",
              }}
            >
              {t(
                "users.newPasswordHint",
                isChinese
                  ? "可选。填写后将重置用户密码。"
                  : "Optional. If provided, user password will be reset.",
              )}
            </div>
          </div>

          {user.role !== "platform_admin" && (
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "10px 12px",
                borderRadius: "8px",
                background: "var(--bg-secondary)",
                border: "1px solid var(--border-subtle)",
              }}
            >
              <div>
                <div style={{ fontSize: "13px", fontWeight: 500 }}>
                  {isChinese ? "账号状态" : "Account Status"}
                </div>
                <div
                  style={{
                    fontSize: "11px",
                    color: "var(--text-tertiary)",
                    marginTop: "2px",
                  }}
                >
                  {isChinese
                    ? "禁用后用户无法登录"
                    : "Disabled users cannot log in"}
                </div>
              </div>
              <label
                style={{
                  position: "relative",
                  display: "inline-block",
                  width: "40px",
                  height: "22px",
                  cursor: "pointer",
                  flexShrink: 0,
                }}
              >
                <input
                  type="checkbox"
                  checked={form.is_active}
                  onChange={(e) =>
                    setForm({ ...form, is_active: e.target.checked })
                  }
                  style={{ opacity: 0, width: 0, height: 0 }}
                />
                <span
                  style={{
                    position: "absolute",
                    inset: 0,
                    background: form.is_active
                      ? "var(--accent-primary)"
                      : "var(--bg-tertiary)",
                    borderRadius: "11px",
                    transition: "background 0.2s",
                  }}
                >
                  <span
                    style={{
                      position: "absolute",
                      left: form.is_active ? "20px" : "2px",
                      top: "2px",
                      width: "18px",
                      height: "18px",
                      background: "#fff",
                      borderRadius: "50%",
                      transition: "left 0.2s",
                    }}
                  />
                </span>
              </label>
            </div>
          )}
        </div>

        {error && (
          <div
            style={{
              color: "var(--error)",
              fontSize: "12px",
              marginTop: "12px",
            }}
          >
            {error}
          </div>
        )}

        <div style={{ display: "flex", gap: "8px", marginTop: "20px" }}>
          <button
            className="btn btn-secondary"
            style={{ flex: 1 }}
            onClick={onClose}
            disabled={saving}
          >
            {t("common.cancel", "Cancel")}
          </button>
          {!isReadOnly && (
            <button
              className="btn btn-primary"
              style={{ flex: 1 }}
              onClick={handleSave}
              disabled={saving}
            >
              {saving
                ? t("common.loading", "Loading...")
                : t("common.save", "Save")}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
