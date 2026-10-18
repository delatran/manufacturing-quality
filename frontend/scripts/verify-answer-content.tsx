import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import EventView, { businessReportText } from '../src/EventView';
import { answerCodeText } from '../src/AnswerCode';
import { I18nProvider } from '../src/i18n';
import type { WorkspaceEvent } from '../src/types';

Object.defineProperty(globalThis, 'React', { value: React });
Object.defineProperty(globalThis, 'window', {
  value: { location: { origin: 'http://localhost:8001' } },
});
const source: WorkspaceEvent = {
  id: 1,
  session_id: 'fixture',
  run_id: 'a'.repeat(32),
  time: '',
  type: 'message',
  data: { role: 'assistant', qa: { verified: true } },
};
for (const code of [
  '```sql\nSELECT event_count FROM monthly;\n```',
  '~~~json\n{"source": "[claim:literal]", "value": 0}\n~~~',
  '    [claim:literal]\n    artifacts/source.json',
  '>     [claim:literal]\n>     artifacts/source.json',
  '````text\n```\n[claim:literal]\n```\n````',
  '`[claim:literal]`',
  '``literal ` [claim:literal]``',
  '`line one\n[claim:literal]`',
  'Literal marker example: `[claim:literal]\\`.',
  '\\' + '`'.repeat(2) + '[claim:literal]' + '`',
  '```text\n<!-- evidence-table:0123456789abcdef01234567:start -->\n```',
]) {
  assert.equal(
    businessReportText(code),
    code,
    'Presentation preserves literal code and inline examples',
  );
  const report = { ...source, data: { ...source.data, text: code } };
  const original = JSON.stringify(report);
  const html = renderToStaticMarkup(<EventView event={report} />);
  assert.doesNotMatch(
    html,
    /citation-badge-wrapper/,
    'Literal claim syntax in code never becomes a citation',
  );
  assert.equal(JSON.stringify(report), original);
}
assert.equal(
  businessReportText('12 [claim:real] and `[claim:literal]`.'),
  '12 [claim:real] and `[claim:literal]`.',
);
assert.match(businessReportText('12 [claim:real] and `[claim:literal]`.'), /`\[claim:literal\]`/);
assert.equal(answerCodeText(<code>{'SELECT 0;\n'}</code>), 'SELECT 0;\n');
assert.equal(answerCodeText(<code>{['SELECT ', 0, ';\n']}</code>), 'SELECT 0;\n');
for (const language of ['en', 'vi'] as const) {
  const html = renderToStaticMarkup(
    <I18nProvider initialLocale={language} persist={false}>
      <EventView
        event={{
          ...source,
          data: { ...source.data, text: '```js\nconst label = "<script>";\n```' },
        }}
        language={language}
      />
    </I18nProvider>,
  );
  assert.match(html, /<pre[^>]*><code class="language-js">/);
  assert.match(html, /role="region" aria-label="(?:Code example|Đoạn mã)"/);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, language === 'vi' ? /Sao chép mã/ : /Copy code/);
}
for (const boundary of [
  'First paragraph `unmatched.\n\nA measured result: 12 [claim:metric] `literal.',
  'First paragraph `unmatched.\n\n## A measured result: 12 [claim:metric] `literal.',
  '- First item `unmatched.\n- A measured result: 12 [claim:metric] `literal.',
  '| Unmatched `example | A measured result: 12 [claim:metric] `literal |\n|---|---|',
  'Paragraph `unmatched\n> 12 [claim:metric] `literal.',
]) {
  const report = { ...source, data: { ...source.data, text: boundary } };
  const original = JSON.stringify(report);
  const html = renderToStaticMarkup(<EventView event={report} />);
  assert.match(
    html,
    /citation-badge-wrapper/,
    'A backtick in a separate paragraph, heading, list item or table cell cannot hide a real claim',
  );
  assert.doesNotMatch(html, /\[claim:metric\]/);
  assert.equal(JSON.stringify(report), original);
}
for (const literal of [
  '&#91;claim:literal&#93;',
  '\\[claim:literal]',
  '[claim\\:literal]',
  '12 [claim:real] with &#91;claim:literal&#93; in the same text node.',
  '[12 [claim:literal]](https://example.org/source)',
  '[literal](#citation:1:fake)',
  '[literal](#claim:1:fake)',
]) {
  const report = { ...source, data: { ...source.data, text: literal } };
  const original = JSON.stringify(report);
  const html = renderToStaticMarkup(<EventView event={report} />);
  assert.doesNotMatch(
    html,
    /citation-badge-wrapper/,
    'Decoded, escaped, linked and namespace-shaped literals cannot create evidence controls',
  );
  assert.equal(JSON.stringify(report), original);
}
const numbered = renderToStaticMarkup(
  <EventView
    event={{
      ...source,
      data: {
        ...source.data,
        text: [
          'Đường dây 😀: 12 [claim:a.rows[0].value].',
          '',
          '**Zero 0 [claim:b.zero].**',
          '',
          'Again 12 [claim:a.rows[0].value].',
          '',
          'Literal text: **&#91;claim:literal&#93;**.',
        ].join('\n'),
      },
    }}
  />,
);
assert.equal((numbered.match(/class="citation-badge-num">1</g) || []).length, 2);
assert.equal((numbered.match(/class="citation-badge-num">2</g) || []).length, 1);
assert.match(numbered, /\[claim:literal\]/);
assert.doesNotMatch(numbered, /\[claim:a\.rows|\[claim:b\.zero/);
const tableMarker = '<!-- evidence-table:0123456789abcdef01234567:start -->';
for (const newline of ['\n', '\r\n']) {
  const html = renderToStaticMarkup(
    <EventView
      event={{
        ...source,
        data: { ...source.data, text: `${tableMarker}${newline}${newline}Visible body.` },
      }}
    />,
  );
  assert.match(html, /Visible body/);
  assert.doesNotMatch(html, /evidence-table/);
}
const literalMarker = renderToStaticMarkup(
  <EventView
    event={{
      ...source,
      data: { ...source.data, text: `\`${tableMarker}\` and \`/api/artifacts/source.json\`` },
    }}
  />,
);
assert.match(literalMarker, /evidence-table/);
assert.match(literalMarker, /\/api\/artifacts\/source.json/);
const registered =
  '/api/sessions/fixture/runs/' + 'a'.repeat(32) + '/artifacts/artifacts/chart.png';
const chart = {
  ...source,
  data: {
    ...source.data,
    text: `![Monthly events](${registered})`,
    chart_urls: ['http://localhost:8001' + registered],
  },
};
const chartHtml = renderToStaticMarkup(<EventView event={chart} language="en" />);
assert.match(chartHtml, /Expand chart/);
assert.match(chartHtml, /answer-chart-paragraph/);
assert.doesNotMatch(chartHtml, /<p><span class="answer-chart/);
const unavailable = renderToStaticMarkup(
  <EventView event={{ ...chart, data: { ...chart.data, chart_urls: [] } }} />,
);
assert.doesNotMatch(
  unavailable,
  /<img|Expand chart/,
  'Unregistered images never gain chart controls',
);
const longAnswer = {
  ...source,
  data: {
    ...source.data,
    text: [
      '# Review',
      '',
      'The recorded source population stays unchanged.',
      '',
      '## Findings',
      '',
      'Review the available evidence and its limitations before making a decision. '.repeat(10),
      '',
      '## Limitations',
      '',
      'Counts describe logged events, with the source definitions preserved. '.repeat(10),
      '',
      '## Follow-up',
      '',
      'Confirm the question and the same frozen context for any follow-up. '.repeat(10),
    ].join('\n'),
  },
};
const longAnswerSnapshot = JSON.stringify(longAnswer);
const integrated = renderToStaticMarkup(
  <I18nProvider initialLocale="en" persist={false}>
    <EventView event={longAnswer} language="en" />
    <EventView event={{ ...longAnswer, id: 2 }} language="vi" />
  </I18nProvider>,
);
const sectionIds = [
  ...integrated.matchAll(/<h[1-6] id="(answer-section-[^"]+)" tabindex="-1">/g),
].map((match) => match[1]);
const sectionLinks = [...integrated.matchAll(/<a href="#(answer-section-[^"]+)"([^>]*)>/g)];
assert.equal(sectionIds.length, 6);
assert.equal(
  new Set(sectionIds).size,
  6,
  'Separate rendered answers have distinct section targets',
);
assert.equal(sectionLinks.length, 6);
for (const link of sectionLinks) {
  assert.ok(sectionIds.includes(link[1]));
  assert.doesNotMatch(
    link[2],
    /target=|rel=/,
    'Generated section links stay within the same answer',
  );
}
assert.match(integrated, /<summary>In this answer<\/summary>/);
assert.match(integrated, /<summary>Trong câu trả lời<\/summary>/);
assert.match(integrated, /Counts describe logged events, with the source definitions preserved/);
assert.equal(JSON.stringify(longAnswer), longAnswerSnapshot);
const userQuestion = renderToStaticMarkup(
  <EventView event={{ ...longAnswer, data: { ...longAnswer.data, role: 'user' } }} />,
);
assert.doesNotMatch(userQuestion, /answer-outline/);
console.log(
  'Verified source-preserving code, copy controls, inert examples, registered chart-only presentation, and scoped answer navigation in the real EventView renderer.',
);
