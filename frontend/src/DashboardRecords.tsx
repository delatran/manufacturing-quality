import { memo, useEffect, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { getRecords } from './api';
import { useDashboardLanguage } from './dashboard-language';
import RecordTrace from './RecordTrace';
import type { DashboardData, RecordsData } from './dashboard-types';
import { Panel } from './DashboardLayout';

const sortOptions = [
  'defect_date',
  'defect_id',
  'product_name',
  'production_line',
  'severity',
  'repair_cost',
] as const;

export const Records = memo(function Records({
  data,
  dateFilter,
  onClearDate,
}: {
  data: DashboardData;
  dateFilter?: string;
  onClearDate?: () => void;
}) {
  const { tr, prose, number, date, sourceLabel } = useDashboardLanguage();

  const [page, setPage] = useState(1);
  const [sort, setSort] = useState('defect_date');
  const [tracedEvent, setTracedEvent] = useState<number>();
  const [direction, setDirection] = useState('desc');
  const [result, setRecords] = useState<RecordsData>();
  const [loadedScope, setLoadedScope] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const recordScope = `${data.links.records}|${dateFilter ?? ''}`;
  const records = loadedScope === recordScope ? result : undefined;
  const previousUrl = useRef(recordScope);
  useEffect(() => {
    setPage(1);
    setTracedEvent(undefined);
  }, [data.context.context_id, dateFilter]);
  useEffect(() => {
    const controller = new AbortController();
    // A new context always starts on page one; never render records from the old view.
    const contextChanged = previousUrl.current !== recordScope;
    previousUrl.current = recordScope;
    setRecords(undefined);
    setLoading(true);
    setError('');
    getRecords(
      data.links.records,
      contextChanged ? 1 : page,
      sort,
      direction,
      controller.signal,
      dateFilter,
    )
      .then((result) => {
        if (!controller.signal.aborted) {
          setRecords(result);
          setLoadedScope(recordScope);
        }
      })
      .catch((failure: unknown) => {
        if (!controller.signal.aborted)
          setError(failure instanceof Error ? failure.message : String(failure));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [data.links.records, recordScope, dateFilter, page, sort, direction, reload]);
  return (
    <Panel
      kicker={tr('ui.the_records_behind_this_view')}
      title={tr('ui.explore_the_filtered_defect_log')}
      className="records-panel"
    >
      {dateFilter && (
        <div className="dashboard-notice" role="status">
          <strong>
            {tr('ui.records_exact_date')}: {date(dateFilter)}
          </strong>
          <p>{tr('ui.records_date_scope_note')}</p>
          {onClearDate && (
            <button type="button" className="text-button" onClick={onClearDate}>
              {tr('ui.clear_record_date')}
            </button>
          )}
        </div>
      )}
      <div className="records-controls">
        <p>
          {records
            ? `${number(records.total)} ${tr(dateFilter ? 'ui.records_in_inspected_date' : 'ui.retained_events_in_this_applied_scope')}`
            : error
              ? tr('ui.records_unavailable')
              : tr('ui.loading_filtered_records')}
        </p>
        <div>
          <label>
            {tr('ui.sort_by')}{' '}
            <select
              value={sort}
              onChange={(event) => {
                setSort(event.target.value);
                setPage(1);
              }}
            >
              {sortOptions.map((key) => (
                <option key={key} value={key}>
                  {tr(`ui.record_sort_${key}`)}
                </option>
              ))}
            </select>
          </label>
          <label className="sr-only" htmlFor="record-order">
            {tr('ui.sort_direction')}
          </label>
          <select
            id="record-order"
            value={direction}
            onChange={(event) => {
              setDirection(event.target.value);
              setPage(1);
            }}
          >
            <option value="desc">{tr('ui.descending')}</option>
            <option value="asc">{tr('ui.ascending')}</option>
          </select>
        </div>
      </div>
      {error && (
        <div className="dashboard-alert" role="alert">
          {prose(error)}
          <button className="text-button" onClick={() => setReload((value) => value + 1)}>
            {tr('ui.retry_records')}
          </button>
        </div>
      )}
      <div
        className="table-scroll"
        aria-busy={loading}
        tabIndex={0}
        role="region"
        aria-label={tr('ui.scrollable_filtered_defect_records')}
      >
        <table className="records-table">
          <caption className="sr-only">{tr('ui.filtered_defect_records')}</caption>
          <thead>
            <tr>
              <th>{tr('ui.id')}</th>
              <th>{tr('ui.date')}</th>
              <th>{tr('ui.product_line')}</th>
              <th>{tr('ui.defect_location')}</th>
              <th>{tr('ui.severity')}</th>
              <th>{tr('ui.inspection')}</th>
              <th>{tr('ui.repair_cost')}</th>
            </tr>
          </thead>
          <tbody>
            {records?.rows.map((row) => (
              <tr key={row.defect_id}>
                <td>
                  <strong>#{row.defect_id}</strong>
                  {data.links.trace && (
                    <button
                      type="button"
                      className="text-button trace-button"
                      onClick={() => setTracedEvent(row.defect_id)}
                    >
                      {tr('ui.trace_record')}
                    </button>
                  )}
                </td>
                <td>{date(row.defect_date)}</td>
                <td>
                  <strong>{sourceLabel(row.product_name)}</strong>
                  <small>
                    #{row.product_id} · {sourceLabel(row.production_line)}
                  </small>
                </td>
                <td>
                  {sourceLabel(row.defect_type)}
                  <small>{sourceLabel(row.defect_location)}</small>
                </td>
                <td>
                  <span className={`severity-label ${row.severity.toLowerCase()}`}>
                    {sourceLabel(row.severity)}
                  </span>
                </td>
                <td>{sourceLabel(row.inspection_method)}</td>
                <td>
                  {row.repair_cost == null ? tr('ui.unknown') : number(row.repair_cost, 2)}
                  <small>{sourceLabel(row.cost_status)}</small>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!loading && records?.total === 0 && (
          <p className="empty-chart">{tr('ui.no_records_match_this_scope')}</p>
        )}
      </div>
      {tracedEvent && data.links.trace && (
        <RecordTrace
          url={data.links.trace}
          defectId={tracedEvent}
          onClose={() => setTracedEvent(undefined)}
        />
      )}
      <div className="pagination">
        <span role="status">
          {loading
            ? tr('ui.loading_records')
            : `${tr('ui.page')} ${records?.page || 1} ${tr('ui.of')} ${records?.page_count || 1}`}
        </span>
        <div>
          <button
            className="secondary-button"
            disabled={loading || page <= 1}
            onClick={() => setPage((value) => value - 1)}
            aria-label={tr('ui.previous_records_page')}
          >
            <ChevronLeft size={15} />
          </button>
          <button
            className="secondary-button"
            disabled={loading || !records || page >= records.page_count}
            onClick={() => setPage((value) => value + 1)}
            aria-label={tr('ui.next_records_page')}
          >
            <ChevronRight size={15} />
          </button>
        </div>
      </div>
    </Panel>
  );
});
