import type { AppConfig, Session, UploadedFile } from './types';
import type { DashboardData, Filters, RecordsData } from './dashboard-types';
import { applicationUrl } from './app-url';
export class HttpError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
  }
}
export async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(applicationUrl(url), init);
  if (!response.ok) {
    const body = await response.text();
    let detail = body;
    try {
      const parsed = JSON.parse(body) as { detail?: unknown };
      const error = parsed.detail;
      detail =
        typeof error === 'string'
          ? error
          : error && typeof error === 'object' && 'message' in error
            ? String(error.message)
            : `Request failed (${response.status}).`;
    } catch {
      /* Plain HTTP error. */
    }
    throw new HttpError(detail || `Request failed (${response.status}).`, response.status);
  }
  return response.json() as Promise<T>;
}
let configPromise: Promise<AppConfig> | null = null;
export const getConfig = () => {
  if (!configPromise) {
    configPromise = request<AppConfig>('/api/config').catch((err) => {
      configPromise = null;
      throw err;
    });
  }
  return configPromise;
};
export const getDashboard = (filters: Filters, signal?: AbortSignal) =>
  request<DashboardData>(`/api/dashboard?${new URLSearchParams(filters)}`, {
    signal,
  });
export const getDashboardContext = (contextId: string, signal?: AbortSignal) =>
  request<DashboardData>(`/api/dashboard/contexts/${encodeURIComponent(contextId)}`, { signal });
export const refreshDashboard = (filters: Filters) =>
  request<DashboardData>('/api/dashboard/refresh', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filters }),
  });
export const getRecords = (
  url: string,
  page: number,
  sort: string,
  direction: string,
  signal?: AbortSignal,
  defectDate?: string,
) =>
  request<RecordsData>(
    `${url}${url.includes('?') ? '&' : '?'}${new URLSearchParams({ page: String(page), page_size: '20', sort_by: sort, sort_order: direction, ...(defectDate ? { defect_date: defectDate } : {}) })}`,
    { signal },
  );
export const createSession = () => request<Session>('/api/sessions', { method: 'POST' });
export const getSession = (id: string) =>
  request<Session>(`/api/sessions/${encodeURIComponent(id)}`);
export const uploadFiles = (id: string, files: File[]) => {
  const body = new FormData();
  files.forEach((file) => body.append('files', file));
  return request<{ files: UploadedFile[] }>(`/api/sessions/${encodeURIComponent(id)}/files`, {
    method: 'POST',
    body,
  });
};
export function socketUrl(id: string, cursor?: string | number) {
  const url = new URL(
    applicationUrl(`/api/sessions/${encodeURIComponent(id)}/ws`),
    window.location.href,
  );
  url.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  if (cursor !== undefined) url.searchParams.set('after', String(cursor));
  return url.toString();
}
export function artifactUrl(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  try {
    const url = new URL(value, window.location.origin);
    if (url.origin !== window.location.origin || !['http:', 'https:'].includes(url.protocol))
      return undefined;
    if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/assets/'))
      url.pathname = applicationUrl(url.pathname);
    return url.href;
  } catch {
    return undefined;
  }
}
