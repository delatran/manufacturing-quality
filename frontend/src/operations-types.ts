import type { DashboardData } from './dashboard-types';

export type OperationContext = DashboardData['context'];
export type OperationStatus =
  'draft' | 'acknowledged' | 'investigating' | 'action_planned' | 'resolved' | 'dismissed';
export type OperationSource = {
  kind: 'manual' | 'recommendation' | 'historical_candidate' | 'source_check';
  key?: string;
};
export type OperationDraftRequest = {
  id: number;
  title: string;
  detail: string;
  actionPlan?: string;
  proposedOwner?: string;
  source?: OperationSource;
  contextId?: string;
  reportId?: string;
};
export type OperationReviewRequest = { id: number; caseId: string; contextId: string };
export type OperationComparison = {
  id: string;
  checked_at: string;
  baseline_context_id: string;
  current_context_id: string | null;
  source_changed: boolean | null;
  scope_unchanged: boolean;
  status: 'unchanged' | 'changed' | 'unavailable';
  summary: string;
  metrics: { key: string; before: number | null; after: number | null; delta: number | null }[];
  data_changes: {
    added_events: number | null;
    removed_events: number | null;
    changed_events: number | null;
  };
  coverage: {
    study_start: string;
    study_end: string;
    latest_event_date: string | null;
    actual_produced_units: null;
    inspected_units: null;
    operating_days: null;
    reporting_complete: null;
  };
  limitations: string[];
  review_required?: boolean;
};
export type OperationCase = {
  id: string;
  version: number;
  title: string;
  description: string;
  category: 'investigation' | 'corrective_action' | 'data_quality';
  priority: 'low' | 'medium' | 'high';
  status: OperationStatus;
  proposed_owner: string | null;
  assigned_owner: string | null;
  due_date: string | null;
  action_plan: string | null;
  outcome: string | null;
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
  context_id: string;
  origin_report_id: string | null;
  context: OperationContext;
  source: Omit<OperationSource, 'key'> & {
    key: string | null;
    summary: string;
    receipt_path: string | null;
  };
  notes: { id: number; text: string; created_at: string; actor: string }[];
  history: {
    id: number;
    at: string;
    actor: string;
    kind: string;
    from_status: OperationStatus | null;
    to_status: OperationStatus | null;
    changes: Record<string, unknown>;
    note: string | null;
  }[];
  allowed_transitions: OperationStatus[];
  is_overdue: boolean;
  follow_up: OperationComparison | null;
};
export type OperationChanges = Partial<
  Pick<
    OperationCase,
    | 'title'
    | 'description'
    | 'category'
    | 'priority'
    | 'proposed_owner'
    | 'assigned_owner'
    | 'due_date'
    | 'action_plan'
  >
>;
export type CreateOperation = OperationChanges & {
  title: string;
  context_id: string;
  source: OperationSource;
  idempotency_key: string;
  origin_report_id?: string | null;
};
export type OperationUpdate = {
  changes?: OperationChanges;
  status?: OperationStatus;
  note?: string;
  outcome?: string;
};
export type OperationSourceCheck = OperationComparison & { review_cases: OperationCase[] };

export const operationIsClosed = (status: OperationStatus) =>
  status === 'resolved' || status === 'dismissed';

export function operationDateIsValid(value: string) {
  if (!value) return true;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return (
    Number.isFinite(parsed.getTime()) &&
    parsed.getUTCFullYear() >= 1 &&
    parsed.toISOString().slice(0, 10) === value
  );
}
