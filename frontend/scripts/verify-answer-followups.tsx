import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import AnswerFollowups from '../src/AnswerFollowups';
import {
  answerFollowups,
  answerSourceQuestion,
  eligibleAnswerFollowup,
} from '../src/answer-followups';
import type { WorkspaceEvent } from '../src/types';

Object.defineProperty(globalThis, 'React', { value: React });

const runId = 'a'.repeat(32);
const sessionId = 'source-session';
const event = (
  id: number,
  type: string,
  data: Record<string, unknown>,
  run = runId,
): WorkspaceEvent => ({ id, session_id: sessionId, run_id: run, type, data, time: '' });
const report = (value: string, data: Record<string, unknown> = {}) =>
  event(4, 'message', { role: 'assistant', text: value, qa: { verified: true }, ...data });
const question = (value: string) => event(1, 'message', { role: 'user', text: value });
const complete = event(5, 'status', { state: 'completed' });
const numericClaim = {
  id: 'verified-claim',
  execution_id: 'execution-1',
  path: '$.analysis.event_count',
  code_sha256: 'b'.repeat(64),
  value: 12,
};
const numbers = report('The recorded count is 12 [source](#citation:verified-claim).', {
  claims: [numericClaim],
});
const ids = (value: WorkspaceEvent, origin = question('Summarize the selected results.')) =>
  answerFollowups(value, origin, 'en').map((choice) => choice.id);

// Only a persisted, verified final answer in its completed run can be targeted.
assert.equal(eligibleAnswerFollowup(numbers, [numbers, complete]), true);
assert.equal(eligibleAnswerFollowup(numbers, [numbers]), false);
assert.equal(
  eligibleAnswerFollowup(
    numbers,
    [numbers, event(5, 'status', {})],
    [{ id: runId, state: 'completed' }],
  ),
  false,
);
assert.equal(eligibleAnswerFollowup(numbers, [numbers], [{ id: runId, state: 'completed' }]), true);
assert.equal(
  eligibleAnswerFollowup(numbers, [numbers, complete], [{ id: runId, state: 'running' }]),
  true,
);
assert.equal(
  eligibleAnswerFollowup(
    numbers,
    [numbers, event(6, 'status', { state: 'running' })],
    [{ id: runId, state: 'completed' }],
  ),
  false,
);
for (const state of ['failed', 'cancelled', 'unverified', 'idle', 'provider_stopped']) {
  assert.equal(eligibleAnswerFollowup(numbers, [numbers, event(6, 'status', { state })]), false);
}
assert.equal(eligibleAnswerFollowup(numbers, [complete]), false);
assert.equal(
  eligibleAnswerFollowup(numbers, [numbers, complete, report('A replacement answer.')]),
  false,
);
assert.equal(
  eligibleAnswerFollowup(numbers, [
    numbers,
    complete,
    { ...report('Another session.'), session_id: 'other' },
  ]),
  true,
);
assert.equal(
  eligibleAnswerFollowup({ ...numbers, data: { ...numbers.data, text: 'Changed display text.' } }, [
    numbers,
    complete,
  ]),
  false,
);
assert.equal(
  eligibleAnswerFollowup({ ...numbers, data: { ...numbers.data, chart_urls: [] } }, [
    numbers,
    complete,
  ]),
  true,
);
for (const id of [0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1, '4', true]) {
  const invalid = { ...numbers, id } as WorkspaceEvent;
  assert.equal(eligibleAnswerFollowup(invalid, [invalid, complete]), false);
}
for (const run of ['', 'legacy-run', 'A'.repeat(32), 'a'.repeat(31)]) {
  const invalid = { ...numbers, run_id: run };
  assert.equal(eligibleAnswerFollowup(invalid, [invalid, { ...complete, run_id: run }]), false);
}
for (const qa of [null, false, [], {}, { verified: false }, { verified: 'true' }]) {
  const invalid = report('An answer.', { qa });
  assert.equal(eligibleAnswerFollowup(invalid, [invalid, complete]), false);
}
for (const invalid of [null, {}, { ...numbers, data: null }, { ...numbers, data: [] }]) {
  assert.equal(eligibleAnswerFollowup(invalid as WorkspaceEvent, [numbers, complete]), false);
  assert.deepEqual(answerFollowups(invalid as WorkspaceEvent, undefined, 'en'), []);
}
assert.equal(eligibleAnswerFollowup(report('  '), [report('  '), complete]), false);
assert.equal(
  eligibleAnswerFollowup(numbers, [numbers, complete, null as unknown as WorkspaceEvent]),
  true,
);

// Domain-specific comparisons and assignments never appear as generic defaults.
assert.deepEqual(ids(numbers), ['finding', 'limits', 'next']);
const numericChoices = answerFollowups(numbers, question('Summarize the selected results.'), 'vi');
assert.equal(numericChoices[0].label, 'Giải thích phát hiện');
assert.match(numericChoices[2].prompt, /Không tạo, sửa hoặc giao công việc/);
assert.doesNotMatch(
  numericChoices.map((choice) => choice.prompt).join(' '),
  /Line 3|Group 4|time halves/i,
);
for (const claim of [
  null,
  {},
  { ...numericClaim, value: NaN },
  { ...numericClaim, value: Infinity },
  { ...numericClaim, value: '12' },
  { ...numericClaim, code_sha256: 'invalid' },
]) {
  assert.deepEqual(ids(report('The recorded count is 12.', { claims: [claim] })), []);
}

const code = report('Here is the example.\n\n```python\nprint("hello")\n```');
assert.deepEqual(ids(code), ['code', 'edge']);
assert.deepEqual(ids(report('~~~~js\nconst count = 0;\n~~~~~')), ['code', 'edge']);
assert.deepEqual(ids(report('```text\nNo data are changed by this example.\n```')), [
  'code',
  'edge',
]);
for (const value of [
  '```python\nprint("hello")',
  '~~~~js\nconst count = 0;\n~~~',
  '```python\nprint("hello")\n~~~',
  '```\n\n```',
  '> ```python\n> print("hello")\n> ```',
  '    ```python\n    print("hello")\n    ```',
  '  ```python\n  print("hello")\n  ```',
  'Use `print("hello")` to print text.',
  '<!-- ```python\nprint("hello")\n``` -->',
]) {
  assert.deepEqual(ids(report(value)), [], value);
}

const table = report('| Option | Description |\n| --- | --- |\n| A | Available |\n| B | Limited |');
assert.deepEqual(ids(table), ['table', 'limits']);
assert.deepEqual(ids(report('Option | Description\n:--- | ---:\nA | Available')), [
  'table',
  'limits',
]);
for (const value of [
  'Option | Description\nA | Available',
  '| Option | Description |\n| --- | --- |',
  '> | Option | Description |\n> | --- | --- |\n> | A | Available |',
  '| Option | Description |\n| --- | --- |\nOnly one cell',
]) {
  assert.deepEqual(ids(report(value)), []);
}
assert.deepEqual(
  ids(report('```text\n| Option | Description |\n| --- | --- |\n| A | Available |\n```')),
  ['code', 'edge'],
);

const chartPath = 'outputs/chart.png';
const chartUrl = `/api/sessions/${sessionId}/runs/${runId}/artifacts/${chartPath}`;
const artifact = {
  url: chartUrl,
  path: chartPath,
  mime_type: 'image/png',
  sha256: 'c'.repeat(64),
};
const chart = report('The registered chart shows the selected records.', {
  chart_urls: [`https://workspace.example${chartUrl}`],
  delivery: { version: 1, artifacts: [artifact] },
});
assert.deepEqual(ids(chart), ['chart', 'limits']);
assert.deepEqual(ids({ ...chart, data: { ...chart.data, claims: [numericClaim] } }), [
  'chart',
  'limits',
  'next',
]);
assert.deepEqual(
  ids({
    ...chart,
    data: {
      ...chart.data,
      chart_urls: [`https://workspace.example/manufacturing-quality${chartUrl}`],
    },
  }),
  ['chart', 'limits'],
);
for (const data of [
  { chart_urls: [] },
  { chart_urls: [chartUrl, 1] },
  { chart_urls: [`${chartUrl}?version=old`] },
  { chart_urls: [chartUrl.replace(runId, 'd'.repeat(32))] },
  { chart_urls: [chartUrl.replace(sessionId, 'other-session')] },
  { delivery: null },
  { delivery: { version: 2, artifacts: [artifact] } },
  { delivery: { version: 1, artifacts: [{ ...artifact, sha256: 'invalid' }] } },
  { delivery: { version: 1, artifacts: [{ ...artifact, path: '../chart.png' }] } },
  {
    delivery: {
      version: 1,
      artifacts: [{ ...artifact, url: `https://external.example${chartUrl}` }],
    },
  },
]) {
  assert.deepEqual(ids({ ...chart, data: { ...chart.data, ...data } }), []);
}
assert.deepEqual(ids(report('![A chart](https://external.example/chart.png)')), []);

// Simple or incomplete exchanges stay simple, even if a numeric claim is present.
for (const [value, origin] of [
  ['Hello. What would you like to explore?', 'Hello'],
  ["You're welcome.", 'Thanks!'],
  ['No matching data are available for this selection.', 'Summarize the results.'],
  ['There are **no matching records** in the selected scope.', 'Summarize the results.'],
  ['There are [no rows](#citation:coverage) to compare.', 'Summarize the results.'],
  ['Không có dữ liệu trong phạm vi này.', 'Tóm tắt kết quả.'],
  ["Sorry, I can't assist with that request.", 'Explain the request.'],
  ['**I can’t assist** with that request.', 'Explain the request.'],
  ['Tôi không thể hỗ trợ yêu cầu đó.', 'Giải thích yêu cầu.'],
  ['Could you clarify which scope you mean?', 'Show the comparison.'],
  ['Bạn muốn chọn phạm vi nào?', 'So sánh kết quả.'],
  ['Which option would you like to compare?', 'Compare the results.'],
]) {
  assert.deepEqual(ids(report(value, { claims: [numericClaim] }), question(origin)), [], value);
}
assert.deepEqual(
  ids(
    report('A reference intensity compares recorded events with reference output.'),
    question('What is reference intensity?'),
  ),
  ['concept'],
);
assert.deepEqual(
  ids(
    report('Cường độ tham chiếu là một phép so sánh.'),
    question('Định nghĩa reference intensity'),
  ),
  ['concept'],
);
assert.deepEqual(ids(report('A concise explanation.'), question('Tell me more.')), []);
assert.deepEqual(
  ids(report('A concise explanation.'), { ...question('What is this?'), session_id: 'other' }),
  [],
);
assert.equal(
  answerSourceQuestion(numbers, question('The exact source question.')),
  'The exact source question.',
);
assert.equal(
  answerSourceQuestion(numbers, { ...question('Other run.'), run_id: 'd'.repeat(32) }),
  undefined,
);
assert.equal(answerSourceQuestion(numbers, { ...question('A later question.'), id: 8 }), undefined);
assert.equal(
  answerSourceQuestion(numbers, { ...question('No canonical identity.'), id: '1' }),
  undefined,
);

// Rendering is inert and uses the source language, independently of the page locale.
let callbackCount = 0;
const onFollowUp = () => {
  callbackCount += 1;
};
const sourceQuestion = question('<script>source()</script> Explain the code.');
const snapshot = JSON.stringify({ code, sourceQuestion });
const english = renderToStaticMarkup(
  <AnswerFollowups
    report={code}
    question={sourceQuestion}
    language="en"
    eligible
    onFollowUp={onFollowUp}
  />,
);
assert.match(english, /lang="en"/);
assert.match(english, /Explain this example/);
assert.match(english, /From the question:/);
assert.match(english, /&lt;script&gt;source\(\)&lt;\/script&gt;/);
assert.doesNotMatch(english, /<script>|<form|type="submit"/);
assert.equal(callbackCount, 0);
const vietnamese = renderToStaticMarkup(
  <AnswerFollowups
    report={code}
    question={sourceQuestion}
    language="vi"
    eligible
    disabled
    onFollowUp={onFollowUp}
  />,
);
assert.match(vietnamese, /lang="vi"/);
assert.match(vietnamese, /Giải thích ví dụ/);
assert.equal((vietnamese.match(/disabled=""/g) || []).length, 2);
assert.equal(
  renderToStaticMarkup(<AnswerFollowups report={code} language="en" onFollowUp={onFollowUp} />),
  '',
);
assert.equal(renderToStaticMarkup(<AnswerFollowups report={code} language="en" eligible />), '');
assert.equal(
  renderToStaticMarkup(
    <AnswerFollowups
      report={report('Hello!')}
      question={question('Hello')}
      language="en"
      eligible
      onFollowUp={onFollowUp}
    />,
  ),
  '',
);
assert.equal(JSON.stringify({ code, sourceQuestion }), snapshot);
assert.equal(callbackCount, 0);

console.log(
  'Answer follow-up verification passed: persisted source, outcome gates, contextual choices, language and inert rendering.',
);
