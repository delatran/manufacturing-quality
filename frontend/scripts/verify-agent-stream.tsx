// Open /scripts/verify-agent-stream.html on the Vite origin, import this module,
// then call runAgentStreamVerification(). No application entry point is loaded.
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import AgentWorkspace from '../src/AgentWorkspace';
import { createI18n, I18nScope } from '../src/i18n';
import type { Locale } from '../src/i18n';
import { initialFilters } from '../src/dashboard-types';
import type { DashboardData } from '../src/dashboard-types';
import type { Session, WorkspaceEvent } from '../src/types';
import type { UiCommand } from '../src/workspace-state';
import { canonicalPromptRequest, pendingPromptStorageKey } from '../src/agent-prompt-requests';
import type { PromptRequest } from '../src/agent-prompt-requests';

function check(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}
const sessionId = 'agent-stream-browser-fixture';
const context: DashboardData['context'] = {
  context_id: 'stream-context',
  snapshot_id: 'stream-snapshot',
  filters: initialFilters,
  source_sha256: 'synthetic',
  master_sha256: 'synthetic',
  code_sha256: 'synthetic',
  kpi_version: 'fixture',
  fetched_at: '2026-10-18T00:00:00Z',
};
const event = (
  id: number,
  type: string,
  data: Record<string, unknown>,
  runId?: string,
): WorkspaceEvent => ({
  id,
  session_id: sessionId,
  type,
  time: '2026-10-18T00:00:00Z',
  data,
  ...(runId ? { run_id: runId } : {}),
});

export async function runAgentStreamVerification() {
  const host = document.createElement('div');
  document.body.append(host);
  const root = createRoot(host);
  const originalFetch = window.fetch;
  const originalSocket = window.WebSocket;
  const sessionKey = 'analyst.workspace.session.v1';
  const draftKey = 'analyst.workspace.draft.v2';
  const savedSession = localStorage.getItem(sessionKey);
  const savedDraft = localStorage.getItem(draftKey);
  const savedPending = localStorage.getItem(pendingPromptStorageKey);
  const environment = globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean };
  const previousActEnvironment = environment.IS_REACT_ACT_ENVIRONMENT;
  environment.IS_REACT_ACT_ENVIRONMENT = true;
  localStorage.setItem(sessionKey, sessionId);
  localStorage.removeItem(draftKey);
  localStorage.removeItem(pendingPromptStorageKey);
  const sockets: FixtureSocket[] = [];
  const sent: Record<string, unknown>[] = [];
  const receiptEvent = (id: number, prompt: Record<string, unknown>, runId?: string) =>
    event(
      id,
      'prompt_receipt',
      {
        client_request_id: prompt.client_request_id,
        session_id: sessionId,
        status: 'accepted',
        run_id: runId ?? null,
        action: prompt.steer_run_id ? 'steer' : 'start',
        request: canonicalPromptRequest(prompt as PromptRequest),
        request_sha256: 'a'.repeat(64),
        replayed: false,
      },
      runId,
    );
  const commands: string[] = [];
  let historicalReportReads = 0;
  const events: WorkspaceEvent[] = [];
  const runs: NonNullable<Session['runs']> = [];
  for (let index = 0; index < 10; index++) {
    const runId = (index + 1).toString(16).padStart(32, '0');
    const first = index * 5 + 1;
    const question = index === 0 ? 'Explain a synthetic quality finding.' : 'Repeated question';
    events.push(
      event(first, 'message', {
        role: 'user',
        text: question,
        analysis_context_id: context.context_id,
        language: 'en',
      }),
      event(first + 1, 'run_started', { question, language: 'en' }, runId),
      event(
        first + 2,
        'analysis_context',
        { context_id: context.context_id, filters: initialFilters, language: 'en' },
        runId,
      ),
      event(
        first + 3,
        'message',
        {
          get role() {
            historicalReportReads += 1;
            return 'assistant';
          },
          get text() {
            historicalReportReads += 1;
            return `Synthetic answer ${index + 1}. Review the selected scope.`;
          },
          qa: { verified: true },
        },
        runId,
      ),
      event(first + 4, 'status', { state: 'completed', language: 'en' }, runId),
    );
    runs.push({
      id: runId,
      state: 'completed',
      analysis_context_id: context.context_id,
      language: 'en',
    });
  }
  const restored: Session = { id: sessionId, events, runs };
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
    send(value: string) {
      check(this.readyState === 1, 'A prompt requires an open socket.');
      sent.push(JSON.parse(value) as Record<string, unknown>);
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
  window.fetch = async (input) => {
    const url = new URL(String(input), location.href);
    let value: unknown;
    if (url.pathname === '/api/config')
      value = { mode: 'simulation', model: 'synthetic', ai_available: true };
    else if (url.pathname === `/api/sessions/${sessionId}`) value = restored;
    else if (url.pathname === `/api/sessions/${sessionId}/scope`) value = {};
    else throw new Error(`Unexpected request in isolated verifier: ${url.pathname}`);
    const response = new Response('{}', { status: 200 });
    // Keep the fixture getters observable. The production request() parser is
    // still exercised, while all network access remains inside this mock.
    response.json = async () => value;
    return response;
  };
  const onCommand = async (command: UiCommand) => {
    commands.push(command.command_id);
    return true;
  };
  const render = async (locale: Locale, dashboard = context, key = 'stream') => {
    await act(async () =>
      root.render(
        <I18nScope locale={locale}>
          <AgentWorkspace
            key={key}
            visible
            contextId={dashboard.context_id}
            dashboardContext={dashboard}
            onUiCommand={onCommand}
          />
        </I18nScope>,
      ),
    );
  };
  const settle = async (milliseconds = 30) => {
    await act(async () => new Promise((resolve) => setTimeout(resolve, milliseconds)));
  };
  const waitFor = async (condition: () => boolean, message: string) => {
    const deadline = Date.now() + 4000;
    while (!condition() && Date.now() < deadline) await settle(20);
    check(condition(), message);
  };
  const enter = async (value: string) => {
    const textarea = host.querySelector('textarea');
    check(textarea, 'The Agent composer is mounted.');
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
    check(setter, 'The browser provides the native textarea value setter.');
    await act(async () => {
      setter.call(textarea, value);
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
    });
    check(textarea.value === value, 'Typing updates the controlled composer.');
  };
  const uiEvent = (id: number, commandId: string) =>
    event(id, 'ui_command', {
      command_id: commandId,
      action: 'open',
      panel: 'records',
      context_id: context.context_id,
      expected_context_id: context.context_id,
      reversible: true,
    });
  try {
    await render('en');
    await waitFor(() => sockets.length === 1, 'Restored session opens one stream.');
    await act(async () => sockets[0].open());
    check(host.querySelectorAll('.analyst-message').length === 10, 'All restored answers render.');
    historicalReportReads = 0;
    for (const value of ['R', 'Re', 'Repeat', 'Repeated question']) await enter(value);
    check(
      historicalReportReads === 0,
      `Composer edits must not reread historical answers; observed ${historicalReportReads}.`,
    );
    const readsDuringTyping = historicalReportReads;
    const form = host.querySelector('form');
    check(form, 'The composer uses a form.');
    await act(async () => {
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    });
    check(sent.length === 1, 'Two submissions before React commits send one prompt.');
    check(
      sent[0].analysis_context_id === context.context_id && sent[0].language === 'en',
      'The admitted prompt keeps its selected context and language.',
    );
    check(host.querySelector('.pending-message'), 'The pending prompt waits for server admission.');
    await act(async () => sockets[0].receive(events[45]));
    await settle();
    check(
      host.querySelector('.pending-message'),
      'Replaying an identical older question cannot acknowledge a new prompt.',
    );
    await act(async () => sockets[0].receive(receiptEvent(51, sent[0])));
    await settle();
    check(
      !host.querySelector('.pending-message'),
      'A fresh persisted admission acknowledges the pending prompt.',
    );
    await act(async () => sockets[0].receive(uiEvent(52, 'fresh-command')));
    await settle();
    check(commands.length === 1, 'A fresh UI command applies once.');
    await act(async () =>
      sockets[0].receive({ events: [uiEvent(52, 'fresh-command'), events[45], events[49]] }),
    );
    await settle();
    check(commands.length === 1, 'A reconnect replay cannot reapply the UI command.');
    const old = sockets[0];
    await act(async () => {
      old.close();
      old.receive(uiEvent(9000, 'closed-socket-command'));
    });
    await settle();
    check(commands.length === 1, 'A closed socket is invalid immediately, before reconnection.');
    await waitFor(() => sockets.length === 2, 'The workspace reconnects automatically.');
    check(
      new URL(sockets[1].url).searchParams.get('after') === '52',
      'Reconnect uses the newest accepted durable cursor.',
    );
    await act(async () => {
      sockets[1].open();
      old.receive(uiEvent(9001, 'replaced-socket-command'));
      sockets[1].receive(uiEvent(53, 'current-socket-command'));
    });
    await settle();
    check(
      commands.join(',') === 'fresh-command,current-socket-command',
      'Only the current socket can apply new events.',
    );
    await render('vi');
    check(
      host.querySelector('h1')?.textContent ===
        createI18n('vi').t('ui.ask_about_production_quality'),
      'Memoization still updates the page language.',
    );
    check(
      host.querySelector('.analyst-message')?.getAttribute('lang') === 'en',
      'Restored answers retain their authored language.',
    );
    const sourceRun = '1'.padStart(32, '0');
    const followup = host.querySelector<HTMLButtonElement>('.answer-followups button');
    check(followup, 'A completed conceptual answer exposes a follow-up.');
    await act(async () => followup.click());
    check(
      host.querySelector('.answer-reply-question')?.textContent ===
        'Explain a synthetic quality finding.',
      'The stable follow-up handler reads the selected run question.',
    );
    const sendButton = () => host.querySelector<HTMLButtonElement>('.send-button');
    check(sendButton()?.disabled === false, 'A verified source allows a follow-up draft.');
    await act(async () =>
      sockets[1].receive(event(54, 'status', { state: 'unverified' }, sourceRun)),
    );
    await settle();
    check(sendButton()?.disabled, 'Late QA failure invalidates the selected source.');
    await act(async () =>
      sockets[1].receive(event(55, 'status', { state: 'completed' }, sourceRun)),
    );
    await settle();
    check(
      sendButton()?.disabled === false,
      'A later completed receipt restores the unchanged source.',
    );
    await act(async () =>
      sockets[1].receive(
        event(
          56,
          'message',
          {
            role: 'assistant',
            text: 'Replacement explanation of the synthetic quality finding.',
            qa: { verified: true },
          },
          sourceRun,
        ),
      ),
    );
    await settle();
    check(sendButton()?.disabled, 'A replacement answer invalidates an older selected message.');
    const clearReply = host.querySelector<HTMLButtonElement>('.answer-reply-clear');
    check(clearReply, 'The invalid reference can be cleared.');
    await act(async () => clearReply.click());
    const replacementFollowup = host.querySelector<HTMLButtonElement>('.answer-followups button');
    check(replacementFollowup, 'The verified replacement has its own follow-up.');
    await act(async () => replacementFollowup.click());
    await act(async () =>
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })),
    );
    check(Number(sent.length) === 2, 'A replacement follow-up submits once.');
    const reference = sent[1].reply_to as Record<string, unknown>;
    check(
      reference.message_id === 56 && reference.run_id === sourceRun,
      'The stable handler submits the latest selected report identity.',
    );
    check(
      sent[1].language === 'en' && sent[1].analysis_context_id === context.context_id,
      'The source language and immutable context survive the page locale change.',
    );
    await act(async () => sockets[1].receive(receiptEvent(57, sent[1])));
    await settle();
    check(!host.querySelector('.pending-message'), 'The follow-up admission is acknowledged.');

    // Workspace emits running before awaiting the immutable analysis context.
    // A new run is not present in the session snapshot loaded at startup.
    const earlyRun = 'e'.repeat(32);
    const otherDashboard = { ...context, context_id: 'other-dashboard-context' };
    await act(async () =>
      sockets[1].receive(event(58, 'status', { state: 'running', language: 'en' }, earlyRun)),
    );
    await settle();
    await render('vi', otherDashboard);
    await enter('Add this detail to the active analysis.');
    check(sendButton()?.disabled, 'Steering waits until the active run context is known.');
    await act(async () => {
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
      host
        .querySelector('textarea')
        ?.dispatchEvent(
          new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }),
        );
    });
    check(Number(sent.length) === 2, 'Neither form nor Enter bypasses the context readiness gate.');
    check(
      host.querySelector('textarea')?.value === 'Add this detail to the active analysis.',
      'Waiting for run context preserves the editable draft.',
    );
    await act(async () =>
      sockets[1].receive(
        event(
          59,
          'analysis_context',
          { context_id: context.context_id, filters: initialFilters, language: 'en' },
          earlyRun,
        ),
      ),
    );
    await settle();
    check(sendButton()?.disabled === false, 'The recorded run context enables steering.');
    await act(async () => {
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    });
    check(Number(sent.length) === 3, 'The ready detail is sent exactly once.');
    check(
      sent[2].steer_run_id === earlyRun &&
        sent[2].analysis_context_id === context.context_id &&
        sent[2].language === 'en',
      'Steering keeps the run context and language despite a different Dashboard and page locale.',
    );
    await act(async () => sockets[1].receive(receiptEvent(60, sent[2], earlyRun)));
    await settle();
    check(
      !host.querySelector('.pending-message') && !host.querySelector('textarea')?.disabled,
      'The server admission clears pending delivery and unlocks the composer.',
    );

    const queuedFollowup = host.querySelector<HTMLButtonElement>('.answer-followups button');
    check(queuedFollowup, 'A completed answer remains available while another run is active.');
    await act(async () => queuedFollowup.click());
    await render('vi');
    check(sendButton()?.disabled === false, 'An explicit answer follow-up can queue separately.');
    await act(async () =>
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })),
    );
    check(Number(sent.length) === 4, 'The queued follow-up is sent once.');
    const queuedReference = sent[3].reply_to as Record<string, unknown>;
    check(
      queuedReference.run_id === sourceRun &&
        queuedReference.message_id === 56 &&
        sent[3].steer_run_id === undefined &&
        sent[3].analysis_context_id === context.context_id &&
        sent[3].language === 'en',
      'The queued question retains its selected answer identity, scope, and language.',
    );
    await act(async () => sockets[1].receive(receiptEvent(61, sent[3])));
    await settle();
    check(!host.querySelector('.pending-message'), 'Queued admission is acknowledged.');

    for (const restoredContext of [context.context_id, null]) {
      const restoredRunId = (restoredContext ? 'a' : 'b').repeat(32);
      restored.events = [event(62, 'status', { state: 'running' }, restoredRunId)];
      restored.runs = [
        {
          id: restoredRunId,
          state: 'running',
          analysis_context_id: restoredContext,
          language: 'vi',
        },
      ];
      const socketCount = sockets.length;
      await render('en', otherDashboard, restoredRunId);
      await waitFor(() => sockets.length === socketCount + 1, 'A restored run opens its stream.');
      const restoredSocket = sockets[socketCount];
      await act(async () => restoredSocket.open());
      await enter('Confirm the restored analysis.');
      check(sendButton()?.disabled === false, 'Saved run metadata permits restored steering.');
      const restoredForm = host.querySelector('form');
      check(restoredForm, 'The restored composer uses a form.');
      await act(async () =>
        restoredForm.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })),
      );
      const restoredPrompt = sent.at(-1);
      check(
        restoredPrompt?.steer_run_id === restoredRunId &&
          (restoredPrompt.analysis_context_id ?? null) === restoredContext &&
          restoredPrompt.language === 'vi',
        'Restored steering preserves a recorded context or explicit null and its language.',
      );
      await act(async () =>
        restoredSocket.receive(receiptEvent(63, restoredPrompt!, restoredRunId)),
      );
      await settle();
      check(
        !host.querySelector('.pending-message'),
        'Restored steering admission is acknowledged.',
      );
    }
    await act(async () => root.unmount());
    old.receive(uiEvent(9002, 'unmounted-command'));
    await settle();
    check(Number(commands.length) === 2, 'Unmounted sockets cannot apply commands.');
    return {
      passed: true,
      restoredAnswers: 10,
      composerEdits: 4,
      historicalReportReads: readsDuringTyping,
      sentPrompts: sent.length,
      appliedCommands: commands,
      reconnectCursor: '52',
      localeChanged: true,
      lateQaInvalidation: true,
      replacementFollowupMessageId: reference.message_id,
      followupLanguage: sent[1].language,
      earlySteeringWaitsForContext: true,
      queuedFollowupPreservesSource: true,
      restoredSteeringContexts: [context.context_id, null],
    };
  } finally {
    await act(async () => root.unmount());
    host.remove();
    window.fetch = originalFetch;
    window.WebSocket = originalSocket;
    if (savedSession === null) localStorage.removeItem(sessionKey);
    else localStorage.setItem(sessionKey, savedSession);
    if (savedDraft === null) localStorage.removeItem(draftKey);
    else localStorage.setItem(draftKey, savedDraft);
    if (savedPending === null) localStorage.removeItem(pendingPromptStorageKey);
    else localStorage.setItem(pendingPromptStorageKey, savedPending);
    environment.IS_REACT_ACT_ENVIRONMENT = previousActEnvironment;
  }
}
