// Browser verifier: import this module from the Vite origin, then call
// runDashboardRequestVerification(). Fetch is replaced only for this isolated run.
import { act, useLayoutEffect } from 'react';
import { createRoot } from 'react-dom/client';
import Dashboard from '../src/Dashboard';
import { ProductRanking } from '../src/DashboardCharts';
import { Records } from '../src/DashboardRecords';
import { I18nScope, createI18n } from '../src/i18n';
import { initialFilters } from '../src/dashboard-types';
import type { DashboardData, Filters, RecordsData } from '../src/dashboard-types';
import type { Locale } from '../src/i18n';

type PendingRequest = {
  url: URL;
  signal?: AbortSignal | null;
  resolve: (response: Response) => void;
};

function check(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

// Only the Records fields are needed; these are synthetic UI states, not source evidence.
function fixture(context: string): DashboardData {
  return {
    context: { context_id: context },
    links: { records: `/api/dashboard/contexts/${context}/records` },
  } as DashboardData;
}

function records(id: number, page = 1): RecordsData {
  return {
    page,
    page_count: 3,
    page_size: 20,
    total: 60,
    rows: [
      {
        defect_id: id,
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
      },
    ],
  } as RecordsData;
}

export async function runDashboardRequestVerification() {
  const host = document.createElement('div');
  document.body.append(host);
  const root = createRoot(host);
  const originalFetch = window.fetch;
  const environment = globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean };
  const previousActEnvironment = environment.IS_REACT_ACT_ENVIRONMENT;
  environment.IS_REACT_ACT_ENVIRONMENT = true;
  const requests: PendingRequest[] = [];
  const commits: string[] = [];
  const en = createI18n('en');
  const stableNoop = () => {};
  window.fetch = (input, init) =>
    new Promise<Response>((resolve) => {
      requests.push({ url: new URL(String(input), location.href), signal: init?.signal, resolve });
    });

  function CommitProbe() {
    useLayoutEffect(() => {
      commits.push(host.textContent ?? '');
    });
    return null;
  }
  const renderRecords = async (data: DashboardData, dateFilter?: string) => {
    await act(async () => {
      root.render(
        <I18nScope locale="en">
          <Records data={data} dateFilter={dateFilter} />
          <CommitProbe />
        </I18nScope>,
      );
    });
  };
  const settle = async (request: PendingRequest, value: unknown, status = 200) => {
    await act(async () => {
      request.resolve(new Response(JSON.stringify(value), { status }));
    });
  };
  const click = async (label: string) => {
    const button = [...host.querySelectorAll('button')].find(
      (item) => item.getAttribute('aria-label') === label || item.textContent === label,
    );
    check(button && !button.disabled, `An enabled button exists: ${label}`);
    await act(async () => {
      button.focus();
      button.click();
    });
  };
  try {
    const data = fixture('scope-a');
    const before = JSON.stringify(data);
    await renderRecords(data);
    check(Number(requests.length) === 1, 'The initial scope requests exactly one page');
    await settle(requests[0], records(101));
    await click(en.t('ui.next_records_page'));
    check(Number(requests.length) === 2, 'Moving to page two requests exactly one page');
    check(requests[1].url.searchParams.get('page') === '2', 'The second request is page two');
    await settle(requests[1], records(102, 2));

    const beforeDate = requests.length;
    await renderRecords(data, '2024-01-03');
    check(
      requests.length === beforeDate + 1,
      `Changing the exact-date scope requests page one once; observed ${requests.length - beforeDate}`,
    );
    const dated = requests.at(-1)!;
    check(dated.url.searchParams.get('page') === '1', 'A different date starts on page one');
    check(dated.url.searchParams.get('defect_date') === '2024-01-03', 'The date reaches the API');
    check(!host.textContent?.includes('#102'), 'The old page is hidden while the new date loads');
    await settle(dated, records(103));
    await click(en.t('ui.next_records_page'));
    await settle(requests.at(-1)!, records(104, 2));

    const beforeClear = requests.length;
    await renderRecords(data);
    check(requests.length === beforeClear + 1, 'Clearing the date requests page one once');
    await settle(requests.at(-1)!, records(105));
    const beforeSort = requests.length;
    const sort = host.querySelector('select');
    check(sort, 'Records provides its sorting control');
    await act(async () => {
      sort.value = 'repair_cost';
      sort.dispatchEvent(new Event('change', { bubbles: true }));
    });
    check(requests.length === beforeSort + 1, 'Changing sorting requests only the selected sort');
    check(
      !host.textContent?.includes('#105'),
      'The previous sort is hidden until replacement arrives',
    );
    const obsoleteSort = requests.at(-1)!;
    await renderRecords(fixture('scope-b'));
    check(obsoleteSort.signal?.aborted, 'Switching scopes cancels the obsolete sort request');
    const currentScope = requests.at(-1)!;
    await settle(currentScope, records(201));
    await settle(obsoleteSort, records(999));
    check(host.textContent?.includes('#201'), 'The current scope remains visible');
    check(!host.textContent?.includes('#999'), 'An aborted late response cannot replace the scope');

    await click(en.t('ui.next_records_page'));
    await settle(requests.at(-1)!, { detail: 'Synthetic records failure' }, 503);
    check(
      host.textContent?.includes('Synthetic records failure'),
      'A failed page provides its error',
    );
    await click(en.t('ui.retry_records'));
    check(!host.textContent?.includes('Synthetic records failure'), 'Retry clears the old failure');
    await settle(requests.at(-1)!, records(202, 2));
    check(host.textContent?.includes('#202'), 'Retry displays the requested page');

    const beforeErrorScope = requests.length;
    await renderRecords(fixture('scope-c'));
    await settle(requests.at(-1)!, { detail: 'Old scope failure' }, 503);
    const commitIndex = commits.length;
    await renderRecords(fixture('scope-d'));
    check(
      commits.slice(commitIndex).every((text) => !text.includes('Old scope failure')),
      'The new scope never commits an error belonging to the previous scope',
    );
    check(requests.length === beforeErrorScope + 2, 'Each replacement scope issues one request');
    check(JSON.stringify(data) === before, 'Browsing records does not mutate the input context');

    await settle(requests.at(-1)!, records(301));
    const region = host.querySelector<HTMLElement>('.table-scroll');
    check(region?.tabIndex === 0, 'The records region remains keyboard focusable');
    region.focus();
    check(document.activeElement === region, 'Keyboard focus can enter the records region');
    check(!host.querySelector('.records-table button'), 'Record rows contain data without actions');
    const focusedSort = host.querySelector('select');
    check(focusedSort, 'The sort control remains present after scope changes');
    const beforeFocusedSort = requests.length;
    await act(async () => {
      focusedSort.focus();
      focusedSort.value = 'product_name';
      focusedSort.dispatchEvent(new Event('change', { bubbles: true }));
    });
    check(requests.length === beforeFocusedSort + 1, 'The focused sort issues one records request');
    await settle(requests.at(-1)!, records(302));
    check(
      document.activeElement === focusedSort,
      'Sorting preserves keyboard focus on its control',
    );
    await click(en.t('ui.next_records_page'));
    await settle(requests.at(-1)!, records(303, 2));
    const nextPage = [...host.querySelectorAll('button')].find(
      (button) => button.getAttribute('aria-label') === en.t('ui.next_records_page'),
    );
    check(nextPage && !nextPage.disabled, 'The next-page control is ready after paging');
    nextPage.focus();
    check(
      document.activeElement === nextPage,
      'Keyboard focus can return to the next-page control after paging',
    );
    check(
      requests.every((request) => request.url.pathname.endsWith('/records')),
      'Browsing, sorting and retrying only request records pages',
    );

    const rankingData = {
      analysis: {
        kpis: { cohort_product_count: 1 },
        products: { top: [], all: [], pareto_product_count_80pct: 1 },
      },
    } as unknown as DashboardData;
    const renderRanking = async (locale: Locale, data = rankingData) => {
      await act(async () => {
        root.render(
          <I18nScope locale={locale}>
            <ProductRanking data={data} />
          </I18nScope>,
        );
      });
    };
    await renderRanking('en');
    await click(en.t('ui.event_count'));
    check(
      host.textContent?.includes('1 of 1 products in this scope account for at least 80%'),
      'Event Pareto uses the selected backend cohort count',
    );
    await renderRanking('vi');
    check(
      host.textContent?.includes('1 trên 1 mã sản phẩm trong phạm vi này chiếm ít nhất 80%'),
      'Event Pareto preserves the cohort and selected ranking across locale changes',
    );
    await renderRanking('vi', {
      ...rankingData,
      analysis: {
        ...rankingData.analysis,
        kpis: { ...rankingData.analysis.kpis, cohort_product_count: 7 },
        products: { ...rankingData.analysis.products, pareto_product_count_80pct: 2 },
      },
    });
    check(
      host.textContent?.includes('2 trên 7 mã sản phẩm trong phạm vi này chiếm ít nhất 80%'),
      'Event Pareto updates when the backend cohort changes',
    );

    let filterReads = 0;
    const filters = new Proxy(initialFilters, {
      get(target, key: keyof typeof initialFilters) {
        filterReads++;
        return target[key];
      },
    });
    const renderDashboard = async (locale: Locale = 'en', appliedFilters: Filters = filters) => {
      await act(async () => {
        root.render(
          <I18nScope locale={locale}>
            <Dashboard
              loading={false}
              refreshing={false}
              error=""
              filters={appliedFilters}
              onApply={stableNoop}
              onRefresh={stableNoop}
              onAnalyze={stableNoop}
            />
          </I18nScope>,
        );
      });
    };
    await renderDashboard();
    filterReads = 0;
    for (let index = 0; index < 5; index++) await renderDashboard();
    check(
      filterReads === 0,
      `Unchanged parent updates should skip Dashboard; observed ${filterReads} filter reads`,
    );
    const unchangedDashboardFilterReads = filterReads;
    await renderDashboard('vi');
    check(
      host.querySelector('h1')?.textContent === createI18n('vi').t('ui.dashboard_title'),
      'A locale change still updates the memoized Dashboard',
    );
    await renderDashboard('vi', { ...initialFilters, start_month: '2024-02' });
    check(
      host.querySelector('.scope-summary strong')?.textContent?.includes('Tháng 2/2024'),
      'A new filter scope still updates the memoized Dashboard',
    );
    return { requests: requests.length, unchangedDashboardFilterReads, passed: true };
  } finally {
    await act(async () => root.unmount());
    host.remove();
    window.fetch = originalFetch;
    environment.IS_REACT_ACT_ENVIRONMENT = previousActEnvironment;
  }
}
