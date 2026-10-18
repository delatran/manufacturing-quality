// Browser-only contract fixture. It mounts the real App against mock fetch, socket and registry.
import { createRoot } from 'react-dom/client';
import App from '../src/App';
import { createI18n, I18nProvider } from '../src/i18n';
import { initialFilters } from '../src/dashboard-types';
import type { DashboardData, Filters, Metrics } from '../src/dashboard-types';
import type { WebMcpContext, WebMcpTool } from '../src/webmcp';
import type { workspaceState } from '../src/workspace-webmcp';
const emptyMetrics: Metrics = {
  eligible_event_count: 0,
  reference_intensity_status: 'no_eligible_products',
  output_known_product_count: 0,
  output_unknown_product_count: 0,
  output_coverage_pct: null,
  known_monthly_output_subtotal: 0,
  cohort_products_with_events: 0,
  cohort_products_without_events: 0,
  products_with_events: 0,
  event_count: 0,
  critical_count: 0,
  critical_share_pct: null,
  linked_event_count: 0,
  unmatched_event_count: 0,
  known_repair_cost: 0,
  cost_known_count: 0,
  cost_unknown_count: 0,
  cost_coverage_pct: null,
  cost_available: false,
  cost_status: 'empty',
  average_known_repair_cost: null,
  reference_output: null,
  reference_intensity_per_1000: null,
  cohort_product_count: 0,
  month_count: 1,
};
const empty: DashboardData = {
  context: {
    context_id: 'context-fixture',
    snapshot_id: 'snapshot-fixture',
    filters: initialFilters,
    source_sha256: 'source-fixture',
    master_sha256: 'master-fixture',
    code_sha256: 'code-fixture',
    kpi_version: 'version-fixture',
    fetched_at: '2026-10-15T00:00:00Z',
  },
  options: { months: ['2024-01', '2024-06'] },
  evidence: {
    execution_id: 'receipt-fixture',
    code_sha256: 'code-fixture',
    result_sha256: 'result-fixture',
  },
  links: {
    records: '/records',
    evidence: '/evidence',
    export_csv: '/export.csv',
    export_zip: '/export.zip',
    quality_workbook: '/quality.xlsx',
  },
  status: {
    stale: false,
    last_error: null,
    last_attempt_at: '2026-10-16T00:00:00Z',
  },
  quality_scope: {
    kind: 'full_snapshot',
    filters_applied: false,
    description: 'Full source snapshot; filters do not change this audit.',
  },
  analysis: {
    kpis: emptyMetrics,
    monthly: [
      {
        ...emptyMetrics,
        month: '2024-01',
        label: 'Jan 2024',
        severity_counts: {},
      },
    ],
    trend: { interpretation: 'Synthetic empty scope.', change_pct: null },
    lines: [],
    location_severity: {
      locations: [],
      severities: [],
      location_totals: [],
      cells: [],
      test: {
        available: false,
        interpretation: 'No test available.',
        p_value: null,
        cramers_v: null,
        reliable: false,
      },
    },
    products: {
      top: [],
      all: [],
      cost_top: [],
      pareto_product_count_80pct: null,
      cost_pareto_product_count_80pct: null,
    },
    costs: { by_type: [], by_severity: [], by_location: [] },
    anomalies: {
      method: 'Synthetic fixed reference.',
      enabled: false,
      baseline_daily_mean: 0,
      upper_reference_limit: null,
      baseline_count: 0,
      candidates: [],
      daily: [],
      limitations: ['Not validated process control.'],
    },
    recommendations: [],
    quality: {
      raw_event_count: 0,
      in_scope_event_count: 0,
      quarantined_count: 0,
      exact_duplicate_count: 0,
      normalized_duplicate_count: 0,
      complementary_duplicate_count: 0,
      unmatched_event_count: 0,
      unknown_cost_count: 0,
      corrected_source_rows: 0,
      flagged_in_scope_event_count: 0,
      reconciliation: { raw_rows: 0, reconciled: true },
      counting_note: 'Overlapping flags.',
      audit_stage_note: 'Source audit.',
      issues: [],
      temporal_coverage: {
        calendar_days: 1,
        days_without_events: 1,
        days_with_events: 0,
      },
    },
    definitions: { event_count: 'Logged events, not defective units.' },
    metadata: {
      limitations: ['Actual exposure unavailable.'],
      reference_output_note: 'Static reference output.',
      fetched_at: '2026-10-15T00:00:00Z',
      stale: false,
      last_error: null,
    },
  },
};

type State = ReturnType<typeof workspaceState>;
function check(value: unknown, message: string): asserts value {
  if (!value) throw new Error(message);
}
const delay = (milliseconds = 20) =>
  new Promise<void>((resolve) => setTimeout(resolve, milliseconds));
async function waitFor(read: () => boolean | Promise<boolean>, message: string) {
  const until = Date.now() + 8000;
  while (!(await read()) && Date.now() < until) await delay();
  check(await read(), message);
}

export async function runWebMcpWorkspaceVerification() {
  const host = document.createElement('div');
  document.body.append(host);
  const root = createRoot(host);
  const originalFetch = window.fetch;
  const originalSocket = window.WebSocket;
  const contextDescriptor = Object.getOwnPropertyDescriptor(document, 'modelContext');
  const storage = new Map<string, string | null>();
  const keys = [
    'analyst.workspace.session.v1',
    'analyst.workspace.draft.v2',
    'analyst.workspace.pending-prompt.v1',
  ];
  keys.forEach((key) => {
    storage.set(key, localStorage.getItem(key));
    localStorage.removeItem(key);
  });
  const tools = new Map<string, WebMcpTool>();
  const registrations = new Map<string, number>();
  const context: WebMcpContext = {
    registerTool(tool, options) {
      check(!tools.has(tool.name), `Duplicate tool registration: ${tool.name}`);
      tools.set(tool.name, tool);
      registrations.set(tool.name, (registrations.get(tool.name) ?? 0) + 1);
      options?.signal?.addEventListener('abort', () => tools.delete(tool.name), { once: true });
      return Promise.resolve();
    },
  };
  Object.defineProperty(document, 'modelContext', { configurable: true, value: context });
  const sent: unknown[] = [];
  class FixtureSocket {
    static OPEN = 1;
    readyState = 0;
    onopen: ((event: Event) => void) | null = null;
    onmessage: ((event: MessageEvent) => void) | null = null;
    onclose: ((event: CloseEvent) => void) | null = null;
    onerror: ((event: Event) => void) | null = null;
    constructor() {
      queueMicrotask(() => {
        this.readyState = 1;
        this.onopen?.(new Event('open'));
      });
    }
    send(value: string) {
      sent.push(JSON.parse(value));
    }
    close() {
      this.readyState = 3;
    }
  }
  window.WebSocket = FixtureSocket as unknown as typeof WebSocket;
  let reads = 0;
  let failNext = false;
  let staleRefresh = false;
  let holdNext = false;
  let release: (() => void) | undefined;
  let held = false;
  empty.options = {
    months: ['2024-01', '2024-02', '2024-03', '2024-04', '2024-05', '2024-06'],
    severities: ['Critical'],
    production_lines: ['Line A'],
    categories: [],
    product_ids: [],
    defect_types: [],
    defect_locations: [],
    inspection_methods: [],
  };
  window.fetch = async (input, init) => {
    const url = new URL(String(input), location.href);
    let value: unknown;
    if (url.pathname === '/api/config')
      value = { mode: 'simulation', model: 'synthetic', ai_available: true };
    else if (url.pathname === '/api/dashboard' || url.pathname === '/api/dashboard/refresh') {
      const filters = url.pathname.endsWith('/refresh')
        ? (JSON.parse(String(init?.body)) as { filters: Filters }).filters
        : { ...initialFilters, ...Object.fromEntries(url.searchParams.entries()) };
      if (holdNext) {
        holdNext = false;
        held = true;
        await new Promise<void>((resolve) => {
          release = resolve;
        });
        held = false;
      }
      if (failNext) {
        failNext = false;
        return new Response(JSON.stringify({ detail: 'Fixture source unavailable' }), {
          status: 503,
        });
      }
      value = {
        ...structuredClone(empty),
        context: { ...empty.context, context_id: `fixture-context-${++reads}`, filters },
        status: { ...empty.status, stale: url.pathname.endsWith('/refresh') && staleRefresh },
      };
    } else if (
      url.pathname === '/api/sessions' ||
      url.pathname === '/api/sessions/webmcp-core-browser'
    )
      value = { id: 'webmcp-core-browser', events: [], runs: [] };
    else if (url.pathname === '/api/sessions/webmcp-core-browser/scope') value = {};
    else throw new Error(`Unexpected request in isolated App fixture: ${url.pathname}`);
    return new Response(JSON.stringify(value), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };
  const call = async (name: string, input: unknown, signal?: AbortSignal) => {
    const tool = tools.get(name);
    check(tool, `Tool is mounted: ${name}`);
    return tool.execute(input, { signal });
  };
  const state = async () => (await call('read_workspace_state', {})) as State;
  try {
    root.render(
      <I18nProvider initialLocale="en" persist={false}>
        <App />
      </I18nProvider>,
    );
    await waitFor(
      () =>
        tools.has('read_workspace_state') && host.querySelector('.scope-summary strong') !== null,
      'App and core tools mount',
    );
    await waitFor(
      () => !host.querySelector('.dashboard-loading') && reads > 0,
      'Initial canonical scope loads',
    );
    await waitFor(async () => !(await state()).dashboard.loading, 'Canonical context commits');
    let current = await state();
    await waitFor(
      () => tools.has('read_dashboard_summary'),
      'Visible Dashboard tools register after loading',
    );
    current = await state();
    check(
      !current.dashboard.loading && current.dashboard.context,
      'Readback has a committed context',
    );
    const initialContext = current.dashboard.context.context_id;
    let rejected = false;
    try {
      await call('apply_dashboard_scope', {
        context_id: initialContext,
        filters: { url: 'https://invalid.test' },
      });
    } catch {
      rejected = true;
    }
    check(rejected && reads === 1, 'Unknown input cannot reach the backend');
    await call('set_workspace_language', { language: 'vi' });
    check(
      host.querySelector<HTMLSelectElement>('.language-picker select')?.value === 'vi',
      'Language is visible before completion',
    );
    check((await state()).language === 'vi', 'Language state matches UI');
    await call('set_workspace_language', { language: 'en' });
    const applied = (await call('apply_dashboard_scope', {
      context_id: initialContext,
      filters: { severity: 'Critical', scenario: 'exclude_cost_10' },
    })) as { status: string };
    check(applied.status === 'applied', 'Scope application completes');
    current = await state();
    check(
      current.dashboard.context?.filters.severity === 'Critical',
      'Applied severity readback matches scope',
    );
    check(host.querySelector('.applied-scenario'), 'Scenario assumption badge is visible');
    check(
      host.querySelector('#dashboard-panel')?.hasAttribute('hidden') === false,
      'Applied Dashboard is visible',
    );
    rejected = false;
    try {
      await call('refresh_dashboard_source', { context_id: initialContext });
    } catch {
      rejected = true;
    }
    check(rejected, 'Old immutable context is rejected');
    const appliedContext = current.dashboard.context?.context_id;
    failNext = true;
    const failure = (await call('apply_dashboard_scope', {
      context_id: appliedContext,
      filters: { severity: '' },
    })) as { status: string };
    check(failure.status === 'failed', 'Failed scope is reported as failed');
    current = await state();
    check(
      current.dashboard.context?.context_id === appliedContext,
      'Failed scope preserves the previous context',
    );
    check(host.textContent?.includes('Fixture source unavailable'), 'Failure is visible');
    staleRefresh = true;
    await call('refresh_dashboard_source', { context_id: appliedContext });
    current = await state();
    check(
      current.dashboard.source_status?.stale && !current.dashboard.source_checked,
      'Stale retained refresh is not marked fresh',
    );
    holdNext = true;
    const delayed = call('apply_dashboard_scope', {
      context_id: current.dashboard.context?.context_id,
      filters: { severity: '' },
    });
    await waitFor(() => held, 'A scope request is pending');
    await call('navigate_workspace', { workspace: 'agent' });
    check(
      host.querySelector('#agent-panel .agent-workspace'),
      'Agent navigation waits for lazy mounted UI',
    );
    release?.();
    check(
      ((await delayed) as { status: string }).status === 'superseded',
      'Late completion cannot claim the Dashboard is still visible',
    );
    current = await state();
    check(current.workspace === 'agent', 'Late scope response does not navigate away from Agent');
    await call('navigate_workspace', { workspace: 'dashboard' });
    current = await state();
    const staged = (await call('stage_dashboard_analysis', {
      context_id: current.dashboard.context?.context_id,
    })) as { status: string; submitted: boolean };
    check(
      staged.status === 'staged' && staged.submitted === false,
      'Staging waits for composer acknowledgement without submitting',
    );
    const textarea = host.querySelector<HTMLTextAreaElement>('#agent-panel textarea');
    check(current.dashboard.context, 'The staged context is known');
    const language = createI18n('en');
    const expectedPrompt = language.t(
      'ui.analyze_this_dashboard_view_value_explain_the_main_trends_where_p',
      {
        value0: language.scope(current.dashboard.context.filters),
      },
    );
    check(
      textarea?.value === expectedPrompt,
      'The exact staged prompt is present in the actual composer',
    );
    check(sent.length === 0, 'Core navigation and staging never send model requests');
    const names = [
      'read_workspace_state',
      'navigate_workspace',
      'set_workspace_language',
      'apply_dashboard_scope',
      'refresh_dashboard_source',
      'stage_dashboard_analysis',
    ];
    check(
      names.every((name) => registrations.get(name) === 1),
      'Core tools register once across ordinary rerenders and language/scope changes',
    );
    return {
      status: 'passed',
      evidence_kind: 'isolated_browser_fixture',
      cases: [
        'mounted navigation',
        'English/Vietnamese state',
        'strict unknown input',
        'canonical scope commit',
        'visible scenario badge',
        'stale context rejection',
        'failure retains context',
        'retained stale refresh',
        'late navigation supersedes visible completion',
        'staging waits for real composer',
        'zero prompt sends',
        'stable core registration',
      ],
      native_webmcp_verified: false,
      live_provider_calls: 0,
    };
  } finally {
    release?.();
    root.unmount();
    host.remove();
    window.fetch = originalFetch;
    window.WebSocket = originalSocket;
    if (contextDescriptor) Object.defineProperty(document, 'modelContext', contextDescriptor);
    else Reflect.deleteProperty(document, 'modelContext');
    storage.forEach((value, key) => {
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    });
  }
}
