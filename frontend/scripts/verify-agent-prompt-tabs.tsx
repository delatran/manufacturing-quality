// Mount once in each of two real, same-origin browser tabs in an isolated browser context.
// Keep the returned controller on window, then reload either tab and mount again without
// initializeSession. HTTP, WebSocket and the tool registry are synthetic; App storage is real.
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import AgentWorkspace from '../src/AgentWorkspace';
import { I18nScope } from '../src/i18n';
import { initialFilters } from '../src/dashboard-types';
import { canonicalPromptRequest } from '../src/agent-prompt-requests';
import type { PromptRequest } from '../src/agent-prompt-requests';
import type { WebMcpTool } from '../src/webmcp';

const contextId = 'c'.repeat(64);
const context = {
  context_id: contextId,
  snapshot_id: 'synthetic',
  filters: initialFilters,
  source_sha256: 'synthetic',
  master_sha256: 'synthetic',
  code_sha256: 'synthetic',
  kpi_version: 'fixture',
  fetched_at: '2026-10-19T00:00:00Z',
};
function check(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

export async function mountAgentPromptTabFixture(sessionId: string, initializeSession = false) {
  const host = document.createElement('div');
  document.body.append(host);
  const root = createRoot(host);
  const savedFetch = window.fetch;
  const savedSocket = window.WebSocket;
  const descriptor = Object.getOwnPropertyDescriptor(document, 'modelContext');
  const environment = globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean };
  const savedAct = environment.IS_REACT_ACT_ENVIRONMENT;
  environment.IS_REACT_ACT_ENVIRONMENT = true;
  if (initializeSession) {
    localStorage.setItem('analyst.workspace.session.v1', sessionId);
    sessionStorage.removeItem('analyst.workspace.session.v1');
  }
  const tools = new Map<string, WebMcpTool>();
  Object.defineProperty(document, 'modelContext', {
    configurable: true,
    value: {
      registerTool(tool: WebMcpTool, options?: { signal?: AbortSignal }) {
        check(!tools.has(tool.name), `Duplicate fixture registration: ${tool.name}`);
        tools.set(tool.name, tool);
        options?.signal?.addEventListener('abort', () => tools.delete(tool.name), { once: true });
        return Promise.resolve();
      },
    },
  });
  const sent: PromptRequest[] = [];
  const sockets: FixtureSocket[] = [];
  class FixtureSocket {
    static OPEN = 1;
    readyState = 0;
    onopen: ((event: Event) => void) | null = null;
    onmessage: ((event: MessageEvent) => void) | null = null;
    onclose: ((event: CloseEvent) => void) | null = null;
    onerror: ((event: Event) => void) | null = null;
    constructor() {
      sockets.push(this);
      queueMicrotask(() => {
        this.readyState = 1;
        this.onopen?.(new Event('open'));
      });
    }
    send(raw: string) {
      sent.push(JSON.parse(raw) as PromptRequest);
    }
    close() {
      this.readyState = 3;
    }
  }
  window.WebSocket = FixtureSocket as unknown as typeof WebSocket;
  const sessionReads: string[] = [];
  const receiptReads: string[] = [];
  window.fetch = async (input) => {
    const url = new URL(String(input), location.href);
    if (url.pathname === '/api/config')
      return Response.json({ mode: 'simulation', model: 'synthetic', ai_available: true });
    const sessionMatch = /^\/api\/sessions\/([^/]+)$/.exec(url.pathname);
    if (sessionMatch) {
      const id = decodeURIComponent(sessionMatch[1]);
      sessionReads.push(id);
      return Response.json({ id, events: [], runs: [] });
    }
    if (/^\/api\/sessions\/[^/]+\/scope$/.test(url.pathname)) return Response.json({});
    if (/^\/api\/sessions\/[^/]+\/prompt-requests\//.test(url.pathname)) {
      receiptReads.push(url.pathname);
      return Response.json({ detail: 'Fixture receipt has not arrived.' }, { status: 404 });
    }
    if (url.pathname.startsWith('/api/dashboard/contexts/'))
      return Response.json({ context, analysis: { kpis: null } });
    throw new Error(`Unexpected request in isolated two-tab fixture: ${url.pathname}`);
  };
  const settle = async () => {
    await act(async () => new Promise((resolve) => setTimeout(resolve, 40)));
  };
  await act(async () =>
    root.render(
      <I18nScope locale="en">
        <AgentWorkspace visible contextId={contextId} dashboardContext={context} />
      </I18nScope>,
    ),
  );
  const until = Date.now() + 5000;
  while ((!tools.has('read_agent_status') || !sockets.length) && Date.now() < until) await settle();
  check(tools.has('read_agent_status') && sockets.length, 'The Agent tab mounted');
  await settle();
  const call = async (name: string, input: unknown) => {
    const tool = tools.get(name);
    check(tool, `The tool is mounted: ${name}`);
    let result: unknown;
    await act(async () => {
      result = await tool.execute(input, {});
    });
    return result as Record<string, unknown>;
  };
  let eventId = 1;
  return {
    async snapshot() {
      return {
        status: await call('read_agent_status', {}),
        visiblePending: host.querySelector('.pending-message')?.textContent ?? null,
        sent: [...sent],
        sessionReads: [...sessionReads],
        receiptReads: [...receiptReads],
      };
    },
    async submit(clientRequestId: string, text: string) {
      await call('stage_agent_prompt', { text, analysis_context_id: contextId });
      return call('submit_agent_prompt', {
        client_request_id: clientRequestId,
        text,
        analysis_context_id: contextId,
        language: 'en',
      });
    },
    async acknowledge(prompt: PromptRequest, receiptSessionId = sessionId) {
      await act(async () => {
        sockets.at(-1)!.onmessage?.(
          new MessageEvent('message', {
            data: JSON.stringify({
              id: eventId++,
              session_id: receiptSessionId,
              time: '2026-10-19T00:00:00Z',
              type: 'prompt_receipt',
              data: {
                client_request_id: prompt.client_request_id,
                session_id: receiptSessionId,
                status: 'accepted',
                run_id: null,
                action: 'start',
                request: canonicalPromptRequest(prompt),
                request_sha256: 'a'.repeat(64),
                replayed: false,
              },
            }),
          }),
        );
      });
      await settle();
    },
    async dispose() {
      await act(async () => root.unmount());
      host.remove();
      window.fetch = savedFetch;
      window.WebSocket = savedSocket;
      if (descriptor) Object.defineProperty(document, 'modelContext', descriptor);
      else Reflect.deleteProperty(document, 'modelContext');
      environment.IS_REACT_ACT_ENVIRONMENT = savedAct;
    },
  };
}
