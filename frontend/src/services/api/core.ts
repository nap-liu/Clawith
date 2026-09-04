/** Shared HTTP and upload helpers for frontend API services. */

const API_BASE = "/api";

export async function clearAuthCredentials(): Promise<void> {
  const token = localStorage.getItem("token");
  localStorage.removeItem("token");
  localStorage.removeItem("user");
  try {
    await fetch("/api/auth/logout", {
      method: "POST",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      credentials: "same-origin",
      keepalive: true,
    });
  } catch {
    // Local credentials are already cleared; network failure must not restore them.
  }
}

type RequestBehavior = {
  redirectOnUnauthorized?: boolean;
};

async function request<T>(
  url: string,
  options: RequestInit = {},
  behavior: RequestBehavior = {},
): Promise<T> {
  const token = localStorage.getItem("token");
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };

  const res = await fetch(`${API_BASE}${url}`, { ...options, headers });

  if (!res.ok) {
    const isAuthEndpoint =
      url.startsWith("/auth/login") ||
      url.startsWith("/auth/register") ||
      url.startsWith("/auth/code/exchange") ||
      url.startsWith("/auth/verify-email") ||
      url.startsWith("/auth/resend-verification") ||
      url.startsWith("/auth/forgot-password") ||
      url.startsWith("/auth/reset-password");
    if (
      res.status === 401 &&
      !isAuthEndpoint &&
      behavior.redirectOnUnauthorized !== false
    ) {
      void clearAuthCredentials();
      const onLoginPage = window.location.pathname === "/login";
      const loginParams = onLoginPage
        ? ""
        : `?${new URLSearchParams({ return_to: window.location.href })}`;
      window.location.replace(`/login${loginParams}`);
      throw new Error("Session expired");
    }
    const bodyText = await res.text();
    let error: { detail?: unknown };
    try {
      error = bodyText ? JSON.parse(bodyText) : {};
    } catch {
      const snippet = bodyText.trim().slice(0, 280);
      error = {
        detail: snippet || `HTTP ${res.status} ${res.statusText || ""}`.trim(),
      };
    }
    const fieldLabels: Record<string, string> = {
      name: "名称",
      role_description: "角色描述",
      agent_type: "数字员工类型",
      primary_model_id: "主模型",
      max_tokens_per_day: "每日 Token 上限",
      max_tokens_per_month: "每月 Token 上限",
    };
    let message = "";
    if (Array.isArray(error.detail)) {
      message = error.detail
        .map((e: any) => {
          const field = e.loc?.slice(-1)[0] || "";
          const label = fieldLabels[field] || field;
          return label ? `${label}: ${e.msg}` : e.msg;
        })
        .join("; ");
    } else if (typeof error.detail === "object" && error.detail !== null) {
      message =
        (error.detail as Record<string, any>).message || `HTTP ${res.status}`;
    } else {
      const detail = error.detail;
      if (typeof detail === "string") message = detail;
      else if (detail != null && typeof detail === "object") {
        message = JSON.stringify(detail);
      } else {
        message = `HTTP ${res.status}`;
      }
    }

    const apiErr: any = new Error(message);
    apiErr.status = res.status;
    apiErr.detail = error.detail;
    throw apiErr;
  }

  if (res.status === 204) return undefined as T;
  return res.json();
}

const fetchJson = request;

async function uploadFile(
  url: string,
  file: File,
  extraFields?: Record<string, string>,
): Promise<any> {
  const token = localStorage.getItem("token");
  const formData = new FormData();
  formData.append("file", file);
  if (extraFields) {
    for (const [key, value] of Object.entries(extraFields)) {
      formData.append(key, value);
    }
  }
  const res = await fetch(`${API_BASE}${url}`, {
    method: "POST",
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: formData,
  });
  if (!res.ok) {
    const error = await res.json().catch(() => ({ detail: "Upload failed" }));
    throw new Error(error.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

export function uploadFileWithProgress(
  url: string,
  file: File,
  onProgress?: (percent: number) => void,
  extraFields?: Record<string, string>,
  timeoutMs: number = 120_000,
): { promise: Promise<any>; abort: () => void } {
  const xhr = new XMLHttpRequest();
  const promise = new Promise<any>((resolve, reject) => {
    const token = localStorage.getItem("token");
    const formData = new FormData();
    formData.append("file", file);
    if (extraFields) {
      for (const [key, value] of Object.entries(extraFields)) {
        formData.append(key, value);
      }
    }
    xhr.open("POST", `${API_BASE}${url}`);
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    };
    xhr.upload.onload = () => {
      if (onProgress) onProgress(101);
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText));
        } catch {
          resolve(undefined);
        }
      } else {
        try {
          const err = JSON.parse(xhr.responseText);
          reject(new Error(err.detail || `HTTP ${xhr.status}`));
        } catch {
          reject(new Error(`HTTP ${xhr.status}`));
        }
      }
    };
    xhr.onerror = () => reject(new Error("Network error"));
    xhr.ontimeout = () => reject(new Error("Upload timed out"));
    xhr.onabort = () => reject(new Error("Upload cancelled"));
    xhr.timeout = timeoutMs;
    xhr.send(formData);
  });
  return { promise, abort: () => xhr.abort() };
}

export { API_BASE, fetchJson, request, uploadFile };
