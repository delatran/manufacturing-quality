import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, FormEvent, KeyboardEvent } from 'react';
import {
  ArrowDown,
  ArrowRight,
  ArrowUp,
  ChartNoAxesCombined,
  ChevronDown,
  CircleAlert,
  MessageSquareText,
  Square,
  Undo2,
  X,
} from 'lucide-react';
import {
  artifactUrl,
  createSession,
  getConfig,
  getDashboardContext,
  getSession,
  HttpError,
  request,
  socketUrl,
} from './api';
import EventView, { chartArtifact, friendlyError } from './EventView';
import { useI18n } from './i18n';
import { applicationUrl } from './app-url';
import { projectEvents } from './stream';
import AgentFlow from './AgentFlow';
import { projectAgentFlow } from './agent-flow';
import type { FlowRun } from './agent-flow';
import type { AppConfig, Session, WorkspaceEvent } from './types';
import { parseUiCommand } from './workspace-state';
import type { DashboardData } from './dashboard-types';
import type { UiCommand } from './workspace-state';
import AgentScope, { questionScope } from './AgentScope';
import { conversationScrollPosition } from './conversation-scroll';

const storageKey = 'analyst.workspace.session.v1';
const draftKey = 'analyst.workspace.draft.v2';
const terminalStates = new Set([
  'completed',
  'complete',
  'done',
  'failed',
  'error',
  'success',
  'cancelled',
  'interrupted',
  'unverified',
  'idle',
  'budget_exceeded',
  'budget_exhausted',
  'provider_stopped',
  'refused',
  'incomplete',
  'timeout',
  'unavailable',
]);
type Draft = { text: string; contextId?: string; contextSummary?: string };
export type AgentActionIntent = 'none' | 'draft_create' | 'draft_update';
export type AgentWorkItemDraft = {
  title: string;
  detail: string;
  contextId?: string;
  reportId?: string;
};
export type AgentWorkItemReview = { caseId: string; contextId: string };
type Suggestion = { id: string; title: string; detail: string; prompt: string };

export function buildPromptMessage(
  text: string,
  language: 'en' | 'vi',
  contextId?: string,
  actionIntent: AgentActionIntent = 'none',
  steerRunId?: string,
) {
  if (actionIntent !== 'none' && !contextId && !steerRunId)
    throw new Error('Choose a dashboard context before preparing an action draft');
  return {
    type: 'prompt' as const,
    text,
    analysis_context_id: contextId,
    language,
    action_intent: actionIntent,
    ...(steerRunId ? { steer_run_id: steerRunId } : {}),
  };
}

export function savedWorkForReport(
  events: WorkspaceEvent[],
  report?: WorkspaceEvent,
  context?: WorkspaceEvent,
): AgentWorkItemReview | undefined {
  const contextId = context?.data.context_id;
  if (
    !report?.run_id ||
    report.type !== 'message' ||
    report.data.role !== 'assistant' ||
    context?.type !== 'analysis_context' ||
    context.run_id !== report.run_id ||
    context.session_id !== report.session_id ||
    typeof contextId !== 'string' ||
    !contextId.trim()
  )
    return;
  const isRecord = (value: unknown): value is Record<string, unknown> =>
    value !== null && typeof value === 'object' && !Array.isArray(value);
  // A later failed retry cannot erase an earlier durable save in this report's run.
  for (const event of [...events].reverse()) {
    if (
      event.type !== 'tool' ||
      event.run_id !== report.run_id ||
      event.session_id !== report.session_id ||
      event.data.status !== 'completed' ||
      (event.data.name !== 'operations_create_draft' &&
        event.data.name !== 'operations_update_draft')
    )
      continue;
    const result = event.data.result;
    if (!isRecord(result) || result.ok !== true || !isRecord(result.case)) continue;
    const item = result.case;
    if (
      typeof item.id !== 'string' ||
      !item.id.trim() ||
      item.context_id !== contextId ||
      item.status !== 'draft'
    )
      continue;
    // The receipt confirms a past save. Opening the item must read its current state.
    return { caseId: item.id, contextId };
  }
}

const description = (error: unknown) => (error instanceof Error ? error.message : String(error));
const readSession = () => {
  try {
    return localStorage.getItem(storageKey);
  } catch {
    return null;
  }
};
const saveSession = (id: string) => {
  try {
    localStorage.setItem(storageKey, id);
  } catch {
    /* A private browser can still use the current session. */
  }
};
const readDraft = (): Draft => {
  try {
    const value = JSON.parse(localStorage.getItem(draftKey) || '{}') as Partial<Draft>;
    return {
      text: typeof value.text === 'string' ? value.text : '',
      contextId: typeof value.contextId === 'string' ? value.contextId : undefined,
      contextSummary: typeof value.contextSummary === 'string' ? value.contextSummary : undefined,
    };
  } catch {
    return { text: '' };
  }
};

const conversationRun = (event: WorkspaceEvent) =>
  JSON.stringify([event.session_id, event.run_id ?? null]);
const hasField = (value: Record<string, unknown>, key: string) =>
  Object.prototype.hasOwnProperty.call(value, key);
const artifactRecord = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value);

function chartIdentity(data: Record<string, unknown>): string | undefined {
  const { path, sha256 } = data;
  if (
    typeof path !== 'string' ||
    !path ||
    path.includes('\\') ||
    path.includes(':') ||
    path.split('/').some((part) => !part || part === '.' || part === '..') ||
    typeof sha256 !== 'string' ||
    !/^[a-f0-9]{64}$/i.test(sha256)
  )
    return;
  return JSON.stringify([path, sha256]);
}

function selectedChartUrls(report: WorkspaceEvent): Map<string, string> {
  const selected = new Map<string, string>();
  const delivery = report.data.delivery;
  if (
    !report.run_id ||
    !artifactRecord(delivery) ||
    delivery.version !== 1 ||
    !Array.isArray(delivery.artifacts) ||
    !Array.isArray(delivery.source_execution_ids) ||
    delivery.source_execution_ids.some((id) => typeof id !== 'string' || !id.trim()) ||
    !hasField(delivery, 'scope') ||
    (delivery.scope !== null && !artifactRecord(delivery.scope))
  )
    return selected;
  for (const item of delivery.artifacts) {
    if (
      !artifactRecord(item) ||
      typeof item.name !== 'string' ||
      !item.name.trim() ||
      typeof item.mime_type !== 'string' ||
      !item.mime_type.trim() ||
      typeof item.size_bytes !== 'number' ||
      !Number.isSafeInteger(item.size_bytes) ||
      item.size_bytes < 0 ||
      (hasField(item, 'kind') && typeof item.kind !== 'string')
    )
      return new Map();
    const identity = chartIdentity(item);
    const url = artifactUrl(item.url);
    if (!identity || !url) return new Map();
    try {
      const address = new URL(url);
      const expected = applicationUrl(
        `/api/sessions/${report.session_id}/runs/${report.run_id}/artifacts/${item.path}`,
      );
      if (decodeURIComponent(address.pathname) !== expected || address.search || address.hash)
        return new Map();
      if (/\.(?:png|jpe?g|webp|svg|gif)$/i.test(item.name)) selected.set(identity, url);
    } catch {
      return new Map();
    }
  }
  return selected;
}

function projectChartEvents(events: WorkspaceEvent[]): WorkspaceEvent[] {
  const reports = new Set<string>();
  const controlled = new Set<string>();
  const selections = new Map<string, Map<string, string>>();
  for (const event of events) {
    const run = conversationRun(event);
    if (
      (event.type === 'run_started' || event.type === 'status') &&
      hasField(event.data, 'artifact_delivery_version')
    )
      controlled.add(run);
    if (event.type === 'artifact' && hasField(event.data, 'delivery_candidate'))
      controlled.add(run);
    if (event.type === 'artifact_group' && Array.isArray(event.data.items)) {
      for (const item of event.data.items as WorkspaceEvent[]) {
        if (hasField(item.data, 'delivery_candidate')) controlled.add(conversationRun(item));
      }
    }
    if (event.type === 'message' && event.data.role === 'assistant') {
      reports.add(run);
      if (hasField(event.data, 'delivery')) {
        controlled.add(run);
        selections.set(run, selectedChartUrls(event));
      } else selections.delete(run);
    }
  }
  // Deduplicate artifact versions before selecting them, so an older matching hash
  // cannot reappear after a newer event records different bytes at the same path.
  const projected = projectEvents(events);
  const allowed = new Set<WorkspaceEvent>();
  const urls = new Map<string, Map<string, string>>();
  for (const event of projected) {
    const candidates =
      event.type === 'artifact_group'
        ? ((Array.isArray(event.data.items) ? event.data.items : []) as WorkspaceEvent[])
        : event.type === 'artifact'
          ? [event]
          : [];
    for (const item of candidates) {
      const run = conversationRun(item);
      if (run !== conversationRun(event) || !reports.has(run)) continue;
      if (!chartArtifact(item)) continue;
      // Legacy reports without either marker retain their earlier charts.
      // A present empty, invalid or unsupported packet never falls back to inventory.
      if (controlled.has(run)) {
        const identity = chartIdentity(item.data);
        const url = artifactUrl(item.data.url);
        if (!identity || !url || selections.get(run)?.get(identity) !== url) continue;
        const values = urls.get(run) || new Map<string, string>();
        values.set(identity, url);
        urls.set(run, values);
      }
      allowed.add(item);
    }
  }
  return projected.flatMap((event) => {
    if (event.type === 'artifact') return allowed.has(event) ? [event] : [];
    if (event.type === 'artifact_group') {
      const items = (
        (Array.isArray(event.data.items) ? event.data.items : []) as WorkspaceEvent[]
      ).filter((item) => allowed.has(item));
      return items.length ? [{ ...event, data: { ...event.data, items } }] : [];
    }
    const run = conversationRun(event);
    if (event.type === 'message' && event.data.role === 'assistant' && controlled.has(run)) {
      // This field belongs only to the display copy, never the stored report.
      // An older report cannot display new bytes registered at its former URL.
      const ownSelection = selectedChartUrls(event);
      const currentSelection = urls.get(run);
      const chartUrls = [...ownSelection]
        .filter(([identity, url]) => currentSelection?.get(identity) === url)
        .map(([, url]) => url);
      return [{ ...event, data: { ...event.data, chart_urls: chartUrls } }];
    }
    return [event];
  });
}

// This is a display projection. Source events, reports and receipts are never changed.
export function projectConversation(events: WorkspaceEvent[]): WorkspaceEvent[] {
  const projected = projectChartEvents(events);
  const result: WorkspaceEvent[] = [];
  const activity = new Map<string, WorkspaceEvent>();
  const charts = new Map<string, WorkspaceEvent[]>();
  const lastReports = new Map<string, string | number>();
  const embeddedImages = new Map<string, Set<string>>();
  projected.forEach((event) => {
    if (event.type !== 'message' || event.data.role !== 'assistant') return;
    const run = conversationRun(event);
    lastReports.set(run, event.id);
    const images = embeddedImages.get(run) || new Set<string>();
    const allowedUrls = event.data.chart_urls;
    for (const match of String(event.data.text ?? event.data.content ?? '').matchAll(
      /!\[[^\]]*\]\(<?([^\s)>]+)>?(?:\s+[^)]*)?\)/g,
    )) {
      const url = artifactUrl(match[1]);
      if (
        url &&
        (!hasField(event.data, 'chart_urls') ||
          (Array.isArray(allowedUrls) && allowedUrls.includes(url)))
      )
        images.add(url);
    }
    embeddedImages.set(run, images);
  });
  projected.forEach((event) => {
    const run = conversationRun(event);
    if (event.type === 'artifact_group' || event.type === 'artifact') {
      const items =
        event.type === 'artifact_group'
          ? ((Array.isArray(event.data.items) ? event.data.items : []) as WorkspaceEvent[])
          : [event];
      for (const item of items) {
        const itemRun = conversationRun(item);
        const url = artifactUrl(item.data.url);
        if (chartArtifact(item) && (!url || !embeddedImages.get(itemRun)?.has(url)))
          charts.set(itemRun, [...(charts.get(itemRun) || []), item]);
      }
      return;
    }
    if (event.type === 'tool' || (event.type === 'status' && event.run_id)) {
      if (
        event.type === 'tool' &&
        /^(?:memory_|todo_|knowledge_)/.test(String(event.data.name || ''))
      )
        return;
      let group = activity.get(run);
      if (!group) {
        group = {
          ...event,
          id: 'activity-' + run,
          type: 'activity',
          data: { state: 'running', items: [] },
        };
        activity.set(run, group);
        result.push(group);
      }
      if (event.type === 'status') group.data.state = event.data.state;
      else (group.data.items as WorkspaceEvent[]).push(event);
      return;
    }
    if (
      event.type === 'message' ||
      event.type === 'error' ||
      (event.type === 'status' && event.data.state === 'queued')
    )
      result.push(event);
  });
  // Place chart previews after their report, without duplicating inline charts.
  const conversation: WorkspaceEvent[] = [];
  const appendCharts = (run: string) => {
    conversation.push(...(charts.get(run) || []));
    charts.delete(run);
  };
  result.forEach((event) => {
    conversation.push(event);
    if (event.type !== 'message' || event.data.role === 'user') return;
    const run = conversationRun(event);
    if (lastReports.get(run) !== event.id) return;
    appendCharts(run);
  });
  charts.forEach((_, run) => appendCharts(run));
  return conversation;
}

export default function AgentWorkspace({
  visible,
  contextId,
  contextSummary,
  dashboardContext,
  dashboardData,
  prefill,
  suggestions: suppliedSuggestions,
  onUiCommand,
  onOpenDashboard,
}: {
  visible: boolean;
  contextId?: string;
  contextSummary?: string;
  dashboardContext?: DashboardData['context'];
  dashboardData?: DashboardData;
  prefill?: { id: number; text: string; contextId: string; contextSummary?: string };
  suggestions?: Suggestion[];
  onUiCommand?: (command: UiCommand) => Promise<boolean>;
  onOpenDashboard?: () => void;
}) {
  const { locale, t, scope, number, percent } = useI18n();
  const [initialDraft] = useState(readDraft);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [events, setEvents] = useState<WorkspaceEvent[]>([]);
  const [text, setText] = useState(initialDraft.text);
  const [prefillContext, setPrefillContext] = useState<string | undefined>(initialDraft.contextId);
  const [frozenContext, setFrozenContext] = useState<DashboardData['context']>();
  const [frozenData, setFrozenData] = useState<DashboardData>();
  const [failedContextId, setFailedContextId] = useState<string>();
  const [scopeRetry, setScopeRetry] = useState(0);
  const [connection, setConnection] = useState('connecting');
  const [error, setError] = useState('');
  const [lastCommand, setLastCommand] = useState<UiCommand>();
  const [commandFeedback, setCommandFeedback] = useState('');
  const [commandBusy, setCommandBusy] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const uiCommandHandler = useRef(onUiCommand);
  uiCommandHandler.current = onUiCommand;
  const commandExecutions = useRef(new Map<string, Promise<boolean>>());
  const applyCommand = useCallback((command: UiCommand) => {
    let pending = commandExecutions.current.get(command.command_id);
    if (!pending) {
      pending = uiCommandHandler.current?.(command) || Promise.resolve(false);
      commandExecutions.current.set(command.command_id, pending);
    }
    return pending;
  }, []);
  const [awaitingPrompt, setAwaitingPrompt] = useState<string | null>(null);
  const [deliveryUncertain, setDeliveryUncertain] = useState(false);
  const [showLatest, setShowLatest] = useState(false);
  const [composerHeight, setComposerHeight] = useState(240);
  const socket = useRef<WebSocket | null>(null);
  const cursor = useRef<string | number | undefined>(undefined);
  const composer = useRef<HTMLTextAreaElement>(null);
  const stream = useRef<HTMLDivElement>(null);
  const conversationTail = useRef<HTMLDivElement>(null);
  const composerDock = useRef<HTMLElement>(null);
  const nearBottom = useRef(true);
  const pendingPrompt = useRef<string | null>(null);
  const pendingDraft = useRef<Draft | null>(null);
  const bootRetry = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const {
    state,
    active,
    providerStopped,
    lastRunStatus,
    hasConversation,
    lastReport,
    lastQuestion,
    runningActionValue: derivedActionValue,
  } = useMemo(() => {
    let lastStatusEv: WorkspaceEvent | undefined;
    let lastRunStatusEv: WorkspaceEvent | undefined;
    let hasMessage = false;
    let lastReportEv: WorkspaceEvent | undefined;
    let lastQuestionEv: WorkspaceEvent | undefined;
    let hasProviderStopped = false;

    for (let i = events.length - 1; i >= 0; i--) {
      const ev = events[i];
      if (ev.type === 'status') {
        if (!lastStatusEv) lastStatusEv = ev;
        if (!lastRunStatusEv && ev.run_id) lastRunStatusEv = ev;
        if (ev.data?.state === 'provider_stopped') hasProviderStopped = true;
      }
      if (ev.type === 'message') {
        hasMessage = true;
        if (!lastReportEv && ev.data?.role !== 'user') lastReportEv = ev;
        if (!lastQuestionEv && ev.data?.role === 'user') lastQuestionEv = ev;
      }
    }

    const currentState = String(lastStatusEv?.data.state || 'idle');
    const isAct = !terminalStates.has(currentState);
    const isProvStopped = currentState === 'provider_stopped' || hasProviderStopped;

    let actionVal: unknown;
    if (lastRunStatusEv?.run_id) {
      for (let i = events.length - 1; i >= 0; i--) {
        const ev = events[i];
        if (
          ev.run_id === lastRunStatusEv.run_id &&
          ['run_started', 'status', 'analysis_context'].includes(ev.type) &&
          typeof ev.data.action_intent === 'string'
        ) {
          actionVal = ev.data.action_intent;
          break;
        }
      }
    }

    return {
      lastStatus: lastStatusEv,
      state: currentState,
      active: isAct,
      providerStopped: isProvStopped,
      lastRunStatus: lastRunStatusEv,
      hasConversation: hasMessage,
      lastReport: lastReportEv,
      lastQuestion: lastQuestionEv,
      runningActionValue: actionVal,
    };
  }, [events]);

  const runContexts = useMemo(() => {
    const contexts = new Map<string, WorkspaceEvent>();
    events.forEach((event) => {
      if (event.type === 'analysis_context' && event.run_id) contexts.set(event.run_id, event);
    });
    return contexts;
  }, [events]);
  const runningContextEvent = lastRunStatus?.run_id
    ? runContexts.get(lastRunStatus.run_id)
    : undefined;
  const restoredRun = session?.runs?.find((run) => run.id === lastRunStatus?.run_id);
  const runningContextId =
    typeof runningContextEvent?.data.context_id === 'string'
      ? runningContextEvent.data.context_id
      : restoredRun?.analysis_context_id;
  const runningLanguage =
    typeof runningContextEvent?.data.language === 'string'
      ? runningContextEvent.data.language
      : typeof lastRunStatus?.data.language === 'string'
        ? lastRunStatus.data.language
        : restoredRun?.language;
  const runningActionValue =
    derivedActionValue ??
    (restoredRun && 'action_intent' in restoredRun ? restoredRun.action_intent : undefined);
  const runningActionIntent: AgentActionIntent =
    runningActionValue === 'draft_create' || runningActionValue === 'draft_update'
      ? runningActionValue
      : 'none';
  const aiUnavailable = config?.ai_available === false;
  const mock = config
    ? ['mock', 'simulation', 'simulated', 'offline'].includes(config.mode?.toLowerCase())
    : false;
  const visibleEvents = useMemo(() => projectConversation(events), [events]);
  // The full provider timeline is projected from the same stored history.
  const flowRuns = useMemo<Map<string, FlowRun>>(() => projectAgentFlow(events), [events]);
  const selectedScope = questionScope({
    dashboardContextId: contextId,
    capturedContextId: prefillContext,
    dashboardContext,
    loadedContext: frozenContext,
    failedContextId,
  });
  const selectedContext = selectedScope.id;
  const composerLabel = active
    ? t('ui.agent_steer_current_work')
    : hasConversation
      ? t('ui.agent_next_question')
      : t('ui.your_analysis_question');
  const sendLabel = active ? t('ui.agent_send_instruction') : t('ui.send_question');
  const capturedViewDiffers = selectedScope.captured;
  const scopeUnavailable = selectedScope.unavailable;
  const scopeLoading = selectedScope.loading;
  const selectedSummary = capturedViewDiffers
    ? scopeUnavailable
      ? t('ui.captured_scope_unavailable')
      : selectedScope.context
        ? scope(selectedScope.context.filters)
        : t('ui.the_earlier_data_selection_saved_with_this_question')
    : contextSummary || t('ui.the_current_dashboard_filters');
  useEffect(() => {
    setFrozenContext(undefined);
    setFrozenData(undefined);
    setFailedContextId(undefined);
    if (!selectedContext || selectedContext === contextId) return;
    const abort = new AbortController();
    void getDashboardContext(selectedContext, abort.signal)
      .then((value) => {
        if (!abort.signal.aborted) {
          if (value.context.context_id === selectedContext) {
            setFrozenContext(value.context);
            setFrozenData(value);
          } else setFailedContextId(selectedContext);
        }
      })
      .catch(() => {
        if (!abort.signal.aborted) setFailedContextId(selectedContext);
      });
    return () => abort.abort();
  }, [selectedContext, contextId, scopeRetry]);
  const activeData =
    selectedContext && frozenData?.context.context_id === selectedContext
      ? frozenData
      : selectedContext && dashboardData?.context.context_id === selectedContext
        ? dashboardData
        : undefined;
  const activeKpis = activeData?.analysis.kpis;
  function summaryFor(context?: WorkspaceEvent) {
    const filters = context?.data.filters;
    return filters && typeof filters === 'object'
      ? scope(filters as Record<string, unknown>)
      : t('ui.the_data_selected_for_this_analysis');
  }

  const suggestions = suppliedSuggestions || [
    {
      id: 'trend',
      title: t('ui.understand_the_trend'),
      detail: t('ui.how_are_recorded_defects_changing'),
      prompt: t('ui.analyze_how_recorded_defect_counts_change_over_the_selected_perio'),
    },
    {
      id: 'severity',
      title: t('ui.find_the_critical_concentration'),
      detail: t('ui.which_locations_need_closer_attention'),
      prompt: t('ui.compare_defect_location_and_severity_in_the_selected_data_where_a'),
    },
    {
      id: 'patterns',
      title: t('ui.investigate_a_pattern'),
      detail: t('ui.compare_lines_products_and_repair_costs'),
      prompt: t('ui.find_notable_patterns_across_production_lines_products_and_record'),
    },
    {
      id: 'priorities',
      title: t('ui.plan_the_next_actions'),
      detail: t('ui.priorities_suggested_owners_and_measures'),
      prompt: t('ui.recommend_the_top_three_issues_production_should_investigate_firs'),
    },
  ];

  useEffect(() => {
    if (!prefill) return;
    setText(prefill.text);
    setPrefillContext(prefill.contextId);
    composer.current?.focus();
  }, [prefill]);

  useEffect(() => {
    const timer = setTimeout(() => {
      try {
        const savedDraft =
          awaitingPrompt && pendingDraft.current
            ? pendingDraft.current
            : { text, contextId: prefillContext, contextSummary: selectedSummary };
        localStorage.setItem(draftKey, JSON.stringify(savedDraft));
      } catch {
        /* Drafts remain available in the current tab without storage. */
      }
    }, 200);
    return () => clearTimeout(timer);
  }, [text, prefillContext, selectedSummary, awaitingPrompt]);

  useEffect(() => {
    let disposed = false;
    const boot = async () => {
      try {
        const configuration = await getConfig();
        if (disposed) return;
        const savedId = readSession();
        let current: Session;
        if (savedId) {
          try {
            current = await getSession(savedId);
          } catch (failure) {
            if (failure instanceof HttpError && failure.status === 404)
              current = await createSession();
            else throw failure;
          }
        } else current = await createSession();
        if (disposed) return;
        saveSession(current.id);
        setConfig(configuration);
        setSession(current);
        setEvents(current.events || []);
        cursor.current = current.events?.at(-1)?.id;
        setError('');
      } catch {
        if (disposed) return;
        setError('Connection unavailable');
        setConnection('reconnecting');
        bootRetry.current = setTimeout(boot, 5000);
      }
    };
    void boot();
    return () => {
      disposed = true;
      clearTimeout(bootRetry.current);
    };
  }, []);

  useEffect(() => {
    if (!session) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    let attempt = 0;
    const connect = () => {
      if (disposed) return;
      const ws = new WebSocket(socketUrl(session.id, cursor.current));
      socket.current = ws;
      ws.onopen = () => {
        if (disposed) return;
        attempt = 0;
        setConnection('connected');
        setError((previous) => (/^Connection|An unreadable/.test(previous) ? '' : previous));
      };
      ws.onmessage = (message) => {
        try {
          const value = JSON.parse(message.data) as
            WorkspaceEvent | { type: string; events: WorkspaceEvent[] };
          const incoming = 'events' in value ? value.events : [value as WorkspaceEvent];
          const valid = incoming.filter(
            (event) => event.id !== undefined && event.type && event.data,
          );
          if (!valid.length) return;
          cursor.current = valid.at(-1)?.id;
          setEvents((previous) => {
            const ids = new Set(previous.map((event) => String(event.id)));
            return [
              ...previous,
              ...valid.filter((event) => {
                const id = String(event.id);
                if (ids.has(id)) return false;
                ids.add(id);
                return true;
              }),
            ];
          });
          valid.forEach((event) => {
            if (event.type === 'ui_command') {
              const command = parseUiCommand(event.data);
              if (command && uiCommandHandler.current) {
                void applyCommand(command)
                  .then((applied) => {
                    setCommandFeedback(applied ? 'applied' : 'stale');
                    if (applied) setLastCommand(command.action === 'undo' ? undefined : command);
                  })
                  .catch(() => setCommandFeedback('failed'));
              }
            }
            if (event.type === 'status' && terminalStates.has(String(event.data.state)))
              setCancelling(false);
            if (
              event.type === 'message' &&
              event.data.role === 'user' &&
              (!pendingPrompt.current || event.data.text === pendingPrompt.current)
            ) {
              pendingPrompt.current = null;
              pendingDraft.current = null;
              setAwaitingPrompt(null);
            }
            if (event.type === 'error' && !event.run_id && pendingPrompt.current) {
              const unsent = pendingPrompt.current;
              const original = pendingDraft.current;
              pendingPrompt.current = null;
              pendingDraft.current = null;
              setText((previous) => previous || unsent);
              setAwaitingPrompt(null);
              if (original) {
                setPrefillContext(original.contextId);
              }
            }
          });
        } catch {
          setError('An unreadable event was received');
        }
      };
      ws.onerror = () => ws.close();
      ws.onclose = () => {
        if (disposed) return;
        setConnection('reconnecting');
        if (pendingPrompt.current) setDeliveryUncertain(true);
        timer = setTimeout(connect, Math.min(1000 * 2 ** attempt++, 10000));
      };
    };
    connect();
    return () => {
      disposed = true;
      clearTimeout(timer);
      socket.current?.close();
    };
  }, [session, applyCommand]);
  useEffect(() => {
    setDeliveryUncertain(false);
    if (!awaitingPrompt) return;
    const timer = setTimeout(() => setDeliveryUncertain(true), 12000);
    return () => clearTimeout(timer);
  }, [awaitingPrompt]);

  const scrollPosition = useCallback(() => {
    const viewport = window.visualViewport;
    return conversationScrollPosition({
      scrollY: window.scrollY,
      viewportTop: viewport?.offsetTop ?? 0,
      viewportHeight: viewport?.height ?? window.innerHeight,
      composerTop: composerDock.current?.getBoundingClientRect().top ?? window.innerHeight,
      tailBottom: conversationTail.current?.getBoundingClientRect().bottom ?? 0,
      documentHeight: document.documentElement.scrollHeight,
      layoutHeight: document.documentElement.clientHeight,
    });
  }, []);
  const followLatest = useCallback(() => {
    window.scrollTo({ top: scrollPosition().target, behavior: 'instant' });
  }, [scrollPosition]);
  useEffect(() => {
    const onScroll = () => {
      if (!visible || !hasConversation) return;
      nearBottom.current = scrollPosition().nearBottom;
      setShowLatest(!nearBottom.current);
    };
    const onViewportChange = () => {
      if (!visible || !hasConversation) return;
      if (nearBottom.current) followLatest();
      else setShowLatest(!scrollPosition().nearBottom);
    };
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onViewportChange);
    window.visualViewport?.addEventListener('resize', onViewportChange);
    window.visualViewport?.addEventListener('scroll', onViewportChange);
    return () => {
      window.removeEventListener('scroll', onScroll);
      window.removeEventListener('resize', onViewportChange);
      window.visualViewport?.removeEventListener('resize', onViewportChange);
      window.visualViewport?.removeEventListener('scroll', onViewportChange);
    };
  }, [visible, hasConversation, scrollPosition, followLatest]);
  const scrollToLatest = () => {
    nearBottom.current = true;
    setShowLatest(false);
    followLatest();
    // The jump button now disappears. Give its keyboard focus a meaningful
    // destination without triggering a second browser-controlled scroll.
    const latestEntry = stream.current?.lastElementChild;
    if (latestEntry instanceof HTMLElement) latestEntry.focus({ preventScroll: true });
  };
  useLayoutEffect(() => {
    if (visible && hasConversation && nearBottom.current) followLatest();
  }, [visibleEvents, awaitingPrompt, visible, hasConversation, composerHeight, followLatest]);
  useEffect(() => {
    if (!stream.current) return;
    const observer = new ResizeObserver(() => {
      if (visible && hasConversation && nearBottom.current) followLatest();
    });
    observer.observe(stream.current);
    return () => observer.disconnect();
  }, [visible, hasConversation, followLatest]);
  useEffect(() => {
    if (!composer.current) return;
    composer.current.style.height = 'auto';
    composer.current.style.height = Math.min(composer.current.scrollHeight, 180) + 'px';
  }, [text]);
  useEffect(() => {
    if (!composerDock.current) return;
    const observer = new ResizeObserver(([entry]) => {
      const height = Math.ceil(entry.target.getBoundingClientRect().height);
      if (height > 0) setComposerHeight(height);
    });
    observer.observe(composerDock.current);
    return () => observer.disconnect();
  }, []);

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    const prompt = text.trim();
    if (
      !prompt ||
      !session ||
      awaitingPrompt ||
      providerStopped ||
      aiUnavailable ||
      (active ? !lastRunStatus?.run_id : scopeUnavailable || scopeLoading || !selectedContext) ||
      socket.current?.readyState !== WebSocket.OPEN
    )
      return;
    setError('');
    // Details target the active run, irrespective of ambient Dashboard or locale changes.
    const promptContext = active ? (runningContextId ?? undefined) : selectedContext;
    const promptLanguage =
      active && (runningLanguage === 'en' || runningLanguage === 'vi') ? runningLanguage : locale;
    try {
      if (socket.current?.readyState !== WebSocket.OPEN)
        throw new Error('Connection lost before the prompt was sent');
      pendingPrompt.current = prompt;
      pendingDraft.current = {
        text: prompt,
        contextId: active ? prefillContext : promptContext,
        contextSummary: selectedSummary,
      };
      socket.current.send(
        JSON.stringify(
          buildPromptMessage(
            prompt,
            promptLanguage,
            promptContext,
            active ? runningActionIntent : 'none',
            active ? lastRunStatus?.run_id : undefined,
          ),
        ),
      );
      setAwaitingPrompt(prompt);
      setText('');
      if (!active) setPrefillContext(undefined);
      nearBottom.current = true;
      composer.current?.focus();
    } catch (failure) {
      setError(description(failure));
    }
  }
  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void submit();
    }
  }
  function chooseSuggestion(suggestion: Suggestion) {
    setText(suggestion.prompt);
    setPrefillContext(selectedContext);
    composer.current?.focus();
  }
  function followUp(prompt: string) {
    setText(prompt);
    const reportContext = lastReport?.run_id ? runContexts.get(lastReport.run_id) : undefined;
    const id = reportContext?.data.context_id;
    if (typeof id === 'string') {
      setPrefillContext(id);
    } else {
      setPrefillContext(undefined);
    }
    composer.current?.focus();
  }

  useEffect(() => {
    if (!session || !contextId) return;
    const abort = new AbortController();
    void request(`/api/sessions/${encodeURIComponent(session.id)}/scope`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ context_id: contextId }),
      signal: abort.signal,
    }).catch((failure: unknown) => {
      if (!abort.signal.aborted) setError(description(failure));
    });
    return () => abort.abort();
  }, [session, contextId]);
  async function undoCommand() {
    if (!session || !lastCommand || commandBusy || !contextId) return;
    setCommandBusy(true);
    try {
      const result = await request<{ command: Record<string, unknown> }>(
        `/api/sessions/${encodeURIComponent(session.id)}/ui-commands/${encodeURIComponent(lastCommand.command_id)}/undo`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ expected_context_id: contextId }),
        },
      );
      const command = parseUiCommand(result.command);
      const applied = command && (await applyCommand(command));
      setCommandFeedback(applied ? 'applied' : 'stale');
      if (applied) setLastCommand(undefined);
    } catch (failure) {
      setError(description(failure));
    } finally {
      setCommandBusy(false);
    }
  }
  async function cancelAnalysis() {
    if (!session || cancelling) return;
    setCancelling(true);
    try {
      await request(`/api/sessions/${encodeURIComponent(session.id)}/cancel`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ run_id: lastRunStatus?.run_id }),
      });
    } catch (failure) {
      setError(description(failure));
      setCancelling(false);
    }
  }
  const availability = aiUnavailable
    ? 'unavailable'
    : providerStopped
      ? 'stopped'
      : connection !== 'connected'
        ? connection
        : awaitingPrompt
          ? 'sending'
          : active
            ? 'running'
            : 'connected';
  return (
    <div
      className={'workspace agent-workspace ' + (hasConversation ? 'has-conversation' : 'is-empty')}
      style={{ '--agent-composer-height': `${composerHeight}px` } as CSSProperties}
    >
      <header className="agent-page-header">
        <div>
          <h1>{t('ui.ask_about_production_quality')}</h1>
          <p>{t('ui.explore_a_finding_understand_a_change_and_decide_what_to_investig')}</p>
        </div>
        {!aiUnavailable && (
          <div className="agent-header-controls">
            <span className={'agent-availability ' + availability} role="status">
              <span aria-hidden="true" className={'connection-dot ' + availability} />
              {providerStopped
                ? t('ui.stopped')
                : connection !== 'connected'
                  ? t('ui.connecting')
                  : awaitingPrompt
                    ? t('ui.sending')
                    : active
                      ? t('ui.analyzing')
                      : t('ui.ready')}
            </span>
          </div>
        )}
      </header>

      <section
        className={'agent-source-card' + (hasConversation ? ' is-compact' : '')}
        aria-label={t('ui.data_for_this_question')}
      >
        <div className="agent-source-top">
          <div className="agent-source-summary">
            <span>{t('ui.selected_scope')}</span>
            {selectedScope.context ? (
              <AgentScope filters={selectedScope.context.filters} />
            ) : (
              <strong>
                {!selectedContext ? t('ui.load_a_dashboard_view_to_get_started') : selectedSummary}
              </strong>
            )}
            {capturedViewDiffers ? (
              <p className="scope-change-note">
                {t('ui.this_draft_keeps_an_earlier_selection')}{' '}
                <button
                  type="button"
                  disabled={!contextId}
                  onClick={() => {
                    setPrefillContext(undefined);
                  }}
                >
                  {t('ui.use_current_dashboard')}
                </button>
              </p>
            ) : null}
          </div>
          {onOpenDashboard && (
            <button
              type="button"
              className="secondary-button agent-scope-action"
              onClick={onOpenDashboard}
            >
              {t('ui.agent_review_dashboard')}
              <ArrowRight size={15} aria-hidden="true" />
            </button>
          )}
        </div>
        {scopeUnavailable && (
          <div className="agent-scope-feedback" role="alert">
            <p>{t('ui.agent_scope_retry_note')}</p>
            <button
              type="button"
              className="text-button"
              onClick={() => {
                setFailedContextId(undefined);
                setScopeRetry((value) => value + 1);
              }}
            >
              {t('ui.agent_scope_retry')}
            </button>
          </div>
        )}
        {scopeLoading && (
          <p className="agent-scope-feedback" role="status">
            {t('ui.agent_scope_loading')}
          </p>
        )}
        {active && runningContextId && runningContextId !== selectedContext && (
          <p className="running-scope-note">
            <CircleAlert size={15} />
            {t('ui.the_analysis_in_progress_keeps_its_original_selection')}{' '}
            {summaryFor(runningContextEvent)}
          </p>
        )}
        {activeKpis && (
          <details className="agent-scope-details">
            <summary>
              <ChartNoAxesCombined size={15} aria-hidden="true" />
              <span>{t('ui.agent_scope_kpis_label')}</span>
              <ChevronDown size={15} aria-hidden="true" />
            </summary>
            <dl className="agent-scope-kpis" aria-label={t('ui.agent_scope_kpis_label')}>
              <div>
                <dt>{t('ui.dashboard_kpi_defects')}</dt>
                <dd>{number(activeKpis.event_count)}</dd>
              </div>
              <div>
                <dt>{t('ui.dashboard_kpi_critical')}</dt>
                <dd className="is-critical">
                  {number(activeKpis.critical_count)}
                  {activeKpis.critical_share_pct != null && (
                    <small> ({percent(activeKpis.critical_share_pct)})</small>
                  )}
                </dd>
              </div>
              <div>
                <dt>{t('ui.dashboard_kpi_rate')}</dt>
                <dd>{number(activeKpis.reference_intensity_per_1000, 2)}</dd>
              </div>
              <div>
                <dt>{t('ui.dashboard_kpi_cost')}</dt>
                <dd>
                  {activeKpis.cost_available
                    ? number(activeKpis.known_repair_cost, 0)
                    : t('ui.unavailable')}
                  {activeKpis.cost_coverage_pct != null && (
                    <small> ({percent(activeKpis.cost_coverage_pct)})</small>
                  )}
                </dd>
              </div>
            </dl>
          </details>
        )}
      </section>

      {commandFeedback && (
        <div className="agent-command-feedback" role="status">
          <p>
            {commandFeedback === 'applied'
              ? t('ui.the_analyst_opened_the_requested_view_the_scope_is_shown_on_the_d')
              : commandFeedback === 'stale'
                ? t('ui.your_selection_changed_the_earlier_agent_command_was_not_applied')
                : t('ui.the_view_command_could_not_be_applied_try_the_dashboard_controls')}
          </p>
          {lastCommand?.reversible && (
            <button
              type="button"
              className="text-button"
              disabled={commandBusy}
              onClick={() => void undoCommand()}
            >
              <Undo2 size={15} />
              {t('ui.undo_agent_view_change')}
            </button>
          )}
        </div>
      )}
      <main
        className={'conversation ' + (!hasConversation ? 'empty' : '')}
        style={hasConversation ? { paddingBottom: composerHeight + 42 } : undefined}
        aria-label={t('ui.analyst_conversation')}
      >
        {mock && (
          <p className="agent-simulation-notice">
            <CircleAlert size={16} />
            {t('ui.simulation_mode_responses_demonstrate_the_workflow_they_are_not_l')}
          </p>
        )}
        <div className="event-stream" ref={stream}>
          {visibleEvents.map((event) => {
            // Show the provider timeline for each run alongside its report and files.
            const flow =
              event.type === 'activity' && event.run_id ? flowRuns.get(event.run_id) : undefined;
            return (
              <div
                className="conversation-entry"
                key={String(event.id)}
                tabIndex={-1}
                role="group"
                aria-label={
                  event.type === 'message'
                    ? event.data.role === 'user'
                      ? t('ui.you')
                      : t('ui.quality_analyst_2')
                    : t('ui.analyst_conversation')
                }
              >
                {event.type === 'message' &&
                  event.data.role !== 'user' &&
                  event.run_id &&
                  runContexts.has(event.run_id) && (
                    <p className="report-scope">
                      <span>{t('ui.based_on')}</span>
                      {summaryFor(runContexts.get(event.run_id))}
                    </p>
                  )}
                {flow ? <AgentFlow run={flow} /> : <EventView event={event} />}
              </div>
            );
          })}
          {awaitingPrompt && (
            <article className="message user-message pending-message" tabIndex={-1}>
              <div className="event-label">
                <span className="user-dot">{t('ui.y')}</span>
                <span>{t('ui.you')}</span>
                <small>
                  {connection === 'connected' ? t('ui.sending') : t('ui.waiting_for_confirmation')}
                </small>
              </div>
              <p>{awaitingPrompt}</p>
            </article>
          )}
        </div>
        {lastReport && !active && (
          <section className="agent-followup" aria-label={t('ui.next_steps')}>
            <div>
              <h2>{t('ui.next_steps')}</h2>
            </div>
            <div className="agent-followup-actions">
              <button
                type="button"
                onClick={() =>
                  followUp(
                    t('ui.from_this_report_which_findings_should_production_investigate_fir'),
                  )
                }
              >
                {t('ui.explore_the_next_check')}
                <ArrowRight size={16} />
              </button>
              <button type="button" onClick={() => followUp(t('ui.agent_followup_halves_prompt'))}>
                {t('ui.agent_followup_halves_label')}
                <ArrowRight size={16} />
              </button>
              <button type="button" onClick={() => followUp(t('ui.agent_followup_mix_prompt'))}>
                {t('ui.agent_followup_mix_label')}
                <ArrowRight size={16} />
              </button>
            </div>
          </section>
        )}
        {!active &&
          [
            'failed',
            'interrupted',
            'unverified',
            'refused',
            'incomplete',
            'timeout',
            'unavailable',
            'cancelled',
          ].includes(state) &&
          lastQuestion && (
            <button
              className="agent-retry-button"
              type="button"
              onClick={() => {
                setText(String(lastQuestion.data.text || ''));
                const originalContext = lastQuestion.data.analysis_context_id;
                if (typeof originalContext === 'string') {
                  setPrefillContext(originalContext);
                } else {
                  setPrefillContext(undefined);
                }
                composer.current?.focus();
              }}
            >
              {t('ui.review_and_resend_the_last_question')}
              <ArrowRight size={15} />
            </button>
          )}
        <div className="conversation-tail" ref={conversationTail} aria-hidden="true" />
      </main>

      <footer className="composer-dock" ref={composerDock}>
        <div className="composer-container">
          {showLatest && hasConversation && (
            <button className="jump-to-latest" type="button" onClick={scrollToLatest}>
              <ArrowDown size={14} />
              {t('ui.latest_response')}
            </button>
          )}
          {error && (
            <div className="composer-error" role="alert">
              <CircleAlert size={16} />
              <span>{friendlyError(error, t)}</span>
              <button
                type="button"
                aria-label={t('ui.dismiss_message')}
                onClick={() => setError('')}
              >
                <X size={15} />
              </button>
            </div>
          )}
          {awaitingPrompt && deliveryUncertain && (
            <div className="composer-delivery-note" role="alert">
              <p>{t('ui.delivery_has_not_been_confirmed_check_the_conversation_before_sen')}</p>
              <button
                type="button"
                onClick={() => {
                  const saved = pendingDraft.current;
                  setText(saved?.text || awaitingPrompt);
                  if (saved) {
                    setPrefillContext(saved.contextId);
                  }
                  pendingPrompt.current = null;
                  pendingDraft.current = null;
                  setAwaitingPrompt(null);
                  requestAnimationFrame(() => composer.current?.focus());
                }}
              >
                {t('ui.restore_the_question_for_review')}
              </button>
            </div>
          )}
          {active && (
            <div className="agent-work-status">
              <button
                type="button"
                className="agent-cancel-button"
                disabled={cancelling}
                onClick={() => void cancelAnalysis()}
              >
                <Square size={12} fill="currentColor" aria-hidden="true" />
                {cancelling ? t('ui.stopping_analysis') : t('ui.stop_analysis')}
              </button>
            </div>
          )}
          <form className={'composer ' + (active ? 'is-active' : '')} onSubmit={submit}>
            <label className="composer-label" htmlFor="analyst-prompt">
              {composerLabel}
            </label>
            <textarea
              id="analyst-prompt"
              ref={composer}
              value={text}
              onChange={(event) => setText(event.target.value)}
              onKeyDown={onKeyDown}
              placeholder={
                active
                  ? t('ui.add_a_detail_or_ask_a_follow_up')
                  : t('ui.ask_a_question_about_your_production_data')
              }
              rows={2}
              disabled={providerStopped || Boolean(awaitingPrompt)}
            />
            <div className="composer-bottom">
              <div className="composer-context">
                <span>{t('ui.using_selected_dashboard_data')}</span>
              </div>
              <button
                className="send-button"
                type="submit"
                disabled={
                  !text.trim() ||
                  !session ||
                  Boolean(awaitingPrompt) ||
                  providerStopped ||
                  aiUnavailable ||
                  (active
                    ? !lastRunStatus?.run_id
                    : scopeUnavailable || scopeLoading || !selectedContext) ||
                  connection !== 'connected'
                }
                aria-label={sendLabel}
                title={active ? sendLabel : t('ui.send_question_enter')}
              >
                <ArrowUp size={20} />
              </button>
            </div>
          </form>
          <div className="composer-footnote">
            <span role="status">
              {connection !== 'connected' ? (
                <>
                  <span className="connection-dot reconnecting" />
                  {t('ui.reconnecting_automatically')}
                </>
              ) : providerStopped ? (
                t('ui.this_conversation_has_stopped')
              ) : active || aiUnavailable ? null : (
                <>
                  <span className="connection-dot connected" />
                  {t('ui.ready_for_your_question')}
                </>
              )}
            </span>
            {!aiUnavailable && (
              <span>
                {t('ui.enter_to_send')}
                <span className="keyboard-hint"> · {t('ui.shift_enter_for_a_new_line')}</span>
              </span>
            )}
          </div>
        </div>
      </footer>
      {!hasConversation && (
        <section className="agent-welcome">
          <div className="agent-welcome-heading">
            <MessageSquareText size={19} aria-hidden="true" />
            <h2>{t('ui.agent_suggestions')}</h2>
          </div>
          <p>{t('ui.edit_the_question_before_sending')}</p>
          <div className="agent-suggestions">
            {suggestions.slice(0, 3).map((suggestion) => (
              <button
                type="button"
                className="agent-suggestion"
                key={suggestion.id}
                disabled={Boolean(awaitingPrompt)}
                onClick={() => chooseSuggestion(suggestion)}
              >
                <span>
                  <strong>{suggestion.title}</strong>
                  <small>{suggestion.detail}</small>
                </span>
                <ArrowRight size={17} />
              </button>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
