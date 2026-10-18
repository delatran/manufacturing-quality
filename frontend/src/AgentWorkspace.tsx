import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
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
  createSession,
  getConfig,
  getDashboardContext,
  getSession,
  HttpError,
  request,
  socketUrl,
} from './api';
import EventView, { friendlyError } from './EventView';
import { createI18n, I18nScope, useI18n } from './i18n';
import { createSessionEventStream } from './stream';
import AgentFlow from './AgentFlow';
import {
  appendConversationState,
  createConversationState,
  eventsForReport,
} from './conversation-state';
import { conversationRun } from './conversation-projection';
import type { ConversationEntry } from './conversation-state';
import type { Locale } from './i18n';
import type { AppConfig, Session, WorkspaceEvent } from './types';
import { parseUiCommand } from './workspace-state';
import type { DashboardData } from './dashboard-types';
import type { UiCommand } from './workspace-state';
import AgentScope, { questionScope } from './AgentScope';
import { conversationScrollPosition } from './conversation-scroll';
import AnswerFollowups from './AnswerFollowups';
import { answerSourceQuestion, eligibleAnswerFollowup } from './answer-followups';
import { answerReplyAvailable, parseAnswerReply, questionForAnswer } from './answer-reply';
import type { AnswerReference, AnswerReply } from './answer-reply';
import {
  canonicalPromptRequest,
  clearPendingPromptRequest,
  createClientRequestId,
  parsePromptReceipt,
  promptRequestStorage,
  readPendingPromptRequest,
  samePromptRequest,
  savePendingPromptRequest,
} from './agent-prompt-requests';
import type { PromptReceipt, PromptRequest } from './agent-prompt-requests';
import { buildAgentActionTools } from './agent-actions-webmcp';
import { buildAgentResultTools } from './agent-result-webmcp';
import { commitUi, throwIfAborted, useWebMcpTools } from './webmcp';

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
type Draft = {
  text: string;
  contextId?: string;
  contextSummary?: string;
  reply?: AnswerReply;
  startSeparateRun?: boolean;
};
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
  replyTo?: AnswerReference,
  clientRequestId?: string,
  requestOrigin: 'ui' | 'webmcp' = 'ui',
) {
  if (replyTo && (steerRunId || actionIntent !== 'none'))
    throw new Error('An answer follow-up must start a separate analysis');
  if (actionIntent !== 'none' && !contextId && !steerRunId)
    throw new Error('Choose a dashboard context before preparing an action draft');
  return {
    type: 'prompt' as const,
    text,
    analysis_context_id: contextId,
    language,
    action_intent: actionIntent,
    ...(steerRunId ? { steer_run_id: steerRunId } : {}),
    ...(replyTo ? { reply_to: { run_id: replyTo.run_id, message_id: replyTo.message_id } } : {}),
    ...(clientRequestId
      ? { client_request_id: clientRequestId, request_origin: requestOrigin }
      : {}),
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
      reply: parseAnswerReply(value.reply),
      startSeparateRun: value.startSeparateRun === true,
    };
  } catch {
    return { text: '' };
  }
};

export { projectConversation } from './conversation-projection';

const ConversationEntryView = memo(function ConversationEntryView({
  entry,
  language,
  disabled,
  onFollowUp,
}: {
  entry: ConversationEntry;
  language: Locale;
  disabled: boolean;
  onFollowUp: (report: WorkspaceEvent, prompt: string, language: Locale) => void;
}) {
  const responseI18n = createI18n(language);
  const filters = entry.context?.data.filters;
  return (
    <I18nScope locale={language}>
      <div
        className="conversation-entry"
        lang={language}
        tabIndex={-1}
        role="group"
        aria-label={
          entry.type === 'message'
            ? entry.role === 'user'
              ? responseI18n.t('ui.you')
              : responseI18n.t('ui.quality_analyst_2')
            : responseI18n.t('ui.analyst_conversation')
        }
      >
        {entry.context && (
          <p className="report-scope">
            <span>{responseI18n.t('ui.based_on')}</span>
            {filters && typeof filters === 'object'
              ? responseI18n.scope(filters as Record<string, unknown>)
              : responseI18n.t('ui.the_data_selected_for_this_analysis')}
          </p>
        )}
        {entry.flow ? (
          <AgentFlow run={entry.flow} language={language} />
        ) : (
          <EventView event={entry.event} language={language} />
        )}
        {entry.type === 'message' && entry.role === 'assistant' && (
          <AnswerFollowups
            report={entry.event}
            question={entry.question}
            language={language}
            eligible={entry.eligible}
            disabled={disabled}
            onFollowUp={onFollowUp}
          />
        )}
      </div>
    </I18nScope>
  );
});

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
  onPrefillApplied,
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
  onPrefillApplied?: (id: number, contextId: string, applied: boolean) => void;
}) {
  const { locale, t, scope, number, percent } = useI18n();
  const [initialDraft] = useState(readDraft);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [conversation, setConversation] = useState(createConversationState);
  const [text, setText] = useState(initialDraft.text);
  const [prefillContext, setPrefillContext] = useState<string | undefined>(initialDraft.contextId);
  const [answerReply, setAnswerReply] = useState<AnswerReply | undefined>(initialDraft.reply);
  const [startSeparateRun, setStartSeparateRun] = useState(initialDraft.startSeparateRun === true);
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
  const [lastReceipt, setLastReceipt] = useState<PromptReceipt | null>(null);
  const [cancellation, setCancellation] = useState<{
    run_id: string;
    status: 'requested' | 'uncertain' | 'not_requested' | 'terminal';
  } | null>(null);
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
  const composer = useRef<HTMLTextAreaElement>(null);
  const stream = useRef<HTMLDivElement>(null);
  const conversationTail = useRef<HTMLDivElement>(null);
  const composerDock = useRef<HTMLElement>(null);
  const nearBottom = useRef(true);
  const pendingPrompt = useRef<string | null>(null);
  const pendingRequest = useRef<PromptRequest | null>(null);
  const pendingDraft = useRef<Draft | null>(null);
  const cancellationRun = useRef<string | null>(null);
  const checkedPrefill = useRef<number | undefined>(undefined);
  const bootRetry = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const {
    lastRunStatus: lastObservedRunStatus,
    hasConversation,
    lastQuestion,
    runContexts,
    visibleEvents,
  } = conversation;
  const activeRunStatus = useMemo(() => {
    let candidate: WorkspaceEvent | undefined;
    // Groups retain first-seen run order. Inspect one projected status per run,
    // so a late amendment to an older answer cannot retarget current controls.
    // This does not reread report bodies or run while the composer is edited.
    for (const group of conversation.groups.values()) {
      const status = group.displayStatus;
      if (status?.run_id && !terminalStates.has(String(status.data.state || 'idle')))
        candidate = status;
    }
    return candidate;
  }, [conversation.groups]);
  const lastRunStatus = activeRunStatus ?? lastObservedRunStatus;
  const receiptRunFinished = useMemo(() => {
    if (lastReceipt?.status !== 'accepted' || !lastReceipt.run_id) return false;
    const run = conversation.groups.get(
      conversationRun({ session_id: lastReceipt.session_id, run_id: lastReceipt.run_id }),
    );
    const runState =
      run?.displayStatus?.data.state ??
      conversation.savedRuns?.find((item) => item.id === lastReceipt.run_id)?.state;
    // Keep the durable admission receipt for readback without promising future
    // results after that specific run has already reached a terminal state.
    return typeof runState === 'string' && terminalStates.has(runState);
  }, [lastReceipt, conversation.groups, conversation.savedRuns]);
  const state = String(
    activeRunStatus?.data.state || conversation.lastStatus?.data.state || 'idle',
  );
  const active = !terminalStates.has(state);
  const providerStopped = conversation.providerStopped;
  const derivedActionValue = lastRunStatus?.run_id
    ? conversation.actionIntents.get(lastRunStatus.run_id)
    : undefined;
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
  const replyAvailable = answerReplyAvailable(
    answerReply,
    session,
    answerReply
      ? eventsForReport(conversation, {
          session_id: answerReply.sessionId,
          run_id: answerReply.run_id,
        })
      : conversation.events,
  );
  const selectedScope = questionScope({
    dashboardContextId: contextId,
    capturedContextId: prefillContext,
    dashboardContext,
    loadedContext: frozenContext,
    failedContextId,
  });
  const selectedContext = selectedScope.id;
  const composerLabel = answerReply
    ? locale === 'vi'
      ? 'Hỏi tiếp về câu trả lời đã chọn'
      : 'Follow up on the selected answer'
    : active && !startSeparateRun
      ? t('ui.agent_steer_current_work')
      : hasConversation
        ? t('ui.agent_next_question')
        : t('ui.your_analysis_question');
  const sendLabel =
    active && !answerReply && !startSeparateRun
      ? t('ui.agent_send_instruction')
      : t('ui.send_question');
  const capturedViewDiffers = selectedScope.captured;
  const scopeUnavailable = selectedScope.unavailable;
  const scopeLoading = selectedScope.loading;
  // Running status can arrive before its context. Wait for the recorded scope
  // so server admission can match it; an explicit saved null remains unscoped.
  const promptScopeUnavailable =
    active && !answerReply && !startSeparateRun
      ? !lastRunStatus?.run_id || runningContextId === undefined
      : scopeUnavailable || scopeLoading || !selectedContext;
  const selectedSummary = capturedViewDiffers
    ? scopeUnavailable
      ? t('ui.captured_scope_unavailable')
      : selectedScope.context
        ? scope(selectedScope.context.filters)
        : t('ui.the_earlier_data_selection_saved_with_this_question')
    : contextSummary || t('ui.the_current_dashboard_filters');
  const currentActionState = {
    session,
    conversation,
    visible,
    text,
    selectedContext,
    selectedSummary,
    prefillContext,
    answerReply,
    startSeparateRun,
    replyAvailable,
    active,
    providerStopped,
    aiUnavailable,
    connection,
    promptScopeUnavailable,
    runningContextId,
    runningLanguage,
    runningActionIntent,
    lastRunStatus,
    state,
    locale,
    cancelling,
    cancellation,
    lastReceipt,
    deliveryUncertain,
  };
  const committedActions = useRef(currentActionState);
  useLayoutEffect(() => {
    committedActions.current = currentActionState;
  });
  const applyPromptReceipt = useCallback((receipt: PromptReceipt) => {
    const current = committedActions.current;
    if (current.session?.id !== receipt.session_id) return;
    const pending = pendingRequest.current;
    if (
      pending &&
      pending.client_request_id === receipt.client_request_id &&
      !samePromptRequest(pending, receipt.request)
    ) {
      setDeliveryUncertain(true);
      setError(
        'The request key belongs to different prompt details. Read its receipt before continuing.',
      );
      return;
    }
    setLastReceipt(receipt);
    if (pending?.client_request_id !== receipt.client_request_id) return;
    if (receipt.status === 'reserved' || receipt.status === 'uncertain') {
      setDeliveryUncertain(receipt.status === 'uncertain');
      return;
    }
    const original = pendingDraft.current;
    clearPendingPromptRequest(
      promptRequestStorage(),
      receipt.session_id,
      receipt.client_request_id,
    );
    pendingPrompt.current = null;
    pendingRequest.current = null;
    pendingDraft.current = null;
    setAwaitingPrompt(null);
    setDeliveryUncertain(false);
    if (receipt.status === 'rejected') {
      if (original) {
        setText((previous) => previous || original.text);
        setPrefillContext(original.contextId);
        setAnswerReply(original.reply);
        setStartSeparateRun(original.startSeparateRun === true);
      }
      setError(receipt.message || 'The prompt was rejected before admission.');
    }
  }, []);
  const lookupPromptReceipt = useCallback(
    async (
      sessionId: string,
      clientRequestId: string,
      signal?: AbortSignal,
    ): Promise<PromptReceipt | undefined> => {
      try {
        const value = await request<unknown>(
          `/api/sessions/${encodeURIComponent(sessionId)}/prompt-requests/${encodeURIComponent(clientRequestId)}`,
          { signal },
        );
        const receipt = parsePromptReceipt(value, sessionId);
        if (!receipt || receipt.client_request_id !== clientRequestId)
          throw new Error('The prompt receipt could not be verified for this session and key.');
        return receipt;
      } catch (failure) {
        if (failure instanceof HttpError && failure.status === 404) return;
        throw failure;
      }
    },
    [],
  );
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
  const summaryFor = useCallback(
    (context?: WorkspaceEvent, language = locale) => {
      const contextI18n = createI18n(language);
      const filters = context?.data.filters;
      return filters && typeof filters === 'object'
        ? contextI18n.scope(filters as Record<string, unknown>)
        : contextI18n.t('ui.the_data_selected_for_this_analysis');
    },
    [locale],
  );

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

  useLayoutEffect(() => {
    if (!prefill) return;
    if (checkedPrefill.current === prefill.id) return;
    if (pendingRequest.current || !visible || prefill.contextId !== contextId) {
      checkedPrefill.current = prefill.id;
      onPrefillApplied?.(prefill.id, prefill.contextId, false);
      return;
    }
    if (
      text === prefill.text &&
      prefillContext === prefill.contextId &&
      !answerReply &&
      startSeparateRun
    ) {
      checkedPrefill.current = prefill.id;
      onPrefillApplied?.(prefill.id, prefill.contextId, true);
      return;
    }
    setText(prefill.text);
    setPrefillContext(prefill.contextId);
    setAnswerReply(undefined);
    setStartSeparateRun(true);
    composer.current?.focus();
  }, [
    prefill,
    visible,
    contextId,
    text,
    prefillContext,
    answerReply,
    startSeparateRun,
    onPrefillApplied,
  ]);

  useEffect(() => {
    const timer = setTimeout(() => {
      try {
        const savedDraft =
          awaitingPrompt && pendingDraft.current
            ? pendingDraft.current
            : {
                text,
                contextId: prefillContext,
                contextSummary: selectedSummary,
                reply: answerReply,
                startSeparateRun,
              };
        localStorage.setItem(draftKey, JSON.stringify(savedDraft));
      } catch {
        /* Drafts remain available in the current tab without storage. */
      }
    }, 200);
    return () => clearTimeout(timer);
  }, [text, prefillContext, selectedSummary, awaitingPrompt, answerReply, startSeparateRun]);

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
        setConversation(createConversationState(current.events || [], current.runs));
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
    const saved = readPendingPromptRequest(promptRequestStorage(), session.id);
    if (!saved) return;
    const value = saved.request;
    const reply = value.reply_to
      ? { ...value.reply_to, sessionId: session.id, language: value.language }
      : undefined;
    pendingRequest.current = value;
    pendingPrompt.current = value.text;
    pendingDraft.current = {
      text: value.text,
      contextId: value.analysis_context_id ?? undefined,
      reply,
      startSeparateRun: !value.steer_run_id,
    };
    setAwaitingPrompt(value.text);
    setText('');
    setAnswerReply(undefined);
    setDeliveryUncertain(true);
    const abort = new AbortController();
    // A reload only reads admission state. It never resends a paid request.
    void lookupPromptReceipt(session.id, value.client_request_id, abort.signal)
      .then((receipt) => {
        if (!abort.signal.aborted && receipt) applyPromptReceipt(receipt);
      })
      .catch(() => {
        if (!abort.signal.aborted) setDeliveryUncertain(true);
      });
    return () => abort.abort();
  }, [session, applyPromptReceipt, lookupPromptReceipt]);

  useEffect(() => {
    if (!session) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    let attempt = 0;
    const eventStream = createSessionEventStream(session.id, session.events || [], (batch) => {
      if (disposed) return;
      setConversation((previous) => appendConversationState(previous, batch));
      batch.forEach((event) => {
        if (event.type === 'ui_command') {
          const command = parseUiCommand(event.data);
          if (command && uiCommandHandler.current) {
            void applyCommand(command)
              .then((applied) => {
                if (disposed) return;
                setCommandFeedback(applied ? 'applied' : 'stale');
                if (applied) setLastCommand(command.action === 'undo' ? undefined : command);
              })
              .catch(() => {
                if (!disposed) setCommandFeedback('failed');
              });
          }
        }
        if (
          event.type === 'status' &&
          terminalStates.has(String(event.data.state)) &&
          event.run_id === cancellationRun.current
        ) {
          setCancelling(false);
          setCancellation({ run_id: event.run_id, status: 'terminal' });
          cancellationRun.current = null;
        }
        if (event.type === 'prompt_receipt') {
          const receipt = parsePromptReceipt(event.data, session.id);
          if (receipt) applyPromptReceipt(receipt);
        }
        if (
          event.type === 'error' &&
          pendingRequest.current &&
          event.data.client_request_id === pendingRequest.current.client_request_id
        ) {
          setDeliveryUncertain(true);
          setError(
            String(event.data.message || 'The request needs receipt readback before another send.'),
          );
        }
      });
    });
    const connect = () => {
      if (disposed) return;
      const ws = new WebSocket(socketUrl(session.id, eventStream.cursor));
      socket.current = ws;
      ws.onopen = () => {
        if (disposed || socket.current !== ws) return;
        attempt = 0;
        setConnection('connected');
        setError((previous) => (/^Connection|An unreadable/.test(previous) ? '' : previous));
      };
      ws.onmessage = (message) => {
        if (disposed || socket.current !== ws) return;
        try {
          eventStream.receive(JSON.parse(message.data));
        } catch {
          setError('An unreadable event was received');
        }
      };
      ws.onerror = () => {
        if (!disposed && socket.current === ws) ws.close();
      };
      ws.onclose = () => {
        if (disposed || socket.current !== ws) return;
        socket.current = null;
        setConnection('reconnecting');
        if (pendingPrompt.current) setDeliveryUncertain(true);
        timer = setTimeout(connect, Math.min(1000 * 2 ** attempt++, 10000));
      };
    };
    connect();
    return () => {
      disposed = true;
      eventStream.dispose();
      clearTimeout(timer);
      socket.current?.close();
      socket.current = null;
    };
  }, [session, applyCommand, applyPromptReceipt]);
  useEffect(() => {
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

  function draftRequest(clientRequestId: string, origin: 'ui' | 'webmcp'): PromptRequest {
    const current = committedActions.current;
    const steering = current.active && !current.answerReply && !current.startSeparateRun;
    return {
      text: current.text.trim(),
      analysis_context_id: steering ? current.runningContextId : current.selectedContext,
      language:
        current.answerReply?.language ??
        (steering && (current.runningLanguage === 'en' || current.runningLanguage === 'vi')
          ? current.runningLanguage
          : current.locale),
      action_intent: steering ? current.runningActionIntent : 'none',
      ...(steering ? { steer_run_id: current.lastRunStatus?.run_id } : {}),
      ...(current.answerReply
        ? {
            reply_to: {
              run_id: current.answerReply.run_id,
              message_id: current.answerReply.message_id,
            },
          }
        : {}),
      client_request_id: clientRequestId,
      request_origin: origin,
    };
  }
  function pendingResult(value: PromptRequest) {
    const current = committedActions.current;
    return {
      status: current.deliveryUncertain ? 'uncertain' : 'awaiting_acknowledgement',
      session_id: current.session?.id,
      client_request_id: value.client_request_id,
      action: value.steer_run_id ? 'steer' : 'start',
      analysis_completed: false,
      next_action:
        'Read the receipt for this same client_request_id. Do not create a replacement key.',
    };
  }
  async function readPromptReceipt(clientRequestId: string, signal?: AbortSignal) {
    const current = committedActions.current;
    if (!current.session || !current.visible) throw new Error('The Agent workspace is not ready.');
    const receipt = await lookupPromptReceipt(current.session.id, clientRequestId, signal);
    throwIfAborted(signal);
    if (committedActions.current.session?.id !== current.session.id)
      throw new Error('The mounted Agent session changed.');
    await commitUi(() => {
      if (receipt) applyPromptReceipt(receipt);
      else if (pendingRequest.current?.client_request_id === clientRequestId)
        setDeliveryUncertain(true);
    }, signal);
    return (
      receipt ?? {
        status: 'not_found',
        session_id: current.session.id,
        client_request_id: clientRequestId,
        delivery:
          pendingRequest.current?.client_request_id === clientRequestId ? 'uncertain' : 'unknown',
        analysis_completed: false,
        next_action:
          'Read this same key again when connectivity is restored. No request was replayed.',
      }
    );
  }
  async function dispatchPrompt(value: PromptRequest, signal?: AbortSignal) {
    const before = committedActions.current;
    if (!before.session) throw new Error('The Agent session is not ready.');
    throwIfAborted(signal);
    // Durable recognition precedes transient run, provider and draft guards.
    // A deliberate UI send has a freshly created UUID; external callers must read first.
    if (value.request_origin === 'webmcp') {
      const receipt = await lookupPromptReceipt(before.session.id, value.client_request_id, signal);
      if (receipt) {
        if (committedActions.current.session?.id !== before.session.id)
          throw new Error('The mounted Agent session changed.');
        if (!samePromptRequest(value, receipt.request))
          throw new Error('This request key is already bound to different prompt details.');
        await commitUi(() => applyPromptReceipt(receipt), signal);
        return receipt;
      }
    }
    throwIfAborted(signal);
    const current = committedActions.current;
    if (current.session?.id !== before.session.id || !current.visible)
      throw new Error('The mounted Agent session changed or is not visible.');
    const pending = pendingRequest.current;
    if (pending?.client_request_id === value.client_request_id) {
      if (!samePromptRequest(value, pending))
        throw new Error('This pending request key is bound to different prompt details.');
      return pendingResult(pending);
    }
    if (pending) throw new Error('Resolve the pending request receipt before another send.');
    if (current.providerStopped || current.aiUnavailable)
      throw new Error('This Agent session cannot accept another request.');
    if (!current.replyAvailable || current.promptScopeUnavailable)
      throw new Error('The selected source answer or immutable context is unavailable.');
    if (
      !value.text.trim() ||
      !samePromptRequest(value, draftRequest(value.client_request_id, value.request_origin))
    )
      throw new Error(
        'The request no longer matches the visible staged prompt, language, context or active run. Read status and stage it again.',
      );
    const ws = socket.current;
    if (!ws || ws.readyState !== WebSocket.OPEN)
      throw new Error('Connection lost before the prompt was sent.');
    const sentRequest = {
      ...canonicalPromptRequest(value),
      client_request_id: value.client_request_id,
    };
    let sent = false;
    try {
      await commitUi(() => {
        throwIfAborted(signal);
        pendingPrompt.current = sentRequest.text;
        pendingRequest.current = sentRequest;
        pendingDraft.current = {
          text: sentRequest.text,
          contextId: sentRequest.steer_run_id
            ? current.prefillContext
            : (sentRequest.analysis_context_id ?? undefined),
          contextSummary: current.selectedSummary,
          reply: current.answerReply,
          startSeparateRun: current.startSeparateRun,
        };
        savePendingPromptRequest(promptRequestStorage(), {
          session_id: before.session!.id,
          request: sentRequest,
        });
        ws.send(
          JSON.stringify(
            buildPromptMessage(
              sentRequest.text,
              sentRequest.language,
              sentRequest.analysis_context_id ?? undefined,
              sentRequest.action_intent,
              sentRequest.steer_run_id ?? undefined,
              sentRequest.reply_to ?? undefined,
              sentRequest.client_request_id,
              sentRequest.request_origin,
            ),
          ),
        );
        sent = true;
        setError('');
        setLastReceipt(null);
        setDeliveryUncertain(false);
        setAwaitingPrompt(sentRequest.text);
        setText('');
        setAnswerReply(undefined);
        setStartSeparateRun(false);
        if (!sentRequest.steer_run_id) setPrefillContext(undefined);
        nearBottom.current = true;
        composer.current?.focus();
      }, signal);
    } catch (failure) {
      if (sent) {
        // Aborting the invocation cannot retract a message already dispatched.
        setDeliveryUncertain(true);
      } else {
        clearPendingPromptRequest(
          promptRequestStorage(),
          before.session.id,
          sentRequest.client_request_id,
        );
        pendingPrompt.current = null;
        pendingRequest.current = null;
        pendingDraft.current = null;
        setAwaitingPrompt(null);
      }
      throw failure;
    }
    const receipt = committedActions.current.lastReceipt;
    return receipt?.client_request_id === sentRequest.client_request_id
      ? receipt
      : pendingResult(sentRequest);
  }
  async function submit(event?: FormEvent) {
    event?.preventDefault();
    if (pendingRequest.current || !committedActions.current.text.trim()) return;
    try {
      await dispatchPrompt(draftRequest(createClientRequestId(), 'ui'));
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
    stageDraft(suggestion.prompt, selectedContext);
  }
  function stageDraft(prompt: string, draftContext: string | undefined) {
    if (pendingRequest.current) return false;
    setText(prompt);
    setPrefillContext(draftContext);
    setAnswerReply(undefined);
    setStartSeparateRun(false);
    composer.current?.focus();
    return true;
  }
  const committedConversation = useRef({ conversation, awaitingPrompt });
  useLayoutEffect(() => {
    committedConversation.current = { conversation, awaitingPrompt };
  }, [conversation, awaitingPrompt]);
  const followUp = useCallback((report: WorkspaceEvent, prompt: string, language: 'en' | 'vi') => {
    const current = committedConversation.current;
    const ownEvents = eventsForReport(current.conversation, report);
    if (
      current.awaitingPrompt ||
      pendingPrompt.current ||
      !eligibleAnswerFollowup(report, ownEvents, current.conversation.savedRuns)
    )
      return false;
    const context = report.run_id ? current.conversation.runContexts.get(report.run_id) : undefined;
    const id = context?.data.context_id;
    if (typeof id !== 'string' || !report.run_id || typeof report.id !== 'number') return false;
    setText(prompt);
    setPrefillContext(id);
    setStartSeparateRun(false);
    setAnswerReply({
      sessionId: report.session_id,
      run_id: report.run_id,
      message_id: report.id,
      language,
      question: answerSourceQuestion(report, questionForAnswer(report, ownEvents)),
    });
    composer.current?.focus();
    return true;
  }, []);

  function agentStatus() {
    const current = committedActions.current;
    if (!current.visible) throw new Error('The Agent workspace is not visible.');
    const draft = draftRequest('00000000-0000-0000-0000-000000000000', 'webmcp');
    return {
      session_id: current.session?.id ?? null,
      connection: current.connection,
      available: !current.aiUnavailable && !current.providerStopped,
      provider_stopped: current.providerStopped,
      run_state: current.state,
      active_run: current.active
        ? {
            run_id: current.lastRunStatus?.run_id ?? null,
            analysis_context_id: current.runningContextId ?? null,
            context_resolved: current.runningContextId !== undefined,
            language: current.runningLanguage ?? null,
            action_intent: current.runningActionIntent,
          }
        : null,
      draft: {
        ...canonicalPromptRequest(draft),
        mode: draft.reply_to ? 'answer_followup' : draft.steer_run_id ? 'steer' : 'start',
        source_available: current.replyAvailable && !current.promptScopeUnavailable,
      },
      pending_request: pendingRequest.current ? pendingResult(pendingRequest.current) : null,
      last_receipt: current.lastReceipt
        ? {
            client_request_id: current.lastReceipt.client_request_id,
            status: current.lastReceipt.status,
            run_id: current.lastReceipt.run_id,
            queued: current.lastReceipt.queued ?? false,
          }
        : null,
      cancellation: current.cancellation,
      result_exports_available: false,
    };
  }
  async function stageAgentPrompt(
    prompt: string,
    requestedContext: string | null,
    signal?: AbortSignal,
  ) {
    const current = committedActions.current;
    if (!current.visible || pendingRequest.current)
      throw new Error('The composer is not available for staging.');
    const expected = current.active ? current.runningContextId : current.selectedContext;
    if (expected === undefined || (expected ?? null) !== requestedContext)
      throw new Error("The requested context is not the composer's known current context.");
    await commitUi(() => {
      if (!stageDraft(prompt, requestedContext ?? undefined))
        throw new Error('A prompt send is pending.');
    }, signal);
    const committed = committedActions.current;
    if (
      committed.text !== prompt ||
      committed.answerReply ||
      (committed.active ? committed.runningContextId : committed.selectedContext) !==
        requestedContext
    )
      throw new Error('The draft or active context changed before staging completed.');
    return { status: 'staged', submitted: false, ...agentStatus() };
  }
  async function stageAnswerFollowup(
    reference: AnswerReference,
    prompt: string,
    signal?: AbortSignal,
  ) {
    const current = committedActions.current;
    if (!current.visible || !current.session) throw new Error('The Agent workspace is not ready.');
    const report = current.conversation.entries.find(
      (entry) =>
        entry.event.session_id === current.session?.id &&
        entry.event.run_id === reference.run_id &&
        entry.event.id === reference.message_id &&
        entry.eligible,
    )?.event;
    if (!report)
      throw new Error('The selected answer is not verified and eligible in this session.');
    const language = current.conversation.runLanguages.get(reference.run_id) ?? current.locale;
    await commitUi(() => {
      if (!followUp(report, prompt, language))
        throw new Error('The answer is no longer eligible or a prompt send is pending.');
    }, signal);
    const committed = committedActions.current;
    if (
      committed.text !== prompt ||
      !committed.replyAvailable ||
      committed.answerReply?.run_id !== reference.run_id ||
      committed.answerReply?.message_id !== reference.message_id
    )
      throw new Error('The selected answer or draft changed before staging completed.');
    return { status: 'staged', submitted: false, ...agentStatus() };
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
  async function cancelAnalysis(requestedRunId?: string, signal?: AbortSignal) {
    const current = committedActions.current;
    const runId = requestedRunId ?? current.lastRunStatus?.run_id;
    if (
      !current.session ||
      !current.visible ||
      !runId ||
      !current.active ||
      current.lastRunStatus?.run_id !== runId
    )
      throw new Error('The requested analysis is not the current active run.');
    throwIfAborted(signal);
    if (current.cancelling && cancellationRun.current === runId)
      return {
        status: current.cancellation?.status ?? 'requested',
        run_id: runId,
        provider_stopped: false,
      };
    const sessionId = current.session.id;
    let dispatched = false;
    try {
      await commitUi(() => {
        cancellationRun.current = runId;
        setCancelling(true);
        setCancellation({ run_id: runId, status: 'requested' });
      }, signal);
      throwIfAborted(signal);
      dispatched = true;
      const result = await request<{ cancel_requested: boolean; run_id: string | null }>(
        `/api/sessions/${encodeURIComponent(sessionId)}/cancel`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ run_id: runId }),
        },
      );
      if (typeof result.cancel_requested !== 'boolean' || result.run_id !== runId)
        throw new Error('The cancellation response could not be verified.');
      const observed = committedActions.current.cancellation;
      const terminal = observed?.run_id === runId && observed.status === 'terminal';
      const status = terminal
        ? 'terminal'
        : result.cancel_requested
          ? 'requested'
          : 'not_requested';
      await commitUi(() => {
        if (committedActions.current.session?.id !== sessionId) return;
        if (committedActions.current.cancellation?.run_id !== runId) return;
        setCancellation({ run_id: runId, status });
        if (!result.cancel_requested || terminal) {
          setCancelling(false);
          cancellationRun.current = null;
        }
      });
      return {
        status,
        session_id: sessionId,
        run_id: runId,
        cancel_requested: result.cancel_requested,
        provider_stopped: false,
        completion_observed: terminal,
      };
    } catch (failure) {
      const observed = committedActions.current.cancellation;
      const terminal = observed?.run_id === runId && observed.status === 'terminal';
      await commitUi(() => {
        if (committedActions.current.session?.id !== sessionId) return;
        if (committedActions.current.cancellation?.run_id !== runId) return;
        setError(description(failure));
        setCancelling(false);
        setCancellation({
          run_id: runId,
          status: terminal ? 'terminal' : dispatched ? 'uncertain' : 'not_requested',
        });
        if (!dispatched) cancellationRun.current = null;
      });
      if (!dispatched) throw failure;
      return {
        status: terminal ? 'terminal' : 'uncertain',
        session_id: sessionId,
        run_id: runId,
        provider_stopped: false,
        completion_observed: terminal,
      };
    }
  }
  useWebMcpTools(
    [
      ...buildAgentActionTools({
        getStatus: agentStatus,
        stagePrompt: stageAgentPrompt,
        stageFollowup: stageAnswerFollowup,
        submit: dispatchPrompt,
        readReceipt: readPromptReceipt,
        cancel: cancelAnalysis,
      }),
      ...buildAgentResultTools({
        getSnapshot: () => ({
          session: committedActions.current.session,
          conversation: committedActions.current.conversation,
          dashboardContextId: contextId,
          visible: committedActions.current.visible,
        }),
        getRoot: () => stream.current?.closest<HTMLElement>('.agent-workspace') ?? null,
      }),
    ],
    visible,
  );
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
  const followupsDisabled =
    Boolean(awaitingPrompt) || providerStopped || aiUnavailable || connection !== 'connected';
  const conversationEntries = useMemo(
    () =>
      conversation.entries.map((entry) => (
        <ConversationEntryView
          key={String(entry.event.id)}
          entry={entry}
          language={entry.language ?? locale}
          disabled={followupsDisabled}
          onFollowUp={followUp}
        />
      )),
    [conversation.entries, locale, followupsDisabled, followUp],
  );

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
                    setAnswerReply(undefined);
                    setStartSeparateRun(true);
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
          {conversationEntries}
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
                const reference = lastQuestion.data.reply_to;
                const restored =
                  reference && typeof reference === 'object'
                    ? parseAnswerReply({
                        ...reference,
                        sessionId: lastQuestion.session_id,
                        language: lastQuestion.data.language,
                      })
                    : undefined;
                if (reference && !restored) {
                  setError(
                    locale === 'vi'
                      ? 'Không thể khôi phục liên kết câu trả lời của yêu cầu này.'
                      : 'The answer reference for this question could not be restored.',
                  );
                  return;
                }
                setText(String(lastQuestion.data.text || ''));
                setAnswerReply(restored);
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
            <div
              className="composer-delivery-note"
              role="alert"
              data-client-request-id={pendingRequest.current?.client_request_id}
            >
              <p>{t('ui.delivery_has_not_been_confirmed_check_the_conversation_before_sen')}</p>
              <button
                type="button"
                onClick={() => {
                  const key = pendingRequest.current?.client_request_id;
                  if (key)
                    void readPromptReceipt(key).catch((failure: unknown) =>
                      setError(description(failure)),
                    );
                }}
              >
                {locale === 'vi' ? 'Kiểm tra trạng thái yêu cầu' : 'Check request status'}
              </button>
              <details>
                <summary>{locale === 'vi' ? 'Mã tra cứu yêu cầu' : 'Request reference'}</summary>
                <code>{pendingRequest.current?.client_request_id}</code>
              </details>
            </div>
          )}
          {lastReceipt && !awaitingPrompt && !receiptRunFinished && (
            <p
              className="composer-delivery-note"
              role="status"
              data-agent-request-status={lastReceipt.status}
            >
              {lastReceipt.status === 'accepted'
                ? lastReceipt.queued
                  ? locale === 'vi'
                    ? 'Yêu cầu đã được xếp hàng cho một lượt phân tích riêng.'
                    : 'The question is queued for a separate analysis.'
                  : locale === 'vi'
                    ? 'Yêu cầu đã được nhận. Kết quả sẽ xuất hiện trong hội thoại.'
                    : 'The request was accepted. Results will appear in the conversation.'
                : lastReceipt.status === 'rejected'
                  ? locale === 'vi'
                    ? 'Yêu cầu chưa được nhận. Xem thông báo và kiểm tra lại câu hỏi.'
                    : 'The request was rejected. Review the message and question.'
                  : locale === 'vi'
                    ? 'Chưa xác định được trạng thái nhận yêu cầu.'
                    : 'Request admission has not been confirmed.'}
            </p>
          )}
          {cancellation?.status === 'uncertain' && (
            <p className="composer-delivery-note" role="alert">
              {locale === 'vi'
                ? 'Chưa xác nhận được yêu cầu dừng. Phân tích có thể vẫn đang chạy.'
                : 'The cancellation request is unconfirmed. Analysis may still be running.'}
            </p>
          )}
          {active && (
            <div className="agent-work-status">
              <button
                type="button"
                className="agent-cancel-button"
                disabled={cancelling}
                onClick={() =>
                  void cancelAnalysis().catch((failure: unknown) => setError(description(failure)))
                }
              >
                <Square size={12} fill="currentColor" aria-hidden="true" />
                {cancelling ? t('ui.stopping_analysis') : t('ui.stop_analysis')}
              </button>
            </div>
          )}
          <form className={'composer ' + (active ? 'is-active' : '')} onSubmit={submit}>
            {active && startSeparateRun && !answerReply && (
              <p className="answer-reply-context" role="status">
                {locale === 'vi'
                  ? 'Câu hỏi từ Dashboard sẽ được xếp cho một lượt phân tích riêng với dữ liệu đã chọn.'
                  : 'This Dashboard question will queue for a separate analysis using the selected data.'}
              </p>
            )}
            {answerReply && (
              <div className="answer-reply-context" role="status">
                <div>
                  <span className="answer-reply-label">
                    {locale === 'vi'
                      ? 'Hỏi tiếp về câu trả lời đã chọn'
                      : 'Following up on the selected answer'}
                  </span>
                  {answerReply.question && (
                    <p className="answer-reply-question">{answerReply.question}</p>
                  )}
                  {!replyAvailable && (
                    <p>
                      {locale === 'vi'
                        ? 'Chưa xác minh được câu trả lời gốc. Hãy chờ tải hội thoại hoặc bỏ liên kết này để đặt câu hỏi mới.'
                        : 'The source answer is unavailable. Wait for the conversation to load, or clear this reference to ask a new question.'}
                    </p>
                  )}
                  {active && (
                    <p>
                      {locale === 'vi'
                        ? 'Yêu cầu này sẽ được xếp cho một lượt riêng sau lượt đang chạy.'
                        : 'This question will queue for a separate analysis after the current run.'}
                    </p>
                  )}
                </div>
                <button
                  type="button"
                  className="answer-reply-clear"
                  aria-label={
                    locale === 'vi' ? 'Bỏ liên kết câu trả lời' : 'Clear answer reference'
                  }
                  onClick={() => {
                    setAnswerReply(undefined);
                    setPrefillContext(undefined);
                    setStartSeparateRun(false);
                    composer.current?.focus();
                  }}
                >
                  <X size={16} />
                </button>
              </div>
            )}
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
                active && !answerReply && !startSeparateRun
                  ? t('ui.add_a_detail_or_ask_a_follow_up')
                  : t('ui.ask_a_question_about_your_production_data')
              }
              rows={2}
              disabled={providerStopped || Boolean(awaitingPrompt)}
            />
            <div className="composer-bottom">
              <div className="composer-context">
                <span>
                  {answerReply
                    ? locale === 'vi'
                      ? 'Dùng dữ liệu đã chốt của câu trả lời này'
                      : "Using this answer's saved data"
                    : t('ui.using_selected_dashboard_data')}
                </span>
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
                  !replyAvailable ||
                  promptScopeUnavailable ||
                  connection !== 'connected'
                }
                aria-label={sendLabel}
                title={
                  active && !answerReply && !startSeparateRun
                    ? sendLabel
                    : t('ui.send_question_enter')
                }
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
