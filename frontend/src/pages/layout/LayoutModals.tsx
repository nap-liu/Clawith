import { IconX } from "@tabler/icons-react";

export default function LayoutModals({
  showTenantSetupModal,
  setShowTenantSetupModal,
  isChinese,
  tenantFormError,
  handleModalJoin,
  joinInviteCode,
  setJoinInviteCode,
  tenantFormLoading,
  allowSelfCreate,
  handleModalCreate,
  createCompanyName,
  setCreateCompanyName,
  showNotifications,
  setShowNotifications,
  unreadCount,
  markAllRead,
  notifCategory,
  setNotifCategory,
  notifications,
  markOneRead,
  setSelectedNotification,
  navigate,
  selectedNotification,
}: any) {
  return (
    <>
      {showTenantSetupModal && (
        <div
          className="tenant-setup-modal-backdrop"
          onClick={() => setShowTenantSetupModal(false)}
        >
          <div
            className="tenant-setup-modal"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="tenant-setup-modal-header">
              <div>
                <h3>
                  {isChinese ? "创建或加入新公司" : "Create or Join Company"}
                </h3>
                <p>
                  {isChinese
                    ? "加入已有公司，或创建一个新的工作空间。"
                    : "Join an existing company or start a new workspace."}
                </p>
              </div>
              <button
                type="button"
                onClick={() => setShowTenantSetupModal(false)}
                aria-label={isChinese ? "关闭" : "Close"}
              >
                <IconX size={18} stroke={1.8} />
              </button>
            </div>

            {tenantFormError && (
              <div className="tenant-setup-error">{tenantFormError}</div>
            )}

            <form onSubmit={handleModalJoin} className="tenant-setup-section">
              <div className="tenant-setup-section-title">
                {isChinese ? "通过邀请码加入" : "Join via invitation code"}
              </div>
              <div className="tenant-setup-row">
                <input
                  className="form-input"
                  value={joinInviteCode}
                  onChange={(e) => setJoinInviteCode(e.target.value)}
                  placeholder={
                    isChinese ? "输入邀请码" : "Enter invitation code"
                  }
                />
                <button
                  className="btn btn-primary"
                  type="submit"
                  disabled={tenantFormLoading || !joinInviteCode.trim()}
                >
                  {tenantFormLoading ? "..." : isChinese ? "加入" : "Join"}
                </button>
              </div>
            </form>

            {allowSelfCreate && (
              <>
                <div className="tenant-setup-divider">
                  <span>{isChinese ? "或者" : "OR"}</span>
                </div>
                <form
                  onSubmit={handleModalCreate}
                  className="tenant-setup-section"
                >
                  <div className="tenant-setup-section-title">
                    {isChinese ? "创建新公司" : "Create a new company"}
                  </div>
                  <div className="tenant-setup-row">
                    <input
                      className="form-input"
                      value={createCompanyName}
                      onChange={(e) => setCreateCompanyName(e.target.value)}
                      placeholder={isChinese ? "公司名称" : "Company name"}
                    />
                    <button
                      className="btn btn-primary"
                      type="submit"
                      disabled={tenantFormLoading || !createCompanyName.trim()}
                    >
                      {tenantFormLoading
                        ? "..."
                        : isChinese
                          ? "创建"
                          : "Create"}
                    </button>
                  </div>
                </form>
              </>
            )}
          </div>
        </div>
      )}

      {/* Notification Modal */}
      {showNotifications && (
        <>
          <div
            style={{
              position: "fixed",
              inset: 0,
              zIndex: 9998,
              background: "rgba(0,0,0,0.5)",
            }}
            onClick={() => setShowNotifications(false)}
          />
          <div
            style={{
              position: "fixed",
              top: "50%",
              left: "50%",
              transform: "translate(-50%, -50%)",
              width: "calc(100vw - 80px)",
              maxWidth: "800px",
              height: "80vh",
              maxHeight: "800px",
              background: "var(--bg-primary)",
              border: "1px solid var(--border-subtle)",
              borderRadius: "12px",
              boxShadow: "0 20px 60px rgba(0,0,0,0.3)",
              zIndex: 9999,
              display: "flex",
              flexDirection: "column",
              overflow: "hidden",
            }}
          >
            <div
              style={{
                borderBottom: "1px solid var(--border-subtle)",
                flexShrink: 0,
              }}
            >
              <div
                style={{
                  padding: "16px 24px 0",
                  display: "flex",
                  alignItems: "center",
                  gap: "8px",
                }}
              >
                <h3
                  style={{
                    margin: 0,
                    fontSize: "16px",
                    fontWeight: 600,
                    flex: 1,
                  }}
                >
                  {isChinese ? "通知" : "Notifications"}
                </h3>
                {(unreadCount as number) > 0 && (
                  <button
                    className="btn btn-ghost"
                    onClick={markAllRead}
                    style={{ fontSize: "12px", padding: "4px 10px" }}
                  >
                    {isChinese ? "全部已读" : "Mark all read"}
                  </button>
                )}
                <button
                  className="btn btn-ghost"
                  onClick={() => setShowNotifications(false)}
                  style={{
                    padding: "4px 8px",
                    fontSize: "18px",
                    lineHeight: 1,
                  }}
                >
                  ×
                </button>
              </div>
              <div
                style={{
                  display: "flex",
                  gap: "0",
                  padding: "0 24px",
                  marginTop: "12px",
                }}
              >
                {[
                  { key: "all", zh: "全部", en: "All" },
                  { key: "tool", zh: "工具执行", en: "Tool" },
                  { key: "approval", zh: "审批", en: "Approval" },
                  { key: "social", zh: "社交", en: "Social" },
                ].map((tab) => (
                  <button
                    key={tab.key}
                    onClick={() => {
                      setNotifCategory(tab.key);
                    }}
                    style={{
                      background: "none",
                      border: "none",
                      cursor: "pointer",
                      padding: "8px 14px",
                      fontSize: "13px",
                      fontWeight: 500,
                      color:
                        notifCategory === tab.key
                          ? "var(--text-primary)"
                          : "var(--text-tertiary)",
                      borderBottom:
                        notifCategory === tab.key
                          ? "2px solid var(--accent-primary)"
                          : "2px solid transparent",
                      marginBottom: "-1px",
                      transition: "all 0.15s",
                    }}
                  >
                    {isChinese ? tab.zh : tab.en}
                  </button>
                ))}
              </div>
            </div>
            <div style={{ flex: 1, overflowY: "auto", padding: "8px 0" }}>
              {(notifications as any[]).length === 0 && (
                <div
                  style={{
                    textAlign: "center",
                    padding: "60px 20px",
                    color: "var(--text-tertiary)",
                    fontSize: "13px",
                  }}
                >
                  {isChinese ? "暂无通知" : "No notifications"}
                </div>
              )}
              {(notifications as any[]).map((n: any) => (
                <div
                  key={n.id}
                  onClick={() => {
                    if (!n.is_read) markOneRead(n.id);
                    if (n.type === "broadcast" || !n.link) {
                      setSelectedNotification(n);
                    } else if (n.link) {
                      navigate(n.link);
                      setShowNotifications(false);
                    }
                  }}
                  style={{
                    padding: "14px 24px",
                    cursor: "pointer",
                    borderBottom: "1px solid var(--border-subtle)",
                    background: n.is_read
                      ? "transparent"
                      : "var(--bg-secondary)",
                    transition: "background 0.15s",
                  }}
                  onMouseEnter={(e) =>
                    (e.currentTarget.style.background = "var(--bg-tertiary)")
                  }
                  onMouseLeave={(e) =>
                    (e.currentTarget.style.background = n.is_read
                      ? "transparent"
                      : "var(--bg-secondary)")
                  }
                >
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: "6px",
                      marginBottom: "4px",
                    }}
                  >
                    {!n.is_read && (
                      <span
                        style={{
                          width: "6px",
                          height: "6px",
                          borderRadius: "50%",
                          background: "var(--accent-primary)",
                          flexShrink: 0,
                        }}
                      />
                    )}
                    <span
                      style={{
                        fontSize: "13px",
                        fontWeight: 500,
                        flex: 1,
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {n.title}
                    </span>
                  </div>
                  {n.body && (
                    <div
                      style={{
                        fontSize: "12px",
                        color: "var(--text-tertiary)",
                        lineHeight: "1.4",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {n.body}
                    </div>
                  )}
                  <div
                    style={{
                      fontSize: "11px",
                      color: "var(--text-quaternary)",
                      marginTop: "4px",
                    }}
                  >
                    {n.created_at
                      ? new Date(n.created_at).toLocaleString()
                      : ""}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </>
      )}

      {/* Notification Detail Modal */}
      {selectedNotification && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 10000,
            background: "rgba(0,0,0,0.5)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
          onClick={() => setSelectedNotification(null)}
        >
          <div
            style={{
              background: "var(--bg-primary)",
              borderRadius: "12px",
              border: "1px solid var(--border-subtle)",
              width: "480px",
              maxHeight: "90vh",
              display: "flex",
              flexDirection: "column",
              boxShadow: "0 20px 60px rgba(0,0,0,0.3)",
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div
              style={{
                padding: "20px 24px",
                borderBottom: "1px solid var(--border-subtle)",
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
              }}
            >
              <h3 style={{ margin: 0, fontSize: "16px", fontWeight: 600 }}>
                {selectedNotification.title}
              </h3>
              <button
                onClick={() => setSelectedNotification(null)}
                style={{
                  background: "none",
                  border: "none",
                  color: "var(--text-tertiary)",
                  fontSize: "20px",
                  cursor: "pointer",
                  padding: "0",
                }}
              >
                ×
              </button>
            </div>
            <div
              style={{
                padding: "20px 24px",
                overflowY: "auto",
                fontSize: "14px",
                lineHeight: "1.6",
                color: "var(--text-primary)",
                whiteSpace: "pre-wrap",
              }}
            >
              {selectedNotification.body ||
                (isChinese ? "无详细内容" : "No details provided")}
            </div>
            <div
              style={{
                padding: "16px 24px",
                borderTop: "1px solid var(--border-subtle)",
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                color: "var(--text-tertiary)",
                fontSize: "12px",
              }}
            >
              <span>
                {selectedNotification.sender_name
                  ? isChinese
                    ? `来自: ${selectedNotification.sender_name}`
                    : `From: ${selectedNotification.sender_name}`
                  : ""}
              </span>
              <span>
                {selectedNotification.created_at
                  ? new Date(selectedNotification.created_at).toLocaleString()
                  : ""}
              </span>
            </div>
          </div>
        </div>
      )}

    </>
  );
}
