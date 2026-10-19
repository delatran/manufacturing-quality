import {
  ArrowRight,
  Check,
  ChevronDown,
  Coins,
  Gauge,
  ListFilter,
  TrendingDown,
  TrendingUp,
  TriangleAlert,
} from 'lucide-react';
import { useDashboardLanguage } from './dashboard-language';
import { InfoTip } from './DashboardLayout';
import type { Recommendation, IconComponent, Delta } from './dashboard-display-types';

export function PriorityCard({
  item,
  onEvidence,
  onAnalyze,
  onFocus,
  isFocused = false,
  canAnalyze = false,
}: {
  item: Recommendation;
  onEvidence: () => void;
  onAnalyze: () => void;
  onFocus?: () => void;
  isFocused?: boolean;
  canAnalyze?: boolean;
}) {
  const { t, prose, number, percent, sourceLabel } = useDashboardLanguage();
  const numeric = (key: string) => {
    const value = item.metrics?.[key];
    return typeof value === 'number' ? value : null;
  };
  const label = (key: string) => {
    const value = item.metrics?.[key];
    return typeof value === 'string' ? sourceLabel(value) : '';
  };
  let tag = '';
  let Icon: IconComponent = TriangleAlert;
  let title = prose(item.title);
  let headline = '';
  let headlineLabel = '';
  let detail = prose(item.evidence);
  if (item.kind === 'critical_location') {
    tag = t('ui.dashboard_tag_severity');
    title = t('ui.dashboard_p_critical_title', { value0: label('location') });
    headline = percent(numeric('critical_share_pct'));
    headlineLabel = t('ui.dashboard_p_critical_value');
    detail = t('ui.dashboard_p_critical_detail', {
      value0: number(numeric('critical_count')),
      value1: number(numeric('event_count')),
      value2: percent(numeric('other_locations_critical_share_pct')),
    });
  } else if (item.kind === 'category_intensity' || item.kind === 'line_intensity') {
    tag = t('ui.dashboard_tag_rate');
    Icon = Gauge;
    headline = number(numeric('reference_intensity_per_1000'), 2);
    headlineLabel = t('ui.dashboard_p_rate_value');
    if (item.kind === 'category_intensity') {
      title = t('ui.dashboard_p_category_title', { value0: label('category') });
      detail = numeric('lines_compared')
        ? t('ui.dashboard_p_category_detail', {
            value0: number(numeric('rest_of_scope_reference_intensity_per_1000'), 2),
            value1: number(numeric('lines_above_rest_of_line')),
            value2: number(numeric('lines_compared')),
          })
        : t('ui.dashboard_p_category_detail_simple', {
            value0: number(numeric('rest_of_scope_reference_intensity_per_1000'), 2),
          });
    } else {
      title = t('ui.dashboard_p_line_title', { value0: label('production_line') });
      detail = t('ui.dashboard_p_line_detail', { value0: number(numeric('event_count')) });
    }
  } else if (item.kind === 'line_repair_cost') {
    tag = t('ui.dashboard_tag_cost');
    Icon = Coins;
    title = t('ui.dashboard_p_cost_title', { value0: label('production_line') });
    headline = number(numeric('average_known_repair_cost'), 2);
    headlineLabel = t('ui.dashboard_p_cost_value');
    detail = t('ui.dashboard_p_cost_detail', {
      value0: number(numeric('other_lines_average_known_repair_cost'), 2),
      value1: number(numeric('ratio_to_other_lines'), 2),
      value2: percent(numeric('known_cost_share_pct')),
    });
  } else if (item.kind === 'defect_type_cost') {
    tag = t('ui.dashboard_tag_cost');
    Icon = Coins;
    title = t('ui.dashboard_p_type_title', { value0: label('defect_type') });
    headline = number(numeric('known_repair_cost'), 0);
    headlineLabel = t('ui.dashboard_p_type_value');
    detail = t('ui.dashboard_p_type_detail', { value0: percent(numeric('known_cost_share_pct')) });
  }
  return (
    <article className={`priority-card kind-${item.kind ?? 'other'}`}>
      <div className="priority-card-heading">
        <span className="priority-number">{item.priority}</span>
        {tag && (
          <span className="priority-tag">
            <Icon size={13} aria-hidden="true" />
            {tag}
          </span>
        )}
      </div>
      <h3>{title}</h3>
      {headline && (
        <p className="priority-headline">
          <strong>{headline}</strong>
          <span>{headlineLabel}</span>
        </p>
      )}
      <p className="priority-evidence">{detail}</p>
      <div className="priority-owner">
        <span>{t('ui.dashboard_owner')}</span>
        <strong>{prose(item.owner)}</strong>
      </div>
      <p className="priority-action">
        <strong>{t('ui.dashboard_action')}: </strong>
        {prose(item.action)}
      </p>
      {item.success_measure ? (
        <details className="priority-plan">
          <summary>
            {t('ui.dashboard_measurement_plan')}
            <ChevronDown size={14} aria-hidden="true" />
          </summary>
          <dl>
            <dt>{t('ui.dashboard_success')}</dt>
            <dd>{prose(item.success_measure)}</dd>
          </dl>
        </details>
      ) : null}
      <div className="priority-actions">
        <div className="priority-actions-main">
          <button type="button" className="secondary-button" onClick={onEvidence}>
            {t('ui.view_evidence')}
          </button>
          {item.filters && onFocus && (
            <button
              type="button"
              className={`secondary-button priority-filter-btn${isFocused ? ' active' : ''}`}
              aria-pressed={isFocused}
              onClick={onFocus}
            >
              {isFocused ? (
                <Check size={14} aria-hidden="true" />
              ) : (
                <ListFilter size={14} aria-hidden="true" />
              )}
              {isFocused ? t('ui.filtered_by_priority') : t('ui.filter_by_priority')}
            </button>
          )}
        </div>
        {canAnalyze && (
          <button type="button" className="text-button" onClick={onAnalyze}>
            {t('ui.dashboard_ask_agent')}
            <ArrowRight size={14} aria-hidden="true" />
          </button>
        )}
      </div>
    </article>
  );
}

export function Kpi({
  label,
  value,
  detail,
  tone = '',
  delta,
  help,
  spark,
  sparkLabel,
}: {
  label: string;
  value: string;
  detail: string;
  tone?: string;
  delta?: Delta;
  help?: string;
  spark?: number[];
  sparkLabel?: string;
}) {
  // Sparkline geometry only rescales backend monthly values between their own low and high.
  const low = spark?.length ? Math.min(...spark) : 0;
  const high = spark?.length ? Math.max(...spark) : 0;
  const sparkY = (value: number) => (high === low ? 16 : 29 - ((value - low) / (high - low)) * 26);
  const points =
    spark && spark.length > 1
      ? spark
          .map((value, index) => `${(index / (spark.length - 1)) * 100},${sparkY(value)}`)
          .join(' ')
      : '';
  const DeltaIcon =
    delta?.direction === 'up' ? TrendingUp : delta?.direction === 'down' ? TrendingDown : null;
  return (
    <article className={`kpi-card${tone ? ` ${tone}` : ''}`}>
      <div className="kpi-heading">
        <span className="kpi-label">{label}</span>
        {help && <InfoTip text={help} label={label} />}
      </div>
      <strong className="kpi-value">{value}</strong>
      {delta && (
        <span className={`kpi-delta ${delta.tone}`}>
          {DeltaIcon && <DeltaIcon size={14} aria-hidden="true" />}
          {delta.text}
        </span>
      )}
      <span className="kpi-detail">{detail}</span>
      {points && (
        <svg
          className="kpi-spark"
          viewBox="0 0 100 32"
          preserveAspectRatio="none"
          role="img"
          aria-label={sparkLabel || label}
        >
          <polyline points={points} />
        </svg>
      )}
    </article>
  );
}
