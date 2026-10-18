import assert from 'node:assert/strict';
import { initialFilters } from '../src/dashboard-types';
import {
  commitUi,
  objectInput,
  registerWebMcpTools,
  stringInput,
  throwIfAborted,
  waitForUi,
} from '../src/webmcp';
import type { WebMcpTool } from '../src/webmcp';
import { buildWorkspaceTools, validateScopePatch } from '../src/workspace-webmcp';
import type {
  WorkspaceDashboardData,
  WorkspaceSnapshot,
  WorkspaceToolAdapter,
} from '../src/workspace-webmcp';

// Isolated contract fixtures. They do not establish native WebMCP or model execution support.
assert.deepEqual(objectInput({}, []), {});
for (const value of [null, [], new Date(), 'text', 2, true, Object.create({ hidden: true })])
  assert.throws(() => objectInput(value, []), TypeError);
assert.throws(() => objectInput({ extra: 'hidden' }, []), /unknown property/);
assert.throws(() => objectInput({ [Symbol('hidden')]: true }, []), /unknown property/);
assert.throws(() => objectInput({}, ['required'], ['required']), /required property/);
assert.equal(stringInput('vi', 'language', { values: ['en', 'vi'] }), 'vi');
assert.throws(() => stringInput('VI', 'language', { values: ['en', 'vi'] }), TypeError);
assert.throws(() => stringInput('', 'id', { minLength: 1 }), TypeError);
assert.throws(() => stringInput('abc', 'id', { maxLength: 2 }), TypeError);
const aborted = new AbortController();
aborted.abort();
assert.throws(() => throwIfAborted(aborted.signal), { name: 'AbortError' });

const tool: WebMcpTool = {
  name: 'fixture_read',
  title: 'Read fixture',
  description: 'Read isolated fixture state.',
  inputSchema: { type: 'object', properties: {}, additionalProperties: false },
  annotations: { readOnlyHint: true, consequentialHint: false, untrustedContentHint: false },
  execute: () => ({ revision: 1 }),
};
let current: WebMcpTool | undefined = tool;
const registrations: { tool: WebMcpTool; signal?: AbortSignal; optionKeys: string[] }[] = [];
const failures: string[] = [];
const unregister = registerWebMcpTools(
  {
    registerTool(registered, options) {
      registrations.push({
        tool: registered,
        signal: options?.signal,
        optionKeys: Object.keys(options ?? {}),
      });
      return Promise.resolve();
    },
  },
  [tool],
  () => current,
  (name) => failures.push(name),
);
assert.equal(registrations.length, 1);
assert.deepEqual(registrations[0].optionKeys, ['signal'], 'No cross-origin exposure override');
assert.deepEqual(await registrations[0].tool.execute({}, {}), { revision: 1 });
current = { ...tool, execute: () => ({ revision: 2 }) };
assert.deepEqual(await registrations[0].tool.execute({}, {}), { revision: 2 });
assert.equal(registrations.length, 1, 'Committed callback updates do not re-register metadata');
current = undefined;
await assert.rejects(async () => registrations[0].tool.execute({}, {}), /no longer available/);
let calls = 0;
current = {
  ...tool,
  execute: () => {
    calls++;
    return {};
  },
};
await assert.rejects(async () => registrations[0].tool.execute({}, { signal: aborted.signal }), {
  name: 'AbortError',
});
assert.equal(calls, 0);
let finishExecution!: (value: unknown) => void;
let invocationSignal: AbortSignal | undefined;
current = {
  ...tool,
  execute: (_input, options) => {
    invocationSignal = options.signal;
    return new Promise((resolve) => {
      finishExecution = resolve;
    });
  },
};
const invocation = new AbortController();
const running = registrations[0].tool.execute({}, { signal: invocation.signal });
unregister();
assert.equal(registrations[0].signal?.aborted, true);
assert.equal(invocationSignal, invocation.signal);
assert.equal(
  invocation.signal.aborted,
  false,
  'Unregistration does not cancel an active invocation',
);
finishExecution({ status: 'completed' });
assert.deepEqual(await running, { status: 'completed' });
await assert.rejects(async () => registrations[0].tool.execute({}, {}), /no longer available/);
registerWebMcpTools(undefined, [tool], () => tool)();
registerWebMcpTools(
  {
    registerTool() {
      throw new Error('fixture sync failure');
    },
  },
  [tool],
  () => tool,
  (name) => failures.push(name),
)();
const rejectCleanup = registerWebMcpTools(
  {
    registerTool() {
      return Promise.reject(new Error('fixture async failure'));
    },
  },
  [tool],
  () => tool,
  (name) => failures.push(name),
);
await Promise.resolve();
await Promise.resolve();
rejectCleanup();
assert.deepEqual(failures, ['fixture_read', 'fixture_read']);
const cleanupFailures: string[] = [];
const cleanup = registerWebMcpTools(
  {
    registerTool() {
      return Promise.reject(new Error('fixture aborted registration'));
    },
  },
  [tool],
  () => tool,
  (name) => cleanupFailures.push(name),
);
cleanup();
await Promise.resolve();
assert.deepEqual(
  cleanupFailures,
  [],
  'Expected cleanup rejection is handled without a false warning',
);

const frames = new Map<number, FrameRequestCallback>();
let nextFrame = 0;
Object.defineProperty(globalThis, 'requestAnimationFrame', {
  configurable: true,
  value: (callback: FrameRequestCallback) => {
    frames.set(++nextFrame, callback);
    return nextFrame;
  },
});
Object.defineProperty(globalThis, 'cancelAnimationFrame', {
  configurable: true,
  value: (id: number) => frames.delete(id),
});
function paint() {
  const callbacks = [...frames.values()];
  frames.clear();
  callbacks.forEach((callback) => callback(0));
}
let committed = false;
let settled = false;
const committing = commitUi(() => {
  committed = true;
}).then(() => {
  settled = true;
});
assert.equal(committed, true);
assert.equal(settled, false);
paint();
await committing;
assert.equal(settled, true);
await assert.rejects(
  commitUi(() => {
    throw new Error('must not run');
  }, aborted.signal),
  { name: 'AbortError' },
);
const waitAbort = new AbortController();
const waiting = waitForUi(() => undefined, waitAbort.signal);
waitAbort.abort();
await assert.rejects(waiting, { name: 'AbortError' });
assert.equal(frames.size, 0, 'Abort releases its pending UI frame');
const pendingState: { value: string | undefined } = { value: undefined };
const stateWait = waitForUi(() => pendingState.value);
pendingState.value = 'visible';
paint();
assert.equal(await stateWait, 'visible');
await assert.rejects(
  waitForUi(() => {
    throw new Error('stale context');
  }),
  /stale context/,
);

const data: WorkspaceDashboardData = {
  context: {
    context_id: 'context-fixture',
    snapshot_id: 'snapshot-fixture',
    filters: { ...initialFilters },
    source_sha256: 'source-fixture',
    master_sha256: 'master-fixture',
    code_sha256: 'code-fixture',
    kpi_version: 'fixture',
    fetched_at: '2026-10-19T00:00:00Z',
  },
  options: {
    months: ['2024-01', '2024-02', '2024-03', '2024-04', '2024-05', '2024-06'],
    production_lines: ['fixture-line'],
    categories: ['fixture-category'],
    product_ids: ['101'],
    severities: ['Critical'],
    defect_types: ['fixture-type'],
    defect_locations: ['fixture-location'],
    inspection_methods: ['fixture-method'],
  },
  status: { stale: false, last_error: null, last_attempt_at: '2026-10-19T00:00:00Z' },
  analysis: {
    recommendations: [
      {
        priority: 1,
        title: 'Fixture',
        evidence: 'Fixture',
        action: 'Fixture',
        owner: 'Fixture',
        success_measure: 'Fixture',
      },
    ],
  },
};
const immutableFilters = JSON.stringify(data.context.filters);
assert.equal(
  validateScopePatch({ severity: 'Critical', scenario: 'year_2024' }, data).scenario,
  'year_2024',
);
assert.equal(JSON.stringify(data.context.filters), immutableFilters);
for (const patch of [
  {},
  { unknown: '' },
  { severity: 'not-known' },
  { product_id: 101 },
  { product_id: '999' },
  { scenario: 'invented' },
  { start_month: '' },
  { start_month: '2024-02', end_month: '2024-01' },
  { start_month: '2025-01' },
])
  assert.throws(() => validateScopePatch(patch, data), TypeError);

let snapshot: WorkspaceSnapshot = {
  tab: 'dashboard',
  locale: 'en',
  data,
  loading: false,
  refreshing: false,
  error: '',
  canAnalyze: true,
  sourceChecked: false,
};
const actions: string[] = [];
const adapter: WorkspaceToolAdapter = {
  getSnapshot: () => snapshot,
  async navigate(tab) {
    actions.push('navigate');
    snapshot = { ...snapshot, tab };
  },
  async changeLanguage(locale) {
    actions.push('language');
    snapshot = { ...snapshot, locale };
  },
  async applyScope(filters) {
    actions.push('apply');
    snapshot = {
      ...snapshot,
      data: { ...data, context: { ...data.context, context_id: 'context-next', filters } },
    };
    return { status: 'applied', context_id: 'context-next' };
  },
  async refreshScope() {
    actions.push('refresh');
    return { status: 'failed', error: 'fixture refresh error' };
  },
  async stageAnalysis() {
    actions.push('stage');
    return { id: 1, context_id: data.context.context_id, status: 'staged' };
  },
};
const workspaceTools = buildWorkspaceTools(adapter);
function execute(name: string, input: unknown, signal?: AbortSignal): Promise<unknown> {
  const selected = workspaceTools.find((item) => item.name === name);
  assert.ok(selected);
  return Promise.resolve().then(() => selected.execute(input, { signal }));
}
for (const definition of workspaceTools) {
  assert.equal(definition.inputSchema.additionalProperties, false);
  assert.equal(
    definition.annotations.consequentialHint,
    false,
    'Core tools never submit paid prompts',
  );
}
assert.equal(
  workspaceTools.find((item) => item.name === 'read_workspace_state')?.annotations.readOnlyHint,
  true,
);
const initialRead = (await execute('read_workspace_state', {})) as ReturnType<
  typeof import('../src/workspace-webmcp').workspaceState
>;
assert.equal(initialRead.dashboard.context?.context_id, 'context-fixture');
assert.deepEqual(initialRead.dashboard.scope_options?.scenario, [
  '',
  'exclude_cost_10',
  'ambiguous_dm',
  'year_2024',
]);
await assert.rejects(execute('read_workspace_state', { unexpected: true }), TypeError);
await assert.rejects(execute('navigate_workspace', { workspace: 'operations' }), TypeError);
await assert.rejects(execute('set_workspace_language', { language: 'fr' }), TypeError);
await assert.rejects(
  execute('apply_dashboard_scope', {
    context_id: 'wrong-context',
    filters: { severity: 'Critical' },
  }),
  /stale/,
);
await assert.rejects(
  execute('apply_dashboard_scope', {
    context_id: data.context.context_id,
    filters: { severity: 'Critical', injected: true },
  }),
  TypeError,
);
snapshot = { ...snapshot, loading: true };
await assert.rejects(
  execute('apply_dashboard_scope', {
    context_id: data.context.context_id,
    filters: { severity: 'Critical' },
  }),
  /changing/,
);
snapshot = { ...snapshot, loading: false, refreshing: true };
await assert.rejects(
  execute('stage_dashboard_analysis', { context_id: data.context.context_id }),
  /changing/,
);
snapshot = { ...snapshot, refreshing: false };
await assert.rejects(
  execute('stage_dashboard_analysis', { context_id: data.context.context_id, priority: 99 }),
  TypeError,
);
await assert.rejects(
  execute('stage_dashboard_analysis', { context_id: data.context.context_id, priority: '1' }),
  TypeError,
);
snapshot = { ...snapshot, canAnalyze: false };
await assert.rejects(
  execute('stage_dashboard_analysis', { context_id: data.context.context_id }),
  /not currently available/,
);
snapshot = { ...snapshot, canAnalyze: true };
await assert.rejects(
  execute(
    'apply_dashboard_scope',
    { context_id: data.context.context_id, filters: { severity: 'Critical' } },
    aborted.signal,
  ),
  { name: 'AbortError' },
);
assert.equal(
  actions.length,
  0,
  'Malformed, stale, unavailable and aborted requests do not dispatch actions',
);
assert.equal(
  ((await execute('navigate_workspace', { workspace: 'agent' })) as { status: string }).status,
  'opened',
);
assert.equal(snapshot.tab, 'agent');
await execute('set_workspace_language', { language: 'vi' });
assert.equal(snapshot.locale, 'vi');
const staged = (await execute('stage_dashboard_analysis', {
  context_id: data.context.context_id,
  priority: 1,
})) as { status: string; submitted: boolean };
assert.equal(staged.status, 'staged');
assert.equal(staged.submitted, false);
const refreshResult = (await execute('refresh_dashboard_source', {
  context_id: data.context.context_id,
})) as { status: string; dashboard: { context: { context_id: string } } };
assert.equal(refreshResult.status, 'failed');
assert.equal(refreshResult.dashboard.context.context_id, data.context.context_id);
const appliedResult = (await execute('apply_dashboard_scope', {
  context_id: data.context.context_id,
  filters: { severity: 'Critical' },
})) as { status: string; dashboard: { context: { context_id: string } } };
assert.equal(appliedResult.status, 'applied');
assert.equal(appliedResult.dashboard.context.context_id, 'context-next');
assert.equal(data.context.filters.severity, '', 'An old immutable context is not mutated');
await assert.rejects(
  execute('refresh_dashboard_source', { context_id: data.context.context_id }),
  /stale/,
);
assert.deepEqual(actions, ['navigate', 'language', 'stage', 'refresh', 'apply']);

console.log(
  'WebMCP core contract fixtures passed: lifecycle, validation, abort, committed UI waits, scope identity and stage-only semantics. Native browser integration remains a separate check.',
);
