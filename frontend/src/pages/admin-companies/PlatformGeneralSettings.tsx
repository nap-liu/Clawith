import LinearCopyButton from "../../components/LinearCopyButton";

export default function PlatformGeneralSettings(props: any) {
  const {
    t,
    settings,
    settingsLoading,
    handleToggleSetting,
    switchStyle,
    switchTrack,
    switchThumb,
    socialProviderMeta,
    oauthProviders,
    setOauthField,
    saveOauthProvider,
    oauthSaving,
    publicBaseUrl,
    nbEnabled,
    nbSaving,
    handleNotificationBarToggle,
    nbText,
    setNbText,
    saveNotificationBar,
    nbSaved,
    setPublicBaseUrl,
    savePublicUrl,
    urlSaving,
    urlSaved,
  } = props;

  return (
    <>
      {/* Allow self-create company and SSO redirect toggle */}
      <div className="card" style={{ padding: "16px", marginBottom: "16px" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
          {[
            {
              key: "allow_self_create_company",
              label: t(
                "admin.allowSelfCreate",
                "Allow users to create their own companies",
              ),
              desc: t(
                "admin.allowSelfCreateDesc",
                "When disabled, only platform admins can create companies.",
              ),
            },
            {
              key: "sso_custom_domain_redirect_enabled",
              label: t(
                "admin.ssoCustomDomainRedirect",
                "Enable tenant SSO custom domain redirect",
              ),
              desc: t(
                "admin.ssoCustomDomainRedirectDesc",
                "When disabled, all tenants will be blocked from using custom domains or dedicated links to redirect to SSO providers.",
              ),
            },
          ].map((s) => (
            <div
              key={s.key}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "8px 0",
              }}
            >
              <div>
                <div style={{ fontSize: "13px", fontWeight: 500 }}>
                  {s.label}
                </div>
                <div
                  style={{
                    fontSize: "11px",
                    color: "var(--text-tertiary)",
                    marginTop: "2px",
                  }}
                >
                  {s.desc}
                </div>
              </div>
              <label style={switchStyle(!!settings[s.key], settingsLoading)}>
                <input
                  type="checkbox"
                  checked={!!settings[s.key]}
                  onChange={(e) => handleToggleSetting(s.key, e.target.checked)}
                  disabled={settingsLoading}
                  style={{ opacity: 0, width: 0, height: 0 }}
                />
                <span style={switchTrack(!!settings[s.key])}>
                  <span style={switchThumb(!!settings[s.key])} />
                </span>
              </label>
            </div>
          ))}
        </div>
      </div>

      <div className="card" style={{ padding: "16px", marginBottom: "16px" }}>
        <div
          style={{
            fontSize: "13px",
            fontWeight: 600,
            marginBottom: "4px",
            color: "var(--text-secondary)",
          }}
        >
          OAuth Login
        </div>
        <p
          style={{
            fontSize: "12px",
            color: "var(--text-tertiary)",
            marginBottom: "16px",
          }}
        >
          Configure platform-wide social login providers. Once enabled, users
          can sign in from the main login page with Google or GitHub.
        </p>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))",
            gap: "16px",
          }}
        >
          {(["google", "github"] as const).map((providerType) => {
            const provider = oauthProviders[providerType];
            const meta = socialProviderMeta[providerType];
            const callbackUrl = `${window.location.origin}/oauth/callback/${providerType}`;
            return (
              <div
                key={providerType}
                style={{
                  border: "1px solid var(--border-subtle)",
                  borderRadius: "12px",
                  padding: "16px",
                  background: "var(--bg-secondary)",
                }}
              >
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    marginBottom: "12px",
                  }}
                >
                  <div>
                    <div style={{ fontSize: "14px", fontWeight: 600 }}>
                      {meta.name}
                    </div>
                    <div
                      style={{
                        fontSize: "11px",
                        color: "var(--text-tertiary)",
                        marginTop: "4px",
                      }}
                    >
                      Scope: {provider?.scope || meta.scope}
                    </div>
                  </div>
                  <label
                    style={switchStyle(
                      !!provider?.is_active,
                      oauthSaving[providerType],
                    )}
                  >
                    <input
                      type="checkbox"
                      checked={!!provider?.is_active}
                      onChange={(e) =>
                        setOauthField(
                          providerType,
                          "is_active",
                          e.target.checked,
                        )
                      }
                      disabled={!!oauthSaving[providerType]}
                      style={{ opacity: 0, width: 0, height: 0 }}
                    />
                    <span style={switchTrack(!!provider?.is_active)}>
                      <span style={switchThumb(!!provider?.is_active)} />
                    </span>
                  </label>
                </div>

                <div style={{ display: "grid", gap: "12px" }}>
                  <div>
                    <label
                      className="form-label"
                      style={{ fontSize: "12px", marginBottom: "6px" }}
                    >
                      Client ID
                    </label>
                    <input
                      className="form-input"
                      value={provider?.client_id || ""}
                      onChange={(e) =>
                        setOauthField(providerType, "client_id", e.target.value)
                      }
                      placeholder={
                        providerType === "google"
                          ? "xxxxxxxx.apps.googleusercontent.com"
                          : "GitHub OAuth App Client ID"
                      }
                      style={{ fontSize: "13px" }}
                    />
                  </div>
                  <div>
                    <label
                      className="form-label"
                      style={{ fontSize: "12px", marginBottom: "6px" }}
                    >
                      Client Secret
                    </label>
                    <input
                      className="form-input"
                      type="password"
                      value={provider?.client_secret || ""}
                      onChange={(e) =>
                        setOauthField(
                          providerType,
                          "client_secret",
                          e.target.value,
                        )
                      }
                      placeholder={`${meta.name} Client Secret`}
                      style={{ fontSize: "13px" }}
                    />
                  </div>
                  <div>
                    <label
                      className="form-label"
                      style={{ fontSize: "12px", marginBottom: "6px" }}
                    >
                      Scope
                    </label>
                    <input
                      className="form-input"
                      value={provider?.scope || meta.scope}
                      onChange={(e) =>
                        setOauthField(providerType, "scope", e.target.value)
                      }
                      style={{ fontSize: "13px" }}
                    />
                  </div>
                  <div
                    style={{
                      padding: "10px 12px",
                      borderRadius: "8px",
                      background: "var(--bg-primary)",
                      border: "1px solid var(--border-subtle)",
                    }}
                  >
                    <div
                      style={{
                        fontSize: "11px",
                        color: "var(--text-tertiary)",
                        marginBottom: "6px",
                      }}
                    >
                      {meta.authorizeLabel}
                    </div>
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: "8px",
                      }}
                    >
                      <code
                        style={{
                          flex: 1,
                          fontSize: "11px",
                          wordBreak: "break-all",
                          color: "var(--text-primary)",
                        }}
                      >
                        {callbackUrl}
                      </code>
                      <LinearCopyButton
                        className="btn btn-ghost"
                        style={{
                          fontSize: "11px",
                          padding: "4px 8px",
                          whiteSpace: "nowrap",
                        }}
                        textToCopy={callbackUrl}
                        label="Copy"
                        copiedLabel="Copied"
                      />
                    </div>
                  </div>
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: "8px",
                    }}
                  >
                    <button
                      className="btn btn-primary"
                      onClick={() => saveOauthProvider(providerType)}
                      disabled={!!oauthSaving[providerType]}
                    >
                      {oauthSaving[providerType]
                        ? t("common.loading")
                        : t("common.save", "Save")}
                    </button>
                    {provider?.id && (
                      <span
                        style={{
                          fontSize: "11px",
                          color: "var(--text-tertiary)",
                        }}
                      >
                        {provider.is_active
                          ? "Enabled on login page"
                          : "Saved but disabled"}
                      </span>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Notification Bar */}
      <div className="card" style={{ padding: "16px", marginBottom: "16px" }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          <div>
            <div
              style={{
                fontSize: "13px",
                fontWeight: 600,
                color: "var(--text-secondary)",
              }}
            >
              {t("enterprise.notificationBar.title", "Notification Bar")}
            </div>
            <div
              style={{
                fontSize: "11px",
                color: "var(--text-tertiary)",
                marginTop: "2px",
              }}
            >
              {t(
                "enterprise.notificationBar.description",
                "Display a notification bar at the top of the page, visible to all users.",
              )}
            </div>
          </div>
          <label style={switchStyle(nbEnabled, nbSaving)}>
            <input
              type="checkbox"
              checked={nbEnabled}
              onChange={(e) => handleNotificationBarToggle(e.target.checked)}
              disabled={nbSaving}
              style={{ opacity: 0, width: 0, height: 0 }}
            />
            <span style={switchTrack(nbEnabled)}>
              <span style={switchThumb(nbEnabled)} />
            </span>
          </label>
        </div>
        <div
          style={{
            maxHeight: nbEnabled ? "200px" : "0",
            opacity: nbEnabled ? 1 : 0,
            overflow: "hidden",
            transition: "max-height 0.3s ease, opacity 0.25s ease",
          }}
        >
          <div style={{ marginBottom: "12px", paddingTop: "16px" }}>
            <label className="form-label">
              {t("enterprise.notificationBar.text", "Notification text")}
            </label>
            <input
              className="form-input"
              value={nbText}
              onChange={(e) => setNbText(e.target.value)}
              placeholder={t(
                "enterprise.notificationBar.textPlaceholder",
                "e.g. v2.1 released with new features!",
              )}
              style={{ fontSize: "13px" }}
            />
          </div>
          <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
            <button
              className="btn btn-primary"
              onClick={() => saveNotificationBar()}
              disabled={nbSaving}
            >
              {nbSaving ? t("common.loading") : t("common.save", "Save")}
            </button>
            {nbSaved && (
              <span style={{ color: "var(--success)", fontSize: "12px" }}>
                {t("enterprise.config.saved", "Saved")}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Public URL */}
      <div className="card" style={{ padding: "16px", marginBottom: "16px" }}>
        <div
          style={{
            fontSize: "13px",
            fontWeight: 600,
            marginBottom: "4px",
            color: "var(--text-secondary)",
          }}
        >
          {t("admin.publicUrl.title", "Public URL")}
        </div>
        <div
          style={{
            fontSize: "11px",
            color: "var(--text-tertiary)",
            marginBottom: "12px",
          }}
        >
          {t(
            "admin.publicUrl.desc",
            "The external URL used for webhook callbacks (Slack, Feishu, Discord, etc.) and published page links. Include the protocol (e.g. https://example.com).",
          )}
        </div>
        <div style={{ marginBottom: "12px" }}>
          <input
            className="form-input"
            value={publicBaseUrl}
            onChange={(e) => setPublicBaseUrl(e.target.value)}
            placeholder="https://your-domain.com"
            style={{ fontSize: "13px" }}
          />
        </div>
        <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
          <button
            className="btn btn-primary"
            onClick={savePublicUrl}
            disabled={urlSaving}
          >
            {urlSaving ? t("common.loading") : t("common.save", "Save")}
          </button>
          {urlSaved && (
            <span style={{ color: "var(--success)", fontSize: "12px" }}>
              {t("enterprise.config.saved", "Saved")}
            </span>
          )}
        </div>
      </div>

    </>
  );
}
