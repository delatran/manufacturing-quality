import type {
  CreateOperation,
  OperationCase,
  OperationChanges,
  OperationSourceCheck,
  OperationStatus,
  OperationUpdate,
} from './operations-types';
import type { DashboardData } from './dashboard-types';
import { applicationUrl } from './app-url';

export class OperationsError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(applicationUrl(url), init);
  if (!response.ok) {
    let message = '';
    try {
      const body: unknown = await response.json();
      if (body && typeof body === 'object' && 'detail' in body && typeof body.detail === 'string') {
        message = body.detail;
      }
    } catch {
      /* Keep unexpected server responses out of the work form. */
    }
    throw new OperationsError(message || `Request failed (${response.status}).`, response.status);
  }
  return response.json() as Promise<T>;
}

function write<T>(url: string, body: unknown, method = 'POST') {
  return request<T>(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

const base = '/api/operations/cases';
const caseUrl = (id: string) => `${base}/${encodeURIComponent(id)}`;

export const listOperationCases = (signal?: AbortSignal) =>
  request<{ cases: OperationCase[]; total: number }>(base, { signal });
export const getOperationCase = (id: string) => request<OperationCase>(caseUrl(id));
export const getOperationContext = (id: string, signal?: AbortSignal) =>
  request<DashboardData>(`/api/dashboard/contexts/${encodeURIComponent(id)}`, { signal });
export const createOperationCase = (body: CreateOperation) => write<OperationCase>(base, body);
export const applyOperationUpdate = (
  id: string,
  expected_version: number,
  update: OperationUpdate,
) =>
  write<OperationCase>(`${caseUrl(id)}/updates`, { ...update, expected_version, confirmed: true });
export const updateOperationCase = (
  id: string,
  expected_version: number,
  changes: OperationChanges,
) => write<OperationCase>(caseUrl(id), { expected_version, changes }, 'PATCH');
export const transitionOperationCase = (
  id: string,
  expected_version: number,
  status: OperationStatus,
  note?: string,
  outcome?: string,
) =>
  write<OperationCase>(`${caseUrl(id)}/transitions`, {
    expected_version,
    status,
    note,
    outcome,
    confirmed: true,
  });
export const addOperationNote = (id: string, expected_version: number, text: string) =>
  write<OperationCase>(`${caseUrl(id)}/notes`, { expected_version, text });
export const checkOperationSource = (context_id: string) =>
  write<OperationSourceCheck>('/api/operations/check-source', { context_id });
export const attachOperationFollowUp = (id: string, expected_version: number, context_id: string) =>
  write<OperationCase>(`${caseUrl(id)}/follow-up`, {
    expected_version,
    context_id,
    confirmed: true,
  });
