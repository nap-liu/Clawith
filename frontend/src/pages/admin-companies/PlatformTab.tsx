import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { adminApi } from "../../services/api";
import { saveAccentColor, getSavedAccentColor } from "../../utils/theme";
import { fetchJson } from "./helpers";
import PlatformGeneralSettings from "./PlatformGeneralSettings";
import PlatformEmailSettings from "./PlatformEmailSettings";

export default function PlatformTab() {
  const { t } = useTranslation();
  const socialProviderMeta = {
    google: {
      name: "Google",
      scope: "openid profile email",
      authorizeLabel: t(
        "admin.oauth.authorizedRedirectUri",
        "Authorized redirect URI",
      ),
    },
    github: {
      name: "GitHub",
      scope: "read:user user:email",
      authorizeLabel: t(
        "admin.oauth.authorizationCallbackUrl",
        "Authorization callback URL",
      ),
    },
  } as const;

  // Platform settings toggles
  const [settings, setSettings] = useState<any>({});
  const [settingsLoading, setSettingsLoading] = useState(false);

  // Notification bar
  const [nbEnabled, setNbEnabled] = useState(false);
  const [nbText, setNbText] = useState("");
  const [nbSaving, setNbSaving] = useState(false);
  const [nbSaved, setNbSaved] = useState(false);

  // Public URL
  const [publicBaseUrl, setPublicBaseUrl] = useState("");
  const [urlSaving, setUrlSaving] = useState(false);
  const [urlSaved, setUrlSaved] = useState(false);

  // System email configuration
  const [systemEmailConfig, setSystemEmailConfig] = useState({
    SYSTEM_EMAIL_ENABLED: false,
    SYSTEM_EMAIL_FROM_ADDRESS: "",
    SYSTEM_EMAIL_FROM_NAME: t(
      "enterprise.systemEmail.defaultSenderName",
      "Digital Employee Platform",
    ),
    SYSTEM_SMTP_HOST: "",
    SYSTEM_SMTP_PORT: 465,
    SYSTEM_SMTP_USERNAME: "",
    SYSTEM_SMTP_PASSWORD: "",
    SYSTEM_SMTP_SSL: true,
    SYSTEM_SMTP_TIMEOUT_SECONDS: 15,
  });
  const [emailConfigSaving, setEmailConfigSaving] = useState(false);
  const [emailConfigSaved, setEmailConfigSaved] = useState(false);

  // Test email
  const [showTestEmail, setShowTestEmail] = useState(false);
  const [testEmailAddr, setTestEmailAddr] = useState("");
  const [testEmailSending, setTestEmailSending] = useState(false);
  const [testEmailResult, setTestEmailResult] = useState<{
    ok: boolean;
    msg: string;
  } | null>(null);

  // Email templates
  const [emailTemplates, setEmailTemplates] = useState<
    Record<string, { subject: string; body: string }>
  >({
    email_verification: { subject: "", body: "" },
    password_reset: { subject: "", body: "" },
    company_invitation: { subject: "", body: "" },
  });
  const [emailTemplateVars, setEmailTemplateVars] = useState<
    Record<string, string[]>
  >({});
  const [emailTemplateDefaults, setEmailTemplateDefaults] = useState<
    Record<string, { subject: string; body: string }>
  >({});
  const [templatesSaving, setTemplatesSaving] = useState(false);
  const [templatesSaved, setTemplatesSaved] = useState(false);
  const [expandedTemplate, setExpandedTemplate] = useState<string | null>(null);
  const [oauthProviders, setOauthProviders] = useState<Record<string, any>>({});
  const [oauthSaving, setOauthSaving] = useState<Record<string, boolean>>({});

  // Toast
  const [toast, setToast] = useState<{
    msg: string;
    type: "success" | "error";
  } | null>(null);
  const showToast = (msg: string, type: "success" | "error" = "success") => {
    setToast({ msg, type });
    setTimeout(() => setToast(null), 3000);
  };

  useEffect(() => {
    // Load platform toggles
    adminApi
      .getPlatformSettings()
      .then(setSettings)
      .catch(() => {});
    // Load notification bar
    const token = localStorage.getItem("token");
    fetch("/api/enterprise/system-settings/notification_bar", {
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
    })
      .then((r) => r.json())
      .then((d) => {
        if (d?.value) {
          setNbEnabled(!!d.value.enabled);
          setNbText(d.value.text || "");
        }
      })
      .catch(() => {});
    // Load Public URL
    fetchJson<any>("/enterprise/system-settings/platform")
      .then((d) => {
        if (d.value?.public_base_url) setPublicBaseUrl(d.value.public_base_url);
      })
      .catch(() => {});

    // Load System Email
    fetchJson<any>("/enterprise/system-settings/system_email_platform")
      .then((d) => {
        if (d?.value) {
          setSystemEmailConfig({
            SYSTEM_EMAIL_ENABLED:
              d.value.SYSTEM_EMAIL_ENABLED !== undefined
                ? !!d.value.SYSTEM_EMAIL_ENABLED
                : !!(
                    d.value.SYSTEM_EMAIL_FROM_ADDRESS &&
                    d.value.SYSTEM_SMTP_HOST
                  ),
            SYSTEM_EMAIL_FROM_ADDRESS: d.value.SYSTEM_EMAIL_FROM_ADDRESS || "",
            SYSTEM_EMAIL_FROM_NAME:
              d.value.SYSTEM_EMAIL_FROM_NAME ||
              t(
                "enterprise.systemEmail.defaultSenderName",
                "Digital Employee Platform",
              ),
            SYSTEM_SMTP_HOST: d.value.SYSTEM_SMTP_HOST || "",
            SYSTEM_SMTP_PORT: d.value.SYSTEM_SMTP_PORT || 465,
            SYSTEM_SMTP_USERNAME: d.value.SYSTEM_SMTP_USERNAME || "",
            SYSTEM_SMTP_PASSWORD: d.value.SYSTEM_SMTP_PASSWORD || "",
            SYSTEM_SMTP_SSL:
              d.value.SYSTEM_SMTP_SSL !== undefined
                ? d.value.SYSTEM_SMTP_SSL
                : true,
            SYSTEM_SMTP_TIMEOUT_SECONDS:
              d.value.SYSTEM_SMTP_TIMEOUT_SECONDS || 15,
          });
        }
      })
      .catch(() => {});

    // Load email templates
    fetchJson<any>("/enterprise/email-templates")
      .then((d) => {
        if (d.templates) setEmailTemplates(d.templates);
        if (d.variables) setEmailTemplateVars(d.variables);
        if (d.defaults) setEmailTemplateDefaults(d.defaults);
      })
      .catch(() => {});

    fetchJson<any[]>("/enterprise/identity-providers?global_only=true")
      .then((items) => {
        const mapped = items.reduce(
          (acc, item) => {
            if (
              item.provider_type === "google" ||
              item.provider_type === "github"
            ) {
              acc[item.provider_type] = {
                id: item.id,
                provider_type: item.provider_type,
                name:
                  item.name ||
                  socialProviderMeta[item.provider_type as "google" | "github"]
                    .name,
                is_active: !!item.is_active,
                config: item.config || {},
                client_id: item.config?.client_id || item.config?.app_id || "",
                client_secret:
                  item.config?.client_secret || item.config?.app_secret || "",
                scope:
                  item.config?.scope ||
                  socialProviderMeta[item.provider_type as "google" | "github"]
                    .scope,
              };
            }
            return acc;
          },
          {} as Record<string, any>,
        );

        setOauthProviders({
          google: mapped.google || {
            provider_type: "google",
            name: socialProviderMeta.google.name,
            is_active: false,
            client_id: "",
            client_secret: "",
            scope: socialProviderMeta.google.scope,
          },
          github: mapped.github || {
            provider_type: "github",
            name: socialProviderMeta.github.name,
            is_active: false,
            client_id: "",
            client_secret: "",
            scope: socialProviderMeta.github.scope,
          },
        });
      })
      .catch(() => {});
  }, [t]);

  const handleToggleSetting = async (key: string, value: boolean) => {
    setSettingsLoading(true);
    try {
      await adminApi.updatePlatformSettings({ [key]: value });
      setSettings((s: any) => ({ ...s, [key]: value }));
      showToast(t("admin.settingUpdated", "Setting updated"));
    } catch (e: any) {
      showToast(
        e.message || t("admin.settingsUpdateFailed", "Failed to update setting"),
        "error",
      );
    }
    setSettingsLoading(false);
  };

  const saveNotificationBar = async (
    nextEnabled = nbEnabled,
    nextText = nbText,
  ) => {
    setNbSaving(true);
    try {
      const token = localStorage.getItem("token");
      const res = await fetch(
        "/api/enterprise/system-settings/notification_bar",
        {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
          },
          body: JSON.stringify({
            value: { enabled: nextEnabled, text: nextText },
          }),
        },
      );
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || res.statusText);
      }
      const payload = await res.json();
      setNbEnabled(nextEnabled);
      setNbText(nextText);
      window.dispatchEvent(
        new CustomEvent("notification-bar-updated", {
          detail: {
            enabled: nextEnabled,
            text: nextText,
            updated_at: payload?.updated_at || null,
          },
        }),
      );
      setNbSaved(true);
      setTimeout(() => setNbSaved(false), 2000);
      return true;
    } catch (e: any) {
      showToast(e.message || t("common.saveFailed", "Save failed"), "error");
      return false;
    } finally {
      setNbSaving(false);
    }
  };

  const handleNotificationBarToggle = async (nextEnabled: boolean) => {
    const previousEnabled = nbEnabled;
    setNbEnabled(nextEnabled);
    const saved = await saveNotificationBar(nextEnabled, nbText);
    if (!saved) setNbEnabled(previousEnabled);
  };

  const savePublicUrl = async () => {
    setUrlSaving(true);
    try {
      await fetchJson("/enterprise/system-settings/platform", {
        method: "PUT",
        body: JSON.stringify({ value: { public_base_url: publicBaseUrl } }),
      });
      setUrlSaved(true);
      setTimeout(() => setUrlSaved(false), 2000);
    } catch (e) {
      showToast(t("common.saveFailed", "Save failed"), "error");
    }
    setUrlSaving(false);
  };

  const saveEmailConfig = async () => {
    setEmailConfigSaving(true);
    try {
      await fetchJson("/enterprise/system-settings/system_email_platform", {
        method: "PUT",
        body: JSON.stringify({ value: systemEmailConfig }),
      });
      setEmailConfigSaved(true);
      setTimeout(() => setEmailConfigSaved(false), 2000);
      showToast(
        t("enterprise.systemEmail.saved", "Email configuration saved"),
      );
    } catch (e: any) {
      showToast(
        t(
          "enterprise.systemEmail.saveFailed",
          "Failed to save email configuration: {{reason}}",
          {
            reason:
              e.message || t("common.unknownError", "Unknown error"),
          },
        ),
        "error",
      );
    } finally {
      setEmailConfigSaving(false);
    }
  };

  const handleSendTestEmail = async () => {
    if (!testEmailAddr.trim()) return;
    setTestEmailSending(true);
    setTestEmailResult(null);
    try {
      await fetchJson("/enterprise/system-email/test", {
        method: "POST",
        body: JSON.stringify({ email: testEmailAddr }),
      });
      setTestEmailResult({
        ok: true,
        msg: t(
          "enterprise.systemEmail.testSuccess",
          "Test email sent successfully!",
        ),
      });
    } catch (e: any) {
      setTestEmailResult({
        ok: false,
        msg:
          e.message ||
          t(
            "enterprise.systemEmail.testFailed",
            "Failed to send test email",
          ),
      });
    }
    setTestEmailSending(false);
  };

  const saveEmailTemplates = async () => {
    setTemplatesSaving(true);
    try {
      await fetchJson("/enterprise/email-templates", {
        method: "PUT",
        body: JSON.stringify({ templates: emailTemplates }),
      });
      setTemplatesSaved(true);
      setTimeout(() => setTemplatesSaved(false), 2000);
      showToast(t("enterprise.emailTemplates.saved", "Email templates saved"));
    } catch (e: any) {
      showToast(
        e.message ||
          t(
            "enterprise.emailTemplates.saveFailed",
            "Failed to save email templates",
          ),
        "error",
      );
    }
    setTemplatesSaving(false);
  };

  const resetTemplate = (key: string) => {
    if (emailTemplateDefaults[key]) {
      setEmailTemplates((prev) => ({
        ...prev,
        [key]: { ...emailTemplateDefaults[key] },
      }));
    }
  };

  const insertVariable = (
    scenarioKey: string,
    field: "subject" | "body",
    varName: string,
  ) => {
    const placeholder = `{{${varName}}}`;
    // Append placeholder to end of the field
    setEmailTemplates((prev) => ({
      ...prev,
      [scenarioKey]: {
        ...prev[scenarioKey],
        [field]: (prev[scenarioKey]?.[field] || "") + placeholder,
      },
    }));
  };

  const setOauthField = (
    providerType: "google" | "github",
    key: string,
    value: string | boolean,
  ) => {
    setOauthProviders((prev) => ({
      ...prev,
      [providerType]: {
        ...prev[providerType],
        [key]: value,
      },
    }));
  };

  const saveOauthProvider = async (providerType: "google" | "github") => {
    const provider = oauthProviders[providerType];
    if (!provider?.client_id?.trim() || !provider?.client_secret?.trim()) {
      showToast(
        t(
          "admin.oauth.credentialsRequired",
          "{{provider}} client ID and client secret are required",
          { provider: socialProviderMeta[providerType].name },
        ),
        "error",
      );
      return;
    }

    setOauthSaving((prev) => ({ ...prev, [providerType]: true }));
    const payload = {
      provider_type: providerType,
      name: socialProviderMeta[providerType].name,
      is_active: !!provider.is_active,
      config: {
        app_id: provider.client_id.trim(),
        client_id: provider.client_id.trim(),
        app_secret: provider.client_secret.trim(),
        client_secret: provider.client_secret.trim(),
        scope: provider.scope?.trim() || socialProviderMeta[providerType].scope,
      },
    };

    try {
      const result = provider.id
        ? await fetchJson<any>(
            `/enterprise/identity-providers/${provider.id}`,
            {
              method: "PUT",
              body: JSON.stringify({
                name: payload.name,
                is_active: payload.is_active,
                config: payload.config,
              }),
            },
          )
        : await fetchJson<any>("/enterprise/identity-providers", {
            method: "POST",
            body: JSON.stringify(payload),
          });

      setOauthProviders((prev) => ({
        ...prev,
        [providerType]: {
          ...prev[providerType],
          id: result.id,
          is_active: !!result.is_active,
        },
      }));
      showToast(
        t("admin.oauth.saved", "{{provider}} OAuth settings saved", {
          provider: socialProviderMeta[providerType].name,
        }),
      );
    } catch (e: any) {
      showToast(
        e.message ||
          t(
            "admin.oauth.saveFailed",
            "Failed to save {{provider}} OAuth settings",
            { provider: socialProviderMeta[providerType].name },
          ),
        "error",
      );
    } finally {
      setOauthSaving((prev) => ({ ...prev, [providerType]: false }));
    }
  };

  const switchStyle = (
    checked: boolean,
    disabled?: boolean,
  ): React.CSSProperties => ({
    position: "relative",
    display: "inline-block",
    width: "40px",
    height: "22px",
    cursor: disabled ? "not-allowed" : "pointer",
    flexShrink: 0,
  });
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
    <>
      {toast && (
        <div
          style={{
            position: "fixed",
            top: "20px",
            right: "20px",
            padding: "10px 20px",
            borderRadius: "8px",
            background:
              toast.type === "success" ? "var(--success)" : "var(--error)",
            color: "#fff",
            fontSize: "13px",
            zIndex: 9999,
            boxShadow: "0 4px 12px rgba(0,0,0,0.2)",
          }}
        >
          {toast.msg}
        </div>
      )}

      <PlatformGeneralSettings
        t={t}
        settings={settings}
        settingsLoading={settingsLoading}
        handleToggleSetting={handleToggleSetting}
        switchStyle={switchStyle}
        switchTrack={switchTrack}
        switchThumb={switchThumb}
        socialProviderMeta={socialProviderMeta}
        oauthProviders={oauthProviders}
        setOauthField={setOauthField}
        saveOauthProvider={saveOauthProvider}
        oauthSaving={oauthSaving}
        publicBaseUrl={publicBaseUrl}
        nbEnabled={nbEnabled}
        nbSaving={nbSaving}
        handleNotificationBarToggle={handleNotificationBarToggle}
        nbText={nbText}
        setNbText={setNbText}
        saveNotificationBar={saveNotificationBar}
        nbSaved={nbSaved}
        setPublicBaseUrl={setPublicBaseUrl}
        savePublicUrl={savePublicUrl}
        urlSaving={urlSaving}
        urlSaved={urlSaved}
      />


      <PlatformEmailSettings
        t={t}
        systemEmailConfig={systemEmailConfig}
        setSystemEmailConfig={setSystemEmailConfig}
        emailConfigSaving={emailConfigSaving}
        saveEmailConfig={saveEmailConfig}
        emailConfigSaved={emailConfigSaved}
        showTestEmail={showTestEmail}
        setShowTestEmail={setShowTestEmail}
        testEmailAddr={testEmailAddr}
        setTestEmailAddr={setTestEmailAddr}
        testEmailSending={testEmailSending}
        handleSendTestEmail={handleSendTestEmail}
        testEmailResult={testEmailResult}
        setTestEmailResult={setTestEmailResult}
        emailTemplates={emailTemplates}
        expandedTemplate={expandedTemplate}
        setExpandedTemplate={setExpandedTemplate}
        emailTemplateVars={emailTemplateVars}
        setEmailTemplates={setEmailTemplates}
        resetTemplate={resetTemplate}
        templatesSaving={templatesSaving}
        saveEmailTemplates={saveEmailTemplates}
        templatesSaved={templatesSaved}
      />
    </>
  );
}
