import { Check, ChevronDown, CircleAlert, Clock, Lightbulb, LoaderCircle } from 'lucide-react';
import { createContext, memo, useContext, useEffect, useMemo, useState } from 'react';
import { createI18n, useI18n } from './i18n';
import { analysisStatus } from './EventView';
import type { Locale, Translate } from './i18n';
import type { FlowCall, FlowPart, FlowRun, FlowStep } from './agent-flow';
import { completedFlowStates, stoppedFlowStates } from './agent-flow';
import { flowSummaryDisplay } from './flow-summary-display';

// Presentation only. Every value shown here comes from a stored provider or
// receipt event; this view never derives a number of its own.

const FlowLanguage = createContext(createI18n('en'));

function duration(value: number | undefined): string {
  if (!value || value <= 0) return '';
  return value < 1000 ? Math.round(value) + 'ms' : (value / 1000).toFixed(1) + 's';
}

function friendlyToolLabel(name: string, locale: string = 'vi'): string {
  const labelsVi: Record<string, string> = {
    dashboard_context: 'Đọc phạm vi Dashboard',
    analysis_query: 'Truy vấn sổ cái chất lượng',
    analysis_compare: 'Đối chiếu kịch bản',
    analysis_sql: 'Phân tích SQL',
    scenario_compare: 'Thử nghiệm giả định',
    analysis_records: 'Truy xuất bản ghi lỗi',
    report_table: 'Bảng dữ liệu nguồn',
    report_chart: 'Tạo biểu đồ',
    ui_command: 'Điều khiển giao diện',
    investigation_update: 'Cập nhật hồ sơ điều tra',
    local_python_repl: 'Chạy mã Python',
    local_sql_engine: 'Thực thi truy vấn SQL',
    ordered_analysis: 'Phân tích tuần tự',
    memory_read: 'Đọc bộ nhớ ngữ cảnh',
    memory_write: 'Ghi nhớ ngữ cảnh',
    web_search: 'Tìm kiếm web',
    web_search_call: 'Tìm kiếm web',
    file_search: 'Tìm kiếm tệp',
    file_search_call: 'Tìm kiếm tệp',
    code_interpreter_call: 'Thực thi mã',
    operations_list_cases: 'Rà soát công việc theo dõi',
    operations_create_draft: 'Chuẩn bị bản nháp công việc',
    operations_update_draft: 'Chỉnh sửa bản nháp công việc',
    evidence_detail: 'Tra cứu chi tiết bằng chứng',
  };
  const labelsEn: Record<string, string> = {
    dashboard_context: 'Read Dashboard scope',
    analysis_query: 'Query the quality ledger',
    analysis_compare: 'Compare scenarios',
    analysis_sql: 'Analyze with SQL',
    scenario_compare: 'Test a scenario',
    analysis_records: 'Look up defect records',
    report_table: 'Source table',
    report_chart: 'Create chart',
    ui_command: 'Update the workspace view',
    investigation_update: 'Update the investigation',
    local_python_repl: 'Run Python code',
    local_sql_engine: 'Run a SQL query',
    ordered_analysis: 'Run the analysis',
    memory_read: 'Recall context',
    memory_write: 'Save context',
    web_search: 'Search the web',
    web_search_call: 'Search the web',
    file_search: 'Search files',
    file_search_call: 'Search files',
    code_interpreter_call: 'Run code',
    operations_list_cases: 'Review follow-up work',
    operations_create_draft: 'Prepare action draft',
    operations_update_draft: 'Revise action draft',
    evidence_detail: 'Inspect evidence details',
  };
  const map = locale === 'vi' ? labelsVi : labelsEn;
  return map[name] || name;
}

function extractRationale(command: string | undefined): string | undefined {
  if (!command) return undefined;
  try {
    const data = JSON.parse(command);
    if (data && typeof data === 'object') {
      if (typeof data.rationale === 'string' && data.rationale.trim()) return data.rationale.trim();
      if (typeof data.plan === 'string' && data.plan.trim()) return data.plan.trim();
    }
  } catch {
    // not JSON
  }
  return undefined;
}

type MicroKpi = { label: string; value: string };
const microKpiFormatters = {
  en: new Intl.NumberFormat('en-US', { maximumFractionDigits: 20 }),
  vi: new Intl.NumberFormat('vi-VN', { maximumFractionDigits: 20 }),
};

function formatKpiLabel(key: string, locale: string): string {
  const viLabels: Record<string, string> = {
    event_count: 'Sự kiện',
    critical_count: 'Nghiêm trọng',
    total_cost: 'Tổng chi phí',
    cost: 'Chi phí',
    defect_rate: 'Tỷ lệ lỗi',
    rows: 'Bản ghi',
    count: 'Số lượng',
    total: 'Tổng cộng',
    anomalies: 'Bất thường',
    findings: 'Phát hiện',
    score: 'Điểm số',
    rate: 'Tỷ lệ',
  };
  const enLabels: Record<string, string> = {
    event_count: 'Events',
    critical_count: 'Critical',
    total_cost: 'Total Cost',
    cost: 'Cost',
    defect_rate: 'Defect Rate',
    rows: 'Rows',
    count: 'Count',
    total: 'Total',
    anomalies: 'Anomalies',
    findings: 'Findings',
    score: 'Score',
    rate: 'Rate',
  };
  const map = locale === 'vi' ? viLabels : enLabels;
  if (map[key]) return map[key];
  return key.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

function extractMicroKpis(status: FlowCall['status'], output: string, locale: string): MicroKpi[] {
  if (status !== 'completed' || !output) return [];
  try {
    const data = JSON.parse(output);
    if (!data || typeof data !== 'object' || data.ok === false) return [];
    const source = data.kpis && typeof data.kpis === 'object' ? data.kpis : data;
    // Only explicit business counts have an unambiguous display unit here.
    // Arbitrary numeric receipt fields include IDs, limits, costs and ratios.
    return ['event_count', 'critical_count', 'rows', 'count']
      .filter((key) => typeof source[key] === 'number' && Number.isFinite(source[key]))
      .map((key) => ({
        label: formatKpiLabel(key, locale),
        value: microKpiFormatters[locale === 'vi' ? 'vi' : 'en'].format(source[key]),
      }));
  } catch {
    return [];
  }
}

function noteLabel(label: string, t: Translate): string {
  const keys: Record<string, string> = {
    plan: 'ui.agent_flow_note_plan',
    review: 'ui.agent_flow_note_review',
    file: 'ui.agent_flow_note_file',
    steering: 'ui.agent_flow_note_steering',
    steering_received: 'ui.agent_flow_note_steering_received',
    steering_submitted: 'ui.agent_flow_note_steering_submitted',
    steering_failed: 'ui.agent_flow_note_steering_failed',
    reconnected: 'ui.agent_flow_note_reconnected',
    retry: 'ui.agent_flow_note_retry',
    stopped: 'ui.agent_flow_note_stopped',
    cancel_requested: 'ui.agent_flow_note_cancel_requested',
    knowledge_prepare: 'ui.agent_flow_note_knowledge',
    memory: 'ui.agent_flow_note_memory',
    memory_recall: 'ui.agent_flow_note_memory_recall',
    recalled: 'ui.agent_flow_note_memory_recall',
    memory_store: 'ui.agent_flow_note_memory_store',
    memory_update: 'ui.agent_flow_note_memory_update',
    provider: 'ui.agent_flow_note_provider',
  };
  return keys[label]
    ? t(keys[label])
    : /^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$/.test(label)
      ? t('ui.agent_flow_note_update')
      : label;
}

function noteDetail(step: Extract<FlowStep, { kind: 'note' }>, t: Translate): string {
  if (step.label === 'stopped' && step.detail === 'failed') return t('ui.agent_flow_stop_failed');
  if (step.label === 'cancel_requested' && step.detail === 'unconfirmed')
    return t('ui.agent_flow_unconfirmed');
  if (step.label === 'provider' || step.label === 'cancel_requested') {
    const phases: Record<string, string> = {
      created: 'ui.agent_flow_phase_created',
      in_progress: 'ui.agent_flow_phase_in_progress',
      requires_action: 'ui.agent_flow_phase_requires_action',
      idle: 'ui.agent_flow_phase_idle',
      completed: 'ui.agent_flow_phase_completed',
      failed: 'ui.agent_flow_phase_failed',
      cancelled: 'ui.agent_flow_phase_cancelled',
    };
    return phases[step.detail] ? t(phases[step.detail]) : step.detail;
  }
  if (step.label !== 'review') return step.detail;
  const reviews: Record<string, string> = {
    verified: 'ui.agent_flow_review_verified',
    unverified: 'ui.agent_flow_review_unverified',
    retrying: 'ui.agent_flow_review_retrying',
  };
  return reviews[step.detail] ? t(reviews[step.detail]) : step.detail;
}

function callStatus(call: FlowCall, t: Translate): string {
  if (call.status === 'completed') return t('ui.agent_flow_completed');
  if (call.status === 'failed') return t('ui.agent_flow_failed');
  if (call.status === 'cancelled') return t('ui.agent_flow_cancelled');
  if (call.status === 'pending') return t('ui.agent_flow_pending');
  if (call.status === 'unconfirmed') return t('ui.agent_flow_unconfirmed');
  return t('ui.agent_flow_running');
}

function StepIcon({ step }: { step: FlowStep }) {
  if (step.kind === 'failure') return <CircleAlert size={14} />;
  if (step.kind === 'call')
    return step.status === 'completed' ? (
      <Check size={14} />
    ) : step.status === 'failed' ? (
      <CircleAlert size={14} />
    ) : step.status === 'unconfirmed' ||
      step.status === 'pending' ||
      step.status === 'cancelled' ? (
      <Clock size={14} />
    ) : (
      <LoaderCircle size={14} />
    );
  if (step.kind === 'thinking')
    return step.done ? (
      <Check size={14} />
    ) : step.stopped ? (
      <Clock size={14} />
    ) : (
      <LoaderCircle size={14} />
    );
  if (step.kind === 'answer') return <Check size={14} />;
  return <Clock size={13} />;
}

function LiveTimer({ startedAt, locale }: { startedAt: number; locale: Locale }) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  return (
    <span
      className="flow-live-timer"
      role="timer"
      aria-live="off"
      aria-label={
        locale === 'vi' ? 'Thời gian từ cập nhật đầu tiên' : 'Time since the first update'
      }
    >
      {Math.floor(Math.max(0, (now - startedAt) / 1000))}s
    </span>
  );
}

const ThinkingPart = memo(function ThinkingPart({ part }: { part: FlowPart }) {
  const { locale } = useContext(FlowLanguage);
  const displayed = flowSummaryDisplay(part);
  return (
    <div className="flow-thought" lang={displayed.displayLanguage}>
      {displayed.displaySource !== 'translated_provider_summary' && (
        <span className="flow-summary-source" lang={locale}>
          {displayed.displaySource === 'task_status'
            ? locale === 'vi'
              ? 'Trạng thái tác vụ'
              : 'Task status'
            : locale === 'vi'
              ? 'Tóm tắt công khai gốc của mô hình'
              : 'Original public model summary'}
        </span>
      )}
      {displayed.title && <strong>{displayed.title}</strong>}
      {displayed.body && <p>{displayed.body}</p>}
    </div>
  );
});

function ThinkingStep({ step }: { step: Extract<FlowStep, { kind: 'thinking' }> }) {
  const { t, locale } = useContext(FlowLanguage);
  const [open, setOpen] = useState(true);
  const parts = useMemo(() => step.parts.map((part) => flowSummaryDisplay(part)), [step.parts]);
  const empty = parts.every((part) => !part.title && !part.body);
  const isActive = !step.done && !step.stopped;

  const recordedDuration = duration(step.durationMs);
  const sectionCount = parts.filter(
    (part) => part.displaySource !== 'task_status' && (part.title || part.body),
  ).length;
  const hasTaskStatus = parts.some((part) => part.displaySource === 'task_status');

  return (
    <li
      className={
        'flow-step is-thinking ' +
        (step.done ? 'is-done' : step.stopped ? 'is-stopped' : 'is-active') +
        (open ? ' is-open' : '')
      }
    >
      <span className="flow-bullet" aria-hidden="true">
        <StepIcon step={step} />
      </span>
      <div className="flow-body">
        <div className="flow-headline">
          <button
            type="button"
            className="flow-thinking-pill"
            onClick={() => setOpen((prev) => !prev)}
            aria-expanded={open}
          >
            <span className="flow-kind">
              {locale === 'vi' ? 'Tóm tắt suy luận' : 'Reasoning summary'}
            </span>
            <span className="flow-pill-badge">
              {hasTaskStatus && !sectionCount
                ? locale === 'vi'
                  ? 'Trạng thái tác vụ'
                  : 'Task status'
                : locale === 'vi'
                  ? `${sectionCount} phần tóm tắt`
                  : `${sectionCount} summary ${sectionCount === 1 ? 'section' : 'sections'}`}
              {recordedDuration && ` · ${recordedDuration}`}
            </span>
            <ChevronDown
              className={'flow-chevron ' + (open ? 'is-open' : '')}
              size={13}
              aria-hidden="true"
            />
          </button>
          {isActive && step.startedAt !== undefined && (
            <LiveTimer startedAt={step.startedAt} locale={locale} />
          )}
          {step.stopped && <small>{t('ui.agent_flow_unconfirmed')}</small>}
          {step.simulation && <small>{t('ui.agent_flow_simulated')}</small>}
        </div>
        {empty ? (
          <p className="flow-dim">{t('ui.agent_flow_encrypted_reasoning')}</p>
        ) : (
          <div className="flow-thoughts" hidden={!open}>
            {step.parts.map((part) => (
              <ThinkingPart part={part} key={part.index} />
            ))}
          </div>
        )}
      </div>
    </li>
  );
}

function CallStep({ step }: { step: FlowCall }) {
  const { t, locale } = useContext(FlowLanguage);
  const [receiptOpen, setReceiptOpen] = useState(false);
  const length = duration(step.durationMs);
  const rationale = step.rationale || extractRationale(step.command);
  // Streaming reprojects the whole run, including unchanged completed receipts.
  const microKpis = useMemo(
    () => extractMicroKpis(step.status, step.output, locale),
    [step.status, step.output, locale],
  );

  return (
    <li className={'flow-step is-call status-' + step.status}>
      <span className="flow-bullet" aria-hidden="true">
        <StepIcon step={step} />
      </span>
      <div className="flow-body">
        <p className="flow-headline">
          <span className="flow-friendly-name">{friendlyToolLabel(step.name, locale)}</span>
          <span className="flow-state">{callStatus(step, t)}</span>
          {length && <small>{length}</small>}
        </p>
        {rationale && (
          <div className="flow-rationale">
            <span className="flow-rationale-badge">
              <Lightbulb size={12} aria-hidden="true" />
              <span>{locale === 'vi' ? 'Mục đích' : 'Purpose'}</span>
            </span>
            <p className="flow-rationale-text">{rationale}</p>
          </div>
        )}
        {step.outcome && (
          <div className="flow-outcome-block">
            {microKpis.length > 0 && (
              <div
                className="flow-micro-kpis"
                aria-label={
                  locale === 'vi' ? 'Số liệu từ kết quả công cụ' : 'Figures from the tool result'
                }
              >
                {microKpis.map((kpi, idx) => (
                  <div className="flow-micro-kpi-chip" key={idx}>
                    <span className="flow-kpi-label">{kpi.label}</span>
                    <span className="flow-kpi-val">{kpi.value}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
        {(step.command ||
          step.output ||
          step.executionId ||
          step.codeSha256 ||
          step.implementationSource) && (
          <details
            className="flow-receipt"
            onToggle={(event) => setReceiptOpen(event.currentTarget.open)}
          >
            <summary>
              {t('ui.agent_flow_receipt')}
              <ChevronDown className="flow-chevron" size={13} aria-hidden="true" />
            </summary>
            <code className="flow-name">{step.name || t('ui.agent_flow_tool')}</code>
            {step.command && (
              <pre className={'flow-command ' + (step.streaming ? 'is-streaming' : '')}>
                <code>{step.command}</code>
              </pre>
            )}
            <p className="flow-outcome">
              <span aria-hidden="true">⎿</span>
              <span>{step.outcome}</span>
            </p>
            {step.output && (
              <pre>
                <code>{step.output}</code>
              </pre>
            )}
            {receiptOpen && step.implementationSource && (
              <pre>
                <code>{step.implementationSource}</code>
              </pre>
            )}
            {(step.executionId || step.codeSha256) && (
              <p className="flow-identity">
                {step.executionId && <span>exec {step.executionId.slice(0, 12)}</span>}
                {step.codeSha256 && <span>sha {step.codeSha256.slice(0, 12)}</span>}
              </p>
            )}
          </details>
        )}
      </div>
    </li>
  );
}

const Step = memo(function Step({ step }: { step: FlowStep }) {
  const { t, locale } = useContext(FlowLanguage);
  if (step.kind === 'thinking') {
    return <ThinkingStep step={step} />;
  }
  if (step.kind === 'call') {
    return <CallStep step={step} />;
  }
  if (step.kind === 'answer') {
    return (
      <li className="flow-step is-answer">
        <span className="flow-bullet" aria-hidden="true">
          <StepIcon step={step} />
        </span>
        <div className="flow-body">
          <p className="flow-headline">
            <span className="flow-kind">{t('ui.agent_flow_answer')}</span>
            {step.simulation && <small>{t('ui.agent_flow_simulated')}</small>}
          </p>
          <p className="flow-dim">{t('ui.agent_flow_answer_below')}</p>
        </div>
      </li>
    );
  }
  if (step.kind === 'draft') {
    return (
      <li className="flow-step is-draft">
        <span className="flow-bullet" aria-hidden="true">
          <Clock size={14} />
        </span>
        <div className="flow-body">
          <div className="flow-headline">
            <span className="flow-kind">
              {step.messagePhase === 'commentary'
                ? locale === 'vi'
                  ? 'Cập nhật tiến độ'
                  : 'Progress update'
                : t('ui.agent_flow_draft')}
            </span>
            {step.messagePhase !== 'commentary' && (
              <small>{t('ui.agent_flow_draft_unverified')}</small>
            )}
          </div>
          <p className="flow-dim">{step.text}</p>
        </div>
      </li>
    );
  }
  if (step.kind === 'failure') {
    return (
      <li className="flow-step is-failure">
        <span className="flow-bullet" aria-hidden="true">
          <StepIcon step={step} />
        </span>
        <div className="flow-body">
          <p className="flow-headline">
            <span className="flow-kind">{t('ui.agent_flow_stopped')}</span>
            {step.code && <small>{step.code}</small>}
          </p>
          <p className="flow-dim">{step.message}</p>
        </div>
      </li>
    );
  }
  return (
    <li className={'flow-step is-note tone-' + step.tone}>
      <span className="flow-bullet" aria-hidden="true">
        <StepIcon step={step} />
      </span>
      <div className="flow-body">
        <p className="flow-headline">
          <span className="flow-kind">{noteLabel(step.label, t)}</span>
        </p>
        {step.detail && <p className="flow-dim">{noteDetail(step, t)}</p>}
      </div>
    </li>
  );
});

function currentStepSummary(step: FlowStep, t: Translate, locale: string) {
  if (step.kind === 'call')
    return {
      label: friendlyToolLabel(step.name, locale),
      detail: step.rationale || extractRationale(step.command),
    };
  // The summary is shown in full in the expanded timeline directly below.
  if (step.kind === 'thinking') {
    let latestPart;
    for (let index = step.parts.length - 1; index >= 0; index -= 1) {
      const part = flowSummaryDisplay(step.parts[index]);
      if (part.title || part.body) {
        latestPart = part;
        break;
      }
    }
    return {
      label: locale === 'vi' ? 'Tóm tắt suy luận' : 'Reasoning summary',
      detail: latestPart?.displaySource === 'task_status' ? latestPart.body : undefined,
    };
  }
  if (step.kind === 'note') {
    if (step.label === 'review')
      return {
        label: noteLabel(step.label, t),
        detail:
          step.tone === 'info' && step.detail === 'verified'
            ? t('ui.agent_flow_review_verified')
            : step.tone === 'warn'
              ? locale === 'vi'
                ? 'Đang rà soát lại câu trả lời trước khi gửi.'
                : 'Reviewing the answer again before delivery.'
              : locale === 'vi'
                ? 'Đang rà soát câu trả lời trước khi gửi.'
                : 'Reviewing the answer before delivery.',
      };
    // Context identifiers and artifact paths remain available in the timeline,
    // while the primary update describes their recorded purpose.
    const contextNote =
      step.label === 'file' ||
      step.label === 'knowledge_prepare' ||
      step.label === 'recalled' ||
      step.label.startsWith('memory');
    return {
      label: noteLabel(step.label, t),
      detail: contextNote ? undefined : noteDetail(step, t),
    };
  }
  if (step.kind === 'draft')
    return {
      label:
        step.messagePhase === 'commentary'
          ? locale === 'vi'
            ? 'Cập nhật tiến độ'
            : 'Progress update'
          : t('ui.agent_flow_draft'),
      detail: step.messagePhase === 'commentary' ? step.text : t('ui.agent_flow_draft_unverified'),
    };
  if (step.kind === 'failure') return { label: t('ui.agent_flow_stopped'), detail: step.message };
  return { label: t('ui.agent_flow_answer'), detail: t('ui.agent_flow_answer_below') };
}

export default function AgentFlow({ run, language }: { run: FlowRun; language?: Locale }) {
  const page = useI18n();
  const locale = run.language ?? language ?? page.locale;
  const i18n = useMemo(() => createI18n(locale), [locale]);
  return (
    <FlowLanguage.Provider value={i18n}>
      <RunFlow run={run} />
    </FlowLanguage.Provider>
  );
}

function RunFlow({ run }: { run: FlowRun }) {
  const { t, number, locale } = useContext(FlowLanguage);
  // The native disclosure retains a person's choice across live updates. A
  // different run gets its own default without moving focus or scrolling.
  const [disclosure, setDisclosure] = useState<{ runId: string; open: boolean } | null>(null);
  const halted = stoppedFlowStates.has(run.state);
  const active = !halted && !completedFlowStates.has(run.state);
  // Keep the full event projection intact, while omitting completed reasoning
  // items without public content from the visible timeline.
  const visibleSteps = run.steps.filter(
    (step) =>
      !(step.kind === 'note' && step.label === 'provider') &&
      !(
        step.kind === 'thinking' &&
        step.done &&
        !step.simulation &&
        step.parts.every((part) => !part.raw && !part.title && !part.body)
      ),
  );
  const meta: string[] = [];
  if (run.compactions)
    meta.push(t('ui.agent_flow_compaction', { value0: number(run.compactions) }));
  const providerStatuses = [
    ...new Set(
      run.steps.flatMap((step) =>
        step.kind === 'note' && step.label === 'provider' ? [noteDetail(step, t)] : [],
      ),
    ),
  ].filter(Boolean);
  const calls = visibleSteps.filter((step) => step.kind === 'call');
  const completedCalls = calls.filter((step) => step.status === 'completed').length;
  const callCount = calls.length
    ? locale === 'vi'
      ? `${number(completedCalls)}/${number(calls.length)} lượt công cụ hoàn tất`
      : `${number(completedCalls)} of ${number(calls.length)} tool calls completed`
    : locale === 'vi'
      ? 'Chưa ghi nhận lượt công cụ'
      : 'No tool calls recorded';
  const currentStep = visibleSteps.at(-1);
  const current = currentStep ? currentStepSummary(currentStep, t, locale) : undefined;
  const issue = [...visibleSteps]
    .reverse()
    .find((step) => step.kind === 'failure' || (step.kind === 'call' && step.status === 'failed'));
  const issueDetail =
    issue?.kind === 'failure' ? issue.message : issue?.kind === 'call' ? issue.outcome : '';
  const timelineOpen = disclosure?.runId === run.runId ? disclosure.open : true;
  return (
    <section
      className={'agent-flow ' + (halted ? 'is-stopped' : active ? 'is-active' : 'is-done')}
      lang={locale}
      aria-label={t('ui.agent_flow_label')}
    >
      <header className="agent-flow-head">
        <div className="agent-flow-heading">
          <span className="agent-flow-title">
            {halted ? (
              <CircleAlert size={15} aria-hidden="true" />
            ) : active ? (
              <LoaderCircle className="spin" size={15} aria-hidden="true" />
            ) : (
              <Check size={15} aria-hidden="true" />
            )}
            {t('ui.agent_flow_label')}
          </span>
          <span className="flow-state" role="status" aria-atomic="true">
            {analysisStatus(run.state, t)}
          </span>
        </div>
        {(run.model || run.effort || run.fastMode !== undefined) && (
          <div
            className="agent-flow-head-meta"
            role="group"
            aria-label={
              locale === 'vi'
                ? 'Mô hình, mức suy luận và Fast mode của lượt này'
                : 'Model, reasoning effort and Fast mode for this run'
            }
          >
            {run.model && <span className="agent-flow-model">{run.model}</span>}
            {run.effort && (
              <span className="agent-flow-effort">
                {locale === 'vi' ? 'Suy luận' : 'Reasoning'}: {run.effort}
              </span>
            )}
            {run.fastMode !== undefined && (
              <span
                className="agent-flow-fast-mode"
                title={
                  locale === 'vi'
                    ? 'Cấu hình Fast mode khi bắt đầu lượt này'
                    : 'Fast mode setting when this run started'
                }
              >
                Fast mode: {run.fastMode ? 'on' : 'off'}
              </span>
            )}
          </div>
        )}
      </header>
      {active && (
        <div className="agent-flow-current">
          <span className="agent-flow-current-label">
            {locale === 'vi' ? 'Cập nhật gần nhất' : 'Latest update'}
          </span>
          <p className="agent-flow-current-title" role="status" aria-atomic="true">
            {current?.label || t('ui.agent_flow_waiting')}
            {currentStep?.kind === 'call' && (
              <span className="flow-state">{callStatus(currentStep, t)}</span>
            )}
          </p>
          {current?.detail && <p className="agent-flow-current-detail">{current.detail}</p>}
        </div>
      )}
      {(halted || issue) && (
        <div className="agent-flow-alert">
          <CircleAlert size={15} aria-hidden="true" />
          <div>
            <strong>
              {halted
                ? analysisStatus(run.state, t)
                : locale === 'vi'
                  ? 'Có bước xử lý gặp lỗi'
                  : 'A processing step encountered an issue'}
            </strong>
            {issueDetail && <p>{issueDetail}</p>}
          </div>
        </div>
      )}
      {(visibleSteps.length > 0 || meta.length > 0 || providerStatuses.length > 0) && (
        <details
          className="agent-flow-timeline"
          open={timelineOpen}
          onToggle={(event) => {
            const open = event.currentTarget.open;
            if (open !== timelineOpen) setDisclosure({ runId: run.runId, open });
          }}
        >
          <summary className="agent-flow-disclosure">
            <span>{locale === 'vi' ? 'Các bước xử lý' : 'Processing steps'}</span>
            <span
              className="agent-flow-counts"
              data-completed-calls={completedCalls}
              data-total-calls={calls.length}
            >
              {callCount}
            </span>
            <ChevronDown className="flow-chevron" size={14} aria-hidden="true" />
          </summary>
          <ol className="agent-flow-steps" aria-live="off">
            {visibleSteps.map((step) => (
              <Step step={step} key={run.runId + ':' + step.id} />
            ))}
          </ol>
          {(meta.length > 0 || providerStatuses.length > 0) && (
            <div className="agent-flow-context">
              {meta.length > 0 && <p className="agent-flow-meta">{meta.join(' · ')}</p>}
              {providerStatuses.length > 0 && (
                <p className="agent-flow-meta">{providerStatuses.join(' · ')}</p>
              )}
            </div>
          )}
        </details>
      )}
    </section>
  );
}
