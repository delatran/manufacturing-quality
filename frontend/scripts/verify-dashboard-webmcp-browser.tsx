import { createRoot } from 'react-dom/client';
import { flushSync } from 'react-dom';
import Dashboard from '../src/Dashboard';
import { I18nScope, createI18n } from '../src/i18n';
import type { Locale } from '../src/i18n';
import type { DashboardData } from '../src/dashboard-types';
import type { WebMcpContext, WebMcpTool } from '../src/webmcp';
import { dashboardFixture, recordsFixture } from './dashboard-webmcp-fixture';

type PendingRequest = { url: URL; init?: RequestInit; resolve: (response: Response) => void };
function check(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}
const frame = () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
async function until(predicate: () => boolean, message: string) {
  for (let i = 0; i < 180; i++) {
    if (predicate()) return;
    await frame();
  }
  throw new Error(`Fixture did not settle: ${message}`);
}
async function rejects(promise: Promise<unknown>, pattern: RegExp) {
  let error: unknown;
  try {
    await promise;
  } catch (failure) {
    error = failure;
  }
  check(
    error instanceof Error && pattern.test(error.message),
    `Expected rejection ${pattern}; received ${String(error)}`,
  );
}

/** Synthetic browser integration. The registry, HTTP responses and download click are mocked. */
export async function runDashboardWebMcpVerification() {
  const host = document.createElement('div');
  host.id = 'dashboard-webmcp-fixture';
  document.body.append(host);
  let root = createRoot(host);
  const originalFetch = window.fetch;
  const originalClick = HTMLAnchorElement.prototype.click;
  const originalContext = Object.getOwnPropertyDescriptor(document, 'modelContext');
  const tools = new Map<string, WebMcpTool>();
  const requests: PendingRequest[] = [];
  const downloads: { filename: string; url: string }[] = [];
  const checks: string[] = [];
  const registry: WebMcpContext = {
    registerTool(tool, options) {
      if (tools.has(tool.name)) throw new Error(`Duplicate synthetic registration: ${tool.name}`);
      tools.set(tool.name, tool);
      options?.signal?.addEventListener(
        'abort',
        () => {
          if (tools.get(tool.name) === tool) tools.delete(tool.name);
        },
        { once: true },
      );
      return Promise.resolve();
    },
  };
  Object.defineProperty(document, 'modelContext', { configurable: true, value: registry });
  window.fetch = (input, init) =>
    new Promise<Response>((resolve) => {
      requests.push({ url: new URL(String(input), location.href), init, resolve });
    });
  HTMLAnchorElement.prototype.click = function () {
    if (this.href.startsWith('blob:')) downloads.push({ filename: this.download, url: this.href });
    else originalClick.call(this);
  };
  const invoke = (name: string, input: unknown, signal?: AbortSignal) =>
    Promise.resolve().then(() => {
      const tool = tools.get(name);
      check(tool, `Registered tool exists: ${name}`);
      return tool.execute(input, { signal });
    });
  let visible = true;
  let locale: Locale = 'en';
  let data = dashboardFixture();
  const originalData = JSON.stringify(data);
  const noop = () => {};
  const render = async () => {
    flushSync(() =>
      root.render(
        <I18nScope locale={locale}>
          <Dashboard
            data={data}
            visible={visible}
            loading={false}
            refreshing={false}
            error=""
            filters={data.context.filters}
            onApply={noop}
            onRefresh={noop}
            onAnalyze={noop}
          />
        </I18nScope>,
      ),
    );
    await frame();
  };
  const reply = async (request: PendingRequest, payload: unknown, status = 200) => {
    request.resolve(
      new Response(JSON.stringify(payload), {
        status,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    await frame();
  };
  const bound = () => ({ context_id: data.context.context_id });
  const recordReply = (scope: DashboardData, request: PendingRequest) => ({
    ...recordsFixture(
      scope,
      Number(request.url.searchParams.get('page') || 1),
      request.url.searchParams.get('sort_by') || 'defect_date',
      request.url.searchParams.get('sort_order') || 'desc',
    ),
    record_filters: { defect_date: request.url.searchParams.get('defect_date') },
  });
  try {
    await render();
    await until(() => tools.size === 4, 'initial Dashboard tool registration');
    const summary = (await invoke('read_dashboard_summary', {})) as {
      context: { context_id: string };
      kpis: unknown;
    };
    check(
      summary.context.context_id === data.context.context_id,
      'Summary reads the mounted context',
    );
    check(
      JSON.stringify(summary.kpis) === JSON.stringify(data.analysis.kpis),
      'Summary retains backend KPI values',
    );
    await rejects(
      invoke('open_dashboard_panel', { ...bound(), panel: 'evidence', priority: 100 }),
      /not in the current/,
    );
    check(!host.querySelector('#scope-evidence'), 'Rejected priority does not open evidence');
    await invoke('open_dashboard_panel', { ...bound(), panel: 'evidence', priority: 1 });
    check(
      host.querySelector('#scope-evidence') === document.activeElement,
      'Evidence is committed and focused before completion',
    );
    checks.push('current summary, priority membership, visible evidence focus');

    const opened = (await invoke('open_dashboard_panel', {
      ...bound(),
      panel: 'records',
      defect_date: '2024-01-03',
    })) as { status: string; navigation_only: boolean };
    check(
      opened.status === 'opened' && opened.navigation_only,
      'Opening records claims navigation only',
    );
    await until(
      () => tools.size === 8 && requests.length === 1,
      'records registration and initial request',
    );
    check(
      requests[0].url.searchParams.get('defect_date') === '2024-01-03',
      'The existing backend receives the exact-date filter',
    );
    check(requests[0].url.searchParams.get('page') === '1', 'Date navigation starts at page one');
    check(
      ((await invoke('read_dashboard_records', bound())) as { status: string }).status ===
        'loading',
      'Readback reports loading before a response commits',
    );
    await reply(requests[0], recordReply(data, requests[0]));
    await until(
      () => host.querySelector('.records-table')?.textContent?.includes('#11') === true,
      'first records page',
    );
    let pageResolved = false;
    const pagePromise = invoke('set_dashboard_records_view', {
      ...bound(),
      page: 2,
      sort_by: 'repair_cost',
      sort_order: 'asc',
    }).then((value) => {
      pageResolved = true;
      return value;
    });
    await until(() => requests.length === 2, 'replacement page request');
    check(!pageResolved, 'Page tool stays pending while HTTP is unresolved');
    check(
      !host.querySelector('.records-table')?.textContent?.includes('#11'),
      'The old page is not shown under the new query',
    );
    await reply(requests[1], recordReply(data, requests[1]));
    const pageResult = (await pagePromise) as {
      records: { page: number; rows: { defect_id: number }[] };
    };
    check(
      pageResult.records.page === 2 && pageResult.records.rows[0].defect_id === 21,
      'Tool readback matches the committed replacement page',
    );
    check(
      host.querySelector('.records-table')?.textContent?.includes('#21'),
      'The returned record is visible',
    );
    check(
      (host.querySelector('.records-controls select') as HTMLSelectElement).value === 'repair_cost',
      'Sort select shares the tool action',
    );
    check(
      (host.querySelector('#record-order') as HTMLSelectElement).value === 'asc',
      'Direction select shares the tool action',
    );
    await rejects(
      invoke('open_dashboard_record_trace', { ...bound(), defect_id: 11 }),
      /current visible records page/,
    );
    checks.push(
      'exact-date navigation, backend pagination, committed sort/page, current-page trace membership',
    );

    let traceResolved = false;
    const tracePromise = invoke('open_dashboard_record_trace', { ...bound(), defect_id: 21 }).then(
      (value) => {
        traceResolved = true;
        return value;
      },
    );
    await until(() => requests.length === 3, 'trace request');
    check(!traceResolved, 'Trace tool stays pending until its panel data commits');
    const trace = {
      context: data.context,
      raw_records: [{ source_row: 1, raw: { defect_id: 21, note: 'Untrusted fixture source.' } }],
      raw_match_count: 1,
      canonical_records: [{ defect_id: 21 }],
      processed_records: [{ defect_id: 21 }],
      normalization_audit: [
        { field: 'defect_id', raw_value: '21', processed_value: 21, code: 'synthetic' },
      ],
      scenario_audit: [],
      truncated: false,
    };
    await reply(requests[2], trace);
    const traceResult = (await tracePromise) as {
      status: string;
      defect_id: number;
      trace: { raw_records: unknown[] };
    };
    check(
      traceResult.status === 'ready' && traceResult.defect_id === 21,
      'Trace tool returns the selected visible lineage',
    );
    check(
      traceResult.trace.raw_records.length === 1 &&
        host.querySelector('.record-trace')?.textContent?.includes('Untrusted fixture source.'),
      'Returned raw rows are present in the trace panel',
    );
    check(
      host.querySelector('.record-trace') === document.activeElement,
      'Trace panel receives focus',
    );
    const rawBefore = [...host.querySelectorAll('.record-trace pre')]
      .map((node) => node.textContent)
      .join('\n');
    const requestCount = requests.length;
    locale = 'vi';
    await render();
    check(requests.length === requestCount, 'Changing locale never refetches source lineage');
    check(
      [...host.querySelectorAll('.record-trace pre')].map((node) => node.textContent).join('\n') ===
        rawBefore,
      'Source JSON is unchanged after translation',
    );
    check(
      host.querySelector('#record-trace-title')?.textContent ===
        createI18n('vi').t('ui.record_trace_title', { id: 21 }),
      'Trace heading switches to Vietnamese',
    );
    checks.push('trace data and focus, EN/VI source preservation, no locale refetch');

    await invoke('open_dashboard_panel', { ...bound(), panel: 'records', defect_date: '' });
    await until(() => requests.length === 4, 'cleared date request');
    check(
      !requests[3].url.searchParams.has('defect_date'),
      'Clearing the exact date removes the backend parameter',
    );
    check(!host.querySelector('.record-trace'), 'A different record scope closes the old trace');
    await reply(requests[3], recordReply(data, requests[3]));
    await until(
      () => host.querySelector('.records-table')?.textContent?.includes('#11') === true,
      'cleared date response',
    );
    const oldContext = bound();
    const pending = invoke('set_dashboard_records_view', { ...oldContext, page: 2 });
    const staleCheck = rejects(pending, /no longer mounted|context changed/);
    await until(() => requests.length === 5, 'stale page request');
    const oldRequest = requests[4];
    const oldData = data;
    data = dashboardFixture('replacement-fixture');
    await render();
    await staleCheck;
    check(oldRequest.init?.signal?.aborted, 'Replacing the context cancels its records fetch');
    await until(() => requests.length === 6, 'replacement context request');
    await reply(requests[5], recordReply(data, requests[5]));
    await reply(oldRequest, {
      ...recordReply(oldData, oldRequest),
      rows: [{ ...recordsFixture(oldData).rows[0], defect_id: 999 }],
    });
    check(
      !host.querySelector('.records-table')?.textContent?.includes('#999'),
      'Late stale rows never replace the current context',
    );
    await rejects(invoke('read_dashboard_summary', oldContext), /context changed/);
    const captured = tools.get('read_dashboard_summary')!;
    visible = false;
    await render();
    await until(() => tools.size === 0, 'hidden workspace cleanup');
    await rejects(
      Promise.resolve().then(() => captured.execute({}, {})),
      /no longer available/,
    );
    visible = true;
    await render();
    await until(() => tools.size === 8, 'visible workspace registration');
    checks.push(
      'date clearing, context race rejection, fetch cancellation, late response exclusion, hidden lifecycle cleanup',
    );

    const csv = new TextEncoder().encode('defect_id,repair_cost\n11,10\n');
    const beforeExport = requests.length;
    const csvAnchor = [...host.querySelectorAll<HTMLAnchorElement>('#section-share a')].find(
      (anchor) => new URL(anchor.href).pathname.endsWith('/export.csv'),
    );
    check(csvAnchor, 'The ordinary CSV export control is present');
    csvAnchor.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    await until(() => requests.length === beforeExport + 1, 'ordinary UI export fetch');
    check(
      requests.at(-1)!.url.searchParams.get('locale') === 'vi',
      'Ordinary export uses the current locale',
    );
    requests
      .at(-1)!
      .resolve(new Response(csv, { headers: { 'Content-Type': 'text/csv; charset=utf-8' } }));
    await until(() => downloads.length === 1, 'ordinary export download request');
    await until(
      () =>
        host.textContent?.includes(
          createI18n('vi').t('ui.dashboard_export_requested', {
            filename: downloads[0].filename,
            bytes: createI18n('vi').number(csv.byteLength),
          }),
        ) === true,
      'visible ordinary export receipt',
    );
    check(
      (host.querySelector('#section-share') as HTMLDetailsElement).open,
      'The exports section opens with its readback',
    );
    const exportReadback = (await invoke('read_dashboard_summary', {})) as {
      export: { receipt: { byte_length: number; save_confirmed: boolean; content_sha256: string } };
    };
    check(
      exportReadback.export.receipt.byte_length === csv.byteLength,
      'Export receipt uses actual bytes',
    );
    check(
      exportReadback.export.receipt.save_confirmed === false &&
        exportReadback.export.receipt.content_sha256.length === 64,
      'Export readback gives checksum without claiming a saved file',
    );
    const workbookPromise = invoke('request_dashboard_export', {
      ...bound(),
      kind: 'quality_workbook',
    });
    await until(() => requests.length === beforeExport + 2, 'workbook fetch');
    check(
      requests.at(-1)!.url.pathname.endsWith('/quality-workbook') && !requests.at(-1)!.url.search,
      'Workbook reuses the unfiltered existing route',
    );
    requests.at(-1)!.resolve(
      new Response(new Uint8Array([80, 75, 3, 4]), {
        headers: {
          'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        },
      }),
    );
    const workbookResult = (await workbookPromise) as {
      status: string;
      scope: { dashboard_filters_applied: boolean };
    };
    check(
      workbookResult.status === 'download_requested' &&
        !workbookResult.scope.dashboard_filters_applied,
      'Workbook receipt states its full-case scope',
    );
    check(downloads.length === 2, 'Workbook download request reached the browser click boundary');
    const failedExport = invoke('request_dashboard_export', { ...bound(), kind: 'filtered_csv' });
    const failedCheck = rejects(failedExport, /unexpected content type/);
    await until(() => requests.length === beforeExport + 3, 'failed export fetch');
    requests
      .at(-1)!
      .resolve(new Response('<html>failure</html>', { headers: { 'Content-Type': 'text/html' } }));
    await failedCheck;
    check(downloads.length === 2, 'Wrong MIME never reaches the browser download action');
    check(
      host.querySelector('#section-share [role="alert"]')?.textContent ===
        createI18n('vi').t('ui.dashboard_export_failed'),
      'Export failure appears in the current UI',
    );
    checks.push(
      'ordinary UI and tool export action parity, response bytes/checksum, full-case workbook scope, MIME failure, explicit save uncertainty',
    );

    const inFlightExport = invoke('request_dashboard_export', { ...bound(), kind: 'filtered_csv' });
    const changedCheck = rejects(inFlightExport, /changed while preparing/);
    await until(() => requests.length === beforeExport + 4, 'export scope race request');
    const staleExportRequest = requests.at(-1)!;
    data = dashboardFixture('export-replacement');
    await render();
    staleExportRequest.resolve(new Response(csv, { headers: { 'Content-Type': 'text/csv' } }));
    await changedCheck;
    check(
      downloads.length === 2,
      'Scope change blocks a late download before its browser side effect',
    );
    check(
      !host.querySelector('#section-share [role="alert"]'),
      'A late export error is not written into a replacement context',
    );
    checks.push('export context race prevents stale side effects');

    flushSync(() => root.unmount());
    Object.defineProperty(document, 'modelContext', { configurable: true, value: undefined });
    root = createRoot(host);
    data = dashboardFixture();
    await render();
    check(tools.size === 0, 'Unsupported browser does not register a replacement or polyfill');
    const nativeCsv = [...host.querySelectorAll<HTMLAnchorElement>('#section-share a')].find(
      (anchor) => new URL(anchor.href).pathname.endsWith('/export.csv'),
    );
    check(nativeCsv, 'The unsupported-browser CSV link remains present');
    const subtleDescriptor = Object.getOwnPropertyDescriptor(crypto, 'subtle');
    let nativeLinkPreserved = false;
    const observeNativeLink = (event: MouseEvent) => {
      if (event.target !== nativeCsv) return;
      nativeLinkPreserved = !event.defaultPrevented;
      event.preventDefault(); // The fixture observes native fallback without leaving its page.
    };
    try {
      Object.defineProperty(crypto, 'subtle', { configurable: true, value: undefined });
      document.addEventListener('click', observeNativeLink);
      nativeCsv.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
      check(
        nativeLinkPreserved,
        'Without Web Crypto the ordinary export remains a native browser link',
      );
    } finally {
      document.removeEventListener('click', observeNativeLink);
      if (subtleDescriptor) Object.defineProperty(crypto, 'subtle', subtleDescriptor);
      else Reflect.deleteProperty(crypto, 'subtle');
    }
    const ordinary = host.querySelector<HTMLButtonElement>('.records-disclosure-toggle');
    check(ordinary, 'Ordinary records control remains available without WebMCP');
    const lastCount = requests.length;
    ordinary.click();
    await until(
      () => requests.length === lastCount + 1,
      'unsupported-browser ordinary records request',
    );
    await reply(requests.at(-1)!, recordReply(data, requests.at(-1)!));
    await until(
      () => host.querySelector('.records-table')?.textContent?.includes('#11') === true,
      'unsupported-browser ordinary table',
    );
    check(JSON.stringify(data) === originalData, 'The test did not mutate the source context');
    checks.push(
      'unsupported-browser ordinary UI, native export fallback without Web Crypto, immutable input',
    );
    return {
      passed: true,
      synthetic: true,
      native_registry_tested: false,
      real_http_tested: false,
      browser_save_confirmed: false,
      checks,
      registrations: 8,
      http_responses: requests.length,
      intercepted_download_requests: downloads.length,
      locales: ['en', 'vi'],
    };
  } finally {
    flushSync(() => root.unmount());
    window.fetch = originalFetch;
    HTMLAnchorElement.prototype.click = originalClick;
    if (originalContext) Object.defineProperty(document, 'modelContext', originalContext);
    else delete (document as Document & { modelContext?: WebMcpContext }).modelContext;
    host.remove();
  }
}
