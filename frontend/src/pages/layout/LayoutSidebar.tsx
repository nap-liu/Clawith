import { createPortal } from "react-dom";
import { NavLink } from "react-router-dom";
import {
  IconPlus,
  IconSettings,
  IconSearch,
  IconX,
  IconPin,
  IconPinnedOff,
  IconArrowUpRight,
  IconBuilding,
  IconChevronDown,
  IconCheck,
  IconApps,
  IconFileDescription,
  IconFolder,
  IconPackage,
} from "@tabler/icons-react";
import LayoutSidebarFooter from "./LayoutSidebarFooter";
import {
  APP_UI_LANGUAGES,
  resolveUiLangCode,
  SidebarIcons,
} from "./sidebarIcons";
const getAgentBadgeStatus = (agent: any): string | null => {
  if (agent.status === "error") return "error";
  if (agent.status === "creating") return "creating";
  // OpenClaw disconnected detection: 60 min timeout
  if (
    agent.agent_type === "openclaw" &&
    agent.status === "running" &&
    agent.openclaw_last_seen
  ) {
    const elapsed = Date.now() - new Date(agent.openclaw_last_seen).getTime();
    if (elapsed > 60 * 60 * 1000) return "disconnected";
  }
  // idle / running / stopped → no badge
  return null;
};
export default function LayoutSidebar({
  sidebarCollapsed,
  tenantSwitcherRef,
  showTenantMenu,
  setShowTenantMenu,
  openTenantModal,
  isChinese,
  currentTenantAvatarTone,
  currentTenantInitial,
  currentTenantLogoUrl,
  currentTenantName,
  toggleSidebar,
  t,
  setShowTalentMarket,
  sidebarSearch,
  setSidebarSearch,
  agentDirectoryQuery,
  agentDirectory,
  agents,
  openAgentDrawer,
  scheduleCloseAgentDrawer,
  showNotifications,
  setShowNotifications,
  unreadCount,
  theme,
  toggleTheme,
  accountMenuRef,
  showAccountMenu,
  setShowAccountMenu,
  accountDropdownRef,
  openLangSubmenu,
  scheduleCloseLangSubmenu,
  showLanguageSubmenu,
  setShowAccountSettings,
  user,
  navigate,
  canAccessPlatformSettings,
  handleLogout,
  langSubmenuPortalRef,
  langSubmenuPos,
  i18n,
  selectUiLanguage,
  tenantMenuPortalRef,
  tenantMenuPos,
  myTenants,
  tenantSearch,
  setTenantSearch,
  filteredTenants,
  currentTenant,
  handleSwitchTenant,
  openTenantSetupModal,
  canAccessCompanySettings,
  pinnedAgents,
  togglePin,
  activeAgentId,
  token,
  agentDrawerOpen,
  setAgentDrawerOpen,
}: any) {
  const langSubmenuContent = showAccountMenu && showLanguageSubmenu && (
    <div
      ref={langSubmenuPortalRef}
      className="account-lang-submenu account-lang-submenu-portal"
      role="menu"
      style={{ top: langSubmenuPos.top, left: langSubmenuPos.left }}
      onMouseEnter={openLangSubmenu}
      onMouseLeave={scheduleCloseLangSubmenu}
    >
      {APP_UI_LANGUAGES.map(({ code, nativeLabel }) => {
        const active = resolveUiLangCode(i18n.language) === code;
        return (
          <button
            key={code}
            type="button"
            role="menuitem"
            className={`account-lang-submenu-item${active ? " is-active" : ""}`}
            onClick={() => selectUiLanguage(code)}
          >
            <span>{nativeLabel}</span>
            {active && <IconCheck size={14} stroke={2} />}
          </button>
        );
      })}
    </div>
  );

  const tenantMenuContent =
    showTenantMenu &&
    typeof document !== "undefined" &&
    createPortal(
      <div
        ref={tenantMenuPortalRef}
        className="tenant-switcher-popover"
        role="menu"
        style={{
          top: tenantMenuPos.top,
          left: tenantMenuPos.left,
          maxHeight: tenantMenuPos.maxHeight,
        }}
      >
        <div className="tenant-switcher-label">
          {isChinese ? "切换公司" : "Switch company"}
        </div>
        {(myTenants as any[]).length > 8 && (
          <div className="tenant-switcher-search">
            <IconSearch size={14} stroke={1.7} />
            <input
              value={tenantSearch}
              onChange={(e) => setTenantSearch(e.target.value)}
              placeholder={isChinese ? "搜索公司" : "Search companies"}
            />
            {tenantSearch && (
              <button
                type="button"
                onClick={() => setTenantSearch("")}
                aria-label={isChinese ? "清空搜索" : "Clear search"}
              >
                <IconX size={14} stroke={1.7} />
              </button>
            )}
          </div>
        )}
        <div className="tenant-switcher-list">
          {filteredTenants.map((tenant: any) => (
            <button
              key={tenant.tenant_id}
              type="button"
              className={`tenant-switcher-item${tenant.tenant_id === currentTenant ? " active" : ""}`}
              onClick={() => {
                if (tenant.tenant_id === currentTenant) {
                  setShowTenantMenu(false);
                  return;
                }
                handleSwitchTenant(tenant.tenant_id);
              }}
            >
              <span className="tenant-switcher-icon">
                <IconBuilding size={16} stroke={1.6} />
              </span>
              <span className="tenant-switcher-name">{tenant.tenant_name}</span>
              {tenant.tenant_id === currentTenant && (
                <IconCheck size={16} stroke={2} />
              )}
            </button>
          ))}
          {filteredTenants.length === 0 && (
            <div className="tenant-switcher-empty">
              {isChinese ? "没有匹配的公司" : "No matching companies"}
            </div>
          )}
        </div>

        <div className="tenant-switcher-divider" />

        <button
          type="button"
          className="tenant-switcher-action"
          onClick={openTenantSetupModal}
        >
          <IconPlus size={17} stroke={1.6} />
          <span>
            {isChinese ? "创建或加入新公司" : "Create or join company"}
          </span>
        </button>
        {canAccessCompanySettings && (
          <button
            type="button"
            className="tenant-switcher-action"
            onClick={() => {
              setShowTenantMenu(false);
              navigate("/enterprise");
            }}
          >
            <IconSettings size={16} stroke={1.6} />
            <span>{isChinese ? "公司信息设置" : "Company settings"}</span>
          </button>
        )}
      </div>,
      document.body,
    );

  const q = sidebarSearch.trim().toLowerCase();
  const sortedAgents = [...agents]
    .filter((a: any) => {
      if (!q) return true;
      return [a.name, a.creator_display_name, a.creator_username].some(
        (value) =>
          String(value || "")
            .toLowerCase()
            .includes(q),
      );
    })
    .sort((a: any, b: any) => {
      const ap = pinnedAgents.has(a.id) ? 1 : 0;
      const bp = pinnedAgents.has(b.id) ? 1 : 0;
      if (ap !== bp) return bp - ap;
      const aTime = a.created_at ? new Date(a.created_at).getTime() : 0;
      const bTime = b.created_at ? new Date(b.created_at).getTime() : 0;
      return bTime - aTime;
    });

  const agentSearchBox = (force = false) =>
    (force || agents.length >= 5) && (
      <div className="sidebar-agent-search">
        <IconSearch
          size={14}
          stroke={2}
          className="sidebar-agent-search-icon"
        />
        <input
          type="text"
          value={sidebarSearch}
          onChange={(e) => setSidebarSearch(e.target.value)}
          placeholder={t("sidebar.agentSearchPlaceholder")}
          aria-label={t("sidebar.agentSearchLabel")}
        />
        {sidebarSearch && (
          <button
            onClick={() => setSidebarSearch("")}
            aria-label={t("sidebar.clearAgentSearch")}
          >
            <IconX size={14} stroke={2} />
          </button>
        )}
      </div>
    );

  const renderAgent = (agent: any, options?: { drawer?: boolean }) => {
    const badge = getAgentBadgeStatus(agent);
    const avatarChar = (
      (Array.from(agent.name || "?")[0] as string) || "?"
    ).toUpperCase();
    const unreadCount = Number(agent.unread_count || 0);
    const showPin = !sidebarCollapsed || options?.drawer;
    return (
      <div
        key={agent.id}
        className={`sidebar-agent-item${agent.creator_id === user?.id ? " owned" : ""}${options?.drawer ? " drawer-agent" : ""}`}
      >
        <NavLink
          to={
            options?.drawer ? `/agents/${agent.id}/chat` : `/agents/${agent.id}`
          }
          className={({ isActive }) =>
            `sidebar-item ${isActive || activeAgentId === agent.id ? "active" : ""}`
          }
          title={agent.name}
          onClick={() => setAgentDrawerOpen(false)}
        >
          <span className="sidebar-item-icon" style={{ position: "relative" }}>
            {agent.avatar_url ? (
              <img
                src={
                  agent.avatar_url.startsWith("/api")
                    ? `${agent.avatar_url}${agent.avatar_url.includes("?") ? "&" : "?"}token=${token}`
                    : agent.avatar_url
                }
                alt=""
                className={`agent-avatar${agent.agent_type === "openclaw" ? " openclaw" : ""}`}
                style={{ objectFit: "cover" }}
              />
            ) : (
              <span
                className={`agent-avatar${agent.agent_type === "openclaw" ? " openclaw" : ""}`}
              >
                {avatarChar}
              </span>
            )}
            {agent.agent_type === "openclaw" && (
              <span className="agent-avatar-link" style={{ display: "flex" }}>
                <IconArrowUpRight size={10} stroke={2.5} />
              </span>
            )}
            {badge && <span className={`agent-avatar-badge ${badge}`} />}
            {unreadCount > 0 && (
              <span className="sidebar-agent-unread">
                {unreadCount > 99 ? "99+" : unreadCount}
              </span>
            )}
          </span>
          <span className="sidebar-item-text">{agent.name}</span>
        </NavLink>
        {showPin && (
          <button
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              togglePin(agent.id);
            }}
            className={`sidebar-pin-btn ${pinnedAgents.has(agent.id) ? "pinned" : ""}`}
            title={
              pinnedAgents.has(agent.id)
                ? isChinese
                  ? "取消置顶"
                  : "Unpin"
                : isChinese
                  ? "置顶"
                  : "Pin to top"
            }
          >
            {pinnedAgents.has(agent.id) ? (
              <>
                <IconPin size={14} stroke={1.5} className="pin-default" />
                <IconPinnedOff size={14} stroke={1.5} className="pin-hover" />
              </>
            ) : (
              <IconPin size={14} stroke={1.5} className="pin-on" />
            )}
          </button>
        )}
      </div>
    );
  };

  const agentListContent = (drawer = false) => (
    <>
      {sortedAgents.map((agent) => renderAgent(agent, { drawer }))}
      {agentDirectoryQuery.isError && (
        <div className="sidebar-agent-empty" role="alert">
          {t("sidebar.agentLoadFailed")} {" "}
          <button type="button" onClick={() => void agentDirectoryQuery.refetch()}>
            {t("sidebar.retry")}
          </button>
        </div>
      )}
      {agentDirectory?.incomplete && (
        <div className="sidebar-agent-empty" role="status">
          {t("sidebar.agentLoadIncomplete")} {" "}
          <button type="button" onClick={() => void agentDirectoryQuery.refetch()}>
            {t("sidebar.retry")}
          </button>
        </div>
      )}
      {agents.length === 0 && (
        <div className="sidebar-section">
          <div className="sidebar-section-title">{t("nav.myAgents")}</div>
        </div>
      )}
      {agents.length > 0 && sortedAgents.length === 0 && q && (
        <div className="sidebar-agent-empty">
          {t("sidebar.noAgentMatches")}
          {agentDirectory?.incomplete && ` · ${t("sidebar.resultsIncomplete")}`}
        </div>
      )}
    </>
  );

  const agentDrawer =
    sidebarCollapsed &&
    agentDrawerOpen &&
    typeof document !== "undefined" &&
    createPortal(
      <div
        className="sidebar-agent-drawer"
        onMouseEnter={openAgentDrawer}
        onMouseLeave={scheduleCloseAgentDrawer}
      >
        <div className="sidebar-agent-drawer-header">
          <span>{t("sidebar.agents")}</span>
          <button
            type="button"
            onClick={() => {
              setShowTalentMarket(true);
              setAgentDrawerOpen(false);
            }}
            title={t("nav.hire", t("nav.newAgent"))}
          >
            <IconPlus size={16} stroke={1.7} />
          </button>
        </div>
        {agentSearchBox(true)}
        <div className="sidebar-agent-drawer-list">
          {agentListContent(true)}
        </div>
      </div>,
      document.body,
    );

  return (
    <>
      <nav className={`sidebar ${sidebarCollapsed ? "collapsed" : ""}`}>
        <div className="sidebar-top">
          <div className="sidebar-logo">
            <button
              ref={tenantSwitcherRef}
              type="button"
              className={`sidebar-logo-switcher${showTenantMenu ? " open" : ""}`}
              data-tour-target="company-switcher"
              onClick={() => {
                if (showTenantMenu) {
                  setShowTenantMenu(false);
                  return;
                }
                openTenantModal();
              }}
              title={isChinese ? "切换企业" : "Switch Organization"}
            >
              <span
                className={`workspace-switcher-avatar tone-${currentTenantAvatarTone}`}
              >
                {currentTenantInitial}
                {currentTenantLogoUrl && (
                  <img
                    src={currentTenantLogoUrl}
                    alt=""
                    onError={(event) => {
                      event.currentTarget.style.display = "none";
                    }}
                  />
                )}
              </span>
              <span className="workspace-switcher-name">
                {currentTenantName}
              </span>
              <IconChevronDown
                className="workspace-switcher-chevron"
                size={15}
                stroke={1.7}
              />
            </button>
            <button
              className="btn btn-ghost sidebar-collapse-btn"
              onClick={toggleSidebar}
              style={{
                padding: "4px",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                marginLeft: "auto",
                color: "var(--text-tertiary)",
              }}
              title={
                sidebarCollapsed
                  ? t("common.expandSidebar")
                  : t("common.collapseSidebar")
              }
            >
              {sidebarCollapsed ? SidebarIcons.expand : SidebarIcons.collapse}
            </button>
          </div>

          <div className="sidebar-section" data-tour-target="main-nav">
            <NavLink
              to="/projects"
              className={({ isActive }) =>
                `sidebar-item ${isActive ? "active" : ""}`
              }
            >
              <span
                className="sidebar-item-icon"
                style={{
                  display: "flex",
                  justifyContent: "center",
                  alignItems: "center",
                }}
              >
                <IconFolder size={14} stroke={1.5} />
              </span>
              <span className="sidebar-item-text">
                {t("nav.projects", "项目")}
              </span>
            </NavLink>
            <NavLink
              to="/explore"
              className={({ isActive }) =>
                `sidebar-item ${isActive ? "active" : ""}`
              }
            >
              <span
                className="sidebar-item-icon"
                style={{
                  display: "flex",
                  justifyContent: "center",
                  alignItems: "center",
                }}
              >
                <IconApps size={14} stroke={1.5} />
              </span>
              <span className="sidebar-item-text">
                {t("nav.explore", "探索")}
              </span>
            </NavLink>
            <NavLink
              to="/published-pages"
              className={({ isActive }) =>
                `sidebar-item ${isActive ? "active" : ""}`
              }
            >
              <span
                className="sidebar-item-icon"
                style={{
                  display: "flex",
                  justifyContent: "center",
                  alignItems: "center",
                }}
              >
                <IconFileDescription size={14} stroke={1.5} />
              </span>
              <span className="sidebar-item-text">
                {isChinese ? "发布管理" : "Published content"}
              </span>
            </NavLink>
            <NavLink
              to="/skill-market"
              className={({ isActive }) =>
                `sidebar-item ${isActive ? "active" : ""}`
              }
            >
              <span
                className="sidebar-item-icon"
                style={{
                  display: "flex",
                  justifyContent: "center",
                  alignItems: "center",
                }}
              >
                <IconPackage size={14} stroke={1.5} />
              </span>
              <span className="sidebar-item-text">{t("nav.skillMarket")}</span>
            </NavLink>
          </div>
        </div>

        <div className="sidebar-divider" />

        <div
          className="sidebar-scrollable"
          data-tour-target="agent-list"
          onMouseEnter={openAgentDrawer}
          onMouseLeave={scheduleCloseAgentDrawer}
        >
          {!sidebarCollapsed && (
            <div className="sidebar-agent-sticky">
              <div className="sidebar-agent-header">
                <span>{t("sidebar.agents")}</span>
                <button
                  type="button"
                  data-tour-target="hire-agent"
                  onClick={() => setShowTalentMarket(true)}
                  title={t("nav.hire", t("nav.newAgent"))}
                >
                  <IconPlus size={15} stroke={1.7} />
                </button>
              </div>
              {agentSearchBox(true)}
            </div>
          )}
          <div className="sidebar-agent-list">{agentListContent()}</div>
        </div>

        <LayoutSidebarFooter
          sidebarCollapsed={sidebarCollapsed}
          isChinese={isChinese}
          toggleTheme={toggleTheme}
          theme={theme}
          t={t}
          unreadCount={unreadCount}
          setShowNotifications={setShowNotifications}
          toggleSidebar={toggleSidebar}
          accountMenuRef={accountMenuRef}
          showAccountMenu={showAccountMenu}
          accountDropdownRef={accountDropdownRef}
          openLangSubmenu={openLangSubmenu}
          scheduleCloseLangSubmenu={scheduleCloseLangSubmenu}
          showLanguageSubmenu={showLanguageSubmenu}
          i18n={i18n}
          setShowAccountSettings={setShowAccountSettings}
          setShowAccountMenu={setShowAccountMenu}
          user={user}
          navigate={navigate}
          canAccessPlatformSettings={canAccessPlatformSettings}
          handleLogout={handleLogout}
          langSubmenuContent={langSubmenuContent}
        />
      </nav>
      {agentDrawer}
      {tenantMenuContent}

    </>
  );
}
