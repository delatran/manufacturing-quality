import { memo, useEffect, useState } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { getRecords } from './api';
import { useDashboardLanguage } from './dashboard-language';
import type { DashboardData, RecordsData } from './dashboard-types';
import { Panel } from './DashboardLayout';
import {
  buildDashboardRecordsTools,
  recordSortOptions as sortOptions,
  useCommittedDashboardState,
} from './dashboard-webmcp';
import { useWebMcpTools } from './webmcp';

type RecordsProps = {
  data: DashboardData;
  dateFilter?: string;
  onClearDate?: () => void;
  visible?: boolean;
  available?: boolean;
};

export const Records = memo(function Records(props: RecordsProps) {
  const [sort, setSort] = useState('defect_date');
  const [direction, setDirection] = useState('desc');
  const scope = JSON.stringify([
    props.data.context.context_id,
    props.data.links.records,
    props.dateFilter,
  ]);
  // Keep sorting preferences, but reset paging atomically with the scope.
  return (
    <ScopedRecords
      key={scope}
      {...props}
      sort={sort}
      onSort={setSort}
      direction={direction}
      onDirection={setDirection}
    />
  );
});

function ScopedRecords({
  data,
  dateFilter,
  onClearDate,
  sort,
  onSort,
  direction,
  onDirection,
  visible = true,
  available = true,
}: RecordsProps & {
  sort: string;
  onSort: (value: string) => void;
  direction: string;
  onDirection: (value: string) => void;
}) {
  const { tr, prose, number, date, sourceLabel } = useDashboardLanguage();

  const [page, setPage] = useState(1);
  const [result, setResult] = useState<{
    key: string;
    records?: RecordsData;
    error?: string;
  }>();
  const [reload, setReload] = useState(0);
  const requestKey = JSON.stringify([page, sort, direction, reload]);
  const currentResult = result?.key === requestKey ? result : undefined;
  const records = currentResult?.records;
  const error = currentResult?.error;
  const loading = currentResult === undefined;
  const getWebMcpState = useCommittedDashboardState({
    data,
    visible,
    available,
    dateFilter,
    page,
    sort,
    direction,
    reload,
    requestKey,
    records,
    error,
    loading,
  });
  useWebMcpTools(
    buildDashboardRecordsTools(getWebMcpState, {
      setView: (view) => {
        onSort(view.sort);
        onDirection(view.direction);
        setPage(view.page);
      },
    }),
    visible,
  );
  useEffect(() => {
    const controller = new AbortController();
    getRecords(data.links.records, page, sort, direction, controller.signal, dateFilter)
      .then((result) => {
        if (!controller.signal.aborted) setResult({ key: requestKey, records: result });
      })
      .catch((failure: unknown) => {
        if (!controller.signal.aborted)
          setResult({
            key: requestKey,
            error: failure instanceof Error ? failure.message : String(failure),
          });
      });
    return () => controller.abort();
  }, [data.links.records, dateFilter, page, sort, direction, requestKey]);
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
                onSort(event.target.value);
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
              onDirection(event.target.value);
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
                  {row.cost_status ? <small>{sourceLabel(row.cost_status)}</small> : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!loading && records?.total === 0 && (
          <p className="empty-chart">{tr('ui.no_records_match_this_scope')}</p>
        )}
      </div>
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
}
