import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import AgentFlow from '../src/AgentFlow';
import { callOutcome, projectAgentFlow, splitSummaryPart } from '../src/agent-flow';
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
assert.match(injectedHtml, /&lt;script&gt;/);

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
  assert.match(markup, /Reading the selected data/);
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
  assert.match(condensedMarkup, /First public summary\.|Second public summary\./);
  assert.ok(
    condensedMarkup.indexOf('Reading real data') < condensedMarkup.indexOf('ordered_analysis'),
  );
  assert.ok(
    condensedMarkup.indexOf('ordered_analysis') < condensedMarkup.indexOf('Checking real results'),
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
    assert.ok(appliedMarkup.indexOf(receivedLabel) < appliedMarkup.indexOf(submittedLabel));
    assert.ok(appliedMarkup.indexOf(submittedLabel) < appliedMarkup.indexOf(appliedLabel));
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
    assert.match(markup, /Checking the selected data/);
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

console.log(
  'Verified provider-ordered agent flow: summary-part grouping, streamed call arguments joined to local receipts, run isolation, metadata-only usage handling, inert trace content and localized EN/VI rendering.',
);
