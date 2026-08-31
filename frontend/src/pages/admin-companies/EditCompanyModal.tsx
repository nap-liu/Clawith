import { useState } from "react";
import { useTranslation } from "react-i18next";
import { adminApi } from "../../services/api";
import { fetchJson } from "./helpers";

export default function EditCompanyModal({
  company,
  publicBaseUrl,
  onClose,
  onUpdated,
}: {
  company: any;
  publicBaseUrl: string;
  onClose: () => void;
  onUpdated: () => void;
}) {
  const { t } = useTranslation();
  const [subdomainPrefix, setSubdomainPrefix] = useState(
    company.subdomain_prefix || "",
  );
  const [prefixStatus, setPrefixStatus] = useState<
    "idle" | "checking" | "available" | "taken"
  >("idle");
  const [isDefault, setIsDefault] = useState(!!company.is_default);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const globalHostname = publicBaseUrl
    ? (() => {
        try {
          return new URL(publicBaseUrl).hostname;
        } catch {
          return "";
        }
      })()
    : "";
  const globalProtocol = publicBaseUrl
    ? (() => {
        try {
          return new URL(publicBaseUrl).protocol + "//";
        } catch {
          return "https://";
        }
      })()
    : "https://";

  const checkPrefix = async (prefix: string) => {
    if (!prefix || prefix === company.subdomain_prefix) {
      setPrefixStatus("idle");
      return;
    }
    setPrefixStatus("checking");
    try {
      const res = await fetchJson<any>(
        `/tenants/check-prefix?prefix=${encodeURIComponent(prefix)}&exclude_tenant_id=${company.id}`,
      );
      setPrefixStatus(res.available ? "available" : "taken");
    } catch {
      setPrefixStatus("idle");
    }
  };

  const handleSave = async () => {
    if (prefixStatus === "taken") {
      setError(
        t(
          "admin.prefixTakenError",
          "The subdomain prefix is already taken. Please choose a different one.",
        ),
      );
      return;
    }
    setSaving(true);
    setError("");
    try {
      const updateData: any = {
        subdomain_prefix: subdomainPrefix.trim() || null,
      };
      if (isDefault !== !!company.is_default) {
        updateData.is_default = isDefault;
      }
      await adminApi.updateCompany(company.id, updateData);
      onUpdated();
      onClose();
    } catch (e: any) {
      setError(e.message || "Failed to update");
    }
    setSaving(false);
  };

  const StatusBadge = ({
    status,
  }: {
    status: "idle" | "checking" | "available" | "taken";
  }) => {
    if (status === "checking")
      return (
        <span style={{ fontSize: "11px", color: "var(--text-tertiary)" }}>
          {t("admin.prefixChecking", "checking...")}
        </span>
      );
    if (status === "available")
      return (
        <span style={{ fontSize: "11px", color: "var(--success)" }}>
          {t("admin.prefixAvailable", "Available")}
        </span>
      );
    if (status === "taken")
      return (
        <span style={{ fontSize: "11px", color: "var(--error)" }}>
          {t("admin.prefixTaken", "Already taken")}
        </span>
      );
    return null;
  };

  const switchStyle: React.CSSProperties = {
    position: "relative",
    display: "inline-block",
    width: "40px",
    height: "22px",
    cursor: "pointer",
    flexShrink: 0,
  };
  const switchTrack = (checked: boolean): React.CSSProperties => ({
    position: "absolute",
    inset: 0,
    background: checked ? "var(--accent-primary)" : "var(--bg-tertiary)",
    borderRadius: "11px",
    transition: "background 0.2s",
  });
  const switchThumb = (checked: boolean): React.CSSProperties => ({
    position: "absolute",
    left: checked ? "20px" : "2px",
    top: "2px",
    width: "18px",
    height: "18px",
    background: "#fff",
    borderRadius: "50%",
    transition: "left 0.2s",
  });

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
            {t("admin.editCompany", "Edit Company")}: {company.name}
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

        {/* Default company toggle */}
        <div
          style={{
            marginBottom: "16px",
            padding: "12px",
            borderRadius: "8px",
            background: "var(--bg-secondary)",
            border: "1px solid var(--border-subtle)",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
            }}
          >
            <div>
              <div style={{ fontSize: "13px", fontWeight: 500 }}>
                {t("admin.setAsDefault", "Default Company")}
              </div>
              <div
                style={{
                  fontSize: "11px",
                  color: "var(--text-tertiary)",
                  marginTop: "2px",
                }}
              >
                {t(
                  "admin.setAsDefaultDesc",
                  "Users visiting the global domain will be associated with this company.",
                )}
              </div>
            </div>
            {company.is_default ? (
              <span
                style={{
                  fontSize: "11px",
                  fontWeight: 600,
                  padding: "3px 8px",
                  borderRadius: "6px",
                  background: "rgba(59,130,246,0.1)",
                  color: "var(--accent-primary)",
                  border: "1px solid rgba(59,130,246,0.2)",
                  whiteSpace: "nowrap",
                }}
              >
                {t("admin.currentDefault", "Current Default")}
              </span>
            ) : (
              <label style={switchStyle}>
                <input
                  type="checkbox"
                  checked={isDefault}
                  onChange={(e) => setIsDefault(e.target.checked)}
                  style={{ opacity: 0, width: 0, height: 0 }}
                />
                <span style={switchTrack(isDefault)}>
                  <span style={switchThumb(isDefault)} />
                </span>
              </label>
            )}
          </div>
        </div>

        {/* Domain configuration section */}
        <div
          style={{
            borderTop: "1px solid var(--border-subtle)",
            paddingTop: "16px",
            marginBottom: "16px",
          }}
        >
          <div
            style={{
              fontSize: "13px",
              fontWeight: 600,
              marginBottom: "4px",
              color: "var(--text-secondary)",
            }}
          >
            {t("admin.domainConfig", "Domain Configuration")}
          </div>
          <div
            style={{
              fontSize: "11px",
              color: "var(--text-tertiary)",
              marginBottom: "12px",
              lineHeight: "1.5",
            }}
          >
            {t(
              "admin.domainConfigDesc",
              "Configure a dedicated access domain for this company.",
            )}
          </div>

          <div
            style={{
              background: "var(--bg-secondary)",
              padding: "12px",
              borderRadius: "8px",
              border: "1px solid var(--border-subtle)",
            }}
          >
            <label
              className="form-label"
              style={{
                fontSize: "12px",
                marginBottom: "4px",
                display: "block",
              }}
            >
              {t("admin.subdomainPrefix", "Subdomain Prefix")}
            </label>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: "6px",
                flexWrap: "wrap",
              }}
            >
              <input
                className="form-input"
                value={subdomainPrefix}
                onChange={(e) => {
                  const v = e.target.value
                    .toLowerCase()
                    .replace(/[^a-z0-9-]/g, "");
                  setSubdomainPrefix(v);
                  setPrefixStatus("idle");
                }}
                onBlur={() => checkPrefix(subdomainPrefix)}
                placeholder={company.slug}
                style={{ fontSize: "13px", maxWidth: "120px" }}
              />
              {globalHostname && (
                <span
                  style={{ fontSize: "12px", color: "var(--text-tertiary)" }}
                >
                  .{globalHostname}
                </span>
              )}
              <StatusBadge status={prefixStatus} />
            </div>
            {(subdomainPrefix || company.slug) && globalHostname && (
              <div
                style={{
                  fontSize: "11px",
                  color: "var(--text-tertiary)",
                  marginTop: "6px",
                  fontFamily: "var(--font-mono)",
                }}
              >
                {globalProtocol}
                {subdomainPrefix || company.slug}.{globalHostname}
              </div>
            )}
          </div>
        </div>

        {error && (
          <div
            style={{
              color: "var(--error)",
              fontSize: "12px",
              marginBottom: "12px",
            }}
          >
            {error}
          </div>
        )}

        <div style={{ display: "flex", gap: "8px" }}>
          <button
            className="btn btn-secondary"
            style={{ flex: 1 }}
            onClick={onClose}
            disabled={saving}
          >
            {t("common.cancel", "Cancel")}
          </button>
          <button
            className="btn btn-primary"
            style={{ flex: 1 }}
            onClick={handleSave}
            disabled={saving || prefixStatus === "taken"}
          >
            {saving ? t("common.loading") : t("common.save", "Save")}
          </button>
        </div>
      </div>
    </div>
  );
}
