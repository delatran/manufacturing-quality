import { Check, ChevronDown, CircleAlert, Clock, LoaderCircle } from 'lucide-react';
import { useState } from 'react';
import { useI18n } from './i18n';
import { analysisStatus } from './EventView';
import type { Translate } from './i18n';
import type { FlowCall, FlowRun, FlowStep } from './agent-flow';
import { completedFlowStates, stoppedFlowStates } from './agent-flow';

// Presentation only. Every value shown here comes from a stored provider or
// receipt event; this view never derives a number of its own.

function duration(value: number | undefined): string {
  if (!value || value <= 0) return '';
  return value < 1000 ? Math.round(value) + 'ms' : (value / 1000).toFixed(1) + 's';
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
      <LoaderCircle className="spin" size={14} />
    );
  if (step.kind === 'thinking')
    return step.done ? (
      <Check size={14} />
    ) : step.stopped ? (
      <Clock size={14} />
    ) : (
      <LoaderCircle className="spin" size={14} />
    );
  if (step.kind === 'answer') return <Check size={14} />;
  return <Clock size={13} />;
}

function Step({ step }: { step: FlowStep }) {
  const { t } = useI18n();
  const [receiptOpen, setReceiptOpen] = useState(false);
  if (step.kind === 'thinking') {
    const empty = step.parts.every((part) => !part.title && !part.body);
    return (
      <li
        className={
          'flow-step is-thinking ' +
          (step.done ? 'is-done' : step.stopped ? 'is-stopped' : 'is-active')
        }
      >
        <span className="flow-bullet" aria-hidden="true">
          <StepIcon step={step} />
        </span>
        <div className="flow-body">
          <div className="flow-headline">
            <span className="flow-kind">{t('ui.agent_flow_thinking')}</span>
            {step.stopped && <small>{t('ui.agent_flow_unconfirmed')}</small>}
            {step.simulation && <small>{t('ui.agent_flow_simulated')}</small>}
          </div>
          {empty ? (
            <p className="flow-dim">{t('ui.agent_flow_encrypted_reasoning')}</p>
          ) : (
            <div className="flow-thoughts">
              {step.parts.map((part) => (
                <div className="flow-thought" key={part.index}>
                  {part.title && <strong>{part.title}</strong>}
                  {part.body && <p>{part.body}</p>}
                </div>
              ))}
            </div>
          )}
        </div>
      </li>
    );
  }
  if (step.kind === 'call') {
    const length = duration(step.durationMs);
    return (
      <li className={'flow-step is-call status-' + step.status}>
        <span className="flow-bullet" aria-hidden="true">
          <StepIcon step={step} />
        </span>
        <div className="flow-body">
          <p className="flow-headline">
            <code className="flow-name">{step.name || t('ui.agent_flow_tool')}</code>
            <span className="flow-state">{callStatus(step, t)}</span>
            {length && <small>{length}</small>}
          </p>
          {step.command && (
            <pre className={'flow-command ' + (step.streaming ? 'is-streaming' : '')}>
              <code>{step.command}</code>
            </pre>
          )}
          {step.outcome && (
            <p className="flow-outcome">
              <span aria-hidden="true">⎿</span>
              <span>{step.outcome}</span>
            </p>
          )}
          {(step.output || step.executionId || step.codeSha256 || step.implementationSource) && (
            <details
              className="flow-receipt"
              onToggle={(event) => setReceiptOpen(event.currentTarget.open)}
            >
              <summary>
                {t('ui.agent_flow_receipt')}
                <ChevronDown className="flow-chevron" size={13} aria-hidden="true" />
              </summary>
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
            <span className="flow-kind">{t('ui.agent_flow_draft')}</span>
            <small>{t('ui.agent_flow_draft_unverified')}</small>
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
        <div className="flow-body" role="alert">
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
}

export default function AgentFlow({ run }: { run: FlowRun }) {
  const { t, number } = useI18n();
  const halted = stoppedFlowStates.has(run.state);
  const active = !halted && !completedFlowStates.has(run.state);
  // Keep the full event projection intact, while omitting completed reasoning
  // items without public content from the visible timeline.
  const visibleSteps = run.steps.filter(
    (step) =>
      !(
        step.kind === 'thinking' &&
        step.done &&
        !step.simulation &&
        step.parts.every((part) => !part.title && !part.body)
      ),
  );
  const meta: string[] = [];
  if (run.model) meta.push(run.model);
  if (run.effort) meta.push(t('ui.agent_flow_effort') + ' ' + run.effort);
  if (run.background) meta.push(t('ui.agent_flow_background'));
  if (run.compactions)
    meta.push(t('ui.agent_flow_compaction', { value0: number(run.compactions) }));
  return (
    <section
      className={'agent-flow ' + (halted ? 'is-stopped' : active ? 'is-active' : 'is-done')}
      aria-label={t('ui.agent_flow_label')}
    >
      <header className="agent-flow-head">
        <span className="agent-flow-title">
          {halted ? (
            <CircleAlert size={15} />
          ) : active ? (
            <LoaderCircle className="spin" size={15} />
          ) : (
            <Check size={15} />
          )}
          {t('ui.agent_flow_label')}
        </span>
        {(!active || ['queued', 'qa', 'verifying'].includes(run.state)) && (
          <span className="flow-state" role="status">
            {analysisStatus(run.state, t)}
          </span>
        )}
        {meta.length > 0 && <span className="agent-flow-meta">{meta.join(' · ')}</span>}
      </header>
      {run.steps.length === 0 ? (
        <p className="flow-dim agent-flow-empty">{t('ui.agent_flow_waiting')}</p>
      ) : (
        <ol className="agent-flow-steps">
          {visibleSteps.map((step) => (
            <Step step={step} key={step.id} />
          ))}
        </ol>
      )}
    </section>
  );
}
