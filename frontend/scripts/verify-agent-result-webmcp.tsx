import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import EventView, { bindCitationSource, citationSourceId } from '../src/EventView';
import { buildAgentResultTools, renderedAnswerText } from '../src/agent-result-webmcp';
import type { AgentResultSnapshot } from '../src/agent-result-webmcp';
import { appendConversationState, createConversationState } from '../src/conversation-state';
import { initialBackendScope } from '../src/generated-scope';
import type { WorkspaceEvent } from '../src/types';

// Isolated state and DOM-control fixtures. Native WebMCP/browser verification is separate.
Object.defineProperty(globalThis, 'React', { value: React });
Object.defineProperty(globalThis, 'window', {
  value: { location: { origin: 'http://localhost:8001', protocol: 'http:' } },
  configurable: true,
});
let frameEffect: (() => void) | undefined;
let nextFrame = 0;
const frames = new Set<number>();
Object.defineProperty(globalThis, 'requestAnimationFrame', {
  value(callback: FrameRequestCallback) {
    const id = ++nextFrame;
    frames.add(id);
    queueMicrotask(() => {
      if (!frames.delete(id)) return;
      const effect = frameEffect;
      frameEffect = undefined;
      effect?.();
      callback(0);
    });
    return id;
  },
  configurable: true,
});
Object.defineProperty(globalThis, 'cancelAnimationFrame', {
  value: (id: number) => frames.delete(id),
  configurable: true,
});

class TextFixture {
  readonly nodeType = 3;
  constructor(public textContent: string) {}
}

class ElementFixture {
  readonly nodeType = 1;
  isConnected = true;
  open = false;
  clicks = 0;
  scrolls = 0;
  parent: ElementFixture | null = null;
  childNodes: (ElementFixture | TextFixture)[] = [];
  dataset: Record<string, string> = {};
  attributes = new Map<string, string>();
  onClick?: () => void;
  get id() {
    return this.getAttribute('id') || '';
  }
  get classList() {
    return {
      contains: (name: string) => (this.getAttribute('class') || '').split(' ').includes(name),
    };
  }
  get ownerDocument() {
    return fixtureDocument;
  }
  constructor(
    public tagName: string,
    attributes: Record<string, string> = {},
  ) {
    for (const [key, value] of Object.entries(attributes)) this.setAttribute(key, value);
  }
  append(...children: (ElementFixture | string)[]) {
    for (const child of children) {
      if (typeof child === 'string') this.childNodes.push(new TextFixture(child));
      else {
        child.parent = this;
        this.childNodes.push(child);
      }
    }
    return this;
  }
  setAttribute(name: string, value: string) {
    this.attributes.set(name, value);
    if (name.startsWith('data-'))
      this.dataset[
        name.slice(5).replace(/-([a-z])/g, (_, letter: string) => letter.toUpperCase())
      ] = value;
  }
  getAttribute(name: string) {
    return this.attributes.get(name) ?? null;
  }
  matches(selectors: string) {
    return selectors.split(',').some((part) => {
      const selector = part.trim();
      if (selector.startsWith('.')) return this.classList.contains(selector.slice(1));
      const match = /^(\w+)?\[([^\]]+)\]$/.exec(selector);
      if (match)
        return (
          (!match[1] || match[1].toUpperCase() === this.tagName) && this.attributes.has(match[2])
        );
      return this.tagName === selector.toUpperCase();
    });
  }
  closest(selector: string): ElementFixture | null {
    return this.matches(selector) ? this : (this.parent?.closest(selector) ?? null);
  }
  querySelectorAll(selector: string): ElementFixture[] {
    return this.childNodes.flatMap((node) =>
      node instanceof ElementFixture
        ? [...(node.matches(selector) ? [node] : []), ...node.querySelectorAll(selector)]
        : [],
    );
  }
  querySelector(selector: string) {
    return this.querySelectorAll(selector)[0] ?? null;
  }
  contains(element: ElementFixture): boolean {
    return (
      element === this ||
      this.childNodes.some((node) => node instanceof ElementFixture && node.contains(element))
    );
  }
  focus() {
    fixtureDocument.activeElement = this;
  }
  scrollIntoView() {
    this.scrolls += 1;
  }
  click() {
    this.clicks += 1;
    this.onClick?.();
  }
}

const root = new ElementFixture('DIV', { class: 'agent-workspace' });
const fixtureDocument = {
  activeElement: null as ElementFixture | null,
  getElementById(id: string) {
    return root.querySelectorAll('[id]').find((item) => item.getAttribute('id') === id) ?? null;
  },
};
const element = (
  tag: string,
  attributes: Record<string, string> = {},
  ...children: (ElementFixture | string)[]
) => new ElementFixture(tag, attributes).append(...children);

const sessionId = 'visitor-result-fixture';
const runId = 'a'.repeat(32);
let sequence = 0;
const event = (type: string, data: Record<string, unknown>, run = runId): WorkspaceEvent => ({
  id: ++sequence,
  session_id: sessionId,
  run_id: run,
  type,
  data,
  time: '2026-10-19T01:00:00Z',
});
const claim = {
  id: 'count-claim',
  execution_id: 'successful-execution',
  path: '$.analysis.event_count',
  value: 12,
  code_sha256: 'b'.repeat(64),
  metric: 'Recorded events',
  labels: { private_receipt_field: 'DO_NOT_RETURN_LABELS' },
};
const chartUrl = `http://localhost:8001/api/sessions/${sessionId}/runs/${runId}/artifacts/trend.png`;
const artifact = event('artifact', {
  path: 'trend.png',
  name: 'trend.png',
  url: chartUrl,
  mime_type: 'image/png',
  sha256: 'c'.repeat(64),
  size_bytes: 120,
  delivery_candidate: true,
});
const context = event('analysis_context', {
  context_id: 'historical-context',
  filters: { ...initialBackendScope, production_line: 'Line 1' },
  snapshot_id: 'recorded-snapshot',
  source_sha256: 'd'.repeat(64),
  links: { export_zip: 'DO_NOT_RETURN_INTERNAL_LINKS' },
});
const started = event('run_started', {
  question: 'Explain the recorded trend.',
  artifact_delivery_version: 1,
});
const report = event('message', {
  role: 'assistant',
  text: 'Recorded count: 12 [claim:count-claim].\n\n![Trend](' + chartUrl + ')',
  qa: { verified: true },
  claims: [claim],
  language: 'en',
  delivery: {
    version: 1,
    scope: null,
    source_execution_ids: ['successful-execution'],
    artifacts: [artifact.data],
  },
});
const completed = event('status', { state: 'completed' });
const initialEvents = [started, context, artifact, report, completed];
let snapshot: AgentResultSnapshot = {
  session: { id: sessionId, events: initialEvents },
  conversation: createConversationState(initialEvents),
  dashboardContextId: 'current-dashboard-context',
  visible: true,
};
const article = element('ARTICLE', {
  'data-answer-session-id': sessionId,
  'data-answer-run-id': runId,
  'data-answer-message-id': String(report.id),
});
const evidenceButton = element('BUTTON', {
  class: 'citation-badge',
  'data-answer-citation': '',
  'data-evidence-index': '1',
  'data-evidence-pinned': 'false',
  'aria-expanded': 'false',
});
const chartButton = element('BUTTON', {
  'data-answer-chart-src': chartUrl,
  'aria-expanded': 'false',
});
const clearCitation = bindCitationSource(evidenceButton as unknown as HTMLButtonElement, claim.id);
assert.equal(citationSourceId(evidenceButton as unknown as HTMLButtonElement), claim.id);
clearCitation();
assert.equal(citationSourceId(evidenceButton as unknown as HTMLButtonElement), undefined);
bindCitationSource(evidenceButton as unknown as HTMLButtonElement, claim.id);
const prose = element(
  'DIV',
  { class: 'prose' },
  element('P', {}, 'Recorded count: 12 ', evidenceButton, '.'),
  element('DIV', { class: 'answer-code-toolbar' }, 'COPY_CONTROL_MUST_BE_OMITTED'),
  element('PRE', {}, 'print("literal code stays visible")'),
  element('IMG', { alt: 'Trend' }),
  chartButton,
  element('DIV', { 'data-answer-table-rows': '7', 'data-answer-table-visible-rows': '2' }),
);
article.append(prose);
root.append(article);
const evidencePanel = element('SPAN', { id: 'evidence-panel' });
const chartPanel = element('DIALOG', { id: 'chart-panel', 'data-answer-chart-dialog': '' });
const closeChart = element('BUTTON', { 'data-answer-chart-close': '' });
chartPanel.append(closeChart);
closeChart.onClick = () => {
  chartPanel.open = false;
  chartPanel.isConnected = false;
  chartButton.setAttribute('aria-expanded', 'false');
};
evidenceButton.onClick = () => {
  evidenceButton.setAttribute('aria-expanded', 'true');
  evidenceButton.setAttribute('data-evidence-pinned', 'true');
  evidenceButton.setAttribute('aria-controls', 'evidence-panel');
  if (!root.contains(evidencePanel)) root.append(evidencePanel);
};
chartButton.onClick = () => {
  chartButton.setAttribute('aria-expanded', 'true');
  chartButton.setAttribute('aria-controls', 'chart-panel');
  chartPanel.open = true;
  chartPanel.isConnected = true;
  if (!root.contains(chartPanel)) root.append(chartPanel);
};
const tools = buildAgentResultTools({
  getSnapshot: () => snapshot,
  getRoot: () => root as unknown as HTMLElement,
});
type Result = {
  [key: string]: unknown;
  answers: {
    run_id: string;
    message_id: number;
    source_context: { context_id: string; relation_to_dashboard: string };
  }[];
  pagination: {
    total: number;
    returned: number;
    next_offset: number | null;
    omitted_after: number;
  };
  history_revision: string;
  content: string;
  content_sha256: string;
  charts: { chart_id: string; rendered: boolean; sha256: string }[];
  claims: { claim_id: string; citation_visible: boolean }[];
};
const call = async (name: string, input: unknown = {}, signal?: AbortSignal) => {
  const tool = tools.find((item) => item.name === name);
  assert.ok(tool, `Tool ${name} exists`);
  return (await tool.execute(input, { signal })) as Result;
};
const ref = { run_id: runId, message_id: report.id };

// Metadata marks reads, UI changes and untrusted report output accurately.
assert.equal(tools.length, 4);
for (const tool of tools) {
  assert.equal(tool.inputSchema.additionalProperties, false);
  assert.equal(tool.annotations.consequentialHint, false);
  assert.equal(tool.annotations.untrustedContentHint, true);
  assert.equal(tool.annotations.readOnlyHint, tool.name !== 'navigate_agent_answer');
  assert.doesNotMatch(tool.name, /export|download|execute/);
}
const first = await call('list_agent_answers');
assert.equal(first.answers.length, 1);
assert.equal(first.answers[0].source_context.context_id, 'historical-context');
assert.equal(first.answers[0].source_context.relation_to_dashboard, 'historical_context');
assert.equal(first.result_exports_available, false);
assert.deepEqual(first.latest_verified_answer, ref);
assert.doesNotMatch(JSON.stringify(first), /DO_NOT_RETURN/);
snapshot = { ...snapshot, dashboardContextId: 'historical-context' };
assert.equal(
  (await call('list_agent_answers')).answers[0].source_context.relation_to_dashboard,
  'same_context',
);
snapshot = { ...snapshot, dashboardContextId: undefined };
assert.equal(
  (await call('list_agent_answers')).answers[0].source_context.relation_to_dashboard,
  'dashboard_context_unavailable',
);

const text = await call('read_agent_answer', { ...ref, limit: 12 });
assert.equal(text.content, 'Recorded cou');
assert.ok(text.pagination.omitted_after > 0);
assert.equal(text.charts[0].rendered, true);
assert.equal(text.charts[0].sha256, artifact.data.sha256);
assert.equal(text.chart_registry_status, 'registered_delivery');
assert.deepEqual(text.table_view, {
  total_tables: 1,
  tables_with_search_omissions: 1,
  omitted_rows_by_current_search: 5,
});
assert.doesNotMatch(JSON.stringify(text), /DO_NOT_RETURN|COPY_CONTROL|artifacts\/trend\.png/);
const continuation = await call('read_agent_answer', {
  ...ref,
  offset: 12,
  content_sha256: text.content_sha256,
});
assert.equal(text.content + continuation.content, renderedAnswerText(prose as unknown as Element));
await assert.rejects(
  call('read_agent_answer', { ...ref, offset: 1 }),
  /content_sha256 is required/,
);
await assert.rejects(
  call('read_agent_answer', { ...ref, offset: 1, content_sha256: 'e'.repeat(64) }),
  /visible answer changed/,
);
await assert.rejects(
  call('read_agent_answer', { ...ref, offset: 99999, content_sha256: text.content_sha256 }),
  /offset exceeds/,
);
const changingRead = call('read_agent_answer', ref);
const paragraph = prose.childNodes[0] as ElementFixture;
const firstText = paragraph.childNodes[0] as TextFixture;
const originalText = firstText.textContent;
firstText.textContent = 'Changed while hashing';
await assert.rejects(changingRead, /visible answer changed/);
firstText.textContent = originalText;
const evidence = await call('read_agent_answer_evidence', ref);
assert.equal(evidence.claims[0].claim_id, claim.id);
assert.equal(evidence.claims[0].citation_visible, true);
assert.doesNotMatch(JSON.stringify(evidence), /DO_NOT_RETURN/);

// Navigation invokes the same controls and waits for actual focus/panel readback.
await call('navigate_agent_answer', { ...ref, target: 'answer' });
assert.equal(fixtureDocument.activeElement, article);
assert.equal(article.scrolls, 1);
await call('navigate_agent_answer', { ...ref, target: 'evidence', claim_id: claim.id });
await call('navigate_agent_answer', { ...ref, target: 'evidence', claim_id: claim.id });
assert.equal(evidenceButton.clicks, 1, 'Repeated navigation does not close a pinned citation.');
await call('navigate_agent_answer', { ...ref, target: 'chart', chart_id: 'chart-1' });
await call('navigate_agent_answer', { ...ref, target: 'chart', chart_id: 'chart-1' });
assert.equal(chartButton.clicks, 1, 'Repeated navigation does not close an open chart.');
await call('navigate_agent_answer', { ...ref, target: 'answer' });
assert.equal(closeChart.clicks, 1, 'An existing chart closes through its ordinary UI control.');
assert.equal(fixtureDocument.activeElement, article);
await call('navigate_agent_answer', { ...ref, target: 'chart', chart_id: 'chart-1' });
chartPanel.open = false;
await assert.rejects(
  call('navigate_agent_answer', { ...ref, target: 'chart', chart_id: 'chart-1' }),
  /did not open/,
);
chartPanel.open = true;

// Malformed, unknown, cross-session and mismatched target arguments fail before acting.
for (const bad of [null, [], { extra: true }, { limit: 51 }, { offset: -1 }, { offset: 0.5 }])
  await assert.rejects(call('list_agent_answers', bad));
for (const bad of [
  { ...ref, extra: true },
  { ...ref, run_id: 'foreign-run' },
  { ...ref, message_id: '4' },
  { ...ref, message_id: 0 },
  { ...ref, message_id: Number.MAX_SAFE_INTEGER + 1 },
])
  await assert.rejects(call('read_agent_answer', bad));
await assert.rejects(
  call('navigate_agent_answer', { ...ref, target: 'answer', chart_id: 'chart-1' }),
);
await assert.rejects(
  call('navigate_agent_answer', { ...ref, target: 'evidence', claim_id: 'unknown' }),
);
await assert.rejects(
  call('navigate_agent_answer', { ...ref, target: 'chart', chart_id: chartUrl }),
);
snapshot = { ...snapshot, visible: false };
await assert.rejects(call('list_agent_answers'), /Open the connected Agent/);
snapshot = { ...snapshot, visible: true, session: { id: 'other-visitor', events: [] } };
assert.equal((await call('list_agent_answers')).answers.length, 0);
await assert.rejects(call('read_agent_answer', ref), /unavailable/);
snapshot = { ...snapshot, session: { id: sessionId, events: initialEvents } };

// Stored full history remains reachable, including more than a typical recent-message cap.
for (let index = 1; index <= 155; index += 1) {
  const run = index.toString(16).padStart(32, '0');
  const answerEvents = [
    event(
      'analysis_context',
      { context_id: `context-${index}`, filters: initialBackendScope },
      run,
    ),
    event('message', { role: 'assistant', text: `Answer ${index}`, qa: { verified: true } }, run),
    event('status', { state: 'completed' }, run),
  ];
  snapshot = {
    ...snapshot,
    conversation: appendConversationState(snapshot.conversation, answerEvents),
  };
}
const pageOne = await call('list_agent_answers', { limit: 50 });
assert.equal(pageOne.pagination.total, 156);
let read = pageOne.answers.length;
let next = pageOne.pagination.next_offset;
while (next !== null) {
  const value = await call('list_agent_answers', {
    offset: next,
    limit: 50,
    history_revision: pageOne.history_revision,
  });
  read += value.answers.length;
  next = value.pagination.next_offset;
}
assert.equal(read, 156);
await assert.rejects(call('list_agent_answers', { offset: 50 }), /history_revision is required/);
snapshot = {
  ...snapshot,
  conversation: appendConversationState(snapshot.conversation, [
    event('status', { state: 'unverified' }),
  ]),
};
await assert.rejects(
  call('list_agent_answers', { offset: 50, history_revision: pageOne.history_revision }),
  /History changed/,
);
await assert.rejects(call('read_agent_answer', ref), /no longer verified/);
assert.equal((await call('list_agent_answers')).omitted_ineligible_answers, 1);

// Late status or chart invalidation cannot return successful navigation or evidence.
const restore = () => {
  snapshot = { ...snapshot, conversation: createConversationState(initialEvents) };
};
restore();
frameEffect = () => {
  snapshot = {
    ...snapshot,
    conversation: appendConversationState(snapshot.conversation, [
      event('status', { state: 'failed' }),
    ]),
  };
};
await assert.rejects(
  call('navigate_agent_answer', { ...ref, target: 'answer' }),
  /no longer verified/,
);
restore();
frameEffect = () => {
  snapshot = {
    ...snapshot,
    conversation: appendConversationState(snapshot.conversation, [
      event('artifact', { ...artifact.data, sha256: 'f'.repeat(64) }),
    ]),
  };
};
await assert.rejects(
  call('navigate_agent_answer', { ...ref, target: 'chart', chart_id: 'chart-1' }),
  /invalidated/,
);
assert.equal((await call('read_agent_answer', ref)).charts.length, 0);
restore();
const replacement = event('message', {
  role: 'assistant',
  text: 'Replacement answer',
  qa: { verified: true },
});
snapshot = {
  ...snapshot,
  conversation: appendConversationState(snapshot.conversation, [replacement]),
};
await assert.rejects(call('read_agent_answer', ref), /superseded/);

// A repeated path never pins the first of two unrelated source claims.
const secondClaim = { ...claim, id: 'second-claim', execution_id: 'another-execution', value: 99 };
const ambiguousReport = { ...report, data: { ...report.data, claims: [claim, secondClaim] } };
snapshot = {
  ...snapshot,
  conversation: createConversationState(
    initialEvents.map((item) => (item === report ? ambiguousReport : item)),
  ),
};
bindCitationSource(evidenceButton as unknown as HTMLButtonElement, claim.path);
assert.equal(
  (await call('read_agent_answer_evidence', { ...ref, claim_id: claim.id })).claims[0]
    .citation_visible,
  false,
);
await assert.rejects(
  call('navigate_agent_answer', { ...ref, target: 'evidence', claim_id: claim.id }),
  /no visible citation/,
);
const duplicateReport = { ...report, data: { ...report.data, claims: [claim, { ...claim }] } };
snapshot = {
  ...snapshot,
  conversation: createConversationState(
    initialEvents.map((item) => (item === report ? duplicateReport : item)),
  ),
};
const duplicates = await call('read_agent_answer_evidence', ref);
assert.equal(duplicates.claims.length, 0);
assert.equal(duplicates.omitted_invalid_or_ambiguous_claims, 2);
restore();
bindCitationSource(evidenceButton as unknown as HTMLButtonElement, claim.id);

const aborted = new AbortController();
aborted.abort();
for (const tool of tools)
  await assert.rejects(
    call(tool.name, tool.name === 'list_agent_answers' ? {} : ref, aborted.signal),
    { name: 'AbortError' },
  );
const sourceCopy = JSON.stringify(initialEvents);
await call('read_agent_answer', ref);
await call('read_agent_answer_evidence', ref);
assert.equal(
  JSON.stringify(initialEvents),
  sourceCopy,
  'Reads preserve source history and evidence.',
);

// Actual React markup carries only the stable hooks needed by the existing controls.
const html = renderToStaticMarkup(
  <EventView
    event={snapshot.conversation.entries.find((entry) => entry.event.id === report.id)!.event}
    language="en"
  />,
);
assert.match(html, /data-answer-message-id="4"/);
assert.match(html, /data-answer-citation=""/);
assert.doesNotMatch(html, /data-claim-id|successful-execution|count-claim/);
assert.match(html, /data-answer-chart-src=/);
assert.doesNotMatch(html, /Download|Export result/);
console.log(
  'Agent result WebMCP isolated fixtures passed: history, provenance, privacy, pagination, visible controls, and invalidation.',
);
