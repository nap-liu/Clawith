import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import {
  IconBuilding,
  IconChevronRight,
  IconChevronUp,
  IconLogout,
  IconSettings,
  IconUser,
  IconWorld,
} from "@tabler/icons-react";
import { SidebarIcons, resolveUiLangCode } from "./sidebarIcons";

/* ────── Version Display (runtime) ────── */
function VersionDisplay() {
  const [info, setInfo] = useState<{ version?: string; commit?: string }>({});
  useEffect(() => {
    fetch("/api/version")
      .then((r) => r.json())
      .then(setInfo)
      .catch(() => {});
  }, []);
  if (!info.version) return null;
  const displayCommit = info.commit?.trim().slice(0, 7);
  return (
    <div
      style={{
        textAlign: "center",
        fontSize: "10px",
        color: "var(--text-quaternary)",
        marginTop: "8px",
        letterSpacing: "0.3px",
      }}
    >
      v{info.version}
      {displayCommit && (
        <span style={{ opacity: 0.6 }} title={info.commit}>
          {" "}({displayCommit})
        </span>
      )}
    </div>
  );
}


export default function LayoutSidebarFooter({
  sidebarCollapsed,
  isChinese,
  toggleTheme,
  theme,
  t,
  unreadCount,
  setShowNotifications,
  toggleSidebar,
  accountMenuRef,
  showAccountMenu,
  accountDropdownRef,
  openLangSubmenu,
  scheduleCloseLangSubmenu,
  showLanguageSubmenu,
  i18n,
  setShowAccountSettings,
  setShowAccountMenu,
  user,
  navigate,
  canAccessPlatformSettings,
  handleLogout,
  langSubmenuContent,
}: any) {
  return (
        <div className="sidebar-bottom">
          <div className="sidebar-footer">
            <div
              className="sidebar-footer-controls"
              style={{
                display: "flex",
                alignItems: "center",
                gap: "4px",
                marginBottom: "8px",
                width: "100%",
              }}
            >
              <button
                className="btn btn-ghost"
                onClick={toggleTheme}
                style={{
                  padding: "4px 8px",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }}
                title={
                  theme === "dark"
                    ? t("common.lightMode")
                    : t("common.darkMode")
                }
              >
                {theme === "dark" ? SidebarIcons.sun : SidebarIcons.moon}
              </button>
              <button
                className="btn btn-ghost"
                onClick={() => setShowNotifications((v: boolean) => !v)}
                style={{
                  padding: "4px 8px",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  position: "relative",
                }}
                title={isChinese ? "通知" : "Notifications"}
              >
                {SidebarIcons.bell}
                {(unreadCount as number) > 0 && (
                  <span
                    style={{
                      position: "absolute",
                      top: "-2px",
                      right: "-4px",
                      minWidth: "16px",
                      height: "16px",
                      borderRadius: "8px",
                      padding: "0 4px",
                      boxSizing: "border-box",
                      background: "var(--error)",
                      color: "#fff",
                      fontSize: "10px",
                      fontWeight: 600,
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      lineHeight: 1,
                    }}
                  >
                    {(unreadCount as number) > 99 ? "99+" : unreadCount}
                  </span>
                )}
              </button>
              <button
                className="btn btn-ghost sidebar-collapse-btn"
                onClick={toggleSidebar}
                style={{
                  padding: "4px 8px",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  color: "var(--text-tertiary)",
                  marginLeft: sidebarCollapsed ? undefined : "auto",
                }}
                title={
                  sidebarCollapsed
                    ? t("common.expandSidebar")
                    : t("common.collapseSidebar")
                }
              >
                {sidebarCollapsed
                  ? SidebarIcons.expand
                  : SidebarIcons.collapse}
              </button>
            </div>
            <div ref={accountMenuRef} style={{ position: "relative" }}>
              {showAccountMenu && (
                <div className="account-menus-container">
                  <div className="account-dropdown" ref={accountDropdownRef}>
                    <div
                      className="account-dropdown-language-hover-wrap"
                      onMouseEnter={openLangSubmenu}
                      onMouseLeave={scheduleCloseLangSubmenu}
                    >
                      <button
                        type="button"
                        className="account-dropdown-item language-menu-trigger"
                        aria-haspopup="menu"
                        aria-expanded={showLanguageSubmenu}
                      >
                        <IconWorld size={15} stroke={1.5} />
                        <span className="language-menu-label">
                          {t("layout.language", {
                            lng: resolveUiLangCode(i18n.language),
                            defaultValue: "Language",
                          })}
                        </span>
                        <span className="language-menu-chevron" aria-hidden>
                          <IconChevronRight size={16} stroke={1.75} />
                        </span>
                      </button>
                    </div>
                    <button
                      className="account-dropdown-item"
                      onClick={() => {
                        setShowAccountSettings(true);
                        setShowAccountMenu(false);
                      }}
                    >
                      <IconUser size={15} stroke={1.5} />
                      <span>{isChinese ? "账户设置" : "Account Settings"}</span>
                    </button>
                    {user &&
                      ["platform_admin", "org_admin"].includes(user.role) && (
                        <button
                          className="account-dropdown-item"
                          onClick={() => {
                            navigate("/enterprise");
                            setShowAccountMenu(false);
                          }}
                        >
                          <IconBuilding size={15} stroke={1.5} />
                          <span>
                            {t(
                              "nav.enterprise",
                              isChinese ? "公司设置" : "Company Settings",
                            )}
                          </span>
                        </button>
                      )}
                    {canAccessPlatformSettings && (
                      <button
                        className="account-dropdown-item"
                        onClick={() => {
                          navigate("/admin/platform-settings");
                          setShowAccountMenu(false);
                        }}
                      >
                        <IconSettings size={15} stroke={1.5} />
                        <span>
                          {t("nav.platformSettings", "Platform Settings")}
                        </span>
                      </button>
                    )}
                    <div
                      style={{
                        height: "1px",
                        background: "var(--border-subtle)",
                        margin: "4px 0",
                      }}
                    />
                    <button
                      className="account-dropdown-item account-dropdown-danger"
                      onClick={() => {
                        handleLogout();
                        setShowAccountMenu(false);
                      }}
                    >
                      <IconLogout size={15} stroke={1.5} />
                      <span>{t("layout.logout", "Logout")}</span>
                    </button>
                  </div>
                </div>
              )}
              {typeof document !== "undefined" &&
                langSubmenuContent &&
                createPortal(langSubmenuContent, document.body)}
              <div
                className="sidebar-account-row"
                onClick={() => setShowAccountMenu((v: boolean) => !v)}
              >
                <div
                  style={{
                    width: "28px",
                    height: "28px",
                    borderRadius: "var(--radius-md)",
                    background: "var(--bg-tertiary)",
                    border: "1px solid var(--border-subtle)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    color: "var(--text-tertiary)",
                    flexShrink: 0,
                  }}
                >
                  {SidebarIcons.user}
                </div>
                <div
                  className="sidebar-footer-user-info"
                  style={{ flex: 1, minWidth: 0 }}
                >
                  <div
                    style={{
                      fontSize: "13px",
                      fontWeight: 500,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {user?.display_name}
                  </div>
                  <div
                    style={{ fontSize: "11px", color: "var(--text-tertiary)" }}
                  >
                    {user?.role === "platform_admin"
                      ? t("roles.platformAdmin")
                      : user?.role === "org_admin"
                        ? t("roles.orgAdmin")
                        : user?.role === "agent_admin"
                          ? t("roles.agentAdmin")
                          : t("roles.member")}
                  </div>
                </div>
                <IconChevronUp
                  size={14}
                  stroke={1.5}
                  style={{
                    color: "var(--text-tertiary)",
                    flexShrink: 0,
                    transform: showAccountMenu
                      ? "rotate(0deg)"
                      : "rotate(180deg)",
                    transition: "transform 0.2s ease",
                  }}
                />
              </div>
            </div>
            <VersionDisplay />
          </div>
        </div>

  );
}
