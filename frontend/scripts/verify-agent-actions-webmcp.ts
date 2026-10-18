import assert from 'node:assert/strict';
import { buildAgentActionTools } from '../src/agent-actions-webmcp';
import type { AgentActionAdapter } from '../src/agent-actions-webmcp';
import {
  canonicalPromptRequest,
  clearPendingPromptRequest,
  createClientRequestId,
  parsePromptReceipt,
  pendingPromptStorageKey,
  readPendingPromptRequest,
  samePromptRequest,
  savePendingPromptRequest,
} from '../src/agent-prompt-requests';
import type { PromptRequest } from '../src/agent-prompt-requests';

const key = 'f56db45e-73e9-48ac-a1c7-a316b0a9bc42';
assert.equal(
  createClientRequestId({
    randomUUID: () => key,
    getRandomValues: crypto.getRandomValues.bind(crypto),
  }),
  key,
);
assert.match(
  createClientRequestId({ getRandomValues: crypto.getRandomValues.bind(crypto) }),
  /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/,
);
const context = 'c'.repeat(64);
const run = 'a'.repeat(32);
const input = {
  client_request_id: key,
  text: '  Explain the source.  ',
  analysis_context_id: context,
  language: 'en',
};
const calls: { action: string; value: unknown }[] = [];
const adapter: AgentActionAdapter = {
  getStatus: () => ({ session_id: 'session', run_state: 'idle' }),
  stagePrompt: async (text, contextId) => {
    calls.push({ action: 'stage', value: { text, contextId } });
    return { status: 'staged', submitted: false };
  },
  stageFollowup: async (reference, text) => {
    calls.push({ action: 'followup', value: { reference, text } });
    return { status: 'staged', submitted: false };
  },
  submit: async (request) => {
    calls.push({ action: 'submit', value: request });
    return { status: 'awaiting_acknowledgement', client_request_id: request.client_request_id };
  },
  readReceipt: async (clientRequestId) => {
    calls.push({ action: 'read', value: clientRequestId });
    return { status: 'uncertain' };
  },
  cancel: async (runId) => {
    calls.push({ action: 'cancel', value: runId });
    return { status: 'requested', provider_stopped: false };
  },
};
const tools = buildAgentActionTools(adapter);
const tool = (name: string) => {
  const found = tools.find((candidate) => candidate.name === name);
  assert.ok(found, name);
  return found;
};
for (const item of tools) {
  assert.equal(item.inputSchema.additionalProperties, false);
  assert.equal(item.annotations.untrustedContentHint, true);
}
assert.equal(tools.length, 7);
assert.equal(tool('read_agent_status').annotations.readOnlyHint, true);
assert.equal(tool('stage_agent_prompt').annotations.consequentialHint, false);
for (const name of ['submit_agent_prompt', 'steer_agent_run', 'request_agent_cancellation'])
  assert.equal(tool(name).annotations.consequentialHint, true);

await tool('stage_agent_prompt').execute(
  { text: '  Inspect this.  ', analysis_context_id: context },
  {},
);
assert.deepEqual(calls.pop(), {
  action: 'stage',
  value: { text: 'Inspect this.', contextId: context },
});
await tool('stage_agent_answer_followup').execute(
  { run_id: run, message_id: 3, text: 'Explain.' },
  {},
);
assert.deepEqual(calls.pop(), {
  action: 'followup',
  value: { reference: { run_id: run, message_id: 3 }, text: 'Explain.' },
});
await tool('submit_agent_prompt').execute(input, {});
await tool('submit_agent_prompt').execute(input, {});
assert.deepEqual(
  calls[0],
  calls[1],
  'Repeated identical tool arguments preserve the caller key and exact canonical payload',
);
const request = calls[0].value as PromptRequest;
assert.equal(request.client_request_id, key);
assert.equal(request.request_origin, 'webmcp');
assert.equal(request.text, 'Explain the source.');
assert.equal(request.action_intent, 'none');
calls.length = 0;
await tool('steer_agent_run').execute(
  { ...input, run_id: run, action_intent: 'draft_update', analysis_context_id: null },
  {},
);
assert.deepEqual(calls.pop()?.value, {
  ...request,
  steer_run_id: run,
  action_intent: 'draft_update',
  analysis_context_id: null,
});

for (const invalid of [
  { ...input, client_request_id: undefined },
  { ...input, client_request_id: 'new-key' },
  { ...input, client_request_id: key.toUpperCase() },
  { ...input, text: '   ' },
  { ...input, analysis_context_id: '../../other' },
  { ...input, language: 'fr' },
  { ...input, arbitrary_url: 'https://example.com' },
  { ...input, reply_to: { run_id: run, message_id: true } },
  { ...input, reply_to: { run_id: run, message_id: Number.MAX_SAFE_INTEGER + 1 } },
  { ...input, reply_to: { run_id: run, message_id: 3, session_id: 'another' } },
])
  await assert.rejects(async () => tool('submit_agent_prompt').execute(invalid, {}));
for (const invalid of [null, [], { surprise: true }, new Date()])
  await assert.rejects(async () => tool('read_agent_status').execute(invalid, {}));
assert.equal(calls.length, 0, 'Malformed input never reaches an action');
const aborted = new AbortController();
aborted.abort();
for (const [name, value] of [
  ['submit_agent_prompt', input],
  ['stage_agent_prompt', { text: 'Stage', analysis_context_id: context }],
  ['read_agent_prompt_receipt', { client_request_id: key }],
  ['request_agent_cancellation', { run_id: run }],
] as const)
  await assert.rejects(async () => tool(name).execute(value, { signal: aborted.signal }), {
    name: 'AbortError',
  });
assert.equal(calls.length, 0, 'Already aborted invocations have no effect');

const receipt = {
  client_request_id: key,
  session_id: 'session',
  status: 'accepted',
  run_id: run,
  action: 'start',
  request: canonicalPromptRequest(request),
  request_sha256: 'f'.repeat(64),
  replayed: false,
  queued: false,
};
assert.deepEqual(parsePromptReceipt(receipt, 'session'), receipt);
assert.equal(parsePromptReceipt(receipt, 'another'), undefined);
for (const invalid of [
  { ...receipt, client_request_id: 'invalid' },
  { ...receipt, status: 'completed' },
  { ...receipt, run_id: '../run' },
  { ...receipt, request_sha256: 'bad' },
  { ...receipt, queued: 'false' },
  { ...receipt, request: { ...receipt.request, language: 'fr' } },
])
  assert.equal(parsePromptReceipt(invalid, 'session'), undefined);
assert.ok(samePromptRequest(request, { ...request, text: '  Explain the source. ' }));
for (const changed of [
  { ...request, text: 'Another question' },
  { ...request, language: 'vi' as const },
  { ...request, request_origin: 'ui' as const },
  { ...request, analysis_context_id: null },
  { ...request, steer_run_id: run },
  { ...request, reply_to: { run_id: run, message_id: 3 } },
])
  assert.equal(samePromptRequest(request, changed), false);

const values = new Map<string, string>();
const storage = {
  getItem: (name: string) => values.get(name) ?? null,
  setItem: (name: string, value: string) => {
    values.set(name, value);
  },
  removeItem: (name: string) => {
    values.delete(name);
  },
};
const pending = { session_id: 'session', request };
savePendingPromptRequest(storage, pending);
assert.deepEqual(readPendingPromptRequest(storage, 'session'), {
  session_id: 'session',
  request: { ...canonicalPromptRequest(request), client_request_id: key },
});
assert.equal(readPendingPromptRequest(storage, 'another'), undefined);
clearPendingPromptRequest(storage, 'another', key);
assert.ok(values.has(pendingPromptStorageKey), 'Another visitor/session cannot clear this request');
clearPendingPromptRequest(storage, 'session', 'b56db45e-73e9-48ac-a1c7-a316b0a9bc42');
assert.ok(
  values.has(pendingPromptStorageKey),
  'A different tab/request cannot clear the pending key',
);
clearPendingPromptRequest(storage, 'session', key);
assert.equal(values.size, 0);
storage.setItem(pendingPromptStorageKey, '{broken');
assert.equal(readPendingPromptRequest(storage, 'session'), undefined);
console.log(
  'Verified seven Agent tool contracts, explicit stable request keys, strict runtime validation, abort-before-action, session-bound receipt parsing and reload storage without retries. This is an isolated deterministic fixture, not native browser or provider evidence.',
);
