// Bundle with React's production profiling renderer, then run in an isolated browser.
// This fixture uses the real workspace and local synthetic responses only.
import { Profiler } from 'react';
import { createRoot } from 'react-dom/client';
import AgentWorkspace from '../src/AgentWorkspace';
import { I18nScope } from '../src/i18n';
import { initialFilters } from '../src/dashboard-types';
import type { Session, WorkspaceEvent } from '../src/types';
import { isolateAgentStorage } from './agent-storage-fixture';

const check = (condition: unknown, message: string) => {
  if (!condition) throw new Error(message);
};
const pause = (delay: number) => new Promise((resolve) => setTimeout(resolve, delay));
const frame = () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));

export async function runAgentConversationBenchmark() {
  const completedRuns = 100;
  const providerEventsPerRun = 64;
  const freshBatches = 10;
  const eventsPerBatch = 8;
  const sessionId = 'conversation-performance-fixture';
  const contextId = 'performance-context';
  const host = document.createElement('div');
  document.body.append(host);
  const root = createRoot(host);
  const savedFetch = window.fetch;
  const savedSocket = window.WebSocket;
  const storage = isolateAgentStorage();
  localStorage.setItem('analyst.workspace.session.v1', sessionId);
  let historicalEventReads = 0;
  let historicalReportReads = 0;
  let sequence = 0;
  const event = (
    type: string,
    data: Record<string, unknown>,
    runId?: string,
    historical = false,
  ): WorkspaceEvent => ({
    id: ++sequence,
    session_id: sessionId,
    run_id: runId,
    time: '2026-10-18T00:00:00Z',
    get type() {
      if (historical) historicalEventReads += 1;
      return type;
    },
    data,
  });
  const events: WorkspaceEvent[] = [];
  const runs: NonNullable<Session['runs']> = [];
  for (let index = 0; index < completedRuns; index++) {
    const runId = (index + 1).toString(16).padStart(32, '0');
    const question = `Review recorded defects for synthetic selection ${index + 1}.`;
    const report = `## Selected scope ${index + 1}\n\nRecorded defects need review with the production owner. These synthetic figures exercise rendering only.\n\n| Measure | Value |\n| --- | ---: |\n| Recorded defects | 12 |\n| Affected groups | 3 |\n\nConfirm the source records before assigning the next action.`;
    events.push(
      event('message', { role: 'user', text: question, language: 'en' }, undefined, true),
      event('run_started', { question, language: 'en' }, runId, true),
      event(
        'analysis_context',
        { context_id: contextId, filters: initialFilters, language: 'en' },
        runId,
        true,
      ),
      event('status', { state: 'running', language: 'en' }, runId, true),
    );
    for (let part = 0; part < providerEventsPerRun; part++)
      events.push(
        event(
          'reasoning_summary',
          {
            item_id: `reasoning-${index}`,
            summary_index: 0,
            text:
              part === providerEventsPerRun - 1
                ? 'Checked the synthetic source records.'
                : 'Checking. ',
            final: part === providerEventsPerRun - 1,
          },
          runId,
          true,
        ),
      );
    events.push(
      event(
        'message',
        {
          get role() {
            historicalReportReads += 1;
            return 'assistant';
          },
          get text() {
            historicalReportReads += 1;
            return report;
          },
          qa: { verified: true },
        },
        runId,
        true,
      ),
      event('status', { state: 'completed', language: 'en' }, runId, true),
    );
    runs.push({ id: runId, state: 'completed', analysis_context_id: contextId, language: 'en' });
  }
  const rawHistoryLength = events.length;
  const session: Session = { id: sessionId, events, runs };
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
    send() {
      throw new Error('This performance fixture never sends a provider request.');
    }
    receive(batch: WorkspaceEvent[]) {
      this.onmessage?.(new MessageEvent('message', { data: JSON.stringify({ events: batch }) }));
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
    else if (url.pathname === `/api/sessions/${sessionId}`) value = session;
    else if (url.pathname === `/api/sessions/${sessionId}/scope`) value = {};
    else throw new Error(`Unexpected request: ${url.pathname}`);
    const response = new Response('{}');
    response.json = async () => value;
    return response;
  };
  const commits: { actualDuration: number; commitTime: number }[] = [];
  const longTasks: { startTime: number; duration: number }[] = [];
  const longTaskObserver = PerformanceObserver.supportedEntryTypes.includes('longtask')
    ? new PerformanceObserver((list) => {
        for (const item of list.getEntries())
          longTasks.push({ startTime: item.startTime, duration: item.duration });
      })
    : undefined;
  longTaskObserver?.observe({ type: 'longtask', buffered: false });
  const waitFor = async (predicate: () => boolean, message: string) => {
    const until = performance.now() + 15000;
    while (!predicate() && performance.now() < until) await pause(10);
    check(predicate(), message);
  };
  try {
    root.render(
      <Profiler
        id="actual-agent-workspace"
        onRender={(_id, _phase, actualDuration, _baseDuration, _start, commitTime) =>
          commits.push({ actualDuration, commitTime })
        }
      >
        <I18nScope locale="en">
          <AgentWorkspace
            visible
            contextId={contextId}
            dashboardContext={{
              context_id: contextId,
              snapshot_id: 'synthetic-snapshot',
              filters: initialFilters,
              source_sha256: 'synthetic',
              master_sha256: 'synthetic',
              code_sha256: 'synthetic',
              kpi_version: 'fixture',
              fetched_at: '2026-10-18T00:00:00Z',
            }}
          />
        </I18nScope>
      </Profiler>,
    );
    await waitFor(() => sockets.length === 1, 'The real workspace restored its session.');
    sockets[0].open();
    await waitFor(
      () => host.querySelectorAll('.analyst-message').length === completedRuns,
      'All completed answers render.',
    );
    const activeRun = 'f'.repeat(32);
    sockets[0].receive([
      event('message', {
        role: 'user',
        text: 'Inspect the current synthetic selection.',
        language: 'en',
      }),
      event(
        'run_started',
        { question: 'Inspect the current synthetic selection.', language: 'en' },
        activeRun,
      ),
      event(
        'analysis_context',
        { context_id: contextId, filters: initialFilters, language: 'en' },
        activeRun,
      ),
      event('status', { state: 'running', language: 'en' }, activeRun),
      event(
        'reasoning_summary',
        { item_id: 'active-reasoning', summary_index: 0, text: 'Inspecting. ' },
        activeRun,
      ),
    ]);
    await pause(100);
    await frame();
    await frame();
    check(commits.length > 0, 'Use React production profiling renderer for timing.');
    historicalEventReads = 0;
    historicalReportReads = 0;
    const measurementStart = performance.now();
    const samples: { durationMs: number; commitLatencyMs: number; commits: number }[] = [];
    for (let batch = 0; batch < freshBatches; batch++) {
      const before = commits.length;
      const started = performance.now();
      sockets[0].receive(
        Array.from({ length: eventsPerBatch }, (_, part) =>
          event(
            'reasoning_summary',
            {
              item_id: 'active-reasoning',
              summary_index: 0,
              text: `Step ${batch}.${part}. `,
            },
            activeRun,
          ),
        ),
      );
      await waitFor(() => commits.length > before, 'A fresh provider batch commits.');
      await frame();
      await frame();
      const updates = commits.slice(before);
      samples.push({
        durationMs: updates.reduce((total, item) => total + item.actualDuration, 0),
        commitLatencyMs: updates[0].commitTime - started,
        commits: updates.length,
      });
    }
    check(
      events.length === rawHistoryLength,
      'Appending stream data never mutates restored history.',
    );
    check(
      host.querySelectorAll('.analyst-message').length === completedRuns,
      'No answer is hidden or truncated.',
    );
    await pause(0);
    const measuredLongTasks = longTasks.filter((item) => item.startTime >= measurementStart);
    const reportSemantics = [...host.querySelectorAll('.conversation-entry')]
      .filter((entry) => entry.querySelector('.analyst-message'))
      .map((entry) => ({
        text: entry.textContent,
        language: entry.getAttribute('lang'),
        links: [...entry.querySelectorAll('a')].map((link) => link.href),
        images: [...entry.querySelectorAll('img')].map((image) => image.src),
      }));
    const fingerprintBytes = await crypto.subtle.digest(
      'SHA-256',
      new TextEncoder().encode(JSON.stringify(reportSemantics)),
    );
    const reportFingerprint = [...new Uint8Array(fingerprintBytes)]
      .map((byte) => byte.toString(16).padStart(2, '0'))
      .join('');
    return {
      completedRuns,
      providerEventsPerRun,
      rawHistoryLength,
      freshBatches,
      eventsPerBatch,
      historicalEventReads,
      historicalReportReads,
      samples,
      retainedAnswers: host.querySelectorAll('.analyst-message').length,
      longTasks: measuredLongTasks,
      reportFingerprint,
      providerRequests: 0,
    };
  } finally {
    longTaskObserver?.disconnect();
    root.unmount();
    host.remove();
    window.fetch = savedFetch;
    window.WebSocket = savedSocket;
    storage.restore();
  }
}
