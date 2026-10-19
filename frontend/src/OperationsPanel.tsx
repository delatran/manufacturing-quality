import { useEffect, useId, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import {
  AlertCircle,
  ArrowUpRight,
  Check,
  ChevronRight,
  ClipboardList,
  LoaderCircle,
  Plus,
  RotateCcw,
  X,
} from 'lucide-react';
import type { DashboardData, Filters } from './dashboard-types';
import { useI18n } from './i18n';
import {
  applyOperationUpdate,
  attachOperationFollowUp,
  checkOperationSource,
  createOperationCase,
  getOperationCase,
  getOperationContext,
  listOperationCases,
  OperationsError,
} from './operations-api';
import type {
  OperationCase,
  OperationChanges,
  OperationComparison,
  OperationContext,
  OperationDraftRequest,
  OperationReviewRequest,
  OperationSource,
  OperationStatus,
  OperationUpdate,
} from './operations-types';
import { operationDateIsValid, operationIsClosed } from './operations-types';

type Recommendation = DashboardData['analysis']['recommendations'][number];
type Candidate = DashboardData['analysis']['anomalies']['candidates'][number];
type Language = ReturnType<typeof useI18n>;
type SourceOption = {
  value: string;
  source: OperationSource;
  title: string;
  owner: string;
  plan: string;
  tracked?: boolean;
};
type WorkDraft = {
  title: string;
  owner: string;
  due: string;
  plan: string;
  status: OperationStatus;
  note: string;
  outcome: string;
};
type EditorState = {
  context: OperationContext;
  item: OperationCase | null;
  sources: SourceOption[];
  initialDraft?: OperationDraftRequest;
  reviewId?: number;
};

export function operationStatusLabel(status: OperationStatus, t: Language['t']) {
  const labels: Record<OperationStatus, [string, string]> = {
    draft: ['Draft', 'Bản nháp'],
    acknowledged: ['Acknowledged', 'Đã ghi nhận'],
    investigating: ['Investigating', 'Đang rà soát'],
    action_planned: ['Action planned', 'Đã có kế hoạch'],
    resolved: ['Closed with outcome', 'Đã đóng, có kết quả'],
    dismissed: ['Set aside', 'Tạm ngừng theo dõi'],
  };
  return t(...labels[status]);
}

export function operationScopeLabel(filters: Filters, language: Language) {
  const { t, month, label } = language;
  const period =
    filters.start_month === filters.end_month
      ? month(filters.start_month)
      : `${month(filters.start_month)} – ${month(filters.end_month)}`;
  const fields: [keyof Filters, string][] = [
    ['production_line', t('ui.line')],
    ['category', t('ui.category')],
    ['product_id', t('ui.product_2')],
    ['severity', t('ui.severity')],
    ['defect_type', t('ui.type')],
    ['defect_location', t('ui.location')],
    ['inspection_method', t('ui.inspection')],
  ];
  const scope = fields
    .filter(([key]) => filters[key])
    .map(([key, name]) => `${name}: ${key === 'product_id' ? filters[key] : label(filters[key])}`);
  return [period, ...(scope.length ? scope : [t('ui.all_production_lines')])].join(' · ');
}

function recommendationOption(item: Recommendation, language: Language): SourceOption {
  const { t, label, locale } = language;
  let title = item.title;
  let owner = item.owner;
  let plan = item.action;
  if (locale === 'vi') {
    const location = item.title.match(/^Investigate Critical defects at (.+)$/);
    const line = item.title.match(/^Validate the burden on (.+)$/);
    const cost = item.title.match(/^Review (.+) repair burden and cost capture$/);
    const category = item.title.match(/^Investigate high-intensity (.+) products$/);
    const lineCost = item.title.match(/^Investigate repair cost per event on (.+)$/);
    if (location) {
      title = `Rà soát lỗi nghiêm trọng tại ${label(location[1])}`;
      owner = 'Kỹ sư chất lượng và trưởng nhóm sản xuất liên quan';
      plan =
        'Cùng nhân viên vận hành xem lại các bản ghi lỗi, kiểm tra mẫu lỗi đại diện và xác minh nguyên nhân trước khi chọn biện pháp khắc phục.';
    } else if (line) {
      title = `Xác minh tình hình lỗi tại ${label(line[1])}`;
      owner = `Trưởng nhóm ${label(line[1])} cùng bộ phận kế hoạch sản xuất`;
      plan =
        'So sánh cơ cấu sản phẩm và loại lỗi, xác nhận sản lượng thực tế cùng độ bao phủ kiểm tra, rồi cùng nhóm phụ trách chuyền tìm hiểu các lỗi lặp lại.';
    } else if (cost) {
      title = `Rà soát chi phí sửa lỗi ${label(cost[1])} và việc ghi nhận chi phí`;
      owner = 'Trưởng bộ phận bảo trì, kỹ sư chất lượng và tài chính';
      plan =
        'Cùng bộ phận bảo trì xem lại các sửa chữa lặp lại có chi phí cao, đối soát chi phí thiếu hoặc không hợp lệ với tài chính và kiểm chứng hiệu quả kinh tế của biện pháp khắc phục.';
    } else if (category) {
      title = `Điều tra sản phẩm ${label(category[1])} có cường độ lỗi cao`;
      owner = 'Kỹ thuật quy trình cùng kỹ sư chất lượng và các trưởng nhóm sản xuất';
      plan =
        'So sánh thiết kế, vật liệu và các bước công đoạn của sản phẩm có cường độ lỗi cao nhất trong nhóm với sản phẩm cường độ thấp hơn trên cùng chuyền; xem xét các lỗi đại diện trước khi thay đổi quy trình.';
    } else if (lineCost) {
      title = `Điều tra chi phí sửa chữa mỗi sự kiện tại ${label(lineCost[1])}`;
      owner = `Trưởng nhóm ${label(lineCost[1])} cùng bộ phận bảo trì và tài chính`;
      plan =
        'Cùng bộ phận bảo trì tách chi phí sửa chữa theo cách sửa, nhân công, vật tư và làm lại; xác nhận quy tắc ghi nhận chi phí với tài chính; so sánh cùng nhóm sản phẩm ở chuyền khác trước khi quy chênh lệch cho chuyền.';
    }
  }
  return {
    value: `recommendation:${item.priority}`,
    source: { kind: 'recommendation', key: String(item.priority) },
    title,
    owner,
    plan: plan || t('ui.review_the_selected_investigation'),
  };
}

export function operationDraftPrefill(initialDraft: OperationDraftRequest) {
  return {
    title: initialDraft.title,
    plan: initialDraft.actionPlan ?? '',
    owner: initialDraft.proposedOwner ?? '',
  };
}

export function operationDraftSource(initialDraft?: OperationDraftRequest): OperationSource {
  return initialDraft?.source ?? { kind: 'manual' };
}

function draftFor(item: OperationCase | null): WorkDraft {
  return {
    title: item?.title ?? '',
    owner: item?.proposed_owner ?? '',
    due: item?.due_date ?? '',
    plan: item?.action_plan ?? '',
    status: item?.status ?? 'draft',
    note: '',
    outcome: item?.outcome ?? '',
  };
}

export function operationReportReference(detail: string, t: Language['t']) {
  const text = detail.trim();
  if (text.length <= 4000) return { text, shortened: false };
  const notice = t('ui.excerpt_from_the_analysis_report_the_complete_report_remains_in_a');
  const available = 4000 - notice.length - 3;
  let excerpt = text.slice(0, available).replace(/[\uD800-\uDBFF]$/, '');
  const lastBreak = excerpt.lastIndexOf('\n');
  if (lastBreak > available * 0.7) excerpt = excerpt.slice(0, lastBreak);
  return { text: `${notice}\n\n${excerpt}…`, shortened: true };
}

export function validateOperationDraft(draft: WorkDraft, previous: OperationStatus | null) {
  const errors: Partial<Record<keyof WorkDraft, 'required' | 'invalid' | 'too_long'>> = {};
  if (!draft.title.trim()) errors.title = 'required';
  else if (draft.title.trim().length > 180) errors.title = 'too_long';
  if (draft.owner.trim().length > 160) errors.owner = 'too_long';
  if (!operationDateIsValid(draft.due)) errors.due = 'invalid';
  for (const field of ['plan', 'note', 'outcome'] as const) {
    if (draft[field].trim().length > 4000) errors[field] = 'too_long';
  }
  if (previous && draft.status === 'action_planned' && !draft.plan.trim()) errors.plan = 'required';
  if (previous && draft.status !== previous) {
    if (draft.status === 'resolved' && !draft.outcome.trim()) errors.outcome = 'required';
    if ((draft.status === 'dismissed' || operationIsClosed(previous)) && !draft.note.trim())
      errors.note = 'required';
  }
  return errors;
}

export function operationUpdateFromDraft(record: OperationCase, draft: WorkDraft): OperationUpdate {
  const editable = {
    title: draft.title.trim(),
    proposed_owner: draft.owner.trim() || null,
    due_date: draft.due || null,
    action_plan: draft.plan.trim() || null,
  };
  const changes: OperationChanges = {};
  for (const key of ['title', 'proposed_owner', 'due_date', 'action_plan'] as const) {
    if (editable[key] !== record[key]) Object.assign(changes, { [key]: editable[key] });
  }
  const update: OperationUpdate = { changes };
  if (draft.status !== record.status) {
    update.status = draft.status;
    if (draft.status === 'resolved') update.outcome = draft.outcome.trim();
  }
  if (draft.note.trim()) update.note = draft.note.trim();
  return update;
}

export function restoreOperationFocus(previous: HTMLElement | null, fallback?: () => void) {
  // A tab switch can leave BODY active before the report form opens.
  if (
    previous &&
    !['BODY', 'HTML'].includes(previous.tagName) &&
    previous.isConnected &&
    previous.getClientRects().length &&
    !previous.matches(':disabled') &&
    !previous.closest('[hidden], [inert]')
  ) {
    previous.focus();
    if (previous.ownerDocument.activeElement === previous) return;
  }
  fallback?.();
}

export function operationEditorForReview(
  item: OperationCase,
  request: OperationReviewRequest,
): EditorState | null {
  if (
    !request.caseId ||
    !request.contextId ||
    item.id !== request.caseId ||
    item.context_id !== request.contextId ||
    item.context?.context_id !== request.contextId
  )
    return null;
  return { context: item.context, item, sources: [], reviewId: request.id };
}

function operationError(error: unknown, t: Language['t']) {
  if (error instanceof OperationsError && error.status === 409) {
    return t('ui.this_work_changed_elsewhere_load_the_latest_version_before_saving');
  }
  if (error instanceof OperationsError && error.status === 404) {
    return t('ui.this_work_or_its_saved_data_view_is_unavailable_reload_the_list_a');
  }
  if (error instanceof OperationsError && error.status === 422) {
    return t('ui.some_details_could_not_be_accepted_check_the_title_date_and_requi');
  }
  return t('ui.could_not_save_the_change_your_entries_are_still_here_try_again');
}

export default function OperationsPanel({
  context,
  visible = true,
  isContextChanging = false,
  recommendations = [],
  candidates = [],
  draft: requestedDraft,
  onDraftConsumed,
  reviewRequest,
  onReviewConsumed,
}: {
  context: OperationContext | null;
  visible?: boolean;
  isContextChanging?: boolean;
  recommendations?: Recommendation[];
  candidates?: Candidate[];
  draft?: OperationDraftRequest;
  onDraftConsumed?: () => void;
  reviewRequest?: OperationReviewRequest;
  onReviewConsumed?: () => void;
}) {
  const language = useI18n();
  const { t, number, date } = language;
  const [cases, setCases] = useState<OperationCase[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [revision, setRevision] = useState(0);
  const [showAll, setShowAll] = useState(false);
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [feedback, setFeedback] = useState('');
  const [draftError, setDraftError] = useState('');
  const [draftLoading, setDraftLoading] = useState(false);
  const [reviewError, setReviewError] = useState('');
  const [reviewLoading, setReviewLoading] = useState(false);
  const consumedDraft = useRef<number | null>(null);
  const consumedReview = useRef<number | null>(null);
  const addButton = useRef<HTMLButtonElement>(null);
  const headingId = useId();
  useEffect(() => {
    if (!visible) return;
    const abort = new AbortController();
    setLoading(true);
    setError(false);
    listOperationCases(abort.signal)
      .then((result) => {
        if (!abort.signal.aborted) setCases(result.cases);
      })
      .catch(() => {
        if (!abort.signal.aborted) setError(true);
      })
      .finally(() => {
        if (!abort.signal.aborted) setLoading(false);
      });
    return () => abort.abort();
  }, [visible, revision]);
  useEffect(() => {
    if (
      !visible ||
      reviewRequest ||
      !requestedDraft ||
      consumedDraft.current === requestedDraft.id
    ) {
      setDraftLoading(false);
      return;
    }
    const abort = new AbortController();
    setDraftError('');
    setReviewError('');
    setFeedback('');
    const consume = () => {
      consumedDraft.current = requestedDraft.id;
      onDraftConsumed?.();
    };
    if (!requestedDraft.contextId) {
      setDraftLoading(false);
      setDraftError(t('ui.this_report_has_no_saved_dashboard_scope_use_add_work_to_start_a_'));
      consume();
      return;
    }
    setDraftLoading(true);
    getOperationContext(requestedDraft.contextId, abort.signal)
      .then((result) => {
        if (abort.signal.aborted) return;
        setEditor({
          context: result.context,
          item: null,
          sources: [],
          initialDraft: requestedDraft,
        });
        setDraftLoading(false);
        consume();
      })
      .catch((failure: unknown) => {
        if (abort.signal.aborted) return;
        setDraftError(
          failure instanceof OperationsError && [404, 409].includes(failure.status)
            ? t('ui.this_report_s_saved_data_view_is_no_longer_available_for_a_new_fo')
            : t('ui.the_report_s_saved_scope_could_not_be_loaded_return_to_the_report'),
        );
        setDraftLoading(false);
        consume();
      })
      .finally(() => {
        if (!abort.signal.aborted) setDraftLoading(false);
      });
    return () => abort.abort();
  }, [visible, requestedDraft, onDraftConsumed, reviewRequest, t]);
  useEffect(() => {
    if (!visible || !reviewRequest || consumedReview.current === reviewRequest.id) {
      setReviewLoading(false);
      return;
    }
    let cancelled = false;
    setReviewError('');
    setDraftError('');
    setFeedback('');
    const consume = () => {
      consumedReview.current = reviewRequest.id;
      onReviewConsumed?.();
    };
    if (!reviewRequest.caseId || !reviewRequest.contextId) {
      setReviewLoading(false);
      setReviewError(t('ui.this_report_does_not_identify_the_saved_work_and_its_original_vie'));
      consume();
      return;
    }
    setReviewLoading(true);
    getOperationCase(reviewRequest.caseId)
      .then((item) => {
        if (cancelled) return;
        const selected = operationEditorForReview(item, reviewRequest);
        if (selected) {
          setCases((current) => [item, ...current.filter((entry) => entry.id !== item.id)]);
          setEditor(selected);
        } else {
          setReviewError(t('ui.the_saved_work_does_not_match_this_report_s_original_view_check_t'));
        }
        setReviewLoading(false);
        consume();
      })
      .catch((failure: unknown) => {
        if (cancelled) return;
        setReviewError(
          failure instanceof OperationsError && failure.status === 404
            ? t('ui.the_saved_work_could_not_be_found_return_to_the_report_to_check_w')
            : t('ui.could_not_open_the_saved_work_return_to_the_report_and_choose_rev'),
        );
        setReviewLoading(false);
        consume();
      });
    return () => {
      cancelled = true;
    };
  }, [visible, reviewRequest, onReviewConsumed, t]);

  const openCases = cases.filter((item) => !operationIsClosed(item.status));
  const closedCases = cases.filter((item) => operationIsClosed(item.status));
  function updateItem(item: OperationCase) {
    setCases((current) => [item, ...current.filter((entry) => entry.id !== item.id)]);
  }
  function openNew() {
    if (!context || isContextChanging) return;
    const sources: SourceOption[] = recommendations.map((item) =>
      recommendationOption(item, language),
    );
    for (const item of candidates)
      sources.push({
        value: `historical_candidate:${item.date}`,
        source: { kind: 'historical_candidate', key: item.date },
        title: t('ui.review_logged_defects_on_value', { value0: date(item.date) }),
        owner: '',
        plan: t('ui.verify_the_day_s_production_inspection_and_reporting_then_review_'),
      });
    for (const source of sources)
      source.tracked = cases.some(
        (entry) =>
          entry.context_id === context.context_id &&
          entry.source.kind === source.source.kind &&
          entry.source.key === source.source.key,
      );
    setFeedback('');
    setEditor({ context, item: null, sources });
  }
  function openItem(item: OperationCase) {
    setFeedback('');
    setEditor({ context: item.context, item, sources: [] });
  }
  function renderItem(item: OperationCase) {
    return (
      <li className="ops-work-row" key={item.id}>
        <button
          type="button"
          className="ops-work-open"
          onClick={() => openItem(item)}
          disabled={draftLoading || reviewLoading}
        >
          <span className="ops-work-main">
            <span className="ops-work-title">{item.title}</span>
            <span className="ops-work-meta">
              {item.proposed_owner
                ? t('ui.proposed_value', { value0: item.proposed_owner })
                : t('ui.owner_to_confirm')}
              {item.due_date && (
                <span className={item.is_overdue ? 'ops-overdue' : ''}>
                  {item.is_overdue ? t('ui.past_due') : t('ui.due')} {date(item.due_date)}
                </span>
              )}
            </span>
            <span className="ops-work-scope">
              {operationScopeLabel(item.context.filters, language)}
            </span>
          </span>
          <span className={`ops-status ops-status-${item.status}`}>
            {operationStatusLabel(item.status, t)}
          </span>
          <ChevronRight size={16} aria-hidden="true" />
        </button>
      </li>
    );
  }
  return (
    <section className="ops-panel" aria-labelledby={headingId}>
      <div className="ops-panel-heading">
        <div>
          <h2 id={headingId}>{t('ui.follow_up_work')}</h2>
          <p>{t('ui.keep_the_next_step_owner_and_outcome_together')}</p>
        </div>
        <button
          ref={addButton}
          type="button"
          className="ops-button ops-primary"
          onClick={openNew}
          disabled={!context || isContextChanging || draftLoading || reviewLoading}
        >
          <Plus size={16} aria-hidden="true" />
          {t('ui.add_work')}
        </button>
      </div>
      <p className="ops-feedback" role="status" aria-live="polite">
        {feedback}
      </p>
      {draftLoading && (
        <p className="ops-state" role="status">
          <LoaderCircle size={18} className="ops-spin" aria-hidden="true" />
          {t('ui.loading_the_report_s_saved_scope')}
        </p>
      )}
      {draftError && (
        <p className="ops-form-feedback" role="alert">
          {draftError}
        </p>
      )}
      {reviewLoading && (
        <p className="ops-state" role="status">
          <LoaderCircle size={18} className="ops-spin" aria-hidden="true" />
          {t('ui.opening_saved_work')}
        </p>
      )}
      {reviewError && (
        <p className="ops-form-feedback" role="alert">
          {reviewError}
        </p>
      )}
      {loading ? (
        <p className="ops-state" role="status">
          <LoaderCircle size={18} className="ops-spin" aria-hidden="true" />
          {t('ui.loading_follow_up_work')}
        </p>
      ) : error ? (
        <div className="ops-state ops-error" role="alert">
          <AlertCircle size={18} aria-hidden="true" />
          <p>{t('ui.could_not_load_saved_work')}</p>
          <button
            type="button"
            className="ops-button"
            onClick={() => setRevision((value) => value + 1)}
          >
            {t('ui.try_again')}
          </button>
        </div>
      ) : (
        <>
          {!openCases.length ? (
            <div className="ops-empty">
              <ClipboardList size={23} aria-hidden="true" />
              <div>
                <strong>{t('ui.no_open_follow_up_work')}</strong>
                <p>{t('ui.choose_an_investigation_to_follow_up_or_add_your_own')}</p>
              </div>
            </div>
          ) : (
            <>
              <ul className="ops-work-list" aria-label={t('ui.open_work')}>
                {(showAll ? openCases : openCases.slice(0, 3)).map(renderItem)}
              </ul>
              {openCases.length > 3 && (
                <button
                  type="button"
                  className="ops-text-button"
                  onClick={() => setShowAll((value) => !value)}
                  aria-expanded={showAll}
                >
                  {showAll
                    ? t('ui.show_fewer')
                    : t('ui.show_all_value_open_items', { value0: number(openCases.length) })}
                </button>
              )}
            </>
          )}
          {closedCases.length > 0 && (
            <details className="ops-closed">
              <summary>
                {t('ui.closed_or_set_aside')} <span>{number(closedCases.length)}</span>
              </summary>
              <ul className="ops-work-list">{closedCases.map(renderItem)}</ul>
            </details>
          )}
        </>
      )}
      {editor && (
        <OperationEditor
          key={
            editor.reviewId != null
              ? `review-${editor.reviewId}`
              : (editor.item?.id ?? editor.initialDraft?.id ?? 'new-work')
          }
          {...editor}
          onChange={updateItem}
          onClose={() => setEditor(null)}
          returnFocus={() => addButton.current?.focus()}
          onReloadList={() => {
            setEditor(null);
            setRevision((value) => value + 1);
          }}
          onSaved={(item, created) => {
            updateItem(item);
            setFeedback(
              created
                ? t('ui.value_is_saved_in_follow_up_work', { value0: item.title })
                : t('ui.saved_value', { value0: item.title }),
            );
            setEditor(null);
          }}
        />
      )}
    </section>
  );
}

export function OperationEditor({
  context,
  item,
  sources,
  initialDraft,
  reviewId,
  onChange,
  onClose,
  onSaved,
  returnFocus,
  onReloadList,
}: EditorState & {
  onChange: (item: OperationCase) => void;
  onClose: () => void;
  onSaved: (item: OperationCase, created: boolean) => void;
  returnFocus?: () => void;
  onReloadList?: () => void;
}) {
  const language = useI18n();
  const { t, date } = language;
  const [record, setRecord] = useState(item);
  const [draft, setDraft] = useState<WorkDraft>(() => ({
    ...draftFor(item),
    ...(initialDraft ? operationDraftPrefill(initialDraft) : {}),
  }));
  const [sourceValue, setSourceValue] = useState('manual');
  const [busy, setBusy] = useState(false);
  const [comparisonBusy, setComparisonBusy] = useState(false);
  const [comparisonRevision, setComparisonRevision] = useState(0);
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState(false);
  const [errors, setErrors] = useState<ReturnType<typeof validateOperationDraft>>({});
  const dialog = useRef<HTMLDialogElement>(null);
  const titleInput = useRef<HTMLInputElement>(null);
  const idempotency = useRef('');
  const busyRef = useRef(false);
  const returnFocusRef = useRef(returnFocus);
  returnFocusRef.current = returnFocus;
  const prefix = useId();
  const locked = busy || comparisonBusy;
  const metadataReadOnly = !!record && operationIsClosed(record.status);
  const reportReference = initialDraft ? operationReportReference(initialDraft.detail, t) : null;
  const contextDescription = record?.description || reportReference?.text || '';
  useEffect(() => {
    const element = dialog.current;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    element?.showModal();
    titleInput.current?.focus();
    return () => {
      element?.close();
      restoreOperationFocus(previous, returnFocusRef.current);
    };
  }, []);
  function updateRecord(next: OperationCase) {
    setRecord(next);
    onChange(next);
  }
  function setField<K extends keyof WorkDraft>(key: K, value: WorkDraft[K]) {
    setDraft((current) => ({ ...current, [key]: value }));
    setErrors((current) => ({ ...current, [key]: undefined }));
  }
  const selectedSource = sources.find((source) => source.value === sourceValue);
  function selectSource(value: string) {
    setSourceValue(value);
    const choice = sources.find((source) => source.value === value);
    setDraft((current) => ({
      ...current,
      title: choice?.title ?? '',
      owner: choice?.owner ?? '',
      plan: choice?.plan ?? '',
    }));
    setErrors({});
  }
  const statusChanged = !!record && draft.status !== record.status;
  const needsReason =
    statusChanged && (draft.status === 'dismissed' || operationIsClosed(record!.status));
  const needsOutcome = statusChanged && draft.status === 'resolved';
  const historical =
    (record?.source.kind ?? selectedSource?.source.kind ?? initialDraft?.source?.kind) ===
    'historical_candidate';
  const fieldError = (field: keyof WorkDraft) =>
    errors[field] ? (
      <span className="ops-field-error" id={`${prefix}-${field}-error`}>
        {errors[field] === 'too_long'
          ? t('ui.please_shorten_this_entry')
          : field === 'due'
            ? t('ui.enter_a_valid_date')
            : field === 'title'
              ? t('ui.enter_a_work_title')
              : field === 'plan'
                ? t('ui.describe_the_planned_action_first')
                : field === 'outcome'
                  ? t('ui.describe_the_observed_outcome_before_closing')
                  : t('ui.give_a_reason_for_this_change')}
      </span>
    ) : null;
  async function reloadLatest() {
    if (!record || busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    try {
      const latest = await getOperationCase(record.id);
      updateRecord(latest);
      setDraft(draftFor(latest));
      setComparisonRevision((value) => value + 1);
      setError('');
      setConflict(false);
      setErrors({});
    } catch (failure) {
      setError(operationError(failure, t));
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (busyRef.current || comparisonBusy || conflict) return;
    const problems = validateOperationDraft(draft, record?.status ?? null);
    setErrors(problems);
    if (Object.keys(problems).length) {
      requestAnimationFrame(() =>
        dialog.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus(),
      );
      return;
    }
    busyRef.current = true;
    setBusy(true);
    setError('');
    const created = !record;
    try {
      const editable = {
        title: draft.title.trim(),
        proposed_owner: draft.owner.trim() || null,
        due_date: draft.due || null,
        action_plan: draft.plan.trim() || null,
      };
      let current = record;
      if (!current) {
        if (!idempotency.current) idempotency.current = crypto.randomUUID();
        current = await createOperationCase({
          ...editable,
          description: reportReference?.text ?? '',
          origin_report_id: initialDraft?.reportId || null,
          context_id: context.context_id,
          source: selectedSource?.source ?? operationDraftSource(initialDraft),
          idempotency_key: idempotency.current,
        });
        updateRecord(current);
        if (
          Object.entries(editable).some(
            ([key, value]) => current![key as keyof OperationCase] !== value,
          )
        ) {
          setDraft((value) => ({ ...value, status: current!.status }));
          setError(t('ui.this_investigation_is_already_being_followed_your_entries_are_sti'));
          return;
        }
      } else {
        current = await applyOperationUpdate(
          current.id,
          current.version,
          operationUpdateFromDraft(current, draft),
        );
        updateRecord(current);
      }
      onSaved(current, created);
    } catch (failure) {
      setError(
        !record && failure instanceof OperationsError && failure.status === 409
          ? t('ui.this_request_may_already_be_saved_review_the_work_list_before_try')
          : operationError(failure, t),
      );
      setConflict(failure instanceof OperationsError && failure.status === 409);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }
  return (
    <dialog
      className="ops-dialog"
      ref={dialog}
      aria-labelledby={`${prefix}-heading`}
      onCancel={(event) => {
        event.preventDefault();
        if (!busyRef.current && !comparisonBusy) onClose();
      }}
    >
      <div className="ops-dialog-header">
        <div>
          <p className="ops-kicker">
            {historical ? t('ui.historical_review') : t('ui.follow_up_work')}
          </p>
          <h2 id={`${prefix}-heading`}>
            {record ? t('ui.review_and_update') : t('ui.add_follow_up_work')}
          </h2>
        </div>
        <button
          type="button"
          className="ops-icon-button"
          onClick={onClose}
          disabled={locked}
          aria-label={t('ui.close_work_details')}
        >
          <X size={20} aria-hidden="true" />
        </button>
      </div>
      <form onSubmit={save} className="ops-form" noValidate>
        <div className="ops-dialog-body">
          <p className="ops-context">
            {(initialDraft || reviewId != null || record?.origin_report_id) && (
              <strong className="ops-context-origin">{t('ui.report_s_saved_scope')}</strong>
            )}
            {operationScopeLabel(context.filters, language)}
          </p>
          {historical && (
            <p className="ops-context-note">
              {t('ui.this_item_reviews_a_past_day_in_the_saved_data_confirm_reporting_')}
            </p>
          )}
          {record && (
            <p className="ops-record-status">
              <span className={`ops-status ops-status-${record.status}`}>
                {operationStatusLabel(record.status, t)}
              </span>
              <span>
                {t('ui.updated')} {date(record.updated_at)}
              </span>
            </p>
          )}
          {metadataReadOnly && (
            <p className="ops-context-note">
              {t('ui.you_can_add_a_note_reopen_and_save_this_work_before_editing_its_d')}
            </p>
          )}
          {!record && sources.length > 0 && (
            <label className="ops-field" htmlFor={`${prefix}-source`}>
              <span>{t('ui.start_from')}</span>
              <select
                id={`${prefix}-source`}
                value={sourceValue}
                onChange={(event) => selectSource(event.target.value)}
                disabled={busy}
              >
                <option value="manual">{t('ui.my_own_follow_up')}</option>
                {sources.some((source) => source.source.kind === 'recommendation') && (
                  <optgroup label={t('ui.suggested_investigations')}>
                    {sources
                      .filter((source) => source.source.kind === 'recommendation')
                      .map((source) => (
                        <option value={source.value} key={source.value} disabled={source.tracked}>
                          {source.title}
                          {source.tracked ? t('ui.already_followed') : ''}
                        </option>
                      ))}
                  </optgroup>
                )}
                {sources.some((source) => source.source.kind === 'historical_candidate') && (
                  <optgroup label={t('ui.past_days_to_review')}>
                    {sources
                      .filter((source) => source.source.kind === 'historical_candidate')
                      .map((source) => (
                        <option value={source.value} key={source.value} disabled={source.tracked}>
                          {source.title}
                          {source.tracked ? t('ui.already_followed') : ''}
                        </option>
                      ))}
                  </optgroup>
                )}
              </select>
            </label>
          )}
          <label className="ops-field" htmlFor={`${prefix}-title`}>
            <span>
              {t('ui.work_title')} <span className="ops-required">*</span>
            </span>
            <input
              ref={titleInput}
              id={`${prefix}-title`}
              value={draft.title}
              onChange={(event) => setField('title', event.target.value)}
              maxLength={180}
              required
              disabled={busy}
              readOnly={metadataReadOnly}
              aria-invalid={!!errors.title}
              aria-describedby={errors.title ? `${prefix}-title-error` : undefined}
              autoComplete="off"
            />
            {fieldError('title')}
          </label>
          <div className="ops-fields-pair">
            <label className="ops-field" htmlFor={`${prefix}-owner`}>
              <span>{t('ui.proposed_owner_2')}</span>
              <input
                id={`${prefix}-owner`}
                value={draft.owner}
                onChange={(event) => setField('owner', event.target.value)}
                maxLength={160}
                placeholder={t('ui.name_or_team')}
                disabled={busy}
                readOnly={metadataReadOnly}
                aria-invalid={!!errors.owner}
                aria-describedby={`${prefix}-owner-hint${errors.owner ? ` ${prefix}-owner-error` : ''}`}
              />
              <small id={`${prefix}-owner-hint`}>
                {t('ui.confirm_responsibility_with_the_person_or_team')}
              </small>
              {fieldError('owner')}
            </label>
            <label className="ops-field" htmlFor={`${prefix}-due`}>
              <span>{t('ui.due_date')}</span>
              <input
                id={`${prefix}-due`}
                type="date"
                value={draft.due}
                onChange={(event) => setField('due', event.target.value)}
                disabled={busy}
                readOnly={metadataReadOnly}
                aria-invalid={!!errors.due}
                aria-describedby={errors.due ? `${prefix}-due-error` : undefined}
              />
              {fieldError('due')}
            </label>
          </div>
          <label className="ops-field" htmlFor={`${prefix}-plan`}>
            <span>
              {t('ui.next_step_2')}
              {record && draft.status === 'action_planned' && (
                <span className="ops-required"> *</span>
              )}
            </span>
            <textarea
              id={`${prefix}-plan`}
              rows={3}
              value={draft.plan}
              onChange={(event) => setField('plan', event.target.value)}
              maxLength={4000}
              placeholder={t('ui.describe_the_specific_action_to_take')}
              disabled={busy}
              readOnly={metadataReadOnly}
              aria-invalid={!!errors.plan}
              aria-describedby={errors.plan ? `${prefix}-plan-error` : undefined}
            />
            {fieldError('plan')}
          </label>
          {contextDescription && (
            <details className="ops-reference">
              <summary>
                {reportReference?.shortened
                  ? t('ui.analysis_context_excerpt')
                  : t('ui.analysis_context')}
              </summary>
              <p className="ops-reference-hint">
                {reportReference?.shortened
                  ? t('ui.a_shortened_excerpt_will_be_saved_with_this_work_read_the_complet')
                  : initialDraft || record?.origin_report_id
                    ? t('ui.reference_from_the_saved_analysis_report_the_original_report_rema')
                    : t('ui.reference_material_for_this_work')}
              </p>
              <p className="ops-saved-text">{contextDescription}</p>
            </details>
          )}
          {record && (
            <>
              <label className="ops-field" htmlFor={`${prefix}-status`}>
                <span>{t('ui.status')}</span>
                <select
                  id={`${prefix}-status`}
                  value={draft.status}
                  onChange={(event) => setField('status', event.target.value as OperationStatus)}
                  disabled={busy}
                >
                  <option value={record.status}>{operationStatusLabel(record.status, t)}</option>
                  {record.allowed_transitions.map((status) => (
                    <option key={status} value={status}>
                      {operationIsClosed(record.status) && status === 'investigating'
                        ? t('ui.reopen_investigation')
                        : operationStatusLabel(status, t)}
                    </option>
                  ))}
                </select>
              </label>
              {needsOutcome && (
                <label className="ops-field" htmlFor={`${prefix}-outcome`}>
                  <span>
                    {t('ui.observed_outcome')} <span className="ops-required">*</span>
                  </span>
                  <textarea
                    id={`${prefix}-outcome`}
                    rows={3}
                    value={draft.outcome}
                    onChange={(event) => setField('outcome', event.target.value)}
                    maxLength={4000}
                    disabled={busy}
                    required
                    aria-invalid={!!errors.outcome}
                    aria-describedby={`${prefix}-outcome-hint${errors.outcome ? ` ${prefix}-outcome-error` : ''}`}
                  />
                  <small id={`${prefix}-outcome-hint`}>
                    {t('ui.describe_what_was_checked_or_changed_and_what_still_needs_verific')}
                  </small>
                  {fieldError('outcome')}
                </label>
              )}
              {!needsOutcome && record.outcome && (
                <div className="ops-outcome">
                  <strong>{t('ui.recorded_outcome')}</strong>
                  <p>{record.outcome}</p>
                </div>
              )}
              <label className="ops-field" htmlFor={`${prefix}-note`}>
                <span>
                  {needsReason ? t('ui.reason_for_this_change') : t('ui.add_a_progress_note')}
                  {needsReason && <span className="ops-required"> *</span>}
                </span>
                <textarea
                  id={`${prefix}-note`}
                  rows={2}
                  value={draft.note}
                  onChange={(event) => setField('note', event.target.value)}
                  maxLength={4000}
                  disabled={busy}
                  required={needsReason}
                  aria-invalid={!!errors.note}
                  aria-describedby={errors.note ? `${prefix}-note-error` : undefined}
                />
                {fieldError('note')}
              </label>
              <FollowUpComparison
                key={`${record.id}:${comparisonRevision}`}
                record={record}
                busy={busy}
                onBusyChange={setComparisonBusy}
                onChange={updateRecord}
                onConflict={() => {
                  setConflict(true);
                  setError(operationError(new OperationsError('', 409), t));
                }}
              />
              {record.history.length > 0 && (
                <details className="ops-history">
                  <summary>{t('ui.previous_updates')}</summary>
                  <ol>
                    {record.history.map((entry) => (
                      <li key={entry.id}>
                        <time dateTime={entry.at}>{date(entry.at)}</time>
                        <p>
                          {entry.to_status && entry.to_status !== entry.from_status
                            ? operationStatusLabel(entry.to_status, t)
                            : entry.kind === 'created'
                              ? t('ui.work_added')
                              : entry.kind === 'follow_up_added'
                                ? t('ui.comparison_saved')
                                : entry.note
                                  ? t('ui.progress_note')
                                  : t('ui.details_updated')}
                          {entry.note && <>: {entry.note}</>}
                        </p>
                      </li>
                    ))}
                  </ol>
                </details>
              )}
            </>
          )}
          <div className="ops-form-feedback" role="alert">
            {error && (
              <>
                <p>{error}</p>
                {conflict && (
                  <>
                    <p>
                      {record
                        ? t('ui.loading_the_latest_version_replaces_the_unsaved_entries_above')
                        : t('ui.returning_to_the_list_closes_this_draft_and_discards_its_unsaved_')}
                    </p>
                    <button
                      type="button"
                      className="ops-button"
                      onClick={() => (record ? void reloadLatest() : (onReloadList ?? onClose)())}
                      disabled={locked}
                    >
                      <RotateCcw size={15} aria-hidden="true" />
                      {record ? t('ui.load_latest_version') : t('ui.review_saved_work')}
                    </button>
                  </>
                )}
              </>
            )}
          </div>
        </div>
        <div className="ops-dialog-footer">
          <button type="button" className="ops-button" onClick={onClose} disabled={locked}>
            {t('ui.cancel')}
          </button>
          <button type="submit" className="ops-button ops-primary" disabled={locked || conflict}>
            {busy ? (
              <LoaderCircle size={16} className="ops-spin" aria-hidden="true" />
            ) : (
              <Check size={16} aria-hidden="true" />
            )}
            {busy
              ? t('ui.saving')
              : metadataReadOnly && draft.status === 'investigating'
                ? t('ui.reopen_work')
                : record
                  ? t('ui.save_update')
                  : t('ui.add_to_follow_up')}
          </button>
        </div>
      </form>
    </dialog>
  );
}

function comparisonMetricLabel(key: string, t: Language['t']) {
  const labels: Record<string, [string, string]> = {
    event_count: ['Logged events', 'Sự kiện ghi nhận'],
    critical_count: ['Critical events', 'Sự kiện nghiêm trọng'],
    critical_share_pct: ['Critical share (%)', 'Tỷ trọng nghiêm trọng (%)'],
    known_repair_cost: ['Recorded repair cost', 'Chi phí sửa chữa ghi nhận'],
    cost_known_count: ['Events with known cost', 'Sự kiện đã rõ chi phí'],
    cost_unknown_count: ['Events with unknown cost', 'Sự kiện chưa rõ chi phí'],
    cost_coverage_pct: ['Cost coverage (%)', 'Tỷ lệ có chi phí (%)'],
    reference_intensity_per_1000: [
      'Events per 1,000 reference units',
      'Sự kiện trên 1.000 đơn vị tham chiếu',
    ],
    reference_output: ['Reference output', 'Sản lượng tham chiếu'],
  };
  return labels[key] ? t(...labels[key]) : key.replaceAll('_', ' ');
}

export function ComparisonResult({ comparison }: { comparison: OperationComparison }) {
  const { t, number, date } = useI18n();
  return (
    <div className="ops-comparison-result">
      <p className="ops-comparison-summary">
        {comparison.status === 'unavailable'
          ? t('ui.the_source_could_not_be_checked_the_saved_view_is_still_available')
          : comparison.status === 'unchanged'
            ? t('ui.the_source_is_unchanged_there_is_no_new_data_here_to_assess_the_o')
            : t('ui.the_source_has_changed_review_the_observed_differences_below')}
      </p>
      {comparison.metrics.length > 0 && comparison.status !== 'unavailable' && (
        <div className="ops-table-scroll">
          <table className="ops-comparison-table">
            <caption className="sr-only">{t('ui.saved_view_and_follow_up_comparison')}</caption>
            <thead>
              <tr>
                <th scope="col">{t('ui.measure')}</th>
                <th scope="col">{t('ui.saved')}</th>
                <th scope="col">{t('ui.checked')}</th>
                <th scope="col">{t('ui.change')}</th>
              </tr>
            </thead>
            <tbody>
              {comparison.metrics.map((metric) => (
                <tr key={metric.key}>
                  <th scope="row">{comparisonMetricLabel(metric.key, t)}</th>
                  <td>
                    {number(
                      metric.before,
                      (metric.key.includes('cost') && !metric.key.includes('count')) ||
                        metric.key.includes('pct') ||
                        metric.key.includes('per_')
                        ? 2
                        : 0,
                    )}
                  </td>
                  <td>
                    {number(
                      metric.after,
                      (metric.key.includes('cost') && !metric.key.includes('count')) ||
                        metric.key.includes('pct') ||
                        metric.key.includes('per_')
                        ? 2
                        : 0,
                    )}
                  </td>
                  <td>
                    {metric.delta != null && metric.delta > 0 ? '+' : ''}
                    {number(
                      metric.delta,
                      (metric.key.includes('cost') && !metric.key.includes('count')) ||
                        metric.key.includes('pct') ||
                        metric.key.includes('per_')
                        ? 2
                        : 0,
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {comparison.coverage?.latest_event_date && (
        <p className="ops-comparison-note">
          {t('ui.latest_recorded_date_in_this_view')} {date(comparison.coverage.latest_event_date)}
        </p>
      )}
      <p className="ops-comparison-note">
        {t('ui.counts_describe_the_recorded_data_actual_production_inspection_an')}
      </p>
      <time className="ops-comparison-note" dateTime={comparison.checked_at}>
        {t('ui.checked_2')} {date(comparison.checked_at)}
      </time>
    </div>
  );
}

function FollowUpComparison({
  record,
  busy,
  onBusyChange,
  onChange,
  onConflict,
}: {
  record: OperationCase;
  busy: boolean;
  onBusyChange: (value: boolean) => void;
  onChange: (item: OperationCase) => void;
  onConflict: () => void;
}) {
  const { t } = useI18n();
  const [checking, setChecking] = useState(false);
  const [saving, setSaving] = useState(false);
  const [comparison, setComparison] = useState<OperationComparison | null>(
    record.follow_up ?? null,
  );
  const [saved, setSaved] = useState(!!record.follow_up);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const pending = useRef(false);
  async function checkSource() {
    if (pending.current) return;
    pending.current = true;
    onBusyChange(true);
    setChecking(true);
    setError('');
    setMessage('');
    try {
      const result = await checkOperationSource(record.context_id);
      setComparison(result);
      setSaved(false);
      setMessage(t('ui.source_check_finished_review_the_comparison_before_saving_it_to_t'));
    } catch {
      setError(t('ui.could_not_check_the_source_try_again_when_the_connection_is_avail'));
    } finally {
      pending.current = false;
      setChecking(false);
      onBusyChange(false);
    }
  }
  async function saveComparison() {
    if (!comparison?.current_context_id || pending.current) return;
    pending.current = true;
    onBusyChange(true);
    setSaving(true);
    setError('');
    try {
      const result = await attachOperationFollowUp(
        record.id,
        record.version,
        comparison.current_context_id,
      );
      onChange(result);
      setComparison(result.follow_up);
      setSaved(true);
      setMessage(t('ui.comparison_saved_to_this_work'));
    } catch (failure) {
      if (failure instanceof OperationsError && failure.status === 409) onConflict();
      else
        setError(
          failure instanceof OperationsError && failure.status === 422
            ? t('ui.the_checked_view_cannot_be_compared_with_this_work_the_saved_filt')
            : operationError(failure, t),
        );
    } finally {
      pending.current = false;
      setSaving(false);
      onBusyChange(false);
    }
  }
  return (
    <details className="ops-comparison">
      <summary>{t('ui.review_with_newer_data')}</summary>
      <p>{t('ui.check_the_source_using_this_work_s_saved_month_and_line_filters')}</p>
      <button
        type="button"
        className="ops-button"
        onClick={() => void checkSource()}
        disabled={busy || checking || saving}
      >
        {checking ? (
          <LoaderCircle size={15} className="ops-spin" aria-hidden="true" />
        ) : (
          <RotateCcw size={15} aria-hidden="true" />
        )}
        {checking ? t('ui.checking_source') : t('ui.check_source_now')}
      </button>
      {comparison && (
        <>
          <ComparisonResult comparison={comparison} />
          {comparison.status !== 'unavailable' && comparison.scope_unchanged && !saved && (
            <button
              type="button"
              className="ops-button"
              onClick={() => void saveComparison()}
              disabled={busy || checking || saving || !comparison.current_context_id}
            >
              {saving ? (
                <LoaderCircle size={15} className="ops-spin" aria-hidden="true" />
              ) : (
                <ArrowUpRight size={15} aria-hidden="true" />
              )}
              {saving ? t('ui.saving_comparison') : t('ui.save_comparison_to_this_work')}
            </button>
          )}
        </>
      )}
      <p className="ops-feedback" role="status" aria-live="polite">
        {message}
      </p>
      <p className="ops-form-feedback" role="alert">
        {error}
      </p>
    </details>
  );
}
