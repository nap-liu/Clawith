import { useState } from "react";
import PatManager from "../../components/PatManager";
import { useAuthStore } from "../../stores";


/* ────── Account Settings Modal ────── */
export default function AccountSettingsModal({
  user,
  onClose,
  isChinese,
}: {
  user: any;
  onClose: () => void;
  isChinese: boolean;
}) {
  const { setUser } = useAuthStore();
  const [username, setUsername] = useState(user?.username || "");
  const [email, setEmail] = useState(user?.email || "");
  const [displayName, setDisplayName] = useState(user?.display_name || "");
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [saving, setSaving] = useState(false);
  const [resendingEmail, setResendingEmail] = useState(false);
  const [msg, setMsg] = useState("");
  const [msgType, setMsgType] = useState<"success" | "error">("success");

  const showMsg = (text: string, type: "success" | "error" = "success") => {
    setMsg(text);
    setMsgType(type);
    setTimeout(() => setMsg(""), 3000);
  };

  const handleSaveProfile = async () => {
    setSaving(true);
    try {
      const token = localStorage.getItem("token");
      const body: any = {};
      if (username !== user?.username) body.username = username;
      if (email !== user?.email) body.email = email;
      if (displayName !== user?.display_name) body.display_name = displayName;
      if (Object.keys(body).length === 0) {
        showMsg(isChinese ? "没有变更" : "No changes", "error");
        setSaving(false);
        return;
      }
      const res = await fetch("/api/auth/me", {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Failed" }));
        throw new Error(err.detail);
      }
      const updated = await res.json();
      setUser(updated);
      showMsg(isChinese ? "个人信息已更新" : "Profile updated");
    } catch (e: any) {
      showMsg(e.message || "Failed", "error");
    }
    setSaving(false);
  };

  const handleResendVerification = async () => {
    setResendingEmail(true);
    try {
      const token = localStorage.getItem("token");
      const res = await fetch("/api/auth/resend-verification", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ email: user?.email }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Failed" }));
        throw new Error(err.detail);
      }
      showMsg(
        isChinese
          ? "验证邮件已发送，请查收"
          : "Verification email sent. Please check your inbox.",
      );
    } catch (e: any) {
      showMsg(e.message || "Failed", "error");
    }
    setResendingEmail(false);
  };

  const handleChangePassword = async () => {
    if (!oldPassword || !newPassword) {
      showMsg(
        isChinese ? "请填写所有密码字段" : "Fill all password fields",
        "error",
      );
      return;
    }
    if (newPassword.length < 6) {
      showMsg(isChinese ? "新密码至少 6 个字符" : "Min 6 characters", "error");
      return;
    }
    if (newPassword !== confirmPassword) {
      showMsg(isChinese ? "两次密码不一致" : "Passwords do not match", "error");
      return;
    }
    setSaving(true);
    try {
      const token = localStorage.getItem("token");
      const res = await fetch("/api/auth/me/password", {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          old_password: oldPassword,
          new_password: newPassword,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Failed" }));
        throw new Error(err.detail);
      }
      showMsg(isChinese ? "密码已修改" : "Password changed");
      setOldPassword("");
      setNewPassword("");
      setConfirmPassword("");
    } catch (e: any) {
      showMsg(e.message || "Failed", "error");
    }
    setSaving(false);
  };

  const inputStyle = { width: "100%", fontSize: "13px" };
  const labelStyle = {
    display: "block" as const,
    fontSize: "12px",
    fontWeight: 500,
    marginBottom: "4px",
    color: "var(--text-secondary)",
  };

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
      }}
      onClick={onClose}
    >
      <div
        style={{
          background: "var(--bg-primary)",
          borderRadius: "12px",
          border: "1px solid var(--border-subtle)",
          width: "420px",
          maxHeight: "90vh",
          overflow: "auto",
          padding: "24px",
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
          <h3 style={{ margin: 0 }}>
            {isChinese ? "账户设置" : "Account Settings"}
          </h3>
          <button
            onClick={onClose}
            style={{
              background: "none",
              border: "none",
              color: "var(--text-tertiary)",
              fontSize: "18px",
              cursor: "pointer",
              padding: "4px 8px",
            }}
          >
            ×
          </button>
        </div>
        {msg && (
          <div
            style={{
              padding: "8px 12px",
              borderRadius: "6px",
              fontSize: "12px",
              marginBottom: "16px",
              background:
                msgType === "success"
                  ? "rgba(0,180,120,0.12)"
                  : "rgba(255,80,80,0.12)",
              color: msgType === "success" ? "var(--success)" : "var(--error)",
            }}
          >
            {msg}
          </div>
        )}
        {/* Profile */}
        <h4
          style={{
            margin: "0 0 12px",
            fontSize: "13px",
            color: "var(--text-secondary)",
          }}
        >
          {isChinese ? "个人信息" : "Profile"}
        </h4>
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: "10px",
            marginBottom: "20px",
          }}
        >
          <div>
            <label style={labelStyle}>
              {isChinese ? "用户名" : "Username"}
            </label>
            <input
              className="form-input"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              style={inputStyle}
            />
          </div>
          <div>
            <label style={labelStyle}>{isChinese ? "邮箱" : "Email"}</label>
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <input
                className="form-input"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                style={inputStyle}
                disabled
              />
              {user?.email_verified ? (
                <span
                  style={{
                    color: "#16a34a",
                    fontSize: "12px",
                    whiteSpace: "nowrap",
                  }}
                >
                  ✓ {isChinese ? "已验证" : "Verified"}
                </span>
              ) : (
                <button
                  onClick={handleResendVerification}
                  disabled={resendingEmail}
                  style={{
                    fontSize: "11px",
                    padding: "4px 8px",
                    borderRadius: "4px",
                    border: "1px solid var(--border-subtle)",
                    background: "var(--bg-secondary)",
                    color: "var(--text-secondary)",
                    cursor: resendingEmail ? "not-allowed" : "pointer",
                    whiteSpace: "nowrap",
                  }}
                >
                  {resendingEmail ? "..." : isChinese ? "发送验证" : "Verify"}
                </button>
              )}
            </div>
            {!user?.email_verified && (
              <div
                style={{
                  fontSize: "11px",
                  color: "var(--text-tertiary)",
                  marginTop: "4px",
                }}
              >
                {isChinese
                  ? "邮箱未验证，请点击按钮发送验证邮件"
                  : "Email not verified. Click button to send verification email."}
              </div>
            )}
          </div>
          <div>
            <label style={labelStyle}>
              {isChinese ? "显示名称" : "Display Name"}
            </label>
            <input
              className="form-input"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              style={inputStyle}
            />
          </div>
          <div style={{ display: "flex", justifyContent: "flex-end" }}>
            <button
              className="btn btn-primary"
              onClick={handleSaveProfile}
              disabled={saving}
              style={{ padding: "6px 16px", fontSize: "12px" }}
            >
              {saving ? "..." : isChinese ? "保存" : "Save"}
            </button>
          </div>
        </div>
        <div
          style={{
            borderTop: "1px solid var(--border-subtle)",
            marginBottom: "20px",
          }}
        />
        {/* Password */}
        <h4
          style={{
            margin: "0 0 12px",
            fontSize: "13px",
            color: "var(--text-secondary)",
          }}
        >
          {isChinese ? "修改密码" : "Change Password"}
        </h4>
        <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
          <div>
            <label style={labelStyle}>
              {isChinese ? "当前密码" : "Current Password"}
            </label>
            <input
              className="form-input"
              type="password"
              value={oldPassword}
              onChange={(e) => setOldPassword(e.target.value)}
              style={inputStyle}
            />
          </div>
          <div>
            <label style={labelStyle}>
              {isChinese ? "新密码" : "New Password"}
            </label>
            <input
              className="form-input"
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              placeholder={isChinese ? "至少 6 个字符" : "Min 6 characters"}
              style={inputStyle}
            />
          </div>
          <div>
            <label style={labelStyle}>
              {isChinese ? "确认新密码" : "Confirm New Password"}
            </label>
            <input
              className="form-input"
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              style={inputStyle}
            />
          </div>
          <div style={{ display: "flex", justifyContent: "flex-end" }}>
            <button
              className="btn btn-primary"
              onClick={handleChangePassword}
              disabled={saving}
              style={{ padding: "6px 16px", fontSize: "12px" }}
            >
              {saving ? "..." : isChinese ? "修改密码" : "Change Password"}
            </button>
          </div>
        </div>
        {/* PAT */}
        <div
          style={{
            borderTop: "1px solid var(--border-subtle)",
            marginTop: "20px",
            paddingTop: "20px",
          }}
        >
          <PatManager />
        </div>
      </div>
    </div>
  );
}
