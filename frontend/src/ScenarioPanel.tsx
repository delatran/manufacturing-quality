import { useEffect, useState } from 'react';
import { ArrowRight, GitCompareArrows, RotateCcw } from 'lucide-react';
import { request } from './api';
import type { DashboardData, Filters, ScenarioComparison } from './dashboard-types';
import { useDashboardLanguage } from './dashboard-language';

export const scenarioNames = {
  '': 'ui.scenario_canonical',
  exclude_cost_10: 'ui.scenario_exclude_cost_10',
  ambiguous_dm: 'ui.scenario_ambiguous_dm',
  year_2024: 'ui.scenario_year_2024',
} as const;

export default function ScenarioPanel({
  data,
  disabled,
  onApply,
}: {
  data: DashboardData;
  disabled: boolean;
  onApply: (filters: Filters) => void;
}) {
  const { t, number, percent, prose, sourceLabel, download } = useDashboardLanguage();
  const selected = data.context.filters.scenario || '';
  const [draft, setDraft] = useState(selected);
  const [comparison, setComparison] = useState<ScenarioComparison>();
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    setDraft(selected);
  }, [selected, data.context.context_id]);
  useEffect(() => {
    setComparison(undefined);
    setError('');
    if (!selected) return;
    const abort = new AbortController();
    void request<ScenarioComparison>(
      `/api/dashboard/contexts/${encodeURIComponent(data.context.context_id)}/scenarios/${encodeURIComponent(selected)}`,
      { signal: abort.signal },
    )
      .then((value) => {
        if (!abort.signal.aborted) setComparison(value);
      })
      .catch((failure: unknown) => {
        if (!abort.signal.aborted)
          setError(failure instanceof Error ? failure.message : String(failure));
      });
    return () => abort.abort();
  }, [data.context.context_id, selected, retry]);
  const metrics = [
    { key: 'event_count', label: t('ui.defect_events'), format: (n: number | null) => number(n) },
    {
      key: 'known_repair_cost',
      label: t('ui.observed_repair_cost'),
      format: (n: number | null) => number(n, 2),
    },
    { key: 'cost_coverage_pct', label: t('ui.cost_coverage_4'), format: percent },
  ];
  return (
    <section className="scenario-panel dashboard-panel" aria-labelledby="scenario-heading">
      <div className="scenario-header">
        <div>
          <span className="section-kicker">{t('ui.test_the_assumption')}</span>
          <h2 id="scenario-heading">
            <GitCompareArrows size={20} aria-hidden="true" />
            {t('ui.would_the_priority_change')}
          </h2>
          <p>{t('ui.a_scenario_recalculates_this_scope_beside_the_canonical_policy_th')}</p>
        </div>
        <form
          className="scenario-controls"
          onSubmit={(event) => {
            event.preventDefault();
            onApply({ ...data.context.filters, scenario: draft });
          }}
        >
          <label>
            <span>{t('ui.scenario')}</span>
            <select
              value={draft}
              disabled={disabled}
              onChange={(event) => setDraft(event.target.value)}
            >
              {Object.entries(scenarioNames).map(([key, names]) => (
                <option key={key} value={key}>
                  {t(names)}
                </option>
              ))}
            </select>
          </label>
          <button
            className="secondary-button"
            type="submit"
            disabled={disabled || draft === selected}
          >
            {t('ui.apply_scenario')}
          </button>
          {draft !== selected && (
            <p className="filter-state pending" role="status">
              {t('ui.scenario_not_applied')}
            </p>
          )}
        </form>
      </div>
      {selected && (
        <div className="scenario-explanation">
          <p>
            {selected === 'exclude_cost_10'
              ? t('ui.this_changes_financial_availability_only_events_with_observed_cos')
              : selected === 'ambiguous_dm'
                ? t('ui.ambiguous_dates_use_day_month_some_events_may_move_outside_januar')
                : t('ui.this_assumes_the_source_year_2026_should_be_2024_it_is_a_sensitiv')}
          </p>
          <button
            type="button"
            className="text-button"
            disabled={disabled}
            onClick={() => onApply({ ...data.context.filters, scenario: '' })}
          >
            <RotateCcw size={14} />
            {t('ui.reset_to_canonical')}
          </button>
        </div>
      )}
      {selected && !comparison && !error && (
        <p role="status" className="panel-footnote">
          {t('ui.comparing_against_the_same_canonical_scope')}
        </p>
      )}
      {error && (
        <div role="alert" className="dashboard-alert">
          <p>
            {t('ui.the_scenario_is_applied_but_its_comparison_could_not_be_loaded')} {prose(error)}
          </p>
          <button
            type="button"
            className="text-button"
            onClick={() => setRetry((value) => value + 1)}
          >
            {t('ui.retry_comparison')}
          </button>
        </div>
      )}
      {comparison && (
        <>
          <div className="scenario-comparison" role="status">
            {metrics.map(({ key, label, format }) => {
              const metric = comparison.comparison.metric_deltas[key];
              return metric ? (
                <div key={key}>
                  <span>{label}</span>
                  <p>
                    <strong>{format(metric.canonical)}</strong>
                    <ArrowRight size={14} />
                    <strong>{format(metric.selected)}</strong>
                  </p>
                  <small>
                    {t('ui.change')}: {metric.delta != null && metric.delta > 0 ? '+' : ''}
                    {metric.delta == null
                      ? t('ui.unavailable')
                      : key === 'cost_coverage_pct'
                        ? `${number(metric.delta, 1)} ${t('ui.percentage_points')}`
                        : format(metric.delta)}
                  </small>
                </div>
              ) : null;
            })}
          </div>
          <p className="scenario-result">
            {comparison.canonical.analysis.recommendations.length === 0 &&
            comparison.selected.analysis.recommendations.length === 0
              ? t('ui.no_priorities_to_compare')
              : comparison.comparison.priority_order_changed
                ? t('ui.the_priority_order_changes_under_this_assumption_review_the_compa')
                : t('ui.the_priority_order_is_unchanged_under_this_assumption_data_limits')}
          </p>
          <details className="nested-details">
            <summary>{t('ui.ranking_changes_and_comparison_evidence')}</summary>
            {Object.entries(comparison.comparison.rankings).map(([key, ranking]) => (
              <div className="scenario-ranking" key={key}>
                <strong>
                  {key === 'lines_by_reference_intensity'
                    ? t('ui.lines_by_reference_intensity')
                    : key === 'defect_types_by_known_cost'
                      ? t('ui.defect_types_by_observed_cost')
                      : t('ui.products_by_observed_cost')}
                </strong>
                <p>
                  {ranking.canonical.map(sourceLabel).join(' → ')}
                  <br />
                  {ranking.selected.map(sourceLabel).join(' → ')}
                </p>
                <span>{ranking.changed ? t('ui.order_changed') : t('ui.order_unchanged')}</span>
              </div>
            ))}
            <div className="evidence-actions">
              <a href={download(comparison.canonical.links.export_zip)} className="text-button">
                {t('ui.canonical_evidence')}
              </a>
              <a href={download(comparison.selected.links.export_zip)} className="text-button">
                {t('ui.scenario_evidence')}
              </a>
            </div>
          </details>
        </>
      )}
    </section>
  );
}
