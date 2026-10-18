import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  answerSectionId,
  isAnswerSectionLink,
  remarkAnswerPresentation,
} from '../src/answer-presentation';
import type { AnswerPresentationOptions } from '../src/answer-presentation';

const options: AnswerPresentationOptions = { prefix: ':answer:one:', language: 'en' };
const detail =
  'These observations describe recorded manufacturing events. Review the source population and its limitations before deciding which process to investigate. ';
const paragraphs = (count = 4) => detail.repeat(count);
const report = [
  '# Quality review',
  '',
  'The recorded scope remains the basis for this answer.',
  '',
  '## Findings [1](#citation:1:analysis.events)',
  '',
  paragraphs(),
  '',
  '## Limitations',
  '',
  paragraphs(),
  '',
  '## Next checks',
  '',
  paragraphs(),
].join('\n');

function render(value: string, presentation: AnswerPresentationOptions | false = options) {
  return renderToStaticMarkup(
    React.createElement(Markdown, {
      remarkPlugins: presentation
        ? [remarkGfm, [remarkAnswerPresentation, presentation]]
        : [remarkGfm],
      children: value,
    }),
  );
}

const sourceSnapshot = report;
const html = render(report);
assert.match(
  html,
  /<details class="answer-outline">\s*<summary>In this answer<\/summary>\s*<nav aria-label="In this answer">\s*<ol class="answer-outline-sections">/,
  'The generated disclosure contains a real summary, navigation landmark, and semantic list',
);
assert.ok(
  html.indexOf('The recorded scope remains') < html.indexOf('<details'),
  'The authored title and introduction stay ahead of the navigation',
);
for (const ordinal of [2, 3, 4]) {
  const id = answerSectionId(options.prefix, ordinal);
  assert.ok(html.includes(`href="#${id}"`));
  assert.ok(html.includes(`<h2 id="${id}" tabindex="-1">`));
}
assert.ok(!html.includes(`href="#${answerSectionId(options.prefix, 1)}"`));
assert.match(html, /<h1>Quality review<\/h1>/);
assert.equal((html.match(/href="#citation:1:analysis.events"/g) || []).length, 1);
assert.match(html, /<a href="#answer-section-[^"]+">Findings<\/a>/);
assert.equal(report, sourceSnapshot);
assert.equal(render(report), html, 'The same prefix and source produce deterministic section IDs');
let withoutNavigation = html.replace(/<details class="answer-outline">[\s\S]*?<\/details>\n?/, '');
for (const ordinal of [2, 3, 4])
  withoutNavigation = withoutNavigation.replace(
    ` id="${answerSectionId(options.prefix, ordinal)}" tabindex="-1"`,
    '',
  );
assert.equal(
  withoutNavigation,
  render(report, false),
  'Removing generated navigation and focus anchors restores the exact original rendered answer',
);

const replayed = renderToStaticMarkup(
  React.createElement(Markdown, {
    remarkPlugins: [
      remarkGfm,
      [remarkAnswerPresentation, options],
      () => remarkAnswerPresentation(options),
    ],
    children: report,
  }),
);
assert.equal(
  replayed,
  html,
  'Applying the projection twice does not duplicate navigation or content',
);

for (const simple of [
  'Hello. What would you like to investigate?',
  'Which production line should this question cover?',
  '## Findings\n\nRecorded events need review.\n\n## Limits\n\nCosts are incomplete.\n\n## Next\n\nConfirm the source.',
  `## Findings\n\n${paragraphs(10)}\n\n## Limits\n\n${paragraphs(10)}`,
]) {
  assert.equal(
    render(simple),
    render(simple, false),
    'A short answer, clarification, or report with few sections remains unchanged',
  );
}
assert.equal(render(report, { ...options, interactive: false }), render(report, false));

const parsedSections = [
  'Findings',
  '--------',
  '',
  paragraphs(),
  '',
  '~~~markdown',
  '# Fake fenced section',
  '~~~',
  '',
  '    # Fake indented section',
  '',
  '> ## A quoted heading',
  '>',
  '> Quoted context remains quoted context.',
  '',
  '- Item with quoted source:',
  '',
  '  ### A heading inside a list',
  '',
  '## Findings',
  '',
  paragraphs(),
  '',
  '## **Giới hạn** và `repair_cost`',
  '',
  paragraphs(),
].join('\n');
const parsedHtml = render(parsedSections);
assert.equal((parsedHtml.match(/href="#answer-section-/g) || []).length, 3);
assert.match(parsedHtml, /<h2 id="[^"]+-1" tabindex="-1">Findings<\/h2>/);
assert.match(parsedHtml, /<h2 id="[^"]+-2" tabindex="-1">Findings<\/h2>/);
assert.match(parsedHtml, /<h2 id="[^"]+-3" tabindex="-1"><strong>Giới hạn<\/strong>/);
assert.match(parsedHtml, /<a href="[^"]+">Giới hạn và repair_cost<\/a>/);
assert.match(parsedHtml, /<h2>A quoted heading<\/h2>/);
assert.match(parsedHtml, /<h3>A heading inside a list<\/h3>/);
assert.match(parsedHtml, /# Fake fenced section/);
assert.match(parsedHtml, /# Fake indented section/);
assert.doesNotMatch(
  parsedHtml,
  /<a href="#answer-section-[^"]+">(?:Fake|A quoted|A heading inside)/,
);

const vietnamese = render(report, { ...options, language: 'vi' });
assert.match(vietnamese, /<summary>Trong câu trả lời<\/summary>/);
assert.match(vietnamese, /<nav aria-label="Trong câu trả lời">/);
assert.match(vietnamese, /Quality review/);
assert.match(vietnamese, /recorded manufacturing events/);
assert.doesNotMatch(vietnamese, /In this answer/);

const inlineSource = report.replace(
  '## Limitations',
  '##### Re**pair** &amp; [`cost`](https://example.org/source)',
);
const inlineHtml = render(inlineSource);
assert.match(inlineHtml, /<a href="#answer-section-[^"]+">Repair &amp; cost<\/a>/);
assert.match(
  inlineHtml,
  /<h5 id="[^"]+-3" tabindex="-1">Re<strong>pair<\/strong> &amp; <a href="https:\/\/example.org\/source"><code>cost<\/code><\/a><\/h5>/,
  'Skipped levels, formatted words, decoded entities, inline code and source links keep their meaning',
);
assert.equal((inlineHtml.match(/href="https:\/\/example.org\/source"/g) || []).length, 1);

const imageOnlySection = report.replace(
  '## Limitations',
  '## ![Hidden chart](/api/unselected.png)',
);
assert.doesNotMatch(
  render(imageOnlySection),
  /answer-outline/,
  'An image-only heading does not promote an unavailable chart alt into a section label',
);

const secondPrefix = ':answer:two:';
const secondHtml = render(report, { ...options, prefix: secondPrefix });
assert.ok(!secondHtml.includes(`id="${answerSectionId(options.prefix, 2)}"`));
assert.ok(secondHtml.includes(`id="${answerSectionId(secondPrefix, 2)}"`));
assert.notEqual(answerSectionId(':a:', 1), answerSectionId('-3a-a-3a-', 1));
assert.notEqual(answerSectionId('', 1), answerSectionId('root', 1));
assert.notEqual(answerSectionId('Tiếng Việt', 1), answerSectionId('Tieng Viet', 1));
assert.notEqual(answerSectionId('😀', 1), answerSectionId('😁', 1));
for (const ordinal of [
  0,
  -1,
  1.5,
  Number.NaN,
  Number.POSITIVE_INFINITY,
  Number.MAX_SAFE_INTEGER + 1,
])
  assert.throws(() => answerSectionId(options.prefix, ordinal), RangeError);

assert.equal(isAnswerSectionLink(`#${answerSectionId(options.prefix, 2)}`, options.prefix), true);
for (const href of [
  undefined,
  null,
  3,
  '#citation:1:analysis.events',
  `#${answerSectionId(secondPrefix, 2)}`,
  `https://example.org/#${answerSectionId(options.prefix, 2)}`,
  `#${answerSectionId(options.prefix, 2)}?query=1`,
  `#${answerSectionId(options.prefix, 2)}#suffix`,
  `#${answerSectionId(options.prefix, 2)}-extra`,
  `#${answerSectionId(options.prefix, 2).replace(/-2$/, '-02')}`,
  `#${answerSectionId(options.prefix, 2).replace(/-2$/, '-0')}`,
])
  assert.equal(isAnswerSectionLink(href, options.prefix), false);

const hiddenLength = [
  '## First',
  '',
  'Short content.',
  '',
  '```text',
  detail.repeat(20),
  '```',
  '',
  '## Second',
  '',
  'Short content.',
  '',
  '## Third',
  '',
  'Short content.',
].join('\n');
assert.equal(
  render(hiddenLength),
  render(hiddenLength, false),
  'Hidden code cannot force a short report into navigation',
);

const hostileHeading = report.replace(
  '## Limitations',
  '## [Review](javascript:alert(1)) <script>',
);
const hostileHtml = render(hostileHeading);
assert.doesNotMatch(hostileHtml, /href="javascript:/);
assert.doesNotMatch(hostileHtml, /<script>/);
assert.match(hostileHtml, /<a href="#answer-section-[^"]+">Review<\/a>/);

const transform = remarkAnswerPresentation(options);
for (const malformed of [undefined, null, true, {}, { type: 'root', children: null }])
  assert.doesNotThrow(() => transform(malformed));

console.log(
  'Verified source-preserving long-answer navigation, real Markdown headings, duplicate and Unicode section identity, literal EN/VI labels, inert content, scoped links, and unchanged simple-answer fallback.',
);
