// Bundle this browser fixture with the installed esbuild into a scratch directory.
// It measures real React rendering against synthetic deferred HTTP responses.
import { act, Profiler } from 'react';
import type { ProfilerOnRenderCallback } from 'react';
import { createRoot } from 'react-dom/client';
import { Records } from '../src/DashboardRecords';
import type { DashboardData, RecordsData } from '../src/dashboard-types';
import { I18nScope, createI18n } from '../src/i18n';
import type { Locale } from '../src/i18n';
export { runDashboardRequestVerification } from './verify-dashboard-requests';

type PendingRequest = {
  url: URL;
  signal?: AbortSignal | null;
  resolve: (response: Response) => void;
};

type TraceFixture = {
  raw_records: { source_row: number; raw: Record<string, unknown> }[];
  canonical_records: Record<string, unknown>[];
  processed_records: Record<string, unknown>[];
  normalization_audit: Record<string, unknown>[];
  scenario_audit: Record<string, unknown>[];
  truncated: boolean;
};

function check(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

async function sha256(text: string) {
  const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(hash)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

function records(page: number): RecordsData {
  return {
    page,
    page_count: 3,
    page_size: 20,
    total: 60,
    rows: Array.from({ length: 20 }, (_, index) => ({
      defect_id: (page - 1) * 20 + index + 1,
      defect_date: '2024-01-03',
      product_id: 1,
      product_name: 'Product A',
      production_line: 'Line-1',
      severity: 'Minor',
      defect_type: 'Surface',
      defect_location: 'Component',
      inspection_method: 'Manual',
      repair_cost: 1,
      cost_status: 'known',
    })),
  } as RecordsData;
}

function traceFixture(rawCount: number, auditCount: number): TraceFixture {
  return {
    raw_records: Array.from({ length: rawCount }, (_, index) => ({
      source_row: index + 1,
      raw: {
        __dashboard_profile_fixture: true,
        defect_id: 1,
        product_id: 1,
        defect_date: '2024-01-03',
        defect_type: ' Surface ',
        severity: 'Minor',
        repair_cost: '1.00',
      },
    })),
    canonical_records: [{ __dashboard_profile_fixture: true, ...records(1).rows[0] }],
    processed_records: [],
    normalization_audit: Array.from({ length: auditCount }, (_, index) => ({
      field: 'defect_type',
      raw_value: ` Surface ${index} `,
      processed_value: `Surface ${index}`,
      code: 'normalize_whitespace',
      detail: 'Synthetic field normalization for the browser performance fixture.',
      policy_version: 'fixture-policy',
    })),
    scenario_audit: [],
    truncated: auditCount > rawCount * 5,
  };
}

export async function profileDashboardTrace({
  rawCount,
  auditCount,
  pagingActions = 8,
}: {
  rawCount: number;
  auditCount: number;
  pagingActions?: number;
}) {
  const host = document.createElement('div');
  document.body.append(host);
  const root = createRoot(host);
  const originalFetch = window.fetch;
  const originalStringify = JSON.stringify;
  const environment = globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean };
  const previousActEnvironment = environment.IS_REACT_ACT_ENVIRONMENT;
  environment.IS_REACT_ACT_ENVIRONMENT = true;
  const en = createI18n('en');
  const requests: PendingRequest[] = [];
  const commits: { phase: string; actualDuration: number; baseDuration: number }[] = [];
  let traceSerializationCalls = 0;
  let auditSerializationCalls = 0;
  let track = false;
  const data = {
    context: { context_id: 'profile-context' },
    links: {
      records: '/api/dashboard/contexts/profile-context/records',
      trace: '/api/dashboard/contexts/profile-context/trace',
    },
  } as DashboardData;
  const trace = traceFixture(rawCount, auditCount);
  const beforeFixture = originalStringify(trace);
  window.fetch = (input, init) =>
    new Promise<Response>((resolve) => {
      requests.push({ url: new URL(String(input), location.href), signal: init?.signal, resolve });
    });
  JSON.stringify = ((value: unknown, ...args: unknown[]) => {
    const record = (Array.isArray(value) ? value[0] : value) as Record<string, unknown> | undefined;
    if (track && record?.__dashboard_profile_fixture === true) traceSerializationCalls++;
    if (track && typeof value === 'string' && /^ ?Surface \d+ ?$/.test(value))
      auditSerializationCalls++;
    return Reflect.apply(originalStringify, JSON, [value, ...args]);
  }) as typeof JSON.stringify;
  const onRender: ProfilerOnRenderCallback = (_id, phase, actualDuration, baseDuration) => {
    if (track) commits.push({ phase, actualDuration, baseDuration });
  };
  const render = async (locale: Locale = 'en') => {
    await act(async () => {
      root.render(
        <I18nScope locale={locale}>
          <Profiler id="records-with-open-trace" onRender={onRender}>
            <Records data={data} />
          </Profiler>
        </I18nScope>,
      );
    });
  };
  const settle = async (request: PendingRequest, payload: unknown) => {
    await act(async () => {
      request.resolve(new Response(originalStringify(payload), { status: 200 }));
    });
  };
  const click = async (label: string) => {
    const button = [...host.querySelectorAll('button')].find(
      (item) => item.getAttribute('aria-label') === label || item.textContent === label,
    );
    check(button && !button.disabled, `An enabled control exists: ${label}`);
    await act(async () => button.click());
  };
  try {
    await render();
    await settle(requests.at(-1)!, records(1));
    await click(en.t('ui.trace_record'));
    check(requests.at(-1)!.url.pathname.endsWith('/trace'), 'Opening a trace requests its data');
    await settle(requests.at(-1)!, trace);
    const tracePanel = host.querySelector('.record-trace');
    check(tracePanel, 'The selected trace is visible');
    const initialTraceHtml = tracePanel.innerHTML;
    check(
      tracePanel.querySelectorAll('tbody tr').length === auditCount,
      'Every synthetic audit row is rendered without truncating the display',
    );
    const firstRequestCount = requests.length;
    track = true;
    const started = performance.now();
    for (let index = 0; index < pagingActions; index++) {
      const page = index % 2 === 0 ? 2 : 1;
      const count = requests.length;
      await click(en.t(page === 2 ? 'ui.next_records_page' : 'ui.previous_records_page'));
      check(requests.length === count + 1, 'Each page change still performs its fresh API read');
      const request = requests.at(-1)!;
      check(request.url.searchParams.get('page') === String(page), 'The correct page is requested');
      await settle(request, records(page));
      check(tracePanel.innerHTML === initialTraceHtml, 'Paging preserves the open trace exactly');
    }
    const pagingElapsedMs = performance.now() - started;
    track = false;
    const measuredSerializations = traceSerializationCalls;
    const pagingCommits = commits.slice();
    const finalHtmlHash = await sha256(host.innerHTML);
    const auditHtml = host.querySelector('.record-trace tbody')?.innerHTML;
    const serializationStart = traceSerializationCalls;
    const auditSerializationStart = auditSerializationCalls;
    track = true;
    await render('vi');
    track = false;
    const localeTraceSerializations = traceSerializationCalls - serializationStart;
    const localeAuditSerializations = auditSerializationCalls - auditSerializationStart;
    const localeCommits = commits.slice(pagingCommits.length);
    check(
      localeTraceSerializations === 0 && localeAuditSerializations === 0,
      'A locale change reuses immutable source JSON and field-decision rows',
    );
    check(
      host.querySelector('#record-trace-title')?.textContent ===
        createI18n('vi').t('ui.record_trace_title', { id: 1 }),
      'The open trace responds to a locale change',
    );
    check(
      host.querySelector('.record-trace tbody')?.textContent?.includes('Surface 0'),
      'A locale change preserves canonical audit values',
    );
    check(
      host.querySelector('.record-trace tbody')?.innerHTML === auditHtml,
      'Changing language preserves the complete audit table',
    );
    check(originalStringify(trace) === beforeFixture, 'Rendering never mutates the trace fixture');
    await click(createI18n('vi').t('ui.close_record_trace'));
    check(!host.querySelector('.record-trace'), 'The trace can still close after paging');
    return {
      rawCount,
      auditCount,
      pagingActions,
      pagingRequestCount: requests.length - firstRequestCount,
      pagingCommitCount: pagingCommits.length,
      traceSerializationCalls: measuredSerializations,
      pagingReactActualDurationMs: pagingCommits.reduce(
        (total, item) => total + item.actualDuration,
        0,
      ),
      pagingReactMaxCommitMs: Math.max(0, ...pagingCommits.map((item) => item.actualDuration)),
      pagingElapsedMs,
      traceHtmlSha256: await sha256(initialTraceHtml),
      finalHtmlSha256: finalHtmlHash,
      commits: pagingCommits,
      localeChange: {
        traceSerializationCalls: localeTraceSerializations,
        auditSerializationCalls: localeAuditSerializations,
        reactActualDurationMs: localeCommits.reduce(
          (total, item) => total + item.actualDuration,
          0,
        ),
      },
      passed: true,
    };
  } finally {
    track = false;
    await act(async () => root.unmount());
    host.remove();
    window.fetch = originalFetch;
    JSON.stringify = originalStringify;
    environment.IS_REACT_ACT_ENVIRONMENT = previousActEnvironment;
  }
}
