import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
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
import { commitUi, throwIfAborted, useWebMcpTools, waitForUi } from './webmcp';
import { buildWorkspaceTools } from './workspace-webmcp';
import type { AnalysisStage, DashboardScopeResult, WorkspaceSnapshot } from './workspace-webmcp';

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
  const [analysisStage, setAnalysisStage] = useState<AnalysisStage>();
  const [prefill, setPrefill] = useState<{
    id: number;
    text: string;
    contextId: string;
    contextSummary: string;
  }>();
  const requestVersion = useRef(0);
  const scopePending = useRef(false);
  const mounted = useRef(false);
  const dashboardAbort = useRef<AbortController | undefined>(undefined);
  const prefillSequence = useRef(0);
  const currentContextId = useRef<string | undefined>(undefined);
  const [panelRequest, setPanelRequest] = useState<{ id: number; panel: WorkspacePanel }>();
  const scroll = useRef<Record<Tab, number>>({ dashboard: 0, agent: 0 });
  const tabs = useRef<(HTMLButtonElement | null)[]>([]);
  const applied = data?.context.filters;
  const contextSummary = applied ? scope(applied) : undefined;
  const committed = useRef<WorkspaceSnapshot>({
    tab,
    locale,
    data,
    loading,
    refreshing,
    error,
    canAnalyze,
    sourceChecked,
    analysisStage,
  });
  useLayoutEffect(() => {
    currentContextId.current = data?.context.context_id;
    committed.current = {
      tab,
      locale,
      data,
      loading,
      refreshing,
      error,
      canAnalyze,
      sourceChecked,
      analysisStage,
    };
  }, [tab, locale, data, loading, refreshing, error, canAnalyze, sourceChecked, analysisStage]);
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
  const loadDashboard = useCallback(
    async (
      next: Filters,
      refreshSource = false,
      signal?: AbortSignal,
    ): Promise<DashboardScopeResult> => {
      throwIfAborted(signal);
      dashboardAbort.current?.abort();
      const abort = new AbortController();
      dashboardAbort.current = abort;
      const cancel = () => abort.abort();
      signal?.addEventListener('abort', cancel, { once: true });
      const version = ++requestVersion.current;
      scopePending.current = true;
      setFilters({ ...next });
      setLoading(true);
      setRefreshing(refreshSource);
      setSourceChecked(false);
      setError('');
      try {
        const result = refreshSource
          ? await refreshDashboard(next, abort.signal)
          : await getDashboard(next, abort.signal);
        throwIfAborted(signal);
        if (!mounted.current || version !== requestVersion.current) return { status: 'superseded' };
        await commitUi(() => {
          setData(result);
          setFilters(result.context.filters);
          setLoading(false);
          setRefreshing(false);
          setSourceChecked(refreshSource && !result.status.stale);
          scopePending.current = false;
        }, signal);
        if (!mounted.current || version !== requestVersion.current) return { status: 'superseded' };
        return { status: 'applied', context_id: result.context.context_id };
      } catch (failure) {
        if (!mounted.current || version !== requestVersion.current) return { status: 'superseded' };
        const message = failure instanceof Error ? failure.message : String(failure);
        // Keep the last committed immutable context. Aborting cannot undo a completed commit.
        await commitUi(() => {
          setLoading(false);
          setRefreshing(false);
          scopePending.current = false;
          if (!signal?.aborted && !abort.signal.aborted) setError(message);
        });
        throwIfAborted(signal);
        return { status: 'failed', error: message };
      } finally {
        signal?.removeEventListener('abort', cancel);
      }
    },
    [],
  );
  useEffect(() => {
    mounted.current = true;
    void loadDashboard(initialFilters);
    return () => {
      mounted.current = false;
      dashboardAbort.current?.abort();
    };
  }, [loadDashboard]);
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
  const applyManualFilters = useCallback(
    (next: Filters) => {
      void loadDashboard(next);
    },
    [loadDashboard],
  );
  function keyTabs(event: KeyboardEvent, index: number) {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? 1 : 1 - index;
    tabs.current[next]?.focus();
    selectTab(next === 0 ? 'dashboard' : 'agent');
  }
  const refresh = useCallback(
    () => loadDashboard(data?.context.filters ?? filters, true),
    [data, filters, loadDashboard],
  );
  const analyze = useCallback(
    (priority?: number) => {
      if (!data || loading || refreshing || scopePending.current || !canAnalyze) return;
      const selected = data.analysis.recommendations.find((item) => item.priority === priority);
      const id = ++prefillSequence.current;
      setPrefill({
        id,
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
      const staging: AnalysisStage = { id, context_id: data.context.context_id, status: 'pending' };
      setAnalysisStage(staging);
      selectTab('agent');
      return staging;
    },
    [data, loading, refreshing, canAnalyze, contextSummary, t, selectTab],
  );
  const prefillApplied = useCallback((id: number, contextId: string, applied: boolean) => {
    setAnalysisStage((current) =>
      current?.id === id && current.context_id === contextId
        ? { ...current, status: applied ? 'staged' : 'not_staged' }
        : current,
    );
  }, []);
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
        dashboardAbort.current?.abort();
        currentContextId.current = result.context.context_id;
        setData(result);
        setFilters(result.context.filters);
        setLoading(false);
        setError('');
        setSourceChecked(false);
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
  const navigate = useCallback(
    async (next: Tab, signal?: AbortSignal) => {
      await commitUi(() => selectTab(next), signal);
      await waitForUi(() => {
        if (!mounted.current || committed.current.tab !== next)
          throw new Error('Workspace navigation was superseded.');
        return next === 'dashboard' || document.querySelector('#agent-panel .agent-workspace')
          ? true
          : undefined;
      }, signal);
    },
    [selectTab],
  );
  useWebMcpTools(
    buildWorkspaceTools({
      getSnapshot: () => committed.current,
      navigate,
      changeLanguage: (next, signal) => commitUi(() => setLocale(next), signal),
      async applyScope(next, signal) {
        const version = requestVersion.current;
        await navigate('dashboard', signal);
        if (version !== requestVersion.current || scopePending.current)
          throw new Error('The Dashboard scope changed before the request could be applied.');
        const result = await loadDashboard(next, false, signal);
        if (result.status === 'applied' && committed.current.tab !== 'dashboard')
          return { status: 'superseded', context_id: result.context_id };
        return result;
      },
      async refreshScope(signal) {
        const version = requestVersion.current;
        const next = committed.current.data?.context.filters;
        if (!next) throw new Error('The Dashboard has no applied scope.');
        await navigate('dashboard', signal);
        if (version !== requestVersion.current || scopePending.current)
          throw new Error('The Dashboard scope changed before the source could be refreshed.');
        const result = await loadDashboard(next, true, signal);
        if (result.status === 'applied' && committed.current.tab !== 'dashboard')
          return { status: 'superseded', context_id: result.context_id };
        return result;
      },
      async stageAnalysis(priority, signal) {
        let staging: AnalysisStage | undefined;
        await commitUi(() => {
          staging = analyze(priority);
        }, signal);
        if (!staging)
          throw new Error('The Dashboard cannot stage an analysis while its scope changes.');
        const stagedId = staging.id;
        return waitForUi(() => {
          if (!mounted.current) throw new Error('This workspace is no longer mounted.');
          const current = committed.current.analysisStage;
          if (current?.id !== stagedId) throw new Error('Analysis staging was superseded.');
          if (committed.current.tab !== 'agent')
            throw new Error('Analysis staging was interrupted by workspace navigation.');
          if (current.context_id !== committed.current.data?.context.context_id)
            throw new Error('The Dashboard scope changed during analysis staging.');
          return current.status !== 'pending' ? current : undefined;
        }, signal);
      },
    }),
  );
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
          visible={tab === 'dashboard'}
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
              onPrefillApplied={prefillApplied}
            />
          </Suspense>
        )}
      </section>
    </div>
  );
}
