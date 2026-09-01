import type {
  Dispatch,
  ReactNode,
  SetStateAction,
} from "react";
import type { TFunction } from "i18next";
import Pagination from "../../components/Pagination";
import { Pencil } from "lucide-react";
import {
  PAGE_SIZE,
  PERIOD_OPTIONS,
  type UserInfo,
} from "./model";

interface UserQuotaForm {
  quota_message_limit: number;
  quota_message_period: string;
  quota_max_agents: number;
  quota_agent_ttl_hours: number;
}

interface UserListPanelProps {
  isChinese: boolean;
  searchQuery: string;
  setSearchQuery: (value: string) => void;
  resultCountLabel: string;
  setShowInviteModal: (show: boolean) => void;
  setInviteEmails: (value: string) => void;
  setInviteResult: (
    value: { invited: number; message: string } | null,
  ) => void;
  showInitialLoading: boolean;
  showRefreshing: boolean;
  t: TFunction;
  userGridTemplateColumns: string;
  toggleSort: () => void;
  sortOrder: "asc" | "desc";
  users: UserInfo[];
  roleBadge: (role: string) => ReactNode;
  formatDate: (iso?: string) => string;
  currentUser: any;
  changingRoleUserId: string | null;
  dialog: {
    confirm: (
      message: string,
      options?: { title?: string },
    ) => Promise<boolean>;
  };
  handleRoleChange: (userId: string, newRole: string) => Promise<void>;
  setEditingProfileUser: (user: UserInfo | null) => void;
  editingUserId: string | null;
  setEditingUserId: (userId: string | null) => void;
  startEdit: (user: UserInfo) => void;
  editForm: UserQuotaForm;
  setEditForm: Dispatch<SetStateAction<UserQuotaForm>>;
  periodLabel: (period: string) => string;
  handleSave: () => Promise<void>;
  saving: boolean;
  totalUsers: number;
  page: number;
  setPage: (page: number) => void;
}

export default function UserListPanel({
  isChinese,
  searchQuery,
  setSearchQuery,
  resultCountLabel,
  setShowInviteModal,
  setInviteEmails,
  setInviteResult,
  showInitialLoading,
  showRefreshing,
  t,
  userGridTemplateColumns,
  toggleSort,
  sortOrder,
  users,
  roleBadge,
  formatDate,
  currentUser,
  changingRoleUserId,
  dialog,
  handleRoleChange,
  setEditingProfileUser,
  editingUserId,
  setEditingUserId,
  startEdit,
  editForm,
  setEditForm,
  periodLabel,
  handleSave,
  saving,
  totalUsers,
  page,
  setPage,
}: UserListPanelProps) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
      {/* Search bar + Invite button */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: "4px",
          gap: "16px",
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "12px",
            minWidth: 0,
          }}
        >
          <input
            className="form-input"
            type="text"
            aria-label={isChinese ? "搜索用户" : "Search users"}
            placeholder={
              isChinese
                ? "搜索用户名、显示名、邮箱或手机号…"
                : "Search username, name, email or phone…"
            }
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            style={{
              width: "360px",
              maxWidth: "min(360px, 52vw)",
              fontSize: "13px",
              padding: "8px 12px 8px 12px",
              background: "var(--bg-elevated)",
              border: "1px solid var(--border-subtle)",
              borderRadius: "8px",
            }}
          />
          <span
            style={{
              fontSize: "12px",
              color: "var(--text-tertiary)",
              whiteSpace: "nowrap",
            }}
          >
            {resultCountLabel}
          </span>
        </div>
        <button
          className="btn btn-primary"
          style={{ fontSize: "13px", padding: "6px 16px", flexShrink: 0 }}
          onClick={() => {
            setShowInviteModal(true);
            setInviteEmails("");
            setInviteResult(null);
          }}
        >
          {isChinese ? "邀请新用户" : "Invite Users"}
        </button>
      </div>

      <div
        style={{
          position: "relative",
          minHeight: "560px",
          display: "flex",
          flexDirection: "column",
          gap: "8px",
        }}
      >
        {showInitialLoading ? (
          <div
            style={{
              textAlign: "center",
              padding: "40px",
              color: "var(--text-tertiary)",
            }}
          >
            {t("common.loading")}...
          </div>
        ) : (
          <>
            {/* Header */}
            <div
              style={{
                display: "grid",
                gridTemplateColumns: userGridTemplateColumns,
                gap: "10px",
                padding: "10px 16px",
                fontSize: "11px",
                fontWeight: 600,
                color: "var(--text-tertiary)",
                textTransform: "uppercase",
                letterSpacing: "0.05em",
              }}
            >
              <div>
                {t("enterprise.users.user", isChinese ? "用户" : "User")}
              </div>
              <div>{t("enterprise.users.email", "Email")}</div>
              {/* Created At with sort toggle */}
              <div
                style={{
                  cursor: "pointer",
                  userSelect: "none",
                  display: "flex",
                  alignItems: "center",
                  gap: "3px",
                }}
                onClick={toggleSort}
                title={
                  isChinese ? "点击切换排序" : "Click to toggle sort order"
                }
              >
                {isChinese ? "注册时间" : "Joined"}{" "}
                {sortOrder === "asc" ? "↑" : "↓"}
              </div>
              <div>{isChinese ? "角色" : "Role"}</div>
              <div>{isChinese ? "来源" : "Source"}</div>
              <div>
                {t(
                  "enterprise.users.msgQuota",
                  isChinese ? "消息配额" : "Msg Quota",
                )}
              </div>
              <div>
                {t("enterprise.users.period", isChinese ? "周期" : "Period")}
              </div>
              <div>
                {t(
                  "enterprise.users.agents",
                  isChinese ? "数字员工" : "Agents",
                )}
              </div>
              <div>{t("enterprise.users.ttl", "TTL")}</div>
              <div></div>
            </div>

            {users.map((user) => (
              <div key={user.id}>
                <div
                  className="card"
                  style={{
                    display: "grid",
                    gridTemplateColumns: userGridTemplateColumns,
                    gap: "10px",
                    alignItems: "center",
                    padding: "12px 16px",
                  }}
                >
                  <div>
                    <div style={{ fontWeight: 500, fontSize: "14px" }}>
                      {user.display_name || user.username}
                      {roleBadge(user.role)}
                    </div>
                    {user.nickname && user.nickname !== user.display_name && (
                      <div
                        style={{
                          fontSize: "11px",
                          color: "var(--text-secondary)",
                        }}
                      >
                        {isChinese
                          ? `昵称：${user.nickname}`
                          : `Nickname: ${user.nickname}`}
                      </div>
                    )}
                    <div
                      style={{
                        fontSize: "11px",
                        color: "var(--text-tertiary)",
                      }}
                    >
                      @{user.username}
                    </div>
                  </div>
                  <div
                    style={{
                      fontSize: "12px",
                      color: "var(--text-secondary)",
                    }}
                  >
                    {user.email}
                  </div>
                  <div
                    style={{
                      fontSize: "11px",
                      color: "var(--text-secondary)",
                    }}
                  >
                    {formatDate(user.created_at)}
                  </div>
                  {/* Role selector — only for admin users, not for platform_admin targets */}
                  <div>
                    {currentUser?.role &&
                    ["platform_admin", "org_admin"].includes(
                      currentUser.role,
                    ) &&
                    user.role !== "platform_admin" ? (
                      <select
                        className="form-input"
                        value={user.role}
                        disabled={changingRoleUserId === user.id}
                        onChange={async (e) => {
                          const newRole = e.target.value;
                          const confirmMsg = isChinese
                            ? `确认将 ${user.display_name || user.username} 的角色更改为 ${newRole === "org_admin" ? "Admin" : "Member"}？`
                            : `Change ${user.display_name || user.username}'s role to ${newRole === "org_admin" ? "Admin" : "Member"}?`;
                          const ok = await dialog.confirm(confirmMsg, {
                            title: isChinese ? "更改角色" : "Change role",
                          });
                          if (ok) handleRoleChange(user.id, newRole);
                        }}
                        style={{
                          fontSize: "11px",
                          padding: "2px 4px",
                          width: "100%",
                          minWidth: 0,
                        }}
                      >
                        <option value="member">
                          {isChinese ? "Member" : "Member"}
                        </option>
                        <option value="org_admin">
                          {isChinese ? "Admin" : "Admin"}
                        </option>
                      </select>
                    ) : (
                      <span
                        style={{
                          fontSize: "11px",
                          color: "var(--text-secondary)",
                        }}
                      >
                        {user.role === "platform_admin"
                          ? "Platform Admin"
                          : user.role === "org_admin"
                            ? "Admin"
                            : "Member"}
                      </span>
                    )}
                  </div>
                  <div>
                    {user.source === "feishu" ? (
                      <span
                        style={{
                          fontSize: "10px",
                          background: "rgba(58,132,255,0.12)",
                          color: "#3a84ff",
                          borderRadius: "4px",
                          padding: "2px 7px",
                          whiteSpace: "nowrap",
                        }}
                      >
                        飞书
                      </span>
                    ) : (
                      <span
                        style={{
                          fontSize: "10px",
                          background: "rgba(0,180,120,0.12)",
                          color: "var(--success)",
                          borderRadius: "4px",
                          padding: "2px 7px",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {isChinese ? "注册" : "Reg"}
                      </span>
                    )}
                  </div>
                  <div>
                    <span style={{ fontSize: "13px", fontWeight: 500 }}>
                      {user.quota_messages_used}
                    </span>
                    <span
                      style={{
                        fontSize: "11px",
                        color: "var(--text-tertiary)",
                      }}
                    >
                      {" "}
                      / {user.quota_message_limit}
                    </span>
                  </div>
                  <div>
                    <span
                      className="badge badge-info"
                      style={{ fontSize: "10px" }}
                    >
                      {periodLabel(user.quota_message_period)}
                    </span>
                  </div>
                  <div>
                    <span style={{ fontSize: "13px", fontWeight: 500 }}>
                      {user.agents_count}
                    </span>
                    <span
                      style={{
                        fontSize: "11px",
                        color: "var(--text-tertiary)",
                      }}
                    >
                      {" "}
                      / {user.quota_max_agents}
                    </span>
                  </div>
                  <div style={{ fontSize: "12px" }}>
                    {user.quota_agent_ttl_hours > 0
                      ? `${user.quota_agent_ttl_hours}h`
                      : t("enterprise.quotas.permanent", "Permanent")}
                  </div>
                  <div>
                    <div
                      style={{
                        display: "flex",
                        gap: "4px",
                        flexDirection: "column",
                      }}
                    >
                      <button
                        className="btn btn-ghost"
                        style={{
                          padding: "2px 8px",
                          fontSize: "11px",
                          color: "var(--accent-primary)",
                        }}
                        onClick={() => setEditingProfileUser(user)}
                      >
                        {isChinese ? "编辑信息" : "Edit Info"}
                      </button>
                      <button
                        className="btn btn-secondary"
                        style={{ padding: "2px 8px", fontSize: "11px" }}
                        onClick={() =>
                          editingUserId === user.id
                            ? setEditingUserId(null)
                            : startEdit(user)
                        }
                      >
                        {editingUserId === user.id ? (
                          t("common.cancel")
                        ) : (
                          <>
                            <Pencil size={14} />{" "}
                            {isChinese ? "配额" : "Quota"}
                          </>
                        )}
                      </button>
                    </div>
                  </div>
                </div>

                {/* Inline edit form */}
                {editingUserId === user.id && (
                  <div
                    className="card"
                    style={{
                      marginTop: "4px",
                      padding: "16px",
                      background: "var(--bg-secondary)",
                      borderLeft: "3px solid var(--accent-color)",
                    }}
                  >
                    <div
                      style={{
                        display: "grid",
                        gridTemplateColumns: "1fr 1fr 1fr 1fr",
                        gap: "16px",
                      }}
                    >
                      <div className="form-group">
                        <label
                          className="form-label"
                          style={{ fontSize: "11px" }}
                        >
                          {t(
                            "enterprise.users.msgLimit",
                            isChinese ? "消息限额" : "Message Limit",
                          )}
                        </label>
                        <input
                          className="form-input"
                          type="number"
                          min={0}
                          value={editForm.quota_message_limit}
                          onChange={(e) =>
                            setEditForm({
                              ...editForm,
                              quota_message_limit: Number(e.target.value),
                            })
                          }
                        />
                      </div>
                      <div className="form-group">
                        <label
                          className="form-label"
                          style={{ fontSize: "11px" }}
                        >
                          {t(
                            "enterprise.users.period",
                            isChinese ? "重置周期" : "Period",
                          )}
                        </label>
                        <select
                          className="form-input"
                          value={editForm.quota_message_period}
                          onChange={(e) =>
                            setEditForm({
                              ...editForm,
                              quota_message_period: e.target.value,
                            })
                          }
                        >
                          {PERIOD_OPTIONS.map((p) => (
                            <option key={p.value} value={p.value}>
                              {periodLabel(p.value)}
                            </option>
                          ))}
                        </select>
                      </div>
                      <div className="form-group">
                        <label
                          className="form-label"
                          style={{ fontSize: "11px" }}
                        >
                          {t(
                            "enterprise.users.maxAgents",
                            isChinese ? "最多数字员工" : "Max Agents",
                          )}
                        </label>
                        <input
                          className="form-input"
                          type="number"
                          min={0}
                          value={editForm.quota_max_agents}
                          onChange={(e) =>
                            setEditForm({
                              ...editForm,
                              quota_max_agents: Number(e.target.value),
                            })
                          }
                        />
                      </div>
                      <div className="form-group">
                        <label
                          className="form-label"
                          style={{ fontSize: "11px" }}
                        >
                          {t(
                            "enterprise.users.agentTTL",
                            isChinese
                              ? "员工存活时长(h)"
                              : "Agent TTL (hours)",
                          )}
                        </label>
                        <input
                          className="form-input"
                          type="number"
                          min={0}
                          value={editForm.quota_agent_ttl_hours}
                          onChange={(e) =>
                            setEditForm({
                              ...editForm,
                              quota_agent_ttl_hours: Number(e.target.value),
                            })
                          }
                        />
                        <div
                          style={{
                            fontSize: "11px",
                            color: "var(--text-tertiary)",
                            marginTop: "4px",
                          }}
                        >
                          {t("enterprise.quotas.agentAutoExpiry")}
                        </div>
                      </div>
                    </div>
                    <div
                      style={{
                        marginTop: "12px",
                        display: "flex",
                        gap: "8px",
                        justifyContent: "flex-end",
                      }}
                    >
                      <button
                        className="btn btn-secondary"
                        onClick={() => setEditingUserId(null)}
                      >
                        {t("common.cancel")}
                      </button>
                      <button
                        className="btn btn-primary"
                        onClick={handleSave}
                        disabled={saving}
                      >
                        {saving
                          ? t("common.loading")
                          : t("common.save", "Save")}
                      </button>
                    </div>
                  </div>
                )}
              </div>
            ))}

            {users.length === 0 && (
              <div
                style={{
                  textAlign: "center",
                  padding: "40px",
                  color: "var(--text-tertiary)",
                }}
              >
                {t("common.noData")}
              </div>
            )}

            {totalUsers > 0 && (
              <Pagination
                page={page}
                pageSize={PAGE_SIZE}
                total={totalUsers}
                onPageChange={setPage}
              />
            )}
          </>
        )}
        {showRefreshing && (
          <div
            style={{
              position: "absolute",
              top: "8px",
              right: "8px",
              padding: "4px 10px",
              borderRadius: "999px",
              border: "1px solid var(--border-subtle)",
              background: "var(--bg-elevated)",
              color: "var(--text-tertiary)",
              fontSize: "12px",
              boxShadow: "var(--shadow-sm)",
              pointerEvents: "none",
            }}
          >
            {t("common.loading")}...
          </div>
        )}
      </div>
    </div>
  );
}
