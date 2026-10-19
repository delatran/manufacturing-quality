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

function check(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
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

export async function profileDashboardRecords({
  pagingActions = 8,
}: { pagingActions?: number } = {}) {
  const host = document.createElement('div');
  document.body.append(host);
  const root = createRoot(host);
  const originalFetch = window.fetch;
  const environment = globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean };
  const previousActEnvironment = environment.IS_REACT_ACT_ENVIRONMENT;
  environment.IS_REACT_ACT_ENVIRONMENT = true;
  const en = createI18n('en');
  const requests: PendingRequest[] = [];
  const commits: { phase: string; actualDuration: number; baseDuration: number }[] = [];
  let track = false;
  const data = {
    context: { context_id: 'profile-context' },
    links: { records: '/api/dashboard/contexts/profile-context/records' },
  } as DashboardData;
  const beforeFixture = JSON.stringify(data);
  window.fetch = (input, init) =>
    new Promise<Response>((resolve) => {
      requests.push({ url: new URL(String(input), location.href), signal: init?.signal, resolve });
    });
  const onRender: ProfilerOnRenderCallback = (_id, phase, actualDuration, baseDuration) => {
    if (track) commits.push({ phase, actualDuration, baseDuration });
  };
  const render = async (locale: Locale = 'en') => {
    await act(async () => {
      root.render(
        <I18nScope locale={locale}>
          <Profiler id="records-page" onRender={onRender}>
            <Records data={data} />
          </Profiler>
        </I18nScope>,
      );
    });
  };
  const settle = async (request: PendingRequest, payload: unknown) => {
    await act(async () => {
      request.resolve(new Response(JSON.stringify(payload), { status: 200 }));
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
    check(
      host.querySelectorAll('.records-table tbody tr').length === 20,
      'The initial page is rendered',
    );
    check(
      !host.querySelector('.records-table button'),
      'The records page contains data without row actions',
    );
    const firstRequestCount = requests.length;
    track = true;
    const started = performance.now();
    for (let index = 0; index < pagingActions; index++) {
      const page = index % 2 === 0 ? 2 : 1;
      const count = requests.length;
      await click(en.t(page === 2 ? 'ui.next_records_page' : 'ui.previous_records_page'));
      check(requests.length === count + 1, 'Each page change performs one fresh API read');
      const request = requests.at(-1)!;
      check(request.url.searchParams.get('page') === String(page), 'The correct page is requested');
      await settle(request, records(page));
      check(
        host.querySelectorAll('.records-table tbody tr').length === 20,
        'The replacement page is complete',
      );
    }
    const pagingElapsedMs = performance.now() - started;
    track = false;
    const pagingCommits = commits.slice();
    const requestCount = requests.length;
    const sourceValues = [...host.querySelectorAll('.records-table tbody tr')].map(
      (row) => row.querySelector('strong')?.textContent,
    );
    track = true;
    await render('vi');
    track = false;
    const localeCommits = commits.slice(pagingCommits.length);
    check(requests.length === requestCount, 'A locale change does not request a new records page');
    check(
      host.querySelector('.records-table caption')?.textContent ===
        createI18n('vi').t('ui.filtered_defect_records'),
      'The records table responds to a locale change',
    );
    check(
      JSON.stringify(
        [...host.querySelectorAll('.records-table tbody tr')].map(
          (row) => row.querySelector('strong')?.textContent,
        ),
      ) === JSON.stringify(sourceValues),
      'A locale change preserves the displayed source record IDs',
    );
    check(
      requests.every((request) => request.url.pathname.endsWith('/records')),
      'Every request is a records page read',
    );
    check(JSON.stringify(data) === beforeFixture, 'Rendering never mutates the fixture context');
    return {
      pagingActions,
      pagingRequestCount: requests.length - firstRequestCount,
      pagingCommitCount: pagingCommits.length,
      pagingReactActualDurationMs: pagingCommits.reduce(
        (total, item) => total + item.actualDuration,
        0,
      ),
      pagingReactMaxCommitMs: Math.max(0, ...pagingCommits.map((item) => item.actualDuration)),
      pagingElapsedMs,
      commits: pagingCommits,
      localeChange: {
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
    environment.IS_REACT_ACT_ENVIRONMENT = previousActEnvironment;
  }
}
