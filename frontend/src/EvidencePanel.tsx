import { useEffect, useRef } from 'react';
import { ArrowDownToLine, X } from 'lucide-react';
import type { DashboardData, Filters } from './dashboard-types';
import { useDashboardLanguage } from './dashboard-language';

export default function EvidencePanel({
  data,
  priority,
  onClose,
  onRecords,
  onFocus,
}: {
  data: DashboardData;
  priority?: number;
  onClose: () => void;
  onRecords: () => void;
  onFocus: (filters: Partial<Filters>) => void;
}) {
  const { t, tr, prose, number, percent, month, download } = useDashboardLanguage();
  const region = useRef<HTMLElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  useEffect(() => {
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    return () => {
      if (opener.current?.isConnected) opener.current.focus({ preventScroll: true });
    };
  }, []);
  useEffect(() => {
    region.current?.focus();
  }, [priority, data.context.context_id]);
  const recommendation = data.analysis.recommendations.find((item) => item.priority === priority);
  const kpis = data.analysis.kpis;
  return (
    <section
      className="evidence-panel dashboard-panel"
      id="scope-evidence"
      ref={region}
      tabIndex={-1}
      aria-labelledby="scope-evidence-heading"
    >
      <div className="evidence-heading">
        <div>
          <span className="section-kicker">{t('ui.check_the_evidence')}</span>
          <h2 id="scope-evidence-heading">
            {recommendation ? prose(recommendation.title) : t('ui.evidence_for_this_view')}
          </h2>
        </div>
        <button
          type="button"
          className="icon-button"
          onClick={onClose}
          aria-label={t('ui.close_evidence')}
        >
          <X size={18} />
        </button>
      </div>
      {recommendation && <p className="evidence-statement">{prose(recommendation.evidence)}</p>}
      <p className="panel-caption">
        {month(data.context.filters.start_month)} → {month(data.context.filters.end_month)}
      </p>
      <dl className="evidence-metrics">
        <div>
          <dt>{tr('ui.logged_defect_events')}</dt>
          <dd>{number(kpis.event_count)}</dd>
        </div>
        <div>
          <dt>{t('ui.critical_events')}</dt>
          <dd>{number(kpis.critical_count)}</dd>
        </div>
        <div>
          <dt>{tr('ui.cost_coverage')}</dt>
          <dd>{percent(kpis.cost_coverage_pct)}</dd>
        </div>
        <div>
          <dt>{t('ui.reference_output_denominator')}</dt>
          <dd>{number(kpis.reference_output)}</dd>
        </div>
      </dl>
      <div className="evidence-actions">
        {recommendation?.filters && (
          <button
            type="button"
            className="primary-button"
            onClick={() => onFocus(recommendation.filters!)}
          >
            {t('ui.apply_priority_focus')}
          </button>
        )}
        <button type="button" className="secondary-button" onClick={onRecords}>
          {t('ui.open_scoped_records')}
        </button>
        <a className="secondary-button" href={download(data.links.export_csv)}>
          <ArrowDownToLine size={15} />
          {t('ui.export_filtered_csv')}
        </a>
      </div>
    </section>
  );
}
