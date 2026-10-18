import assert from 'node:assert/strict';
import React from 'react';
import { buildPromptMessage } from '../src/AgentWorkspace';
import {
  answerReplyAvailable,
  parseAnswerReply,
  promptAcknowledges,
  questionForAnswer,
} from '../src/answer-reply';
import type { Session, WorkspaceEvent } from '../src/types';

Object.defineProperty(globalThis, 'React', { value: React });
const run = 'a'.repeat(32);
const base = { session_id: 'session', run_id: run, time: '' };
const started: WorkspaceEvent = {
  ...base,
  id: 2,
  type: 'run_started',
  data: { question: 'What is a logged event?' },
};
const report: WorkspaceEvent = {
  ...base,
  id: 3,
  type: 'message',
  data: { role: 'assistant', text: 'An event is a recorded observation.', qa: { verified: true } },
};
const status: WorkspaceEvent = { ...base, id: 4, type: 'status', data: { state: 'completed' } };
const events = [started, report, status];
const session: Session = {
  id: 'session',
  events,
  runs: [{ id: run, state: 'completed', language: 'en' }],
};
const reference = {
  sessionId: 'session',
  run_id: run,
  message_id: 3,
  language: 'en' as const,
  question: 'What is a logged event?',
};
assert.deepEqual(parseAnswerReply(reference), reference);
for (const invalid of [
  null,
  true,
  [],
  {},
  { ...reference, message_id: true },
  { ...reference, message_id: '3' },
  { ...reference, message_id: -1 },
  { ...reference, message_id: Number.MAX_SAFE_INTEGER + 1 },
  { ...reference, run_id: '../run' },
  { ...reference, language: 'fr' },
]) {
  assert.equal(parseAnswerReply(invalid), undefined);
}
assert.equal(answerReplyAvailable(reference, session, events), true);
assert.equal(answerReplyAvailable(reference, null, events), false);
assert.equal(answerReplyAvailable({ ...reference, sessionId: 'other' }, session, events), false);
assert.equal(
  answerReplyAvailable(
    reference,
    session,
    events.map((event) =>
      event.id === report.id
        ? { ...event, data: { ...event.data, qa: { verified: false } } }
        : event,
    ),
  ),
  false,
);
const laterRun = { ...status, run_id: 'b'.repeat(32), id: 5, data: { state: 'running' } };
assert.equal(
  answerReplyAvailable(reference, session, [...events, laterRun]),
  true,
  'Other active work cannot retarget the source answer',
);
assert.deepEqual(questionForAnswer(report, events), {
  ...started,
  type: 'message',
  data: { role: 'user', text: 'What is a logged event?' },
});
assert.equal(
  questionForAnswer(report, [{ ...started, data: {} }, report, status]),
  undefined,
  'Legacy queued questions are never guessed',
);
const payload = buildPromptMessage(
  'Explain this simply.',
  'en',
  'original-context',
  'none',
  undefined,
  reference,
);
assert.deepEqual(payload.reply_to, { run_id: run, message_id: 3 });
assert.equal(payload.steer_run_id, undefined);
assert.equal(payload.action_intent, 'none');
assert.equal(payload.language, 'en');
const acknowledgement: WorkspaceEvent = {
  ...base,
  id: 6,
  type: 'message',
  data: {
    role: 'user',
    text: payload.text,
    analysis_context_id: payload.analysis_context_id,
    action_intent: 'none',
    reply_to: payload.reply_to,
  },
};
assert.equal(promptAcknowledges(acknowledgement, payload), true);
for (const conflicting of [
  { ...acknowledgement.data, reply_to: undefined },
  { ...acknowledgement.data, reply_to: { run_id: 'b'.repeat(32), message_id: 3 } },
  { ...acknowledgement.data, reply_to: { run_id: run, message_id: 7 } },
  { ...acknowledgement.data, analysis_context_id: 'other-context' },
  { ...acknowledgement.data, action_intent: 'draft_update' },
])
  assert.equal(
    promptAcknowledges({ ...acknowledgement, data: conflicting }, payload),
    false,
    'Same-text replay for another source cannot acknowledge this send',
  );
assert.equal(promptAcknowledges(acknowledgement, null), false);
assert.equal(
  promptAcknowledges(
    { ...acknowledgement, data: { ...acknowledgement.data, language: 'vi' } },
    payload,
  ),
  true,
  'Server question-language resolution does not change source admission',
);
assert.throws(() =>
  buildPromptMessage('Explain.', 'en', 'context', 'draft_create', undefined, reference),
);
assert.throws(() =>
  buildPromptMessage('Explain.', 'en', 'context', 'none', 'b'.repeat(32), reference),
);
assert.equal(
  JSON.stringify(reference),
  JSON.stringify(parseAnswerReply(JSON.parse(JSON.stringify(reference)))),
  'Reload preserves the exact reference and language',
);
console.log(
  'Verified persistent answer references, exact question metadata, session isolation and separate-run request construction.',
);
