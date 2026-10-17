import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import App from '../src/App';
import Dashboard from '../src/Dashboard';
import { PeriodHalvesSummary } from '../src/DashboardCharts';
import { useDashboardLanguage } from '../src/dashboard-language';
import { I18nProvider } from '../src/i18n';
import type { Locale } from '../src/i18n';
import { initialFilters } from '../src/dashboard-types';
import type { DashboardData, Filters, Metrics } from '../src/dashboard-types';
import catalog from '../src/ui-messages.json';
import { applyPriorityFilters } from '../src/workspace-state';

Object.defineProperty(globalThis, 'React', { value: React });
// These are synthetic display states, never analytical expected results.
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
function render(
  data: DashboardData,
  locale: Locale = 'en',
  filters: Filters = initialFilters,
  canAnalyze = false,
) {
  return renderToStaticMarkup(
    <I18nProvider initialLocale={locale} persist={false}>
      <Dashboard
        data={data}
        canAnalyze={canAnalyze}
        filters={filters}
        loading={false}
        refreshing={false}
        error=""
        onApply={() => {}}
        onRefresh={() => {}}
        onAnalyze={() => {}}
      />
    </I18nProvider>,
  );
}
const html = render(empty);
assert.doesNotMatch(html, /class="primary-button analyze-button"/);
assert.match(render(empty, 'en', initialFilters, true), /class="primary-button analyze-button"/);
const appliedPriorityScope: Filters = {
  ...initialFilters,
  start_month: '2024-06',
  end_month: '2024-06',
  production_line: 'Line-3',
  category: 'Category 2',
  product_id: '46',
  inspection_method: 'Manual Testing',
  scenario: 'exclude_cost_10',
};
const appliedPrioritySnapshot = JSON.stringify(appliedPriorityScope);
const priorityFocus: Partial<Filters> = { severity: 'Critical', defect_location: 'Component' };
const focusedScope = applyPriorityFilters(appliedPriorityScope, priorityFocus);
assert.deepEqual(
  focusedScope,
  { ...appliedPriorityScope, ...priorityFocus },
  'A priority changes only its explicit focus dimensions',
);
assert.equal(focusedScope.start_month, '2024-06');
assert.equal(focusedScope.end_month, '2024-06');
assert.equal(focusedScope.production_line, 'Line-3');
assert.equal(focusedScope.scenario, 'exclude_cost_10');
assert.ok(
  Object.values(focusedScope).every((value) => typeof value === 'string'),
  'The applied request and filter draft must remain complete',
);
assert.equal(
  JSON.stringify(appliedPriorityScope),
  appliedPrioritySnapshot,
  'Applying focus must preserve the immutable source scope',
);
const focusedData = structuredClone(empty);
focusedData.context.filters = focusedScope;
const focusedHtml = render(focusedData, 'en', focusedScope);
assert.match(focusedHtml, /class="filter-state">Applied filters/);
assert.doesNotMatch(focusedHtml, /Unapplied changes/);
assert.match(focusedHtml, /Production line: Line-3/);
assert.match(focusedHtml, /Jun 2024/);
assert.match(focusedHtml, /Exclude cost 10 from financial analysis/);
assert.deepEqual(
  applyPriorityFilters(appliedPriorityScope, { production_line: 'Line-2', severity: '' }),
  { ...appliedPriorityScope, production_line: 'Line-2', severity: '' },
  'Explicit line overrides and cleared event filters still preserve the rest of the scope',
);
const reordered = structuredClone(empty);
reordered.context.filters = Object.fromEntries(Object.entries(initialFilters).reverse()) as Filters;
const reorderedHtml = render(reordered);
assert.match(reorderedHtml, /class="filter-state">Applied filters/);
assert.doesNotMatch(reorderedHtml, /Unapplied changes/);
const unchangedFilterForm = reorderedHtml.match(
  /<form[^>]*id="dashboard-filter-form"[\s\S]*?<\/form>/,
)?.[0];
assert.ok(unchangedFilterForm);
assert.match(unchangedFilterForm, /<button[^>]*type="submit"[^>]*disabled=""/);
assert.match(reorderedHtml, /All categories/);
assert.doesNotMatch(reorderedHtml, /All categorys/);
const changedScope = structuredClone(empty);
changedScope.context.filters.severity = 'Critical';
changedScope.context.filters.defect_location = 'Component';
changedScope.context.filters.inspection_method = 'Manual Testing';
const changedHtml = render(changedScope);
assert.match(changedHtml, /Unapplied changes/);
assert.match(changedHtml, /Severity: Critical/);
assert.match(changedHtml, /Defect location: Component/);
assert.match(changedHtml, /Inspection method: Manual Testing/);
const pendingFilterForm = changedHtml.match(
  /<form[^>]*id="dashboard-filter-form"[\s\S]*?<\/form>/,
)?.[0];
assert.ok(pendingFilterForm);
assert.match(pendingFilterForm, /<button[^>]*type="submit"/);
assert.doesNotMatch(pendingFilterForm, /<button[^>]*type="submit"[^>]*disabled=""/);
assert.match(html, /No defects in this view/);
assert.match(html, /No January baseline events/);
assert.match(html, /Unavailable/);
assert.doesNotMatch(html, /NaN|Infinity|undefined/);
assert.doesNotMatch(html, /Full source snapshot; filters do not change this audit/);
assert.match(html, /Last source check/);
assert.match(html, /Reference output repeats each product/);
assert.match(html, /actual production is unverified/);
assert.doesNotMatch(html, /Units produced|Rates use output from the product master/);
assert.match(html, /aria-controls="dashboard-filter-form"/);
assert.match(html, /id="dashboard-filter-form" hidden=""/);
assert.match(html, /href="\/export.csv\?locale=en"/);
assert.doesNotMatch(html, /href="\/export\.zip|Download quality brief|Download evidence bundle/);
assert.match(html, /href="\/quality.xlsx"/);
assert.match(html, /separate full-case file and does not follow dashboard filters/);
assert.doesNotMatch(
  html,
  /source-fixture|result-fixture|context-fixture|snapshot-fixture|View evidence|Evidence &amp; provenance/,
);
assert.match(html, /<nav class="dashboard-nav" aria-label="Dashboard sections">/);
const mainSections = ['overview', 'trend', 'location', 'hotspots', 'cost'];
const navigation = html.match(/<nav class="dashboard-nav"[^>]*>([\s\S]*?)<\/nav>/)?.[1];
assert.ok(navigation, 'The five analysis destinations have a named navigation landmark');
assert.deepEqual(
  [...navigation.matchAll(/href="#section-([^"#]+)"/g)].map((match) => match[1]),
  mainSections,
  'Utilities must not become peer analysis destinations',
);
const mobileSelector = navigation.match(/<select[^>]*>([\s\S]*?)<\/select>/)?.[1];
assert.ok(mobileSelector, 'Narrow screens have a discoverable section selector');
assert.deepEqual(
  [...mobileSelector.matchAll(/<option value="section-([^"]+)"/g)].map((match) => match[1]),
  mainSections,
  'Mobile navigation exposes the same destinations in the same order',
);
for (const section of mainSections) {
  assert.match(html, new RegExp(`<section class="dashboard-section" id="section-${section}"`));
}
for (const section of ['share']) {
  const disclosure = html.match(new RegExp(`<details[^>]*id="section-${section}"[^>]*>`))?.[0];
  assert.ok(disclosure, `${section} remains accessible as a utility disclosure`);
  assert.doesNotMatch(disclosure, /\sopen(?:=|\s|>)/, `${section} starts collapsed`);
}
assert.doesNotMatch(
  html,
  /id="section-(quality|scenarios|tools)"|Source quality &amp; assumptions|Explore alternative assumptions|Follow-up work|operations-panel/,
);
const exportMenu = html.match(/<details class="export-menu"[\s\S]*?<\/details>/)?.[0];
assert.ok(exportMenu);
assert.match(exportMenu, /href="\/quality.xlsx"/);
assert.match(exportMenu, /separate full-case file and does not follow dashboard filters/);
assert.match(html, /class="dashboard-utilities"/);
assert.match(html, /<details class="export-menu" id="section-share">/);
assert.match(html, /id="section-records"/);
const trendStart = html.indexOf('id="section-trend"');
const warningStart = html.indexOf('id="section-warning"');
assert.ok(trendStart < warningStart && warningStart < html.indexOf('id="section-location"'));
const overview = html.slice(html.indexOf('id="section-overview"'), trendStart);
const topKpis = overview.match(/<section class="kpi-grid"[^>]*>([\s\S]*?)<\/section>/)?.[1];
assert.ok(topKpis);
assert.equal([...topKpis.matchAll(/<article class="kpi-card/g)].length, 4);
assert.doesNotMatch(overview, /class="monthly-chart|class="cost-average-kpi/);
assert.match(html.slice(html.indexOf('id="section-cost"')), /class="cost-average-kpi"/);
assert.ok(
  changedHtml.indexOf('Unapplied changes') < changedHtml.indexOf('id="dashboard-filter-form"'),
  'Pending selections are visible outside the collapsed filter form',
);

assert.doesNotMatch(html, /class="kpi-delta/, 'No half-period deltas without backend halves');
assert.match(html, /<article class="kpi-card/);
assert.doesNotMatch(html, /<button[^>]*class="kpi-card/);
assert.match(html, /id="defect-records" hidden=""/);

const unknown = structuredClone(empty);
unknown.analysis.kpis = {
  ...emptyMetrics,
  event_count: 2,
  cost_unknown_count: 2,
  cost_coverage_pct: 0,
  cost_status: 'unavailable',
};
unknown.analysis.costs.by_type = [
  { ...unknown.analysis.kpis, name: '<script>fake</script>', known_cost_share_pct: null },
];
unknown.analysis.location_severity.test = {
  available: true,
  reliable: true,
  p_value: 0.00000265,
  cramers_v: 0.2,
  interpretation: 'Exploratory.',
};
const unknownHtml = render(unknown);
assert.match(unknownHtml, /cost recorded for 0\.0% of events · currency unspecified/);
assert.match(unknownHtml, /2 defects without a valid cost/);
const unknownCostKpi = [
  ...unknownHtml.matchAll(/<article class="kpi-card[^>]*>[\s\S]*?<\/article>/g),
]
  .map((match) => match[0])
  .find((card) => card.includes('Recorded repair cost'));
assert.ok(unknownCostKpi);
assert.match(unknownCostKpi, /<strong class="kpi-value">Unavailable<\/strong>/);
assert.match(unknownHtml, /&lt;script&gt;fake&lt;\/script&gt;<\/span><strong>Unavailable/);
assert.match(unknownHtml, /p-value: &lt;0\.0001/);
assert.doesNotMatch(unknownHtml, /<script>|NaN|Infinity|p-value: 0\.0000/);
const beforeVietnamese = JSON.stringify(unknown);
const vietnameseHtml = render(unknown, 'vi');
for (const [direction, signal, translated] of [
  ['increased', 'adverse', 'tiêu cực'],
  ['decreased', 'favourable', 'tích cực'],
] as const) {
  const trend = structuredClone(unknown);
  const movement = direction === 'increased' ? 'rising' : 'falling';
  const [first, last] = direction === 'increased' ? [147, 252] : [252, 147];
  trend.analysis.trend.interpretation = `Logged events ${direction} from ${first} in Jan 2024 to ${last} in Jun 2024. This is ${signal === 'adverse' ? 'an adverse' : 'a favourable'} signal: the logged defect workload is ${movement}. Confirm actual output and inspection coverage before treating it as a defect-rate change.`;
  const localized = render(trend, 'vi');
  assert.match(localized, new RegExp(`Đây là tín hiệu ${translated}`));
  assert.doesNotMatch(localized, /This is an? |logged defect workload/);
}
assert.match(vietnameseHtml, /<h1>Lỗi sản xuất<\/h1>/);
assert.match(vietnameseHtml, /Sự kiện lỗi nghiêm trọng/);
assert.match(vietnameseHtml, /Sự kiện \/ 1\.000 đơn vị tham chiếu/);
assert.match(vietnameseHtml, /có chi phí ở 0,0% sự kiện · chưa rõ loại tiền/);

assert.match(vietnameseHtml, /2 lỗi chưa có chi phí hợp lệ/);
assert.match(vietnameseHtml, /Chưa có dữ liệu/);
assert.match(vietnameseHtml, /Tất cả nhóm/);
assert.match(vietnameseHtml, /href="\/export.csv\?locale=vi"/);
assert.doesNotMatch(vietnameseHtml, /source-fixture|result-fixture/);
assert.equal(
  JSON.stringify(unknown),
  beforeVietnamese,
  'Localization must not mutate canonical data',
);

const ranked = structuredClone(empty);
const product = {
  ...emptyMetrics,
  category: 'Category 1',
  production_line: 'Line-1',
  intensity_available: true,
};
ranked.analysis.products.intensity_top = [
  {
    ...product,
    product_id: 2,
    product_name: 'Product B',
    rank: 1,
    event_count: 2,
    linked_event_count: 2,
    reference_output: 800,
    reference_intensity_per_1000: 2.5,
  },
  {
    ...product,
    product_id: 1,
    product_name: 'Product A',
    rank: 2,
    event_count: 5,
    linked_event_count: 5,
    reference_output: 5000,
    reference_intensity_per_1000: 1,
  },
];
ranked.analysis.products.intensity_all = ranked.analysis.products.intensity_top;
const rankedHtml = render(ranked);
assert.ok(
  rankedHtml.indexOf('Product B') < rankedHtml.indexOf('Product A'),
  'Server-supplied intensity ranking must remain in server order',
);
assert.match(rankedHtml, />800<\/td>/);
assert.match(rankedHtml, />5,000<\/td>/);
assert.match(rankedHtml, />2\.50<\/td>/);
assert.match(rankedHtml, /Reference output = fixed monthly output × full months in view/);
const beforeRanking = JSON.stringify(ranked);
render(ranked, 'vi');
assert.equal(
  JSON.stringify(ranked),
  beforeRanking,
  'Rendering must not modify or reorder backend metrics',
);

// Section titles, KPI deltas and priority cards are display templates over backend values.
const signalled = structuredClone(empty);
signalled.analysis.kpis = {
  ...emptyMetrics,
  event_count: 10,
  critical_count: 4,
  critical_share_pct: 40,
};
const half = { known_repair_cost: 0, cost_known_count: 0, average_known_repair_cost: null };
signalled.analysis.period_halves = {
  enabled: true,
  first: { ...half, months: ['2024-01'], event_count: 4, critical_count: 4 },
  second: { ...half, months: ['2024-06'], event_count: 6, critical_count: 3 },
  event_count_change: 2,
  event_count_change_pct: 50,
  critical_count_change_pct: -25,
  known_repair_cost_change_pct: null,
};
signalled.analysis.recommendations = [
  {
    priority: 1,
    kind: 'critical_location',
    title: 'Synthetic title',
    evidence: 'Synthetic evidence',
    action: 'Synthetic action',
    owner: 'Synthetic owner',
    success_measure: 'Synthetic measure',
    metrics: {
      location: 'Component',
      critical_share_pct: 42.8,
      critical_count: 148,
      event_count: 346,
      other_locations_critical_share_pct: 31.6,
    },
  },
];
const beforeSignals = JSON.stringify(signalled);
const signalledHtml = render(signalled);
assert.doesNotMatch(signalledHtml, /Prepare follow-up|section-tools|operations-panel/);
assert.doesNotMatch(signalledHtml, /Ask the analyst/);
assert.match(render(signalled, 'en', initialFilters, true), /Ask the analyst/);
assert.match(signalledHtml, /class="kpi-delta worse">.*?\+50\.0% vs Jan</);
assert.match(signalledHtml, /class="kpi-delta better">.*?-25\.0% vs Jan</);
assert.match(signalledHtml, /Component has the highest Critical share: 42\.8%/);
assert.match(signalledHtml, /Critical defects at Component/);
assert.match(signalledHtml, /148 of 346 defects here · 31\.6% Critical at other locations/);
assert.match(signalledHtml, /Synthetic action/);
assert.match(signalledHtml, /Suggested owner<\/span><strong>Synthetic owner/);
assert.ok(
  signalledHtml.indexOf('Synthetic action') < signalledHtml.indexOf('class="priority-plan"'),
  'The proposed action stays visible before the detailed measurement disclosure',
);
assert.doesNotMatch(signalledHtml, /Synthetic title|Synthetic evidence|NaN|Infinity|undefined/);
const signalledVietnamese = render(signalled, 'vi');
assert.match(signalledVietnamese, /Lỗi nghiêm trọng tại Linh kiện/);
assert.match(signalledVietnamese, /Người phụ trách đề xuất/);
assert.match(signalledVietnamese, /Linh kiện có tỷ trọng lỗi nghiêm trọng cao nhất: 42,8%/);
assert.equal(JSON.stringify(signalled), beforeSignals, 'Templates must not modify backend values');

for (const locale of ['en', 'vi'] as const) {
  const half = {
    months: ['2024-01'],
    event_count: 10,
    critical_count: 2,
    known_repair_cost: 20,
    cost_known_count: 2,
    average_known_repair_cost: 10,
  };
  const summary = (costChange: number | null) =>
    renderToStaticMarkup(
      <I18nProvider initialLocale={locale} persist={false}>
        <PeriodHalvesSummary
          halves={{
            enabled: true,
            first: half,
            second: { ...half, months: ['2024-02'] },
            event_count_change: 0,
            event_count_change_pct: 0,
            critical_count_change_pct: 0,
            known_repair_cost_change_pct: costChange,
          }}
        />
      </I18nProvider>,
    );
  assert.doesNotMatch(summary(null), /repair cost|Chi phí sửa|ui\.half_period/i);
  assert.match(
    summary(0),
    locale === 'en' ? /Known repair cost 0\.0%/ : /Chi phí sửa đã biết 0,0%/,
  );
}
function GenericProse({ value }: { value: string }) {
  const { prose } = useDashboardLanguage();
  return <p>{prose(value)}</p>;
}
for (const source of [
  'Review these defect records with operators, inspect representative failures and verify causes before choosing corrective actions.',
  'Track Critical events and their share at this location with actual inspected/produced units and comparable inspection coverage; set a target after baseline validation.',
  'Compare design, materials and process steps of the highest-intensity products in this category with lower-intensity products on the same line; review representative failures before changing the process.',
  'Break down repair cost by repair route, labour, parts and rework with Maintenance; confirm cost-recording rules with Finance; compare the same category on other lines before attributing the gap to the line.',
  'Track average known repair cost per event on this line against other lines for the same category, with cost coverage; set a savings target only after repair-cost records are verified.',
  "Track this category's reference intensity on each line and its ratio to the rest of the line, with Critical share; set a reduction target after actual output is confirmed.",
]) {
  const localized = renderToStaticMarkup(
    <I18nProvider initialLocale="vi" persist={false}>
      <GenericProse value={source} />
    </I18nProvider>,
  );
  assert.doesNotMatch(localized, /Linh kiện|[Nn]hóm\s*4|[Cc]huyền\s*3|Group\s*4|Line[- ]?3|\d/);
}

const shell = renderToStaticMarkup(<App />);
assert.match(shell, /id="dashboard-tab" role="tab" aria-selected="true"/);
assert.match(shell, /id="agent-tab" role="tab" aria-selected="false"/);
assert.match(shell, /id="agent-panel"[^>]*hidden=""/);
assert.match(shell, /Skip to content/);
assert.doesNotMatch(shell, /shared-view-bar/);
for (const filename of readdirSync('src').filter((name) => /\.tsx?$/.test(name))) {
  const source = readFileSync(`src/${filename}`, 'utf-8');
  for (const [, key] of source.matchAll(/["'](ui\.[a-z0-9_]+)["']/g)) {
    assert.ok(key in catalog.messages, `Missing catalog entry ${key} in ${filename}`);
  }
}
console.log(
  'Verified five desktop/mobile destinations, four overview KPIs, removed auxiliary panels, workbook in exports, optional Agent actions, proposed owners, reference output, scope labels, unknown costs, backend ranking and deltas, workbook scope, no technical provenance, localization, escaping and immutable data.',
);

if (process.argv[2]) {
  const live = JSON.parse(
    readFileSync(process.argv[2], 'utf-8').replace(/^\uFEFF/, ''),
  ) as DashboardData;
  const liveHtml = render(live);
  assert.doesNotMatch(liveHtml, /NaN|Infinity|undefined/);
  assert.ok(
    liveHtml.includes(new Intl.NumberFormat('en-US').format(live.analysis.kpis.event_count)),
  );
  assert.ok(!liveHtml.includes(live.context.source_sha256));
  assert.ok(!liveHtml.includes(live.evidence.result_sha256));
  assert.ok(liveHtml.includes(live.links.export_csv + '?locale=en'));
  if (live.links.quality_workbook) assert.ok(liveHtml.includes(live.links.quality_workbook));
  const liveVietnamese = render(live, 'vi');
  assert.ok(
    liveVietnamese.includes(new Intl.NumberFormat('vi-VN').format(live.analysis.kpis.event_count)),
  );
  for (const text of [
    live.analysis.trend.interpretation,
    ...live.analysis.metadata.limitations,
    ...live.analysis.anomalies.limitations,
    live.quality_scope.description,
    ...live.analysis.recommendations.flatMap((item) => [
      item.title,
      item.evidence,
      item.action,
      item.owner,
      item.success_measure,
    ]),
  ]) {
    const escaped = text
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#x27;');
    assert.ok(
      !liveVietnamese.includes(escaped),
      `Canonical prose must have a Vietnamese display translation: ${text}`,
    );
  }
  console.log('Verified rendering against the supplied Dashboard payload.');
}
