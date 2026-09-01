import LinearCopyButton from "../../components/LinearCopyButton";
import EditCompanyModal from "./EditCompanyModal";

export default function CompanyManagementModals(props: any) {
  const {
    t,
    editingCompany,
    publicBaseUrl,
    setEditingCompany,
    loadCompanies,
    deleteConfirmCompany,
    setDeleteConfirmCompany,
    handleDelete,
    deletingCompanyId,
    createdCode,
    createdCompanyName,
    setCreatedCode,
    codesModal,
    setCodesModal,
    newlyGeneratedCode,
    setNewlyGeneratedCode,
    handleGenerateCode,
    loadingCodes,
    companyCodes,
    codeCopied2,
    setCodeCopied2,
  } = props;

  return (
    <>
      {/* Edit Company Modal */}
      {editingCompany && (
        <EditCompanyModal
          company={editingCompany}
          publicBaseUrl={publicBaseUrl}
          onClose={() => setEditingCompany(null)}
          onUpdated={() => {
            loadCompanies();
            setEditingCompany(null);
          }}
        />
      )}

      {/* Delete Confirmation Modal */}
      {deleteConfirmCompany && (
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
          onClick={() => setDeleteConfirmCompany(null)}
        >
          <div
            className="card"
            style={{
              padding: "24px",
              maxWidth: "420px",
              width: "90%",
              boxShadow: "0 20px 60px rgba(0,0,0,0.3)",
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <h2
              style={{
                fontSize: "16px",
                fontWeight: 600,
                marginBottom: "8px",
                color: "var(--error)",
              }}
            >
              {t("admin.deleteCompany", "Delete Company")}
            </h2>
            <p
              style={{
                fontSize: "13px",
                color: "var(--text-secondary)",
                marginBottom: "8px",
              }}
            >
              {t(
                "admin.deleteCompanyWarning",
                "This action is irreversible. The following data will be permanently deleted:",
              )}
            </p>
            <ul
              style={{
                fontSize: "12px",
                color: "var(--text-tertiary)",
                marginBottom: "16px",
                paddingLeft: "16px",
                lineHeight: "1.8",
              }}
            >
              <li>{t("admin.deleteDataUsers", "All users in this company")}</li>
              <li>
                {t(
                  "admin.deleteDataAgents",
                  "All agents and their conversations",
                )}
              </li>
              <li>
                {t(
                  "admin.deleteDataSkills",
                  "All skills and LLM model configurations",
                )}
              </li>
              <li>
                {t(
                  "admin.deleteDataOther",
                  "All invitation codes, org structure, token records",
                )}
              </li>
            </ul>
            <div
              style={{
                padding: "12px",
                borderRadius: "8px",
                marginBottom: "16px",
                background: "rgba(239,68,68,0.06)",
                border: "1px solid rgba(239,68,68,0.2)",
                fontSize: "13px",
                fontWeight: 500,
              }}
            >
              {t("admin.deleteCompanyName", "Company")}:{" "}
              <strong>{deleteConfirmCompany.name}</strong>
              <div
                style={{
                  fontSize: "11px",
                  color: "var(--text-tertiary)",
                  fontFamily: "monospace",
                  marginTop: "2px",
                }}
              >
                {deleteConfirmCompany.user_count} {t("admin.users", "users")}{" "}
                &middot; {deleteConfirmCompany.agent_count}{" "}
                {t("admin.agents", "agents")}
              </div>
            </div>
            <div style={{ display: "flex", gap: "8px" }}>
              <button
                className="btn btn-secondary"
                style={{ flex: 1 }}
                onClick={() => setDeleteConfirmCompany(null)}
                disabled={!!deletingCompanyId}
              >
                {t("common.cancel", "Cancel")}
              </button>
              <button
                className="btn"
                style={{
                  flex: 1,
                  background: "var(--error)",
                  color: "#fff",
                  border: "none",
                  borderRadius: "var(--radius)",
                  cursor: "pointer",
                  opacity: deletingCompanyId ? 0.6 : 1,
                }}
                onClick={() => handleDelete(deleteConfirmCompany)}
                disabled={!!deletingCompanyId}
              >
                {deletingCompanyId === deleteConfirmCompany.id
                  ? t("common.loading", "Loading...")
                  : t("admin.confirmDelete", "Yes, Delete")}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Invitation Code Modal */}
      {createdCode && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.5)",
            zIndex: 10000,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            backdropFilter: "blur(4px)",
          }}
          onClick={() => setCreatedCode("")}
        >
          <div
            className="card"
            style={{
              padding: "32px",
              maxWidth: "480px",
              width: "90%",
              boxShadow: "0 20px 60px rgba(0,0,0,0.3)",
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div style={{ textAlign: "center", marginBottom: "20px" }}>
              <div
                style={{
                  width: "48px",
                  height: "48px",
                  borderRadius: "50%",
                  background: "rgba(34,197,94,0.1)",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  margin: "0 auto 12px",
                  fontSize: "20px",
                }}
              >
                <svg
                  width="24"
                  height="24"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="#22c55e"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
                  <polyline points="22 4 12 14.01 9 11.01" />
                </svg>
              </div>
              <h2
                style={{
                  fontSize: "18px",
                  fontWeight: 600,
                  marginBottom: "4px",
                }}
              >
                {t("admin.companyCreated", "Company Created")}
              </h2>
              <p style={{ fontSize: "13px", color: "var(--text-tertiary)" }}>
                <span style={{ fontWeight: 500, color: "var(--text-primary)" }}>
                  {createdCompanyName}
                </span>{" "}
                {t(
                  "admin.companyCreatedDesc",
                  "has been created successfully.",
                )}
              </p>
            </div>

            <div
              style={{
                padding: "16px",
                borderRadius: "8px",
                background: "var(--bg-secondary)",
                border: "1px solid var(--border-subtle)",
                marginBottom: "16px",
              }}
            >
              <div
                style={{
                  fontSize: "12px",
                  fontWeight: 600,
                  color: "var(--text-secondary)",
                  marginBottom: "8px",
                }}
              >
                {t("admin.inviteCodeLabel", "Admin Invitation Code")}
              </div>
              <div
                style={{
                  fontFamily: "monospace",
                  fontSize: "22px",
                  fontWeight: 700,
                  letterSpacing: "3px",
                  color: "var(--success)",
                  textAlign: "center",
                  padding: "8px 0",
                  userSelect: "all",
                }}
              >
                {createdCode}
              </div>
            </div>

            <div
              style={{
                fontSize: "12px",
                color: "var(--text-tertiary)",
                lineHeight: "1.6",
                marginBottom: "20px",
                padding: "12px",
                borderRadius: "6px",
                background: "rgba(59,130,246,0.06)",
                border: "1px solid rgba(59,130,246,0.12)",
              }}
            >
              <div
                style={{
                  fontWeight: 600,
                  color: "var(--text-secondary)",
                  marginBottom: "4px",
                }}
              >
                {t("admin.inviteCodeHowTo", "How to use this code:")}
              </div>
              {t(
                "admin.inviteCodeExplain",
                "Send this code to the person who will manage this company. They should register a new account on the platform, then enter this code to join. The first person to use it will automatically become the Org Admin of this company. This code is single-use.",
              )}
            </div>

            <div style={{ display: "flex", gap: "8px" }}>
              <LinearCopyButton
                className="btn btn-primary"
                style={{ flex: 1, height: "36px" }}
                textToCopy={createdCode}
                label={t("admin.copyCode", "Copy Code")}
                copiedLabel={t("admin.copied", "Copied")}
              />
              <button
                className="btn btn-secondary"
                onClick={() => setCreatedCode("")}
                style={{ height: "36px", padding: "0 20px" }}
              >
                {t("common.close", "Close")}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Invitation Codes Modal */}
      {codesModal && (
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
          onClick={() => {
            setCodesModal(null);
            setNewlyGeneratedCode("");
          }}
        >
          <div
            className="card"
            style={{
              padding: "24px",
              maxWidth: "480px",
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
                marginBottom: "16px",
              }}
            >
              <h2 style={{ fontSize: "16px", fontWeight: 600 }}>
                邀请码 — {codesModal.companyName}
              </h2>
              <button
                onClick={() => {
                  setCodesModal(null);
                  setNewlyGeneratedCode("");
                }}
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

            {newlyGeneratedCode && (
              <div
                style={{
                  padding: "12px 16px",
                  borderRadius: "8px",
                  background: "rgba(34,197,94,0.06)",
                  border: "1px solid rgba(34,197,94,0.2)",
                  marginBottom: "16px",
                }}
              >
                <div
                  style={{
                    fontSize: "11px",
                    fontWeight: 600,
                    color: "var(--text-secondary)",
                    marginBottom: "6px",
                  }}
                >
                  新生成的邀请码
                </div>
                <div
                  style={{ display: "flex", alignItems: "center", gap: "8px" }}
                >
                  <span
                    style={{
                      fontFamily: "monospace",
                      fontSize: "18px",
                      fontWeight: 700,
                      letterSpacing: "2px",
                      color: "var(--success)",
                      userSelect: "all",
                    }}
                  >
                    {newlyGeneratedCode}
                  </span>
                  <button
                    className="btn btn-secondary"
                    style={{
                      padding: "2px 8px",
                      fontSize: "11px",
                      height: "24px",
                    }}
                    onClick={() => {
                      navigator.clipboard
                        .writeText(newlyGeneratedCode)
                        .then(() => {
                          setCodeCopied2(true);
                          setTimeout(() => setCodeCopied2(false), 2000);
                        });
                    }}
                  >
                    {codeCopied2 ? "已复制" : "复制"}
                  </button>
                </div>
              </div>
            )}

            <div style={{ marginBottom: "12px" }}>
              <div
                style={{
                  fontSize: "12px",
                  fontWeight: 600,
                  color: "var(--text-secondary)",
                  marginBottom: "8px",
                }}
              >
                有效邀请码
              </div>
              {loadingCodes ? (
                <div
                  style={{
                    fontSize: "12px",
                    color: "var(--text-tertiary)",
                    padding: "12px 0",
                  }}
                >
                  加载中...
                </div>
              ) : companyCodes.length === 0 ? (
                <div
                  style={{
                    fontSize: "12px",
                    color: "var(--text-tertiary)",
                    padding: "12px 0",
                  }}
                >
                  暂无有效邀请码
                </div>
              ) : (
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: "6px",
                    maxHeight: "200px",
                    overflowY: "auto",
                  }}
                >
                  {companyCodes.map((c: any) => (
                    <div
                      key={c.id}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        padding: "8px 12px",
                        borderRadius: "6px",
                        background: "var(--bg-secondary)",
                        border: "1px solid var(--border-subtle)",
                        fontSize: "12px",
                      }}
                    >
                      <span
                        style={{
                          fontFamily: "monospace",
                          fontWeight: 600,
                          letterSpacing: "1px",
                        }}
                      >
                        {c.code}
                      </span>
                      <span
                        style={{
                          color: "var(--text-tertiary)",
                          fontSize: "11px",
                        }}
                      >
                        {c.used_count}/{c.max_uses === 0 ? "∞" : c.max_uses}{" "}
                        次使用
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div style={{ display: "flex", gap: "8px" }}>
              <button
                className="btn btn-primary"
                onClick={handleGenerateCode}
                style={{ flex: 1 }}
              >
                生成新邀请码
              </button>
              <button
                className="btn btn-secondary"
                onClick={() => {
                  setCodesModal(null);
                  setNewlyGeneratedCode("");
                }}
                style={{ padding: "0 20px" }}
              >
                关闭
              </button>
            </div>
          </div>
        </div>
      )}

    </>
  );
}
