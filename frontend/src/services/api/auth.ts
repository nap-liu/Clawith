import type { TokenResponse, User } from "../../types";
import { request } from "./core";

export const authApi = {
  register: (data: {
    username?: string;
    email: string;
    password: string;
    display_name: string;
    invitation_code?: string;
    provider?: string;
    provider_code?: string;
  }) =>
    request<{
      user_id: string;
      email: string;
      access_token: string;
      message: string;
      user?: any;
      needs_company_setup: boolean;
    }>("/auth/register", { method: "POST", body: JSON.stringify(data) }),

  login: (data: {
    login_identifier: string;
    password: string;
    tenant_id?: string;
  }) =>
    request<
      | TokenResponse
      | {
          requires_tenant_selection: boolean;
          login_identifier: string;
          tenants: any[];
        }
    >("/auth/login", { method: "POST", body: JSON.stringify(data) }),

  forgotPassword: (data: { email: string }) =>
    request<{ ok: boolean; message: string }>("/auth/forgot-password", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  resetPassword: (data: { token: string; new_password: string }) =>
    request<{ ok: boolean }>("/auth/reset-password", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  emailHint: (username: string) =>
    request<{ hint: string }>(
      `/auth/email-hint?username=${encodeURIComponent(username)}`,
    ),

  me: () => request<User>("/auth/me"),

  validateSession: () =>
    request<User>("/auth/me", {}, { redirectOnUnauthorized: false }),

  updateMe: (data: Partial<User>) =>
    request<User>("/auth/me", { method: "PATCH", body: JSON.stringify(data) }),

  verifyEmail: (token: string) =>
    request<{
      ok: boolean;
      message: string;
      access_token: string;
      user: User;
      needs_company_setup: boolean;
    }>("/auth/verify-email", {
      method: "POST",
      body: JSON.stringify({ token }),
    }),

  resendVerification: (email: string) =>
    request<{ ok: boolean; message: string }>("/auth/resend-verification", {
      method: "POST",
      body: JSON.stringify({ email }),
    }),

  getMyTenants: () => request<any[]>("/auth/my-tenants"),

  switchTenant: (tenantId: string) =>
    request<{ access_token: string; redirect_url?: string; message?: string }>(
      "/auth/switch-tenant",
      { method: "POST", body: JSON.stringify({ tenant_id: tenantId }) },
    ),

  exchangeCode: (data: {
    provider: string;
    code: string;
    state?: string | null;
    redirect_uri: string;
    purpose: string;
    channel?: string;
    context?: Record<string, any>;
  }) =>
    request<TokenResponse>("/auth/code/exchange", {
      method: "POST",
      body: JSON.stringify(data),
    }),
};

export const tenantApi = {
  selfCreate: (data: { name: string }) =>
    request<any>("/tenants/self-create", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  join: (invitationCode: string) =>
    request<any>("/tenants/join", {
      method: "POST",
      body: JSON.stringify({ invitation_code: invitationCode }),
    }),

  registrationConfig: () =>
    request<{ allow_self_create_company: boolean }>(
      "/tenants/registration-config",
    ),

  resolveByDomain: (domain: string) =>
    request<any>(
      `/tenants/resolve-by-domain?domain=${encodeURIComponent(domain)}`,
    ),

  me: () =>
    request<{
      id: string;
      name: string;
      default_model_id: string | null;
      [k: string]: any;
    }>("/tenants/me"),

  tokenUsage: () => request<any>("/tenants/me/token-usage"),
};

export const onboardingApi = {
  status: () => request<any>("/onboarding/status"),

  start: (entryMode: "create" | "join") =>
    request<any>("/onboarding/start", {
      method: "POST",
      body: JSON.stringify({ entry_mode: entryMode }),
    }),

  createPersonalAssistant: (data: {
    name: string;
    personality: string;
    work_style: string;
    boundaries?: string;
  }) =>
    request<any>("/onboarding/personal-assistant", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  complete: () => request<any>("/onboarding/complete", { method: "POST" }),
};

export const adminApi = {
  listCompanies: () => request<any[]>("/admin/companies"),

  createCompany: (data: { name: string }) =>
    request<any>("/admin/companies", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  updateCompany: (id: string, data: any) =>
    request<any>(`/tenants/${id}`, {
      method: "PUT",
      body: JSON.stringify(data),
    }),

  deleteCompany: (id: string) =>
    request<void>(`/admin/companies/${id}`, { method: "DELETE" }),

  toggleCompany: (id: string) =>
    request<any>(`/admin/companies/${id}/toggle`, { method: "PUT" }),

  getPlatformSettings: () => request<any>("/admin/platform-settings"),

  updatePlatformSettings: (data: any) =>
    request<any>("/admin/platform-settings", {
      method: "PUT",
      body: JSON.stringify(data),
    }),

  listCompanyCodes: (companyId: string) =>
    request<any>(`/admin/companies/${companyId}/invitation-codes`),

  createCompanyCode: (companyId: string) =>
    request<any>(`/admin/companies/${companyId}/invitation-codes`, {
      method: "POST",
    }),
};
