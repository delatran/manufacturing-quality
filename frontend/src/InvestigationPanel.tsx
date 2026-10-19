import { useEffect, useRef, useState } from 'react';
import { ArrowDownToLine, BookOpen, Save } from 'lucide-react';
import { artifactUrl, request } from './api';
import { applicationUrl } from './app-url';
import { useI18n } from './i18n';
import type { WorkspacePanel } from './workspace-state';

type Finding = {
  id: string;
  text: string;
  status: string;
  evidence?: { execution_id: string; path: string; value?: unknown }[];
};
type Hypothesis = { id: string; text: string; status: string };
type Priority = { id: string; evidence: string; action: string; owner: string; metric: string };
export type Investigation = {
  session_id: string;
  version: number;
  question: string;
  summary: string;
  context_id?: string;
  dataset_version?: string;
  policy_version?: string;
  content_status?: string;
  readonly unit_notes?: Readonly<Partial<Record<'en' | 'vi', string>>>;
  findings: Finding[];
  hypotheses: Hypothesis[];
  next_steps: string[];
  priorities: Priority[];
  artifacts: {
    run_id: string;
    path: string;
    name: string;
    sha256: string;
    url: string;
    context_id?: string;
  }[];
};

export function InvestigationUnitNotes({ notes }: { notes: Investigation['unit_notes'] }) {
  const { locale, t } = useI18n();
  const text = notes?.[locale];
  if (!text?.trim()) return null;
  return (
    <aside className="investigation-unit-notes" aria-labelledby="investigation-unit-notes-heading">
      <strong id="investigation-unit-notes-heading">{t('ui.investigation_units')}</strong>
      <p>{text}</p>
    </aside>
  );
}

export function buildInvestigationPatch(draft: Investigation, saved?: Investigation) {
  const patch: Record<string, unknown> = { expected_version: draft.version };
  for (const key of ['question', 'summary', 'hypotheses', 'next_steps', 'priorities'] as const) {
    if (JSON.stringify(draft[key]) !== JSON.stringify(saved?.[key])) patch[key] = draft[key];
  }
  if (JSON.stringify(draft.findings) !== JSON.stringify(saved?.findings))
    patch.findings = draft.findings.map(({ id, text }) => ({ id, text }));
  return patch;
}

export default function InvestigationPanel({
  sessionId,
  revision,
  focus,
  currentContextId,
}: {
  sessionId?: string;
  revision: number;
  focus?: { id: number; panel: WorkspacePanel };
  currentContextId?: string;
}) {
  const { locale, t, label, number } = useI18n();
  const [saved, setSaved] = useState<Investigation>();
  const [draft, setDraft] = useState<Investigation>();
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const [open, setOpen] = useState(false);
  const panel = useRef<HTMLElement>(null);
  const dirtyRef = useRef(false);
  dirtyRef.current = dirty;
  useEffect(() => {
    if (!focus || !['investigation', 'brief'].includes(focus.panel)) return;
    setOpen(true);
    requestAnimationFrame(() => {
      panel.current?.focus();
      panel.current?.scrollIntoView({ block: 'start' });
    });
  }, [focus]);
  useEffect(() => {
    if (!sessionId) return;
    const abort = new AbortController();
    void request<Investigation>(`/api/sessions/${encodeURIComponent(sessionId)}/investigation`, {
      signal: abort.signal,
    })
      .then((value) => {
        if (abort.signal.aborted) return;
        setSaved(value);
        if (!dirtyRef.current) setDraft(value);
        setError('');
      })
      .catch((failure: unknown) => {
        if (!abort.signal.aborted)
          setError(failure instanceof Error ? failure.message : String(failure));
      });
    return () => abort.abort();
  }, [sessionId, revision, reload]);
  function edit(values: Partial<Investigation>) {
    setDraft((current) => (current ? { ...current, ...values } : current));
    setDirty(true);
  }
  async function save() {
    if (!draft || !sessionId || busy) return;
    setBusy(true);
    setError('');
    try {
      const patch = buildInvestigationPatch(draft, saved);
      const value = await request<Investigation>(
        `/api/sessions/${encodeURIComponent(sessionId)}/investigation`,
        {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(patch),
        },
      );
      setSaved(value);
      setDraft(value);
      setDirty(false);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(false);
    }
  }
  const stale = saved?.context_id && currentContextId && saved.context_id !== currentContextId;
  return (
    <section
      className="investigation-panel"
      id="investigation-panel"
      ref={panel}
      tabIndex={-1}
      aria-labelledby="investigation-heading"
    >
      <button
        type="button"
        className="investigation-toggle"
        aria-expanded={open}
        aria-controls="investigation-body"
        onClick={() => setOpen((value) => !value)}
      >
        <BookOpen size={19} />
        <span>
          <strong id="investigation-heading">{t('ui.investigation_and_handoff_brief')}</strong>
          {saved?.question ? <small>{saved.question}</small> : null}
        </span>
        <span>{open ? t('ui.close') : t('ui.open')}</span>
      </button>
      <div id="investigation-body" hidden={!open}>
        {error && (
          <div role="alert" className="dashboard-alert">
            <p>
              {t('ui.the_investigation_could_not_be_loaded_or_saved_your_edits_remain_')}{' '}
              {label(error)}
            </p>
            <button
              type="button"
              className="text-button"
              onClick={() => setReload((value) => value + 1)}
            >
              {t('ui.reload_saved_version')}
            </button>
          </div>
        )}
        {stale && (
          <p className="investigation-stale" role="status">
            {t('ui.this_investigation_belongs_to_an_earlier_scope_its_findings_do_no')}
          </p>
        )}
        {saved && draft ? (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void save();
            }}
          >
            <div className="investigation-meta">
              <span>
                {t('ui.saved_version')} {number(saved.version)}
              </span>
              <span>{saved.policy_version}</span>
              {saved.content_status && (
                <span className={`investigation-verification-badge status-${saved.content_status}`}>
                  {label(saved.content_status)}
                </span>
              )}
            </div>
            <InvestigationUnitNotes notes={saved.unit_notes} />
            {dirty && saved.version !== draft.version && (
              <p role="status" className="investigation-stale">
                {t('ui.the_saved_investigation_changed_while_you_were_editing_copy_your_')}
              </p>
            )}
            <label>
              {t('ui.investigation_question')}
              <textarea
                rows={2}
                value={draft.question}
                onChange={(event) => edit({ question: event.target.value })}
              />
            </label>
            <label>
              {t('ui.working_summary')}
              <textarea
                rows={3}
                value={draft.summary}
                onChange={(event) => edit({ summary: event.target.value })}
              />
            </label>
            <div className="investigation-columns">
              <div>
                <h3>{t('ui.checked_findings')}</h3>
                {draft.findings.length ? (
                  draft.findings.map((finding, index) => (
                    <label key={finding.id}>
                      <span>
                        <span className={`finding-status-badge status-${finding.status}`}>
                          {label(finding.status)}
                        </span>
                        {finding.evidence?.length
                          ? ` · ${number(finding.evidence.length)} ${t('ui.evidence_references')}`
                          : ''}
                      </span>
                      <textarea
                        rows={3}
                        value={finding.text}
                        onChange={(event) =>
                          edit({
                            findings: draft.findings.map((item, i) =>
                              i === index ? { ...item, text: event.target.value } : item,
                            ),
                          })
                        }
                      />
                      {finding.evidence?.map((evidence) => (
                        <small
                          className="finding-reference"
                          key={`${evidence.execution_id}:${evidence.path}`}
                        >
                          <code>{evidence.execution_id}</code> · <code>{evidence.path}</code>
                        </small>
                      ))}
                    </label>
                  ))
                ) : (
                  <p>{t('ui.no_checked_findings_saved_yet')}</p>
                )}
              </div>
              <div>
                <h3>{t('ui.open_and_tested_hypotheses')}</h3>
                {draft.hypotheses.map((hypothesis, index) => (
                  <label key={hypothesis.id}>
                    <textarea
                      rows={2}
                      value={hypothesis.text}
                      onChange={(event) =>
                        edit({
                          hypotheses: draft.hypotheses.map((item, i) =>
                            i === index ? { ...item, text: event.target.value } : item,
                          ),
                        })
                      }
                    />
                    <select
                      value={hypothesis.status}
                      onChange={(event) =>
                        edit({
                          hypotheses: draft.hypotheses.map((item, i) =>
                            i === index ? { ...item, status: event.target.value } : item,
                          ),
                        })
                      }
                    >
                      {['open', 'rejected', 'supported', 'unresolved'].map((state) => (
                        <option key={state} value={state}>
                          {label(state)}
                        </option>
                      ))}
                    </select>
                  </label>
                ))}
                <button
                  type="button"
                  className="text-button"
                  onClick={() =>
                    edit({
                      hypotheses: [
                        ...draft.hypotheses,
                        { id: `user-${Date.now()}`, text: '', status: 'open' },
                      ],
                    })
                  }
                >
                  {t('ui.add_a_hypothesis')}
                </button>
              </div>
            </div>
            {draft.priorities.length > 0 && (
              <div className="investigation-priorities">
                <h3>{t('ui.handoff_priorities')}</h3>
                {draft.priorities.map((priority, index) => (
                  <fieldset key={priority.id}>
                    <legend>
                      {t('ui.priority')} {number(index + 1)}
                    </legend>
                    {(['evidence', 'action', 'owner', 'metric'] as const).map((key) => (
                      <label key={key}>
                        {key === 'evidence'
                          ? t('ui.evidence')
                          : key === 'action'
                            ? t('ui.action')
                            : key === 'owner'
                              ? t('ui.proposed_owner_role')
                              : t('ui.metric')}
                        <textarea
                          rows={2}
                          value={priority[key]}
                          onChange={(event) =>
                            edit({
                              priorities: draft.priorities.map((item, i) =>
                                i === index ? { ...item, [key]: event.target.value } : item,
                              ),
                            })
                          }
                        />
                      </label>
                    ))}
                  </fieldset>
                ))}
              </div>
            )}
            <label>
              {t('ui.next_steps_one_per_line')}
              <textarea
                rows={3}
                value={draft.next_steps.join('\n')}
                onChange={(event) => edit({ next_steps: event.target.value.split('\n') })}
              />
            </label>
            <div className="evidence-actions">
              <button
                type="submit"
                className="primary-button"
                disabled={!dirty || busy || saved.version !== draft.version}
              >
                <Save size={15} />
                {busy ? t('ui.saving') : t('ui.save_investigation')}
              </button>
              {!dirty && !busy && sessionId && (
                <a
                  className="secondary-button"
                  href={applicationUrl(
                    `/api/sessions/${encodeURIComponent(sessionId)}/investigation/export.json?version=${saved.version}&language=${locale}`,
                  )}
                >
                  <ArrowDownToLine size={15} />
                  {t('ui.export_saved_investigation', { version: number(saved.version) })}
                </a>
              )}
              {dirty && (
                <button
                  type="button"
                  className="text-button"
                  onClick={() => {
                    setDraft(saved);
                    setDirty(false);
                  }}
                >
                  {t('ui.use_saved_version')}
                </button>
              )}
            </div>
            <div className="investigation-artifacts" id="brief-artifacts">
              <h3>{t('ui.registered_briefs_and_artifacts')}</h3>
              {saved.artifacts.length ? (
                saved.artifacts.map((artifact) => {
                  const url = artifactUrl(artifact.url);
                  return url ? (
                    <a
                      key={`${artifact.run_id}:${artifact.path}`}
                      className="secondary-button"
                      href={url}
                    >
                      <ArrowDownToLine size={15} />
                      {artifact.name || artifact.path.split('/').at(-1)}
                    </a>
                  ) : null;
                })
              ) : (
                <p>{t('ui.no_registered_brief_yet_ask_the_analyst_to_prepare_a_handoff_brie')}</p>
              )}
            </div>
          </form>
        ) : (
          !error && <p role="status">{t('ui.loading_the_saved_investigation')}</p>
        )}
      </div>
    </section>
  );
}
