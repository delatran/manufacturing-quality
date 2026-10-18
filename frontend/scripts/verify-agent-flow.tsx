import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import AgentFlow from '../src/AgentFlow';
import { callOutcome, projectAgentFlow, splitSummaryPart } from '../src/agent-flow';
import { flowSummaryDisplay } from '../src/flow-summary-display';
import type { FlowPart } from '../src/agent-flow';
import { I18nProvider } from '../src/i18n';
import type { WorkspaceEvent } from '../src/types';

(globalThis as Record<string, unknown>).React = React;
Object.defineProperty(globalThis, 'window', {
  value: { location: { origin: 'http://localhost:8001', protocol: 'http:' } },
  configurable: true,
  writable: true,
});

let sequence = 0;
const event = (
  type: string,
  data: Record<string, unknown>,
  run: string | null = 'run-1',
): WorkspaceEvent => ({
  id: ++sequence,
  session_id: 'session-1',
  run_id: run === null ? undefined : run,
  type,
  time: '2026-10-17T00:00:00Z',
  data,
});

// 1. Reasoning summary parts keep the provider's own section boundaries.
assert.deepEqual(splitSummaryPart('**Reading the data**\n\nI pull Line-3 rows.'), {
  title: 'Reading the data',
  body: 'I pull Line-3 rows.',
});
assert.deepEqual(splitSummaryPart('No heading here.'), {
  title: '',
  body: 'No heading here.',
});

// 2. A compact outcome is derived only from fields the receipt returned.
assert.equal(callOutcome({ ok: true, rows: 1284, critical: 312 }), 'rows=1284 · critical=312');
assert.equal(callOutcome({ ok: false, error: 'column missing' }), 'column missing');
assert.equal(callOutcome('not a record'), '');

const history: WorkspaceEvent[] = [
  event('message', { role: 'user', text: 'How are Line-3 defects trending?' }, null),
  event('status', { state: 'running', label: 'Preparing' }),
  event('model_started', {
    model: 'gpt-6-luna',
    background: true,
    reasoning: { effort: 'medium', summary: 'auto' },
  }),
  event('reasoning', {
    text: '**Reading the ',
    item_id: 'rs_1',
    summary_index: 0,
    simulation: false,
  }),
  event('reasoning', {
    text: 'selected data**\n\nI need H1 counts.',
    item_id: 'rs_1',
    summary_index: 0,
    simulation: false,
  }),
  event('reasoning', {
    text: '**Checking cost coverage**\n\nSome repairs lack cost.',
    item_id: 'rs_1',
    summary_index: 1,
    final: true,
    simulation: false,
  }),
  event('reasoning_part', { phase: 'done', item_id: 'rs_1', summary_index: 1 }),
  event('output_item', {
    phase: 'done',
    item_type: 'reasoning',
    item_id: 'rs_1',
  }),
  event('output_item', {
    phase: 'added',
    item_type: 'function_call',
    item_id: 'fc_1',
    call_id: 'call_abc',
    name: 'analysis_query',
  }),
  event('tool_arguments', { phase: 'delta', item_id: 'fc_1', delta: '{"production_line":' }),
  event('tool_arguments', {
    phase: 'done',
    item_id: 'fc_1',
    arguments: '{"production_line":"Line-3"}',
  }),
  event('tool', {
    name: 'analysis_query',
    tool_id: 'tool-1',
    call_id: 'call_abc',
    status: 'running',
    code: 'SELECT count(*) FROM defects',
  }),
  event('tool', {
    name: 'analysis_query',
    tool_id: 'tool-1',
    call_id: 'call_abc',
    status: 'completed',
    code: 'SELECT count(*) FROM defects',
    result: { ok: true, event_count: 1284, critical_count: 312 },
    execution_id: 'exec-0123456789abcdef',
    code_sha256: 'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789',
    duration_ms: 812,
  }),
  event('tool', { name: 'memory_write', tool_id: 'tool-2', status: 'completed' }),
  event('usage', {
    response_id: 'resp_1',
    model: 'gpt-6-luna',
    input_tokens: 12480,
    cached_tokens: 1024,
    output_tokens: 2103,
    reasoning_tokens: 1500,
    cost_usd: 0.002134,
    elapsed_ms: 18600,
    estimated: true,
  }),
  event('usage', {
    scope: 'run',
    model: 'gpt-6-luna',
    input_tokens: 12480,
    cached_tokens: 1024,
    output_tokens: 2103,
    reasoning_tokens: 1500,
    cost_usd: 0.002134,
    elapsed_ms: 18600,
    estimated: true,
  }),
  event('qa', { status: 'verified', verified: true, issues: [] }),
  event('message', { role: 'assistant', text: 'Line-3 critical defects fell 12%.' }),
  event('status', { state: 'completed' }),
];
const snapshot = JSON.stringify(history);

const runs = projectAgentFlow(history);
assert.equal(JSON.stringify(history), snapshot, 'Projection never mutates stored events');
assert.deepEqual([...runs.keys()], ['run-1'], 'One flow per run');
const run = runs.get('run-1')!;

// 3. Run identity and provider configuration come from the emitted events.
assert.equal(run.prompt, 'How are Line-3 defects trending?');
assert.equal(run.model, 'gpt-6-luna');
assert.equal(run.effort, 'medium');
assert.equal(run.background, true);
assert.equal(run.state, 'completed');

// 4. Deltas rebuild one thinking block with the provider's two summary parts.
const thinking = run.steps.filter((step) => step.kind === 'thinking');
assert.equal(thinking.length, 1, 'Deltas sharing a reasoning item stay in one block');
assert.equal(thinking[0].kind === 'thinking' && thinking[0].done, true);
assert.deepEqual(
  thinking[0].kind === 'thinking' ? thinking[0].parts.map((part) => part.title) : [],
  ['Reading the selected data', 'Checking cost coverage'],
);
assert.deepEqual(
  thinking[0].kind === 'thinking' ? thinking[0].parts.map((part) => part.body) : [],
  ['I need H1 counts.', 'Some repairs lack cost.'],
);
assert.equal(
  thinking[0].kind === 'thinking' ? thinking[0].parts[1].done : false,
  true,
  'A completed summary part is marked done',
);

// 5. The streamed call, its arguments and its local receipt stay one step.
const calls = run.steps.filter((step) => step.kind === 'call');
assert.equal(calls.length, 2, 'Each distinct call keeps its own step');
const first = calls[0];
assert.ok(first.kind === 'call');
assert.equal(first.name, 'analysis_query');
assert.equal(first.status, 'completed');
assert.equal(first.streaming, false);
assert.equal(
  first.command,
  '{"production_line":"Line-3"}',
  'Product calls retain provider arguments instead of their implementation source',
);
assert.equal(first.implementationSource, 'SELECT count(*) FROM defects');
assert.equal(first.outcome, 'event_count=1284 · critical_count=312');
assert.equal(first.executionId, 'exec-0123456789abcdef');
assert.equal(first.durationMs, 812);
assert.equal(calls[1].kind === 'call' && calls[1].name, 'memory_write');

// 6. Bounded product tool module source does not clobber streamed JSON call arguments,
// and encrypted reasoning items without summary text still appear in the flow.
const boundedRun = projectAgentFlow([
  event('output_item', { phase: 'added', item_type: 'reasoning', item_id: 'rs_enc' }, 'run-b'),
  event('output_item', { phase: 'done', item_type: 'reasoning', item_id: 'rs_enc' }, 'run-b'),
  event(
    'output_item',
    {
      phase: 'added',
      item_type: 'function_call',
      item_id: 'fc_b',
      call_id: 'call_b',
      name: 'analysis_query',
    },
    'run-b',
  ),
  event(
    'tool_arguments',
    { phase: 'done', item_id: 'fc_b', arguments: '{"section":"overview"}' },
    'run-b',
  ),
  event(
    'tool',
    {
      name: 'analysis_query',
      tool_id: 'tool-b',
      call_id: 'call_b',
      status: 'completed',
      code: '"""Bounded product capabilities for the Manufacturing Quality Analyst."""\ndef run(): pass',
      result: { ok: true, rows: 10 },
    },
    'run-b',
  ),
]).get('run-b')!;
assert.equal(boundedRun.steps.length, 2);
assert.equal(boundedRun.steps[0].kind === 'thinking' && boundedRun.steps[0].done, true);
assert.equal(
  boundedRun.steps[1].kind === 'call' ? boundedRun.steps[1].command : '',
  '{"section":"overview"}',
  'Internal module source does not overwrite streamed JSON arguments',
);

// 7. A second run never inherits the first run's steps or prompt.
const twoRuns = projectAgentFlow([
  ...history,
  event('message', { role: 'user', text: 'And the cost impact?' }, null),
  event('status', { state: 'running' }, 'run-2'),
  event('reasoning', { text: 'Checking costs', item_id: 'rs_2', summary_index: 0 }, 'run-2'),
]);
assert.deepEqual([...twoRuns.keys()], ['run-1', 'run-2']);
assert.equal(twoRuns.get('run-2')!.prompt, 'And the cost impact?');
assert.equal(twoRuns.get('run-2')!.steps.length, 1);
assert.equal(twoRuns.get('run-1')!.steps.length, run.steps.length);

// 8. Events without a run identity never create a flow.
assert.equal(projectAgentFlow([event('reasoning', { text: 'orphan' }, null)]).size, 0);

// 9. Rendering shows the provider timeline in both languages and escapes input.
const injected = projectAgentFlow([
  event('status', { state: 'running' }, 'run-x'),
  event(
    'reasoning',
    { text: '**<script>alert(1)</script>**\n\nbody & "quoted"', item_id: 'rs_x', summary_index: 0 },
    'run-x',
  ),
  event(
    'tool',
    { name: 'local_python_repl', tool_id: 't', status: 'failed', code: '<b>x</b>' },
    'run-x',
  ),
]).get('run-x')!;
const injectedHtml = renderToStaticMarkup(
  <I18nProvider initialLocale="en" persist={false}>
    <AgentFlow run={injected} />
  </I18nProvider>,
);
assert.doesNotMatch(injectedHtml, /<script>|<b>x<\/b>/, 'Trace content stays inert');
assert.match(injectedHtml, /&lt;b&gt;x&lt;\/b&gt;/);
assert.doesNotMatch(injectedHtml, /alert\(1\)/, 'Unclassified provider prose is not promoted');

for (const locale of ['en', 'vi'] as const) {
  const markup = renderToStaticMarkup(
    <I18nProvider initialLocale={locale} persist={false}>
      <AgentFlow run={run} />
    </I18nProvider>,
  );
  assert.match(markup, /class="agent-flow is-done"/);
  assert.match(markup, /gpt-6-luna/);
  assert.match(markup, /analysis_query/);
  assert.match(markup, /production_line/);
  assert.doesNotMatch(
    markup,
    /SELECT count\(\*\) FROM defects/,
    'Implementation source is loaded only after opening receipt details',
  );
  assert.match(markup, /event_count=1284/);
  assert.match(markup, /exec exec-0123456/);
  assert.doesNotMatch(markup, /12\.5k|agent-flow-usage/);
  assert.doesNotMatch(markup, /Reading the selected data/);
  assert.match(
    markup,
    locale === 'vi' ? /Đang xem xét yêu cầu và bằng chứng/ : /Reviewing the request and evidence/,
  );
  assert.match(
    markup,
    locale === 'en' ? /Agent flow/ : /Luồng xử lý/,
    'The flow header is localized',
  );
  assert.match(
    markup,
    locale === 'en' ? />Thinking</ : />Suy luận</,
    'The thinking label is localized',
  );
  assert.doesNotMatch(markup, /ui\.agent_flow/, 'Every flow message has a translation');
}

const activeRun = projectAgentFlow([
  event('status', { state: 'running' }, 'run-a'),
  event(
    'output_item',
    { phase: 'added', item_type: 'function_call', item_id: 'f', name: 'q' },
    'run-a',
  ),
  event('tool_arguments', { phase: 'delta', item_id: 'f', delta: '{"a":' }, 'run-a'),
]).get('run-a')!;
const activeHtml = renderToStaticMarkup(
  <I18nProvider initialLocale="en" persist={false}>
    <AgentFlow run={activeRun} />
  </I18nProvider>,
);
assert.match(activeHtml, /class="agent-flow is-active"/);
assert.match(activeHtml, /flow-command is-streaming/, 'Arguments still arriving are marked live');
assert.match(activeHtml, /preparing/);

const emptyHtml = renderToStaticMarkup(
  <I18nProvider initialLocale="vi" persist={false}>
    <AgentFlow
      run={{
        runId: 'r',
        sessionId: 's',
        model: '',
        effort: '',
        background: false,
        state: 'running',
        prompt: '',
        steps: [],
        compactions: 0,
      }}
    />
  </I18nProvider>,
);
assert.match(emptyHtml, /Đang chờ sự kiện đầu tiên/);
assert.doesNotMatch(emptyHtml, /agent-flow-usage/, 'No usage footer without provider usage');

// Terminal states stop animations without inventing completion or a tool receipt.
for (const state of ['cancelled', 'failed', 'interrupted', 'incomplete', 'completed']) {
  const terminalRun = projectAgentFlow([
    event('output_item', { phase: 'added', item_type: 'reasoning', item_id: 'unfinished' }),
    event('output_item', { phase: 'added', item_type: 'function_call', item_id: 'pending' }),
    event('tool_arguments', { phase: 'delta', item_id: 'pending', delta: '{' }),
    event('status', { state }),
  ]).get('run-1')!;
  const terminalCall = terminalRun.steps.find((step) => step.kind === 'call')!;
  assert.equal(terminalCall.status, 'unconfirmed');
  assert.equal(terminalCall.streaming, false);
  const terminalHtml = renderToStaticMarkup(
    <I18nProvider initialLocale="en" persist={false}>
      <AgentFlow run={terminalRun} />
    </I18nProvider>,
  );
  assert.doesNotMatch(terminalHtml, /class="spin"|flow-command is-streaming/);
  assert.match(terminalHtml, /completion unconfirmed/);
}

// Distinct empty reasoning items remain distinct when no summaries were emitted.
const distinctReasoning = projectAgentFlow([
  event('output_item', { phase: 'added', item_type: 'reasoning', item_id: 'empty-1' }),
  event('output_item', { phase: 'done', item_type: 'reasoning', item_id: 'empty-1' }),
  event('output_item', { phase: 'added', item_type: 'reasoning', item_id: 'empty-2' }),
]).get('run-1')!;
assert.equal(distinctReasoning.steps.length, 2);
assert.equal(
  distinctReasoning.steps[0].kind === 'thinking' && distinctReasoning.steps[0].done,
  true,
);
assert.equal(
  distinctReasoning.steps[1].kind === 'thinking' && distinctReasoning.steps[1].done,
  false,
);

const manyEmptyNativeItems = Array.from({ length: 80 }, (_, index) => [
  event('output_item', {
    provider_api: 'agents',
    session_id: 'many-empty',
    turn_id: 'turn-empty',
    item_id: 'distinct-empty-' + index,
    item_type: 'reasoning',
    phase: 'added',
  }),
  event('output_item', {
    provider_api: 'agents',
    session_id: 'many-empty',
    turn_id: 'turn-empty',
    item_id: 'distinct-empty-' + index,
    item_type: 'reasoning',
    phase: 'done',
  }),
]).flat();
const condensedEmptyRun = projectAgentFlow([
  ...manyEmptyNativeItems,
  event('reasoning', {
    item_id: 'public-first',
    text: '**Reading real data**\n\nFirst public summary.',
    final: true,
  }),
  event('output_item', { item_id: 'public-first', item_type: 'reasoning', phase: 'done' }),
  event('tool', {
    name: 'ordered_analysis',
    tool_id: 'between-public-parts',
    status: 'running',
    code: '{"section":"overview"}',
  }),
  event('reasoning', {
    item_id: 'public-second',
    text: '**Checking real results**\n\nSecond public summary.',
    final: true,
  }),
  event('output_item', { item_id: 'public-second', item_type: 'reasoning', phase: 'done' }),
  event('output_item', { item_id: 'active-empty', item_type: 'reasoning', phase: 'added' }),
]).get('run-1')!;
assert.equal(
  condensedEmptyRun.steps.length,
  84,
  'Distinct provider items remain intact in the event projection',
);
for (const locale of ['en', 'vi'] as const) {
  const condensedMarkup = renderToStaticMarkup(
    <I18nProvider initialLocale={locale} persist={false}>
      <AgentFlow run={condensedEmptyRun} />
    </I18nProvider>,
  );
  assert.equal(
    (condensedMarkup.match(/class="flow-step is-thinking /g) || []).length,
    3,
    'Only actual public summaries and active thinking keep individual rows',
  );
  assert.doesNotMatch(
    condensedMarkup,
    /flow-summary-absence|No public summary for|mục suy luận đã hoàn tất không có tóm tắt/,
  );
  assert.doesNotMatch(condensedMarkup, /First public summary\.|Second public summary\./);
  const summaryStatus =
    locale === 'vi' ? 'Đang xem xét yêu cầu và bằng chứng' : 'Reviewing the request and evidence';
  assert.ok(condensedMarkup.indexOf(summaryStatus) < condensedMarkup.indexOf('ordered_analysis'));
  assert.ok(
    condensedMarkup.indexOf('ordered_analysis') < condensedMarkup.lastIndexOf(summaryStatus),
  );
  assert.match(condensedMarkup, /class="flow-thoughts"/);
  assert.doesNotMatch(condensedMarkup, /<details class="flow-body"/);
  assert.doesNotMatch(condensedMarkup, /ui\.agent_flow/);
}

// Reconciled snapshots replace partial summaries/arguments without duplicating steps.
const reconciled = projectAgentFlow([
  event('reasoning', { item_id: 'rs-replay', summary_index: 0, text: 'partial' }),
  event('reasoning_part', {
    item_id: 'rs-replay',
    summary_index: 0,
    phase: 'done',
    text: '**Restored**\n\nFull summary.',
  }),
  event('output_item', {
    item_id: 'fc-replay',
    call_id: 'call-replay',
    item_type: 'function_call',
    phase: 'added',
    name: 'query',
  }),
  event('tool_arguments', { item_id: 'fc-replay', phase: 'delta', delta: '{"section":' }),
  event('output_item', {
    item_id: 'fc-replay',
    call_id: 'call-replay',
    item_type: 'function_call',
    phase: 'added',
    name: 'query',
    reconciled: true,
  }),
  event('tool_arguments', {
    item_id: 'fc-replay',
    phase: 'done',
    arguments: '{"section":"overview"}',
    reconciled: true,
  }),
  event('output_item', {
    item_id: 'fc-replay',
    item_type: 'function_call',
    phase: 'done',
    status: 'completed',
  }),
  event('compaction_progress', { phase: 'compacting' }),
  event('compaction', { id: 'compact-1' }),
  event('compaction', { id: 'compact-1', reconciled: true }),
  event('usage', { response_id: 'response-1', input_tokens: 10 }),
  event('usage', { response_id: 'response-1', input_tokens: 10 }),
]).get('run-1')!;
assert.equal(reconciled.steps.length, 2);
const replayThinking = reconciled.steps[0];
assert.equal(replayThinking.kind === 'thinking' && replayThinking.parts[0].body, 'Full summary.');
const replayCall = reconciled.steps[1];
assert.equal(replayCall.kind === 'call' && replayCall.command, '{"section":"overview"}');
assert.equal(
  replayCall.kind === 'call' && replayCall.status,
  'pending',
  'Argument completion does not invent execution success',
);
assert.equal(reconciled.compactions, 1);

// Acceptance of a detail stays distinct from its actual application to this run.
for (const state of ['running', 'qa']) {
  const steeringEvents = [
    event('status', { state }),
    event('steering_received', { message: 'Compare Critical events too.' }),
  ];
  const pendingSteering = projectAgentFlow(steeringEvents).get('run-1')!;
  assert.equal(pendingSteering.state, state, 'Receiving a detail never queues the active run');
  assert.equal(
    pendingSteering.steps[0].kind === 'note' && pendingSteering.steps[0].label,
    'steering_received',
  );
  const submittedEvents = [
    ...steeringEvents,
    event('steering_submitted', { message: 'Compare Critical events too.' }),
  ];
  const submittedSteering = projectAgentFlow(submittedEvents).get('run-1')!;
  assert.equal(submittedSteering.state, state);
  const appliedSteering = projectAgentFlow([
    ...submittedEvents,
    event('steering_applied', { message: 'Compare Critical events too.' }),
  ]).get('run-1')!;
  assert.equal(appliedSteering.state, state);
  assert.deepEqual(
    appliedSteering.steps.map((step) => step.kind === 'note' && step.label),
    ['steering_received', 'steering_submitted', 'steering'],
  );
  for (const locale of ['en', 'vi'] as const) {
    const pendingMarkup = renderToStaticMarkup(
      <I18nProvider initialLocale={locale} persist={false}>
        <AgentFlow run={pendingSteering} />
      </I18nProvider>,
    );
    assert.match(
      pendingMarkup,
      locale === 'en'
        ? /Instructions received for this analysis/
        : /Đã nhận hướng dẫn cho phân tích này/,
    );
    assert.doesNotMatch(
      pendingMarkup,
      /Instructions applied|Đã áp dụng hướng dẫn|Your request is queued|Yêu cầu của bạn đang chờ/,
    );
    const submittedMarkup = renderToStaticMarkup(
      <I18nProvider initialLocale={locale} persist={false}>
        <AgentFlow run={submittedSteering} />
      </I18nProvider>,
    );
    assert.match(
      submittedMarkup,
      locale === 'en'
        ? /Instructions sent to the active analysis/
        : /Đã gửi hướng dẫn vào phân tích đang chạy/,
    );
    assert.doesNotMatch(
      submittedMarkup,
      /Instructions applied|Đã áp dụng hướng dẫn|Your request is queued/,
    );
    const failedSteering = projectAgentFlow([
      ...steeringEvents,
      event('steering_failed', { error_type: 'PrivateProviderTransportError' }),
    ]).get('run-1')!;
    assert.equal(failedSteering.state, state);
    const failedMarkup = renderToStaticMarkup(
      <I18nProvider initialLocale={locale} persist={false}>
        <AgentFlow run={failedSteering} />
      </I18nProvider>,
    );
    assert.match(
      failedMarkup,
      locale === 'en'
        ? /Instruction delivery could not be confirmed/
        : /Chưa thể xác nhận hướng dẫn đã được gửi/,
    );
    assert.doesNotMatch(
      failedMarkup,
      /PrivateProviderTransportError|Instructions applied|Đã áp dụng hướng dẫn/,
    );
    const appliedMarkup = renderToStaticMarkup(
      <I18nProvider initialLocale={locale} persist={false}>
        <AgentFlow run={appliedSteering} />
      </I18nProvider>,
    );
    assert.match(appliedMarkup, locale === 'en' ? /Instructions applied/ : /Đã áp dụng hướng dẫn/);
    const receivedLabel =
      locale === 'en'
        ? 'Instructions received for this analysis'
        : 'Đã nhận hướng dẫn cho phân tích này';
    const submittedLabel =
      locale === 'en'
        ? 'Instructions sent to the active analysis'
        : 'Đã gửi hướng dẫn vào phân tích đang chạy';
    const appliedLabel = locale === 'en' ? 'Instructions applied' : 'Đã áp dụng hướng dẫn';
    const appliedTimeline = appliedMarkup.match(/<ol[^>]*>([\s\S]*?)<\/ol>/)?.[1] || '';
    assert.ok(appliedTimeline.indexOf(receivedLabel) < appliedTimeline.indexOf(submittedLabel));
    assert.ok(appliedTimeline.indexOf(submittedLabel) < appliedTimeline.indexOf(appliedLabel));
  }
}

// Public answer deltas are labelled drafts; final reports are never duplicated.
const draftEvents = [
  event('text_delta', { item_id: 'message-1', delta: 'Partial ' }),
  event('text_done', { item_id: 'message-1', text: 'Complete draft' }),
];
const draft = projectAgentFlow(draftEvents).get('run-1')!;
assert.equal(draft.steps[0].kind === 'draft' && draft.steps[0].text, 'Complete draft');
const draftHtml = renderToStaticMarkup(
  <I18nProvider initialLocale="en" persist={false}>
    <AgentFlow run={draft} />
  </I18nProvider>,
);
assert.match(draftHtml, /not yet verified/);
const accepted = projectAgentFlow([
  ...draftEvents,
  event('message', { role: 'assistant', text: 'Accepted report unique text' }),
]).get('run-1')!;
assert.equal(
  accepted.steps.some((step) => step.kind === 'draft'),
  false,
);
const acceptedHtml = renderToStaticMarkup(
  <I18nProvider initialLocale="en" persist={false}>
    <AgentFlow run={accepted} />
  </I18nProvider>,
);
assert.doesNotMatch(acceptedHtml, /Complete draft|Accepted report unique text/);
assert.match(acceptedHtml, /The report appears below this flow/);

const rawReasoning = projectAgentFlow([
  event('reasoning_text', { item_id: 'private', delta: 'raw reasoning' }),
]).get('run-1')!;
assert.equal(rawReasoning.steps.length, 0, 'Only public provider summaries enter the flow');

const syntheticSummary = projectAgentFlow([
  event('output_item', { phase: 'added', item_type: 'reasoning', item_id: 'synthetic-item' }),
  event('reasoning', {
    item_id: 'synthetic-item',
    text: 'Synthetic workflow example.',
    simulation: true,
  }),
  event('reasoning', {
    item_id: 'synthetic-item',
    text: 'Synthetic workflow example.',
    final: true,
  }),
]).get('run-1')!;
assert.equal(
  syntheticSummary.steps[0].kind === 'thinking' && syntheticSummary.steps[0].simulation,
  true,
  'A later synthetic summary marks the existing provider item and never loses its simulation disclosure',
);
const syntheticMarkup = renderToStaticMarkup(
  <I18nProvider initialLocale="en" persist={false}>
    <AgentFlow run={syntheticSummary} />
  </I18nProvider>,
);
assert.match(syntheticMarkup, />simulated</);

const pythonDefinition = projectAgentFlow([
  event('tool', {
    name: 'local_python_repl',
    tool_id: 'python-function',
    status: 'completed',
    code: 'def normalize(value):\n    return value.strip()',
  }),
]).get('run-1')!.steps[0];
assert.equal(
  pythonDefinition.kind === 'call' && pythonDefinition.command.startsWith('def normalize'),
  true,
  'Executed user Python definitions are not mistaken for internal tool source',
);

const changedModuleSource =
  '"""Product capabilities over the same immutable analysis service as the UI."""\nfrom __future__ import annotations\nimport json\ndef run(): pass';
const productArguments = projectAgentFlow([
  event('tool', {
    name: 'analysis_query',
    tool_id: 'product-start',
    status: 'running',
    code: '{"section":"overview"}',
  }),
  event('tool', {
    name: 'analysis_query',
    tool_id: 'product-start',
    status: 'completed',
    code: changedModuleSource,
    result: { ok: true },
  }),
  event('tool', {
    name: 'analysis_export',
    tool_id: 'product-explicit',
    status: 'completed',
    arguments: { format: 'markdown' },
    code: 'arbitrary implementation source with no recognizable header',
  }),
  event('tool', {
    name: 'analysis_compare',
    tool_id: 'completion-only',
    status: 'completed',
    code: changedModuleSource,
  }),
  event('tool', {
    name: 'local_sql_engine',
    tool_id: 'sql-command',
    status: 'completed',
    code: 'SELECT COUNT(*) FROM defects',
  }),
]).get('run-1')!;
assert.equal(
  productArguments.steps[0].kind === 'call' && productArguments.steps[0].command,
  '{"section":"overview"}',
);
assert.equal(
  productArguments.steps[1].kind === 'call' &&
    productArguments.steps[1].command.includes('markdown'),
  true,
);
assert.equal(
  productArguments.steps[2].kind === 'call' && productArguments.steps[2].command,
  '',
  'A completion-only product receipt never promotes implementation source into invocation arguments',
);
assert.equal(
  productArguments.steps[3].kind === 'call' && productArguments.steps[3].command,
  'SELECT COUNT(*) FROM defects',
);
const productMarkup = renderToStaticMarkup(
  <I18nProvider initialLocale="en" persist={false}>
    <AgentFlow run={productArguments} />
  </I18nProvider>,
);
assert.doesNotMatch(productMarkup, /from __future__|Product capabilities|arbitrary implementation/);
assert.match(productMarkup, /section|markdown|SELECT COUNT/);

// Actual native continuation replay repeats added boundaries with new IDs and
// full summary deltas even when the initial stream has not emitted a final part.
const replayBase = {
  provider_api: 'agents',
  session_id: 'native-replay-session',
  turn_id: 'native-replay-turn',
  item_id: 'native-replay-reasoning',
  summary_index: 0,
};
const replayPrefix = [
  event('output_item', {
    ...replayBase,
    phase: 'added',
    item_type: 'reasoning',
    event_id: 'replay-added-1',
  }),
  event('reasoning_part', { ...replayBase, phase: 'added', event_id: 'replay-part-1' }),
  event('reasoning', {
    ...replayBase,
    text: '**Adjusting wait time**\n\nPublic summary.',
    event_id: 'replay-delta-1',
  }),
  event('output_item', {
    ...replayBase,
    phase: 'added',
    item_type: 'reasoning',
    event_id: 'replay-added-2',
  }),
  event('reasoning_part', { ...replayBase, phase: 'added', event_id: 'replay-part-2' }),
  event('reasoning', {
    ...replayBase,
    text: '**Adjusting wait time**\n\nPublic summary.',
    event_id: 'replay-delta-2',
  }),
];
const unfinishedReplay = projectAgentFlow(replayPrefix).get('run-1')!;
assert.equal(
  unfinishedReplay.steps[0].kind === 'thinking' && unfinishedReplay.steps[0].parts[0].body,
  'Public summary.',
);
const finishedReplay = projectAgentFlow([
  ...replayPrefix,
  event('reasoning_part', {
    ...replayBase,
    phase: 'done',
    text: '**Adjusting wait time**\n\nFinal public summary.',
  }),
  event('output_item', { ...replayBase, phase: 'done', item_type: 'reasoning' }),
  event('output_item', {
    ...replayBase,
    phase: 'added',
    item_type: 'reasoning',
    event_id: 'replay-added-3',
  }),
  event('reasoning_part', { ...replayBase, phase: 'added', event_id: 'replay-part-3' }),
  event('reasoning', {
    ...replayBase,
    text: '**Adjusting wait time**\n\nStale public summary.',
    event_id: 'replay-delta-3',
  }),
]).get('run-1')!;
assert.equal(finishedReplay.steps.length, 1);
assert.equal(finishedReplay.steps[0].kind === 'thinking' && finishedReplay.steps[0].done, true);
assert.equal(
  finishedReplay.steps[0].kind === 'thinking' && finishedReplay.steps[0].parts[0].body,
  'Final public summary.',
);
const finalReplacement = projectAgentFlow([
  ...replayPrefix,
  event('reasoning', { ...replayBase, text: 'Final summary.', final: true }),
  event('reasoning', {
    ...replayBase,
    text: 'Authoritative snapshot.',
    final: true,
    reconciled: true,
  }),
]).get('run-1')!;
assert.equal(
  finalReplacement.steps[0].kind === 'thinking' && finalReplacement.steps[0].parts[0].body,
  'Authoritative snapshot.',
);
const compactProviderPhases = projectAgentFlow([
  event('provider_phase', {
    provider_api: 'agents',
    session_id: 'phase-session',
    phase: 'created',
  }),
  event('memory', { action: 'memory_recall' }),
  event('provider_phase', {
    provider_api: 'agents',
    session_id: 'phase-session',
    phase: 'in_progress',
  }),
  event('provider_phase', {
    provider_api: 'agents',
    session_id: 'phase-session',
    phase: 'requires_action',
  }),
]).get('run-1')!;
const providerNotes = compactProviderPhases.steps.filter(
  (step) => step.kind === 'note' && step.label === 'provider',
);
assert.equal(
  providerNotes.length,
  1,
  'Provider phases keep one latest activity note per native session',
);
const latestProviderNote = providerNotes[0];
assert.ok(latestProviderNote.kind === 'note');
assert.equal(latestProviderNote.detail, 'requires_action');

for (const locale of ['en', 'vi'] as const) {
  const preparation = projectAgentFlow([
    event('status', { state: 'running' }),
    event('memory', { action: 'knowledge_prepare' }),
    event('memory', { action: 'memory_recall' }),
    event('qa', { status: 'verified', issues: [] }),
  ]).get('run-1')!;
  const preparationHtml = renderToStaticMarkup(
    <I18nProvider initialLocale={locale} persist={false}>
      <AgentFlow run={preparation} />
    </I18nProvider>,
  );
  assert.doesNotMatch(preparationHtml, /knowledge_prepare|memory_recall|>verified</);
  assert.match(
    preparationHtml,
    locale === 'en' ? /Preparing analysis guidance/ : /Chuẩn bị hướng dẫn phân tích/,
  );
  assert.doesNotMatch(preparationHtml, /Working on your question|Đang phân tích câu hỏi/);
}

// Native managed events preserve public summaries, identities and delivery state.
const native = (data: Record<string, unknown>) => ({
  provider_api: 'agents',
  session_id: 'managed-1',
  turn_id: 'turn-1',
  ...data,
});
const nativeReplay = event(
  'reasoning_summary',
  native({
    event_id: 'native-summary-1',
    item_id: 'reason-native',
    delta: '**Checking**\n\nPublic ',
  }),
);
const nativeHistory = [
  event('status', { state: 'running' }),
  nativeReplay,
  nativeReplay,
  event(
    'reasoning_summary',
    native({
      event_id: 'native-summary-2',
      item_id: 'reason-native',
      text: '**Checking**\n\nPublic summary.',
      done: true,
    }),
  ),
  event(
    'output_item',
    native({
      event_id: 'native-item-done',
      phase: 'done',
      item_id: 'reason-native',
      item_type: 'reasoning',
    }),
  ),
  event('provider_phase', native({ event_id: 'native-turn-done', phase: 'completed' })),
  event('usage', {
    provider_api: 'agents',
    provider_session_id: 'managed-1',
    provider_turn_id: 'turn-1',
    usage_scope: 'session_cumulative',
    boundary_id: 'b1',
    input_tokens: 100,
    output_tokens: 10,
    cached_tokens: 20,
    estimated_token_cost_usd: 0.001,
    cost_usd: 1000,
    cost_is_reservation: true,
    elapsed_ms: 10,
  }),
  event('usage', {
    provider_api: 'agents',
    provider_session_id: 'managed-1',
    provider_turn_id: 'turn-1',
    usage_scope: 'session_cumulative',
    boundary_id: 'b1',
    input_tokens: 100,
    output_tokens: 10,
    cached_tokens: 20,
    estimated_token_cost_usd: 0.001,
    cost_usd: 1000,
    cost_is_reservation: true,
    elapsed_ms: 10,
  }),
  event('usage', {
    provider_api: 'agents',
    provider_session_id: 'managed-1',
    provider_turn_id: 'turn-2',
    usage_scope: 'session_cumulative',
    boundary_id: 'b2',
    input_tokens: 180,
    output_tokens: 22,
    cached_tokens: null,
    estimated_token_cost_usd: 0.002,
    elapsed_ms: 20,
  }),
  event('usage', {
    provider_api: 'agents',
    provider_session_id: 'managed-2',
    usage_scope: 'session_cumulative',
    boundary_id: 'b3',
    input_tokens: 50,
    output_tokens: 5,
    cost_usd: null,
  }),
  event('usage', { scope: 'run', input_tokens: 999999, cost_usd: 999999 }),
];
const nativeRun = projectAgentFlow(nativeHistory).get('run-1')!;
assert.equal(
  nativeRun.state,
  'running',
  'A completed provider turn does not claim application delivery success',
);
assert.equal(nativeRun.steps.filter((step) => step.kind === 'thinking').length, 1);
assert.equal(
  nativeRun.steps[0].kind === 'thinking' && nativeRun.steps[0].parts[0].body,
  'Public summary.',
);
const reusedNativeIds = projectAgentFlow([
  event(
    'reasoning_summary',
    native({ event_id: 'same-event', item_id: 'same-item', delta: 'First turn' }),
  ),
  event(
    'reasoning_summary',
    native({
      event_id: 'same-event',
      item_id: 'same-item',
      turn_id: 'turn-2',
      delta: 'Second turn',
    }),
  ),
]).get('run-1')!;
assert.equal(
  reusedNativeIds.steps.length,
  2,
  'Provider identities stay scoped to their managed session and turn',
);
assert.deepEqual(
  reusedNativeIds.steps.map((step) => step.kind === 'thinking' && step.parts[0].body),
  ['First turn', 'Second turn'],
);

// Durable reconciliation joins provider replay aliases without changing source
// history or collapsing genuine saved items that happen to repeat public text.
const replaySummaries = ['First public section', 'Second public section', 'Third public section'];
function replayedReasoning(itemId: string, turnId = 'turn-1') {
  const identity = native({ item_id: itemId, turn_id: turnId });
  return [
    event('output_item', { ...identity, phase: 'added', item_type: 'reasoning' }),
    ...replaySummaries.map((text, summary_index) =>
      event('reasoning', { ...identity, text, summary_index, final: true }),
    ),
    event('output_item', { ...identity, phase: 'done', item_type: 'reasoning' }),
  ];
}
const aliasReconciliation = event(
  'reasoning_alias',
  native({
    item_id: 'transient-replay',
    canonical_item_id: 'durable-original',
    source: 'session_snapshot',
    reconciled: true,
  }),
);
const aliasHistory = [
  ...replayedReasoning('durable-original'),
  ...replayedReasoning('transient-replay'),
  aliasReconciliation,
];
const aliasSourceCopy = structuredClone(aliasHistory);
const reconciledAlias = projectAgentFlow(aliasHistory).get('run-1')!;
assert.equal(reconciledAlias.steps.length, 1, 'One flow item per confirmed durable identity');
assert.deepEqual(
  reconciledAlias.steps[0].kind === 'thinking'
    ? reconciledAlias.steps[0].parts.map((part) => part.body)
    : [],
  replaySummaries,
);
assert.deepEqual(aliasHistory, aliasSourceCopy, 'Replay reconciliation keeps source events intact');
const distinctAliasReasoning = projectAgentFlow([
  ...aliasHistory,
  ...replayedReasoning('genuine-saved-repeat'),
  ...replayedReasoning('transient-replay', 'turn-2'),
]).get('run-1')!;
assert.equal(distinctAliasReasoning.steps.length, 3, 'Saved repeats and other turns stay distinct');
const unconfirmedAlias = projectAgentFlow([
  ...aliasHistory.slice(0, -1),
  { ...aliasReconciliation, data: { ...aliasReconciliation.data, reconciled: false } },
]).get('run-1')!;
assert.equal(unconfirmedAlias.steps.length, 2, 'Only durable alias confirmations merge items');

// Hidden usage metadata stays in source history, without frontend accounting.
for (const metadata of [
  { scope: 'run' },
  { response_id: 'response-usage-only' },
  {
    provider_api: 'agents',
    provider_session_id: 'usage-only-session',
    usage_scope: 'session_cumulative',
  },
]) {
  const usageEvent = event('usage', {
    ...metadata,
    model: 'gpt-6-luna',
    input_tokens: 100,
    output_tokens: null,
    cost_usd: 500,
    cost_is_reservation: true,
  });
  const sourceCopy = structuredClone(usageEvent);
  const metadataRun = projectAgentFlow([
    event('status', { state: 'running' }),
    usageEvent,
    event('reasoning', { item_id: 'public-step', text: 'Checking the selected data.' }),
  ]).get('run-1')!;
  assert.equal(metadataRun.model, 'gpt-6-luna', 'Usage-only model metadata remains visible');
  assert.equal(metadataRun.state, 'running');
  assert.deepEqual(usageEvent, sourceCopy, 'Presentation cleanup never mutates source events');
  assert.equal(Object.hasOwn(metadataRun, 'usage'), false);
  assert.equal(Object.hasOwn(metadataRun, 'providerSessionIds'), false);
  assert.equal(Object.hasOwn(metadataRun, 'providerTurnIds'), false);
  for (const locale of ['en', 'vi'] as const) {
    const markup = renderToStaticMarkup(
      <I18nProvider initialLocale={locale} persist={false}>
        <AgentFlow run={metadataRun} />
      </I18nProvider>,
    );
    assert.match(markup, /gpt-6-luna/);
    assert.doesNotMatch(markup, /Checking the selected data/);
    assert.match(
      markup,
      locale === 'vi' ? /Đang xem xét yêu cầu và bằng chứng/ : /Reviewing the request and evidence/,
    );
    assert.doesNotMatch(
      markup,
      /usage-only-session|agent-flow-usage|flow-provider-details|Token usage|best effort|Provider session|Chưa có số liệu token|hóa đơn chưa được chốt|\$500|ui\.agent_flow/,
    );
  }
}

const cancelledTool = projectAgentFlow([
  event('tool', { name: 'analysis_query', tool_id: 'cancel-me', status: 'cancelled' }),
  event('status', { state: 'cancelled' }),
]).get('run-1')!;
assert.equal(cancelledTool.steps[0].kind === 'call' && cancelledTool.steps[0].status, 'cancelled');
const cancelledMarkup = renderToStaticMarkup(
  <I18nProvider initialLocale="en" persist={false}>
    <AgentFlow run={cancelledTool} />
  </I18nProvider>,
);
assert.doesNotMatch(cancelledMarkup, /class="spin"/);
assert.match(cancelledMarkup, /cancelled/);

// Cleanup without a provider request must not invent a cancellation. A sent
// request is distinct from a confirmed terminal stop.
const unattemptedCleanup = projectAgentFlow([
  event('provider_cancel', {
    provider_api: 'agents',
    attempted: false,
    session_id: null,
    terminal_confirmed: false,
  }),
  event('error', { code: 'validation_error', message: 'Provider session creation failed.' }),
  event('status', { state: 'failed' }),
]).get('run-1')!;
assert.equal(
  unattemptedCleanup.steps.some((step) => step.kind === 'note'),
  false,
);
// This exact native cleanup shape provides `status` without a phase field.
// Its settled idle session confirms that processing has stopped.
const statusOnlyIdleCleanup = projectAgentFlow([
  event('provider_cancel', { status: 'idle', attempted: true, terminal_confirmed: true }),
]).get('run-1')!;
const idleCleanupNote = statusOnlyIdleCleanup.steps[0];
assert.ok(idleCleanupNote.kind === 'note');
assert.equal(idleCleanupNote.label, 'stopped');
assert.equal(idleCleanupNote.detail, '');
for (const locale of ['en', 'vi'] as const) {
  const idleMarkup = renderToStaticMarkup(
    <I18nProvider initialLocale={locale} persist={false}>
      <AgentFlow run={statusOnlyIdleCleanup} />
    </I18nProvider>,
  );
  assert.match(idleMarkup, locale === 'en' ? /Analysis stopped/ : /Đã dừng phân tích/);
  assert.doesNotMatch(
    idleMarkup,
    /Provider session|Phiên nhà cung cấp|lượt đang chờ|turn idle|Stop requested|Đã yêu cầu dừng|ui\.agent_flow/,
  );
}
for (const status of ['unknown', 'idle', 'completed', 'failed', 'cancelled']) {
  for (const terminalConfirmed of [false, true]) {
    const cleanup = projectAgentFlow([
      event('provider_cancel', { attempted: true, status, terminal_confirmed: terminalConfirmed }),
    ]).get('run-1')!;
    const expectedStopped =
      terminalConfirmed && ['idle', 'completed', 'cancelled', 'failed'].includes(status);
    assert.equal(
      cleanup.steps[0].kind === 'note' && cleanup.steps[0].label,
      expectedStopped ? 'stopped' : 'cancel_requested',
    );
    for (const locale of ['en', 'vi'] as const) {
      const cleanupMarkup = renderToStaticMarkup(
        <I18nProvider initialLocale={locale} persist={false}>
          <AgentFlow run={cleanup} />
        </I18nProvider>,
      );
      assert.doesNotMatch(cleanupMarkup, /ui\.agent_flow|Provider session|Phiên nhà cung cấp/);
      if (!expectedStopped) {
        assert.doesNotMatch(cleanupMarkup, /Analysis stopped|Đã dừng phân tích/);
        assert.match(cleanupMarkup, locale === 'en' ? /completion unconfirmed/ : /chưa xác nhận/);
        assert.match(cleanupMarkup, locale === 'en' ? /Stop requested/ : /Đã yêu cầu dừng/);
      } else {
        assert.match(cleanupMarkup, locale === 'en' ? /Analysis stopped/ : /Đã dừng phân tích/);
        if (status === 'failed')
          assert.match(
            cleanupMarkup,
            locale === 'en' ? /Analysis ended with an error/ : /Phân tích kết thúc do lỗi/,
          );
      }
    }
  }
}
for (const locale of ['en', 'vi'] as const) {
  const cleanupMarkup = renderToStaticMarkup(
    <I18nProvider initialLocale={locale} persist={false}>
      <AgentFlow run={unattemptedCleanup} />
    </I18nProvider>,
  );
  assert.doesNotMatch(
    cleanupMarkup,
    />Analysis stopped<|>Đã dừng phân tích<|Stop requested|Đã yêu cầu dừng/,
  );
}

// 10. Streaming tool arguments, rationale parsing, and plan extraction
const streamingHistory = [
  event('status', { state: 'running' }, 'run-stream'),
  event(
    'output_item',
    {
      phase: 'added',
      item_type: 'function_call',
      item_id: 'fc_stream',
      call_id: 'call_stream',
      name: 'analysis_query',
    },
    'run-stream',
  ),
  event(
    'tool_arguments_delta',
    {
      item_id: 'fc_stream',
      call_id: 'call_stream',
      delta:
        '{"rationale":"Check defect trends","plan":["Inspect Line-3","Compare costs"],"query":"SELECT',
    },
    'run-stream',
  ),
];
const streamedRun = projectAgentFlow(streamingHistory).get('run-stream')!;
const streamedCall = streamedRun.steps.find((s) => s.kind === 'call');
assert.ok(streamedCall && streamedCall.kind === 'call');
assert.equal(
  streamedCall.streaming,
  true,
  'Streaming call maintains streaming = true while arriving',
);
assert.equal(streamedCall.command.startsWith('{"rationale":'), true);
assert.equal(
  streamedCall.rationale,
  'Check defect trends',
  'Rationale is parsed and stored on FlowCall',
);

const planStep = streamedRun.steps.find((s) => s.kind === 'note' && s.label === 'plan');
assert.ok(
  planStep && planStep.kind === 'note',
  'Plan is extracted and projected as a FlowNote step',
);
assert.equal(planStep.detail, 'Inspect Line-3 → Compare costs');
assert.ok(
  streamedRun.steps.indexOf(planStep) < streamedRun.steps.indexOf(streamedCall),
  'Plan step is projected before the tool call',
);

// Append more delta and verify completion
const completedStreamHistory = [
  ...streamingHistory,
  event(
    'tool_arguments_delta',
    {
      item_id: 'fc_stream',
      call_id: 'call_stream',
      delta: ' count(*) FROM defects"}',
    },
    'run-stream',
  ),
  event(
    'tool',
    {
      name: 'analysis_query',
      tool_id: 'tool_s1',
      call_id: 'call_stream',
      status: 'running',
    },
    'run-stream',
  ),
  event(
    'tool',
    {
      name: 'analysis_query',
      tool_id: 'tool_s1',
      call_id: 'call_stream',
      status: 'completed',
      result: { ok: true, rows: 100 },
    },
    'run-stream',
  ),
];
const completedRun = projectAgentFlow(completedStreamHistory).get('run-stream')!;
const completedCall = completedRun.steps.find((s) => s.kind === 'call');
assert.ok(completedCall && completedCall.kind === 'call');
assert.equal(
  completedCall.streaming,
  false,
  'Streaming call resets streaming = false once status is completed',
);
assert.equal(completedCall.status, 'completed');
assert.equal(completedCall.rationale, 'Check defect trends');

// Replayed persisted events must not duplicate unfinished provider deltas.
const persistedReplay = event('reasoning', { item_id: 'persisted', text: 'One summary.' });
const once = projectAgentFlow([persistedReplay, persistedReplay]).get('run-1')!;
assert.equal(once.steps[0].kind === 'thinking' && once.steps[0].parts[0].body, 'One summary.');

// Public commentary survives accepted delivery, while structured final output
// stays hidden even before the app has delivered its verified report.
const phaseHistory = [
  event('output_item', {
    item_id: 'commentary',
    item_type: 'message',
    phase: 'added',
    message_phase: 'commentary',
  }),
  event('text_done', {
    item_id: 'commentary',
    content_index: 0,
    text: 'I will compare both halves.',
  }),
  event('text_done', {
    item_id: 'commentary',
    content_index: 1,
    text: 'Then I will check cost coverage.',
  }),
  event('text_delta', { item_id: 'commentary', content_index: 0, delta: 'Stale replay' }),
  event('text_done', {
    item_id: 'structured',
    text: '{"report":"Never show raw structured output"}',
  }),
  event('text_done', { item_id: 'final', text: 'Unverified final narrative' }),
  event('output_item', {
    item_id: 'final',
    item_type: 'message',
    phase: 'done',
    message_phase: 'final_answer',
  }),
];
const phasesBeforeDelivery = projectAgentFlow(phaseHistory).get('run-1')!;
assert.deepEqual(
  phasesBeforeDelivery.steps.filter((step) => step.kind === 'draft').map((step) => step.text),
  ['I will compare both halves.', 'Then I will check cost coverage.'],
);
const phasesAfterDelivery = projectAgentFlow([
  ...phaseHistory,
  event('message', { role: 'assistant', text: 'Verified report' }),
  event('status', { state: 'completed' }),
]).get('run-1')!;
assert.equal(phasesAfterDelivery.steps.filter((step) => step.kind === 'draft').length, 2);

// Timing comes from saved timestamps, with no invented duration or aspect count.
const noTiming = projectAgentFlow([
  {
    ...event('reasoning', { item_id: 'no-timing', text: 'One public section.', final: true }),
    time: '',
  },
  {
    ...event('output_item', { item_id: 'no-timing', item_type: 'reasoning', phase: 'done' }),
    time: '',
  },
  event('status', { state: 'completed' }),
]).get('run-1')!;
for (const locale of ['en', 'vi'] as const) {
  const markup = renderToStaticMarkup(
    <I18nProvider initialLocale={locale} persist={false}>
      <AgentFlow run={noTiming} />
    </I18nProvider>,
  );
  assert.doesNotMatch(markup, /1\.2s|aspects|khía cạnh|Thought for|Đã suy nghĩ/);
  assert.match(markup, locale === 'vi' ? /Trạng thái tác vụ/ : /Task status/);
  assert.doesNotMatch(markup, /1 phần tóm tắt|1 summary section/);
}
const timed = projectAgentFlow([event('reasoning', { item_id: 'timed', text: 'Checking.' })]).get(
  'run-1',
)!;
assert.equal(
  timed.steps[0].kind === 'thinking' && timed.steps[0].startedAt,
  Date.parse('2026-10-17T00:00:00Z'),
);

// Failed and cancelled calls cannot resume their argument cursor on replay.
for (const status of ['failed', 'cancelled']) {
  const stoppedCall = projectAgentFlow([
    event('tool_arguments', {
      call_id: 'terminal-call',
      arguments: '{"scope":"Line-3"}',
      phase: 'done',
    }),
    event('tool', {
      call_id: 'terminal-call',
      tool_id: 'terminal-tool',
      name: 'analysis_query',
      status,
    }),
    event('tool_arguments_delta', { call_id: 'terminal-call', delta: 'stale' }),
  ])
    .get('run-1')!
    .steps.find((step) => step.kind === 'call');
  assert.ok(stoppedCall && stoppedCall.kind === 'call');
  assert.equal(stoppedCall.status, status);
  assert.equal(stoppedCall.streaming, false);
  assert.equal(stoppedCall.command, '{"scope":"Line-3"}');
}

// Provider transport lifecycle and model configuration stay in opt-in details.
// Replayed phases update their own turn, without adding generic timeline rows.
const lifecycleRun = projectAgentFlow([
  event('status', { state: 'running' }),
  event('model_started', { model: 'gpt-6-luna', background: true, reasoning: { effort: 'max' } }),
  event('memory', { action: 'knowledge_prepare' }),
  event('provider_phase', native({ phase: 'created' })),
  event('provider_phase', native({ phase: 'in_progress' })),
  event('provider_phase', native({ phase: 'requires_action' })),
  event('provider_phase', native({ phase: 'in_progress', turn_id: 'turn-2' })),
  event('qa', { status: 'retrying', issues: [] }),
]).get('run-1')!;
assert.deepEqual(
  lifecycleRun.steps.flatMap((step) =>
    step.kind === 'note' && step.label === 'provider' ? [step.detail] : [],
  ),
  ['requires_action', 'in_progress'],
);
for (const locale of ['en', 'vi'] as const) {
  const markup = renderToStaticMarkup(
    <I18nProvider initialLocale={locale} persist={false}>
      <AgentFlow run={lifecycleRun} />
    </I18nProvider>,
  );
  const header = markup.match(/<header[^>]*>([\s\S]*?)<\/header>/)?.[1] ?? '';
  const timeline = markup.match(/<ol[^>]*>([\s\S]*?)<\/ol>/)?.[1] ?? '';
  assert.doesNotMatch(header, /gpt-6-luna|\bmax\b|background|chạy nền/);
  assert.doesNotMatch(timeline, /Processing status|Trạng thái xử lý/);
  assert.match(markup, /<details class="agent-flow-details">[\s\S]*gpt-6-luna/);
  assert.match(
    timeline,
    locale === 'vi' ? /Chuẩn bị hướng dẫn phân tích/ : /Preparing analysis guidance/,
  );
  assert.match(markup, locale === 'vi' ? /Chi tiết xử lý/ : /Processing details/);
}

// Recorded work stays accessible without competing with the delivered report.
// Native details starts compact, while a failure is visible outside disclosure.
for (const locale of ['en', 'vi'] as const) {
  const renderFlow = (flow: typeof run) =>
    renderToStaticMarkup(
      <I18nProvider initialLocale={locale} persist={false}>
        <AgentFlow run={flow} />
      </I18nProvider>,
    );
  const completedMarkup = renderFlow(run);
  const completedDisclosure = completedMarkup.match(/<details class="agent-flow-timeline"([^>]*)>/);
  assert.ok(completedDisclosure, 'Recorded timeline remains available');
  assert.doesNotMatch(completedDisclosure[1], /\bopen\b/, 'Delivered runs start compact');
  assert.doesNotMatch(
    completedMarkup,
    /agent-flow-current|role="alert"|class="[^"]*\bspin\b[^"]*"/,
  );
  assert.match(completedMarkup, /data-completed-calls="2" data-total-calls="2"/);
  assert.match(
    completedMarkup,
    locale === 'vi' ? /2\/2 lượt công cụ hoàn tất/ : /2 of 2 tool calls completed/,
  );
  assert.match(completedMarkup, /<ol class="agent-flow-steps" aria-live="off">/);

  const workingFlow = projectAgentFlow([
    event('status', { state: 'running' }),
    event('reasoning', { item_id: 'first-summary', text: 'Reading scope first.' }),
    event('tool', { tool_id: 'scope', name: 'dashboard_context', status: 'completed' }),
    event('tool_arguments', {
      call_id: 'query-call',
      phase: 'done',
      arguments: '{"rationale":"Compare the selected months."}',
    }),
    event('tool', {
      tool_id: 'query',
      call_id: 'query-call',
      name: 'analysis_query',
      status: 'running',
    }),
  ]).get('run-1')!;
  const workingMarkup = renderFlow(workingFlow);
  const currentPanel = workingMarkup.match(
    /<div class="agent-flow-current">([\s\S]*?)<\/div>/,
  )?.[1];
  assert.ok(currentPanel, 'Live work remains visible while the full timeline is collapsed');
  assert.match(currentPanel, /Compare the selected months\./);
  assert.match(workingMarkup, /data-completed-calls="1" data-total-calls="2"/);
  assert.equal(
    (workingMarkup.match(/class="[^"]*\bspin\b[^"]*"/g) || []).length,
    1,
    'Only the current run indicator animates, not every recorded unfinished item',
  );
  assert.match(workingMarkup, locale === 'vi' ? /Đọc phạm vi Dashboard/ : /Read Dashboard scope/);
  assert.doesNotMatch(workingMarkup, /[\u{1F300}-\u{1FAFF}]|\b(?:25|50|75|100)%/u);
  assert.doesNotMatch(
    workingMarkup.match(/<details class="agent-flow-timeline"([^>]*)>/)?.[1] || '',
    /\bopen\b/,
    'Live runs reveal their latest recorded work without opening verbose history',
  );

  const summaryFlow = projectAgentFlow([
    event('status', { state: 'running' }),
    event('reasoning', {
      item_id: 'optional-public-summary',
      text: '**Recommending checks for classification**\n\nI should recommend checks. I need to cite evidence [value:1071].',
    }),
  ]).get('run-1')!;
  const summaryMarkup = renderFlow(summaryFlow);
  const summaryPrimary = summaryMarkup.match(
    /<div class="agent-flow-current">([\s\S]*?)<\/div>/,
  )?.[1];
  assert.ok(summaryPrimary);
  assert.doesNotMatch(
    summaryPrimary,
    /Recommending checks for classification|I should recommend checks|I need to cite evidence|\[value:/,
    'Model-oriented public summaries never become the primary progress message',
  );
  assert.match(summaryPrimary, locale === 'vi' ? /Suy luận/ : /Thinking/);
  const summaryTimeline = summaryMarkup.match(/<ol[^>]*>([\s\S]*?)<\/ol>/)?.[1] || '';
  assert.doesNotMatch(summaryTimeline, /Recommending checks for classification/);
  assert.doesNotMatch(
    summaryTimeline,
    /I should recommend checks\. I need to cite evidence \[value:1071\]\./,
  );
  assert.match(
    summaryTimeline,
    locale === 'vi' ? /Đang xem xét yêu cầu và bằng chứng/ : /Reviewing the request and evidence/,
  );
  const retainedSummary = summaryFlow.steps.find((step) => step.kind === 'thinking');
  assert.ok(retainedSummary?.kind === 'thinking');
  assert.match(retainedSummary.parts[0].raw, /I should recommend checks/);

  const reviewerInstruction =
    'Phân tích chỉ có số liệu tổng hợp; hãy giữ giới hạn suy luận và sửa claim [value:1071].';
  const reviewFlow = projectAgentFlow([
    event('status', { state: 'running' }),
    event('qa', { status: 'retrying', verified: false, issues: [reviewerInstruction] }),
  ]).get('run-1')!;
  const reviewMarkup = renderFlow(reviewFlow);
  const reviewPrimary = reviewMarkup.match(
    /<div class="agent-flow-current">([\s\S]*?)<\/div>/,
  )?.[1];
  assert.ok(reviewPrimary);
  assert.doesNotMatch(reviewPrimary, /Phân tích chỉ có số liệu tổng hợp|hãy giữ|\[value:/);
  assert.match(
    reviewPrimary,
    locale === 'vi'
      ? /Đang rà soát lại câu trả lời trước khi gửi\./
      : /Reviewing the answer again before delivery\./,
  );
  const reviewTimeline = reviewMarkup.match(/<ol[^>]*>([\s\S]*?)<\/ol>/)?.[1] || '';
  assert.ok(reviewTimeline.includes(reviewerInstruction));
  const ambiguousReview = renderFlow(
    projectAgentFlow([
      event('qa', { status: 'retrying', verified: false, issues: ['verified'] }),
    ]).get('run-1')!,
  );
  const ambiguousPrimary = ambiguousReview.match(
    /<div class="agent-flow-current">([\s\S]*?)<\/div>/,
  )?.[1];
  assert.doesNotMatch(
    ambiguousPrimary || '',
    /Numbers verified against source|Đã đối chiếu khớp số liệu nguồn/,
    'Reviewer text alone cannot claim that verification succeeded',
  );

  for (const internalNote of [
    event('memory', { action: 'memory_recall', key: 'private/context-snapshot.json' }),
    event('artifact', { path: 'private/receipt-output.json' }),
  ]) {
    const internalMarkup = renderFlow(projectAgentFlow([internalNote]).get('run-1')!);
    const internalPrimary = internalMarkup.match(
      /<div class="agent-flow-current">([\s\S]*?)<\/div>/,
    )?.[1];
    assert.ok(internalPrimary);
    assert.doesNotMatch(internalPrimary, /private\//);
    assert.match(internalMarkup, /private\//, 'Recorded paths remain in the optional timeline');
  }

  const failedFlow = projectAgentFlow([
    event('tool', {
      name: 'analysis_query',
      tool_id: 'failed-query',
      status: 'failed',
      result: { ok: false, error: 'Selected source could not be read.' },
    }),
    event('error', { code: 'source_unavailable', message: 'Selected source could not be read.' }),
    event('status', { state: 'failed' }),
  ]).get('run-1')!;
  const failedMarkup = renderFlow(failedFlow);
  assert.match(failedMarkup, /<details class="agent-flow-timeline" open="">/);
  assert.match(failedMarkup, /data-completed-calls="0" data-total-calls="1"/);
  const failureNotice = failedMarkup.match(
    /<div class="agent-flow-alert">([\s\S]*?)<\/div><\/div>/,
  )?.[1];
  assert.match(failureNotice || '', /Selected source could not be read\./);
  assert.ok(
    failedMarkup.indexOf('agent-flow-alert') < failedMarkup.indexOf('agent-flow-timeline'),
    'Failure remains visible even when a person closes the timeline',
  );
  assert.doesNotMatch(failedMarkup, /class="[^"]*\bspin\b[^"]*"|role="alert"/);

  const recoveredMarkup = renderFlow({ ...failedFlow, state: 'completed' });
  assert.match(recoveredMarkup, /agent-flow-alert[\s\S]*Selected source could not be read\./);
  assert.doesNotMatch(
    recoveredMarkup.match(/<details class="agent-flow-timeline"([^>]*)>/)?.[1] || '',
    /\bopen\b/,
  );
}

// Each run follows its own recorded language, across alternating questions in
// the same conversation and independently of the page's language selector.
const bilingualHistory = [
  event('message', { role: 'user', text: 'Compare the defect trends.', language: 'en' }, null),
  event('run_started', { language: 'en' }, 'bilingual-en'),
  event('tool', { name: 'analysis_query', tool_id: 'en-query', status: 'running' }, 'bilingual-en'),
  event('message', { role: 'user', text: 'Đối chiếu chi phí sửa chữa.', language: 'vi' }, null),
  event('run_started', { language: 'vi' }, 'bilingual-vi'),
  event('tool', { name: 'analysis_query', tool_id: 'vi-query', status: 'running' }, 'bilingual-vi'),
  event('status', { state: 'completed', language: 'vi' }, 'bilingual-en'),
  event('message', { role: 'user', text: 'Now check data provenance.', language: 'en' }, null),
  event('run_started', { language: 'en' }, 'bilingual-next-en'),
  event(
    'tool',
    { name: 'analysis_provenance', tool_id: 'provenance', status: 'running' },
    'bilingual-next-en',
  ),
];
const bilingualCopy = structuredClone(bilingualHistory);
const bilingualRuns = projectAgentFlow(bilingualHistory);
assert.deepEqual(bilingualHistory, bilingualCopy, 'Language projection preserves event history');
assert.deepEqual(
  [...bilingualRuns.values()].map((item) => [item.runId, item.language, item.prompt]),
  [
    ['bilingual-en', 'en', 'Compare the defect trends.'],
    ['bilingual-vi', 'vi', 'Đối chiếu chi phí sửa chữa.'],
    ['bilingual-next-en', 'en', 'Now check data provenance.'],
  ],
  'A later question or inconsistent late event cannot change an earlier run language',
);
for (const pageLocale of ['en', 'vi'] as const) {
  const markup = renderToStaticMarkup(
    <I18nProvider initialLocale={pageLocale} persist={false}>
      {[...bilingualRuns.values()].map((item) => (
        <AgentFlow key={item.runId} run={item} />
      ))}
    </I18nProvider>,
  );
  const sections = [...markup.matchAll(/<section\b[\s\S]*?<\/section>/g)].map((match) => match[0]);
  assert.equal(sections.length, 3);
  assert.match(sections[0], /lang="en"[\s\S]*Query the quality ledger/);
  assert.doesNotMatch(sections[0], /Luồng xử lý|Truy vấn sổ cái chất lượng/);
  assert.match(sections[1], /lang="vi"[\s\S]*Luồng xử lý[\s\S]*Truy vấn sổ cái chất lượng/);
  assert.doesNotMatch(sections[1], /Agent flow|Query the quality ledger/);
  assert.match(sections[2], /lang="en"[\s\S]*Check data provenance/);
}
// Source-backed presentation tools follow the recorded run language in both
// the current work panel and processing history, independent of page locale.
const presentationToolLabels = [
  ['report_table', 'Source table', 'Bảng dữ liệu nguồn'],
  ['report_chart', 'Create chart', 'Tạo biểu đồ'],
] as const;
for (const [toolName, englishLabel, vietnameseLabel] of presentationToolLabels) {
  for (const runLanguage of ['en', 'vi'] as const) {
    const toolHistory = [
      event('run_started', { language: runLanguage }, 'presentation-tool'),
      event(
        'tool',
        { name: toolName, tool_id: 'presentation-tool-call', status: 'running' },
        'presentation-tool',
      ),
    ];
    const original = JSON.stringify(toolHistory);
    const toolRun = projectAgentFlow(toolHistory).get('presentation-tool')!;
    const label = runLanguage === 'vi' ? vietnameseLabel : englishLabel;
    const otherLabel = runLanguage === 'vi' ? englishLabel : vietnameseLabel;
    for (const pageLocale of ['en', 'vi'] as const) {
      for (const state of ['running', 'completed'] as const) {
        const markup = renderToStaticMarkup(
          <I18nProvider initialLocale={pageLocale} persist={false}>
            <AgentFlow run={{ ...toolRun, state }} />
          </I18nProvider>,
        );
        assert.ok(markup.includes(`<span class="flow-friendly-name">${label}</span>`));
        assert.ok(!markup.includes(otherLabel));
        assert.doesNotMatch(markup, /report_table|report_chart/);
        if (state === 'running') {
          const current = markup.match(/<div class="agent-flow-current">([\s\S]*?)<\/div>/)?.[1];
          assert.ok(current?.includes(label));
        }
      }
    }
    assert.equal(JSON.stringify(toolHistory), original, 'Tool labels never rewrite stored events');
  }
}

const legacyLanguageRun = projectAgentFlow([
  event('run_started', { language: 'unsupported' }),
  event('tool', { name: 'analysis_query', tool_id: 'legacy-language', status: 'running' }),
]).get('run-1')!;
assert.equal(legacyLanguageRun.language, undefined, 'Unknown language values stay unknown');
const hydratedLanguageMarkup = renderToStaticMarkup(
  <I18nProvider initialLocale="en" persist={false}>
    <AgentFlow run={legacyLanguageRun} language="vi" />
  </I18nProvider>,
);
assert.match(hydratedLanguageMarkup, /lang="vi"[\s\S]*Truy vấn sổ cái chất lượng/);

// A localized task status is a full display snapshot, not a translated provider
// summary. Raw public text stays available in the projection and source events.
const rawPublicSummary = '**Reading scope**\n\nI will inspect Line-3.';
const taskStatusText = 'Đang xem xét yêu cầu và bằng chứng để chuẩn bị câu trả lời.';
const displayHistory = [
  event('run_started', { language: 'vi' }),
  event('reasoning', {
    item_id: 'localized-summary',
    summary_index: 0,
    text: '**Reading scope**\n\n',
    display_text: taskStatusText,
    display_mode: 'snapshot',
    display_source: 'task_status',
  }),
  event('reasoning', {
    item_id: 'localized-summary',
    summary_index: 0,
    text: 'I will inspect Line-3.',
    display_text: taskStatusText,
    display_mode: 'snapshot',
    display_source: 'task_status',
  }),
  event('reasoning_part', {
    item_id: 'localized-summary',
    summary_index: 0,
    phase: 'done',
    text: rawPublicSummary,
  }),
];
const displayHistoryCopy = structuredClone(displayHistory);
const displayRun = projectAgentFlow(displayHistory).get('run-1')!;
const displayBlock = displayRun.steps.find((step) => step.kind === 'thinking');
assert.ok(displayBlock?.kind === 'thinking');
assert.equal(displayBlock.parts.length, 1);
assert.equal(displayBlock.parts[0].raw, rawPublicSummary);
assert.equal(displayBlock.parts[0].body, taskStatusText);
assert.equal(displayBlock.parts[0].displaySource, 'task_status');
assert.deepEqual(displayHistory, displayHistoryCopy);
const displayMarkup = renderToStaticMarkup(
  <I18nProvider initialLocale="en" persist={false}>
    <AgentFlow run={displayRun} />
  </I18nProvider>,
);
assert.match(displayMarkup, /lang="vi"/);
assert.match(displayMarkup, /Trạng thái tác vụ/);
assert.doesNotMatch(displayMarkup, /Reading scope|I will inspect Line-3/);
const displayTimeline = displayMarkup.match(/<ol[^>]*>([\s\S]*?)<\/ol>/)?.[1] || '';
assert.equal(
  displayTimeline.split(taskStatusText).length - 1,
  1,
  'Snapshots do not repeat fallback text',
);
assert.match(
  displayMarkup.match(/<div class="agent-flow-current">([\s\S]*?)<\/div>/)?.[1] || '',
  /Đang xem xét yêu cầu và bằng chứng/,
  'The recorded task status provides a useful primary update',
);

const nativeDisplayRun = projectAgentFlow([
  event('run_started', { language: 'vi' }),
  event('reasoning_part', {
    item_id: 'native-display',
    summary_index: 0,
    phase: 'added',
    display_text: taskStatusText,
    display_mode: 'snapshot',
    display_source: 'task_status',
  }),
  event('reasoning', {
    item_id: 'native-display',
    summary_index: 0,
    text: '**Đọc phạm vi**\n\nĐối chiếu dữ liệu đã chọn.',
    display_text: '**Đọc phạm vi**\n\nĐối chiếu dữ liệu đã chọn.',
    display_mode: 'snapshot',
    display_source: 'provider_summary',
    final: true,
  }),
  event('output_item', { item_id: 'native-display', item_type: 'reasoning', phase: 'done' }),
]).get('run-1')!;
const nativeDisplay = nativeDisplayRun.steps.find((step) => step.kind === 'thinking');
assert.ok(nativeDisplay?.kind === 'thinking');
assert.equal(nativeDisplay.parts[0].title, 'Đọc phạm vi');
assert.equal(nativeDisplay.parts[0].displaySource, 'provider_summary');
const nativeDisplayMarkup = renderToStaticMarkup(
  <I18nProvider initialLocale="en" persist={false}>
    <AgentFlow run={nativeDisplayRun} />
  </I18nProvider>,
);
assert.match(nativeDisplayMarkup, /1 phần tóm tắt/);
assert.doesNotMatch(nativeDisplayMarkup, /Trạng thái tác vụ|Đang xem xét yêu cầu/);

const laterPublicPartRun = projectAgentFlow([
  ...displayHistory,
  event('reasoning', {
    item_id: 'localized-summary',
    summary_index: 1,
    text: 'Đang đối chiếu phạm vi đã chọn.',
    display_text: 'Đang đối chiếu phạm vi đã chọn.',
    display_mode: 'snapshot',
    display_source: 'provider_summary',
  }),
]).get('run-1')!;
const laterPublicPartMarkup = renderToStaticMarkup(
  <I18nProvider initialLocale="en" persist={false}>
    <AgentFlow run={laterPublicPartRun} />
  </I18nProvider>,
);
assert.doesNotMatch(
  laterPublicPartMarkup.match(/<div class="agent-flow-current">([\s\S]*?)<\/div>/)?.[1] || '',
  /Đang xem xét yêu cầu/,
  'An earlier fallback does not replace a newer recorded update',
);

// An unverified answer draft is not promoted as a public progress message.
const unverifiedDraftRun = projectAgentFlow([
  event('run_started', { language: 'en' }),
  event('text_delta', { item_id: 'draft-answer', delta: 'An unverified conclusion.' }),
]).get('run-1')!;
const unverifiedDraftMarkup = renderToStaticMarkup(
  <I18nProvider initialLocale="vi" persist={false}>
    <AgentFlow run={unverifiedDraftRun} />
  </I18nProvider>,
);
assert.doesNotMatch(
  unverifiedDraftMarkup.match(/<div class="agent-flow-current">([\s\S]*?)<\/div>/)?.[1] || '',
  /An unverified conclusion/,
);
assert.match(unverifiedDraftMarkup, /An unverified conclusion/);

// Partial language detection can temporarily accept an English sentence that
// quotes a Vietnamese label. The view waits for a finalized, compatible snapshot.
const uncertainSummaryText =
  '**Assessing the request**\n\nNeed to review "dữ liệu" before continuing.';
const uncertainSummaryPart: FlowPart = {
  index: 0,
  raw: uncertainSummaryText,
  title: 'Assessing the request',
  body: 'Need to review "dữ liệu" before continuing.',
  done: false,
  displayText: uncertainSummaryText,
  displaySource: 'provider_summary',
};
const uncertainCopy = structuredClone(uncertainSummaryPart);
for (const done of [false, true]) {
  const projected = flowSummaryDisplay({ ...uncertainSummaryPart, done }, 'vi');
  assert.equal(projected.displaySource, 'task_status');
  assert.equal(projected.body, taskStatusText);
  assert.equal(projected.title, '');
  assert.equal(projected.raw, uncertainSummaryText);
}
assert.deepEqual(uncertainSummaryPart, uncertainCopy);
const provenVietnamesePart: FlowPart = {
  ...uncertainSummaryPart,
  raw: '**Đọc phạm vi**\n\nĐối chiếu dữ liệu đã chọn.',
  title: 'Đọc phạm vi',
  body: 'Đối chiếu dữ liệu đã chọn.',
  displayText: '**Đọc phạm vi**\n\nĐối chiếu dữ liệu đã chọn.',
  done: true,
};
assert.equal(flowSummaryDisplay(provenVietnamesePart, 'vi'), provenVietnamesePart);
assert.equal(
  flowSummaryDisplay({ ...provenVietnamesePart, done: false }, 'vi').displaySource,
  'task_status',
);
const uncertainTitle = flowSummaryDisplay(
  { ...provenVietnamesePart, title: 'Checking results' },
  'vi',
);
assert.equal(uncertainTitle.title, '');
assert.equal(uncertainTitle.body, provenVietnamesePart.body);
assert.equal(uncertainTitle.displaySource, 'provider_summary');
assert.equal(
  flowSummaryDisplay({ ...provenVietnamesePart, displaySource: undefined }, 'vi').displaySource,
  'task_status',
);
const provenEnglishPart: FlowPart = {
  ...provenVietnamesePart,
  raw: '**Checking evidence**\n\nReview the recorded results.',
  title: 'Checking evidence',
  body: 'Review the recorded results.',
  displayText: '**Checking evidence**\n\nReview the recorded results.',
};
assert.equal(flowSummaryDisplay(provenEnglishPart, 'en'), provenEnglishPart);
assert.equal(flowSummaryDisplay(provenEnglishPart, 'vi').displaySource, 'task_status');
assert.equal(flowSummaryDisplay(provenVietnamesePart, 'en').displaySource, 'task_status');
const recordedTaskStatus = {
  ...uncertainSummaryPart,
  body: taskStatusText,
  displaySource: 'task_status' as const,
};
assert.equal(flowSummaryDisplay(recordedTaskStatus, 'vi'), recordedTaskStatus);
for (const final of [false, true]) {
  const mixedHistory = [
    event('run_started', { language: 'vi' }),
    event('reasoning', {
      item_id: 'mixed-language-public-part',
      text: uncertainSummaryText,
      display_text: uncertainSummaryText,
      display_mode: 'snapshot',
      display_source: 'provider_summary',
      final,
    }),
  ];
  const before = structuredClone(mixedHistory);
  const mixedRun = projectAgentFlow(mixedHistory).get('run-1')!;
  const markup = renderToStaticMarkup(
    <I18nProvider initialLocale="en" persist={false}>
      <AgentFlow run={mixedRun} />
    </I18nProvider>,
  );
  const historyMarkup = markup.match(/<ol[^>]*>([\s\S]*?)<\/ol>/)?.[1] || '';
  assert.match(markup, /lang="vi"/);
  assert.match(historyMarkup, /Trạng thái tác vụ/);
  assert.ok(historyMarkup.includes(taskStatusText));
  assert.doesNotMatch(historyMarkup, /Assessing the request|Need to review/);
  const retained = mixedRun.steps.find((step) => step.kind === 'thinking');
  assert.ok(retained?.kind === 'thinking');
  assert.equal(retained.parts[0].raw, uncertainSummaryText);
  assert.deepEqual(mixedHistory, before);
}

console.log(
  'Verified provider-ordered agent flow: summary-part grouping, streamed call arguments joined to local receipts, run isolation, per-run EN/VI rendering, public display snapshots, metadata-only usage handling and inert trace content.',
);
