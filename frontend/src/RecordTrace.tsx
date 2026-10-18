import { useEffect, useRef, useState } from 'react';
import { X } from 'lucide-react';
import { request } from './api';
import { useI18n } from './i18n';

type Trace = {
  raw_records: { source_row: number; raw: Record<string, unknown> }[];
  canonical_records: Record<string, unknown>[];
  processed_records: Record<string, unknown>[];
  normalization_audit: Record<string, unknown>[];
  scenario_audit: Record<string, unknown>[];
  truncated: boolean;
};

export default function RecordTrace({
  url,
  defectId,
  onClose,
}: {
  url: string;
  defectId: number;
  onClose: () => void;
}) {
  const { t, label, number } = useI18n();
  const [trace, setTrace] = useState<Trace>();
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  const panel = useRef<HTMLElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  useEffect(() => {
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  }, [defectId]);
  useEffect(() => {
    return () => {
      if (opener.current?.isConnected) opener.current.focus({ preventScroll: true });
    };
  }, []);
  useEffect(() => {
    panel.current?.focus();
    setTrace(undefined);
    setError('');
    const abort = new AbortController();
    void request<Trace>(`${url}?defect_id=${defectId}`, { signal: abort.signal })
      .then((value) => {
        if (!abort.signal.aborted) setTrace(value);
      })
      .catch((failure: unknown) => {
        if (!abort.signal.aborted)
          setError(failure instanceof Error ? failure.message : String(failure));
      });
    return () => abort.abort();
  }, [url, defectId, retry]);
  return (
    <section
      ref={panel}
      tabIndex={-1}
      className="record-trace"
      aria-labelledby="record-trace-title"
    >
      <div className="evidence-heading">
        <h3 id="record-trace-title">{t('ui.record_trace_title', { id: defectId })}</h3>
        <button
          type="button"
          className="icon-button"
          onClick={onClose}
          aria-label={t('ui.close_record_trace')}
        >
          <X size={17} />
        </button>
      </div>
      <p>{t('ui.record_trace_explanation')}</p>
      {error && (
        <div className="dashboard-alert" role="alert">
          <p>{label(error)}</p>
          <button
            type="button"
            className="text-button"
            onClick={() => setRetry((value) => value + 1)}
          >
            {t('ui.retry_trace')}
          </button>
        </div>
      )}
      {!trace && !error && <p role="status">{t('ui.loading_record_trace')}</p>}
      {trace && (
        <>
          <div className="trace-source-grid">
            {trace.raw_records.map((record) => (
              <div key={record.source_row}>
                <strong>{t('ui.original_http_row', { row: number(record.source_row) })}</strong>
                <pre>{JSON.stringify(record.raw, null, 2)}</pre>
              </div>
            ))}
            <div>
              <strong>{t('ui.canonical_event')}</strong>
              <pre>{JSON.stringify(trace.canonical_records, null, 2)}</pre>
            </div>
          </div>
          <h4>{t('ui.field_decisions')}</h4>
          {[...trace.normalization_audit, ...trace.scenario_audit].length ? (
            <div
              className="table-scroll"
              tabIndex={0}
              role="region"
              aria-label={t('ui.field_decisions')}
            >
              <table>
                <thead>
                  <tr>
                    <th>{t('ui.field')}</th>
                    <th>{t('ui.original_value')}</th>
                    <th>{t('ui.processed_value')}</th>
                    <th>{t('ui.policy_decision')}</th>
                  </tr>
                </thead>
                <tbody>
                  {[...trace.normalization_audit, ...trace.scenario_audit].map((entry, index) => (
                    <tr key={index}>
                      <td>
                        <code>{String(entry.field || '')}</code>
                      </td>
                      <td>
                        <code>{JSON.stringify(entry.raw_value)}</code>
                      </td>
                      <td>
                        <code>{JSON.stringify(entry.processed_value)}</code>
                      </td>
                      <td>
                        <strong>{String(entry.code || '')}</strong>
                        <small>{String(entry.detail || '')}</small>
                        <small>{String(entry.policy_version || '')}</small>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p>{t('ui.no_field_changes')}</p>
          )}
          {trace.truncated && <p role="status">{t('ui.trace_truncated')}</p>}
        </>
      )}
    </section>
  );
}
