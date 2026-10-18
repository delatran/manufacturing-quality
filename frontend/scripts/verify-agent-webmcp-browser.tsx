// Isolated browser fixture. HTTP, WebSocket and WebMCP registration are mocked.
// The real mounted AgentWorkspace, action callbacks, state and DOM are exercised.
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import AgentWorkspace from '../src/AgentWorkspace';
import { I18nScope } from '../src/i18n';
import type { Locale } from '../src/i18n';
import { initialFilters } from '../src/dashboard-types';
import type { DashboardData } from '../src/dashboard-types';
import { canonicalPromptRequest, pendingPromptStorageKey } from '../src/agent-prompt-requests';
import type { PromptReceipt, PromptRequest } from '../src/agent-prompt-requests';
import type { Session, WorkspaceEvent } from '../src/types';
import type { WebMcpTool } from '../src/webmcp';

function check(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}
const sid = 'webmcp-agent-browser-fixture';
const contextId = 'c'.repeat(64);
const otherContextId = 'd'.repeat(64);
const originalRun = 'a'.repeat(32);
const activeRun = 'b'.repeat(32);
const receiptRun = 'e'.repeat(32);
const context = (id: string): DashboardData['context'] => ({
  context_id: id,
  snapshot_id: 'synthetic',
  filters: initialFilters,
  source_sha256: 'synthetic',
  master_sha256: 'synthetic',
  code_sha256: 'synthetic',
  kpi_version: 'fixture',
  fetched_at: '2026-10-19T00:00:00Z',
});
const event = (
  id: number,
  type: string,
  data: Record<string, unknown>,
  run_id?: string,
): WorkspaceEvent => ({
  id,
  type,
  session_id: sid,
  time: '2026-10-19T00:00:00Z',
  data,
  ...(run_id ? { run_id } : {}),
});

export async function runAgentWebMcpVerification() {
  const host = document.createElement('div');
  document.body.append(host);
  const root = createRoot(host);
  const savedFetch = window.fetch;
  const savedSocket = window.WebSocket;
  const descriptor = Object.getOwnPropertyDescriptor(document, 'modelContext');
  const storageKeys = [
    'analyst.workspace.session.v1',
    'analyst.workspace.draft.v2',
    pendingPromptStorageKey,
  ];
  const savedStorage = storageKeys.map((key) => [key, localStorage.getItem(key)] as const);
  const environment = globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean };
  const savedAct = environment.IS_REACT_ACT_ENVIRONMENT;
  environment.IS_REACT_ACT_ENVIRONMENT = true;
  storageKeys.forEach((key) => localStorage.removeItem(key));
  localStorage.setItem(storageKeys[0], sid);
  const registry = new Map<string, WebMcpTool>();
  Object.defineProperty(document, 'modelContext', {
    configurable: true,
    value: {
      registerTool(tool: WebMcpTool, options?: { signal?: AbortSignal }) {
        check(!registry.has(tool.name), `Unique tool registration: ${tool.name}`);
        registry.set(tool.name, tool);
        options?.signal?.addEventListener(
          'abort',
          () => {
            if (registry.get(tool.name) === tool) registry.delete(tool.name);
          },
          { once: true },
        );
        return Promise.resolve();
      },
    },
  });
  const history = [
    event(1, 'message', {
      role: 'user',
      text: 'Explain this synthetic finding.',
      analysis_context_id: contextId,
      language: 'en',
    }),
    event(
      2,
      'run_started',
      { question: 'Explain this synthetic finding.', language: 'en' },
      originalRun,
    ),
    event(
      3,
      'analysis_context',
      { context_id: contextId, filters: initialFilters, language: 'en' },
      originalRun,
    ),
    event(
      4,
      'message',
      { role: 'assistant', text: 'A synthetic explanation.', qa: { verified: true } },
      originalRun,
    ),
    event(5, 'status', { state: 'completed', language: 'en' }, originalRun),
  ];
  const session: Session = {
    id: sid,
    events: history,
    runs: [{ id: originalRun, state: 'completed', analysis_context_id: contextId, language: 'en' }],
  };
  const receipts = new Map<string, PromptReceipt>();
  const sent: PromptRequest[] = [];
  let abortAfterSend: AbortController | undefined;
  let cancelRequests = 0;
  const sockets: FixtureSocket[] = [];
  class FixtureSocket {
    static OPEN = 1;
    readyState = 0;
    onopen: ((event: Event) => void) | null = null;
    onmessage: ((event: MessageEvent) => void) | null = null;
    onerror: ((event: Event) => void) | null = null;
    onclose: ((event: CloseEvent) => void) | null = null;
    constructor(public readonly url: string) {
      sockets.push(this);
    }
    open() {
      this.readyState = 1;
      this.onopen?.(new Event('open'));
    }
    send(raw: string) {
      check(this.readyState === 1, 'WebSocket is open for a send');
      sent.push(JSON.parse(raw) as PromptRequest);
      abortAfterSend?.abort();
      abortAfterSend = undefined;
    }
    receive(value: unknown) {
      this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(value) }));
    }
    close() {
      this.readyState = 3;
      this.onclose?.(new CloseEvent('close'));
    }
  }
  window.WebSocket = FixtureSocket as unknown as typeof WebSocket;
  window.fetch = async (input, init) => {
    const url = new URL(String(input), location.href);
    if (url.pathname === '/api/config')
      return Response.json({ mode: 'simulation', model: 'synthetic', ai_available: true });
    if (url.pathname === `/api/sessions/${sid}`) return Response.json(session);
    if (url.pathname === `/api/sessions/${sid}/scope`) return Response.json({});
    if (url.pathname.startsWith(`/api/sessions/${sid}/prompt-requests/`)) {
      const key = url.pathname.split('/').at(-1)!;
      const receipt = receipts.get(key);
      return receipt
        ? Response.json(receipt)
        : Response.json({ detail: 'not found' }, { status: 404 });
    }
    if (url.pathname === `/api/sessions/${sid}/cancel`) {
      cancelRequests += 1;
      const body = JSON.parse(String(init?.body)) as { run_id: string };
      return Response.json({ cancel_requested: true, run_id: body.run_id });
    }
    if (url.pathname.startsWith('/api/dashboard/contexts/')) {
      const id = url.pathname.split('/').at(-1)!;
      return Response.json({ context: context(id), analysis: { kpis: null } });
    }
    throw new Error(`Unexpected isolated fixture request: ${url.pathname}`);
  };
  const settle = async () => {
    await act(async () => new Promise((resolve) => setTimeout(resolve, 40)));
  };
  const waitFor = async (condition: () => boolean) => {
    const until = Date.now() + 5000;
    while (!condition() && Date.now() < until) await settle();
    check(condition(), 'The mounted fixture reached the expected state');
  };
  let locale: Locale = 'en';
  let dashboardId = contextId;
  let mountKey = 'first';
  let visible = true;
  let prefill: { id: number; text: string; contextId: string } | undefined;
  const prefillReceipts: { id: number; contextId: string; applied: boolean }[] = [];
  const onPrefillApplied = (id: number, idOfContext: string, applied: boolean) => {
    prefillReceipts.push({ id, contextId: idOfContext, applied });
  };
  const render = async () => {
    await act(async () =>
      root.render(
        <I18nScope locale={locale}>
          <AgentWorkspace
            key={mountKey}
            visible={visible}
            contextId={dashboardId}
            dashboardContext={context(dashboardId)}
            prefill={prefill}
            onPrefillApplied={onPrefillApplied}
          />
        </I18nScope>,
      ),
    );
  };
  const execute = async (name: string, input: unknown, signal?: AbortSignal) => {
    const tool = registry.get(name);
    check(tool, `Registered tool: ${name}`);
    let result: unknown;
    await act(async () => {
      result = await tool.execute(input, { signal });
    });
    return result as Record<string, unknown>;
  };
  const reject = async (name: string, input: unknown, signal?: AbortSignal) => {
    let rejected = false;
    try {
      await execute(name, input, signal);
    } catch {
      rejected = true;
    }
    check(rejected, `${name} rejected invalid or unavailable work`);
  };
  let eventId = 10;
  const receive = async (type: string, data: Record<string, unknown>, runId?: string) => {
    await act(async () => sockets.at(-1)!.receive(event(eventId++, type, data, runId)));
    await settle();
  };
  const makeReceipt = (
    prompt: PromptRequest,
    status: PromptReceipt['status'] = 'accepted',
    extra: Partial<PromptReceipt> = {},
  ): PromptReceipt => ({
    client_request_id: prompt.client_request_id,
    session_id: sid,
    status,
    run_id: prompt.steer_run_id ?? null,
    action: prompt.steer_run_id ? 'steer' : 'start',
    request: canonicalPromptRequest(prompt),
    request_sha256: 'e'.repeat(64),
    replayed: false,
    ...extra,
  });
  const acknowledge = async (prompt: PromptRequest, extra: Partial<PromptReceipt> = {}) => {
    const receipt = makeReceipt(prompt, 'accepted', extra);
    receipts.set(prompt.client_request_id, receipt);
    await receive('prompt_receipt', receipt, receipt.run_id ?? undefined);
  };
  const key = (index: number) => `00000000-0000-4000-8000-${String(index).padStart(12, '0')}`;
  const submission = (index: number, text: string, id = contextId, language = 'en') => ({
    client_request_id: key(index),
    text,
    analysis_context_id: id,
    language,
  });
  try {
    await render();
    await waitFor(() => sockets.length === 1 && registry.has('read_agent_status'));
    await act(async () => sockets[0].open());
    check(registry.size === 11, 'Seven action tools and four result tools are registered');
    const staged = await execute('stage_agent_prompt', {
      text: 'Explain the source.',
      analysis_context_id: contextId,
    });
    check(
      staged.status === 'staged' && host.querySelector('textarea')?.value === 'Explain the source.',
      'Staging returns only after the draft is visible',
    );
    const aborted = new AbortController();
    aborted.abort();
    await reject('submit_agent_prompt', submission(1, 'Explain the source.'), aborted.signal);
    check(Number(sent.length) === 0, 'Aborting before submit causes no send');
    const first = await execute('submit_agent_prompt', submission(1, 'Explain the source.'));
    check(
      first.status === 'awaiting_acknowledgement' && sent.length === 1,
      'Submission is sent once and honestly awaits admission',
    );
    check(host.querySelector('.pending-message'), 'The submitted question is visibly pending');
    check(
      localStorage.getItem(pendingPromptStorageKey)?.includes(key(1)),
      'Pending identity is retained before acknowledgement',
    );
    await execute('submit_agent_prompt', submission(1, 'Explain the source.'));
    await reject('submit_agent_prompt', submission(1, 'Conflicting text.'));
    await reject('submit_agent_prompt', submission(2, 'Explain the source.'));
    check(
      sent.length === 1,
      'Repeated, conflicting and competing submissions cannot silently launch another run',
    );
    await receive('message', { ...sent[0], role: 'user' });
    check(
      host.querySelector('.pending-message'),
      'A user message alone cannot substitute for a durable admission receipt',
    );
    prefill = { id: 1, text: 'Do not overwrite pending work.', contextId };
    await render();
    check(
      prefillReceipts.at(-1)?.applied === false,
      'Dashboard staging refuses to replace a pending send',
    );
    await acknowledge(sent[0], { run_id: receiptRun });
    check(
      !host.querySelector('.pending-message') && !localStorage.getItem(pendingPromptStorageKey),
      'Accepted receipt clears the pending state and key',
    );
    check(
      host.querySelector('textarea')?.value === '',
      'Rejected Dashboard prefill does not apply late after admission',
    );
    check(
      host.querySelector('[data-agent-request-status="accepted"]'),
      'Admission remains visible before its own run reaches a terminal state',
    );
    await receive('status', { state: 'running', language: 'en' }, receiptRun);
    await receive(
      'analysis_context',
      { context_id: contextId, filters: initialFilters, language: 'en' },
      receiptRun,
    );
    await receive('qa', { verified: true, status: 'verified' }, receiptRun);
    await receive(
      'message',
      { role: 'assistant', text: 'The requested synthetic explanation.', qa: { verified: true } },
      receiptRun,
    );
    await receive('status', { state: 'completed', language: 'en' }, receiptRun);
    check(
      !host.querySelector('[data-agent-request-status="accepted"]'),
      'A completed run no longer displays a notice promising future results',
    );
    dashboardId = otherContextId;
    locale = 'vi';
    await render();
    const replay = await execute('submit_agent_prompt', submission(1, 'Explain the source.'));
    check(
      replay.status === 'accepted' && sent.length === 1,
      'Durable replay works after the draft, Dashboard context and locale change',
    );
    check(
      !host.querySelector('[data-agent-request-status="accepted"]') &&
        (await execute('read_agent_status', {})).run_state === 'completed',
      'Replaying a completed request retains its receipt without restoring a pending notice',
    );
    check(
      ((await execute('read_agent_status', {})).last_receipt as Record<string, unknown>)
        .client_request_id === key(1),
      'The completed request receipt remains available for durable readback',
    );
    await reject('submit_agent_prompt', {
      ...submission(1, 'Explain the source.'),
      language: 'vi',
    });

    await receive('status', { state: 'running', language: 'en' }, activeRun);
    await receive(
      'analysis_context',
      { context_id: contextId, filters: initialFilters, language: 'en' },
      activeRun,
    );
    await execute('stage_agent_prompt', {
      text: 'Keep the active scope.',
      analysis_context_id: contextId,
    });
    const status = await execute('read_agent_status', {});
    const active = status.active_run as Record<string, unknown>;
    check(
      active.analysis_context_id === contextId && active.language === 'en',
      'Active context and language are immutable',
    );
    await execute('steer_agent_run', {
      ...submission(2, 'Keep the active scope.'),
      run_id: activeRun,
      action_intent: 'none',
    });
    check(
      sent[1].steer_run_id === activeRun &&
        sent[1].analysis_context_id === contextId &&
        sent[1].language === 'en',
      'Steering follows the run, not the changed Dashboard or UI locale',
    );
    await acknowledge(sent[1], { steering: true });

    for (const id of [otherContextId, contextId]) {
      dashboardId = id;
      prefill = { id: eventId++, text: 'A separate Dashboard analysis.', contextId: id };
      await render();
      check(
        prefillReceipts.at(-1)?.applied === true,
        'Dashboard prefill acknowledges the committed draft',
      );
      const draftStatus = await execute('read_agent_status', {});
      const draft = draftStatus.draft as Record<string, unknown>;
      check(
        draft.mode === 'start' && draft.analysis_context_id === id,
        'Explicit Dashboard staging preserves its context and requests a separate run',
      );
      check(
        host.textContent?.includes('lượt phân tích riêng'),
        'The visible composer explains separate queued analysis',
      );
      const index = id === otherContextId ? 3 : 4;
      await execute('submit_agent_prompt', submission(index, prefill.text, id, 'vi'));
      const prompt = sent.at(-1)!;
      check(
        !prompt.steer_run_id && prompt.analysis_context_id === id,
        'Same-context and changed-context Dashboard actions never silently steer',
      );
      await acknowledge(prompt, { queued: true });
    }

    await execute('stage_agent_answer_followup', {
      run_id: originalRun,
      message_id: 4,
      text: 'Explain this answer.',
    });
    const followStatus = await execute('read_agent_status', {});
    const followDraft = followStatus.draft as Record<string, unknown>;
    check(
      followDraft.analysis_context_id === contextId &&
        followDraft.language === 'en' &&
        followDraft.mode === 'answer_followup',
      'Historical follow-up preserves source context/language while another run is active',
    );
    await receive('status', { state: 'unverified' }, originalRun);
    check(
      ((await execute('read_agent_status', {})).active_run as Record<string, unknown>)?.run_id ===
        activeRun,
      'Late historical QA status cannot replace the current active run',
    );
    const followRequest = {
      ...submission(5, 'Explain this answer.'),
      reply_to: { run_id: originalRun, message_id: 4 },
    };
    await reject('submit_agent_prompt', followRequest);
    check(
      host.querySelector('textarea')?.value === 'Explain this answer.',
      'Late QA failure preserves the follow-up draft for review',
    );
    await receive('status', { state: 'completed' }, originalRun);
    // Restore the active run as the latest status without changing the source answer.
    await receive('status', { state: 'running', language: 'en' }, activeRun);
    await execute('submit_agent_prompt', followRequest);
    check(
      sent.at(-1)?.reply_to?.message_id === 4 && !sent.at(-1)?.steer_run_id,
      'Historical follow-up submits to a separate analysis with exact source identity',
    );
    await acknowledge(sent.at(-1)!, { queued: true });
    await reject('stage_agent_answer_followup', {
      run_id: 'f'.repeat(32),
      message_id: 4,
      text: 'Other visitor',
    });

    const cancel = await execute('request_agent_cancellation', { run_id: activeRun });
    check(
      cancel.status === 'requested' && cancel.provider_stopped === false && cancelRequests === 1,
      'Cancellation response only reports a request',
    );
    check(
      host.querySelector<HTMLButtonElement>('.agent-cancel-button')?.disabled,
      'The visible run is stopping until a terminal event',
    );
    await receive('status', { state: 'completed' }, originalRun);
    check(
      host.querySelector<HTMLButtonElement>('.agent-cancel-button')?.disabled,
      'A late historical terminal status keeps the current run and its stopping control visible',
    );
    const cancellationStatus = await execute('read_agent_status', {});
    check(
      (cancellationStatus.cancellation as Record<string, unknown>).status === 'requested',
      'Another run terminal event cannot acknowledge cancellation',
    );
    await receive('status', { state: 'cancelled' }, activeRun);
    check(
      (await execute('read_agent_status', {})).cancellation !== null,
      'Terminal cancellation is retained for readback',
    );

    prefill = undefined;
    dashboardId = contextId;
    locale = 'en';
    await render();
    await execute('stage_agent_prompt', {
      text: 'Uncertain after dispatch.',
      analysis_context_id: contextId,
    });
    const afterSend = new AbortController();
    abortAfterSend = afterSend;
    await reject(
      'submit_agent_prompt',
      submission(6, 'Uncertain after dispatch.'),
      afterSend.signal,
    );
    check(
      sent.at(-1)?.client_request_id === key(6) && cancelRequests === 1,
      'Invocation abort after send retains the request and never cancels the provider',
    );
    const countBeforeReload = sent.length;
    receipts.set(key(6), makeReceipt(sent.at(-1)!, 'uncertain'));
    mountKey = 'reload';
    await render();
    await waitFor(() => sockets.length === 2);
    await act(async () => sockets[1].open());
    await settle();
    check(
      sent.length === countBeforeReload && host.querySelector('.pending-message'),
      'Reload only reads the retained key and never resends uncertain work',
    );
    const uncertain = await execute('read_agent_prompt_receipt', { client_request_id: key(6) });
    check(uncertain.status === 'uncertain', 'Durable uncertainty remains explicit after reload');
    visible = false;
    await render();
    check(
      Number(registry.size) === 0,
      'All Agent registrations are removed when its panel is hidden',
    );
    visible = true;
    await render();
    check(registry.size === 11, 'Showing the Agent restores one registration per tool');

    await act(async () => root.render(null));
    Object.defineProperty(document, 'modelContext', { configurable: true, value: undefined });
    localStorage.removeItem(pendingPromptStorageKey);
    localStorage.removeItem(storageKeys[1]);
    mountKey = 'unsupported';
    await render();
    await waitFor(() => sockets.length === 3);
    await act(async () => sockets[2].open());
    const textarea = host.querySelector('textarea');
    check(textarea, 'Ordinary composer survives unsupported browsers');
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!;
    await act(async () => {
      setter.call(textarea, 'Ordinary UI question.');
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await act(async () =>
      host
        .querySelector('form')!
        .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })),
    );
    check(
      sent.at(-1)?.text === 'Ordinary UI question.' && sent.at(-1)?.request_origin === 'ui',
      'Ordinary UI uses the same keyed dispatch without WebMCP support',
    );
    await acknowledge(sent.at(-1)!);
    return {
      passed: true,
      fixture: 'mocked HTTP/WebSocket/WebMCP registry, real mounted AgentWorkspace',
      sends: sent.length,
      registrationCount: 11,
      receiptReplay: true,
      completedReceiptReplayNotice: true,
      callerKeysStable: true,
      noAutomaticRetry: true,
      abortAfterSendPreservesUncertainty: true,
      reloadReadback: true,
      dashboardExplicitSeparateSameAndChangedContext: true,
      lateQaFollowupInvalidation: true,
      activeContextImmutable: true,
      cancellationRequestLifecycle: true,
      unsupportedBrowserUi: true,
    };
  } finally {
    await act(async () => root.unmount());
    host.remove();
    window.fetch = savedFetch;
    window.WebSocket = savedSocket;
    if (descriptor) Object.defineProperty(document, 'modelContext', descriptor);
    else Reflect.deleteProperty(document, 'modelContext');
    for (const [key, value] of savedStorage) {
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    }
    environment.IS_REACT_ACT_ENVIRONMENT = savedAct;
  }
}
