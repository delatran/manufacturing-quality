import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import OperationsPanel, {
  ComparisonResult,
  OperationEditor,
  operationEditorForReview,
  operationDraftPrefill,
  operationDraftSource,
  operationReportReference,
  operationScopeLabel,
  operationUpdateFromDraft,
  restoreOperationFocus,
  validateOperationDraft,
} from '../src/OperationsPanel';
import { I18nProvider, createI18n } from '../src/i18n';
import type { Locale } from '../src/i18n';
import { initialFilters } from '../src/dashboard-types';
import type {
  OperationCase,
  OperationComparison,
  OperationContext,
  OperationReviewRequest,
} from '../src/operations-types';
import { operationDateIsValid } from '../src/operations-types';
import {
  addOperationNote,
  applyOperationUpdate,
  attachOperationFollowUp,
  checkOperationSource,
  createOperationCase,
  getOperationCase,
  getOperationContext,
  listOperationCases,
  OperationsError,
  transitionOperationCase,
  updateOperationCase,
} from '../src/operations-api';

Object.defineProperty(globalThis, 'React', { value: React });
// Synthetic workflow fixtures test UI semantics, not analytical correctness.
const context: OperationContext = {
  context_id: 'original-context-not-for-display',
  snapshot_id: 'snapshot-not-for-display',
  source_sha256: 'source-hash-not-for-display',
  master_sha256: 'master-hash-not-for-display',
  code_sha256: 'code-hash-not-for-display',
  kpi_version: 'kpi-not-for-display',
  fetched_at: '2026-10-16T00:00:00Z',
  filters: { ...initialFilters, production_line: 'Line-2', severity: 'Critical', product_id: '4' },
};
const comparison: OperationComparison = {
  id: 'comparison-not-for-display',
  checked_at: '2026-10-16T00:00:00Z',
  baseline_context_id: context.context_id,
  current_context_id: context.context_id,
  source_changed: false,
  scope_unchanged: true,
  status: 'unchanged',
  summary: 'Internal summary.',
  metrics: [
    { key: 'event_count', before: 20, after: 20, delta: 0 },
    { key: 'known_repair_cost', before: null, after: null, delta: null },
  ],
  data_changes: { added_events: 0, removed_events: 0, changed_events: 0 },
  coverage: {
    study_start: '2024-01-01',
    study_end: '2024-06-30',
    latest_event_date: '2024-06-28',
    actual_produced_units: null,
    inspected_units: null,
    operating_days: null,
    reporting_complete: null,
  },
  limitations: [],
};
const work: OperationCase = {
  id: 'case-1',
  version: 3,
  title: '<script>alert(1)</script> Kiểm tra riêng',
  description: 'Do not translate this saved description.',
  category: 'investigation',
  priority: 'medium',
  status: 'investigating',
  proposed_owner: 'Minh & QA',
  assigned_owner: null,
  due_date: '2024-07-01',
  action_plan: 'Check the station log.',
  outcome: null,
  created_at: '2026-10-16T00:00:00Z',
  updated_at: '2026-10-16T00:10:00Z',
  resolved_at: null,
  context_id: context.context_id,
  origin_report_id: null,
  context,
  source: {
    kind: 'historical_candidate',
    key: '2024-06-28',
    summary: 'Internal source summary.',
    receipt_path: 'receipt-not-for-display',
  },
  notes: [
    {
      id: 1,
      text: 'Owner-authored note: giữ nguyên.',
      created_at: '2026-10-16T00:10:00Z',
      actor: 'user',
    },
  ],
  history: [
    {
      id: 1,
      at: '2026-10-16T00:10:00Z',
      actor: 'user',
      kind: 'note_added',
      from_status: 'investigating',
      to_status: 'investigating',
      changes: {},
      note: 'Owner-authored note: giữ nguyên.',
    },
  ],
  allowed_transitions: ['action_planned', 'dismissed'],
  is_overdue: true,
  follow_up: comparison,
};
const reviewRequest: OperationReviewRequest = {
  id: 41,
  caseId: work.id,
  contextId: context.context_id,
};
const savedWorkReview = operationEditorForReview(work, reviewRequest);
assert.ok(savedWorkReview, 'A report can review its existing work in the original view.');
assert.equal(
  savedWorkReview.item,
  work,
  'Review retains the fetched case, including its current version and status.',
);
assert.equal(savedWorkReview.context, context);
assert.equal(savedWorkReview.reviewId, reviewRequest.id);
assert.equal(
  savedWorkReview.item?.status,
  'investigating',
  'A historical draft receipt must not reset work to draft.',
);
assert.equal(savedWorkReview.item?.version, 3);
assert.equal(
  savedWorkReview.initialDraft,
  undefined,
  'Review must not create a new prefilled draft.',
);
for (const invalidRequest of [
  { ...reviewRequest, caseId: '' },
  { ...reviewRequest, contextId: '' },
  { ...reviewRequest, caseId: 'another-case' },
  { ...reviewRequest, contextId: 'another-context' },
])
  assert.equal(operationEditorForReview(work, invalidRequest), null);
assert.equal(
  operationEditorForReview({ ...work, context_id: 'wrong-context' }, reviewRequest),
  null,
);
assert.equal(
  operationEditorForReview(
    { ...work, context: { ...context, context_id: 'wrong-nested-context' } },
    reviewRequest,
  ),
  null,
);
const valid = {
  title: 'Review',
  owner: '',
  due: '',
  plan: '',
  status: 'draft' as const,
  note: '',
  outcome: '',
};
assert.deepEqual(
  validateOperationDraft(valid, null),
  {},
  'An owner and deadline may remain unconfirmed.',
);
assert.equal(validateOperationDraft({ ...valid, title: '  ' }, null).title, 'required');
assert.equal(
  validateOperationDraft({ ...valid, status: 'action_planned' }, 'investigating').plan,
  'required',
);
assert.equal(
  validateOperationDraft({ ...valid, status: 'action_planned' }, 'action_planned').plan,
  'required',
);
assert.deepEqual(
  validateOperationDraft({ ...valid, status: 'investigating', plan: '' }, 'action_planned'),
  {},
  'Leaving the planned state may clear an obsolete plan.',
);
assert.equal(
  validateOperationDraft({ ...valid, status: 'resolved' }, 'action_planned').outcome,
  'required',
);
assert.equal(
  validateOperationDraft({ ...valid, status: 'dismissed' }, 'investigating').note,
  'required',
);
assert.equal(
  validateOperationDraft({ ...valid, status: 'investigating' }, 'resolved').note,
  'required',
);
assert.equal(
  validateOperationDraft({ ...valid, status: 'investigating' }, 'dismissed').note,
  'required',
);
assert.deepEqual(
  validateOperationDraft(
    { ...valid, status: 'resolved', outcome: 'Inspected the log; impact is not yet known.' },
    'action_planned',
  ),
  {},
);
assert.equal(operationDateIsValid('2024-02-29'), true);
assert.equal(operationDateIsValid('2025-02-29'), false);
assert.equal(operationDateIsValid('2024-13-01'), false);
assert.equal(operationDateIsValid('0000-01-01'), false);
assert.equal(operationDateIsValid(''), true);
assert.match(operationScopeLabel(context.filters, createI18n('vi')), /Chuyền 2/);
assert.match(operationScopeLabel(context.filters, createI18n('vi')), /Sản phẩm: 4/);
assert.match(operationScopeLabel(context.filters, createI18n('vi')), /Nghiêm trọng/);

// Probe the actual cleanup helper with the focus states observed across tabs.
const focusDocument = { activeElement: null as HTMLElement | null };
function focusTarget(
  tagName: string,
  options: {
    visible?: boolean;
    disabled?: boolean;
    connected?: boolean;
    acceptsFocus?: boolean;
    hiddenParent?: boolean;
  } = {},
) {
  let focusCalls = 0;
  const target = {
    tagName,
    isConnected: options.connected ?? true,
    ownerDocument: focusDocument,
    getClientRects: () => (options.visible === false ? [] : [{}]),
    matches: () => options.disabled ?? false,
    closest: () => (options.hiddenParent ? {} : null),
    focus: () => {
      focusCalls++;
      if (options.acceptsFocus !== false) focusDocument.activeElement = target;
    },
  } as unknown as HTMLElement;
  return { target, calls: () => focusCalls };
}
const addWork = focusTarget('BUTTON');
const fallbackFocus = () => addWork.target.focus();
for (const tag of ['BODY', 'HTML']) {
  const prior = focusTarget(tag);
  focusDocument.activeElement = prior.target;
  restoreOperationFocus(prior.target, fallbackFocus);
  assert.equal(
    prior.calls(),
    0,
    `${tag} must not suppress the Add work fallback after a report launch.`,
  );
  assert.equal(focusDocument.activeElement, addWork.target);
}
for (const options of [
  { visible: false },
  { hiddenParent: true },
  { disabled: true },
  { connected: false },
  { acceptsFocus: false },
]) {
  const prior = focusTarget('BUTTON', options);
  focusDocument.activeElement = null;
  restoreOperationFocus(prior.target, fallbackFocus);
  assert.equal(
    focusDocument.activeElement,
    addWork.target,
    'Unavailable or ineffective launch controls must use the visible fallback.',
  );
}
const existingRow = focusTarget('BUTTON');
const fallbackCalls = addWork.calls();
restoreOperationFocus(existingRow.target, fallbackFocus);
assert.equal(
  focusDocument.activeElement,
  existingRow.target,
  'Saving or cancelling a list-opened case must keep its original launch target.',
);
assert.equal(addWork.calls(), fallbackCalls);
restoreOperationFocus(null, fallbackFocus);
assert.equal(focusDocument.activeElement, addWork.target);

function render(node: React.ReactNode, locale: Locale = 'en') {
  return renderToStaticMarkup(
    <I18nProvider initialLocale={locale} persist={false}>
      {node}
    </I18nProvider>,
  );
}
const panel = render(<OperationsPanel context={context} />);
assert.match(panel, /Follow-up work/);
assert.match(panel, /Loading follow-up work/);
assert.match(panel, /role="status"/);
assert.doesNotMatch(panel, /source-hash-not-for-display|context-not-for-display/);
const disabledPanel = render(<OperationsPanel context={context} isContextChanging />);
assert.match(disabledPanel, /<button[^>]*disabled=""[^>]*>/);
const itemBeforeRender = JSON.stringify(work);
const callbacks = { onClose: () => {}, onChange: () => {}, onSaved: () => {} };
const reviewed = render(<OperationEditor {...savedWorkReview} {...callbacks} />);
assert.match(reviewed, /Review and update/);
assert.match(reviewed, /Report’s saved scope/);
assert.match(reviewed, /Line: Line-2/);
assert.match(reviewed, /value="investigating" selected=""/);
assert.doesNotMatch(reviewed, /Add to follow-up|Start from|value="draft"/);
const english = render(
  <OperationEditor context={context} item={work} sources={[]} {...callbacks} />,
);
const vietnamese = render(
  <OperationEditor context={context} item={work} sources={[]} {...callbacks} />,
  'vi',
);
for (const html of [english, vietnamese]) {
  assert.match(html, /<dialog[^>]*aria-labelledby=/);
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt; Kiểm tra riêng/);
  assert.doesNotMatch(
    html,
    /<script>|receipt-not-for-display|hash-not-for-display|context-not-for-display|snapshot-not-for-display/,
  );
  assert.match(html, /Owner-authored note: giữ nguyên\./);
  assert.match(html, /Do not translate this saved description\./);
  assert.match(html, /Minh &amp; QA/);
  assert.match(html, /type="date"/);
  assert.doesNotMatch(
    html,
    /value="resolved"/,
    'A direct jump from investigation to resolution is not offered.',
  );
}
assert.match(english, /Proposed owner/);
assert.match(english, /Historical review/);
assert.match(vietnamese, /Đầu mối dự kiến/);
assert.match(vietnamese, /Rà soát dữ liệu lịch sử/);
assert.match(vietnamese, /Xác nhận|xác nhận/);
assert.equal(
  JSON.stringify(work),
  itemBeforeRender,
  'Localization must preserve saved work and history.',
);
const reopened = render(
  <OperationEditor
    context={context}
    item={{
      ...work,
      status: 'resolved',
      outcome: 'Checked records only.',
      allowed_transitions: ['investigating'],
    }}
    sources={[]}
    {...callbacks}
  />,
);
assert.match(reopened, /Closed with outcome/);
assert.match(reopened, /Reopen investigation/);
assert.match(reopened, /Checked records only\./);
assert.match(reopened, /Reopen and save this work before editing its details/);
assert.equal(
  [...reopened.matchAll(/<(?:input|textarea)[^>]*readonly=""/gi)].length,
  4,
  'Closed title, owner, due date and plan stay read-only until reopening is saved.',
);
assert.doesNotMatch(english, /readonly=""/i, 'Active working details remain editable.');
const planned: OperationCase = {
  ...work,
  status: 'action_planned',
  allowed_transitions: ['investigating', 'resolved', 'dismissed'],
};
const changedDraft = {
  title: 'Revised investigation',
  owner: work.proposed_owner!,
  due: work.due_date!,
  plan: '',
  status: 'investigating' as const,
  note: 'The previous action needs rechecking.',
  outcome: '',
};
const combinedUpdate = operationUpdateFromDraft(planned, changedDraft);
assert.deepEqual(
  combinedUpdate,
  {
    changes: { title: 'Revised investigation', action_plan: null },
    status: 'investigating',
    note: 'The previous action needs rechecking.',
  },
  'The target status and plan edit must be sent together.',
);
const closed = { ...work, status: 'resolved' as const, outcome: 'Previous result.' };
const reopenDraft = {
  title: work.title,
  owner: work.proposed_owner!,
  due: work.due_date!,
  plan: work.action_plan!,
  status: 'investigating' as const,
  note: 'Recheck after new information.',
  outcome: closed.outcome,
};
assert.deepEqual(
  operationUpdateFromDraft(closed, reopenDraft),
  { changes: {}, status: 'investigating', note: 'Recheck after new information.' },
  'Reopening preserves working details and does not resend a previous outcome.',
);
const prefilled = render(
  <OperationEditor
    context={context}
    item={null}
    sources={[]}
    initialDraft={{
      id: 1,
      title: 'From the report',
      detail: 'Original report text.',
      contextId: context.context_id,
    }}
    {...callbacks}
  />,
);
assert.match(prefilled, /value="From the report"/);
assert.match(prefilled, /Original report text\./);
assert.match(prefilled, /Report’s saved scope/);
assert.match(prefilled, /<details class="ops-reference"><summary>Analysis context<\/summary>/);
assert.equal(
  [...prefilled.matchAll(/<textarea[^>]*>([\s\S]*?)<\/textarea>/g)][0]?.[1],
  '',
  'A report is reference material, not an actionable next step.',
);
const explicitPlan = render(
  <OperationEditor
    context={context}
    item={null}
    sources={[]}
    initialDraft={{
      id: 2,
      title: 'From a selected action',
      detail: 'Report background.',
      actionPlan: 'Inspect three selected records with QA.',
      contextId: context.context_id,
    }}
    {...callbacks}
  />,
);
assert.equal(
  [...explicitPlan.matchAll(/<textarea[^>]*>([\s\S]*?)<\/textarea>/g)][0]?.[1],
  'Inspect three selected records with QA.',
);
assert.deepEqual(operationReportReference('Complete short report.', createI18n('en').t), {
  text: 'Complete short report.',
  shortened: false,
});
for (const locale of ['en', 'vi'] as const) {
  const reference = operationReportReference(
    'A long report line with Unicode 🧪 and numbers 1,234.\n'.repeat(150),
    createI18n(locale).t,
  );
  assert.equal(reference.shortened, true);
  assert.ok(reference.text.length <= 4000);
  assert.match(
    reference.text,
    locale === 'en' ? /Excerpt from the analysis report/ : /Trích đoạn báo cáo phân tích/,
  );
  assert.match(reference.text, /1,234/);
  assert.ok(reference.text.endsWith('…'));
  assert.doesNotMatch(reference.text, /[\uD800-\uDBFF](?![\uDC00-\uDFFF])/);
}
const unchanged = render(<ComparisonResult comparison={comparison} />);
assert.match(unchanged, /The source is unchanged/);
assert.match(unchanged, /no new data here to assess the outcome/);
assert.match(unchanged, /Unavailable/);
assert.match(unchanged, /Latest recorded date in this view:/);
assert.doesNotMatch(unchanged, /Latest event in the checked source:/);
assert.doesNotMatch(unchanged, /comparison-not-for-display|context-not-for-display|NaN|Infinity/);
const unavailable = render(
  <ComparisonResult
    comparison={{
      ...comparison,
      status: 'unavailable',
      source_changed: null,
      current_context_id: null,
      metrics: [],
    }}
  />,
  'vi',
);
assert.match(unavailable, /Chưa kiểm tra được nguồn/);

const requests: { url: string; method: string; body?: Record<string, unknown> }[] = [];
const savedFetch = globalThis.fetch;
try {
  globalThis.fetch = (async (url: string | URL | Request, init?: RequestInit) => {
    requests.push({
      url: String(url),
      method: init?.method ?? 'GET',
      body: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined,
    });
    return new Response(
      JSON.stringify(
        String(url).endsWith('/cases') && !init?.method ? { cases: [work], total: 1 } : work,
      ),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    );
  }) as typeof fetch;
  assert.equal((await listOperationCases()).total, 1);
  await getOperationContext('report/context');
  await createOperationCase({
    title: 'Draft',
    context_id: 'original',
    description: 'Report background.',
    origin_report_id: 'saved-report-123',
    action_plan: null,
    source: { kind: 'recommendation', key: '2' },
    idempotency_key: 'stable-request-1',
  });
  await updateOperationCase('case/one', 3, { action_plan: 'Check the log.' });
  await addOperationNote('case/one', 4, 'Observed only.');
  await transitionOperationCase('case/one', 5, 'resolved', 'Reviewed.', 'Impact remains unknown.');
  await checkOperationSource('original');
  await attachOperationFollowUp('case/one', 6, 'new-context');
  const beforeAtomic = requests.length;
  await applyOperationUpdate('case/one', 7, combinedUpdate);
  assert.equal(
    requests.length - beforeAtomic,
    1,
    'One save sends one atomic update rather than patching before transitioning.',
  );
  const beforeReview = requests.length;
  const reviewedCase = await getOperationCase('existing/case');
  assert.equal(requests.length - beforeReview, 1, 'Review fetches the existing case in one read.');
  assert.equal(reviewedCase.id, work.id);
  assert.equal(requests[1].url, '/api/dashboard/contexts/report%2Fcontext');
  assert.equal(requests[2].body?.context_id, 'original');
  assert.equal(requests[2].body?.idempotency_key, 'stable-request-1');
  assert.equal(requests[2].body?.description, 'Report background.');
  assert.equal(requests[2].body?.origin_report_id, 'saved-report-123');
  assert.equal(requests[2].body?.action_plan, null);
  assert.deepEqual(requests[2].body?.source, { kind: 'recommendation', key: '2' });
  assert.equal(requests[3].url, '/api/operations/cases/case%2Fone');
  assert.equal(requests[3].method, 'PATCH');
  assert.equal(requests[3].body?.expected_version, 3);
  assert.equal(requests[4].body?.expected_version, 4);
  assert.equal(requests[5].body?.confirmed, true);
  assert.equal(requests[5].body?.outcome, 'Impact remains unknown.');
  assert.deepEqual(requests[6].body, { context_id: 'original' });
  assert.deepEqual(requests[7].body, {
    expected_version: 6,
    context_id: 'new-context',
    confirmed: true,
  });
  assert.equal(requests[8].url, '/api/operations/cases/case%2Fone/updates');
  assert.equal(requests[8].method, 'POST');
  assert.deepEqual(requests[8].body, { ...combinedUpdate, expected_version: 7, confirmed: true });
  assert.deepEqual(
    requests[9],
    { url: '/api/operations/cases/existing%2Fcase', method: 'GET', body: undefined },
    'Opening saved work must not mutate or create a case.',
  );
  globalThis.fetch = (async () =>
    new Response(JSON.stringify({ detail: 'Updated by another editor.' }), {
      status: 409,
    })) as typeof fetch;
  await assert.rejects(
    updateOperationCase('one', 1, { title: 'Change' }),
    (error: unknown) => error instanceof OperationsError && error.status === 409,
  );
  await assert.rejects(
    applyOperationUpdate('one', 1, combinedUpdate),
    (error: unknown) => error instanceof OperationsError && error.status === 409,
  );
} finally {
  globalThis.fetch = savedFetch;
}
console.log(
  'Verified follow-up required fields, closed-detail protection, atomic status and detail updates, review of existing work in its original context, locale preservation, escaping, historical wording, unavailable values and versioned API writes.',
);

// Priority quick drafts must retain structured ownership and recommendation identity.
const priorityQuickDraft = {
  id: 731,
  title: 'Review component defects',
  detail: 'Historical finding',
  actionPlan: 'Review representative records',
  proposedOwner: 'Quality Engineer',
  contextId: context.context_id,
  source: { kind: 'recommendation' as const, key: '1' },
};
assert.deepEqual(operationDraftPrefill(priorityQuickDraft), {
  title: priorityQuickDraft.title,
  plan: priorityQuickDraft.actionPlan,
  owner: priorityQuickDraft.proposedOwner,
});
assert.deepEqual(operationDraftSource(priorityQuickDraft), { kind: 'recommendation', key: '1' });
const agentQuickDraft = { id: 732, title: 'Agent finding', detail: 'Report excerpt' };
assert.deepEqual(operationDraftSource(agentQuickDraft), { kind: 'manual' });
assert.equal(operationDraftPrefill(agentQuickDraft).owner, '');
const priorityQuickEditor = render(
  <OperationEditor
    context={context}
    item={null}
    sources={[]}
    initialDraft={priorityQuickDraft}
    {...callbacks}
  />,
);
assert(
  priorityQuickEditor.includes('value="Quality Engineer"'),
  'quick follow-up prefills the editable proposed owner',
);
