import assert from 'node:assert/strict';
import { projectAgentFlow } from '../src/agent-flow';
import { eligibleAnswerFollowup } from '../src/answer-followups';
import { questionForAnswer } from '../src/answer-reply';
import { conversationRunLanguages } from '../src/conversation-language';
import { projectConversation } from '../src/conversation-projection';
import { appendConversationState, createConversationState } from '../src/conversation-state';
import type { ConversationState } from '../src/conversation-state';
import type { Session, WorkspaceEvent } from '../src/types';

if (!('window' in globalThis))
  Object.defineProperty(globalThis, 'window', {
    value: { location: { origin: 'http://localhost:8001', protocol: 'http:' } },
    configurable: true,
  });
let sequence = 0;
const runA = 'a'.repeat(32);
const runB = 'b'.repeat(32);
const runC = 'c'.repeat(32);
const event = (type: string, data: Record<string, unknown>, runId?: string): WorkspaceEvent => ({
  id: ++sequence,
  session_id: 'projection-fixture',
  run_id: runId,
  type,
  data,
  time: '2026-10-18T00:00:00Z',
});
const chart = (runId: string, hash: string) =>
  event(
    'artifact',
    {
      path: 'trend.png',
      name: 'trend.png',
      mime: 'image/png',
      mime_type: 'image/png',
      sha256: hash.repeat(64),
      size_bytes: 12,
      delivery_candidate: true,
      url: `http://localhost:8001/api/sessions/projection-fixture/runs/${runId}/artifacts/trend.png`,
    },
    runId,
  );
const delivery = (artifact: WorkspaceEvent) => ({
  version: 1,
  scope: null,
  source_execution_ids: ['execution-1'],
  artifacts: [{ ...artifact.data }],
});
const snapshot = (state: ConversationState) =>
  JSON.stringify({
    events: state.events,
    entries: state.entries,
    flows: [...state.flowRuns],
    contexts: [...state.runContexts],
    languages: [...state.runLanguages],
    groups: [...state.groups],
    positions: [...state.positions],
    providers: [...state.providerIds],
  });
let checkedPrefixes = 0;
function equivalent(
  state: ConversationState,
  history: WorkspaceEvent[],
  savedRuns: Session['runs'],
) {
  assert.deepEqual(state.events, history, 'Every raw event survives in its admitted order.');
  assert.deepEqual(
    state.visibleEvents,
    projectConversation(history),
    'Display matches the full projector.',
  );
  assert.deepEqual(state.flowRuns, projectAgentFlow(history), 'Flows match the full projector.');
  assert.deepEqual(state.runLanguages, conversationRunLanguages(history, savedRuns));
  const contexts = new Map<string, WorkspaceEvent>();
  for (const item of history)
    if (item.type === 'analysis_context' && item.run_id) contexts.set(item.run_id, item);
  assert.deepEqual(state.runContexts, contexts);
  assert.equal(
    state.lastStatus,
    [...history].reverse().find((item) => item.type === 'status'),
  );
  assert.equal(
    state.lastRunStatus,
    [...history].reverse().find((item) => item.type === 'status' && item.run_id),
  );
  assert.equal(
    state.lastQuestion,
    [...history].reverse().find((item) => item.type === 'message' && item.data.role === 'user'),
  );
  assert.equal(
    state.hasConversation,
    history.some((item) => item.type === 'message'),
  );
  assert.equal(
    state.providerStopped,
    history.some((item) => item.type === 'status' && item.data.state === 'provider_stopped'),
  );
  for (const entry of state.entries)
    if (entry.type === 'message' && entry.role === 'assistant') {
      assert.deepEqual(entry.question, questionForAnswer(entry.event, history));
      assert.equal(
        entry.eligible,
        Boolean(entry.context) && eligibleAnswerFollowup(entry.event, history, savedRuns),
      );
    }
  checkedPrefixes += 1;
}
function verifyPrefixes(history: WorkspaceEvent[], savedRuns: Session['runs'] = []) {
  const rawBefore = JSON.stringify(history);
  let state = createConversationState([], savedRuns);
  for (let index = 0; index < history.length; index++) {
    const prior = snapshot(state);
    const next = appendConversationState(state, [history[index]]);
    assert.equal(snapshot(state), prior, 'Appending must not mutate a published snapshot.');
    equivalent(next, history.slice(0, index + 1), savedRuns);
    assert.deepEqual(
      appendConversationState(state, [history[index]]),
      next,
      'Repeated updater is safe.',
    );
    state = next;
  }
  equivalent(createConversationState(history, savedRuns), history, savedRuns);
  assert.equal(
    JSON.stringify(history),
    rawBefore,
    'Projection cannot annotate canonical evidence.',
  );
  return state;
}
const firstChart = chart(runA, '1');
const secondChart = chart(runA, '2');
const history = [
  event('message', { role: 'user', text: 'Original question A', language: 'en' }),
  event('status', { state: 'queued' }),
  event(
    'run_started',
    { question: 'Original question A', language: 'en', artifact_delivery_version: 1 },
    runA,
  ),
  event(
    'analysis_context',
    { context_id: 'context-a', language: 'en', filters: { production_line: 'Line-1' } },
    runA,
  ),
  event('status', { state: 'running', language: 'en', action_intent: 'draft_create' }, runA),
  event(
    'reasoning_summary',
    {
      provider_api: 'agents',
      session_id: 'provider-a',
      turn_id: 'turn-a',
      item_id: 'old-item',
      text: 'Checking. ',
    },
    runA,
  ),
  event('tool', { tool_id: 'tool-1', name: 'analysis_query', status: 'running' }, runA),
  event('message', { role: 'user', text: 'Question B', language: 'vi' }, runB),
  event('status', { state: 'queued' }),
  event('run_started', { question: 'Question B', language: 'vi' }, runB),
  event(
    'analysis_context',
    { context_id: 'context-b', language: 'vi', filters: { production_line: 'Line-2' } },
    runB,
  ),
  event('status', { state: 'running', language: 'vi' }, runB),
  event('message', { role: 'user', text: 'Steer the existing work', language: 'vi' }, runA),
  event(
    'tool',
    { tool_id: 'tool-1', name: 'analysis_query', status: 'completed', result: { rows: 12 } },
    runA,
  ),
  firstChart,
  event(
    'message',
    { role: 'assistant', text: 'Answer A', qa: { verified: true }, delivery: delivery(firstChart) },
    runA,
  ),
  event('status', { state: 'completed' }, runA),
  event('message', { role: 'assistant', text: 'Answer B', qa: { verified: true } }, runB),
  event('status', { state: 'completed' }, runB),
  event(
    'reasoning_alias',
    {
      provider_api: 'agents',
      session_id: 'provider-a',
      turn_id: 'turn-a',
      item_id: 'old-item',
      canonical_item_id: 'canonical-item',
      source: 'session_snapshot',
      reconciled: true,
    },
    runA,
  ),
  event(
    'reasoning_summary',
    {
      provider_api: 'agents',
      session_id: 'provider-a',
      turn_id: 'turn-a',
      item_id: 'canonical-item',
      text: 'Verified summary.',
      final: true,
    },
    runA,
  ),
  secondChart,
  event(
    'message',
    {
      role: 'assistant',
      text: 'Revised answer A',
      qa: { verified: false },
      delivery: delivery(secondChart),
    },
    runA,
  ),
  event('status', { state: 'unverified' }, runA),
  event(
    'message',
    {
      role: 'assistant',
      text: 'Verified answer A',
      qa: { verified: true },
      delivery: delivery(secondChart),
    },
    runA,
  ),
  event('status', { state: 'completed' }, runA),
  event('message', { role: 'user', text: 'Question C' }),
  event('status', { state: 'running' }, runC),
  event('error', { message: 'Synthetic cancellation' }, runC),
  event('status', { state: 'cancelled' }, runC),
  event('status', { state: 'provider_stopped' }, runC),
  event('status', { state: 'queued' }),
  event('status', { state: 'running' }, runB),
  event('analysis_context', { context_id: 'context-b', language: 'en' }, runB),
];
const savedRuns: Session['runs'] = [
  { id: runA, state: 'running', language: 'en' },
  { id: runB, state: 'completed', language: 'vi' },
];
const state = verifyPrefixes(history, savedRuns);
assert.equal(
  state.fullProjection,
  false,
  'Interleaving, late charts and aliases stay incremental.',
);
const historyBefore = snapshot(state);
const aEntries = state.entries.filter((entry) => entry.event.run_id === runA);
const next = appendConversationState(state, [
  event('reasoning_summary', { item_id: 'b-reasoning', text: 'More B.' }, runB),
]);
assert.equal(next.flowRuns.get(runA), state.flowRuns.get(runA));
for (const entry of aEntries)
  assert.ok(next.entries.includes(entry), 'Untouched entries retain identity.');
assert.equal(snapshot(state), historyBefore);
assert.equal(appendConversationState(state, []), state);

// A growing run keeps completed receipts and unchanged summary parts stable,
// while amendments and late identity reconciliation invalidate the right nodes.
const stableHistory = [
  event('run_started', { question: 'Keep every public part.', language: 'vi' }, runC),
  event('status', { state: 'running' }, runC),
  event(
    'tool',
    {
      tool_id: 'stable-call',
      name: 'analysis_query',
      status: 'completed',
      result: { ok: true, event_count: 12 },
    },
    runC,
  ),
  event(
    'reasoning_summary',
    { item_id: 'stable-summary', summary_index: 0, text: 'First complete part.', final: true },
    runC,
  ),
  event(
    'reasoning_summary',
    { item_id: 'stable-summary', summary_index: 1, text: 'Second ' },
    runC,
  ),
];
const stableState = createConversationState(stableHistory);
const stableSnapshot = snapshot(stableState);
const oldSteps = stableState.flowRuns.get(runC)!.steps;
const oldThought = oldSteps.find((step) => step.kind === 'thinking')!;
assert.ok(oldThought.kind === 'thinking');
const summaryDelta = event(
  'reasoning_summary',
  { item_id: 'stable-summary', summary_index: 1, text: 'streaming part.' },
  runC,
);
const updatedState = appendConversationState(stableState, [summaryDelta]);
const updatedSteps = updatedState.flowRuns.get(runC)!.steps;
const updatedThought = updatedSteps.find((step) => step.kind === 'thinking')!;
assert.ok(updatedThought.kind === 'thinking');
assert.equal(updatedSteps[0], oldSteps[0], 'An unchanged receipt does not rerender for a delta.');
assert.notEqual(updatedThought, oldThought);
assert.equal(updatedThought.parts[0], oldThought.parts[0]);
assert.notEqual(updatedThought.parts[1], oldThought.parts[1]);
assert.equal(updatedThought.parts[1].body, 'Second streaming part.');
assert.equal(snapshot(stableState), stableSnapshot);
equivalent(updatedState, [...stableHistory, summaryDelta], []);
const amendedReceipt = event(
  'tool',
  {
    tool_id: 'stable-call',
    name: 'analysis_query',
    status: 'completed',
    result: { ok: true, event_count: 13 },
  },
  runC,
);
const amendedState = appendConversationState(updatedState, [amendedReceipt]);
assert.notEqual(amendedState.flowRuns.get(runC)!.steps[0], updatedSteps[0]);
assert.equal(amendedState.flowRuns.get(runC)!.steps[1], updatedThought);
equivalent(amendedState, [...stableHistory, summaryDelta, amendedReceipt], []);
const lateAliases = [
  ...['First aliased part.', 'Second aliased part.'].map((text, index) =>
    event(
      'reasoning_summary',
      {
        item_id: index === 0 ? 'transient-a' : 'transient-b',
        text,
        provider_api: 'agents',
        session_id: 'projection-provider',
        turn_id: 'projection-turn',
      },
      runA,
    ),
  ),
  ...['transient-a', 'transient-b'].map((item_id, index) =>
    event(
      'reasoning_alias',
      {
        item_id,
        canonical_item_id: 'canonical-parts',
        canonical_summary_indices: [index],
        source: 'session_snapshot',
        reconciled: true,
        provider_api: 'agents',
        session_id: 'projection-provider',
        turn_id: 'projection-turn',
      },
      runA,
    ),
  ),
];
const aliasState = verifyPrefixes(lateAliases);
const aliasSteps = aliasState.flowRuns.get(runA)!.steps;
assert.equal(aliasSteps.length, 1);
assert.ok(aliasSteps[0].kind === 'thinking');
assert.deepEqual(
  aliasSteps[0].parts.map((part) => part.raw),
  ['First aliased part.', 'Second aliased part.'],
);

// The conversation-only projection never opens reasoning payloads. The full
// flow still consumes every original source event and reconciles late aliases.
let reasoningDataReads = 0;
const guardedReasoning = lateAliases.map((source) => ({
  ...source,
  get data() {
    reasoningDataReads += 1;
    return source.data;
  },
}));
guardedReasoning.splice(
  2,
  0,
  ...[
    event(
      'reasoning',
      {
        item_id: 'third-native',
        text: 'Third source part.',
        provider_api: 'agents',
        session_id: 'projection-provider',
        turn_id: 'projection-turn',
      },
      runA,
    ),
    event(
      'reasoning_part',
      {
        item_id: 'third-native',
        phase: 'done',
        text: 'Third source part.',
        provider_api: 'agents',
        session_id: 'projection-provider',
        turn_id: 'projection-turn',
      },
      runA,
    ),
  ].map((source) => ({
    ...source,
    get data() {
      reasoningDataReads += 1;
      return source.data;
    },
  })),
);
const projectionChart = chart(runA, 'd');
const projectionReport = event(
  'message',
  {
    role: 'assistant',
    text: 'A source-bound answer.',
    qa: { verified: true },
    delivery: delivery(projectionChart),
  },
  runA,
);
const conversationOnly = [
  event('message', { role: 'user', text: 'Inspect this source.' }),
  event('status', { state: 'running' }, runA),
  event(
    'tool',
    {
      tool_id: 'projection-tool',
      name: 'analysis_query',
      status: 'completed',
      result: { ok: true },
    },
    runA,
  ),
  projectionReport,
  projectionChart,
  event('status', { state: 'completed' }, runA),
];
const conversationWithReasoning = [
  ...conversationOnly.slice(0, 2),
  ...guardedReasoning,
  ...conversationOnly.slice(2),
];
assert.deepEqual(
  projectConversation(conversationWithReasoning),
  projectConversation(conversationOnly),
);
assert.equal(reasoningDataReads, 0, 'Conversation/chart projection does not read reasoning data');
const fullSourceFlow = projectAgentFlow(conversationWithReasoning).get(runA)!;
assert.ok(reasoningDataReads > 0, 'Full flow still reads original reasoning source');
assert.deepEqual(
  fullSourceFlow.steps.flatMap((step) =>
    step.kind === 'thinking' ? step.parts.map((part) => part.raw) : [],
  ),
  ['First aliased part.', 'Second aliased part.', 'Third source part.'],
);
const collidingReasoning = { ...guardedReasoning[0], id: projectionReport.id };
const collisionProjection = projectConversation([collidingReasoning, ...conversationOnly]);
assert.ok(
  !collisionProjection.some((item) => item.type === 'message' && item.data.role === 'assistant'),
  'An earlier reasoning ID still shadows a corrupted report with that same ID',
);
assert.ok(
  collisionProjection.some((item) => item.type === 'artifact'),
  'Duplicate report metadata retains the existing chart selection behavior',
);
assert.deepEqual(
  projectConversation([...conversationOnly, collidingReasoning]),
  projectConversation(conversationOnly),
  'A later reasoning duplicate cannot shadow an earlier report',
);
verifyPrefixes([...history, history[3], history[15]], savedRuns);
verifyPrefixes([
  event('message', { role: 'user', text: 'Old pending' }, runA),
  event('message', { role: 'user', text: 'Latest pending' }),
  event('status', { state: 'running' }, runB),
  event('status', { state: 'running' }, runA),
  event('message', { role: 'user', text: 'Later steering' }, runB),
  event('reasoning_summary', { text: 'Keep the first prompt.', item_id: 'rs' }, runA),
]);
verifyPrefixes([
  event(
    'status',
    { state: 'running', event_id: 'same-status', provider_session_id: 'p', provider_turn_id: 't' },
    runA,
  ),
  event(
    'status',
    {
      state: 'completed',
      event_id: 'same-status',
      provider_session_id: 'p',
      provider_turn_id: 't',
    },
    runA,
  ),
  event('status', { state: 'queued' }),
  event(
    'status',
    {
      state: 'cancelled',
      event_id: 'same-status',
      provider_session_id: 'p',
      provider_turn_id: 't',
    },
    runA,
  ),
]);
verifyPrefixes([
  event('message', { role: 'user', text: 'Multiple sessions remain exact.' }),
  { ...event('status', { state: 'running' }, runA), session_id: 'second-session' },
  event('status', { state: 'running' }, 'session'),
]);

// Fixed schedules exercise old-run amendments and batched updates. Arrival order
// is authoritative even when durable IDs do not follow numerical order.
for (let seed = 1; seed <= 12; seed++) {
  let random = seed;
  const choose = (size: number) => {
    random = (random * 1664525 + 1013904223) >>> 0;
    return random % size;
  };
  const ids = [runA, runB, runC];
  const generated: WorkspaceEvent[] = [];
  for (let index = 0; index < 60; index++) {
    const run = ids[choose(ids.length)];
    const kind = choose(9);
    generated.push(
      kind === 0
        ? event(
            'message',
            { role: 'user', text: `Question ${index}`, language: choose(2) ? 'en' : 'vi' },
            choose(2) ? run : undefined,
          )
        : kind === 1
          ? event(
              'run_started',
              { question: `Admitted ${index}`, language: choose(2) ? 'en' : 'vi' },
              run,
            )
          : kind === 2
            ? event(
                'analysis_context',
                { context_id: `context-${run}`, language: choose(2) ? 'en' : 'vi' },
                run,
              )
            : kind === 3
              ? event(
                  'message',
                  {
                    role: 'assistant',
                    text: `Answer ${index}`,
                    qa: { verified: Boolean(choose(2)) },
                  },
                  run,
                )
              : kind === 4
                ? event(
                    'status',
                    { state: ['running', 'completed', 'cancelled', 'unverified'][choose(4)] },
                    run,
                  )
                : kind === 5
                  ? event(
                      'tool',
                      {
                        tool_id: `tool-${choose(3)}`,
                        name: 'analysis_query',
                        status: choose(2) ? 'running' : 'completed',
                      },
                      run,
                    )
                  : kind === 6
                    ? event(
                        'reasoning_summary',
                        {
                          item_id: `item-${choose(2)}`,
                          text: `Part ${index}. `,
                          final: Boolean(choose(3)),
                        },
                        run,
                      )
                    : kind === 7
                      ? event('error', { message: 'Synthetic error.' }, choose(2) ? run : undefined)
                      : event('status', { state: 'queued' }),
    );
  }
  verifyPrefixes(generated, savedRuns);
  let batched = createConversationState([], savedRuns);
  for (let start = 0; start < generated.length; start += 7) {
    batched = appendConversationState(batched, generated.slice(start, start + 7));
    equivalent(batched, generated.slice(0, start + 7), savedRuns);
  }
}
console.log(
  `Verified ${checkedPrefixes} incremental/full projection comparisons, immutable replay, interleaved and late amendments, locale/context identity, queued status, cancellation, and completed entry reuse.`,
);
