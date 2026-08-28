// API wrappers for the CLI-tools backend at /api/tools/cli.

import type {
  BinaryVersion,
  CliTool,
  TestRunRequest,
  TestRunResponse,
} from './types';

// Backend refuses any top-level `binary` or `config` key on create /
// update (extra=forbid). Wire-level types here enumerate only the
// admin-editable fields so TypeScript catches accidental regressions at
// the call site.
export interface CliToolCreateBody {
  name: string;
  display_name: string;
  description?: string;
  env?: Record<string, string>;
  tenant_id?: string | null;
}

export interface CliToolUpdateBody {
  display_name?: string;
  description?: string;
  env?: Record<string, string>;
  is_active?: boolean;
}

export interface BinaryUploadProgress {
  loaded: number;
  total: number;
  percent: number;
  bytesPerSecond: number;
  etaSeconds: number | null;
  phase: 'uploading' | 'finalizing';
  resumed: boolean;
}

export interface BinaryUploadTask {
  promise: Promise<CliTool>;
  pause: () => void;
}

interface UploadStatus {
  upload_id: string;
  received: number;
  total: number;
  original_name: string;
}

const BINARY_CHUNK_BYTES = 8 * 1024 * 1024;

function authHeader(): HeadersInit {
  const token = localStorage.getItem('token') || '';
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function uploadStorageKey(toolId: string, file: File): string {
  return `cli-binary-upload:${toolId}:${file.name}:${file.size}:${file.lastModified}`;
}

async function readUploadStatus(toolId: string, uploadId: string): Promise<UploadStatus | null> {
  const res = await fetch(`/api/tools/cli/${toolId}/binary/uploads/${uploadId}`, {
    headers: authHeader(),
  });
  if (res.status === 404) return null;
  if (!res.ok) {
    throw new Error(`upload status failed: ${res.status} ${await res.text()}`);
  }
  return res.json() as Promise<UploadStatus>;
}

async function request<T>(url: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...authHeader(),
      ...(init.headers || {}),
    },
  });
  if (res.status === 204) return undefined as unknown as T;
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  return res.json() as Promise<T>;
}

export const cliToolsApi = {
  list: () => request<CliTool[]>('/api/tools/cli'),

  get: (id: string) => request<CliTool>(`/api/tools/cli/${id}`),

  create: (body: CliToolCreateBody) =>
    request<CliTool>('/api/tools/cli', { method: 'POST', body: JSON.stringify(body) }),

  update: (id: string, body: CliToolUpdateBody) =>
    request<CliTool>(`/api/tools/cli/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),

  delete: (id: string) =>
    request<void>(`/api/tools/cli/${id}`, { method: 'DELETE' }),

  testRun: (id: string, req: TestRunRequest) =>
    request<TestRunResponse>(`/api/tools/cli/${id}/test-run`, {
      method: 'POST',
      body: JSON.stringify(req),
    }),

  // Binary version history + rollback. The server owns retention (soft
  // cap of 5 by default) so the client just lists whatever survived and
  // lets the admin pick a rollback target.
  listVersions: (toolId: string) =>
    request<BinaryVersion[]>(`/api/tools/cli/${toolId}/versions`),

  rollback: (toolId: string, versionId: string, notes?: string) =>
    request<CliTool>(`/api/tools/cli/${toolId}/rollback`, {
      method: 'POST',
      body: JSON.stringify({ version_id: versionId, ...(notes ? { notes } : {}) }),
    }),

  // multipart — can't use the JSON request helper.
  uploadBinary: async (id: string, file: File): Promise<CliTool> => {
    const fd = new FormData();
    fd.append('file', file);
    const res = await fetch(`/api/tools/cli/${id}/binary`, {
      method: 'POST',
      headers: authHeader(),
      body: fd,
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`upload failed: ${res.status} ${text}`);
    }
    return res.json();
  },

  uploadBinaryResumable: (
    toolId: string,
    file: File,
    onProgress: (progress: BinaryUploadProgress) => void,
  ): BinaryUploadTask => {
    let activeRequest: XMLHttpRequest | null = null;
    let paused = false;

    const pause = () => {
      paused = true;
      activeRequest?.abort();
    };

    const promise = (async (): Promise<CliTool> => {
      const storageKey = uploadStorageKey(toolId, file);
      let uploadId = localStorage.getItem(storageKey) || crypto.randomUUID();
      let status = await readUploadStatus(toolId, uploadId);
      if (status && (status.total !== file.size || status.original_name !== file.name)) {
        status = null;
        uploadId = crypto.randomUUID();
      }
      if (!status) localStorage.setItem(storageKey, uploadId);

      let offset = status?.received ?? 0;
      if (offset > file.size) {
        uploadId = crypto.randomUUID();
        localStorage.setItem(storageKey, uploadId);
        offset = 0;
      }
      const resumed = offset > 0;
      const measuredFrom = offset;
      const startedAt = performance.now();

      const report = (loaded: number, phase: BinaryUploadProgress['phase']) => {
        const elapsedSeconds = Math.max((performance.now() - startedAt) / 1000, 0.001);
        const bytesPerSecond = Math.max(0, loaded - measuredFrom) / elapsedSeconds;
        onProgress({
          loaded,
          total: file.size,
          percent: file.size === 0 ? 0 : Math.min(100, (loaded / file.size) * 100),
          bytesPerSecond,
          etaSeconds: bytesPerSecond > 0 ? Math.max(0, file.size - loaded) / bytesPerSecond : null,
          phase,
          resumed,
        });
      };

      report(offset, 'uploading');
      while (offset < file.size) {
        if (paused) throw new DOMException('Upload paused', 'AbortError');
        const chunkStart = offset;
        const chunk = file.slice(chunkStart, Math.min(chunkStart + BINARY_CHUNK_BYTES, file.size));
        const params = new URLSearchParams({
          offset: String(chunkStart),
          total: String(file.size),
          original_name: file.name,
        });

        const next = await new Promise<UploadStatus>((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          activeRequest = xhr;
          xhr.open('PUT', `/api/tools/cli/${toolId}/binary/uploads/${uploadId}?${params}`);
          const token = localStorage.getItem('token');
          if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);
          xhr.setRequestHeader('Content-Type', 'application/octet-stream');
          xhr.upload.onprogress = (event) => report(chunkStart + event.loaded, 'uploading');
          xhr.onload = () => {
            activeRequest = null;
            if (xhr.status >= 200 && xhr.status < 300) {
              resolve(JSON.parse(xhr.responseText) as UploadStatus);
            } else {
              reject(new Error(`upload failed: ${xhr.status} ${xhr.responseText}`));
            }
          };
          xhr.onerror = () => {
            activeRequest = null;
            reject(new Error('upload failed: network error'));
          };
          xhr.onabort = () => {
            activeRequest = null;
            reject(new DOMException('Upload paused', 'AbortError'));
          };
          xhr.send(chunk);
        });
        offset = next.received;
        report(offset, 'uploading');
      }

      if (paused) throw new DOMException('Upload paused', 'AbortError');
      report(file.size, 'finalizing');
      const updated = await request<CliTool>(
        `/api/tools/cli/${toolId}/binary/uploads/${uploadId}/complete`,
        { method: 'POST' },
      );
      localStorage.removeItem(storageKey);
      return updated;
    })();

    return { promise, pause };
  },
};
