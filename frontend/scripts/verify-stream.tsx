import assert from 'node:assert/strict';
import './verify-session-stream';
import './verify-conversation-state';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import EventView, {
  chartArtifact,
  businessReportText,
  friendlyError,
  resolveNumericClaim,
  formatEvidenceValue,
  citationPosition,
  filterReportRows,
} from '../src/EventView';
import AgentWorkspace, {
  buildPromptMessage,
  projectConversation,
  savedWorkForReport,
} from '../src/AgentWorkspace';
import { createI18n, I18nProvider } from '../src/i18n';
import { commandMatchesContext, parseUiCommand } from '../src/workspace-state';
import { projectEvents } from '../src/stream';
import { buildInvestigationPatch, InvestigationUnitNotes } from '../src/InvestigationPanel';
import type { Investigation } from '../src/InvestigationPanel';
import type { WorkspaceEvent } from '../src/types';
import AgentScope, { questionScope } from '../src/AgentScope';
import { initialFilters } from '../src/dashboard-types';
import type { DashboardData } from '../src/dashboard-types';

Object.defineProperty(globalThis, 'window', {
  value: { location: { origin: 'http://localhost:8001' } },
});
// The standalone TSX runner uses classic JSX; production Vite uses automatic JSX.
Object.defineProperty(globalThis, 'React', { value: React });
// Synthetic contexts exercise scope identity and rendering, not analytical results.
const dashboardScope: DashboardData['context'] = {
  context_id: 'dashboard-private',
  snapshot_id: 'snapshot-private',
  filters: { ...initialFilters, production_line: 'Line-3' },
  source_sha256: 'source-private',
  master_sha256: 'master-private',
  code_sha256: 'code-private',
  kpi_version: 'fixture',
  fetched_at: '2026-10-17T00:00:00Z',
};
const capturedScope = { ...dashboardScope, context_id: 'captured-private' };
assert.deepEqual(questionScope({}), {
  id: undefined,
  context: undefined,
  captured: false,
  unavailable: false,
  loading: false,
});
assert.equal(
  questionScope({ dashboardContextId: dashboardScope.context_id, dashboardContext: dashboardScope })
    .context,
  dashboardScope,
);
const missingDashboard = questionScope({ capturedContextId: capturedScope.context_id });
assert.equal(
  missingDashboard.captured,
  true,
  'A restored draft remains captured while the Dashboard is unavailable',
);
assert.equal(missingDashboard.loading, true, 'An unresolved captured scope must block sending');
assert.equal(missingDashboard.id, capturedScope.context_id);
const selection = {
  dashboardContextId: dashboardScope.context_id,
  dashboardContext: dashboardScope,
  capturedContextId: capturedScope.context_id,
};
const staleScope = questionScope({
  ...selection,
  loadedContext: dashboardScope,
  failedContextId: 'another-context',
});
assert.equal(
  staleScope.context,
  undefined,
  'Never display stale loaded filters under the captured ID',
);
assert.equal(staleScope.loading, true);
assert.equal(
  staleScope.unavailable,
  false,
  'A failure for another context cannot block this selection',
);
const failedScope = questionScope({ ...selection, failedContextId: capturedScope.context_id });
assert.equal(failedScope.unavailable, true);
assert.equal(failedScope.loading, false);
assert.equal(
  failedScope.id,
  capturedScope.context_id,
  'Failure retains the selected immutable context',
);
const recoveredScope = questionScope({ ...selection, loadedContext: capturedScope });
assert.equal(recoveredScope.context, capturedScope);
assert.equal(recoveredScope.loading, false);
assert.equal(recoveredScope.unavailable, false);
assert.equal(recoveredScope.id, failedScope.id, 'Retry recovery does not replace the draft scope');
const matchedScope = questionScope({ ...selection, capturedContextId: dashboardScope.context_id });
assert.equal(matchedScope.captured, false);
assert.equal(matchedScope.context, dashboardScope);
assert.equal(matchedScope.loading, false);
for (const locale of ['en', 'vi'] as const) {
  const i18n = createI18n(locale);
  const filters = {
    ...initialFilters,
    production_line: 'Line-3',
    product_id: '17',
    defect_type: '<script>alert(1)</script>',
    scenario: 'exclude_cost_10',
  };
  const snapshot = JSON.stringify(filters);
  const markup = renderToStaticMarkup(
    <I18nProvider initialLocale={locale} persist={false}>
      <AgentScope filters={filters} />
    </I18nProvider>,
  );
  assert.ok(markup.includes(i18n.month(filters.start_month)));
  assert.ok(markup.includes(i18n.month(filters.end_month)));
  assert.ok(markup.includes(i18n.t('ui.filter_production_line')));
  assert.ok(markup.includes(i18n.t('ui.agent_scope_scenario')));
  assert.ok(markup.includes(i18n.label(filters.scenario)));
  assert.match(markup, /#17/);
  assert.match(markup, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.doesNotMatch(markup, /<script|context_id|source_sha256|exclude_cost_10/);
  assert.match(markup, /<ul[^>]+aria-label=/);
  assert.equal(
    JSON.stringify(filters),
    snapshot,
    'Scope presentation must not mutate source filters',
  );
  const allScope = renderToStaticMarkup(
    <I18nProvider initialLocale={locale} persist={false}>
      <AgentScope filters={initialFilters} />
    </I18nProvider>,
  );
  assert.ok(allScope.includes(i18n.t('ui.agent_scope_all')));
  assert.doesNotMatch(allScope, /<ul/);
}
let sequence = 0;
const event = (type: string, data: Record<string, unknown>, run_id = 'run-a'): WorkspaceEvent => ({
  id: ++sequence,
  session_id: 'fixture',
  run_id,
  type,
  time: '2026-10-15T12:00:00Z',
  data,
});
const history = [
  event('status', { state: 'running', label: 'Preparing' }),
  event('reasoning', { text: 'First ', response_id: 'response-a' }),
  event('reasoning', { text: 'part.', response_id: 'response-a' }),
  event('tool', {
    name: 'local_python_repl',
    tool_id: 'tool-a',
    status: 'running',
    code: 'print(12)',
  }),
  event('tool', {
    name: 'local_python_repl',
    tool_id: 'tool-a',
    status: 'completed',
    result: 12,
    execution_id: 'exec-a',
    code_sha256: 'b'.repeat(64),
  }),
  event('reasoning', { text: 'Contiguous ' }),
  event('reasoning', { text: 'fallback.' }),
  event('todo', { items: [{ text: 'Analyze', status: 'running' }] }),
  event('todo', { items: [{ text: 'Analyze', status: 'completed' }] }),
  event('artifact', {
    name: 'report.md',
    mime: 'text/markdown',
    path: 'report.md',
    url: '/api/report.md',
    sha256: 'a'.repeat(64),
  }),
  event('artifact', { name: 'data.csv', mime: 'text/csv', path: 'data.csv', url: '/api/data.csv' }),
  event('artifact', {
    name: 'monthly.png',
    mime: 'image/png',
    path: 'monthly.png',
    url: '/api/monthly.png',
  }),
  event('status', { state: 'completed', label: 'Verified' }),
];
const snapshot = JSON.stringify(history);
const projected = projectEvents(history);
assert.equal(history.length, 13, 'Projection must not mutate source history');
assert.equal(projected.filter((e) => e.type === 'status').length, 1);
assert.equal(
  projected.at(-1)?.data.state,
  'completed',
  'Latest status retains its chronological position',
);
assert.equal(projected.filter((e) => e.type === 'tool').length, 1);
assert.equal(
  projected.find((e) => e.type === 'tool')?.data.code,
  'print(12)',
  'The source projection preserves technical evidence',
);
assert.equal(projected.find((e) => e.type === 'tool')?.data.status, 'completed');
assert.deepEqual(
  projected.filter((e) => e.type === 'reasoning').map((e) => e.data.text),
  ['First part.', 'Contiguous fallback.'],
);
assert.equal(projected.filter((e) => e.type === 'todo').length, 1);
assert.equal(
  (projected.find((e) => e.type === 'artifact_group')?.data.items as unknown[]).length,
  2,
);
assert.equal(projected.filter((e) => e.type === 'artifact').length, 1, 'Charts remain available');
assert.equal(JSON.stringify(history), snapshot);

const replayedReport = event('message', { role: 'assistant', text: 'Saved response.' });
const replayHistory = [replayedReport, { ...replayedReport }];
const replaySnapshot = JSON.stringify(replayHistory);
assert.equal(
  projectEvents(replayHistory).filter((item) => item.type === 'message').length,
  1,
  'Reconnected history displays a persisted report once',
);
assert.equal(JSON.stringify(replayHistory), replaySnapshot);
const providerSummary = (session: string, turn: string, text: string, eventId: string) =>
  event('reasoning', {
    provider_api: 'agents',
    provider_session_id: session,
    provider_turn_id: turn,
    event_id: eventId,
    item_id: 'shared-provider-item',
    text,
  });
const firstSummary = providerSummary('session-a', 'turn-a', 'First ', 'provider-event-a');
const managedSummaries = [
  firstSummary,
  { ...firstSummary, id: ++sequence },
  providerSummary('session-a', 'turn-a', 'summary.', 'provider-event-b'),
  providerSummary('session-a', 'turn-b', 'Second turn.', 'provider-event-a'),
  providerSummary('session-b', 'turn-a', 'Other session.', 'provider-event-a'),
];
const managedSummarySnapshot = JSON.stringify(managedSummaries);
assert.deepEqual(
  projectEvents(managedSummaries)
    .filter((item) => item.type === 'reasoning')
    .map((item) => item.data.text),
  ['First summary.', 'Second turn.', 'Other session.'],
  'Provider replay is deduplicated while repeated item IDs stay scoped to their session and turn',
);
assert.equal(JSON.stringify(managedSummaries), managedSummarySnapshot);

const simulations = projectEvents([
  event('reasoning', { simulation: true, text: 'Lead plan' }),
  event('tool', { name: 'test' }),
  event('reasoning', { simulation: true, text: 'Lead plan' }),
  event('reasoning', { simulation: true, text: 'QA plan' }),
]);
assert.equal(simulations.filter((e) => e.type === 'reasoning').length, 1);
assert.equal(simulations[0].data.text, 'Lead plan\n\nQA plan');

const report = event('message', {
  role: 'assistant',
  text: '| Metric | Value |\n|---|---:|\n| Count | 12 [claim:analysis.rows[0].value] |\n\nThe count reflects events, not unique units.\n\n<script>alert(1)</script>\n\n[bad](javascript:alert)\n\n![Chart](/api/monthly.png)\n\n[Technical bundle](/api/verified_evidence.zip)\n\n~~~json\n{"execution_id":"secret-execution"}\n~~~',
  claims: [
    {
      id: 'analysis.rows[0].value',
      value: 12,
      path: '$.rows[0].value',
      execution_id: 'exec-a',
      code_sha256: 'b'.repeat(64),
    },
  ],
  qa: { verified: true },
});
const originalReport = JSON.stringify(report);
const html = renderToStaticMarkup(<EventView event={report} />);
assert.match(html, /<table>/, 'GFM tables remain semantic and horizontally scrollable');
assert.match(
  html,
  /<td[^>]*>12\s*<span class="citation-badge-wrapper/,
  'The business number is accompanied by an interactive citation badge',
);
assert.match(html, /not unique units/, 'Business caveats remain in the report');
assert.doesNotMatch(html, /claim-reference|bbbbbbbb|Numeric evidence/);
assert.match(html, /secret-execution/, 'Authored JSON examples remain visible as inert code');
assert.doesNotMatch(html, /Technical bundle|verified_evidence\.zip/);
assert.doesNotMatch(html, /<script>/);
assert.doesNotMatch(html, /href="javascript:/);
assert.match(html, /src="http:\/\/localhost:8001\/api\/monthly.png"/);
assert.equal(
  JSON.stringify(report),
  originalReport,
  'Business rendering leaves canonical report, claims and QA untouched',
);
assert.equal(
  businessReportText('12 [claim:a.rows[0].value] and 14 [claim:a.other[12].value].'),
  '12 [claim:a.rows[0].value] and 14 [claim:a.other[12].value].',
  'Preparse cleanup leaves claim markers for the source-aware Markdown projection',
);
assert.equal(
  businessReportText({ execution_id: 'raw-json' }),
  '',
  'Unknown structured content must not expose raw JSON',
);
assert.equal(
  businessReportText(
    'Quy ước số để đối chiếu bằng chứng: dấu phẩy phân tách hàng nghìn, dấu chấm phân tách phần thập phân.',
  ),
  '',
);

// Old app-owned notes disappear wherever their standalone paragraph occurs.
const numberFormatPrefix =
  'Định dạng số: dấu chấm phân tách hàng nghìn, dấu phẩy phân tách phần thập phân.';
assert.equal(businessReportText(`${numberFormatPrefix}\n\nChào bạn!`), 'Chào bạn!');
assert.equal(businessReportText(`${numberFormatPrefix}\r\n\r\nChào bạn!`), 'Chào bạn!');
assert.equal(businessReportText(numberFormatPrefix), '');
assert.equal(businessReportText(`Chào bạn!\n\n${numberFormatPrefix}`), 'Chào bạn!');
assert.equal(
  businessReportText(`${numberFormatPrefix}\n\nChào bạn!\n\n${numberFormatPrefix}`),
  'Chào bạn!',
);
for (const wrapper of [
  ` ${numberFormatPrefix}`,
  `   ${numberFormatPrefix}`,
  `\uFEFF  ${numberFormatPrefix}  `,
  `**${numberFormatPrefix}**`,
  numberFormatPrefix.replace('Định dạng số:', '**Định dạng số:**'),
  `> ${numberFormatPrefix}`,
  `- ${numberFormatPrefix}`,
  '### Số trong báo cáo: dấu phẩy tách hàng nghìn, dấu chấm cho phần thập phân.',
])
  assert.equal(businessReportText(`${wrapper}\n\nCâu trả lời.`), 'Câu trả lời.');
assert.equal(
  businessReportText('Định dạng số: nội dung do người dùng viết.'),
  'Định dạng số: nội dung do người dùng viết.',
);
for (const authored of [
  `The old answer said “${numberFormatPrefix}” and the user asked us to remove it.`,
  `| Source note | Value |\n|---|---|\n| ${numberFormatPrefix} | 12 |`,
  `~~~text\n${numberFormatPrefix}\n~~~`,
  `Authored example:\n\n    ${numberFormatPrefix}\n\nKeep this paragraph.`,
  `Authored example:\n\n\t${numberFormatPrefix}\n\nKeep this paragraph.`,
  `Authored example:\n\n    first code line\n\n  \t${numberFormatPrefix}\n\nKeep this paragraph.`,
  `Authored example:\n\n>     ${numberFormatPrefix}\n\nKeep this paragraph.`,
  `Authored example:\n\n> \t${numberFormatPrefix}\n\nKeep this paragraph.`,
  `Authored example:\n\n> >     ${numberFormatPrefix}\n\nKeep this paragraph.`,
  `> ~~~text\n> ${numberFormatPrefix}\n> ~~~`,
  `  >  ~~~text\n  > ${numberFormatPrefix}\n  > ~~~`,
  `~~~text\n> ~~~\n${numberFormatPrefix}\n~~~`,
  `~~~text\n~~~not-a-close\n${numberFormatPrefix}\n~~~`,
  `~~~text\n    ~~~\n${numberFormatPrefix}\n~~~`,
  `${numberFormatPrefix} Đây là ví dụ cần phân tích.`,
  'Giá trị null là chưa biết chi phí, không phải chi phí bằng 0.',
])
  assert.equal(businessReportText(authored), authored, 'Meaningful authored source text survives');
assert.equal(
  businessReportText(
    `    ~~~\n    ${numberFormatPrefix}\n    ~~~\n\n${numberFormatPrefix}\n\nKeep this paragraph.`,
  ),
  `    ~~~\n    ${numberFormatPrefix}\n    ~~~\n\n\nKeep this paragraph.`,
  'Indented fences remain code while a following standalone app note is removed',
);
assert.equal(
  businessReportText(
    `> ~~~text\n> ${numberFormatPrefix}\n\n${numberFormatPrefix}\n\nKeep this paragraph.`,
  ),
  `> ~~~text\n> ${numberFormatPrefix}\n\n\nKeep this paragraph.`,
  'A quoted fence cannot hide an ordinary app note outside that quote',
);
const prefixedReport = event('message', {
  role: 'assistant',
  text: `${numberFormatPrefix}\n\nChào bạn!`,
  claims: [],
  qa: { verified: true },
});
const prefixedReportSnapshot = JSON.stringify(prefixedReport);
const prefixedReportHtml = renderToStaticMarkup(<EventView event={prefixedReport} />);
assert.match(prefixedReportHtml, /Chào bạn!/);
assert.doesNotMatch(prefixedReportHtml, /Định dạng số:/);
assert.equal(JSON.stringify(prefixedReport), prefixedReportSnapshot);

const explorationRows = [
  <tr key="line-one">
    <td>Đường dây 1</td>
    <td>
      00123 <a href="#citation:1:private-source-id">[1]</a>
    </td>
  </tr>,
  <tr key="line-two">
    <td>Đường dây 2</td>
    <td>12 [.*]</td>
  </tr>,
];
assert.equal(filterReportRows(explorationRows, '  '), explorationRows);
assert.deepEqual(filterReportRows(explorationRows, 'duong [.*]'), [explorationRows[1]]);
assert.deepEqual(filterReportRows(explorationRows, 'ĐƯỜNG 00123'), [explorationRows[0]]);
assert.deepEqual(filterReportRows(explorationRows, '[.*]'), [explorationRows[1]]);
assert.deepEqual(filterReportRows(explorationRows, 'private-source-id'), []);
assert.deepEqual(filterReportRows(explorationRows, 'unmatched'), []);

const explorationReport = event('message', {
  role: 'assistant',
  language: 'vi',
  text: [
    '| Line | Events |',
    '|---|---:|',
    '| Đường dây 1 | 12 [claim:analysis.rows[0].value] |',
    ...Array.from({ length: 6 }, (_, index) => `| Line ${index + 2} | ${index} |`),
    '',
    'Counts are logged events, not distinct defective units.',
  ].join('\n'),
  claims: report.data.claims,
});
const explorationSnapshot = JSON.stringify(explorationReport);
const explorationHtml = renderToStaticMarkup(
  <I18nProvider initialLocale="en" persist={false}>
    <EventView event={explorationReport} />
  </I18nProvider>,
);
assert.match(explorationHtml, /Tìm trong bảng/);
assert.match(explorationHtml, /7 dòng/);
assert.match(explorationHtml, /type="search"/);
assert.match(explorationHtml, /aria-controls="[^"]+-table"/);
assert.match(explorationHtml, /aria-live="polite"/);
assert.match(explorationHtml, /Bằng chứng đối chiếu/);
assert.match(explorationHtml, /not distinct defective units/);
assert.match(explorationHtml, /lang="vi"/);
assert.doesNotMatch(explorationHtml, /Find in table/);
const explicitEnglish = renderToStaticMarkup(<EventView event={explorationReport} language="en" />);
assert.match(explicitEnglish, /Find in table/);
assert.match(explicitEnglish, /7 rows/);
assert.match(explicitEnglish, /Evidence reference/);
assert.doesNotMatch(html, /type="search"/, 'Small tables retain the simple answer presentation');
const userTable = renderToStaticMarkup(
  <EventView event={{ ...explorationReport, data: { ...explorationReport.data, role: 'user' } }} />,
);
assert.doesNotMatch(userTable, /type="search"/, 'User-authored tables are not adaptive answer UI');
assert.equal(JSON.stringify(explorationReport), explorationSnapshot);

const tableId = '0123456789abcdef01234567';
const tableStart = `<!-- evidence-table:${tableId}:start -->`;
const tableEnd = `<!-- evidence-table:${tableId}:end -->`;
const boundTableMarkdown = [
  tableStart,
  '| source&#95;label | source&#95;value |',
  '| --- | --- |',
  '| 00123 | 12 [claim:table-source.rows[0].value] |',
  '| 2026&#45;01&#45;02 | 0 [claim:table-source.rows[1].value] |',
  '| A&#124;B &#38; C | null |',
  '| &#91;claim&#58;source&#46;rows&#91;0&#93;&#46;value&#93; | false |',
  '| &#91;value&#58;source&#46;rows&#91;0&#93;&#46;value&#93; | source text |',
  '| &#91;table&#58;ffffffffffffffffffffffff&#93; | source token |',
  '| &#60;&#33;&#45;&#45; evidence&#45;table&#58;ffffffffffffffffffffffff&#58;start &#45;&#45;&#62; | source marker |',
  '| &#91;click&#93;&#40;https&#58;&#47;&#47;example&#46;org&#47;unsafe&#41; | source link |',
  '| https&#58;&#47;&#47;example&#46;org&#47;raw&#45;data | source URL |',
  '| https&#58;&#47;&#47;example&#46;org&#47;evidence&#46;json | source technical URL |',
  '| user&#64;example&#46;org | source email |',
  '| www&#46;example&#46;org | source domain |',
  '| &#60;script&#62;alert&#40;1&#41;&#60;&#47;script&#62; | source HTML |',
  '| &#38;&#35;91&#59;claim&#38;&#35;58&#59;literal&#38;&#35;93&#59; | source entity |',
  '| Dữ liệu&#124;gốc | giữ nguyên |',
  tableEnd,
].join('\n');
assert.equal(
  businessReportText(`${tableStart}\nBody\n${tableEnd}`),
  `${tableStart}\nBody\n${tableEnd}`,
  'Preparse cleanup leaves table markers for the source-aware Markdown projection',
);
assert.equal(
  businessReportText(`${tableStart}\r\nBody\r\n${tableEnd}\r\n`),
  `${tableStart}\r\nBody\r\n${tableEnd}\r\n`,
  'Preparse cleanup preserves CRLF source offsets for Markdown projection',
);
for (const sourceText of [
  `Source ${tableStart}`,
  `${tableEnd} source`,
  ` ${tableStart}`,
  `${tableEnd} `,
  tableStart.replace(tableId, tableId.toUpperCase()),
  tableStart.replace(tableId, tableId.slice(1)),
  tableStart.replace('<!-- ', '<!--'),
  tableStart.replace(':start', ':other'),
])
  assert.equal(
    businessReportText(sourceText),
    sourceText,
    'Inline or non-contract table-marker text is not stripped',
  );
const boundTableReport = event('message', {
  role: 'assistant',
  text: boundTableMarkdown,
  qa: { verified: true },
});
const boundTableSnapshot = JSON.stringify(boundTableReport);
for (const locale of ['en', 'vi'] as const) {
  const tableHtml = renderToStaticMarkup(
    <I18nProvider initialLocale={locale} persist={false}>
      <EventView event={boundTableReport} />
    </I18nProvider>,
  );
  assert.equal(
    (tableHtml.match(/<table(?:\s[^>]*)?>/g) || []).length,
    1,
    'The annotated result remains a GFM table',
  );
  assert.match(
    tableHtml,
    locale === 'vi' ? /aria-label="Bảng số liệu báo cáo"/ : /aria-label="Report data table"/,
  );
  for (const cell of [
    '00123',
    '2026-01-02',
    'A|B &amp; C',
    'null',
    'false',
    '[claim:source.rows[0].value]',
    '[value:source.rows[0].value]',
    '[table:ffffffffffffffffffffffff]',
    'https://example.org/raw-data',
    'https://example.org/evidence.json',
    'user@example.org',
    'www.example.org',
    'Dữ liệu|gốc',
  ]) {
    assert.equal(
      tableHtml.includes(`<td>${cell}</td>`),
      true,
      `Source table cell stays literal: ${cell}`,
    );
  }
  assert.match(
    tableHtml,
    /<td>12\s*<span class="citation-badge-wrapper/,
    'Source table numeric cell includes citation badge',
  );
  assert.match(
    tableHtml,
    /<td>0\s*<span class="citation-badge-wrapper/,
    'Source table zero numeric cell includes citation badge',
  );
  assert.match(
    tableHtml,
    /&lt;!-- evidence-table:ffffffffffffffffffffffff:start --&gt;/,
    'An encoded source marker remains visible data',
  );
  assert.match(
    tableHtml,
    /<td>\[click\]\(https:\/\/example\.org\/unsafe\)<\/td>/,
    'Encoded Markdown punctuation does not create a link',
  );
  assert.match(
    tableHtml,
    /&lt;script&gt;alert\(1\)&lt;\/script&gt;/,
    'Source HTML remains inert text',
  );
  assert.match(
    tableHtml,
    /&amp;#91;claim&amp;#58;literal&amp;#93;/,
    'Source entities are not recursively decoded',
  );
  assert.doesNotMatch(
    tableHtml,
    /0123456789abcdef01234567|table-source\.rows|<script>|href=/,
    'Issued annotations stay internal and source strings gain no active markup',
  );
}
assert.equal(
  JSON.stringify(boundTableReport),
  boundTableSnapshot,
  'Table display leaves the raw annotated report and QA unchanged',
);
assert.deepEqual(
  projectConversation([JSON.parse(boundTableSnapshot)]),
  projectConversation([boundTableReport]),
  'Annotated table history replays without rewriting the stored report',
);
const authoredLinkReport = event('message', {
  role: 'assistant',
  text: 'Source: https://example.org/normal\n\n[Source publication](https://example.org/publication)\n\n| Source | Link |\n| --- | --- |\n| Citation | [Table citation](https://example.org/table) |',
});
for (const locale of ['en', 'vi'] as const) {
  const authoredLinkHtml = renderToStaticMarkup(
    <I18nProvider initialLocale={locale} persist={false}>
      <EventView event={authoredLinkReport} />
    </I18nProvider>,
  );
  for (const path of ['normal', 'publication', 'table']) {
    assert.equal(
      authoredLinkHtml.includes(`href="https://example.org/${path}"`),
      true,
      'Authored bare URLs and explicit citations keep their real source positions and remain clickable',
    );
  }
}

const chartProjection = projectEvents([...history, report]);
assert.equal(
  chartProjection.filter((e) => e.type === 'artifact').length,
  0,
  'An embedded chart is not shown twice',
);
const managerView = projectConversation([...history, report]);
assert.equal(
  managerView.filter((e) => e.type === 'activity').length,
  1,
  'One activity disclosure per run',
);
assert.equal(managerView.find((e) => e.type === 'activity')?.data.state, 'completed');
for (const locale of ['en', 'vi'] as const) {
  const activityHtml = renderToStaticMarkup(
    <I18nProvider initialLocale={locale} persist={false}>
      <EventView event={managerView.find((e) => e.type === 'activity')!} />
    </I18nProvider>,
  );
  const status = createI18n(locale).t('ui.analysis_ready');
  const disclosure = createI18n(locale).t('ui.activity');
  assert.ok(
    activityHtml.includes(`<summary aria-label="${status}. ${disclosure}">`),
    'Compact activity keeps an explicit localized accessible name when mobile hides its secondary label',
  );
  assert.ok(
    activityHtml.includes(`<span class="activity-status">${status}</span>`),
    'The lifecycle state remains ordinary visible summary text',
  );
}
assert.equal(
  managerView.some((e) =>
    ['reasoning', 'todo', 'qa', 'usage', 'analysis_context', 'memory'].includes(e.type),
  ),
  false,
);
assert.equal(
  managerView.some((entry) => entry.type === 'artifact_group'),
  false,
  'Agent exports never enter the conversation',
);
assert.equal(JSON.stringify(history), snapshot, 'Manager projection never changes stored history');
const chartOnly = projectConversation([
  ...history,
  event('message', { role: 'assistant', text: 'The monthly chart is below.' }),
]);
assert.equal(
  chartOnly.filter((item) => item.type === 'artifact').length,
  1,
  'A chart not embedded in report prose remains visible as a preview',
);
assert.equal(
  chartOnly.findIndex((item) => item.type === 'artifact') >
    chartOnly.findIndex((item) => item.type === 'message'),
  true,
  'A chart preview follows the business answer',
);

// Historical files stay internal; only selected, non-stale charts remain visible.
const deliveryRun = 'delivery-run';
const chart = {
  path: 'artifacts/selected_chart.png',
  name: 'selected_chart.png',
  sha256: 'd'.repeat(64),
  size_bytes: 240,
  mime_type: 'image/png',
  url: '/api/sessions/fixture/runs/delivery-run/artifacts/artifacts/selected_chart.png',
};
const deliveryStart = event('run_started', { artifact_delivery_version: 1 }, deliveryRun);
const chartEvent = event(
  'artifact',
  { ...chart, size: 240, mime: 'image/png', delivery_candidate: true },
  deliveryRun,
);
const packet = (artifacts: unknown[]) => ({
  version: 1,
  artifacts,
  source_execution_ids: ['receipt'],
  scope: null,
});
const chartReport = (delivery: unknown, body = `![Selected chart](${chart.url})`) =>
  event('message', { role: 'assistant', text: body, delivery }, deliveryRun);
const renderConversation = (items: WorkspaceEvent[], locale: 'en' | 'vi' = 'en') =>
  renderToStaticMarkup(
    <I18nProvider initialLocale={locale} persist={false}>
      {projectConversation(items).map((item) => (
        <EventView event={item} key={String(item.id)} />
      ))}
    </I18nProvider>,
  );
const chartHistory = [deliveryStart, chartEvent, chartReport(packet([chart]))];
const frozenChartHistory = JSON.stringify(chartHistory);
assert.match(renderConversation(chartHistory), /src="[^"]*selected_chart\.png"/);
assert.equal((renderConversation(chartHistory).match(/<img /g) || []).length, 1);
assert.match(
  renderConversation([
    deliveryStart,
    chartEvent,
    chartReport(packet([chart]), `[![Selected chart](${chart.url})](${chart.url})`),
  ]),
  /<img /,
  'Removing a chart download wrapper preserves its analytical image',
);
assert.equal(
  JSON.stringify(chartHistory),
  frozenChartHistory,
  'Chart replay never mutates source receipts',
);
assert.doesNotMatch(
  renderConversation([deliveryStart, chartEvent]),
  /<img /,
  'Unselected streaming charts stay internal',
);
assert.doesNotMatch(
  renderConversation([deliveryStart, chartEvent, chartReport(packet([]))]),
  /<img /,
);
assert.doesNotMatch(
  renderConversation([deliveryStart, chartEvent, chartReport({ version: 2 })]),
  /<img /,
);
assert.doesNotMatch(
  renderConversation([
    deliveryStart,
    { ...chartEvent, data: { ...chartEvent.data, sha256: 'e'.repeat(64) } },
    chartReport(packet([chart])),
  ]),
  /<img /,
  'Replaced chart bytes cannot reuse a stale selection',
);
assert.doesNotMatch(
  renderConversation([
    deliveryStart,
    chartEvent,
    chartReport(
      packet([{ ...chart, url: chart.url.replace('/runs/delivery-run/', '/runs/foreign/') }]),
    ),
  ]),
  /<img /,
  'Selected charts stay bound to their owning run',
);
const standaloneChart = renderConversation([
  deliveryStart,
  chartEvent,
  chartReport(packet([chart]), 'The chart is below.'),
]);
assert.match(standaloneChart, /class="agent-chart"/);
assert.doesNotMatch(standaloneChart, /<a |download|business-file/);
assert.equal(
  chartArtifact(event('artifact', { name: 'chart.png', url: 'https://foreign.example/chart.png' })),
  false,
);
for (const locale of ['en', 'vi'] as const) {
  for (const path of [
    'DATA_QUALITY.xlsx',
    'data.csv',
    'brief.md',
    'report.html',
    'report.pdf',
    'evidence.zip',
    'Chất lượng 50% #1.csv',
  ]) {
    const url =
      '/api/sessions/fixture/runs/delivery-run/artifacts/artifacts/' + encodeURIComponent(path);
    const exportEvent = event(
      'artifact',
      { name: path, path: 'artifacts/' + path, url, size: 240, audience: 'business' },
      deliveryRun,
    );
    const reportWithLinks = event(
      'message',
      {
        role: 'assistant',
        text: `Analytical result: **13 events**.\n\n[Download file](${url})\n\n${url}\n\n[Source](https://example.org/publication)`,
      },
      deliveryRun,
    );
    const historyWithExports = [
      deliveryStart,
      exportEvent,
      event('artifact_group', { items: [exportEvent] }, deliveryRun),
      reportWithLinks,
    ];
    const before = JSON.stringify(historyWithExports);
    const markup = renderConversation(historyWithExports, locale);
    assert.match(markup, /13 events/);
    assert.match(markup, /href="https:\/\/example.org\/publication"/);
    assert.doesNotMatch(
      markup,
      /<a [^>]*(?:\/api\/|download)|Take the results with you|Mang kết quả|business-file|business-files/,
    );
    assert.doesNotMatch(
      markup,
      /Download file|\/api\/sessions\/fixture\/runs\/delivery-run\/artifacts\//,
      'Historical export URLs and labels are absent from report prose',
    );
    assert.equal(JSON.stringify(historyWithExports), before);
    assert.equal(renderToStaticMarkup(<EventView event={exportEvent} />), '');
    assert.equal(
      renderToStaticMarkup(<EventView event={event('artifact_group', { items: [exportEvent] })} />),
      '',
    );
  }
}

for (const hidden of [
  'reasoning',
  'memory',
  'qa',
  'usage',
  'analysis_context',
  'run_started',
  'unknown_event',
]) {
  const output = renderToStaticMarkup(
    <EventView
      event={event(hidden, {
        text: 'PRIVATE_TRACE',
        execution_id: 'PRIVATE_TRACE',
        cost_usd: 12,
        input_tokens: 5000,
        issues: ['PRIVATE_TRACE'],
      })}
    />,
  );
  assert.equal(output, '', hidden + ' stays out of the manager conversation');
}
const toolHtml = renderToStaticMarkup(<EventView event={history[4]} />);
assert.match(toolHtml, /Analyzing the results/);
assert.doesNotMatch(toolHtml, /local_python_repl|exec-a|print\(12\)|bbbbbbbb|<pre>/);
const failureHtml = renderToStaticMarkup(
  <EventView
    event={event('error', {
      message: 'RuntimeError: token abc hidden; local_python_repl failed at C:\\private\\path.py',
    })}
  />,
);
assert.match(failureHtml, /role="alert"/);
assert.doesNotMatch(failureHtml, /abc hidden|local_python_repl|private|RuntimeError/);

for (const locale of ['en', 'vi'] as const) {
  const { t } = createI18n(locale);
  const verificationFailure = event('error', {
    code: 'unverified',
    message: 'Connection to a cost claim could not be verified: PRIVATE_TRACE',
    partial_evidence_retained: true,
  });
  const preservedFailure = JSON.stringify(verificationFailure);
  const markup = renderToStaticMarkup(
    <I18nProvider initialLocale={locale} persist={false}>
      <EventView event={verificationFailure} />
    </I18nProvider>,
  );
  assert.match(markup, /role="alert"/);
  assert.match(
    markup,
    locale === 'en' ? /The result could not be verified/ : /Kết quả chưa được xác minh/,
    'Structured verification failure takes priority over message keyword guesses',
  );
  assert.doesNotMatch(markup, /PRIVATE_TRACE|[Cc]onnection|kết nối|usage limit|ngân sách/);
  assert.equal(JSON.stringify(verificationFailure), preservedFailure);
  assert.equal(
    friendlyError('Unexpected failure', t),
    t('ui.the_analysis_could_not_finish_your_request_and_any_saved_results_'),
  );
  assert.doesNotMatch(friendlyError('Unexpected failure', t), /[Cc]onnection|kết nối/);
  assert.equal(
    friendlyError('Failed to fetch', t),
    t('ui.connection_interrupted_we_are_reconnecting_your_draft_is_kept_her'),
    'A real network failure retains the reconnecting guidance',
  );
}

const reviewHtml = renderToStaticMarkup(
  <EventView
    event={event('message', {
      role: 'assistant',
      text: 'A provisional result.',
      qa: { verified: false },
    })}
  />,
);
assert.match(reviewHtml, /Draft: some figures still need checking/);
assert.doesNotMatch(reviewHtml, /QA verified/);
const stoppedActivity = renderToStaticMarkup(
  <EventView
    event={event('activity', {
      state: 'failed',
      items: [event('tool', { name: 'local_python_repl', status: 'running' })],
    })}
  />,
);
assert.match(stoppedActivity, /Analysis stopped before finishing/);
assert.match(stoppedActivity, /Not confirmed/);
assert.doesNotMatch(
  stoppedActivity,
  /class="spin"/,
  'Stopped activity cannot keep claiming work is running',
);

const vietnameseReport = renderToStaticMarkup(
  <I18nProvider initialLocale="vi" persist={false}>
    <EventView event={report} />
  </I18nProvider>,
);
assert.match(vietnameseReport, /Trợ lý chất lượng/);
assert.match(
  vietnameseReport,
  /<th>Metric<\/th>/,
  'Changing interface locale does not rewrite saved report prose',
);
assert.doesNotMatch(vietnameseReport, /analysis\.rows|exec-a|Bằng chứng số liệu|claim-reference/);
const vietnameseStatus = renderToStaticMarkup(
  <I18nProvider initialLocale="vi" persist={false}>
    <EventView event={event('status', { state: 'completed' })} />
  </I18nProvider>,
);
assert.match(vietnameseStatus, /Đã có kết quả phân tích/);
assert.doesNotMatch(vietnameseStatus, /completed|verified/);
const vietnameseAgent = renderToStaticMarkup(
  <I18nProvider initialLocale="vi" persist={false}>
    <AgentWorkspace
      visible
      contextId="context-fixed-private"
      contextSummary="Tháng 1/2024 đến Tháng 6/2024 · Chuyền 3"
    />
  </I18nProvider>,
);
assert.match(vietnameseAgent, /aria-label="Dữ liệu cho câu hỏi này"/);
assert.doesNotMatch(vietnameseAgent, /Tệp và nguồn dữ liệu|type="file"/);
assert.match(vietnameseAgent, /Tháng 1\/2024 đến Tháng 6\/2024 · Chuyền 3/);
assert.match(vietnameseAgent, /Hiểu xu hướng lỗi/);
assert.match(vietnameseAgent, /Gửi câu hỏi/);
assert.doesNotMatch(
  vietnameseAgent,
  /context-fixed-private|SQL|Python|SHA-256|What do you want|Send prompt/,
);
const englishAgent = renderToStaticMarkup(
  <AgentWorkspace
    visible
    contextId="context-private"
    contextSummary="Jan 2024 to Jun 2024 · Line-3"
  />,
);
assert.match(englishAgent, /Understand the trend/);
assert.equal([...englishAgent.matchAll(/class="agent-suggestion"/g)].length, 3);
assert.match(englishAgent, /class="agent-availability connecting" role="status"/);
assert.match(vietnameseAgent, /Đang kết nối/);
assert.doesNotMatch(englishAgent, /agent-work-mode|agent-context-details|<select/);
assert.doesNotMatch(englishAgent, /Show agent flow|Hide agent flow|agent-flow-toggle/);
assert.doesNotMatch(vietnameseAgent, /Hiện luồng xử lý|Ẩn luồng xử lý|agent-flow-toggle/);
assert.doesNotMatch(
  englishAgent,
  /What should the analyst do|Investigation and handoff brief|Prepare a new action draft|Review saved work|Open follow-up form/,
);
assert.doesNotMatch(
  englishAgent,
  /This scope is saved with your question|Investigate a top priority|AI is unavailable in this environment/,
);
assert.match(englishAgent, /Data for this question/);
assert.doesNotMatch(englishAgent, /context-private/);
assert.doesNotMatch(
  vietnameseAgent,
  /Chỉnh sửa bản nháp hiện có|agent-work-mode|investigation-panel/,
);
assert.equal(
  buildPromptMessage('Create a draft', 'en', 'captured-context').action_intent,
  'none',
  'Prompt wording alone never grants write intent',
);
assert.deepEqual(
  buildPromptMessage('Investigate critical events', 'vi', 'original-context', 'draft_create'),
  {
    type: 'prompt',
    text: 'Investigate critical events',
    language: 'vi',
    analysis_context_id: 'original-context',
    action_intent: 'draft_create',
  },
);
assert.equal(
  buildPromptMessage('Revise the named draft', 'en', 'original-context', 'draft_update')
    .action_intent,
  'draft_update',
);
assert.throws(
  () => buildPromptMessage('Create a draft', 'vi', undefined, 'draft_create'),
  /dashboard context/,
);
assert.throws(
  () => buildPromptMessage('Revise a draft', 'vi', undefined, 'draft_update'),
  /dashboard context/,
);
const activeDetail = buildPromptMessage(
  'Also compare Critical events',
  'vi',
  'original-frozen-context',
  'draft_update',
  'active-analysis',
);
assert.deepEqual(JSON.parse(JSON.stringify(activeDetail)), {
  type: 'prompt',
  text: 'Also compare Critical events',
  language: 'vi',
  analysis_context_id: 'original-frozen-context',
  action_intent: 'draft_update',
  steer_run_id: 'active-analysis',
});
assert.equal(
  Object.hasOwn(
    buildPromptMessage('A new question', 'en', 'new-dashboard-context'),
    'steer_run_id',
  ),
  false,
  'Ordinary prompts retain their separate question contract',
);
assert.equal(
  buildPromptMessage(
    'Keep the active draft scope',
    'en',
    undefined,
    'draft_update',
    'active-analysis',
  ).steer_run_id,
  'active-analysis',
  'A validated target run lets the server restore missing immutable context metadata',
);

const savedReport = event(
  'message',
  { role: 'assistant', text: 'The action draft was saved.' },
  'saved-run',
);
const savedContext = event('analysis_context', { context_id: 'report-context' }, 'saved-run');
const savedCase = { id: 'case-created', context_id: 'report-context', status: 'draft' };
const createReceipt = event(
  'tool',
  { name: 'operations_create_draft', status: 'completed', result: { ok: true, case: savedCase } },
  'saved-run',
);
const updateReceipt = event(
  'tool',
  {
    name: 'operations_update_draft',
    status: 'completed',
    result: { ok: true, case: { ...savedCase, id: 'case-updated' } },
  },
  'saved-run',
);
assert.deepEqual(
  savedWorkForReport([savedContext, createReceipt, savedReport], savedReport, savedContext),
  { caseId: 'case-created', contextId: 'report-context' },
  'A durable create receipt opens the saved case',
);
assert.deepEqual(
  savedWorkForReport([savedContext, updateReceipt, savedReport], savedReport, savedContext),
  { caseId: 'case-updated', contextId: 'report-context' },
  'A durable update receipt opens the existing case',
);
assert.equal(
  savedWorkForReport([savedContext, savedReport], savedReport, savedContext),
  undefined,
  'Report prose alone cannot claim a saved item',
);
assert.equal(savedWorkForReport([createReceipt], undefined, savedContext), undefined);
assert.equal(
  savedWorkForReport([createReceipt], savedReport),
  undefined,
  'A report without a saved scope cannot bind a work item',
);
assert.equal(
  savedWorkForReport([createReceipt], { ...savedReport, run_id: undefined }, savedContext),
  undefined,
);
assert.equal(
  savedWorkForReport([createReceipt], { ...savedReport, data: { role: 'user' } }, savedContext),
  undefined,
);
assert.equal(
  savedWorkForReport([createReceipt], savedReport, { ...savedContext, data: { context_id: ' ' } }),
  undefined,
);
assert.equal(
  savedWorkForReport([createReceipt], savedReport, { ...savedContext, type: 'message' }),
  undefined,
);
assert.equal(
  savedWorkForReport([createReceipt], savedReport, { ...savedContext, run_id: 'older-run' }),
  undefined,
  'A different run cannot supply the report scope',
);
assert.equal(
  savedWorkForReport([createReceipt], savedReport, {
    ...savedContext,
    session_id: 'other-session',
  }),
  undefined,
);
assert.equal(
  savedWorkForReport(
    [{ ...createReceipt, run_id: 'older-run' }, savedReport],
    savedReport,
    savedContext,
  ),
  undefined,
  'An earlier run saving work cannot change this report action',
);
assert.equal(
  savedWorkForReport(
    [{ ...createReceipt, session_id: 'other-session' }],
    savedReport,
    savedContext,
  ),
  undefined,
);

for (const result of [
  null,
  [],
  'saved',
  1,
  {},
  { ok: false, case: savedCase },
  { ok: 'true', case: savedCase },
  { ok: true },
  { ok: true, case: null },
  { ok: true, case: [] },
  { ok: true, case: 'case-created' },
]) {
  assert.equal(
    savedWorkForReport(
      [{ ...createReceipt, data: { ...createReceipt.data, result } }],
      savedReport,
      savedContext,
    ),
    undefined,
    'Malformed or failed results cannot offer an existing-item action',
  );
}
for (const item of [
  { ...savedCase, id: '' },
  { ...savedCase, id: ' ' },
  { ...savedCase, id: 12 },
  { ...savedCase, context_id: 'other-context' },
  { ...savedCase, context_id: null },
  { ...savedCase, status: 'investigating' },
  { ...savedCase, status: undefined },
]) {
  assert.equal(
    savedWorkForReport(
      [{ ...createReceipt, data: { ...createReceipt.data, result: { ok: true, case: item } } }],
      savedReport,
      savedContext,
    ),
    undefined,
    'The saved case must match the report scope and permitted receipt state',
  );
}
for (const invalidEvent of [
  { ...createReceipt, type: 'message' },
  { ...createReceipt, data: { ...createReceipt.data, status: 'running' } },
  { ...createReceipt, data: { ...createReceipt.data, status: 'failed' } },
  { ...createReceipt, data: { ...createReceipt.data, name: 'operations_list_cases' } },
  { ...createReceipt, data: { ...createReceipt.data, name: ['operations_create_draft'] } },
]) {
  assert.equal(
    savedWorkForReport([invalidEvent], savedReport, savedContext),
    undefined,
    'Only a completed draft write can establish a past save',
  );
}
const failedRetry = event(
  'tool',
  { name: 'operations_update_draft', status: 'failed', result: { ok: false, error: 'Conflict' } },
  'saved-run',
);
const savedHistory = [savedContext, createReceipt, updateReceipt, failedRetry, savedReport];
const savedSnapshot = JSON.stringify(savedHistory);
assert.deepEqual(
  savedWorkForReport(savedHistory, savedReport, savedContext),
  { caseId: 'case-updated', contextId: 'report-context' },
  'A later failed retry preserves the latest successful save',
);
const replay = JSON.parse(savedSnapshot) as WorkspaceEvent[];
assert.deepEqual(
  savedWorkForReport(replay, replay.at(-1), replay[0]),
  { caseId: 'case-updated', contextId: 'report-context' },
  'Restored history identifies the same saved work',
);
assert.equal(
  JSON.stringify(savedHistory),
  savedSnapshot,
  'Saved-work lookup does not alter canonical history',
);
for (const state of ['refused', 'incomplete', 'timeout', 'unavailable', 'cancelled']) {
  for (const locale of ['en', 'vi'] as const) {
    const markup = renderToStaticMarkup(
      <I18nProvider initialLocale={locale} persist={false}>
        <EventView event={event('status', { state })} />
      </I18nProvider>,
    );
    assert.match(markup, /is-stopped/);
    assert.doesNotMatch(
      markup,
      /lucide-loader|ui\./,
      `${state} must render as a terminal state in ${locale}`,
    );
  }
}
const viewCommand = parseUiCommand({
  command_id: 'view-1',
  action: 'apply_scope',
  panel: 'records',
  context_id: 'new-context',
  expected_context_id: 'old-context',
  reversible: true,
});
assert.ok(viewCommand);
assert.equal(commandMatchesContext(viewCommand, 'old-context'), true);
assert.equal(
  commandMatchesContext(viewCommand, 'manually-changed-context'),
  false,
  'A command from an earlier view must not overwrite a manual selection',
);
assert.equal(commandMatchesContext(viewCommand, undefined), false);
assert.equal(parseUiCommand({ ...viewCommand, panel: 'arbitrary-path' }), undefined);
assert.equal(parseUiCommand({ ...viewCommand, panel: 'investigation' }), undefined);
assert.equal(parseUiCommand({ ...viewCommand, panel: 'brief' }), undefined);
assert.equal(parseUiCommand({ ...viewCommand, expected_context_id: null }), undefined);
assert.equal(createI18n('vi').number(1090), '1.090');
assert.equal(createI18n('vi').percent(12.5), '12,5%');
assert.equal(createI18n('en').number(1090), '1,090');
assert.equal(createI18n('vi').term('defect_event'), 'sự kiện lỗi');
const scopeLabelFixture = {
  start_month: '2024-01',
  end_month: '2024-06',
  production_line: 'Line-3',
  scenario: 'exclude_cost_10',
};
assert.match(
  createI18n('vi').scope(scopeLabelFixture),
  /Tháng 1\/2024 đến Tháng 6\/2024 · Chuyền 3/,
);
assert.doesNotMatch(createI18n('vi').scope(scopeLabelFixture), /Jan|Jun| to |exclude_cost_10/);
assert.match(createI18n('en').scope(scopeLabelFixture), /Jan 2024 to Jun 2024 · Line-3/);

// Synthetic display metadata exercises the saved-state UI, not analytical expected results.
const sourceUnitNotes = Object.freeze({
  en: 'Cost uses local currency. Coverage is known-cost events / events. Intensity is events per 1,000 reference units. <script>alert(1)</script>',
  vi: 'Chi phí dùng tiền địa phương. Độ phủ là số sự kiện có chi phí / tổng sự kiện. Cường độ là số sự kiện trên 1.000 đơn vị tham chiếu. <img src=x onerror=alert(1)>',
});
const unitNotesSnapshot = JSON.stringify(sourceUnitNotes);
for (const locale of ['en', 'vi'] as const) {
  const noteMarkup = renderToStaticMarkup(
    <I18nProvider initialLocale={locale} persist={false}>
      <InvestigationUnitNotes notes={sourceUnitNotes} />
    </I18nProvider>,
  );
  assert.match(noteMarkup, /<aside[^>]+aria-labelledby="investigation-unit-notes-heading"/);
  assert.match(
    noteMarkup,
    locale === 'en' ? /Units and metric definitions/ : /Đơn vị và cách tính chỉ số/,
  );
  assert.match(noteMarkup, locale === 'en' ? /per 1,000 reference units/ : /trên 1\.000 đơn vị/);
  assert.match(noteMarkup, locale === 'en' ? /&lt;script&gt;/ : /&lt;img src=x/);
  assert.doesNotMatch(
    noteMarkup,
    /<script|<img|<textarea|<input|<select|contenteditable|href=|ui\./,
    'Source unit metadata is escaped plain text with no owner edit control or active markup.',
  );
  assert.doesNotMatch(noteMarkup, locale === 'en' ? /Chi phí dùng/ : /Cost uses/);
  for (const notes of [undefined, {}, { en: '', vi: '' }, { en: ' \n ', vi: '\t' }]) {
    assert.equal(
      renderToStaticMarkup(
        <I18nProvider initialLocale={locale} persist={false}>
          <InvestigationUnitNotes notes={notes} />
        </I18nProvider>,
      ),
      '',
      'Legacy absent/empty metadata renders neither a legend nor a placeholder.',
    );
  }
}
assert.equal(
  renderToStaticMarkup(
    <I18nProvider initialLocale="vi" persist={false}>
      <InvestigationUnitNotes notes={{ en: sourceUnitNotes.en }} />
    </I18nProvider>,
  ),
  '',
  'Missing notes for the selected locale do not leak another locale into the UI.',
);
assert.equal(JSON.stringify(sourceUnitNotes), unitNotesSnapshot);

const savedInvestigation: Investigation = {
  session_id: 'synthetic-investigation',
  version: 7,
  question: 'Compare the selected scope.',
  summary: 'Original authored wording stays intact.',
  context_id: 'saved-context',
  dataset_version: 'source-version',
  policy_version: 'canonical-v2',
  content_status: 'unverified',
  unit_notes: sourceUnitNotes,
  findings: [{ id: 'finding-1', text: 'Check the source first.', status: 'unverified' }],
  hypotheses: [],
  next_steps: [],
  priorities: [],
  artifacts: [],
};
const ownerDraft: Investigation = {
  ...savedInvestigation,
  summary: 'Owner wording remains exactly as typed.',
  unit_notes: { en: 'Owner-supplied metadata must never be sent.', vi: 'Không gửi.' },
  context_id: 'forged-context',
  content_status: 'verified',
  findings: [
    {
      id: 'finding-1',
      text: 'Owner observation, still requiring review.',
      status: 'verified',
      evidence: [{ execution_id: 'forged', path: '$.forged' }],
    },
  ],
};
const savedInvestigationSnapshot = JSON.stringify(savedInvestigation);
const ownerDraftSnapshot = JSON.stringify(ownerDraft);
assert.deepEqual(
  buildInvestigationPatch(ownerDraft, savedInvestigation),
  {
    expected_version: 7,
    summary: ownerDraft.summary,
    findings: [{ id: 'finding-1', text: ownerDraft.findings[0].text }],
  },
  'Owner PATCH preserves authored text/CAS and excludes source units, scope, review status and evidence.',
);
assert.deepEqual(
  buildInvestigationPatch({ ...savedInvestigation, unit_notes: {} }, savedInvestigation),
  { expected_version: 7 },
  'A metadata-only difference is never an owner edit.',
);
assert.equal(
  buildInvestigationPatch(ownerDraft, { ...savedInvestigation, version: 8 }).expected_version,
  7,
  'The expected version remains the owner draft version and cannot skip the stale-edit guard.',
);
assert.equal(JSON.stringify(savedInvestigation), savedInvestigationSnapshot);
assert.equal(JSON.stringify(ownerDraft), ownerDraftSnapshot);

// Verify incoming tool_arguments_delta events are mapped into the live event store
const deltaHistory = [
  event('tool_arguments_delta', {
    call_id: 'call-stream',
    item_id: 'item-stream',
    delta: '{"param":',
  }),
  event('tool_arguments_delta', {
    call_id: 'call-stream',
    item_id: 'item-stream',
    delta: '"value"}',
  }),
];
const projectedDeltas = projectEvents(deltaHistory);
assert.equal(
  projectedDeltas.length,
  1,
  'tool_arguments_delta events are joined in the live event store',
);
assert.equal(projectedDeltas[0].type, 'tool_arguments');
assert.equal(projectedDeltas[0].data.phase, 'delta');
assert.equal(projectedDeltas[0].data.delta, '{"param":"value"}');

// Citation details need an exact, unique identity; cohort metric names and
// repeated JSON paths are not evidence identities.
const sourceClaims = [
  { id: 'claim-A', path: '$.count', metric: 'count', value: 12, execution_id: 'exec-A' },
  { id: 'claim-B', path: '$.count', metric: 'count', value: 19, execution_id: 'exec-B' },
];
assert.equal(resolveNumericClaim(sourceClaims, 'claim-A'), sourceClaims[0]);
assert.equal(resolveNumericClaim(sourceClaims, 'claim-a'), undefined);
assert.equal(resolveNumericClaim(sourceClaims, '$.count'), undefined);
assert.equal(resolveNumericClaim(sourceClaims, 'count'), undefined);
assert.equal(resolveNumericClaim([sourceClaims[0]], '$.count'), sourceClaims[0]);
assert.equal(resolveNumericClaim([sourceClaims[0], sourceClaims[0]], 'claim-A'), undefined);
assert.equal(resolveNumericClaim(undefined, 'missing'), undefined);

const fullArguments = event('tool_arguments', {
  call_id: 'call-stream',
  item_id: 'item-stream',
  phase: 'done',
  arguments: '{"param":"value"}',
});
const finalArguments = projectEvents([
  ...deltaHistory,
  fullArguments,
  event('tool_arguments_delta', { call_id: 'call-stream', item_id: 'item-stream', delta: 'stale' }),
]);
assert.equal(finalArguments[0].data.delta, '{"param":"value"}');
assert.equal(finalArguments[0].data.phase, 'done');

assert.equal(formatEvidenceValue(12.3456, 'en'), '12.3456');
assert.equal(formatEvidenceValue(0.00000003, 'en'), '0.00000003');
assert.equal(formatEvidenceValue(0.00000003, 'vi'), '0,00000003');
assert.equal(formatEvidenceValue(0, 'vi'), '0');
assert.equal(formatEvidenceValue(undefined, 'en'), undefined);
const malformedReference = event('message', {
  role: 'assistant',
  text: '[Reference](#citation:1:%E0%A4%A)',
});
assert.doesNotThrow(() => renderToStaticMarkup(<EventView event={malformedReference} />));

// These assertions check viewport-placement arithmetic only. Actual fixed
// containing blocks, table clipping and rendered bounds require browser replay.
assert.deepEqual(
  citationPosition(
    { left: 310, top: 20, bottom: 38 },
    { width: 290, height: 288 },
    { left: 0, top: 0, width: 320, height: 568 },
  ),
  { left: 18, top: 46 },
);
assert.deepEqual(
  citationPosition(
    { left: 380, top: 560, bottom: 578 },
    { width: 290, height: 288 },
    { left: 0, top: 0, width: 390, height: 600 },
  ),
  { left: 88, top: 264 },
);
assert.deepEqual(
  citationPosition(
    { left: 220, top: 110, bottom: 128 },
    { width: 176, height: 196 },
    { left: 50, top: 100, width: 200, height: 220 },
  ),
  { left: 62, top: 112 },
);

console.log(
  'Verified lifecycle preservation, business conversation projection, chart selection and replay, suppressed historical exports, safe inline charts, source-table rendering and inert encoded links, clean reports, hidden technical traces, incomplete-result warnings, accessible EN/VI controls, durable same-run saved-work lookup, read-only localized source units, owner PATCH boundaries, and unchanged canonical evidence.',
);
