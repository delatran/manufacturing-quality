import assert from 'node:assert/strict';
import {
  buildDashboardRecordsTools,
  buildDashboardWebMcpTools,
  dashboardExportTarget,
  fetchDashboardExport,
  validateRecordDate,
} from '../src/dashboard-webmcp';
import type { DashboardRecordsState, DashboardWebMcpState } from '../src/dashboard-webmcp';
import type { WebMcpTool } from '../src/webmcp';
import { dashboardFixture, recordsFixture } from './dashboard-webmcp-fixture';

// Isolated contracts with synthetic actions, responses and a fake animation scheduler.
// Native registration, real rendering, provider behavior and browser saving are not tested here.
const frames = new Map<number, ReturnType<typeof setTimeout>>();
let frameId = 0;
globalThis.requestAnimationFrame = (callback) => {
  const id = ++frameId;
  frames.set(
    id,
    setTimeout(() => {
      frames.delete(id);
      callback(performance.now());
    }, 0),
  );
  return id;
};
globalThis.cancelAnimationFrame = (id) => {
  clearTimeout(frames.get(id));
  frames.delete(id);
};
const invoke = (tools: WebMcpTool[], name: string, input: unknown, signal?: AbortSignal) =>
  Promise.resolve().then(() => {
    const tool = tools.find((item) => item.name === name);
    assert.ok(tool, `Tool exists: ${name}`);
    return tool.execute(input, { signal });
  });

const data = dashboardFixture();
const before = JSON.stringify(data);
let state: DashboardWebMcpState = {
  data,
  visible: true,
  loading: false,
  refreshing: false,
  error: '',
  locale: 'en',
  recordsExpanded: false,
  evidencePriority: null,
};
let opened = 0;
let downloads = 0;
const tools = buildDashboardWebMcpTools(() => state, {
  async openPanel(panel, details) {
    opened++;
    if (panel === 'records')
      state = {
        ...state,
        recordsExpanded: true,
        recordsDate: details.setDate ? details.defectDate : state.recordsDate,
      };
    if (panel === 'evidence') state = { ...state, evidencePriority: details.priority ?? 0 };
  },
  async requestExport() {
    downloads++;
    throw new Error('Synthetic export action.');
  },
});
for (const tool of tools) {
  assert.equal(tool.inputSchema.additionalProperties, false);
  assert.equal(tool.annotations.untrustedContentHint, true);
  assert.equal(tool.annotations.consequentialHint, false);
  assert.equal(tool.annotations.readOnlyHint, tool.name.startsWith('read_'));
}
const summary = (await invoke(tools, 'read_dashboard_summary', {})) as {
  kpis: unknown;
  context: unknown;
};
assert.deepEqual(summary.kpis, data.analysis.kpis);
assert.deepEqual(summary.context, data.context);
state = { ...state, error: 'Synthetic update failure; the previous applied view is still shown.' };
assert.equal(
  ((await invoke(tools, 'read_dashboard_summary', {})) as { update_error: string }).update_error,
  state.error,
);
state = { ...state, error: '' };
await assert.rejects(
  invoke(tools, 'read_dashboard_summary', { extra: 'ignored?' }),
  /unknown property/,
);
await assert.rejects(
  invoke(tools, 'read_dashboard_summary', { context_id: 'old-context' }),
  /context changed/,
);
state = { ...state, loading: true };
assert.equal(
  ((await invoke(tools, 'read_dashboard_summary', {})) as { status: string }).status,
  'updating',
);
await assert.rejects(
  invoke(tools, 'open_dashboard_panel', { context_id: data.context.context_id, panel: 'cost' }),
  /updating/,
);
state = { ...state, loading: false, visible: false };
await assert.rejects(invoke(tools, 'read_dashboard_summary', {}), /Open the Dashboard/);
state = { ...state, visible: true };
await assert.rejects(
  invoke(tools, 'read_dashboard_panel', {
    context_id: data.context.context_id,
    panel: 'sql',
    limit: 1,
  }),
  /allowed string/,
);
await assert.rejects(
  invoke(tools, 'read_dashboard_panel', {
    context_id: data.context.context_id,
    panel: 'overview',
    limit: 101,
  }),
  /integer/,
);
const evidence = (await invoke(tools, 'read_dashboard_panel', {
  context_id: data.context.context_id,
  panel: 'evidence',
  offset: 1,
  limit: 1,
})) as {
  values: { priorities: { returned: number; omitted: number }; omitted_receipt_fields: string[] };
};
assert.deepEqual(evidence.values.priorities, {
  rows: [],
  total: 1,
  offset: 1,
  returned: 0,
  omitted: 1,
  next_offset: null,
});
assert.ok(evidence.values.omitted_receipt_fields.includes('numeric_values'));
for (const [extra, expected] of [
  [{ panel: 'records', defect_date: '2024-02-30' }, /valid ISO/],
  [{ panel: 'records', defect_date: '2025-01-03' }, /applied Dashboard months/],
  [{ panel: 'cost', defect_date: '2024-01-03' }, /only supported/],
  [{ panel: 'evidence', priority: 99 }, /not in the current/],
] as const) {
  await assert.rejects(
    invoke(tools, 'open_dashboard_panel', { context_id: data.context.context_id, ...extra }),
    expected,
  );
}
assert.equal(opened, 0, 'Rejected navigation never calls the UI action');
const navigation = (await invoke(tools, 'open_dashboard_panel', {
  context_id: data.context.context_id,
  panel: 'records',
  defect_date: '2024-01-03',
})) as { status: string; navigation_only: boolean; record_date: string };
assert.equal(navigation.status, 'opened');
assert.equal(navigation.navigation_only, true);
assert.equal(navigation.record_date, '2024-01-03');
await invoke(tools, 'open_dashboard_panel', {
  context_id: data.context.context_id,
  panel: 'records',
  defect_date: '',
});
assert.equal(state.recordsDate, undefined);
assert.equal(validateRecordDate('2024-02-29', data), '2024-02-29');
await assert.rejects(
  invoke(tools, 'request_dashboard_export', { context_id: data.context.context_id, kind: 'zip' }),
  /allowed string/,
);
assert.equal(downloads, 0);

let recordsState: DashboardRecordsState = {
  data,
  visible: true,
  available: true,
  page: 1,
  sort: 'defect_date',
  direction: 'desc',
  reload: 0,
  requestKey: JSON.stringify([1, 'defect_date', 'desc', 0]),
  records: recordsFixture(data),
  loading: false,
};
let deferRecords: (() => void) | undefined;
const recordTools = buildDashboardRecordsTools(() => recordsState, {
  setView(view) {
    recordsState = {
      ...recordsState,
      ...view,
      requestKey: JSON.stringify([view.page, view.sort, view.direction, recordsState.reload]),
      records: undefined,
      loading: true,
    };
    deferRecords = () => {
      recordsState = {
        ...recordsState,
        records: recordsFixture(recordsState.data, view.page, view.sort, view.direction),
        loading: false,
      };
    };
  },
});
assert.deepEqual(
  recordTools.map((tool) => tool.name).sort(),
  ['read_dashboard_records', 'set_dashboard_records_view'],
  'Records expose only page reading and view controls',
);
const binding = { context_id: data.context.context_id };
await assert.rejects(
  invoke(recordTools, 'set_dashboard_records_view', { ...binding, page: 3 }),
  /integer/,
);
await assert.rejects(
  invoke(recordTools, 'set_dashboard_records_view', { ...binding, sort_by: 'arbitrary_sql' }),
  /allowed string/,
);
let pageDone = false;
const pagePromise = invoke(recordTools, 'set_dashboard_records_view', {
  ...binding,
  page: 2,
  sort_by: 'repair_cost',
  sort_order: 'asc',
}).then((value) => {
  pageDone = true;
  return value;
});
await new Promise((resolve) => setTimeout(resolve, 5));
assert.equal(
  pageDone,
  false,
  'The tool cannot report page success before the loaded state commits',
);
assert.ok(deferRecords);
deferRecords();
const pageResult = (await pagePromise) as {
  status: string;
  records: { page: number; omitted: number };
  query: { sort_by: string };
};
assert.equal(pageResult.status, 'ready');
assert.equal(pageResult.records.page, 2);
assert.equal(pageResult.records.omitted, 20);
assert.equal(pageResult.query.sort_by, 'repair_cost');
assert.deepEqual(
  Object.keys(pageResult).sort(),
  ['context', 'error', 'freshness', 'query', 'records', 'status'],
  'Records readback contains only the current page, scope and status',
);
recordsState = {
  ...recordsState,
  records: { ...recordsFixture(data), context: { context_id: 'foreign' } },
};
await assert.rejects(invoke(recordTools, 'read_dashboard_records', binding), /does not match/);
recordsState = { ...recordsState, records: recordsFixture(data) };
const scopeRace = invoke(recordTools, 'set_dashboard_records_view', { ...binding, page: 1 });
const scopeRaceCheck = assert.rejects(scopeRace, /context changed/);
await new Promise((resolve) => setTimeout(resolve, 5));
recordsState = { ...recordsState, data: dashboardFixture('new-context') };
await scopeRaceCheck;
recordsState = { ...recordsState, data, records: recordsFixture(data), loading: false, page: 1 };
const abort = new AbortController();
const canceled = invoke(
  recordTools,
  'set_dashboard_records_view',
  { ...binding, page: 2 },
  abort.signal,
);
const cancelCheck = assert.rejects(canceled, { name: 'AbortError' });
await new Promise((resolve) => setTimeout(resolve, 5));
abort.abort();
await cancelCheck;

const originalFetch = globalThis.fetch;
const content = new TextEncoder().encode('defect_id,repair_cost\n11,10\n');
const contentHash = [...new Uint8Array(await crypto.subtle.digest('SHA-256', content))]
  .map((value) => value.toString(16).padStart(2, '0'))
  .join('');
let observedUrl = '';
let observedInit: RequestInit | undefined;
try {
  globalThis.fetch = async (url, init) => {
    observedUrl = String(url);
    observedInit = init;
    return new Response(content, {
      headers: { 'Content-Type': 'text/csv; charset=utf-8', ETag: `"${contentHash}"` },
    });
  };
  const output = await fetchDashboardExport(data, 'filtered_csv', 'vi', 'https://example.test');
  assert.equal(
    observedUrl,
    `https://example.test/api/dashboard/contexts/${data.context.context_id}/export.csv?locale=vi`,
  );
  assert.equal(observedInit?.redirect, 'error');
  assert.equal(observedInit?.credentials, 'same-origin');
  assert.equal(output.receipt.byte_length, content.byteLength);
  assert.equal(output.receipt.content_sha256, contentHash);
  assert.equal(output.receipt.save_confirmed, false);
  assert.equal(output.receipt.scope.record_date_filter_applied, false);
  const workbook = dashboardExportTarget(data, 'quality_workbook', 'vi', 'https://example.test');
  assert.equal(workbook.url, 'https://example.test/api/dashboard/quality-workbook');
  for (const supplied of [
    'https://evil.test/export.csv',
    '/api/sessions/private/file',
    `${data.links.export_csv}?redirect=evil`,
    `${data.links.export_csv}#fragment`,
  ]) {
    assert.throws(
      () =>
        dashboardExportTarget(
          { ...data, links: { ...data.links, export_csv: supplied } },
          'filtered_csv',
          'en',
          'https://example.test',
        ),
      /expected same-origin/,
    );
  }
  globalThis.fetch = async () => new Response('failure', { status: 503 });
  await assert.rejects(
    fetchDashboardExport(data, 'filtered_csv', 'en', 'https://example.test'),
    /503/,
  );
  globalThis.fetch = async () =>
    new Response('<html>not a file</html>', { headers: { 'Content-Type': 'text/html' } });
  await assert.rejects(
    fetchDashboardExport(data, 'filtered_csv', 'en', 'https://example.test'),
    /unexpected content type/,
  );
  globalThis.fetch = async () =>
    new Response(content, { headers: { 'Content-Type': 'text/csv', ETag: `"${'0'.repeat(64)}"` } });
  await assert.rejects(
    fetchDashboardExport(data, 'filtered_csv', 'en', 'https://example.test'),
    /checksum/,
  );
} finally {
  globalThis.fetch = originalFetch;
  for (const timer of frames.values()) clearTimeout(timer);
}
assert.equal(JSON.stringify(data), before, 'Tools never mutate the immutable source context');
console.log(
  'Dashboard WebMCP synthetic contracts passed: validation, source binding, page-only tools, pagination, committed-state waiting, races, cancellation and export response integrity.',
);
