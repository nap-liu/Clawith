export default function PlatformEmailSettings(props: any) {
  const {
    t,
    systemEmailConfig,
    setSystemEmailConfig,
    emailConfigSaving,
    saveEmailConfig,
    emailConfigSaved,
    showTestEmail,
    setShowTestEmail,
    testEmailAddr,
    setTestEmailAddr,
    testEmailSending,
    handleSendTestEmail,
    testEmailResult,
    setTestEmailResult,
    emailTemplates,
    expandedTemplate,
    setExpandedTemplate,
    emailTemplateVars,
    setEmailTemplates,
    resetTemplate,
    templatesSaving,
    saveEmailTemplates,
    templatesSaved,
  } = props;

  return (
    <>
      {/* System Email Configuration */}
      <div className="card" style={{ padding: "16px", marginBottom: "16px" }}>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            gap: "16px",
            alignItems: "flex-start",
            marginBottom: "4px",
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
              {t("enterprise.systemEmail.title", "System Email Configuration")}
            </div>
            <p
              style={{
                fontSize: "12px",
                color: "var(--text-tertiary)",
                margin: "4px 0 0",
              }}
            >
              {t(
                "enterprise.systemEmail.description",
                "Configure SMTP settings for sending system emails such as password resets and notifications.",
              )}
            </p>
          </div>
          <label
            style={{
              display: "flex",
              alignItems: "center",
              gap: "8px",
              cursor: "pointer",
              paddingTop: "2px",
              flexShrink: 0,
            }}
          >
            <input
              type="checkbox"
              checked={systemEmailConfig.SYSTEM_EMAIL_ENABLED}
              onChange={(e) =>
                setSystemEmailConfig({
                  ...systemEmailConfig,
                  SYSTEM_EMAIL_ENABLED: e.target.checked,
                })
              }
              style={{ width: "16px", height: "16px" }}
            />
            <span
              style={{
                fontSize: "13px",
                fontWeight: 500,
                color: "var(--text-primary)",
              }}
            >
              {t("enterprise.systemEmail.enabled", "Enable system email")}
            </span>
          </label>
        </div>
        <p
          style={{
            fontSize: "12px",
            color: "var(--text-tertiary)",
            marginBottom: "16px",
          }}
        >
          {systemEmailConfig.SYSTEM_EMAIL_ENABLED
            ? t(
                "enterprise.systemEmail.enabledHint",
                "System emails are enabled. New email/password registrations will require email verification.",
              )
            : t(
                "enterprise.systemEmail.disabledHint",
                "System emails are disabled. SMTP settings can be saved and tested, but registration email verification will be skipped.",
              )}
        </p>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "1fr 1fr",
            gap: "16px",
          }}
        >
          <div>
            <label
              className="form-label"
              style={{ fontSize: "12px", marginBottom: "6px" }}
            >
              {t("enterprise.systemEmail.fromAddress", "From Email Address")}
            </label>
            <input
              className="form-input"
              value={systemEmailConfig.SYSTEM_EMAIL_FROM_ADDRESS}
              onChange={(e) =>
                setSystemEmailConfig({
                  ...systemEmailConfig,
                  SYSTEM_EMAIL_FROM_ADDRESS: e.target.value,
                })
              }
              placeholder="noreply@yourcompany.com"
              style={{ fontSize: "13px" }}
            />
          </div>
          <div>
            <label
              className="form-label"
              style={{ fontSize: "12px", marginBottom: "6px" }}
            >
              {t("enterprise.systemEmail.fromName", "From Name")}
            </label>
            <input
              className="form-input"
              value={systemEmailConfig.SYSTEM_EMAIL_FROM_NAME}
              onChange={(e) =>
                setSystemEmailConfig({
                  ...systemEmailConfig,
                  SYSTEM_EMAIL_FROM_NAME: e.target.value,
                })
              }
              placeholder="Digital Employee Platform"
              style={{ fontSize: "13px" }}
            />
          </div>
          <div>
            <label
              className="form-label"
              style={{ fontSize: "12px", marginBottom: "6px" }}
            >
              {t("enterprise.systemEmail.smtpHost", "SMTP Host")}
            </label>
            <input
              className="form-input"
              value={systemEmailConfig.SYSTEM_SMTP_HOST}
              onChange={(e) =>
                setSystemEmailConfig({
                  ...systemEmailConfig,
                  SYSTEM_SMTP_HOST: e.target.value,
                })
              }
              placeholder="smtp.gmail.com"
              style={{ fontSize: "13px" }}
            />
          </div>
          <div>
            <label
              className="form-label"
              style={{ fontSize: "12px", marginBottom: "6px" }}
            >
              {t("enterprise.systemEmail.smtpPort", "SMTP Port")}
            </label>
            <input
              className="form-input"
              type="number"
              value={systemEmailConfig.SYSTEM_SMTP_PORT}
              onChange={(e) =>
                setSystemEmailConfig({
                  ...systemEmailConfig,
                  SYSTEM_SMTP_PORT: parseInt(e.target.value) || 465,
                })
              }
              placeholder="465"
              style={{ fontSize: "13px" }}
            />
          </div>
          <div>
            <label
              className="form-label"
              style={{ fontSize: "12px", marginBottom: "6px" }}
            >
              {t("enterprise.systemEmail.username", "SMTP Username")}
            </label>
            <input
              className="form-input"
              value={systemEmailConfig.SYSTEM_SMTP_USERNAME}
              onChange={(e) =>
                setSystemEmailConfig({
                  ...systemEmailConfig,
                  SYSTEM_SMTP_USERNAME: e.target.value,
                })
              }
              placeholder="your-email@gmail.com"
              style={{ fontSize: "13px" }}
            />
          </div>
          <div>
            <label
              className="form-label"
              style={{ fontSize: "12px", marginBottom: "6px" }}
            >
              {t(
                "enterprise.systemEmail.password",
                "SMTP Password / App Password",
              )}
            </label>
            <input
              className="form-input"
              type="password"
              value={systemEmailConfig.SYSTEM_SMTP_PASSWORD}
              onChange={(e) =>
                setSystemEmailConfig({
                  ...systemEmailConfig,
                  SYSTEM_SMTP_PASSWORD: e.target.value,
                })
              }
              placeholder="••••••••"
              style={{ fontSize: "13px" }}
            />
          </div>
          <div>
            <label
              className="form-label"
              style={{ fontSize: "12px", marginBottom: "6px" }}
            >
              {t("enterprise.systemEmail.timeout", "Timeout (seconds)")}
            </label>
            <input
              className="form-input"
              type="number"
              value={systemEmailConfig.SYSTEM_SMTP_TIMEOUT_SECONDS}
              onChange={(e) =>
                setSystemEmailConfig({
                  ...systemEmailConfig,
                  SYSTEM_SMTP_TIMEOUT_SECONDS: parseInt(e.target.value) || 15,
                })
              }
              placeholder="15"
              style={{ fontSize: "13px" }}
            />
          </div>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              paddingTop: "24px",
            }}
          >
            <label
              style={{
                display: "flex",
                alignItems: "center",
                gap: "8px",
                cursor: "pointer",
              }}
            >
              <input
                type="checkbox"
                checked={systemEmailConfig.SYSTEM_SMTP_SSL}
                onChange={(e) =>
                  setSystemEmailConfig({
                    ...systemEmailConfig,
                    SYSTEM_SMTP_SSL: e.target.checked,
                  })
                }
                style={{ width: "16px", height: "16px" }}
              />
              <span style={{ fontSize: "13px" }}>
                {t("enterprise.systemEmail.useSsl", "Use SSL/TLS")}
              </span>
            </label>
          </div>
        </div>
        <div
          style={{
            marginTop: "16px",
            display: "flex",
            gap: "8px",
            alignItems: "center",
            flexWrap: "wrap",
          }}
        >
          <button
            className="btn btn-primary"
            onClick={saveEmailConfig}
            disabled={emailConfigSaving}
          >
            {emailConfigSaving ? t("common.loading") : t("common.save", "Save")}
          </button>
          <button
            className="btn btn-secondary"
            onClick={() => {
              setShowTestEmail(!showTestEmail);
              setTestEmailResult(null);
            }}
          >
            {t("enterprise.systemEmail.sendTest", "Send Test Email")}
          </button>
          {emailConfigSaved && (
            <span style={{ color: "var(--success)", fontSize: "12px" }}>
              {t("common.saved", "Saved")}
            </span>
          )}
        </div>

        {/* Test email inline form */}
        {showTestEmail && (
          <div
            style={{
              marginTop: "12px",
              padding: "12px 16px",
              background: "var(--bg-secondary)",
              borderRadius: "8px",
              border: "1px solid var(--border-subtle)",
            }}
          >
            <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
              <input
                className="form-input"
                type="email"
                placeholder={t(
                  "enterprise.systemEmail.testPlaceholder",
                  "Enter recipient email...",
                )}
                value={testEmailAddr}
                onChange={(e) => setTestEmailAddr(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleSendTestEmail()}
                style={{ flex: 1, fontSize: "13px" }}
              />
              <button
                className="btn btn-primary btn-sm"
                onClick={handleSendTestEmail}
                disabled={testEmailSending || !testEmailAddr.trim()}
              >
                {testEmailSending
                  ? t("common.loading", "Sending...")
                  : t("enterprise.systemEmail.send", "Send")}
              </button>
            </div>
            {testEmailResult && (
              <div
                style={{
                  marginTop: "8px",
                  fontSize: "12px",
                  color: testEmailResult.ok ? "var(--success)" : "var(--error)",
                }}
              >
                {testEmailResult.msg}
              </div>
            )}
          </div>
        )}

        <div
          style={{
            marginTop: "12px",
            fontSize: "11px",
            color: "var(--text-tertiary)",
          }}
        >
          {t(
            "enterprise.systemEmail.hint",
            "For Gmail, use an App Password. For QQ/163 mail, use the SMTP authorization code.",
          )}
        </div>
      </div>

      {/* Email Templates Configuration */}
      <div className="card" style={{ padding: "16px", marginBottom: "16px" }}>
        <div
          style={{
            fontSize: "13px",
            fontWeight: 600,
            marginBottom: "4px",
            color: "var(--text-secondary)",
          }}
        >
          {t("enterprise.emailTemplates.title", "Email Templates")}
        </div>
        <p
          style={{
            fontSize: "12px",
            color: "var(--text-tertiary)",
            marginBottom: "16px",
          }}
        >
          {t(
            "enterprise.emailTemplates.description",
            "Customize email content for each scenario. Use {{variable}} placeholders to insert dynamic data.",
          )}
        </p>

        <div style={{ display: "flex", flexDirection: "column", gap: "2px" }}>
          {(
            [
              {
                key: "email_verification",
                label: t(
                  "enterprise.emailTemplates.emailVerification",
                  "Email Verification Code",
                ),
                desc: t(
                  "enterprise.emailTemplates.emailVerificationDesc",
                  "Sent when a user registers and needs to verify their email address.",
                ),
              },
              {
                key: "password_reset",
                label: t(
                  "enterprise.emailTemplates.passwordReset",
                  "Password Reset",
                ),
                desc: t(
                  "enterprise.emailTemplates.passwordResetDesc",
                  "Sent when a user requests to reset their password.",
                ),
              },
              {
                key: "company_invitation",
                label: t(
                  "enterprise.emailTemplates.companyInvitation",
                  "Company Invitation",
                ),
                desc: t(
                  "enterprise.emailTemplates.companyInvitationDesc",
                  "Sent when an admin invites a user to join their company.",
                ),
              },
            ] as const
          ).map((scenario) => {
            const isExpanded = expandedTemplate === scenario.key;
            const template = emailTemplates[scenario.key] || {
              subject: "",
              body: "",
            };
            const vars = emailTemplateVars[scenario.key] || [];

            return (
              <div
                key={scenario.key}
                style={{
                  border: "1px solid var(--border-subtle)",
                  borderRadius: "8px",
                  overflow: "hidden",
                  marginBottom: "8px",
                }}
              >
                {/* Scenario header */}
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    padding: "12px 16px",
                    cursor: "pointer",
                    background: isExpanded
                      ? "var(--bg-secondary)"
                      : "transparent",
                    transition: "background 0.15s",
                  }}
                  onClick={() =>
                    setExpandedTemplate(isExpanded ? null : scenario.key)
                  }
                >
                  <div>
                    <div style={{ fontWeight: 500, fontSize: "13px" }}>
                      {scenario.label}
                    </div>
                    <div
                      style={{
                        fontSize: "11px",
                        color: "var(--text-tertiary)",
                        marginTop: "2px",
                      }}
                    >
                      {scenario.desc}
                    </div>
                  </div>
                  <div
                    style={{
                      color: "var(--text-tertiary)",
                      transform: isExpanded ? "rotate(180deg)" : "none",
                      transition: "transform 0.2s",
                      fontSize: "12px",
                    }}
                  >
                    ▼
                  </div>
                </div>

                {/* Expanded editor */}
                {isExpanded && (
                  <div
                    style={{
                      padding: "16px",
                      background: "var(--bg-secondary)",
                      borderTop: "1px solid var(--border-subtle)",
                    }}
                  >
                    {/* Available variables */}
                    <div style={{ marginBottom: "12px" }}>
                      <div
                        style={{
                          fontSize: "11px",
                          fontWeight: 600,
                          color: "var(--text-tertiary)",
                          marginBottom: "6px",
                          textTransform: "uppercase",
                          letterSpacing: "0.05em",
                        }}
                      >
                        {t(
                          "enterprise.emailTemplates.availableVars",
                          "Available Variables",
                        )}
                      </div>
                      <div
                        style={{
                          display: "flex",
                          gap: "6px",
                          flexWrap: "wrap",
                        }}
                      >
                        {vars.map((v: any) => (
                          <span
                            key={v}
                            style={{
                              display: "inline-flex",
                              alignItems: "center",
                              gap: "4px",
                              padding: "3px 8px",
                              borderRadius: "4px",
                              background:
                                "rgba(var(--accent-rgb, 99,102,241), 0.1)",
                              color: "var(--accent-primary)",
                              fontSize: "11px",
                              fontFamily: "var(--font-mono)",
                              cursor: "default",
                            }}
                            title={t(
                              "enterprise.emailTemplates.clickToInsert",
                              "Click to copy variable",
                            )}
                          >
                            {`{{${v}}}`}
                          </span>
                        ))}
                      </div>
                    </div>

                    {/* Subject */}
                    <div style={{ marginBottom: "12px" }}>
                      <label
                        className="form-label"
                        style={{ fontSize: "12px", marginBottom: "4px" }}
                      >
                        {t("enterprise.emailTemplates.subject", "Subject")}
                      </label>
                      <input
                        className="form-input"
                        value={template.subject}
                        onChange={(e) =>
                          setEmailTemplates((prev: any) => ({
                            ...prev,
                            [scenario.key]: {
                              ...prev[scenario.key],
                              subject: e.target.value,
                            },
                          }))
                        }
                        style={{ fontSize: "13px" }}
                      />
                    </div>

                    {/* Body */}
                    <div style={{ marginBottom: "12px" }}>
                      <label
                        className="form-label"
                        style={{ fontSize: "12px", marginBottom: "4px" }}
                      >
                        {t("enterprise.emailTemplates.body", "Body")}
                      </label>
                      <textarea
                        className="form-input"
                        rows={8}
                        value={template.body}
                        onChange={(e) =>
                          setEmailTemplates((prev: any) => ({
                            ...prev,
                            [scenario.key]: {
                              ...prev[scenario.key],
                              body: e.target.value,
                            },
                          }))
                        }
                        style={{
                          fontSize: "13px",
                          fontFamily: "var(--font-mono)",
                          resize: "vertical",
                          lineHeight: 1.6,
                          minHeight: "200px",
                          height: "auto",
                        }}
                      />
                    </div>

                    {/* Reset to default */}
                    <div
                      style={{ display: "flex", justifyContent: "flex-end" }}
                    >
                      <button
                        className="btn btn-ghost"
                        style={{
                          fontSize: "12px",
                          color: "var(--text-tertiary)",
                        }}
                        onClick={() => resetTemplate(scenario.key)}
                      >
                        {t(
                          "enterprise.emailTemplates.resetDefault",
                          "Reset to Default",
                        )}
                      </button>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>

        <div
          style={{
            marginTop: "12px",
            display: "flex",
            gap: "8px",
            alignItems: "center",
          }}
        >
          <button
            className="btn btn-primary"
            onClick={saveEmailTemplates}
            disabled={templatesSaving}
          >
            {templatesSaving
              ? t("common.loading")
              : t("enterprise.emailTemplates.saveTemplates", "Save Templates")}
          </button>
          {templatesSaved && (
            <span style={{ color: "var(--success)", fontSize: "12px" }}>
              {t("common.saved", "Saved")}
            </span>
          )}
        </div>
      </div>
    </>
  );
}
