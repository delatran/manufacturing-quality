import { memo, useMemo, useState } from 'react';
import type { CSSProperties } from 'react';
import { ArrowRight, ChevronDown } from 'lucide-react';
import { useDashboardLanguage } from './dashboard-language';
import type { DashboardData, LineCostComparison, Metrics, PeriodHalves } from './dashboard-types';
import { shortMonth } from './dashboard-format';

export const LineCostBars = memo(function LineCostBars({ rows }: { rows: LineCostComparison[] }) {
  const { t, number, percent, sourceLabel } = useDashboardLanguage();
  // Scale only changes bar geometry; the server supplies metrics and order.
  const max = Math.max(1, ...rows.map((row) => row.average_known_repair_cost ?? 0));
  if (!rows.length)
    return <p className="empty-chart">{t('ui.no_known_repair_cost_in_this_view')}</p>;
  return (
    <div className="line-rankings cost-rankings">
      {rows.map((row, index) => (
        <div
          className={`line-row${index === 0 && (row.ratio_to_other_lines ?? 0) > 1 ? ' highlight' : ''}`}
          key={row.production_line}
        >
          <span className="rank-index">{String(index + 1).padStart(2, '0')}</span>
          <div>
            <div className="ranking-label">
              <strong>{sourceLabel(row.production_line)}</strong>
              <span>{number(row.average_known_repair_cost, 2)}</span>
            </div>
            <div className="ranking-track">
              <span style={{ width: `${((row.average_known_repair_cost ?? 0) / max) * 100}%` }} />
            </div>
            <small>
              {row.ratio_to_other_lines == null
                ? ''
                : `${t('ui.ratio_to_other_lines', { value0: number(row.ratio_to_other_lines, 2) })} · `}
              {t('ui.dashboard_cost_share_line', { value0: percent(row.known_cost_share_pct) })}
            </small>
          </div>
        </div>
      ))}
    </div>
  );
});

export const PeriodHalvesSummary = memo(function PeriodHalvesSummary({
  halves,
}: {
  halves?: PeriodHalves;
}) {
  const { t, number, percent, month } = useDashboardLanguage();
  if (!halves?.enabled) return null;
  const signed = (value: number | null) =>
    value == null ? number(value) : `${value > 0 ? '+' : ''}${percent(value)}`;
  const span = (months: string[]) =>
    months.length > 1
      ? `${month(months[0])} – ${month(months[months.length - 1])}`
      : month(months[0]);
  return (
    <div className="period-halves" role="group" aria-label={t('ui.half_period_comparison')}>
      <div>
        <span>{span(halves.first.months)}</span>
        <strong>{number(halves.first.event_count)}</strong>
      </div>
      <ArrowRight size={16} aria-hidden="true" />
      <div>
        <span>{span(halves.second.months)}</span>
        <strong>{number(halves.second.event_count)}</strong>
      </div>
      <p>
        {t(
          halves.known_repair_cost_change_pct == null
            ? 'ui.half_period_changes_without_cost'
            : 'ui.half_period_changes',
          {
            value0: signed(halves.event_count_change_pct),
            value1: signed(halves.critical_count_change_pct),
            value2: signed(halves.known_repair_cost_change_pct),
          },
        )}
      </p>
    </div>
  );
});

export const CategoryLineMatrix = memo(function CategoryLineMatrix({
  data,
}: {
  data: DashboardData;
}) {
  const { t, number, sourceLabel } = useDashboardLanguage();
  const report = data.analysis;
  const cells = report.line_category;
  const costs = report.line_cost_comparison;

  const present = useMemo(
    () =>
      new Set(
        report.lines.filter((row) => row.product_count > 0).map((row) => row.production_line),
      ),
    [report.lines],
  );

  const cellMap = useMemo(() => {
    const map = new Map<string, NonNullable<typeof cells>[0]>();
    for (const row of cells ?? []) {
      if (row.cohort_product_count > 0) {
        map.set(`${row.production_line}::${row.category}`, row);
      }
    }
    return map;
  }, [cells]);

  const lineCostMap = useMemo(() => {
    const map = new Map<string, NonNullable<typeof costs>[0]>();
    for (const row of costs ?? []) {
      map.set(row.production_line, row);
    }
    return map;
  }, [costs]);

  const rows = (report.category_summary ?? []).filter((row) => row.product_count > 0);
  const columns = (data.options.production_lines ?? []).filter((line) => present.has(line));
  if (!cells?.length || rows.length < 2 || !columns.length) return null;

  const cell = (line: string, category: string) => cellMap.get(`${line}::${category}`);
  const lineCost = (line: string) => lineCostMap.get(line);
  const leadCost = costs?.[0];
  const controls = (report.shared_category_controls ?? []).filter(
    (row) => row.lines_compared > 0 && row.lines_above_rest_of_line > 0,
  );
  // Shading only changes cell emphasis; the server supplies every displayed value and order.
  const max = Math.max(1e-9, ...cells.map((row) => row.reference_intensity_per_1000 ?? 0));
  return (
    <section className="dashboard-panel mix-panel" aria-labelledby="mix-heading">
      <span className="section-kicker">{t('ui.mix_check')}</span>
      <h3 id="mix-heading">{t('ui.category_line_matrix_title')}</h3>
      <p className="panel-caption">{t('ui.category_line_matrix_caption')}</p>
      {(controls.length > 0 || leadCost?.ratio_to_other_lines != null) && (
        <ul className="mix-findings">
          {controls.map((control) => (
            <li key={control.category}>
              {t('ui.category_versus_rest_of_line', {
                value0: sourceLabel(control.category),
                value1: number(control.lines_above_rest_of_line),
                value2: number(control.lines_compared),
                value3: number(control.min_intensity_ratio_to_rest_of_line, 2),
                value4: number(control.max_intensity_ratio_to_rest_of_line, 2),
              })}
            </li>
          ))}
          {leadCost?.ratio_to_other_lines != null && (
            <li>
              {t('ui.line_cost_versus_other_lines', {
                value0: sourceLabel(leadCost.production_line),
                value1: number(leadCost.average_known_repair_cost, 2),
                value2: number(leadCost.other_lines_average_known_repair_cost, 2),
                value3: number(leadCost.ratio_to_other_lines, 2),
              })}
            </li>
          )}
        </ul>
      )}
      <div
        className="table-scroll"
        tabIndex={0}
        role="region"
        aria-label={t('ui.category_line_matrix_title')}
      >
        <table className="mix-matrix">
          <thead>
            <tr>
              <th scope="col">{t('ui.category')}</th>
              {columns.map((line) => (
                <th scope="col" key={line}>
                  {sourceLabel(line)}
                </th>
              ))}
              <th scope="col">{t('ui.all_lines_column')}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.category}>
                <th scope="row">
                  {sourceLabel(row.category)}
                  <small>
                    {t('ui.events_per_product', {
                      value0: number(row.eligible_events_per_cohort_product, 1),
                    })}
                  </small>
                </th>
                {columns.map((line) => {
                  const item = cell(line, row.category);
                  if (!item)
                    return (
                      <td key={line} className="mix-empty">
                        <span aria-label={t('ui.not_carried_on_line')}>—</span>
                      </td>
                    );
                  const strength = ((item.reference_intensity_per_1000 ?? 0) / max) * 100;
                  return (
                    <td
                      key={line}
                      style={{ '--mix-strength': `${strength.toFixed(1)}%` } as CSSProperties}
                    >
                      <strong>{number(item.reference_intensity_per_1000, 2)}</strong>
                      <span>
                        {t('ui.avg_cost_short', {
                          value0: number(item.average_known_repair_cost, 0),
                        })}
                      </span>
                      <small>
                        {number(item.event_count)} {t('ui.events')}
                      </small>
                    </td>
                  );
                })}
                <td className="mix-total">
                  <strong>{number(row.reference_intensity_per_1000, 2)}</strong>
                  <span>
                    {t('ui.avg_cost_short', { value0: number(row.average_known_repair_cost, 0) })}
                  </span>
                  <small>
                    {number(row.event_count)} {t('ui.events')}
                  </small>
                </td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <th scope="row">{t('ui.cost_per_known_cost_event')}</th>
              {columns.map((line) => {
                const item = lineCost(line);
                return (
                  <td key={line}>
                    <strong>{number(item?.average_known_repair_cost, 0)}</strong>
                    {item?.ratio_to_other_lines != null && (
                      <small>
                        {t('ui.ratio_to_other_lines', {
                          value0: number(item.ratio_to_other_lines, 2),
                        })}
                      </small>
                    )}
                  </td>
                );
              })}
              <td className="mix-total">
                <strong>{number(report.kpis.average_known_repair_cost, 0)}</strong>
              </td>
            </tr>
          </tfoot>
        </table>
      </div>
      <p className="panel-footnote">{t('ui.category_line_matrix_footnote')}</p>
    </section>
  );
});

export const CostDistribution = memo(function CostDistribution({
  distribution,
}: {
  distribution: NonNullable<DashboardData['analysis']['cost_distribution']>;
}) {
  const { t, number, percent } = useDashboardLanguage();
  return (
    <section className="cost-histogram" aria-label={t('ui.repair_cost_distribution')}>
      <h3>{t('ui.distribution_of_observed_valid_costs')}</h3>
      <p className="panel-caption">
        {number(distribution.cost_known_count)} {t('ui.events_with_valid_observed_costs')} ·{' '}
        {distribution.cost_coverage_pct == null
          ? t('ui.no_events_for_cost_coverage')
          : `${percent(distribution.cost_coverage_pct)} ${t('ui.cost_coverage_3')}`}
      </p>
      {distribution.histogram.map((bin) => (
        <div className="histogram-row" key={bin.lower}>
          <span>
            {number(bin.lower)}
            {bin.upper == null ? '+' : ` – <${number(bin.upper)}`}
          </span>
          <div
            role="img"
            aria-label={`${number(bin.count)} ${t('ui.events')} · ${percent(bin.share_of_known_cost_events_pct)}`}
          >
            <span style={{ width: `${bin.share_of_known_cost_events_pct || 0}%` }} />
          </div>
          <strong>{number(bin.count)}</strong>
        </div>
      ))}
      <dl className="cost-quantiles">
        <div>
          <dt>{t('ui.median')}</dt>
          <dd>{number(distribution.quantiles.median, 2)}</dd>
        </div>
        <div>
          <dt>{t('ui.25th_percentile')}</dt>
          <dd>{number(distribution.quantiles.p25, 2)}</dd>
        </div>
        <div>
          <dt>{t('ui.75th_percentile')}</dt>
          <dd>{number(distribution.quantiles.p75, 2)}</dd>
        </div>
        <div>
          <dt>{t('ui.95th_percentile')}</dt>
          <dd>{number(distribution.quantiles.p95, 2)}</dd>
        </div>
      </dl>
      <p className="panel-footnote">
        {t('ui.local_currency_counts_cover_events_with_valid_cost_only_missing_o')}
      </p>
    </section>
  );
});

export const MonthlyChart = memo(function MonthlyChart({
  rows,
}: {
  rows: DashboardData['analysis']['monthly'];
}) {
  const { t, tr, number, month, locale } = useDashboardLanguage();

  const maximum = Math.max(1, ...rows.map((item) => item.event_count));
  return (
    <div className="monthly-chart">
      <div className="chart-axis-label">{tr('ui.logged_events')}</div>
      <div
        className="monthly-bars"
        role="img"
        aria-label={rows
          .map(
            (row) =>
              `${month(row.month)}: ${number(row.event_count)} ${tr('ui.logged_events_2')}, ${number(row.critical_count)} ${tr('ui.critical')}`,
          )
          .join('; ')}
      >
        {rows.map((row) => {
          const height = (row.event_count / maximum) * 100;
          return (
            <div className="month-column" key={row.month}>
              <div className="month-bar-track">
                <span
                  className="month-bar"
                  style={{ height: `${height}%` }}
                  title={`${month(row.month)}: ${number(row.event_count)} ${tr('ui.logged_events_2')}, ${number(row.critical_count)} ${tr('ui.critical')}`}
                >
                  <span
                    style={{
                      height: `${row.critical_share_pct ?? 0}%`,
                    }}
                  />
                </span>
                <strong className="month-value" style={{ bottom: `${height}%` }}>
                  {number(row.event_count)}
                </strong>
              </div>
              <span className="month-label">
                <span className="month-long">{month(row.month)}</span>
                <span className="month-short">{shortMonth(row.month, locale)}</span>
              </span>
              <small className="month-critical">
                {number(row.critical_count)}
                <span> {t('ui.critical_2')}</span>
              </small>
            </div>
          );
        })}
      </div>
      <div className="chart-legend">
        <span>
          <i className="legend-swatch events" />
          {tr('ui.logged_events')}
        </span>
        <span>
          <i className="legend-swatch critical" />
          {tr('ui.critical_events_within_total')}
        </span>
      </div>
    </div>
  );
});

export const MonthlyBreakdownTable = memo(function MonthlyBreakdownTable({
  rows,
}: {
  rows: DashboardData['analysis']['monthly'];
}) {
  const { t, tr, number, percent, month } = useDashboardLanguage();
  if (rows.length <= 1) return null;
  return (
    <details className="nested-details">
      <summary>{t('ui.monthly_table_summary')}</summary>
      <div
        className="table-scroll"
        tabIndex={0}
        role="region"
        aria-label={t('ui.monthly_table_summary')}
      >
        <table>
          <thead>
            <tr>
              <th scope="col">{t('ui.month_col')}</th>
              <th scope="col">{tr('ui.logged_events')}</th>
              <th scope="col">{t('ui.critical_share_col')}</th>
              <th scope="col">{t('ui.per_1_000')}</th>
              <th scope="col">{t('ui.known_cost')}</th>
              <th scope="col">{t('ui.avg_cost_col')}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.month}>
                <th scope="row">{month(row.month)}</th>
                <td>{number(row.event_count)}</td>
                <td>
                  {number(row.critical_count)} ({percent(row.critical_share_pct)})
                </td>
                <td>{number(row.reference_intensity_per_1000, 2)}</td>
                <td>
                  {row.cost_available ? number(row.known_repair_cost, 2) : tr('ui.unavailable')}
                </td>
                <td>{number(row.average_known_repair_cost, 2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
});

export const TrendDrivers = memo(function TrendDrivers({ data }: { data: DashboardData }) {
  const { t, number, percent, month, sourceLabel } = useDashboardLanguage();
  const decomp = data.analysis.decomposition;
  const byType = decomp?.defect_type;
  const byLine = decomp?.production_line;
  if (!byType || !byLine || data.analysis.monthly.length <= 1) return null;
  const startMonth = byType.baseline.scope.start_month;
  const endMonth = byType.comparison.scope.end_month;
  if (startMonth === endMonth) return null;
  const netChange = byType.event_count_change;
  const signedInt = (n: number) => `${n > 0 ? '+' : ''}${number(n)}`;
  const typeRows = byType.rows.filter(
    (r) => r.name !== 'Unknown' || r.baseline.event_count > 0 || r.comparison.event_count > 0,
  );
  const lineRows = byLine.rows.filter((r) => r.name !== 'Unknown');
  const maxTypeAbs = Math.max(1, ...typeRows.map((r) => Math.abs(r.event_count_change)));
  const maxLineAbs = Math.max(1, ...lineRows.map((r) => Math.abs(r.event_count_change)));
  return (
    <section
      className="dashboard-panel trend-drivers-panel"
      aria-labelledby="trend-drivers-heading"
    >
      <span className="section-kicker">{t('ui.trend_drivers_kicker')}</span>
      <h3 id="trend-drivers-heading">
        {t('ui.trend_drivers_title', {
          value0: month(startMonth),
          value1: month(endMonth),
          value2: signedInt(netChange),
        })}
      </h3>
      <p className="panel-caption">{t('ui.trend_drivers_caption')}</p>
      <div className="dashboard-grid equal trend-drivers-grid">
        <div className="driver-column">
          <h4>{t('ui.trend_drivers_by_type')}</h4>
          <div className="driver-rows">
            {typeRows.map((row) => (
              <div className="driver-row" key={row.name}>
                <div className="driver-row-head">
                  <strong>{sourceLabel(row.name)}</strong>
                  <span className={row.event_count_change > 0 ? 'driver-delta up' : 'driver-delta'}>
                    {number(row.baseline.event_count)} → {number(row.comparison.event_count)} (
                    {signedInt(row.event_count_change)})
                  </span>
                </div>
                <div className="ranking-track">
                  <span
                    style={{
                      width: `${(Math.abs(row.event_count_change) / maxTypeAbs) * 100}%`,
                    }}
                  />
                </div>
                {row.contribution_pct_of_event_change != null && (
                  <small>
                    {t('ui.trend_drivers_share', {
                      value0: percent(row.contribution_pct_of_event_change),
                    })}
                  </small>
                )}
              </div>
            ))}
          </div>
        </div>
        <div className="driver-column">
          <h4>{t('ui.trend_drivers_by_line')}</h4>
          <div className="driver-rows">
            {lineRows.map((row) => (
              <div className="driver-row" key={row.name}>
                <div className="driver-row-head">
                  <strong>{sourceLabel(row.name)}</strong>
                  <span className={row.event_count_change > 0 ? 'driver-delta up' : 'driver-delta'}>
                    {number(row.baseline.event_count)} → {number(row.comparison.event_count)} (
                    {signedInt(row.event_count_change)})
                  </span>
                </div>
                <div className="ranking-track">
                  <span
                    style={{
                      width: `${(Math.abs(row.event_count_change) / maxLineAbs) * 100}%`,
                    }}
                  />
                </div>
                {row.contribution_pct_of_event_change != null && (
                  <small>
                    {t('ui.trend_drivers_share', {
                      value0: percent(row.contribution_pct_of_event_change),
                    })}
                  </small>
                )}
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
});

export const LocationMatrix = memo(function LocationMatrix({ data }: { data: DashboardData }) {
  const { t, tr, prose, number, percent, sourceLabel } = useDashboardLanguage();
  const matrix = data.analysis.location_severity;
  const { location_totals, cells } = matrix;

  const totalMap = useMemo(() => {
    const map = new Map<string, NonNullable<typeof location_totals>[0]>();
    for (const item of location_totals ?? []) {
      map.set(item.location, item);
    }
    return map;
  }, [location_totals]);

  const cellMap = useMemo(() => {
    const map = new Map<string, (typeof cells)[0]>();
    for (const item of cells) {
      map.set(`${item.location}::${item.severity}`, item);
    }
    return map;
  }, [cells]);

  return (
    <>
      <div className="location-rows">
        {matrix.locations.map((location) => {
          const total = totalMap.get(location);
          return (
            <div className="location-row" key={location}>
              <div className="location-row-label">
                <strong>{sourceLabel(location)}</strong>
                {total && (
                  <span>
                    {number(total.event_count)} {t('ui.events')}
                  </span>
                )}
              </div>
              <div className="location-bar-group">
                <div
                  className="severity-stack"
                  role="img"
                  aria-label={matrix.severities
                    .map((severity) => {
                      const cell = cellMap.get(`${location}::${severity}`);
                      return `${sourceLabel(severity)}: ${number(cell?.count)}, ${percent(cell?.row_pct)}`;
                    })
                    .join('; ')}
                >
                  {matrix.severities.map((severity) => {
                    const cell = cellMap.get(`${location}::${severity}`);
                    return (
                      <span
                        key={severity}
                        className={`severity-segment ${severity.toLowerCase()}`}
                        style={{ width: `${cell?.row_pct ?? 0}%` }}
                        title={`${sourceLabel(severity)}: ${number(cell?.count)} (${percent(cell?.row_pct)})`}
                      />
                    );
                  })}
                </div>
                <div className="location-severity-chips">
                  {matrix.severities.map((severity) => {
                    const cell = cellMap.get(`${location}::${severity}`);
                    return (
                      <span key={severity} className={`severity-chip ${severity.toLowerCase()}`}>
                        <i className={`legend-swatch ${severity.toLowerCase()}`} />
                        {sourceLabel(severity)}: <strong>{number(cell?.count)}</strong> (
                        {percent(cell?.row_pct)})
                      </span>
                    );
                  })}
                </div>
              </div>
              {total && (
                <p className="location-critical">
                  <span className="critical-dot" />
                  {number(total.critical_count)} {t('ui.critical_2')}
                  <strong>{percent(total.critical_share_pct)}</strong>
                </p>
              )}
            </div>
          );
        })}
      </div>
      {!matrix.locations.length && (
        <p className="empty-chart">{tr('ui.no_location_records_in_this_view')}</p>
      )}
      <div className="chart-legend severity-legend">
        {matrix.severities.map((severity) => (
          <span key={severity}>
            <i className={`legend-swatch ${severity.toLowerCase()}`} />
            {sourceLabel(severity)}
          </span>
        ))}
      </div>
      <details className="nested-details">
        <summary>{t('ui.counts_relationship')}</summary>
        <div
          className="table-scroll"
          tabIndex={0}
          role="region"
          aria-label={tr('ui.logged_events_by_defect_location_and_severity')}
        >
          <table className="severity-matrix">
            <thead>
              <tr>
                <th scope="col">{tr('ui.location')}</th>
                {matrix.severities.map((level) => (
                  <th scope="col" key={level}>
                    {sourceLabel(level)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {matrix.locations.map((location) => (
                <tr key={location}>
                  <th scope="row">{sourceLabel(location)}</th>
                  {matrix.severities.map((level) => (
                    <td key={level}>{number(cellMap.get(`${location}::${level}`)?.count)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p>{prose(matrix.test.interpretation)}</p>
        {matrix.test.available && (
          <p className="association-values">
            {tr('ui.cram_r_s_v')} {number(matrix.test.cramers_v, 3)} {tr('ui.p_value')}{' '}
            {matrix.test.p_value == null
              ? tr('ui.unavailable')
              : matrix.test.p_value < 0.0001
                ? '<' + number(0.0001, 4)
                : number(matrix.test.p_value, 4)}
            <br />
            {matrix.test.reliable ? tr('ui.meets_expected_cell_checks') : tr('ui.unreliable')}
          </p>
        )}
      </details>
    </>
  );
});

export const InspectionMethodPanel = memo(function InspectionMethodPanel({
  data,
}: {
  data: DashboardData;
}) {
  const { t, tr, number, percent, sourceLabel } = useDashboardLanguage();
  const rows = data.analysis.inspection_methods?.rows ?? [];
  if (!rows.length) return null;
  const max = Math.max(1, ...rows.map((r) => r.event_count));
  return (
    <section className="dashboard-panel inspection-panel" aria-labelledby="inspection-heading">
      <span className="section-kicker">{t('ui.inspection_kicker')}</span>
      <h3 id="inspection-heading">{t('ui.inspection_title')}</h3>
      <p className="panel-caption">{t('ui.inspection_caption')}</p>
      <div className="inspection-grid">
        {rows.map((row) => (
          <div className="inspection-card" key={row.inspection_method}>
            <div className="ranking-label">
              <strong>{sourceLabel(row.inspection_method)}</strong>
              <span>
                {number(row.event_count)} {t('ui.events')}
              </span>
            </div>
            <div className="ranking-track">
              <span style={{ width: `${(row.event_count / max) * 100}%` }} />
            </div>
            <small>
              {t('ui.inspection_share_detail', {
                value0: percent(row.event_share_pct),
                value1: number(row.critical_count),
                value2: percent(row.critical_share_pct),
                value3: row.cost_available
                  ? number(row.average_known_repair_cost, 2)
                  : tr('ui.unavailable'),
              })}
            </small>
          </div>
        ))}
      </div>
    </section>
  );
});

export const LineRanking = memo(function LineRanking({
  rows,
}: {
  rows: (Metrics & { production_line: string; product_count: number })[];
}) {
  const { t, tr, number, sourceLabel } = useDashboardLanguage();
  // Scale only changes chart geometry; the server supplies metrics and order.
  const max = Math.max(1, ...rows.map((row) => row.reference_intensity_per_1000 ?? 0));
  return (
    <div className="line-rankings">
      {rows.map((row, index) =>
        row.reference_intensity_per_1000 == null ? (
          <div className="line-row unranked" key={row.production_line}>
            <span className="rank-index" aria-hidden="true">
              –
            </span>
            <div>
              <div className="ranking-label">
                <strong>{sourceLabel(row.production_line)}</strong>
              </div>
              <small>{t('ui.dashboard_line_unlinked', { value0: number(row.event_count) })}</small>
            </div>
          </div>
        ) : (
          <div className="line-row" key={row.production_line}>
            <span className="rank-index">{String(index + 1).padStart(2, '0')}</span>
            <div>
              <div className="ranking-label">
                <strong>{sourceLabel(row.production_line)}</strong>
                <span>{number(row.reference_intensity_per_1000, 2)}</span>
              </div>
              <div className="ranking-track">
                <span style={{ width: `${(row.reference_intensity_per_1000 / max) * 100}%` }} />
              </div>
              <small>
                {number(row.linked_event_count)} {t('ui.linked_events')} /{' '}
                {number(row.reference_output)} {t('ui.reference_units')}
                {row.product_count > 0 && row.cost_available
                  ? ` · ${t('ui.line_products_meta', {
                      value0: number(row.product_count),
                      value1: number(row.average_known_repair_cost, 2),
                    })}`
                  : ''}
              </small>
              <small className="line-critical">
                {number(row.critical_count)} {t('ui.critical_events_2')}
              </small>
            </div>
          </div>
        ),
      )}
      {!rows.length && <p className="empty-chart">{tr('ui.no_eligible_lines_in_this_cohort')}</p>}
    </div>
  );
});

export const ProductRanking = memo(function ProductRanking({ data }: { data: DashboardData }) {
  const { t, tr, number, percent, sourceLabel } = useDashboardLanguage();
  const [ranking, setRanking] = useState<'intensity' | 'events'>('intensity');
  const [allProducts, setAllProducts] = useState(false);
  const products = data.analysis.products;
  const rows =
    ranking === 'intensity'
      ? (allProducts ? products.intensity_all : products.intensity_top) || []
      : allProducts
        ? products.all
        : products.top;
  return (
    <>
      <div className="ranking-controls" role="group" aria-label={t('ui.rank_products_by')}>
        <button
          type="button"
          aria-pressed={ranking === 'intensity'}
          onClick={() => setRanking('intensity')}
        >
          {t('ui.reference_intensity')}
        </button>
        <button
          type="button"
          aria-pressed={ranking === 'events'}
          onClick={() => setRanking('events')}
        >
          {t('ui.event_count')}
        </button>
      </div>
      <p className="panel-caption">
        {ranking === 'intensity'
          ? t('ui.highest_linked_events_per_1_000_reference_output_units')
          : t('ui.highest_number_of_logged_events_in_the_selected_view')}
      </p>
      <div
        className={`table-scroll product-scroll${allProducts ? ' all-products' : ''}`}
        tabIndex={0}
        role="region"
        aria-label={t('ui.product_ranking')}
      >
        <table className="product-table">
          <caption className="sr-only">
            {ranking === 'intensity'
              ? t('ui.products_ranked_by_reference_output_intensity')
              : tr('ui.top_products_ranked_by_logged_event_count')}
          </caption>
          <thead>
            <tr>
              <th scope="col">{tr('ui.product_2')}</th>
              <th scope="col">
                {ranking === 'intensity' ? t('ui.linked_events_2') : tr('ui.events_2')}
              </th>
              <th scope="col">
                {ranking === 'intensity' ? t('ui.reference_units_2') : tr('ui.critical')}
              </th>
              <th scope="col">
                {ranking === 'intensity' ? t('ui.per_1_000') : tr('ui.cumulative')}
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.product_id}>
                <td>
                  <strong>{sourceLabel(row.product_name)}</strong>
                  <small>
                    #{row.product_id}
                    {row.category ? ` · ${sourceLabel(row.category)}` : ''} ·{' '}
                    {sourceLabel(row.production_line)}
                  </small>
                </td>
                <td>
                  {number(ranking === 'intensity' ? row.linked_event_count : row.event_count)}
                </td>
                <td>
                  {ranking === 'intensity'
                    ? number(row.reference_output)
                    : number(row.critical_count)}
                </td>
                <td className="ranking-value">
                  {ranking === 'intensity'
                    ? number(row.reference_intensity_per_1000, 2)
                    : percent('cumulative_share_pct' in row ? row.cumulative_share_pct : null)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!rows.length && (
        <p className="empty-chart">{t('ui.no_eligible_products_for_this_ranking')}</p>
      )}
      <button
        type="button"
        className="text-button product-expand"
        onClick={() => setAllProducts((value) => !value)}
      >
        {allProducts ? t('ui.show_leading_products') : t('ui.show_all_eligible_products')}
        <ChevronDown size={14} className={allProducts ? 'chevron-open' : ''} aria-hidden="true" />
      </button>
      <p className="panel-footnote">
        {ranking === 'intensity'
          ? t('ui.static_monthly_output_selected_months_event_filters_change_the_ev')
          : products.pareto_product_count_80pct == null
            ? tr('ui.no_positive_linked_event_burden_for_a_pareto_threshold')
            : `${number(products.pareto_product_count_80pct)} ${tr('ui.master_products_reach_80_of_linked_logged_events_in_this_scope')}`}
      </p>
    </>
  );
});

export const AnomalyChart = memo(function AnomalyChart({ data }: { data: DashboardData }) {
  const { t, tr, number, date, month, locale } = useDashboardLanguage();
  const values = data.analysis.anomalies;
  const days = values.daily;
  const limit = values.enabled ? values.upper_reference_limit : null;
  // Geometry only: the server supplies the daily counts, the limit and the flagged days.
  // The plot stretches to its box (0-100 units on both axes); labels and flags are HTML
  // overlays placed by percentage, so their text size stays fixed at every screen width.
  // A whole-count plot ceiling keeps fractional reference limits from duplicating count ticks.
  const maximum = Math.ceil(Math.max(1, limit ?? 0, ...days.map((day) => day.count)));
  const x = (index: number) => (index / Math.max(1, days.length - 1)) * 100;
  const y = (count: number) => 100 - (count / maximum) * 100;
  const points = days.map((day, index) => `${x(index)},${y(day.count)}`).join(' ');
  const middle = Math.round(maximum / 2);
  const ticks = [0, ...(middle > 0 && middle < maximum ? [middle] : []), maximum];
  const months = days.flatMap((day, index) =>
    day.date.endsWith('-01') ? [{ index, value: day.date.slice(0, 7) }] : [],
  );
  return (
    <div className="anomaly-chart">
      <div className="anomaly-plot" role="img" aria-label={tr('ui.daily_logged_events')}>
        <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
          {ticks.map((tick) => (
            <line key={tick} className="chart-grid" x1="0" x2="100" y1={y(tick)} y2={y(tick)} />
          ))}
          {days.length > 1 && <polygon className="chart-area" points={`0,100 ${points} 100,100`} />}
          {days.length > 0 && <polyline className="chart-series" points={points} />}
          {limit != null && (
            <line className="chart-limit" x1="0" x2="100" y1={y(limit)} y2={y(limit)} />
          )}
        </svg>
        {ticks.map((tick) => (
          <span key={tick} className="chart-axis" style={{ top: `${y(tick)}%` }}>
            {number(tick)}
          </span>
        ))}
        {limit != null && (
          <span className="chart-limit-label" style={{ top: `${y(limit)}%` }}>
            {t('ui.dashboard_limit_label', { value0: number(limit, 2) })}
          </span>
        )}
        {days.map((day, index) =>
          day.candidate ? (
            <span
              key={day.date}
              className="chart-flag"
              style={{ left: `${x(index)}%`, top: `${y(day.count)}%` }}
              title={`${date(day.date)}: ${number(day.count)} ${tr('ui.logged_events_2')}`}
            />
          ) : null,
        )}
      </div>
      <div className="chart-months" aria-hidden="true">
        {months.map((item) => (
          <span key={item.index} style={{ left: `${x(item.index)}%` }}>
            <span className="month-long">{month(item.value)}</span>
            <span className="month-short">{shortMonth(item.value, locale)}</span>
          </span>
        ))}
      </div>
      <div className="chart-legend">
        <span>
          <i className="legend-swatch events" />
          {tr('ui.daily_logged_events')}
        </span>
        {limit != null && (
          <>
            <span>
              <i className="legend-swatch reference" />
              {tr('ui.january_reference')}
            </span>
            <span>
              <i className="legend-swatch critical" />
              {tr('ui.candidate')}
            </span>
          </>
        )}
      </div>
    </div>
  );
});
