import type { DashboardData, Filters } from './dashboard-types';
import { useI18n } from './i18n';

type Context = DashboardData['context'];

export function questionScope({
  dashboardContextId,
  capturedContextId,
  dashboardContext,
  loadedContext,
  failedContextId,
}: {
  dashboardContextId?: string;
  capturedContextId?: string;
  dashboardContext?: Context;
  loadedContext?: Context;
  failedContextId?: string;
}) {
  const id = capturedContextId || dashboardContextId;
  const captured = Boolean(capturedContextId && capturedContextId !== dashboardContextId);
  const context =
    id && loadedContext?.context_id === id
      ? loadedContext
      : id && dashboardContext?.context_id === id
        ? dashboardContext
        : undefined;
  const unavailable = Boolean(captured && !context && id === failedContextId);
  return { id, context, captured, unavailable, loading: captured && !context && !unavailable };
}

const fields: { key: keyof Filters; label: string }[] = [
  { key: 'production_line', label: 'ui.filter_production_line' },
  { key: 'category', label: 'ui.filter_category' },
  { key: 'product_id', label: 'ui.filter_product' },
  { key: 'severity', label: 'ui.filter_severity' },
  { key: 'defect_type', label: 'ui.filter_defect_type' },
  { key: 'defect_location', label: 'ui.filter_defect_location' },
  { key: 'inspection_method', label: 'ui.filter_inspection_method' },
  { key: 'scenario', label: 'ui.agent_scope_scenario' },
];

export default function AgentScope({ filters }: { filters: Filters }) {
  const { t, month, label } = useI18n();
  const selected = fields.filter(({ key }) => filters[key]);
  return (
    <>
      <strong className="agent-scope-period">
        {month(filters.start_month)} {t('ui.to')} {month(filters.end_month)}
      </strong>
      {selected.length ? (
        <ul className="agent-scope-filters" aria-label={t('ui.selected_scope')}>
          {selected.map(({ key, label: title }) => (
            <li key={key} className={key === 'scenario' ? 'is-scenario' : undefined}>
              <span>{t(title)}:</span>{' '}
              {key === 'product_id' ? `#${filters[key]}` : label(filters[key])}
            </li>
          ))}
        </ul>
      ) : (
        <p>{t('ui.agent_scope_all')}</p>
      )}
    </>
  );
}
