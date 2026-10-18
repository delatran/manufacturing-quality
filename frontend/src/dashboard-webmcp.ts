import { useLayoutEffect, useRef } from 'react';
import { applicationUrl } from './app-url';
import type { DashboardData, RecordsData } from './dashboard-types';
import type { Locale } from './i18n';
import { commitUi, objectInput, stringInput, throwIfAborted, waitForUi } from './webmcp';
import type { WebMcpTool } from './webmcp';

export const dashboardPanels = [
  'overview',
  'trend',
  'warnings',
  'location',
  'hotspots',
  'cost',
  'records',
  'evidence',
  'exports',
] as const;
export type DashboardPanel = (typeof dashboardPanels)[number];
export const recordSortOptions = [
  'defect_date',
  'defect_id',
  'product_name',
  'production_line',
  'severity',
  'repair_cost',
] as const;
export type DashboardExportKind = 'filtered_csv' | 'quality_workbook';
export type DashboardExportReceipt = {
  status: 'download_requested';
  kind: DashboardExportKind;
  context_id: string;
  filename: string;
  byte_length: number;
  content_type: string;
  content_sha256: string;
  etag: string | null;
  save_confirmed: false;
  scope: {
    kind: 'dashboard_context' | 'completed_full_case';
    dashboard_filters_applied: boolean;
    record_date_filter_applied: false;
    record_sort_and_page_applied: false;
  };
};
export type DashboardExportState = {
  context_id: string;
  kind: DashboardExportKind;
  status: 'preparing' | 'requested' | 'failed';
  receipt?: DashboardExportReceipt;
  error?: string;
};
export type DashboardWebMcpState = {
  data?: DashboardData;
  visible: boolean;
  loading: boolean;
  refreshing: boolean;
  error: string;
  locale: Locale;
  recordsExpanded: boolean;
  recordsDate?: string;
  evidencePriority: number | null;
  exportState?: DashboardExportState;
};
export type DashboardRecordTrace = {
  context?: DashboardData['context'];
  raw_records: { source_row: number; raw: Record<string, unknown> }[];
  raw_match_count?: number;
  canonical_records: Record<string, unknown>[];
  processed_records: Record<string, unknown>[];
  normalization_audit: Record<string, unknown>[];
  scenario_audit: Record<string, unknown>[];
  truncated: boolean;
};
export type DashboardTraceState = {
  url: string;
  defectId: number;
  status: 'loading' | 'ready' | 'error';
  trace?: DashboardRecordTrace;
  error?: string;
};
export type DashboardRecordsState = {
  data: DashboardData;
  visible: boolean;
  available: boolean;
  dateFilter?: string;
  page: number;
  sort: string;
  direction: string;
  reload: number;
  requestKey: string;
  records?: RecordsData;
  error?: string;
  loading: boolean;
  tracedEvent?: number;
  traceState?: DashboardTraceState;
};

/** Async tools read React's committed state, including unmounts and scope changes. */
export function useCommittedDashboardState<T>(state: T): () => T {
  const latest = useRef<{ mounted: boolean; state: T }>({ mounted: false, state });
  useLayoutEffect(() => {
    const committed = latest.current;
    committed.mounted = true;
    return () => {
      committed.mounted = false;
    };
  }, []);
  useLayoutEffect(() => {
    latest.current.state = state;
  });
  return () => {
    if (!latest.current.mounted) throw new Error('This Dashboard view is no longer mounted.');
    return latest.current.state;
  };
}

const contextSchema = { type: 'string', minLength: 1, maxLength: 256 };
const contextKeys = ['context_id'] as const;
const annotations = (readOnlyHint: boolean) => ({
  readOnlyHint,
  consequentialHint: false,
  untrustedContentHint: true,
});
function schema(properties: Record<string, unknown>, required: readonly string[]) {
  return { type: 'object', properties, required, additionalProperties: false };
}
function contextInput(value: unknown) {
  return stringInput(value, 'context_id', { minLength: 1, maxLength: 256 });
}
function integerInput(
  value: unknown,
  name: string,
  minimum: number,
  maximum = Number.MAX_SAFE_INTEGER,
) {
  if (!Number.isSafeInteger(value) || (value as number) < minimum || (value as number) > maximum)
    throw new TypeError(`${name} must be an integer between ${minimum} and ${maximum}.`);
  return value as number;
}
function requireContext(
  state: { data?: DashboardData; visible: boolean },
  contextId: string,
  signal?: AbortSignal,
) {
  throwIfAborted(signal);
  if (!state.visible) throw new Error('Open the Dashboard workspace before using this tool.');
  if (!state.data || state.data.context.context_id !== contextId)
    throw new Error('The Dashboard context changed. Read the current summary before trying again.');
  return state.data;
}
function requireDashboard(
  get: () => DashboardWebMcpState,
  contextId: string,
  signal?: AbortSignal,
) {
  const current = get();
  const data = requireContext(current, contextId, signal);
  if (current.loading || current.refreshing)
    throw new Error('The Dashboard is updating. Wait for the current view before trying again.');
  return { current, data };
}
function requireRecords(get: () => DashboardRecordsState, contextId: string, signal?: AbortSignal) {
  const current = get();
  requireContext(current, contextId, signal);
  if (!current.available)
    throw new Error('The Dashboard is updating. Wait for the current view before trying again.');
  return current;
}

/** Pagination describes a displayed backend series; it never recalculates its measures. */
function series<T>(rows: readonly T[] | undefined, offset: number, limit: number) {
  if (!rows) return null;
  const selected = rows.slice(offset, offset + limit);
  return {
    rows: selected,
    total: rows.length,
    offset,
    returned: selected.length,
    omitted: rows.length - selected.length,
    next_offset: offset + selected.length < rows.length ? offset + selected.length : null,
  };
}

export function dashboardPanelData(
  data: DashboardData,
  panel: DashboardPanel,
  offset: number,
  limit: number,
) {
  const a = data.analysis;
  switch (panel) {
    case 'overview':
      return {
        kpis: a.kpis,
        priorities: series(a.recommendations, offset, limit),
        period_halves: a.period_halves ?? null,
      };
    case 'trend':
      return {
        trend: a.trend,
        monthly: series(a.monthly, offset, limit),
        period_halves: a.period_halves ?? null,
        drivers: Object.fromEntries(
          Object.entries(a.decomposition ?? {}).map(([name, value]) => [
            name,
            { ...value, rows: series(value.rows, offset, limit) },
          ]),
        ),
        calendar_coverage: a.calendar_coverage ?? null,
      };
    case 'warnings':
      return {
        ...a.anomalies,
        candidates: series(a.anomalies.candidates, offset, limit),
        daily: series(a.anomalies.daily, offset, limit),
        calendar_coverage: a.calendar_coverage ?? null,
      };
    case 'location':
      return {
        locations: a.location_severity.locations,
        severities: a.location_severity.severities,
        totals: series(a.location_severity.location_totals, offset, limit),
        cells: series(a.location_severity.cells, offset, limit),
        association_test: a.location_severity.test,
        inspection_methods: a.inspection_methods
          ? { ...a.inspection_methods, rows: series(a.inspection_methods.rows, offset, limit) }
          : null,
      };
    case 'hotspots':
      return {
        lines: series(a.lines, offset, limit),
        category_summary: series(a.category_summary, offset, limit),
        line_category: series(a.line_category, offset, limit),
        shared_category_controls: series(a.shared_category_controls, offset, limit),
        event_ranking: series(a.products.top, offset, limit),
        intensity_ranking: series(a.products.intensity_top, offset, limit),
        line_comparison_note: a.line_comparison_note ?? null,
        ranking_note:
          'Both backend rankings are available to the panel. This read does not change its ranking controls.',
      };
    case 'cost':
      return {
        kpis: a.kpis,
        by_type: series(a.costs.by_type, offset, limit),
        by_severity: series(a.costs.by_severity, offset, limit),
        line_comparison: series(a.line_cost_comparison, offset, limit),
        distribution: a.cost_distribution ?? null,
        product_cost_ranking: series(a.products.cost_top, offset, limit),
        cost_pareto_product_count_80pct: a.products.cost_pareto_product_count_80pct,
      };
    case 'evidence':
      return {
        kpis: a.kpis,
        priorities: series(a.recommendations, offset, limit),
        receipt: {
          execution_id: data.evidence.execution_id,
          code_sha256: data.evidence.code_sha256,
          result_sha256: data.evidence.result_sha256,
        },
        definitions: a.definitions,
        limitations: a.metadata.limitations,
        omitted_receipt_fields: Object.keys(data.evidence).filter(
          (key) => !['execution_id', 'code_sha256', 'result_sha256'].includes(key),
        ),
      };
    case 'exports':
      return {
        kinds: ['filtered_csv', ...(data.links.quality_workbook ? ['quality_workbook'] : [])],
        csv_scope:
          'The applied Dashboard context. Record date, sort and page controls do not change the exported CSV.',
        workbook_scope: data.quality_workbook_scope ?? {
          kind: 'completed_full_case',
          filters_applied: false,
        },
      };
    case 'records':
      return {
        instruction:
          'Open the records panel, then use read_dashboard_records or set_dashboard_records_view for the committed table and its backend pagination.',
      };
  }
}

export function validateRecordDate(value: unknown, data: DashboardData): string | undefined {
  const text = stringInput(value, 'defect_date', { maxLength: 10 });
  if (text === '') return undefined;
  if (
    !/^\d{4}-\d{2}-\d{2}$/.test(text) ||
    new Date(`${text}T00:00:00Z`).toISOString().slice(0, 10) !== text
  )
    throw new TypeError(
      'defect_date must be a valid ISO calendar date, or an empty string to clear it.',
    );
  const month = text.slice(0, 7);
  if (month < data.context.filters.start_month || month > data.context.filters.end_month)
    throw new Error('defect_date must be within the currently applied Dashboard months.');
  return text;
}

type DashboardActions = {
  openPanel: (
    panel: DashboardPanel,
    details: { priority?: number; defectDate?: string; setDate: boolean },
    signal?: AbortSignal,
  ) => Promise<void>;
  requestExport: (
    kind: DashboardExportKind,
    contextId: string,
    signal?: AbortSignal,
  ) => Promise<DashboardExportReceipt>;
};
export function buildDashboardWebMcpTools(
  get: () => DashboardWebMcpState,
  actions: DashboardActions,
): WebMcpTool[] {
  return [
    {
      name: 'read_dashboard_summary',
      title: 'Read the applied Dashboard summary',
      description:
        'Read the currently displayed Dashboard context, backend KPIs, priorities, freshness and available panels. Optional context_id rejects a stale caller scope. Does not apply filters or run an Agent analysis.',
      inputSchema: schema({ context_id: contextSchema }, []),
      annotations: annotations(true),
      execute(input, { signal }) {
        const args = objectInput(input, contextKeys);
        throwIfAborted(signal);
        const current = get();
        if (!current.visible)
          throw new Error('Open the Dashboard workspace before using this tool.');
        if (args.context_id !== undefined)
          requireContext(current, contextInput(args.context_id), signal);
        if (!current.data || current.loading || current.refreshing)
          return {
            status: current.loading || current.refreshing ? 'updating' : 'unavailable',
            displayed_context_id: current.data?.context.context_id ?? null,
            error: current.error || null,
          };
        const data = current.data;
        return {
          status: 'ready',
          context: data.context,
          freshness: data.status,
          update_error: current.error || null,
          locale: current.locale,
          kpis: data.analysis.kpis,
          priorities: data.analysis.recommendations,
          definitions: data.analysis.definitions,
          limitations: data.analysis.metadata.limitations,
          panels: dashboardPanels,
          visible_state: {
            records_open: current.recordsExpanded,
            record_date: current.recordsDate ?? null,
            evidence_priority: current.evidencePriority,
          },
          export:
            current.exportState?.context_id === data.context.context_id
              ? current.exportState
              : null,
        };
      },
    },
    {
      name: 'read_dashboard_panel',
      title: 'Read Dashboard section values',
      description:
        'Read canonical backend values backing one section of the applied Dashboard. Series include explicit pagination and omission counts; offset applies to each series independently. This is read-only and does not open a section or change chart controls.',
      inputSchema: schema(
        {
          context_id: contextSchema,
          panel: { type: 'string', enum: dashboardPanels },
          offset: { type: 'integer', minimum: 0 },
          limit: { type: 'integer', minimum: 1, maximum: 100 },
        },
        ['context_id', 'panel'],
      ),
      annotations: annotations(true),
      execute(input, { signal }) {
        const args = objectInput(
          input,
          ['context_id', 'panel', 'offset', 'limit'],
          ['context_id', 'panel'],
        );
        const contextId = contextInput(args.context_id);
        const panel = stringInput(args.panel, 'panel', {
          values: dashboardPanels,
        }) as DashboardPanel;
        const offset = args.offset === undefined ? 0 : integerInput(args.offset, 'offset', 0);
        const limit = args.limit === undefined ? 25 : integerInput(args.limit, 'limit', 1, 100);
        const { data } = requireDashboard(get, contextId, signal);
        return {
          status: 'ready',
          context: data.context,
          freshness: data.status,
          panel,
          values: dashboardPanelData(data, panel, offset, limit),
        };
      },
    },
    {
      name: 'open_dashboard_panel',
      title: 'Open a Dashboard section',
      description:
        'Open and focus an existing Dashboard section. The records panel optionally uses the existing exact defect_date filter within the applied months; an empty string clears it. Evidence accepts a current priority number, or zero for the whole view. This completes navigation only: records may still be loading, and no Agent analysis is submitted.',
      inputSchema: schema(
        {
          context_id: contextSchema,
          panel: { type: 'string', enum: dashboardPanels },
          defect_date: {
            type: 'string',
            maxLength: 10,
            description:
              'Records only: YYYY-MM-DD, or an empty string to clear the exact-date filter.',
          },
          priority: {
            type: 'integer',
            minimum: 0,
            description:
              'Evidence only: a priority returned by the current summary, or zero for the whole view.',
          },
        },
        ['context_id', 'panel'],
      ),
      annotations: annotations(false),
      async execute(input, { signal }) {
        const args = objectInput(
          input,
          ['context_id', 'panel', 'defect_date', 'priority'],
          ['context_id', 'panel'],
        );
        const contextId = contextInput(args.context_id);
        const panel = stringInput(args.panel, 'panel', {
          values: dashboardPanels,
        }) as DashboardPanel;
        const { data } = requireDashboard(get, contextId, signal);
        if (args.defect_date !== undefined && panel !== 'records')
          throw new TypeError('defect_date is only supported by the records panel.');
        if (args.priority !== undefined && panel !== 'evidence')
          throw new TypeError('priority is only supported by the evidence panel.');
        const defectDate =
          args.defect_date === undefined ? undefined : validateRecordDate(args.defect_date, data);
        const priority =
          args.priority === undefined ? undefined : integerInput(args.priority, 'priority', 0);
        if (priority && !data.analysis.recommendations.some((item) => item.priority === priority))
          throw new Error('This priority is not in the current Dashboard context.');
        await actions.openPanel(
          panel,
          { priority, defectDate, setDate: args.defect_date !== undefined },
          signal,
        );
        const { current } = requireDashboard(get, contextId, signal);
        return {
          status: 'opened',
          context_id: contextId,
          panel,
          navigation_only: true,
          record_date: panel === 'records' ? (current.recordsDate ?? null) : undefined,
          evidence_priority: panel === 'evidence' ? current.evidencePriority : undefined,
          readback_tool: panel === 'records' ? 'read_dashboard_records' : 'read_dashboard_panel',
        };
      },
    },
    {
      name: 'request_dashboard_export',
      title: 'Request an existing Dashboard download',
      description:
        'Fetch the existing filtered CSV or full-case quality workbook and request a browser download after opening the exports section. Returns actual response bytes, MIME and SHA-256. download_requested does not confirm that the browser saved a file. CSV uses the Dashboard context, not record date/sort/page; the workbook ignores Dashboard filters.',
      inputSchema: schema(
        {
          context_id: contextSchema,
          kind: { type: 'string', enum: ['filtered_csv', 'quality_workbook'] },
        },
        ['context_id', 'kind'],
      ),
      annotations: annotations(false),
      async execute(input, { signal }) {
        const args = objectInput(input, ['context_id', 'kind'], ['context_id', 'kind']);
        const contextId = contextInput(args.context_id);
        const kind = stringInput(args.kind, 'kind', {
          values: ['filtered_csv', 'quality_workbook'],
        }) as DashboardExportKind;
        requireDashboard(get, contextId, signal);
        return actions.requestExport(kind, contextId, signal);
      },
    },
  ];
}

function recordsReadback(current: DashboardRecordsState) {
  const rows = current.records;
  return {
    status: current.loading ? 'loading' : current.error ? 'error' : 'ready',
    context: current.data.context,
    freshness: current.data.status,
    query: {
      page: current.page,
      sort_by: current.sort,
      sort_order: current.direction,
      defect_date: current.dateFilter ?? null,
    },
    records: rows
      ? {
          rows: rows.rows,
          total: rows.total,
          page: rows.page,
          page_size: rows.page_size,
          page_count: rows.page_count,
          returned: rows.rows.length,
          omitted: rows.total - rows.rows.length,
          next_page: rows.page < rows.page_count ? rows.page + 1 : null,
        }
      : null,
    open_trace_defect_id: current.tracedEvent ?? null,
    error: current.error ?? null,
  };
}
function assertRecordResponse(current: DashboardRecordsState) {
  const context = current.records?.context as { context_id?: unknown } | undefined;
  if (context?.context_id !== current.data.context.context_id)
    throw new Error('The records response does not match the current Dashboard context.');
}
function traceReadback(current: DashboardRecordsState) {
  const state = current.traceState;
  const trace = state?.trace;
  if (current.tracedEvent === undefined)
    return { status: 'closed', context_id: current.data.context.context_id };
  if (!state || state.defectId !== current.tracedEvent || state.url !== current.data.links.trace)
    return {
      status: 'loading',
      context_id: current.data.context.context_id,
      defect_id: current.tracedEvent,
    };
  if (trace && trace.context?.context_id !== current.data.context.context_id)
    throw new Error('The trace response does not match the current Dashboard context.');
  return {
    status: state.status,
    context: current.data.context,
    defect_id: current.tracedEvent,
    trace: trace
      ? {
          raw_records: trace.raw_records,
          raw_match_count: trace.raw_match_count ?? null,
          canonical_records: trace.canonical_records,
          normalization_audit: trace.normalization_audit,
          scenario_audit: trace.scenario_audit,
          truncated: trace.truncated,
        }
      : null,
    omitted_sections: ['processed_records', 'master_lineage'],
    scope_note:
      'Raw lineage can include source rows excluded or changed by canonical policy. The selected event was opened from this Dashboard records view.',
    error: state.error ?? null,
  };
}
export function buildDashboardRecordsTools(
  get: () => DashboardRecordsState,
  actions: {
    setView: (view: { page: number; sort: string; direction: string }) => void;
    openTrace: (id: number) => void;
  },
): WebMcpTool[] {
  return [
    {
      name: 'read_dashboard_records',
      title: 'Read the visible records page',
      description:
        'Read the current committed records table, exact-date filter, sorting, backend pagination and loading/error state. Returns only this page with explicit omission metadata. Open the records panel first.',
      inputSchema: schema({ context_id: contextSchema }, contextKeys),
      annotations: annotations(true),
      execute(input, { signal }) {
        const args = objectInput(input, contextKeys, contextKeys);
        const current = requireRecords(get, contextInput(args.context_id), signal);
        if (current.records) assertRecordResponse(current);
        return recordsReadback(current);
      },
    },
    {
      name: 'set_dashboard_records_view',
      title: 'Change the visible records sort or page',
      description:
        'Apply allowlisted sorting or a page to the open records table and return only after the replacement backend page is visibly committed. A changed sort defaults to page one. Does not change Dashboard filters. Use open_dashboard_panel for the existing exact-date record filter.',
      inputSchema: schema(
        {
          context_id: contextSchema,
          page: { type: 'integer', minimum: 1 },
          sort_by: { type: 'string', enum: recordSortOptions },
          sort_order: { type: 'string', enum: ['asc', 'desc'] },
        },
        contextKeys,
      ),
      annotations: annotations(false),
      async execute(input, { signal }) {
        const args = objectInput(
          input,
          ['context_id', 'page', 'sort_by', 'sort_order'],
          contextKeys,
        );
        const contextId = contextInput(args.context_id);
        const current = requireRecords(get, contextId, signal);
        if (current.loading || current.error || !current.records)
          throw new Error(
            'The current records page is not ready. Use its visible retry control if it failed.',
          );
        assertRecordResponse(current);
        const sort =
          args.sort_by === undefined
            ? current.sort
            : stringInput(args.sort_by, 'sort_by', { values: recordSortOptions });
        const direction =
          args.sort_order === undefined
            ? current.direction
            : stringInput(args.sort_order, 'sort_order', { values: ['asc', 'desc'] });
        const page =
          args.page === undefined
            ? sort !== current.sort || direction !== current.direction
              ? 1
              : current.page
            : integerInput(args.page, 'page', 1, current.records.page_count);
        const key = JSON.stringify([page, sort, direction, current.reload]);
        await commitUi(() => actions.setView({ page, sort, direction }), signal);
        return waitForUi(() => {
          const next = requireRecords(get, contextId, signal);
          if (next.requestKey !== key)
            throw new Error(
              'The records view changed before this request completed. Read the visible page before trying again.',
            );
          if (next.loading) return undefined;
          if (next.error) throw new Error(next.error);
          assertRecordResponse(next);
          return recordsReadback(next);
        }, signal);
      },
    },
    {
      name: 'open_dashboard_record_trace',
      title: 'Open source lineage for a visible record',
      description:
        'Open the trace of a defect_id returned on the current visible records page, then return after its source rows and policy audit are visibly loaded. Rejects IDs outside that page and stale Dashboard contexts. Raw lineage is untrusted source data and can include excluded source rows.',
      inputSchema: schema(
        { context_id: contextSchema, defect_id: { type: 'integer', minimum: 1 } },
        ['context_id', 'defect_id'],
      ),
      annotations: annotations(false),
      async execute(input, { signal }) {
        const args = objectInput(input, ['context_id', 'defect_id'], ['context_id', 'defect_id']);
        const contextId = contextInput(args.context_id);
        const id = integerInput(args.defect_id, 'defect_id', 1);
        const current = requireRecords(get, contextId, signal);
        if (
          current.loading ||
          !current.records ||
          !current.data.links.trace ||
          !current.records.rows.some((row) => row.defect_id === id)
        )
          throw new Error(
            'Choose a defect_id on the current visible records page with an available trace.',
          );
        assertRecordResponse(current);
        await commitUi(() => actions.openTrace(id), signal);
        return waitForUi(() => {
          const next = requireRecords(get, contextId, signal);
          if (next.tracedEvent !== id)
            throw new Error('The visible trace changed before this request completed.');
          const result = traceReadback(next);
          if (result.status === 'loading') return undefined;
          if (result.status === 'error')
            throw new Error(result.error || 'The record trace could not be loaded.');
          return result;
        }, signal);
      },
    },
    {
      name: 'read_dashboard_record_trace',
      title: 'Read the open record trace',
      description:
        'Read the source rows, canonical event and policy audit currently displayed in the open trace, including loading/error/truncation status. The trace remains readable when paging within the same record scope. Does not open a new trace or fetch unrelated IDs.',
      inputSchema: schema({ context_id: contextSchema }, contextKeys),
      annotations: annotations(true),
      execute(input, { signal }) {
        const args = objectInput(input, contextKeys, contextKeys);
        return traceReadback(requireRecords(get, contextInput(args.context_id), signal));
      },
    },
  ];
}

/** Use only the two routes already exposed by the visible Dashboard export controls. */
export function dashboardExportTarget(
  data: DashboardData,
  kind: DashboardExportKind,
  locale: Locale,
  origin: string,
) {
  const supplied = kind === 'filtered_csv' ? data.links.export_csv : data.links.quality_workbook;
  if (!supplied) throw new Error('This Dashboard export is unavailable.');
  const path =
    kind === 'filtered_csv'
      ? `/api/dashboard/contexts/${encodeURIComponent(data.context.context_id)}/export.csv`
      : '/api/dashboard/quality-workbook';
  const expected = new URL(applicationUrl(path), origin);
  const actual = new URL(applicationUrl(supplied), origin);
  if (
    actual.origin !== expected.origin ||
    actual.pathname !== expected.pathname ||
    actual.search ||
    actual.hash ||
    actual.username ||
    actual.password
  )
    throw new Error('This export link is not the expected same-origin Dashboard route.');
  if (kind === 'filtered_csv') actual.searchParams.set('locale', locale);
  return {
    url: actual.href,
    filename:
      kind === 'filtered_csv'
        ? `records-${data.context.context_id.slice(0, 16)}-${locale}.csv`
        : 'DATA_QUALITY-full-case.xlsx',
    mime:
      kind === 'filtered_csv'
        ? 'text/csv'
        : 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  };
}

export async function fetchDashboardExport(
  data: DashboardData,
  kind: DashboardExportKind,
  locale: Locale,
  origin: string,
  signal?: AbortSignal,
): Promise<{ blob: Blob; receipt: DashboardExportReceipt }> {
  throwIfAborted(signal);
  const target = dashboardExportTarget(data, kind, locale, origin);
  const response = await fetch(target.url, {
    signal,
    credentials: 'same-origin',
    redirect: 'error',
    cache: 'no-store',
  });
  if (!response.ok) throw new Error(`Dashboard export failed (${response.status}).`);
  const contentType = response.headers.get('Content-Type') ?? '';
  if (contentType.split(';', 1)[0].trim().toLowerCase() !== target.mime)
    throw new Error('The Dashboard export returned an unexpected content type.');
  const blob = await response.blob();
  if (!blob.size) throw new Error('The Dashboard export returned an empty file.');
  const digest = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer());
  const contentSha256 = [...new Uint8Array(digest)]
    .map((value) => value.toString(16).padStart(2, '0'))
    .join('');
  const etag = response.headers.get('ETag');
  if (etag && /^"[0-9a-f]{64}"$/i.test(etag) && etag.slice(1, -1).toLowerCase() !== contentSha256)
    throw new Error('The Dashboard export did not match its response checksum.');
  throwIfAborted(signal);
  return {
    blob,
    receipt: {
      status: 'download_requested',
      kind,
      context_id: data.context.context_id,
      filename: target.filename,
      byte_length: blob.size,
      content_type: contentType,
      content_sha256: contentSha256,
      etag,
      save_confirmed: false,
      scope: {
        kind: kind === 'filtered_csv' ? 'dashboard_context' : 'completed_full_case',
        dashboard_filters_applied: kind === 'filtered_csv',
        record_date_filter_applied: false,
        record_sort_and_page_applied: false,
      },
    },
  };
}
