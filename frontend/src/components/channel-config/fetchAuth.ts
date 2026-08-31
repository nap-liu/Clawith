export function fetchAuth<T>(url: string, options?: RequestInit): Promise<T> {
    const token = localStorage.getItem('token');
    return fetch(`/api${url}`, {
        ...options,
        headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    }).then(async r => {
        if (r.status === 204) {
            return undefined as T;
        }
        if (!r.ok) {
            const error = await r.json().catch(() => ({ detail: `HTTP ${r.status}` }));
            throw new Error(error.detail || `HTTP ${r.status}`);
        }
        return r.json() as Promise<T>;
    });
}
