import type { DashboardData, Filters } from './dashboard-types';
import type { Locale } from './i18n';
import { objectInput, stringInput, throwIfAborted } from './webmcp';
import type { WebMcpTool } from './webmcp';

export type WorkspaceTab = 'dashboard' | 'agent';
export type WorkspaceDashboardData = Pick<DashboardData, 'context' | 'options' | 'status'> & {
  analysis: Pick<DashboardData['analysis'], 'recommendations'>;
};
export type DashboardScopeResult = {
  status: 'applied' | 'failed' | 'superseded';
  context_id?: string;
  error?: string;
};
export type AnalysisStage = {
  id: number;
  context_id: string;
  status: 'pending' | 'staged' | 'not_staged';
};
export type WorkspaceSnapshot = {
  tab: WorkspaceTab;
  locale: Locale;
  data?: WorkspaceDashboardData;
  loading: boolean;
  refreshing: boolean;
  error: string;
  canAnalyze: boolean;
  sourceChecked: boolean;
  analysisStage?: AnalysisStage;
};
export type WorkspaceToolAdapter = {
  getSnapshot(): WorkspaceSnapshot;
  navigate(tab: WorkspaceTab, signal?: AbortSignal): Promise<void>;
  changeLanguage(locale: Locale, signal?: AbortSignal): Promise<void>;
  applyScope(filters: Filters, signal?: AbortSignal): Promise<DashboardScopeResult>;
  refreshScope(signal?: AbortSignal): Promise<DashboardScopeResult>;
  stageAnalysis(priority?: number, signal?: AbortSignal): Promise<AnalysisStage>;
};

const scopeOptions: Record<keyof Filters, string> = {
  start_month: 'months',
  end_month: 'months',
  production_line: 'production_lines',
  category: 'categories',
  severity: 'severities',
  defect_type: 'defect_types',
  defect_location: 'defect_locations',
  inspection_method: 'inspection_methods',
  product_id: 'product_ids',
  scenario: 'scenarios',
};
// These scenario identifiers are the existing backend policy choices, not new calculations.
const scenarios = ['', 'exclude_cost_10', 'ambiguous_dm', 'year_2024'] as const;
const filterKeys = Object.keys(scopeOptions) as (keyof Filters)[];
const contextProperty = {
  type: 'string',
  minLength: 1,
  description: 'The current Dashboard context_id returned by read_workspace_state.',
};
const filterProperties = Object.fromEntries(
  filterKeys.map((key) => [
    key,
    {
      type: 'string',
      description:
        key === 'scenario'
          ? 'An existing policy assumption; an empty string restores the canonical policy.'
          : `Use a value from scope_options.${key}. Empty strings clear optional filters.`,
      ...(key === 'scenario' ? { enum: scenarios } : {}),
    },
  ]),
);
const mutationHints = {
  readOnlyHint: false,
  consequentialHint: false,
  untrustedContentHint: true,
};

export function workspaceState(snapshot: WorkspaceSnapshot) {
  const { data } = snapshot;
  return {
    workspace: snapshot.tab,
    language: snapshot.locale,
    dashboard: {
      loading: snapshot.loading,
      refreshing: snapshot.refreshing,
      error: snapshot.error || null,
      context: data?.context ?? null,
      source_status: data?.status ?? null,
      source_checked: snapshot.sourceChecked,
      scope_options: data
        ? Object.fromEntries(
            filterKeys.map((key) => [
              key,
              key === 'scenario'
                ? [...scenarios]
                : [
                    ...(['start_month', 'end_month'].includes(key) ? [] : ['']),
                    ...(data.options[scopeOptions[key]] ?? []),
                  ],
            ]),
          )
        : null,
      recommendation_priorities: data?.analysis.recommendations.map((item) => item.priority) ?? [],
    },
    agent_available: snapshot.canAnalyze,
    analysis_staging: snapshot.analysisStage ?? null,
    analysis_staging_scope:
      'Most recent Dashboard staging receipt. Read Agent status for the current draft.',
    analysis_submission: 'Staging prepares a draft only. Submission is a separate Agent action.',
  };
}

function currentDashboard(
  snapshot: WorkspaceSnapshot,
  inputContext: unknown,
): WorkspaceDashboardData {
  const contextId = stringInput(inputContext, 'context_id', { minLength: 1 });
  if (snapshot.loading || snapshot.refreshing)
    throw new Error('The Dashboard scope is changing. Read workspace state when it finishes.');
  if (!snapshot.data || snapshot.data.context.context_id !== contextId)
    throw new Error('The Dashboard context is stale. Read workspace state before continuing.');
  return snapshot.data;
}

export function validateScopePatch(
  input: unknown,
  data: Pick<DashboardData, 'context' | 'options'>,
): Filters {
  const patch = objectInput(input, filterKeys);
  if (!Object.keys(patch).length)
    throw new TypeError('filters must change at least one scope field.');
  const next = { ...data.context.filters };
  for (const key of filterKeys) {
    if (!Object.hasOwn(patch, key)) continue;
    const values =
      key === 'scenario'
        ? scenarios
        : [
            ...(['start_month', 'end_month'].includes(key) ? [] : ['']),
            ...(data.options[scopeOptions[key]] ?? []),
          ];
    next[key] = stringInput(patch[key], key, { values });
  }
  if (next.start_month > next.end_month)
    throw new TypeError('start_month must be on or before end_month.');
  return next;
}

export function buildWorkspaceTools(adapter: WorkspaceToolAdapter): WebMcpTool[] {
  return [
    {
      name: 'read_workspace_state',
      title: 'Read current workspace',
      description:
        'Read the currently visible workspace, language, immutable Dashboard context, loading/source status, allowed scope values and analysis staging status. No state is changed. Read this before context-bound actions.',
      inputSchema: { type: 'object', properties: {}, additionalProperties: false },
      annotations: { ...mutationHints, readOnlyHint: true },
      execute(input, { signal }) {
        objectInput(input, []);
        throwIfAborted(signal);
        return workspaceState(adapter.getSnapshot());
      },
    },
    {
      name: 'navigate_workspace',
      title: 'Open Dashboard or Agent',
      description:
        'Open the selected workspace and wait for its mounted UI. This does not stage or submit a prompt. Discover tools again after changing workspace because tools follow the visible workspace.',
      inputSchema: {
        type: 'object',
        properties: { workspace: { type: 'string', enum: ['dashboard', 'agent'] } },
        required: ['workspace'],
        additionalProperties: false,
      },
      annotations: mutationHints,
      async execute(input, { signal }) {
        const value = objectInput(input, ['workspace'], ['workspace']);
        const target = stringInput(value.workspace, 'workspace', {
          values: ['dashboard', 'agent'],
        }) as WorkspaceTab;
        throwIfAborted(signal);
        await adapter.navigate(target, signal);
        throwIfAborted(signal);
        if (adapter.getSnapshot().tab !== target)
          throw new Error('Workspace navigation was superseded.');
        return { status: 'opened', ...workspaceState(adapter.getSnapshot()) };
      },
    },
    {
      name: 'set_workspace_language',
      title: 'Set workspace language',
      description:
        'Set the visible UI language to English or Vietnamese and return after it is committed. Existing answer content retains its own source language.',
      inputSchema: {
        type: 'object',
        properties: { language: { type: 'string', enum: ['en', 'vi'] } },
        required: ['language'],
        additionalProperties: false,
      },
      annotations: mutationHints,
      async execute(input, { signal }) {
        const value = objectInput(input, ['language'], ['language']);
        const next = stringInput(value.language, 'language', { values: ['en', 'vi'] }) as Locale;
        throwIfAborted(signal);
        await adapter.changeLanguage(next, signal);
        throwIfAborted(signal);
        if (adapter.getSnapshot().locale !== next)
          throw new Error('Language change was superseded.');
        return { status: 'applied', language: adapter.getSnapshot().locale };
      },
    },
    {
      name: 'apply_dashboard_scope',
      title: 'Apply Dashboard scope',
      description:
        'Patch the current Dashboard filters with known scope values, open Dashboard and wait for the canonical backend result to be displayed. Empty optional values clear filters. Existing Agent runs keep their immutable context. Scenario values are assumptions and do not establish observed improvements.',
      inputSchema: {
        type: 'object',
        properties: {
          context_id: contextProperty,
          filters: {
            type: 'object',
            properties: filterProperties,
            minProperties: 1,
            additionalProperties: false,
          },
        },
        required: ['context_id', 'filters'],
        additionalProperties: false,
      },
      annotations: mutationHints,
      async execute(input, { signal }) {
        const value = objectInput(input, ['context_id', 'filters'], ['context_id', 'filters']);
        const data = currentDashboard(adapter.getSnapshot(), value.context_id);
        const next = validateScopePatch(value.filters, data);
        throwIfAborted(signal);
        const result = await adapter.applyScope(next, signal);
        return { ...result, ...workspaceState(adapter.getSnapshot()) };
      },
    },
    {
      name: 'refresh_dashboard_source',
      title: 'Refresh Dashboard source',
      description:
        'Check the configured source through the existing Dashboard refresh action and display its canonical result for the current applied filters. A retained stale snapshot is explicitly reported. This does not edit source data or submit an Agent prompt.',
      inputSchema: {
        type: 'object',
        properties: { context_id: contextProperty },
        required: ['context_id'],
        additionalProperties: false,
      },
      annotations: mutationHints,
      async execute(input, { signal }) {
        const value = objectInput(input, ['context_id'], ['context_id']);
        currentDashboard(adapter.getSnapshot(), value.context_id);
        throwIfAborted(signal);
        const result = await adapter.refreshScope(signal);
        return { ...result, ...workspaceState(adapter.getSnapshot()) };
      },
    },
    {
      name: 'stage_dashboard_analysis',
      title: 'Stage Dashboard analysis',
      description:
        'Open Agent and place a Dashboard analysis request or a known recommendation priority into its visible draft, bound to the current immutable context. Returns staged only after the composer confirms it. This does not submit a request, start a model run or complete an analysis.',
      inputSchema: {
        type: 'object',
        properties: { context_id: contextProperty, priority: { type: 'integer', minimum: 1 } },
        required: ['context_id'],
        additionalProperties: false,
      },
      annotations: mutationHints,
      async execute(input, { signal }) {
        const value = objectInput(input, ['context_id', 'priority'], ['context_id']);
        const snapshot = adapter.getSnapshot();
        const data = currentDashboard(snapshot, value.context_id);
        if (!snapshot.canAnalyze) throw new Error('Agent capability is not currently available.');
        if (
          Object.hasOwn(value, 'priority') &&
          (typeof value.priority !== 'number' ||
            !Number.isInteger(value.priority) ||
            value.priority < 1 ||
            !data.analysis.recommendations.some((item) => item.priority === value.priority))
        )
          throw new TypeError('priority must identify a current Dashboard recommendation.');
        throwIfAborted(signal);
        const result = await adapter.stageAnalysis(value.priority as number | undefined, signal);
        return { ...result, submitted: false, workspace: adapter.getSnapshot().tab };
      },
    },
  ];
}
