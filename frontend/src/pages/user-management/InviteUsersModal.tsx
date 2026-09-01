interface InviteUsersModalProps {
  isChinese: boolean;
  inviteEmails: string;
  setInviteEmails: (value: string) => void;
  inviteResult: { invited: number; message: string } | null;
  inviting: boolean;
  setShowInviteModal: (show: boolean) => void;
  handleSendInvites: () => Promise<void>;
}

export default function InviteUsersModal({
  isChinese,
  inviteEmails,
  setInviteEmails,
  inviteResult,
  inviting,
  setShowInviteModal,
  handleSendInvites,
}: InviteUsersModalProps) {
  return (
      <div
        style={{
          position: "fixed",
          inset: 0,
          zIndex: 10000,
          background: "rgba(0,0,0,0.5)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          backdropFilter: "blur(4px)",
        }}
        onClick={() => setShowInviteModal(false)}
      >
        <div
          style={{
            background: "var(--bg-primary)",
            borderRadius: "12px",
            border: "1px solid var(--border-subtle)",
            width: "500px",
            boxShadow: "0 20px 60px rgba(0,0,0,0.3)",
          }}
          onClick={(e) => e.stopPropagation()}
        >
          {/* Header */}
          <div
            style={{
              padding: "20px",
              borderBottom: "1px solid var(--border-subtle)",
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
            }}
          >
            <h3 style={{ margin: 0, fontSize: "16px", fontWeight: 600 }}>
              {isChinese ? "邀请新用户" : "Invite Users"}
            </h3>
            <button
              onClick={() => setShowInviteModal(false)}
              style={{
                background: "none",
                border: "none",
                color: "var(--text-tertiary)",
                fontSize: "18px",
                cursor: "pointer",
                padding: "4px 8px",
              }}
            >
              x
            </button>
          </div>
          {/* Body */}
          <div style={{ padding: "20px" }}>
            <label
              className="form-label"
              style={{ fontSize: "13px", marginBottom: "6px" }}
            >
              {isChinese ? "邮箱地址" : "Email Addresses"}
            </label>
            <textarea
              className="form-input"
              rows={5}
              placeholder={
                isChinese
                  ? "输入邮箱地址，用逗号或换行分隔..."
                  : "Enter email addresses, separated by commas or newlines..."
              }
              value={inviteEmails}
              onChange={(e) => setInviteEmails(e.target.value)}
              style={{
                resize: "vertical",
                fontSize: "13px",
                marginBottom: "16px",
                width: "100%",
              }}
            />
            {/* public link generated here previously */}
            {inviteResult && (
              <div
                style={{
                  marginTop: "12px",
                  padding: "10px 14px",
                  background: "rgba(0,200,100,0.1)",
                  color: "var(--success)",
                  borderRadius: "6px",
                  fontSize: "13px",
                }}
              >
                {inviteResult.message} ({inviteResult.invited}{" "}
                {isChinese ? "位用户" : "users"})
              </div>
            )}
          </div>
          {/* Footer */}
          <div
            style={{
              padding: "16px 20px",
              borderTop: "1px solid var(--border-subtle)",
              display: "flex",
              justifyContent: "flex-end",
              gap: "12px",
              background: "var(--bg-secondary)",
              borderRadius: "0 0 12px 12px",
            }}
          >
            <button
              className="btn btn-secondary"
              onClick={() => setShowInviteModal(false)}
            >
              {isChinese ? "取消" : "Cancel"}
            </button>
            <button
              className="btn btn-primary"
              onClick={handleSendInvites}
              disabled={inviting || !inviteEmails.trim()}
            >
              {inviting
                ? isChinese
                  ? "发送中..."
                  : "Sending..."
                : isChinese
                  ? "发送邀请"
                  : "Send Invitations"}
            </button>
          </div>
        </div>
      </div>
  );
}
