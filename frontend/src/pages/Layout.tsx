import {
  useState,
  useEffect,
  useLayoutEffect,
  useRef,
  useCallback,
  useMemo,
} from "react";
import {
  Outlet,
  useNavigate,
  useMatch,
  useLocation,
} from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuthStore } from "../stores";
import { agentApi, tenantApi, authApi, onboardingApi } from "../services/api";
import { useToast } from "../components/Toast/ToastProvider";
import { useAppStore } from "../stores";
import TalentMarketModal from "../components/TalentMarketModal";
import {
  applyDocumentTheme,
  readSavedTheme,
  saveTheme,
} from "../utils/themeMode";
import AccountSettingsModal from "./layout/AccountSettingsModal";
import CompanyTourOverlay from "./layout/CompanyTourOverlay";
import LayoutModals from "./layout/LayoutModals";
import LayoutSidebar from "./layout/LayoutSidebar";


const fetchJson = async <T,>(url: string): Promise<T> => {
  const token = localStorage.getItem("token");
  const res = await fetch(`/api${url}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!res.ok) return [] as T;
  return res.json();
};


const getWorkspaceAvatarTone = (name: string): number => {
  let hash = 0;
  for (const char of name) {
    hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  }
  return (hash % 6) + 1;
};

export default function Layout() {
  const { t, i18n } = useTranslation();
  const toast = useToast();
  const navigate = useNavigate();
  const location = useLocation();
  const { user, logout, setAuth, token } = useAuthStore();
  const queryClient = useQueryClient();
  const isChinese = i18n.language?.startsWith("zh");
  // Detect chat page: needs fixed-height main-content for inner scroll to work
  const isChatPage = !!useMatch("/agents/:id/chat");
  const isAgentSettingsPage = !!useMatch("/agents/:id/settings");
  const isProjectWorkspacePage = !!useMatch("/projects/:projectId/*");
  const activeAgentNestedMatch = useMatch("/agents/:id/*");
  const activeAgentRootMatch = useMatch("/agents/:id");
  const activeAgentId =
    activeAgentNestedMatch?.params.id || activeAgentRootMatch?.params.id;
  const canAccessPlatformSettings =
    user?.role === "platform_admin" || !!(user as any)?.is_platform_admin;
  const canAccessCompanySettings =
    user?.role === "platform_admin" ||
    user?.role === "org_admin" ||
    !!(user as any)?.is_platform_admin;
  const routeParams = new URLSearchParams(location.search);
  const showCompanyTour = routeParams.get("tour") === "company";
  const tourAssistantId = routeParams.get("assistantId") || "";

  const [showAccountSettings, setShowAccountSettings] = useState(false);
  const [showAccountMenu, setShowAccountMenu] = useState(false);
  const [showLanguageSubmenu, setShowLanguageSubmenu] = useState(false);
  const [langSubmenuPos, setLangSubmenuPos] = useState({ top: 0, left: 0 });
  const accountMenuRef = useRef<HTMLDivElement>(null);
  const accountDropdownRef = useRef<HTMLDivElement>(null);
  const langSubmenuPortalRef = useRef<HTMLDivElement>(null);
  const langHoverCloseTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const [showNotifications, setShowNotifications] = useState(false);
  const [showTalentMarket, setShowTalentMarket] = useState(false);
  const [notifCategory, setNotifCategory] = useState<string>("all");
  const [selectedNotification, setSelectedNotification] = useState<any | null>(
    null,
  );
  const [showTenantMenu, setShowTenantMenu] = useState(false);
  const [showTenantSetupModal, setShowTenantSetupModal] = useState(false);
  const [tenantSearch, setTenantSearch] = useState("");
  const [joinInviteCode, setJoinInviteCode] = useState("");
  const [createCompanyName, setCreateCompanyName] = useState("");
  const [tenantFormLoading, setTenantFormLoading] = useState(false);
  const [tenantFormError, setTenantFormError] = useState("");
  const [allowSelfCreate, setAllowSelfCreate] = useState(true);
  const tenantSwitcherRef = useRef<HTMLButtonElement>(null);
  const tenantMenuPortalRef = useRef<HTMLDivElement>(null);
  const [tenantMenuPos, setTenantMenuPos] = useState({
    top: 0,
    left: 0,
    maxHeight: 520,
  });

  // Notification polling
  const { data: unreadCount = 0 } = useQuery({
    queryKey: ["notifications-unread"],
    queryFn: async () => {
      const res = await fetchJson<{ unread_count: number }>(
        "/notifications/unread-count",
      );
      return (res as any)?.unread_count || 0;
    },
    refetchInterval: 30000,
    enabled: !!user,
  });
  const { data: notifications = [] } = useQuery({
    queryKey: ["notifications", notifCategory],
    queryFn: () =>
      fetchJson<any[]>(
        `/notifications?limit=50${notifCategory !== "all" ? `&category=${notifCategory}` : ""}`,
      ),
    enabled: !!user && showNotifications,
  });
  const markAllRead = async () => {
    const token = localStorage.getItem("token");
    await fetch("/api/notifications/read-all", {
      method: "POST",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    queryClient.invalidateQueries({ queryKey: ["notifications-unread"] });
    queryClient.invalidateQueries({ queryKey: ["notifications"] });
  };
  const markOneRead = async (id: string) => {
    const token = localStorage.getItem("token");
    await fetch(`/api/notifications/${id}/read`, {
      method: "POST",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    queryClient.invalidateQueries({ queryKey: ["notifications-unread"] });
    queryClient.invalidateQueries({ queryKey: ["notifications"] });
  };

  // Tenant switching
  const { data: myTenants = [] } = useQuery({
    queryKey: ["my-tenants"],
    queryFn: async () => {
      const token = localStorage.getItem("token");
      const res = await fetch("/api/auth/my-tenants", {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!res.ok) return [];
      return res.json();
    },
    enabled: !!user,
  });

  const handleSwitchTenant = async (tenantId: string) => {
    const token = localStorage.getItem("token");
    const res = await fetch("/api/auth/switch-tenant", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ tenant_id: tenantId }),
    });
    if (!res.ok) {
      const err = await res
        .json()
        .catch(() => ({ detail: "Failed to switch tenant" }));
      toast.error(t("common.error.companySwitchFailed"), {
        details: String(err.detail || `HTTP ${res.status}`),
      });
      return;
    }
    const data = await res.json();
    if (data.redirect_url) {
      localStorage.setItem("token", data.access_token);
      const targetUrl = new URL(data.redirect_url, window.location.origin);
      if (targetUrl.hostname === window.location.hostname) {
        targetUrl.protocol = window.location.protocol;
        targetUrl.port = window.location.port;
      }
      targetUrl.pathname = "/";
      targetUrl.hash = "";
      window.location.href = targetUrl.toString();
    } else if (data.access_token) {
      localStorage.setItem("token", data.access_token);
      window.location.href = "/";
    }
  };

  // Open the tenant switcher modal — also fetch self-create config
  const openTenantModal = () => {
    setShowTenantMenu(true);
    setTenantSearch("");
  };

  const openTenantSetupModal = () => {
    setShowTenantMenu(false);
    setShowTenantSetupModal(true);
    setJoinInviteCode("");
    setCreateCompanyName("");
    setTenantFormError("");
    tenantApi
      .registrationConfig()
      .then((d: any) => {
        setAllowSelfCreate(d.allow_self_create_company);
      })
      .catch(() => {});
  };

  // Join company via invite code (inside modal)
  const handleModalJoin = async (e: React.FormEvent) => {
    e.preventDefault();
    setTenantFormError("");
    setTenantFormLoading(true);
    try {
      const result = await tenantApi.join(joinInviteCode);
      if (result.access_token) {
        // Multi-tenant: backend created a new User record, switch context
        localStorage.setItem("token", result.access_token);
      } else {
        // Registration flow: same user updated, refresh store
        const me = await authApi.me();
        const token = localStorage.getItem("token");
        if (token) setAuth(me, token);
      }
      setShowTenantMenu(false);
      setShowTenantSetupModal(false);
      window.location.href = "/onboarding?mode=join";
    } catch (err: any) {
      setTenantFormError(err.message || "Failed to join company");
    } finally {
      setTenantFormLoading(false);
    }
  };

  // Create company (inside modal)
  const handleModalCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    setTenantFormError("");
    setTenantFormLoading(true);
    try {
      const result = await tenantApi.selfCreate({ name: createCompanyName });
      if (result.access_token) {
        // Multi-tenant: backend created a new User record, switch context
        localStorage.setItem("token", result.access_token);
      } else {
        // Registration flow: same user updated, refresh store
        const me = await authApi.me();
        const token = localStorage.getItem("token");
        if (token) setAuth(me, token);
      }
      setShowTenantMenu(false);
      setShowTenantSetupModal(false);
      window.location.href = "/onboarding?mode=create";
    } catch (err: any) {
      setTenantFormError(err.message || "Failed to create company");
    } finally {
      setTenantFormLoading(false);
    }
  };

  // Theme
  const [theme, setTheme] = useState<"dark" | "light">(() => readSavedTheme());

  useEffect(() => {
    applyDocumentTheme(theme);
    saveTheme(theme);
  }, [theme]);

  const toggleTheme = () =>
    setTheme((prev) => (prev === "dark" ? "light" : "dark"));

  // Sidebar collapse state
  const isSidebarCollapsed = useAppStore((s) => s.sidebarCollapsed);
  const toggleSidebar = useAppStore((s) => s.toggleSidebar);
  const [isNarrowViewport, setIsNarrowViewport] = useState(() =>
    window.matchMedia("(max-width: 760px)").matches,
  );
  useEffect(() => {
    const media = window.matchMedia("(max-width: 760px)");
    const syncViewport = () => setIsNarrowViewport(media.matches);
    media.addEventListener("change", syncViewport);
    return () => media.removeEventListener("change", syncViewport);
  }, []);
  const sidebarCollapsed = isSidebarCollapsed || isNarrowViewport;

  // Sidebar agent search & pin
  const [sidebarSearch, setSidebarSearch] = useState("");
  const [agentDrawerOpen, setAgentDrawerOpen] = useState(false);
  const agentDrawerCloseTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const [pinnedAgents, setPinnedAgents] = useState<Set<string>>(() => {
    try {
      const stored = localStorage.getItem("pinned_agents");
      return stored ? new Set(JSON.parse(stored)) : new Set();
    } catch {
      return new Set();
    }
  });
  const togglePin = (agentId: string) => {
    setPinnedAgents((prev) => {
      const next = new Set(prev);
      if (next.has(agentId)) next.delete(agentId);
      else next.add(agentId);
      localStorage.setItem("pinned_agents", JSON.stringify([...next]));
      return next;
    });
  };

  // Use user's own tenant_id directly (no switching)
  const currentTenant = user?.tenant_id || "";
  const currentTenantName = useMemo(() => {
    const tenant = (myTenants as any[]).find(
      (item: any) => item.tenant_id === currentTenant,
    );
    return tenant?.tenant_name || (isChinese ? "当前公司" : "Current Company");
  }, [currentTenant, isChinese, myTenants]);
  const currentTenantLogoUrl = useMemo(() => {
    const tenant = (myTenants as any[]).find(
      (item: any) => item.tenant_id === currentTenant,
    );
    return tenant?.logo_url || "";
  }, [currentTenant, myTenants]);
  const currentTenantInitial =
    (
      Array.from(currentTenantName.trim())[0] as string | undefined
    )?.toUpperCase() || "C";
  const currentTenantAvatarTone = useMemo(
    () => getWorkspaceAvatarTone(currentTenantName),
    [currentTenantName],
  );
  const filteredTenants = useMemo(() => {
    const query = tenantSearch.trim().toLowerCase();
    if (!query) return myTenants as any[];
    return (myTenants as any[]).filter((tenant: any) =>
      (tenant.tenant_name || "").toLowerCase().includes(query),
    );
  }, [myTenants, tenantSearch]);

  // Keep tenant in localStorage for other components that read it
  useEffect(() => {
    if (currentTenant) {
      localStorage.setItem("current_tenant_id", currentTenant);
    }
  }, [currentTenant]);

  const agentDirectoryQuery = useQuery({
    queryKey: ["agents", "directory", currentTenant],
    queryFn: ({ signal }) =>
      agentApi.exploreAll({ tenantId: currentTenant, signal }),
    enabled: Boolean(currentTenant),
    refetchInterval: 30000,
  });
  const agentDirectory = agentDirectoryQuery.data;
  const agents = agentDirectory?.items || [];

  const openAgentDrawer = useCallback(() => {
    if (!sidebarCollapsed) return;
    if (agentDrawerCloseTimerRef.current) {
      clearTimeout(agentDrawerCloseTimerRef.current);
      agentDrawerCloseTimerRef.current = null;
    }
    setAgentDrawerOpen(true);
  }, [sidebarCollapsed]);

  const scheduleCloseAgentDrawer = useCallback(() => {
    if (agentDrawerCloseTimerRef.current)
      clearTimeout(agentDrawerCloseTimerRef.current);
    agentDrawerCloseTimerRef.current = setTimeout(() => {
      setAgentDrawerOpen(false);
      agentDrawerCloseTimerRef.current = null;
    }, 160);
  }, []);

  const handleLogout = async () => {
    await fetch("/api/pages/session", { method: "DELETE" }).catch(
      () => undefined,
    );
    await logout();
    navigate("/login");
  };

  const selectUiLanguage = (code: string) => {
    i18n.changeLanguage(code);
    setShowLanguageSubmenu(false);
    setShowAccountMenu(false);
  };

  const openLangSubmenu = useCallback(() => {
    if (langHoverCloseTimerRef.current) {
      clearTimeout(langHoverCloseTimerRef.current);
      langHoverCloseTimerRef.current = null;
    }
    setShowLanguageSubmenu(true);
  }, []);

  const scheduleCloseLangSubmenu = useCallback(() => {
    if (langHoverCloseTimerRef.current)
      clearTimeout(langHoverCloseTimerRef.current);
    langHoverCloseTimerRef.current = setTimeout(() => {
      setShowLanguageSubmenu(false);
      langHoverCloseTimerRef.current = null;
    }, 200);
  }, []);

  useEffect(() => {
    if (!showAccountMenu) {
      if (langHoverCloseTimerRef.current) {
        clearTimeout(langHoverCloseTimerRef.current);
        langHoverCloseTimerRef.current = null;
      }
      setShowLanguageSubmenu(false);
    }
  }, [showAccountMenu]);

  useEffect(
    () => () => {
      if (langHoverCloseTimerRef.current)
        clearTimeout(langHoverCloseTimerRef.current);
      if (agentDrawerCloseTimerRef.current)
        clearTimeout(agentDrawerCloseTimerRef.current);
    },
    [],
  );

  useEffect(() => {
    if (!sidebarCollapsed) setAgentDrawerOpen(false);
  }, [sidebarCollapsed]);

  const updateLangSubmenuPosition = useCallback(() => {
    const el = accountDropdownRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    setLangSubmenuPos({ top: r.top, left: r.right + 2 });
  }, []);

  const updateTenantMenuPosition = useCallback(() => {
    const el = tenantSwitcherRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const viewportPadding = 12;
    const menuWidth = 304;
    const preferredLeft = sidebarCollapsed ? rect.right + 8 : rect.left;
    const left = Math.min(
      Math.max(viewportPadding, preferredLeft),
      Math.max(
        viewportPadding,
        window.innerWidth - menuWidth - viewportPadding,
      ),
    );
    const top = Math.max(viewportPadding, rect.bottom + 8);
    const maxHeight = Math.max(220, window.innerHeight - top - viewportPadding);
    setTenantMenuPos({ top, left, maxHeight });
  }, [sidebarCollapsed]);

  useLayoutEffect(() => {
    if (!showLanguageSubmenu) return;
    updateLangSubmenuPosition();
    window.addEventListener("resize", updateLangSubmenuPosition);
    window.addEventListener("scroll", updateLangSubmenuPosition, true);
    return () => {
      window.removeEventListener("resize", updateLangSubmenuPosition);
      window.removeEventListener("scroll", updateLangSubmenuPosition, true);
    };
  }, [showLanguageSubmenu, updateLangSubmenuPosition]);

  useLayoutEffect(() => {
    if (!showTenantMenu) return;
    updateTenantMenuPosition();
    window.addEventListener("resize", updateTenantMenuPosition);
    window.addEventListener("scroll", updateTenantMenuPosition, true);
    return () => {
      window.removeEventListener("resize", updateTenantMenuPosition);
      window.removeEventListener("scroll", updateTenantMenuPosition, true);
    };
  }, [showTenantMenu, updateTenantMenuPosition]);

  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      const t = e.target as Node;
      if (accountMenuRef.current?.contains(t)) return;
      if (langSubmenuPortalRef.current?.contains(t)) return;
      if (tenantSwitcherRef.current?.contains(t)) return;
      if (tenantMenuPortalRef.current?.contains(t)) return;
      setShowAccountMenu(false);
      setShowTenantMenu(false);
    };
    if (showAccountMenu || showTenantMenu)
      document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [showAccountMenu, showTenantMenu]);

  return (
    <div
      className={`app-layout ${sidebarCollapsed ? "sidebar-collapsed" : ""}`}
    >
      <LayoutSidebar
        sidebarCollapsed={sidebarCollapsed}
        tenantSwitcherRef={tenantSwitcherRef}
        showTenantMenu={showTenantMenu}
        setShowTenantMenu={setShowTenantMenu}
        openTenantModal={openTenantModal}
        isChinese={isChinese}
        currentTenantAvatarTone={currentTenantAvatarTone}
        currentTenantInitial={currentTenantInitial}
        currentTenantLogoUrl={currentTenantLogoUrl}
        currentTenantName={currentTenantName}
        toggleSidebar={toggleSidebar}
        t={t}
        setShowTalentMarket={setShowTalentMarket}
        sidebarSearch={sidebarSearch}
        setSidebarSearch={setSidebarSearch}
        agentDirectoryQuery={agentDirectoryQuery}
        agentDirectory={agentDirectory}
        agents={agents}
        openAgentDrawer={openAgentDrawer}
        scheduleCloseAgentDrawer={scheduleCloseAgentDrawer}
        showNotifications={showNotifications}
        setShowNotifications={setShowNotifications}
        unreadCount={unreadCount}
        theme={theme}
        toggleTheme={toggleTheme}
        accountMenuRef={accountMenuRef}
        showAccountMenu={showAccountMenu}
        setShowAccountMenu={setShowAccountMenu}
        accountDropdownRef={accountDropdownRef}
        openLangSubmenu={openLangSubmenu}
        scheduleCloseLangSubmenu={scheduleCloseLangSubmenu}
        showLanguageSubmenu={showLanguageSubmenu}
        setShowAccountSettings={setShowAccountSettings}
        user={user}
        navigate={navigate}
        canAccessPlatformSettings={canAccessPlatformSettings}
        handleLogout={handleLogout}
        langSubmenuPortalRef={langSubmenuPortalRef}
        langSubmenuPos={langSubmenuPos}
        i18n={i18n}
        selectUiLanguage={selectUiLanguage}
        tenantMenuPortalRef={tenantMenuPortalRef}
        tenantMenuPos={tenantMenuPos}
        myTenants={myTenants}
        tenantSearch={tenantSearch}
        setTenantSearch={setTenantSearch}
        filteredTenants={filteredTenants}
        currentTenant={currentTenant}
        handleSwitchTenant={handleSwitchTenant}
        openTenantSetupModal={openTenantSetupModal}
        canAccessCompanySettings={canAccessCompanySettings}
        pinnedAgents={pinnedAgents}
        togglePin={togglePin}
        activeAgentId={activeAgentId}
        token={token}
        agentDrawerOpen={agentDrawerOpen}
        setAgentDrawerOpen={setAgentDrawerOpen}
      />
      <LayoutModals
        showTenantSetupModal={showTenantSetupModal}
        setShowTenantSetupModal={setShowTenantSetupModal}
        isChinese={isChinese}
        tenantFormError={tenantFormError}
        handleModalJoin={handleModalJoin}
        joinInviteCode={joinInviteCode}
        setJoinInviteCode={setJoinInviteCode}
        tenantFormLoading={tenantFormLoading}
        allowSelfCreate={allowSelfCreate}
        handleModalCreate={handleModalCreate}
        createCompanyName={createCompanyName}
        setCreateCompanyName={setCreateCompanyName}
        showNotifications={showNotifications}
        setShowNotifications={setShowNotifications}
        unreadCount={unreadCount}
        markAllRead={markAllRead}
        notifCategory={notifCategory}
        setNotifCategory={setNotifCategory}
        notifications={notifications}
        markOneRead={markOneRead}
        setSelectedNotification={setSelectedNotification}
        navigate={navigate}
        selectedNotification={selectedNotification}
      />
      <main
        className={`main-content${isChatPage ? " chat-page" : ""}${isAgentSettingsPage ? " agent-settings-page" : ""}${isProjectWorkspacePage ? " project-workspace-page" : ""}`}
      >
        <Outlet
          context={{ openTalentMarket: () => setShowTalentMarket(true) }}
        />
      </main>

      {showAccountSettings && (
        <AccountSettingsModal
          user={user}
          onClose={() => setShowAccountSettings(false)}
          isChinese={!!isChinese}
        />
      )}

      <TalentMarketModal
        open={showTalentMarket}
        onClose={() => setShowTalentMarket(false)}
      />
      {showCompanyTour && (
        <CompanyTourOverlay
          assistantId={tourAssistantId}
          isChinese={!!isChinese}
          onDone={async () => {
            try {
              await onboardingApi.complete();
            } catch {}
            navigate(
              tourAssistantId
                ? `/agents/${tourAssistantId}/chat?onboarding=1`
                : "/explore",
              { replace: true },
            );
          }}
        />
      )}
    </div>
  );
}
