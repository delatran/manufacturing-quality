import { memo, useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { createPortal } from 'react-dom';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  Check,
  ChevronDown,
  Circle,
  CircleAlert,
  Copy,
  LoaderCircle,
  Search,
  ShieldCheck,
} from 'lucide-react';
import { artifactUrl } from './api';
import { applicationUrl } from './app-url';
import { useI18n } from './i18n';
import type { WorkspaceEvent } from './types';

import type { Translate } from './i18n';
const text = (value: unknown) => (typeof value === 'string' ? value : '');
const completed = new Set(['completed', 'complete', 'done', 'success']);
const stopped = new Set([
  'failed',
  'error',
  'cancelled',
  'interrupted',
  'unverified',
  'budget_exceeded',
  'budget_exhausted',
  'provider_stopped',
  'refused',
  'incomplete',
  'timeout',
  'unavailable',
]);
const technicalFile =
  /(?:^|[/\\_.-])(?:evidence|manifest|ledger|receipt|receipts|claims|qa|verification|scope_manifest|run_metadata|schema_summary)(?:[/\\_.-]|$)/i;

export type NumericClaim = {
  id?: string;
  execution_id?: string;
  path?: string;
  value?: unknown;
  code_sha256?: string;
  metric?: string;
  unit?: string;
  labels?: Record<string, unknown>;
  [key: string]: unknown;
};

export function resolveNumericClaim(claims: readonly NumericClaim[] | undefined, claimId: string) {
  // A display reference must identify one exact source. Metrics and path suffixes
  // can repeat across tools or cohorts and must never select the first match.
  const exact = claims?.filter((claim) => claim.id === claimId) ?? [];
  if (exact.length === 1) return exact[0];
  if (exact.length > 1) return undefined;
  const paths = claims?.filter((claim) => claim.path === claimId) ?? [];
  return paths.length === 1 ? paths[0] : undefined;
}

export function formatEvidenceValue(value: unknown, locale: string): string | undefined {
  if (value === undefined || value === null) return undefined;
  return typeof value === 'number' && Number.isFinite(value)
    ? new Intl.NumberFormat(locale === 'vi' ? 'vi-VN' : 'en-US', {
        maximumSignificantDigits: 21,
      }).format(value)
    : String(value);
}

export function citationPosition(
  anchor: { left: number; top: number; bottom: number },
  panel: { width: number; height: number },
  viewport: { left: number; top: number; width: number; height: number },
) {
  const minLeft = viewport.left + 12;
  const minTop = viewport.top + 12;
  const maxLeft = viewport.left + viewport.width - panel.width - 12;
  const maxTop = viewport.top + viewport.height - panel.height - 12;
  const above = anchor.top - panel.height - 8;
  return {
    left: Math.max(minLeft, Math.min(anchor.left, maxLeft)),
    top: Math.max(minTop, Math.min(above >= minTop ? above : anchor.bottom + 8, maxTop)),
  };
}

export const CitationBadge = memo(function CitationBadge({
  index,
  claimId,
  claims,
}: {
  index: number;
  claimId: string;
  claims?: readonly NumericClaim[];
}) {
  const { locale } = useI18n();
  const [isHovered, setIsHovered] = useState(false);
  const [isPinned, setIsPinned] = useState(false);
  const [copiedField, setCopiedField] = useState<'id' | 'path' | null>(null);
  const wrapperRef = useRef<HTMLSpanElement>(null);
  const timeoutRef = useRef<number | null>(null);
  const copyTimeoutRef = useRef<number | null>(null);
  const popoverRef = useRef<HTMLSpanElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const popoverId = useId();
  const [position, setPosition] = useState({ left: 12, top: 12 });
  const [copyFailed, setCopyFailed] = useState(false);

  const isOpen = isPinned || isHovered;
  // The report's animated entry and scrolling table are containing blocks.
  // Portal into the stable workspace root so fixed coordinates use the viewport.
  const portalRoot = wrapperRef.current?.closest('.agent-workspace');

  const claim = useMemo(() => {
    return resolveNumericClaim(claims, claimId);
  }, [claims, claimId]);

  const metricName = useMemo(() => {
    if (claim?.metric && typeof claim.metric === 'string') return claim.metric;
    const target = claim?.path || claimId;
    const clean = target
      .replace(/^\$\.?/, '')
      .replace(/\[\d+\]/g, '')
      .split('.')
      .filter(Boolean)
      .pop();
    return clean || claimId;
  }, [claim, claimId]);

  const jsonPath = useMemo(() => {
    if (claim?.path) return claim.path;
    return undefined;
  }, [claim]);

  const executionId = useMemo(() => {
    return claim?.execution_id;
  }, [claim]);

  const shortExecutionId = useMemo(() => {
    if (executionId && executionId.length > 16) {
      return `${executionId.slice(0, 10)}...${executionId.slice(-4)}`;
    }
    return executionId;
  }, [executionId]);

  const formattedValue = useMemo(() => {
    return formatEvidenceValue(claim?.value, locale);
  }, [claim, locale]);

  const handleMouseEnter = useCallback(() => {
    if (timeoutRef.current !== null) {
      window.clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
    setIsHovered(true);
  }, []);

  const handleMouseLeave = useCallback(() => {
    if (timeoutRef.current !== null) {
      window.clearTimeout(timeoutRef.current);
    }
    timeoutRef.current = window.setTimeout(() => {
      setIsHovered(false);
      timeoutRef.current = null;
    }, 180);
  }, []);

  const handleToggle = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsPinned((prev) => !prev);
  }, []);

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      setIsPinned(false);
      setIsHovered(false);
      buttonRef.current?.focus();
    }
  }, []);

  useEffect(() => {
    if (!isPinned) return;
    const handlePointerDown = (e: MouseEvent | PointerEvent) => {
      if (
        !wrapperRef.current?.contains(e.target as Node) &&
        !popoverRef.current?.contains(e.target as Node)
      ) {
        setIsPinned(false);
        setIsHovered(false);
      }
    };
    document.addEventListener('pointerdown', handlePointerDown);
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown);
    };
  }, [isPinned]);

  useEffect(() => {
    if (isPinned) (popoverRef.current?.querySelector('button') ?? popoverRef.current)?.focus();
  }, [isPinned]);

  useEffect(() => {
    if (!isOpen) return;
    const place = () => {
      const anchor = buttonRef.current?.getBoundingClientRect();
      const panel = popoverRef.current;
      if (!anchor || !panel) return;
      const visual = window.visualViewport;
      const viewport = {
        left: visual?.offsetLeft ?? 0,
        top: visual?.offsetTop ?? 0,
        width: visual?.width ?? window.innerWidth,
        height: visual?.height ?? window.innerHeight,
      };
      // Apply viewport limits before measuring wrapping and scrollable height.
      panel.style.width = `${Math.max(0, Math.min(290, viewport.width - 24))}px`;
      panel.style.maxHeight = `${Math.max(0, viewport.height - 24)}px`;
      setPosition(citationPosition(anchor, panel.getBoundingClientRect(), viewport));
    };
    place();
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    window.visualViewport?.addEventListener('resize', place);
    window.visualViewport?.addEventListener('scroll', place);
    return () => {
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, true);
      window.visualViewport?.removeEventListener('resize', place);
      window.visualViewport?.removeEventListener('scroll', place);
    };
  }, [isOpen, copiedField, copyFailed]);

  useEffect(() => {
    return () => {
      if (timeoutRef.current !== null) {
        window.clearTimeout(timeoutRef.current);
      }
      if (copyTimeoutRef.current !== null) window.clearTimeout(copyTimeoutRef.current);
    };
  }, []);

  const copyText = useCallback(async (textToCopy: string, field: 'id' | 'path') => {
    if (copyTimeoutRef.current !== null) window.clearTimeout(copyTimeoutRef.current);
    try {
      await navigator.clipboard.writeText(textToCopy);
      setCopiedField(field);
      setCopyFailed(false);
    } catch {
      setCopiedField(null);
      setCopyFailed(true);
    }
    copyTimeoutRef.current = window.setTimeout(() => {
      setCopiedField(null);
      setCopyFailed(false);
    }, 1500);
  }, []);

  const isVi = locale === 'vi';

  return (
    <span
      ref={wrapperRef}
      className={`citation-badge-wrapper ${isOpen ? 'is-open' : ''}`}
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
      onKeyDown={handleKeyDown}
      onBlur={(event) => {
        if (
          !event.currentTarget.contains(event.relatedTarget) &&
          !popoverRef.current?.contains(event.relatedTarget)
        ) {
          setIsPinned(false);
          setIsHovered(false);
        }
      }}
    >
      <button
        ref={buttonRef}
        type="button"
        className={`citation-badge ${isOpen ? 'is-active' : ''}`}
        onClick={handleToggle}
        aria-expanded={isOpen}
        aria-haspopup="dialog"
        aria-controls={isOpen ? popoverId : undefined}
        aria-label={
          isVi
            ? `Bằng chứng đối chiếu [${index}]: ${metricName}`
            : `Evidence reference [${index}]: ${metricName}`
        }
        title={
          isVi
            ? `Bằng chứng đối chiếu [${index}]: ${metricName}`
            : `Evidence reference [${index}]: ${metricName}`
        }
      >
        <span className="citation-badge-bracket">[</span>
        <span className="citation-badge-num">{index}</span>
        <span className="citation-badge-bracket">]</span>
      </button>

      {isOpen &&
        portalRoot &&
        createPortal(
          <span
            id={popoverId}
            ref={popoverRef}
            style={position}
            className="citation-popover"
            role="dialog"
            tabIndex={-1}
            aria-label={
              isVi
                ? `Chi tiết bằng chứng sổ cái [${index}]`
                : `Evidence reference details [${index}]`
            }
            onMouseEnter={handleMouseEnter}
            onMouseLeave={handleMouseLeave}
          >
            <span className="citation-popover-header">
              <span className="citation-popover-title">
                <ShieldCheck className="citation-popover-icon" size={14} aria-hidden="true" />
                <span>{isVi ? 'Đối chiếu nguồn số liệu' : 'Source evidence'}</span>
              </span>
              <span className="citation-popover-badge-pill">[{index}]</span>
            </span>

            <span className="citation-popover-body">
              <span className="citation-row">
                <span className="citation-label">{isVi ? 'Chỉ số' : 'Metric'}</span>
                <span className="citation-val citation-metric" title={metricName}>
                  {metricName}
                </span>
              </span>

              {jsonPath && (
                <span className="citation-row">
                  <span className="citation-label">{isVi ? 'Đường dẫn JSON' : 'JSON Path'}</span>
                  <span className="citation-val-group">
                    <code className="citation-code" title={jsonPath}>
                      {jsonPath}
                    </code>
                    <button
                      type="button"
                      className="citation-copy-btn"
                      onClick={() => copyText(jsonPath, 'path')}
                      title={isVi ? 'Sao chép đường dẫn' : 'Copy JSON path'}
                      aria-label={isVi ? 'Sao chép đường dẫn' : 'Copy JSON path'}
                    >
                      {copiedField === 'path' ? (
                        <Check size={11} aria-hidden="true" />
                      ) : (
                        <Copy size={11} aria-hidden="true" />
                      )}
                    </button>
                  </span>
                </span>
              )}

              {executionId && (
                <span className="citation-row">
                  <span className="citation-label">{isVi ? 'Mã thực thi' : 'Execution ID'}</span>
                  <span className="citation-val-group">
                    <code className="citation-code" title={executionId}>
                      {shortExecutionId}
                    </code>
                    <button
                      type="button"
                      className="citation-copy-btn"
                      onClick={() => copyText(executionId, 'id')}
                      title={isVi ? 'Sao chép mã thực thi' : 'Copy execution ID'}
                      aria-label={isVi ? 'Sao chép mã thực thi' : 'Copy execution ID'}
                    >
                      {copiedField === 'id' ? (
                        <Check size={11} aria-hidden="true" />
                      ) : (
                        <Copy size={11} aria-hidden="true" />
                      )}
                    </button>
                  </span>
                </span>
              )}

              {formattedValue !== undefined && (
                <span className="citation-row">
                  <span className="citation-label">
                    {isVi ? 'Giá trị từ nguồn' : 'Source value'}
                  </span>
                  <span className="citation-val citation-value-num">{formattedValue}</span>
                </span>
              )}
            </span>

            <span className="citation-popover-footer">
              <span className="citation-verified-badge" role="status">
                {claim ? (
                  <Check size={11} aria-hidden="true" />
                ) : (
                  <CircleAlert size={11} aria-hidden="true" />
                )}
                <span>
                  {copyFailed
                    ? isVi
                      ? 'Chưa sao chép được. Hãy chọn và sao chép văn bản.'
                      : 'Could not copy. Select and copy the text.'
                    : copiedField
                      ? isVi
                        ? 'Đã sao chép'
                        : 'Copied'
                      : claim
                        ? isVi
                          ? 'Đã liên kết với bằng chứng của báo cáo'
                          : 'Linked to the report evidence'
                        : isVi
                          ? 'Chưa có chi tiết nguồn cho tham chiếu này'
                          : 'Source details are unavailable for this reference'}
                </span>
              </span>
            </span>
          </span>,
          portalRoot,
        )}
    </span>
  );
});

function localFileLink(value: string): boolean {
  const url = artifactUrl(value);
  if (!url) return false;
  try {
    const address = new URL(url);
    return /(?:\/api\/|\/artifacts\/|\.(?:zip|pdf|docx?|xlsx?|csv|tsv|png|jpe?g|webp|svg|gif|html?|md)$)/i.test(
      decodeURIComponent(address.pathname),
    );
  } catch {
    return false;
  }
}

// Presentation transforms claims into citation badges and simplifies the number-format note.
// The event and audited report stay intact.
export function businessReportText(value: unknown): string {
  let citationIndex = 0;
  const citationMap = new Map<string, number>();
  const numberFormatPrefix =
    'Định dạng số: dấu chấm phân tách hàng nghìn, dấu phẩy phân tách phần thập phân.';
  const report = text(value);
  // Hide only the app-owned leading note; authored mentions later in the report
  // and the persisted report, claims and delivery evidence remain unchanged.
  const visibleReport = report.startsWith(numberFormatPrefix)
    ? report.slice(numberFormatPrefix.length).replace(/^(?:\r?\n)+/, '')
    : report;

  return visibleReport
    .split('\n')
    .filter(
      (line) =>
        !(/^(?:https?:\/\/|\/|artifacts\/)/i.test(line.trim()) && localFileLink(line.trim())),
    )
    .join('\n')
    .replace(/^<!-- evidence-table:[a-f0-9]{24}:(?:start|end) -->\r?$/gm, '')
    .replace(/\s*\[claim:((?:[^[\]]|\[\d+\])+)\]/g, (_match, claimId) => {
      const trimmed = claimId.trim();
      let index = citationMap.get(trimmed);
      if (index === undefined) {
        citationIndex += 1;
        index = citationIndex;
        citationMap.set(trimmed, index);
      }
      return ` [${index}](#citation:${index}:${encodeURIComponent(trimmed)})`;
    })
    .replace(
      'Quy ước số để đối chiếu bằng chứng: dấu phẩy phân tách hàng nghìn, dấu chấm phân tách phần thập phân.',
      'Số trong báo cáo: dấu phẩy tách hàng nghìn, dấu chấm cho phần thập phân.',
    );
}

function isTechnicalLink(value: string): boolean {
  return (
    value.startsWith('#evidence-') ||
    /\.(?:zip|jsonl?|ndjson|py|sql|log|ya?ml|toml)(?:[?#]|$)/i.test(value) ||
    /\/evidence(?:[/?#]|$)/i.test(value) ||
    /\/(?:verified_)?report\.md(?:[?#]|$)/i.test(value) ||
    (value.includes('/api/') && technicalFile.test(value.split(/[?#]/)[0]))
  );
}

const defaultRemarkPlugins = [remarkGfm];

// Markdown keeps HTML inert. Charts are limited to this workspace's files.
export const Prose = memo(function Prose({
  value,
  report = false,
  chartUrls,
  claims,
}: {
  value: unknown;
  report?: boolean;
  chartUrls?: readonly string[];
  claims?: readonly NumericClaim[];
}) {
  const { t } = useI18n();
  const components = useMemo(
    () => ({
      table: ({ children }: { children?: ReactNode }) => (
        <div
          className="report-table-scroll"
          tabIndex={0}
          role="region"
          aria-label={t('ui.report_data_table')}
        >
          <table>{children}</table>
        </div>
      ),
      a: ({
        href,
        children,
        node,
      }: {
        href?: string;
        children?: ReactNode;
        node?: { position?: unknown; children?: Array<{ type: string; tagName?: string }> };
      }) => {
        if (href && (href.startsWith('#citation:') || href.startsWith('#claim:'))) {
          const match = href.match(/^#(?:citation|claim):(\d+):(.*)$/);
          const index = match ? parseInt(match[1], 10) : 1;
          let rawId = match ? match[2] : href.replace(/^#(?:citation|claim):/, '');
          try {
            rawId = decodeURIComponent(rawId);
          } catch {
            // Keep malformed authored references inert and unresolved.
          }
          return <CitationBadge index={index} claimId={rawId} claims={claims} />;
        }
        // GFM synthesizes links without source positions after decoding text entities.
        if (report && !node?.position) return <>{children}</>;
        if (report && href && isTechnicalLink(href)) return null;
        if (report && href) {
          if (localFileLink(href))
            return node?.children?.some(
              (child) => child.type === 'element' && child.tagName === 'img',
            ) ? (
              <>{children}</>
            ) : null;
        }
        return (
          <a
            href={
              href?.startsWith('#')
                ? href
                : href?.startsWith('/')
                  ? applicationUrl(href)
                  : artifactUrl(href) || href
            }
            target="_blank"
            rel="noreferrer"
          >
            {children}
          </a>
        );
      },
      pre: ({ children }: { children?: ReactNode }) => (report ? null : <pre>{children}</pre>),
      img: ({ src, alt }: { src?: string; alt?: string }) => {
        const url = artifactUrl(src);
        if (report && chartUrls !== undefined && (!url || !chartUrls.includes(url))) return null;
        return url ? (
          <img src={url} alt={alt || t('ui.analysis_chart')} loading="lazy" />
        ) : (
          <span className="report-file-unavailable">
            {alt || t('ui.chart')}: {t('ui.not_available')}
          </span>
        );
      },
    }),
    [report, chartUrls, t, claims],
  );

  return (
    <div className="prose">
      <Markdown remarkPlugins={defaultRemarkPlugins} components={components}>
        {report ? businessReportText(value) : text(value)}
      </Markdown>
    </div>
  );
});

export function toolStep(name: unknown, t: Translate): string {
  const value = text(name).toLowerCase();
  if (/operations_list_cases/.test(value)) return t('ui.reviewing_follow_up_work');
  if (/operations_create_draft/.test(value)) return t('ui.preparing_an_action_draft');
  if (/operations_update_draft/.test(value)) return t('ui.revising_the_action_draft');
  if (/dashboard_compare|analysis_compare|scenario_compare/.test(value))
    return t('ui.comparing_the_selected_views');
  if (/dashboard_context|analysis_query|evidence_detail/.test(value))
    return t('ui.reading_the_selected_data');
  if (/profile|source|fetch|read|search|analysis_records|analysis_provenance/.test(value))
    return t('ui.checking_the_data');
  if (/export|artifact|report|report_table/.test(value)) return t('ui.preparing_the_report');
  return t('ui.analyzing_the_results');
}

export function analysisStatus(state: string, t: Translate): string {
  if (completed.has(state)) return t('ui.analysis_ready');
  if (state === 'refused') return t('ui.analysis_refused');
  if (state === 'incomplete') return t('ui.analysis_incomplete');
  if (state === 'timeout') return t('ui.analysis_timeout');
  if (state === 'unavailable') return t('ui.analysis_unavailable');
  if (state === 'unverified') return t('ui.draft_needs_review');
  if (state === 'interrupted') return t('ui.analysis_interrupted');
  if (state === 'cancelled') return t('ui.analysis_cancelled');
  if (state === 'provider_stopped') return t('ui.this_analysis_cannot_continue');
  if (state === 'budget_exceeded' || state === 'budget_exhausted')
    return t('ui.analysis_paused_at_its_usage_limit');
  if (state === 'failed' || state === 'error') return t('ui.analysis_stopped_before_finishing');
  if (state === 'queued') return t('ui.your_request_is_queued');
  if (state === 'qa' || state === 'verifying') return t('ui.checking_the_results');
  if (state === 'idle') return t('ui.ready_for_your_question');
  return t('ui.analyzing');
}

export function friendlyError(value: unknown, t: Translate, code?: unknown): string {
  if (text(code) === 'unverified') return t('ui.result_could_not_be_verified');
  const message = text(value).toLowerCase();
  if (/snapshot|dashboard (?:context|view)|phạm vi|bản dữ liệu/.test(message))
    return t('ui.the_selected_data_is_no_longer_available_refresh_the_dashboard_re');
  if (/too long|quá dài/.test(message))
    return t('ui.this_request_is_too_long_shorten_it_or_attach_the_details_as_a_fi');
  if (/provider.*stop|cannot be retried|không thể thử lại/.test(message))
    return t('ui.this_conversation_cannot_continue_your_previous_results_are_still');
  if (/cost|budget|usage limit|ngân sách/.test(message))
    return t('ui.the_analysis_reached_its_usage_limit_before_finishing_results_so_');
  if (/upload|attachment|unsupported extension|file is too large|đính kèm/.test(message))
    return t('ui.a_file_could_not_be_attached_check_its_format_and_size_then_try_a');
  if (/connect|network|fetch|socket|unreadable|kết nối/.test(message))
    return t('ui.connection_interrupted_we_are_reconnecting_your_draft_is_kept_her');
  return t('ui.the_analysis_could_not_finish_your_request_and_any_saved_results_');
}

function EventLabel({ icon, children }: { icon: ReactNode; children: ReactNode }) {
  return (
    <div className="event-label">
      {icon}
      <span>{children}</span>
    </div>
  );
}

export function chartArtifact(event: WorkspaceEvent): boolean {
  const d = event.data;
  return Boolean(
    artifactUrl(d.url) &&
    /\.(?:png|jpe?g|webp|svg|gif)$/i.test(text(d.name || d.path)) &&
    !technicalFile.test(text(d.name || d.path)) &&
    d.available !== false &&
    d.size !== 0 &&
    !['failed', 'error', 'unavailable'].includes(text(d.status)),
  );
}

function ArtifactChart({ event }: { event: WorkspaceEvent }) {
  const { t } = useI18n();
  if (!chartArtifact(event)) return null;
  return (
    <figure className="agent-chart">
      <img
        src={artifactUrl(event.data.url)}
        alt={text(event.data.description) || t('ui.analysis_chart')}
        loading="lazy"
      />
    </figure>
  );
}

function Activity({ event }: { event: WorkspaceEvent }) {
  const { t } = useI18n();
  const state = text(event.data.state) || 'running';
  const isDone = completed.has(state);
  const isStopped = stopped.has(state);
  const items = (Array.isArray(event.data.items) ? event.data.items : []) as WorkspaceEvent[];
  const steps = new Map<string, string>();
  items.forEach((item) => {
    if (item.type !== 'tool' || /^(?:memory_|todo_|knowledge_)/.test(text(item.data.name))) return;
    steps.set(toolStep(item.data.name, t), text(item.data.status));
  });
  return (
    <details
      className={
        'analysis-activity ' + (isDone ? 'is-done' : isStopped ? 'is-stopped' : 'is-working')
      }
    >
      <summary aria-label={`${analysisStatus(state, t)}. ${t('ui.activity')}`}>
        {isDone ? (
          <Check size={16} aria-hidden="true" />
        ) : isStopped ? (
          <CircleAlert size={16} aria-hidden="true" />
        ) : (
          <LoaderCircle className="spin" size={16} aria-hidden="true" />
        )}
        <span className="activity-status">{analysisStatus(state, t)}</span>
        <span className="activity-disclosure-label">{t('ui.activity')}</span>
        <ChevronDown className="activity-chevron" size={15} aria-hidden="true" />
      </summary>
      <div className="activity-content">
        {steps.size > 0 ? (
          <ul>
            {[...steps].map(([step, status]) => (
              <li key={step}>
                {completed.has(status) ? (
                  <Check size={14} />
                ) : ['failed', 'error'].includes(status) ? (
                  <CircleAlert size={14} />
                ) : isDone || isStopped ? (
                  <Circle size={13} />
                ) : (
                  <LoaderCircle className="spin" size={14} />
                )}
                <span>{step}</span>
                <small>
                  {completed.has(status)
                    ? t('ui.done')
                    : ['failed', 'error'].includes(status)
                      ? t('ui.could_not_finish')
                      : isDone || isStopped
                        ? t('ui.not_confirmed')
                        : t('ui.in_progress')}
                </small>
              </li>
            ))}
          </ul>
        ) : (
          <p>
            {isDone
              ? t('ui.your_analysis_is_available_below')
              : isStopped
                ? t('ui.any_available_results_are_kept_in_this_conversation')
                : t('ui.the_analyst_is_preparing_your_request')}
          </p>
        )}
        {state === 'unverified' && (
          <p className="activity-note">
            {t('ui.some_figures_could_not_be_confirmed_review_the_draft_before_using')}
          </p>
        )}
        {['budget_exceeded', 'budget_exhausted'].includes(state) && (
          <p className="activity-note">
            {t('ui.the_usage_limit_was_reached_this_analysis_is_incomplete')}
          </p>
        )}
      </div>
    </details>
  );
}

export const EventView = memo(function EventView({ event }: { event: WorkspaceEvent }) {
  const { t } = useI18n();
  const d = event.data || {};
  if (event.type === 'message') {
    const user = d.role === 'user';
    const qa =
      typeof d.qa === 'object' && d.qa !== null ? (d.qa as Record<string, unknown>) : undefined;
    const restrictedCharts =
      Object.prototype.hasOwnProperty.call(d, 'delivery') ||
      Object.prototype.hasOwnProperty.call(d, 'chart_urls');
    const chartUrls = restrictedCharts
      ? Array.isArray(d.chart_urls)
        ? d.chart_urls.filter((url): url is string => typeof url === 'string')
        : []
      : undefined;
    const claims = Array.isArray(d.claims) ? (d.claims as NumericClaim[]) : undefined;
    return (
      <article className={user ? 'message user-message' : 'message analyst-message'}>
        <EventLabel
          icon={
            user ? (
              <span className="user-dot">{t('ui.y')}</span>
            ) : (
              <span className="agent-dot">
                <Search size={15} />
              </span>
            )
          }
        >
          {user ? t('ui.you') : t('ui.quality_analyst_2')}
          {user && (d.action_intent === 'draft_create' || d.action_intent === 'draft_update') && (
            <small className="request-work-mode">
              {d.action_intent === 'draft_create'
                ? t('ui.prepare_an_action_draft')
                : t('ui.revise_a_draft')}
            </small>
          )}
          {d.simulation === true && !user && (
            <small className="report-simulation-label">{t('ui.simulated_response')}</small>
          )}
        </EventLabel>
        {qa?.verified === false && !user && (
          <p className="report-review-note">
            <CircleAlert size={16} />
            {t('ui.draft_some_figures_still_need_checking_review_before_acting_on_th')}
          </p>
        )}
        <Prose value={d.text ?? d.content} report={!user} chartUrls={chartUrls} claims={claims} />
      </article>
    );
  }
  if (event.type === 'activity') return <Activity event={event} />;
  if (event.type === 'tool') {
    if (/^(?:memory_|todo_|knowledge_)/.test(text(d.name))) return null;
    const status = text(d.status);
    return (
      <div className="simple-tool-status">
        {completed.has(status) ? (
          <Check size={14} />
        ) : stopped.has(status) ? (
          <CircleAlert size={14} />
        ) : (
          <LoaderCircle className="spin" size={14} />
        )}
        <span>{toolStep(d.name, t)}</span>
      </div>
    );
  }
  if (event.type === 'artifact') return <ArtifactChart event={event} />;
  if (event.type === 'artifact_group') return null;
  if (event.type === 'error')
    return (
      <div className="analysis-error" role="alert">
        <CircleAlert size={18} />
        <p>{friendlyError(d.message || d.text, t, d.code)}</p>
      </div>
    );
  if (event.type === 'status') {
    const state = text(d.state);
    return (
      <div
        className={'simple-analysis-status ' + (stopped.has(state) ? 'is-stopped' : '')}
        role="status"
      >
        {completed.has(state) ? (
          <Check size={14} />
        ) : stopped.has(state) ? (
          <CircleAlert size={14} />
        ) : (
          <LoaderCircle className="spin" size={14} />
        )}
        <span>{analysisStatus(state, t)}</span>
      </div>
    );
  }
  // Operational traces, reasoning, QA and unknown events stay in the event store.
  return null;
});

export default EventView;
