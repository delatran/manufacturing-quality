import { memo, useCallback, useEffect, useRef, useState } from 'react';
import { applicationUrl } from './app-url';
import {
  AlertCircle,
  ArrowDownToLine,
  ArrowRight,
  ChartLine,
  CheckCircle2,
  ChevronDown,
  Coins,
  Factory,
  FileSpreadsheet,
  LayoutDashboard,
  ListFilter,
  MapPin,
  RefreshCw,
  SearchX,
  SlidersHorizontal,
  MessageSquare,
  X,
} from 'lucide-react';
import { useDashboardLanguage } from './dashboard-language';
import EvidencePanel from './EvidencePanel';
import type { WorkspacePanel } from './workspace-state';
import { applyPriorityFilters } from './workspace-state';
import { initialFilters } from './dashboard-types';
import type { DashboardData, Filters } from './dashboard-types';
import { Section, SectionNav, Panel, ShareCard } from './DashboardLayout';
import { PriorityCard, Kpi } from './DashboardPriorities';
import {
  LineCostBars,
  PeriodHalvesSummary,
  CategoryLineMatrix,
  CostDistribution,
  MonthlyChart,
  MonthlyBreakdownTable,
  TrendDrivers,
  LocationMatrix,
  InspectionMethodPanel,
  LineRanking,
  ProductRanking,
  AnomalyChart,
} from './DashboardCharts';
import { Records } from './DashboardRecords';
import { shortMonth } from './dashboard-format';
import type { Recommendation, IconComponent, Delta } from './dashboard-display-types';

const fields: {
  key: keyof Filters;
  label: string;
  options: string;
  allLabel?: string;
}[] = [
  { key: 'start_month', label: 'ui.filter_from_month', options: 'months' },
  { key: 'end_month', label: 'ui.filter_through_month', options: 'months' },
  {
    key: 'production_line',
    label: 'ui.filter_production_line',
    options: 'production_lines',
    allLabel: 'ui.filter_all_production_lines',
  },
  {
    key: 'category',
    label: 'ui.filter_category',
    options: 'categories',
    allLabel: 'ui.filter_all_categories',
  },
  {
    key: 'product_id',
    label: 'ui.filter_product',
    options: 'product_ids',
    allLabel: 'ui.filter_all_products',
  },
  {
    key: 'severity',
    label: 'ui.filter_severity',
    options: 'severities',
    allLabel: 'ui.filter_all_severity_levels',
  },
  {
    key: 'defect_type',
    label: 'ui.filter_defect_type',
    options: 'defect_types',
    allLabel: 'ui.filter_all_defect_types',
  },
  {
    key: 'defect_location',
    label: 'ui.filter_defect_location',
    options: 'defect_locations',
    allLabel: 'ui.filter_all_defect_locations',
  },
  {
    key: 'inspection_method',
    label: 'ui.filter_inspection_method',
    options: 'inspection_methods',
    allLabel: 'ui.filter_all_inspection_methods',
  },
];

export default memo(function Dashboard({
  data,
  loading,
  refreshing,
  refreshFeedback = '',
  error,
  filters,
  onApply,
  onRefresh,
  onAnalyze,
  canAnalyze = false,
  inspectionRequest,
}: {
  data?: DashboardData;
  loading: boolean;
  refreshing: boolean;
  refreshFeedback?: string;
  error: string;
  filters: Filters;
  onApply: (filters: Filters) => void;
  onRefresh: () => void;
  onAnalyze: (priority?: number) => void;
  canAnalyze?: boolean;
  inspectionRequest?: { id: number; panel: WorkspacePanel };
}) {
  const { t, tr, prose, number, percent, month, date, sourceLabel, download, locale } =
    useDashboardLanguage();
  const [draft, setDraft] = useState<Filters>(filters);
  const [filtersExpanded, setFiltersExpanded] = useState(false);
  const [recordsExpanded, setRecordsExpanded] = useState(false);
  const [recordsDate, setRecordsDate] = useState<string>();
  const clearRecordsDate = useCallback(() => setRecordsDate(undefined), []);
  const [previousScope, setPreviousScope] = useState<Filters>();
  const [evidencePriority, setEvidencePriority] = useState<number | null>(null);
  const filterToggle = useRef<HTMLButtonElement>(null);
  const recordsSection = useRef<HTMLElement>(null);
  const pendingRecordFilters = useRef<Filters | undefined>(undefined);
  const pendingScopeReturn = useRef<Filters | undefined>(undefined);
  useEffect(() => {
    const expected = pendingScopeReturn.current;
    if (
      !loading &&
      data &&
      expected &&
      Object.entries(expected).every(
        ([key, value]) => data.context.filters[key as keyof Filters] === value,
      )
    ) {
      pendingScopeReturn.current = undefined;
      setPreviousScope(undefined);
    }
  }, [loading, data]);
  function openRecords() {
    setRecordsExpanded(true);
    requestAnimationFrame(() => {
      recordsSection.current?.focus();
      recordsSection.current?.scrollIntoView({ block: 'start' });
    });
  }
  useEffect(() => {
    if (loading || !data || !pendingRecordFilters.current) return;
    const expected = pendingRecordFilters.current;
    pendingRecordFilters.current = undefined;
    if (
      !Object.entries(expected).every(
        ([key, value]) => data.context.filters[key as keyof Filters] === value,
      )
    )
      return;
    requestAnimationFrame(() => {
      recordsSection.current?.focus();
      recordsSection.current?.scrollIntoView({ block: 'start' });
    });
  }, [loading, data]);
  useEffect(() => {
    setEvidencePriority(null);
    setRecordsDate(undefined);
  }, [data?.context.context_id]);
  // Reset the old scope before opening a panel requested with the new context.
  useEffect(() => {
    if (inspectionRequest?.panel === 'records') openRecords();
    if (inspectionRequest?.panel === 'evidence') setEvidencePriority(0);
  }, [inspectionRequest]);
  useEffect(() => setDraft(filters), [filters]);
  const applied = data?.context.filters || filters;
  const dirty =
    fields.some(({ key }) => draft[key] !== applied[key]) || draft.scenario !== applied.scenario;
  const invalid = draft.start_month > draft.end_month;
  const report = data?.analysis;
  const kpis = report?.kpis;
  const activeFilterEntries = fields
    .filter(({ key }) => !['start_month', 'end_month'].includes(key) && applied[key])
    .map(({ key, label }) => ({
      key,
      text: `${tr(label)}: ${key === 'product_id' ? `#${applied[key]}` : sourceLabel(applied[key])}`,
    }));
  const scopeLabels = activeFilterEntries.map((entry) => entry.text);
  const hasNarrowedFilters =
    activeFilterEntries.length > 0 ||
    applied.start_month !== initialFilters.start_month ||
    applied.end_month !== initialFilters.end_month;
  const halves = report?.period_halves?.enabled ? report.period_halves : undefined;
  // Sparklines plot backend monthly values as-is; a series with any missing month is not drawn.
  const monthlySeries = (
    pick: (row: DashboardData['analysis']['monthly'][number]) => number | null,
  ) => {
    const values = report?.monthly.map(pick) ?? [];
    return values.every((value): value is number => value != null) ? values : undefined;
  };
  const signed = (value: number | null | undefined) =>
    value == null ? number(value) : `${value > 0 ? '+' : ''}${percent(value)}`;
  // Higher defect counts and costs are worse; the backend supplies every compared value.
  const firstMonths = halves?.first.months ?? [];
  const secondMonths = halves?.second.months ?? [];
  const span = (months: string[]) =>
    months.length > 1
      ? `${shortMonth(months[0], locale)}–${shortMonth(months[months.length - 1], locale)}`
      : months.length
        ? shortMonth(months[0], locale)
        : '';
  const firstSpan = span(firstMonths);
  const change = (value: number | null | undefined): Delta | undefined =>
    value == null || !firstSpan
      ? undefined
      : {
          text: t('ui.dashboard_vs_first_half', { value0: signed(value), value1: firstSpan }),
          direction: value > 0 ? 'up' : value < 0 ? 'down' : 'flat',
          tone: value > 0 ? 'worse' : value < 0 ? 'better' : 'neutral',
        };
  const recommendation = (kind: string) =>
    report?.recommendations.find((item) => item.kind === kind);
  const numeric = (item: Recommendation | undefined, key: string) => {
    const value = item?.metrics?.[key];
    return typeof value === 'number' ? value : null;
  };
  const label = (item: Recommendation | undefined, key: string) => {
    const value = item?.metrics?.[key];
    return typeof value === 'string' ? sourceLabel(value) : '';
  };
  const critical = recommendation('critical_location');
  const category = recommendation('category_intensity');
  const lineCost = recommendation('line_repair_cost');
  const typeCost = recommendation('defect_type_cost');
  const lineLeader = report?.lines.find(
    (row) => row.reference_intensity_per_1000 != null && row.event_count > 0,
  );
  const firstMonth = report?.monthly[0];
  const lastMonth = report?.monthly[report.monthly.length - 1];
  const trendChange = report?.trend.change_pct;
  // Prefer the half-versus-half change (the same comparison as the KPI badges); fall back to
  // the backend's first-versus-last-month change when halves are unavailable.
  const halfChange = halves?.event_count_change_pct;
  const trendTitle =
    halfChange != null && halfChange !== 0 && firstSpan
      ? t(halfChange > 0 ? 'ui.dashboard_trend_up' : 'ui.dashboard_trend_down', {
          value0: percent(Math.abs(halfChange)),
          value1: firstSpan,
          value2: span(secondMonths),
        })
      : report && firstMonth && lastMonth && report.monthly.length > 1 && trendChange
        ? t(trendChange > 0 ? 'ui.dashboard_trend_up' : 'ui.dashboard_trend_down', {
            value0: percent(Math.abs(trendChange)),
            value1: month(firstMonth.month),
            value2: month(lastMonth.month),
          })
        : t('ui.dashboard_trend_flat');
  // A severity filter makes every location's severity mix identical, so no mix headline.
  const locationTitle =
    critical && !applied.severity
      ? t('ui.dashboard_location_title', {
          value0: label(critical, 'location'),
          value1: percent(numeric(critical, 'critical_share_pct')),
        })
      : t('ui.dashboard_location_flat');
  const compared = numeric(category, 'lines_compared');
  const above = numeric(category, 'lines_above_rest_of_line');
  const hotspotTitle = category
    ? compared && compared >= 2 && above === compared
      ? t('ui.dashboard_hotspots_every', { value0: label(category, 'category') })
      : compared
        ? t('ui.dashboard_hotspots_some', {
            value0: label(category, 'category'),
            value1: number(above),
            value2: number(compared),
          })
        : t('ui.dashboard_hotspots_leader', {
            value0: label(category, 'category'),
            value1: number(numeric(category, 'reference_intensity_per_1000'), 2),
          })
    : lineLeader
      ? t('ui.dashboard_hotspots_leader', {
          value0: sourceLabel(lineLeader.production_line),
          value1: number(lineLeader.reference_intensity_per_1000, 2),
        })
      : t('ui.dashboard_hotspots_flat');
  const costTitle = lineCost
    ? t('ui.dashboard_cost_line_title', {
        value0: label(lineCost, 'production_line'),
        value1: number(numeric(lineCost, 'ratio_to_other_lines'), 2),
      })
    : typeCost
      ? t('ui.dashboard_cost_type_title', {
          value0: label(typeCost, 'defect_type'),
          value1: percent(numeric(typeCost, 'known_cost_share_pct')),
        })
      : t('ui.dashboard_cost_flat');
  const screen = report?.anomalies;
  const warningTitle = !screen?.enabled
    ? t('ui.dashboard_warning_disabled')
    : screen.candidates.length
      ? t('ui.dashboard_warning_some', {
          value0: number(screen.candidates.length),
          value1: date(screen.candidates[0].date),
        })
      : t('ui.dashboard_warning_none');
  const navItems: { id: string; label: string; icon: IconComponent }[] = [
    { id: 'section-overview', label: t('ui.dashboard_nav_overview'), icon: LayoutDashboard },
    { id: 'section-trend', label: t('ui.dashboard_nav_trend'), icon: ChartLine },
    { id: 'section-location', label: t('ui.dashboard_nav_location'), icon: MapPin },
    { id: 'section-hotspots', label: t('ui.dashboard_nav_hotspots'), icon: Factory },
    { id: 'section-cost', label: t('ui.dashboard_nav_cost'), icon: Coins },
  ];
  return (
    <main className="dashboard" id="dashboard-content" tabIndex={-1}>
      <div className="dashboard-header">
        <div>
          <span className="dashboard-eyebrow">{t('ui.dashboard_eyebrow')}</span>
          <h1>{t('ui.dashboard_title')}</h1>
          <p>{t('ui.dashboard_subtitle')}</p>
        </div>
        {canAnalyze && (
          <button
            className="primary-button analyze-button"
            onClick={() => onAnalyze()}
            disabled={!data || loading || refreshing}
          >
            <MessageSquare size={16} aria-hidden="true" />
            {t('ui.dashboard_ask_agent')}
            <ArrowRight size={16} aria-hidden="true" />
          </button>
        )}
      </div>
      <section className="scope-panel dashboard-toolbar" aria-label={tr('ui.dashboard_filters')}>
        <div className="scope-toolbar">
          <div className="scope-summary">
            <span className="scope-label">{t('ui.viewing')}</span>
            <strong>
              {month(applied.start_month)} <span className="scope-arrow">→</span>{' '}
              {month(applied.end_month)}
            </strong>
            <span>
              {scopeLabels.length ? scopeLabels.join(' · ') : t('ui.all_lines_all_products')}
            </span>
            {activeFilterEntries.length > 0 && (
              <div className="active-filter-chips">
                {activeFilterEntries.map((entry) => (
                  <button
                    key={entry.key}
                    type="button"
                    className="filter-chip"
                    disabled={loading || refreshing}
                    aria-label={t('ui.remove_filter', { value0: entry.text })}
                    onClick={() => {
                      const next = { ...applied, [entry.key]: '' };
                      setDraft(next);
                      onApply(next);
                    }}
                  >
                    <span>{entry.text}</span>
                    <X size={13} aria-hidden="true" />
                  </button>
                ))}
              </div>
            )}
            {applied.scenario && (
              <span className="applied-scenario">
                {t('ui.scenario')}: {sourceLabel(applied.scenario)}
              </span>
            )}
          </div>
          <div className="scope-toolbar-actions">
            {hasNarrowedFilters && (
              <button
                type="button"
                className="text-button toolbar-reset-btn"
                disabled={loading || refreshing}
                onClick={() => {
                  setPreviousScope(undefined);
                  setDraft({ ...initialFilters });
                  onApply({ ...initialFilters });
                }}
              >
                {t('ui.reset_filters')}
              </button>
            )}
            <button
              ref={filterToggle}
              className="secondary-button"
              aria-expanded={filtersExpanded}
              aria-controls="dashboard-filter-form"
              onClick={() => setFiltersExpanded((value) => !value)}
            >
              <SlidersHorizontal size={16} aria-hidden="true" />
              {t('ui.filters')}
              {dirty && <span className="filter-state pending">{t('ui.unapplied_changes')}</span>}
              <ChevronDown
                size={14}
                className={filtersExpanded ? 'chevron-open' : ''}
                aria-hidden="true"
              />
            </button>
          </div>
        </div>
        {previousScope && (
          <div className="priority-focus-return" role="status">
            <button
              type="button"
              className="text-button"
              disabled={loading || refreshing}
              onClick={() => {
                pendingScopeReturn.current = { ...previousScope };
                onApply({ ...previousScope });
              }}
            >
              {t('ui.dashboard_return_scope')}
            </button>
          </div>
        )}
        <form
          id="dashboard-filter-form"
          hidden={!filtersExpanded}
          onSubmit={(event) => {
            event.preventDefault();
            if (!invalid && !loading && !refreshing) {
              onApply({ ...draft });
              setFiltersExpanded(false);
              filterToggle.current?.focus({ preventScroll: true });
            }
          }}
        >
          <div className="filter-form-heading">
            <strong>{t('ui.choose_your_view')}</strong>
            <span className={`filter-state${dirty ? ' pending' : ''}`}>
              {dirty ? tr('ui.unapplied_changes') : tr('ui.applied_filters')}
            </span>
          </div>
          <div className="filter-grid">
            {fields.map((field) => (
              <label key={field.key}>
                <span>{tr(field.label)}</span>
                <select
                  value={draft[field.key]}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, [field.key]: event.target.value }))
                  }
                >
                  {field.allLabel && <option value="">{tr(field.allLabel)}</option>}
                  {(
                    data?.options[field.options] ||
                    (field.options === 'months'
                      ? ['2024-01', '2024-02', '2024-03', '2024-04', '2024-05', '2024-06']
                      : [])
                  ).map((value) => (
                    <option key={value} value={value}>
                      {field.options === 'months'
                        ? month(value)
                        : field.key === 'product_id'
                          ? `#${value}`
                          : sourceLabel(value)}
                    </option>
                  ))}
                </select>
              </label>
            ))}
          </div>
          {invalid && (
            <p className="filter-error" role="alert">
              {tr('ui.the_start_month_must_be_on_or_before_the_end_month')}
            </p>
          )}
          <div className="filter-footer">
            <p>{t('ui.changes_take_effect_when_you_apply_the_view_uses_complete_months')}</p>
            <div>
              <button
                type="button"
                className="text-button"
                onClick={() => setDraft({ ...initialFilters })}
              >
                {t('ui.dashboard_reset_selections')}
              </button>
              <button
                type="button"
                className="text-button"
                disabled={!dirty}
                onClick={() => setDraft({ ...applied })}
              >
                {t('ui.dashboard_discard_changes')}
              </button>
              <button
                type="submit"
                className="primary-button"
                disabled={!dirty || invalid || loading || refreshing}
              >
                {loading ? tr('ui.applying') : tr('ui.apply_filters')}
                <ArrowRight size={14} aria-hidden="true" />
              </button>
            </div>
          </div>
        </form>
        {loading && data && (
          <span
            className="dashboard-progress"
            role="status"
            aria-label={t('ui.dashboard_updating')}
          />
        )}
      </section>
      {data && (
        <div className="dashboard-utilities" aria-label={t('ui.dashboard_utilities')}>
          <details className="export-menu" id="section-share">
            <summary>{t('ui.dashboard_exports')}</summary>
            <div className="export-menu-content">
              <p>{t('ui.dashboard_share_caption')}</p>
              <div className="share-grid">
                <ShareCard
                  icon={ArrowDownToLine}
                  title={t('ui.dashboard_share_csv')}
                  description={t('ui.dashboard_share_csv_desc')}
                  href={download(data.links.export_csv)}
                  action={t('ui.export_filtered_csv')}
                />
                {data.links.quality_workbook && (
                  <ShareCard
                    icon={FileSpreadsheet}
                    title={t('ui.dashboard_share_workbook')}
                    description={t('ui.dashboard_share_workbook_desc')}
                    href={applicationUrl(data.links.quality_workbook)}
                    action={t('ui.dashboard_download_workbook')}
                    note={t('ui.the_workbook_is_a_separate_full_case_file_and_does_not_follow_das')}
                  />
                )}
              </div>
              <div className="source-check">
                <span>{t('ui.last_source_check')}</span>
                <time dateTime={data.status.last_attempt_at || data.context.fetched_at}>
                  {date(data.status.last_attempt_at || data.context.fetched_at)}
                </time>
                <button
                  className="text-button"
                  onClick={onRefresh}
                  disabled={refreshing || loading}
                >
                  <RefreshCw size={14} className={refreshing ? 'spin' : ''} aria-hidden="true" />
                  {refreshing ? t('ui.checking') : t('ui.check_source')}
                </button>
                <small>{t('ui.a_source_check_does_not_change_the_study_s_historical_dates')}</small>
              </div>
            </div>
          </details>
          <button
            type="button"
            className="text-button"
            onClick={() => {
              setRecordsDate(undefined);
              openRecords();
            }}
          >
            {t('ui.explore_the_defect_log')}
          </button>
        </div>
      )}
      {error && (
        <div className="dashboard-alert" role="alert">
          <AlertCircle size={19} aria-hidden="true" />
          <div>
            <strong>{tr('ui.could_not_update_this_view')}</strong>
            <p>
              {data
                ? t('ui.the_last_successful_view_is_still_shown_your_selection_has_not_be')
                : t('ui.check_the_connection_and_try_again')}
            </p>
            <p>{prose(error)}</p>
          </div>
          <button className="secondary-button" onClick={() => onApply({ ...filters })}>
            {tr('ui.retry')}
          </button>
        </div>
      )}
      {data?.status.stale && (
        <div className="dashboard-alert" role="status">
          <AlertCircle size={19} aria-hidden="true" />
          <div>
            <strong>{t('ui.source_check_failed_saved_data_is_still_available')}</strong>
            <p>
              {tr('ui.last_attempt')} {date(data.status.last_attempt_at)}
            </p>
          </div>
        </div>
      )}
      {refreshFeedback && (
        <p className="refresh-feedback" role="status">
          <CheckCircle2 size={16} aria-hidden="true" />
          {t('ui.source_checked_the_records_still_cover_the_historical_study_perio')}
        </p>
      )}
      {loading && !data && (
        <div className="loading-strip" role="status">
          <RefreshCw size={15} className="spin" aria-hidden="true" />
          {t('ui.preparing_your_quality_overview')}
        </div>
      )}
      {!data && loading && (
        <div className="dashboard-skeleton" aria-hidden="true">
          <div className="skeleton-kpis">
            {[0, 1, 2, 3].map((n) => (
              <div key={n} />
            ))}
          </div>
          <div className="skeleton-chart" />
        </div>
      )}
      {data && report && kpis && (
        <div className="dashboard-layout">
          <SectionNav items={navItems} label={t('ui.dashboard_nav_label')} />
          <div className={`dashboard-results${loading ? ' updating' : ''}`} aria-busy={loading}>
            <Section
              id="section-overview"
              kicker={t('ui.dashboard_overview_kicker')}
              title={
                applied.start_month === applied.end_month
                  ? t('ui.dashboard_overview_title_single', { value0: month(applied.start_month) })
                  : t('ui.dashboard_overview_title', {
                      value0: month(applied.start_month),
                      value1: month(applied.end_month),
                    })
              }
              caption={t('ui.dashboard_overview_caption')}
            >
              <section className="kpi-grid" aria-label={tr('ui.key_performance_indicators')}>
                <Kpi
                  label={t('ui.dashboard_kpi_defects')}
                  value={number(kpis.event_count)}
                  delta={change(halves?.event_count_change_pct)}
                  detail={t('ui.dashboard_kpi_defects_detail')}
                  spark={monthlySeries((row) => row.event_count)}
                  sparkLabel={t('ui.dashboard_spark_label')}
                />
                <Kpi
                  label={t('ui.dashboard_kpi_critical')}
                  value={number(kpis.critical_count)}
                  delta={change(halves?.critical_count_change_pct)}
                  detail={
                    kpis.critical_share_pct == null
                      ? t('ui.no_events_for_share')
                      : t('ui.dashboard_kpi_critical_detail', {
                          value0: percent(kpis.critical_share_pct),
                        })
                  }
                  tone="critical"
                  spark={monthlySeries((row) => row.critical_count)}
                  sparkLabel={t('ui.dashboard_spark_label')}
                />
                <Kpi
                  label={t('ui.dashboard_kpi_rate')}
                  value={number(kpis.reference_intensity_per_1000, 2)}
                  help={t('ui.dashboard_kpi_rate_help')}
                  detail={
                    kpis.reference_intensity_status === 'no_eligible_products'
                      ? t('ui.no_eligible_output')
                      : kpis.reference_intensity_status === 'incomplete_output'
                        ? t('ui.incomplete_output')
                        : t('ui.dashboard_kpi_rate_detail', {
                            value0: number(kpis.reference_output),
                          })
                  }
                  spark={monthlySeries((row) => row.reference_intensity_per_1000)}
                  sparkLabel={t('ui.dashboard_spark_label')}
                />
                <Kpi
                  label={t('ui.dashboard_kpi_cost')}
                  value={
                    kpis.cost_available ? number(kpis.known_repair_cost, 0) : tr('ui.unavailable')
                  }
                  delta={change(halves?.known_repair_cost_change_pct)}
                  help={t('ui.dashboard_kpi_cost_help')}
                  detail={
                    kpis.cost_coverage_pct == null
                      ? t('ui.no_events_for_cost_coverage')
                      : t('ui.dashboard_kpi_cost_detail', {
                          value0: percent(kpis.cost_coverage_pct),
                        })
                  }
                  spark={monthlySeries((row) =>
                    row.cost_available ? row.known_repair_cost : null,
                  )}
                  sparkLabel={t('ui.dashboard_spark_label')}
                />
              </section>
              {kpis.event_count === 0 && (
                <div className="empty-scope">
                  <SearchX size={26} aria-hidden="true" />
                  <div>
                    <h3>{tr('ui.no_logged_events_in_this_scope')}</h3>
                    <p>
                      {t('ui.try_a_wider_view_no_records_does_not_establish_zero_defects_or_ze')}
                    </p>
                  </div>
                  <button
                    className="secondary-button"
                    onClick={() => {
                      setDraft({ ...initialFilters });
                      onApply({ ...initialFilters });
                    }}
                  >
                    {t('ui.show_all_data')}
                  </button>
                </div>
              )}
              {report.recommendations.length > 0 && (
                <div className="priorities-section">
                  <div className="dashboard-subhead">
                    <span className="section-kicker">{t('ui.dashboard_priorities_kicker')}</span>
                    <h3>{t('ui.dashboard_priorities_title')}</h3>
                    <p>{t('ui.dashboard_priorities_caption')}</p>
                  </div>
                  <div className="priorities-grid">
                    {report.recommendations.map((item) => {
                      const isFocused = Boolean(
                        item.filters &&
                        Object.entries(item.filters).every(
                          ([k, v]) => data.context.filters[k as keyof Filters] === v,
                        ),
                      );
                      return (
                        <PriorityCard
                          key={item.priority}
                          item={item}
                          onEvidence={() => setEvidencePriority(item.priority)}
                          isFocused={isFocused}
                          onFocus={
                            item.filters
                              ? () => {
                                  if (isFocused && previousScope) {
                                    pendingScopeReturn.current = { ...previousScope };
                                    onApply({ ...previousScope });
                                    return;
                                  }
                                  const next = applyPriorityFilters(
                                    data.context.filters,
                                    item.filters!,
                                  );
                                  setPreviousScope(
                                    (current) => current ?? { ...data.context.filters },
                                  );
                                  onApply(next);
                                }
                              : undefined
                          }
                          canAnalyze={canAnalyze}
                          onAnalyze={() => onAnalyze(item.priority)}
                        />
                      );
                    })}
                  </div>
                </div>
              )}
              {evidencePriority !== null && (
                <EvidencePanel
                  data={data}
                  priority={evidencePriority || undefined}
                  onClose={() => setEvidencePriority(null)}
                  onRecords={() => {
                    setRecordsDate(undefined);
                    openRecords();
                  }}
                  onFocus={(focus) => {
                    const next = applyPriorityFilters(data.context.filters, focus);
                    setPreviousScope((current) => current ?? { ...data.context.filters });
                    pendingRecordFilters.current = next;
                    onApply(next);
                    setRecordsExpanded(true);
                  }}
                />
              )}
            </Section>
            <Section
              id="section-trend"
              kicker={t('ui.dashboard_trend_kicker')}
              title={trendTitle}
              caption={t('ui.dashboard_trend_caption')}
            >
              <div className="dashboard-panel trend-panel">
                <MonthlyChart rows={report.monthly} />
                <PeriodHalvesSummary halves={report.period_halves} />
                <p className="chart-interpretation">{prose(report.trend.interpretation)}</p>
                <MonthlyBreakdownTable rows={report.monthly} />
              </div>
              <TrendDrivers data={data} />
              <Section
                id="section-warning"
                kicker={t('ui.dashboard_warning_kicker')}
                title={warningTitle}
                caption={
                  report.anomalies.enabled
                    ? t('ui.dashboard_warning_caption', {
                        value0: number(report.anomalies.baseline_daily_mean, 2),
                        value1: number(report.anomalies.upper_reference_limit, 2),
                      })
                    : undefined
                }
              >
                <div className="dashboard-panel warning-panel">
                  <AnomalyChart data={data} />
                  {report.calendar_coverage?.max_observed_day_of_month != null && (
                    <p className="calendar-gap-callout">
                      {t('ui.calendar_gap_badge', {
                        value0: number(report.calendar_coverage.max_observed_day_of_month),
                        value1: number(report.calendar_coverage.days_without_events),
                        value2: number(report.calendar_coverage.calendar_days),
                      })}
                    </p>
                  )}
                  {!report.anomalies.enabled && (
                    <p className="empty-chart">
                      {tr('ui.no_january_baseline_events_for_this_scope_the_screen_is_unavailab')}
                    </p>
                  )}
                  {report.anomalies.candidates.length > 0 && (
                    <div className="unusual-days">
                      <h3>{t('ui.dashboard_days_to_review')}</h3>
                      <ul>
                        {report.anomalies.candidates.map((item) => (
                          <li key={item.date}>
                            <time dateTime={item.date}>{date(item.date)}</time>
                            <strong>
                              {t('ui.dashboard_defects_n', { value0: number(item.count) })}
                            </strong>
                            <button
                              type="button"
                              className="text-button"
                              onClick={() => {
                                setRecordsDate(item.date);
                                openRecords();
                              }}
                            >
                              {t('ui.dashboard_review_day')}
                            </button>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                  <details className="nested-details">
                    <summary>{t('ui.how_to_read_this_signal')}</summary>
                    <p>
                      {prose(report.anomalies.method)}
                      {tr('ui.january_stays_fixed_when_selected_months_change')}
                    </p>
                    <ul>
                      {report.anomalies.limitations.map((text) => (
                        <li key={text}>{prose(text)}</li>
                      ))}
                    </ul>
                  </details>
                </div>
              </Section>
            </Section>
            <Section
              id="section-location"
              kicker={t('ui.dashboard_location_kicker')}
              title={locationTitle}
              caption={t('ui.dashboard_location_caption')}
            >
              <div className="dashboard-panel">
                <LocationMatrix data={data} />
              </div>
              <InspectionMethodPanel data={data} />
            </Section>
            <Section
              id="section-hotspots"
              kicker={t('ui.dashboard_hotspots_kicker')}
              title={hotspotTitle}
              caption={t('ui.dashboard_hotspots_caption')}
            >
              <div className="dashboard-grid ranking-grid">
                <Panel kicker={t('ui.compare_lines')} title={t('ui.dashboard_lines_title')}>
                  <p className="panel-caption">{t('ui.dashboard_lines_caption')}</p>
                  <LineRanking rows={report.lines} />
                </Panel>
                <Panel kicker={t('ui.compare_products')} title={t('ui.dashboard_products_title')}>
                  <ProductRanking data={data} />
                </Panel>
              </div>
              <CategoryLineMatrix data={data} />
            </Section>
            <Section
              id="section-cost"
              kicker={t('ui.dashboard_cost_kicker')}
              title={costTitle}
              caption={t('ui.dashboard_cost_caption')}
            >
              <div className="cost-average-kpi">
                <Kpi
                  label={t('ui.dashboard_kpi_avg')}
                  value={number(kpis.average_known_repair_cost, 2)}
                  detail={
                    halves
                      ? t('ui.dashboard_kpi_avg_detail', {
                          value0: number(halves.first.average_known_repair_cost, 0),
                          value1: number(halves.second.average_known_repair_cost, 0),
                          value2: firstSpan,
                          value3: span(secondMonths),
                        })
                      : t('ui.dashboard_kpi_avg_detail_simple')
                  }
                  spark={monthlySeries((row) => row.average_known_repair_cost)}
                  sparkLabel={t('ui.dashboard_spark_label')}
                />
              </div>
              <div className="dashboard-grid equal">
                <Panel kicker={t('ui.repair_costs')} title={t('ui.dashboard_cost_by_line')}>
                  <p className="panel-caption">{t('ui.dashboard_cost_by_line_caption')}</p>
                  <LineCostBars rows={report.line_cost_comparison ?? []} />
                </Panel>
                <Panel kicker={t('ui.repair_costs')} title={t('ui.dashboard_cost_by_type')}>
                  <div className="cost-summary">
                    <div>
                      <span>{tr('ui.observed_repair_cost')}</span>
                      <strong>
                        {kpis.cost_available
                          ? number(kpis.known_repair_cost, 2)
                          : tr('ui.unavailable')}
                      </strong>
                      <small>{tr('ui.local_currency_unspecified')}</small>
                    </div>
                    <div>
                      <span>{tr('ui.cost_coverage')}</span>
                      <strong>{percent(kpis.cost_coverage_pct)}</strong>
                      <small>
                        {number(kpis.cost_unknown_count)}{' '}
                        {tr('ui.selected_events_have_unknown_cost')}
                      </small>
                    </div>
                  </div>
                  <div
                    className="coverage-track"
                    role="img"
                    aria-label={
                      kpis.cost_coverage_pct == null
                        ? t('ui.no_events_for_cost_coverage')
                        : `${t('ui.dashboard_cost_coverage_bar')}: ${percent(kpis.cost_coverage_pct)}`
                    }
                  >
                    <span style={{ width: `${kpis.cost_coverage_pct ?? 0}%` }} />
                  </div>
                  <div className="cost-rows">
                    {report.costs.by_type.map((item) => (
                      <div className="cost-row" key={item.name}>
                        <div>
                          <span>{sourceLabel(item.name)}</span>
                          <strong>
                            {item.cost_available
                              ? number(item.known_repair_cost, 2)
                              : tr('ui.unavailable')}
                          </strong>
                        </div>
                        <div className="cost-bar">
                          <span style={{ width: `${item.known_cost_share_pct ?? 0}%` }} />
                        </div>
                        <small>
                          {percent(item.known_cost_share_pct)} ·{' '}
                          {t('ui.dashboard_avg_short', {
                            value0: number(item.average_known_repair_cost, 2),
                          })}
                        </small>
                      </div>
                    ))}
                  </div>
                </Panel>
              </div>
              <div className="dashboard-panel cost-distribution-panel">
                {report.cost_distribution && (
                  <CostDistribution distribution={report.cost_distribution} />
                )}
                {report.costs.by_severity.length > 0 && (
                  <div className="cost-severity-strip">
                    <h4>{t('ui.cost_by_severity_title')}</h4>
                    <div className="cost-severity-cards">
                      {report.costs.by_severity.map((item) => (
                        <div className="cost-severity-item" key={item.name}>
                          <span>
                            <i className={`legend-swatch ${item.name.toLowerCase()}`} />
                            {sourceLabel(item.name)}
                          </span>
                          <strong>
                            {item.cost_available
                              ? number(item.average_known_repair_cost, 2)
                              : tr('ui.unavailable')}
                          </strong>
                          <small>
                            {t('ui.dashboard_cost_share_line', {
                              value0: percent(item.known_cost_share_pct),
                            })}
                          </small>
                        </div>
                      ))}
                    </div>
                    <p className="panel-footnote">{t('ui.cost_by_severity_note')}</p>
                  </div>
                )}
                <details className="nested-details">
                  <summary>{tr('ui.product_repair_cost_concentration')}</summary>
                  <div
                    className="table-scroll"
                    tabIndex={0}
                    role="region"
                    aria-label={t('ui.product_repair_costs')}
                  >
                    <table>
                      <thead>
                        <tr>
                          <th>{tr('ui.product_2')}</th>
                          <th>{tr('ui.known_cost')}</th>
                          <th>{tr('ui.coverage')}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {report.products.cost_top.map((item) => (
                          <tr key={item.product_id}>
                            <td>{sourceLabel(item.product_name)}</td>
                            <td>
                              {item.cost_available
                                ? number(item.known_repair_cost, 2)
                                : tr('ui.unavailable')}
                            </td>
                            <td>{percent(item.cost_coverage_pct)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <p>
                    {report.products.cost_pareto_product_count_80pct == null
                      ? t('ui.no_positive_linked_repair_cost_for_pareto')
                      : t('ui.products_account_for_80_of_known_linked_repair_costs', {
                          count: number(report.products.cost_pareto_product_count_80pct),
                          total: number(kpis.cohort_product_count),
                        })}
                  </p>
                </details>
              </div>
            </Section>
            <div id="section-records">
              <section
                className="records-disclosure"
                id="records-section"
                ref={recordsSection}
                tabIndex={-1}
              >
                <button
                  className="records-disclosure-toggle"
                  aria-expanded={recordsExpanded}
                  aria-controls="defect-records"
                  onClick={() => setRecordsExpanded((value) => !value)}
                >
                  <span>
                    <ListFilter size={20} aria-hidden="true" />
                    <span>
                      <strong>{t('ui.explore_the_defect_log')}</strong>
                      <small>
                        {recordsDate ? (
                          date(recordsDate)
                        ) : (
                          <>
                            {number(kpis.event_count)} {t('ui.events_in_this_view')}
                          </>
                        )}
                      </small>
                    </span>
                  </span>
                  <ChevronDown
                    size={19}
                    className={recordsExpanded ? 'chevron-open' : ''}
                    aria-hidden="true"
                  />
                </button>
                <div id="defect-records" hidden={!recordsExpanded}>
                  {recordsExpanded && (
                    <Records
                      key={data.context.context_id}
                      data={data}
                      dateFilter={recordsDate}
                      onClearDate={clearRecordsDate}
                    />
                  )}
                </div>
              </section>
            </div>
          </div>
        </div>
      )}
    </main>
  );
});
