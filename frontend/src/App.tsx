import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { LayoutDashboard, Search } from 'lucide-react';
import Dashboard from './Dashboard';
import { getConfig, getDashboard, getDashboardContext, refreshDashboard } from './api';
import { initialFilters } from './dashboard-types';
import type { DashboardData, Filters } from './dashboard-types';
import { useI18n } from './i18n';
import { commandMatchesContext, isVisibleWorkspacePanel } from './workspace-state';
import type { UiCommand, WorkspacePanel } from './workspace-state';
import { applicationUrl } from './app-url';

type Tab = 'dashboard' | 'agent';
const AgentWorkspace = lazy(() => import('./AgentWorkspace'));
export default function App() {
  const { locale, setLocale, t, scope } = useI18n();
  const [tab, setTab] = useState<Tab>('dashboard');
  const [agentVisited, setAgentVisited] = useState(false);
  const [canAnalyze, setCanAnalyze] = useState(false);
  const [data, setData] = useState<DashboardData>();
  const [filters, setFilters] = useState<Filters>(initialFilters);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState('');
  const [sourceChecked, setSourceChecked] = useState(false);
  const [revision, setRevision] = useState(0);
  const [prefill, setPrefill] = useState<{
    id: number;
    text: string;
    contextId: string;
    contextSummary: string;
  }>();
  const requestVersion = useRef(0);
  const scopePending = useRef(false);
  const currentContextId = useRef<string | undefined>(undefined);
  currentContextId.current = data?.context.context_id;
  const adoptedFilters = useRef<string | undefined>(undefined);
  const [panelRequest, setPanelRequest] = useState<{ id: number; panel: WorkspacePanel }>();
  const scroll = useRef<Record<Tab, number>>({ dashboard: 0, agent: 0 });
  const tabs = useRef<(HTMLButtonElement | null)[]>([]);
  const applied = data?.context.filters;
  const contextSummary = applied ? scope(applied) : undefined;
  useEffect(() => {
    let disposed = false;
    void getConfig()
      .then((configuration) => {
        if (!disposed) setCanAnalyze(configuration.ai_available === true);
      })
      .catch(() => {
        // Dashboard remains usable when optional Agent capability cannot be confirmed.
        if (!disposed) setCanAnalyze(false);
      });
    return () => {
      disposed = true;
    };
  }, []);
  useEffect(() => {
    if (adoptedFilters.current === JSON.stringify(filters)) {
      adoptedFilters.current = undefined;
      return;
    }
    const abort = new AbortController();
    const version = ++requestVersion.current;
    setLoading(true);
    setError('');
    getDashboard(filters, abort.signal)
      .then((result) => {
        if (version === requestVersion.current) {
          setData(result);
          scopePending.current = false;
        }
      })
      .catch((failure: unknown) => {
        if (!abort.signal.aborted && version === requestVersion.current) {
          // A failed selection leaves the previous applied context available.
          scopePending.current = false;
          setError(failure instanceof Error ? failure.message : String(failure));
        }
      })
      .finally(() => {
        if (version === requestVersion.current) setLoading(false);
      });
    return () => abort.abort();
  }, [filters, revision]);
  const selectTab = useCallback(
    (next: Tab) => {
      scroll.current[tab] = window.scrollY;
      if (next === 'agent') setAgentVisited(true);
      setTab(next);
      requestAnimationFrame(() =>
        window.scrollTo({ top: scroll.current[next], behavior: 'instant' }),
      );
    },
    [tab],
  );
  const applyManualFilters = useCallback((next: Filters) => {
    scopePending.current = true;
    requestVersion.current += 1;
    setFilters(next);
  }, []);
  function keyTabs(event: KeyboardEvent, index: number) {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? 1 : 1 - index;
    tabs.current[next]?.focus();
    selectTab(next === 0 ? 'dashboard' : 'agent');
  }
  const refresh = useCallback(async () => {
    setRefreshing(true);
    setError('');
    setSourceChecked(false);
    try {
      const result = await refreshDashboard(filters);
      setSourceChecked(!result.status?.stale);
      setRevision((value) => value + 1);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setRefreshing(false);
    }
  }, [filters]);
  const analyze = useCallback(
    (priority?: number) => {
      if (!data || loading || !canAnalyze) return;
      const selected = data.analysis.recommendations.find((item) => item.priority === priority);
      setPrefill({
        id: Date.now(),
        contextId: data.context.context_id,
        contextSummary: contextSummary || '',
        text: selected
          ? t('ui.investigate_this_priority_in_the_selected_scope_value_verify_the_', {
              value0: selected.title,
            })
          : t('ui.analyze_this_dashboard_view_value_explain_the_main_trends_where_p', {
              value0: contextSummary,
            }),
      });
      selectTab('agent');
    },
    [data, loading, canAnalyze, contextSummary, t, selectTab],
  );
  const applyUiCommand = useCallback(
    async (command: UiCommand): Promise<boolean> => {
      if (
        !isVisibleWorkspacePanel(command.panel) ||
        scopePending.current ||
        !commandMatchesContext(command, currentContextId.current)
      )
        return false;
      const version = requestVersion.current;
      if (command.context_id !== currentContextId.current) {
        const result = await getDashboardContext(command.context_id);
        if (
          scopePending.current ||
          !commandMatchesContext(command, currentContextId.current) ||
          version !== requestVersion.current
        )
          return false;
        requestVersion.current += 1;
        currentContextId.current = result.context.context_id;
        adoptedFilters.current = JSON.stringify(result.context.filters);
        setData(result);
        setFilters(result.context.filters);
        setLoading(false);
        setError('');
      }
      setPanelRequest({ id: Date.now(), panel: command.panel });
      selectTab('dashboard');
      return true;
    },
    [selectTab],
  );
  const openDashboard = useCallback(() => {
    selectTab('dashboard');
    requestAnimationFrame(() =>
      document.getElementById('dashboard-content')?.focus({ preventScroll: true }),
    );
  }, [selectTab]);
  return (
    <div className="app-shell">
      <a className="skip-link" href={tab === 'dashboard' ? '#dashboard-content' : '#agent-panel'}>
        {t('ui.skip_to_content')}
      </a>
      <header className="app-header">
        <a
          className="brand"
          href={applicationUrl('/')}
          aria-label={t('ui.manufacturing_quality_home')}
        >
          <span className="brand-mark">
            <img
              className="xnhan-logo-avatar"
              src={applicationUrl('/assets/portrait-icon-20d683e7-192.png')}
              srcSet={`${applicationUrl('/assets/portrait-icon-20d683e7-32.png')} 32w, ${applicationUrl('/assets/portrait-icon-20d683e7-192.png')} 192w`}
              sizes="32px"
              width="192"
              height="192"
              alt=""
              decoding="async"
            />
          </span>
          <span>{t('ui.production_quality')}</span>
        </a>
        <div className="app-tabs" role="tablist" aria-label={t('ui.workspace_views')}>
          {(['dashboard', 'agent'] as const).map((value, index) => (
            <button
              key={value}
              ref={(element) => {
                tabs.current[index] = element;
              }}
              id={`${value}-tab`}
              role="tab"
              aria-selected={tab === value}
              aria-controls={`${value}-panel`}
              tabIndex={tab === value ? 0 : -1}
              onClick={() => selectTab(value)}
              onKeyDown={(event) => keyTabs(event, index)}
            >
              {index === 0 ? <LayoutDashboard size={15} /> : <Search size={15} />}{' '}
              {index === 0 ? t('ui.dashboard') : t('ui.agent')}
            </button>
          ))}
        </div>
        <div className="app-header-tools">
          <label className="language-picker">
            <span className="sr-only">{t('ui.language')}</span>
            <select
              value={locale}
              onChange={(event) => setLocale(event.target.value === 'vi' ? 'vi' : 'en')}
            >
              <option value="en" lang="en">
                English
              </option>
              <option value="vi" lang="vi">
                Tiếng Việt
              </option>
            </select>
          </label>
        </div>
      </header>
      <section
        id="dashboard-panel"
        role="tabpanel"
        aria-labelledby="dashboard-tab"
        hidden={tab !== 'dashboard'}
      >
        <Dashboard
          data={data}
          loading={loading}
          refreshing={refreshing}
          refreshFeedback={
            sourceChecked
              ? t('ui.source_checked_successfully_this_view_uses_the_latest_retained_so')
              : ''
          }
          error={error}
          filters={filters}
          onApply={applyManualFilters}
          onRefresh={refresh}
          onAnalyze={analyze}
          canAnalyze={canAnalyze}
          inspectionRequest={panelRequest}
        />
      </section>
      <section
        id="agent-panel"
        role="tabpanel"
        tabIndex={-1}
        aria-labelledby="agent-tab"
        hidden={tab !== 'agent'}
      >
        {agentVisited && (
          <Suspense
            fallback={
              <div className="agent-loading" role="status">
                {t('ui.opening_the_analyst')}
              </div>
            }
          >
            <AgentWorkspace
              visible={tab === 'agent'}
              contextId={data?.context.context_id}
              contextSummary={contextSummary}
              dashboardContext={data?.context}
              dashboardData={data}
              prefill={prefill}
              onUiCommand={applyUiCommand}
              onOpenDashboard={openDashboard}
            />
          </Suspense>
        )}
      </section>
    </div>
  );
}
