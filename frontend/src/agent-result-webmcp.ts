import { artifactUrl } from './api';
import { answerSourceQuestion, eligibleAnswerFollowup } from './answer-followups';
import { eventsForReport } from './conversation-state';
import type { ConversationEntry, ConversationState } from './conversation-state';
import { initialBackendScope } from './generated-scope';
import { citationSourceId, resolveNumericClaim } from './EventView';
import type { NumericClaim } from './EventView';
import type { Session, WorkspaceEvent } from './types';
import { commitUi, objectInput, stringInput, throwIfAborted } from './webmcp';
import type { WebMcpTool } from './webmcp';

export type AgentResultSnapshot = {
  session: Session | null;
  conversation: ConversationState;
  dashboardContextId?: string;
  visible: boolean;
};

export type AgentResultAdapter = {
  getSnapshot(): AgentResultSnapshot;
  getRoot(): HTMLElement | null;
};

const record = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value);
const string = (value: unknown): string | undefined =>
  typeof value === 'string' && value.trim() ? value : undefined;
const hash = (value: unknown): value is string =>
  typeof value === 'string' && /^[a-f0-9]{64}$/i.test(value);
const identifier = { type: 'string', pattern: '^[a-f0-9]{32}$' };
const answerProperties = {
  run_id: identifier,
  message_id: { type: 'integer', minimum: 1 },
};
const pageProperties = {
  offset: { type: 'integer', minimum: 0, default: 0 },
  limit: { type: 'integer', minimum: 1, maximum: 50, default: 20 },
};
const readAnnotations = {
  readOnlyHint: true,
  consequentialHint: false,
  untrustedContentHint: true,
};
const evidenceNotice =
  'Stored evidence for this original answer and context. It is not fresh Dashboard evidence or authority for a new action.';

function integer(value: unknown, name: string, minimum: number, maximum = Number.MAX_SAFE_INTEGER) {
  if (
    typeof value !== 'number' ||
    !Number.isSafeInteger(value) ||
    value < minimum ||
    value > maximum
  )
    throw new Error(`${name} must be an integer from ${minimum} to ${maximum}`);
  return value;
}

function reference(input: Record<string, unknown>) {
  const run = stringInput(input.run_id, 'run_id');
  if (!/^[a-f0-9]{32}$/.test(run)) throw new Error('run_id is not a valid answer run identifier');
  return { run_id: run, message_id: integer(input.message_id, 'message_id', 1) };
}

function pagination(total: number, offset: number, limit: number) {
  if (offset > total) throw new Error('offset exceeds the available records');
  const returned = Math.min(limit, total - offset);
  return {
    total,
    offset,
    returned,
    omitted_before: offset,
    omitted_after: total - offset - returned,
    next_offset: offset + returned < total ? offset + returned : null,
  };
}

function page(input: Record<string, unknown>) {
  return {
    offset: input.offset === undefined ? 0 : integer(input.offset, 'offset', 0),
    limit: input.limit === undefined ? 20 : integer(input.limit, 'limit', 1, 50),
  };
}

function current(adapter: AgentResultAdapter) {
  const snapshot = adapter.getSnapshot();
  if (!snapshot.visible || !snapshot.session) throw new Error('Open the connected Agent first');
  return snapshot as AgentResultSnapshot & { session: Session };
}

function eligible(entry: ConversationEntry, snapshot: AgentResultSnapshot & { session: Session }) {
  const { event, context } = entry;
  return (
    entry.eligible &&
    event.session_id === snapshot.session.id &&
    context?.session_id === event.session_id &&
    context.run_id === event.run_id &&
    context.type === 'analysis_context' &&
    Boolean(string(context.data.context_id)) &&
    eligibleAnswerFollowup(
      event,
      eventsForReport(snapshot.conversation, event),
      snapshot.conversation.savedRuns,
    )
  );
}

function selected(adapter: AgentResultAdapter, ref: ReturnType<typeof reference>) {
  const snapshot = current(adapter);
  const entry = snapshot.conversation.entries.find(
    (item) =>
      item.event.session_id === snapshot.session.id &&
      item.event.id === ref.message_id &&
      item.event.run_id === ref.run_id,
  );
  if (!entry || !eligible(entry, snapshot))
    throw new Error(
      'This answer is unavailable, superseded, or no longer verified in this session',
    );
  return { snapshot, entry };
}

function rootFor(adapter: AgentResultAdapter): HTMLElement {
  const root = adapter.getRoot();
  if (!root?.isConnected || root.closest('[hidden], [inert]'))
    throw new Error('The Agent is not mounted in the visible workspace');
  return root;
}

function answerElement(root: HTMLElement, report: WorkspaceEvent): HTMLElement {
  const element = [...root.querySelectorAll<HTMLElement>('[data-answer-message-id]')].find(
    (item) =>
      item.dataset.answerSessionId === report.session_id &&
      item.dataset.answerRunId === report.run_id &&
      item.dataset.answerMessageId === String(report.id),
  );
  if (!element?.isConnected) throw new Error('The selected answer is not rendered');
  return element;
}

/** Read the same rendered projection as the person, without hidden links or controls. */
export function renderedAnswerText(prose: Element): string {
  function visit(node: Node): string {
    if (node.nodeType === 3) return node.textContent || '';
    if (node.nodeType !== 1) return '';
    const element = node as Element;
    if (
      element.matches(
        'button, input, select, textarea, script, style, .answer-outline, .answer-code-toolbar, .report-table-tools, .report-table-empty, .citation-popover',
      )
    )
      return element.classList.contains('citation-badge')
        ? `[${element.getAttribute('data-evidence-index') || '?'}]`
        : '';
    if (element.tagName === 'IMG') return `[Chart: ${element.getAttribute('alt') || 'Chart'}]`;
    if (element.tagName === 'BR') return '\n';
    const content = [...element.childNodes].map(visit).join('');
    if (/^(?:TD|TH)$/.test(element.tagName)) return `${content}\t`;
    return /^(?:P|DIV|H[1-6]|LI|TR|PRE|BLOCKQUOTE|TABLE)$/.test(element.tagName)
      ? `${content}\n`
      : content;
  }
  return visit(prose)
    .replace(/\t\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

function excerpt(value: string | undefined, limit = 240) {
  const source = value || '';
  return { text: source.slice(0, limit), omitted_characters: Math.max(0, source.length - limit) };
}

function sourceContext(entry: ConversationEntry, snapshot: AgentResultSnapshot) {
  const source = entry.context!.data;
  const filters = record(source.filters) ? source.filters : {};
  const provenance: Record<string, string> = {};
  for (const key of [
    'snapshot_id',
    'source_sha256',
    'master_sha256',
    'code_sha256',
    'kpi_version',
    'dataset_version',
    'policy_version',
    'fetched_at',
    'scenario',
  ]) {
    const value = string(source[key]);
    if (value) provenance[key] = value;
  }
  return {
    context_id: source.context_id,
    relation_to_dashboard: !snapshot.dashboardContextId
      ? 'dashboard_context_unavailable'
      : source.context_id === snapshot.dashboardContextId
        ? 'same_context'
        : 'historical_context',
    filters: Object.fromEntries(
      Object.keys(initialBackendScope)
        .filter((key) => typeof filters[key] === 'string')
        .map((key) => [key, filters[key]]),
    ),
    provenance,
    source_was_stale:
      record(source.source_status) && typeof source.source_status.stale === 'boolean'
        ? source.source_status.stale
        : null,
    evidence_notice: evidenceNotice,
  };
}

function summary(entry: ConversationEntry, snapshot: AgentResultSnapshot) {
  return {
    run_id: entry.event.run_id,
    message_id: entry.event.id,
    time: entry.event.time,
    language: entry.language ?? null,
    verification: 'stored_report_qa_passed_completed_run',
    simulation: entry.event.data.simulation === true,
    source_question: excerpt(answerSourceQuestion(entry.event, entry.question)),
    source_context: sourceContext(entry, snapshot),
    result_exports_available: false,
  };
}

function claimsFor(report: WorkspaceEvent) {
  const raw: unknown[] = Array.isArray(report.data.claims) ? report.data.claims : [];
  const records = raw.filter(record) as NumericClaim[];
  const claims = records.flatMap((claim) => {
    const id = string(claim.id) || string(claim.path);
    if (
      !id ||
      resolveNumericClaim(records, id) !== claim ||
      !string(claim.execution_id) ||
      !string(claim.path) ||
      !hash(claim.code_sha256) ||
      typeof claim.value !== 'number' ||
      !Number.isFinite(claim.value)
    )
      return [];
    return [
      {
        claim_id: id,
        execution_id: claim.execution_id,
        path: claim.path,
        code_sha256: claim.code_sha256,
        value: claim.value,
        metric: string(claim.metric) ?? null,
        unit: string(claim.unit) ?? null,
      },
    ];
  });
  return { claims, omitted_invalid_or_ambiguous_claims: raw.length - claims.length };
}

function chartsFor(report: WorkspaceEvent) {
  const delivery = report.data.delivery;
  const selectedUrls = report.data.chart_urls;
  const artifacts = record(delivery) && Array.isArray(delivery.artifacts) ? delivery.artifacts : [];
  if (
    !record(delivery) ||
    delivery.version !== 1 ||
    !Array.isArray(delivery.artifacts) ||
    !Array.isArray(selectedUrls)
  )
    return {
      charts: [],
      chart_registry_status: 'unavailable',
      omitted_non_chart_or_unavailable_artifacts: artifacts.length,
    };
  const charts = artifacts.flatMap((item: unknown, index: number) => {
    if (
      !record(item) ||
      !hash(item.sha256) ||
      typeof item.name !== 'string' ||
      typeof item.mime_type !== 'string' ||
      !/^image\/(?:png|jpeg|webp|svg\+xml|gif)$/.test(item.mime_type) ||
      typeof item.size_bytes !== 'number' ||
      !Number.isSafeInteger(item.size_bytes) ||
      item.size_bytes <= 0
    )
      return [];
    const url = artifactUrl(item.url);
    // The canonical conversation projector binds these URLs to exact delivery
    // paths, hashes and this session/run. It also removes later invalidated bytes.
    if (!url || !selectedUrls.includes(url)) return [];
    return [
      {
        chart_id: `chart-${index + 1}`,
        name: item.name,
        mime_type: item.mime_type,
        size_bytes: item.size_bytes,
        sha256: item.sha256,
        url,
      },
    ];
  });
  return {
    charts,
    chart_registry_status: 'registered_delivery',
    omitted_non_chart_or_unavailable_artifacts: artifacts.length - charts.length,
  };
}

function chartButton(root: HTMLElement, report: WorkspaceEvent, url: string) {
  return [...root.querySelectorAll<HTMLButtonElement>('button[data-answer-chart-src]')].find(
    (button) => {
      const owner = button.closest<HTMLElement>('[data-answer-run-id]');
      return (
        owner !== null &&
        button.dataset.answerChartSrc === url &&
        owner.dataset.answerRunId === report.run_id &&
        owner.dataset.answerSessionId === report.session_id &&
        (!owner.dataset.answerMessageId || owner.dataset.answerMessageId === String(report.id))
      );
    },
  );
}

function citationButtons(
  element: HTMLElement,
  report: WorkspaceEvent,
  claim: ReturnType<typeof claimsFor>['claims'][number],
) {
  const records = (Array.isArray(report.data.claims) ? report.data.claims : []).filter(
    record,
  ) as NumericClaim[];
  const source = resolveNumericClaim(records, claim.claim_id);
  return [...element.querySelectorAll<HTMLButtonElement>('button[data-answer-citation]')].filter(
    (button) =>
      Boolean(source) && resolveNumericClaim(records, citationSourceId(button) || '') === source,
  );
}

function answerBody(element: HTMLElement) {
  const prose = element.querySelector('.prose');
  if (!prose) throw new Error('The selected answer content is not rendered');
  return renderedAnswerText(prose);
}

function historyRevision(snapshot: AgentResultSnapshot & { session: Session }) {
  const events = snapshot.conversation.events.filter(
    (event) => event.session_id === snapshot.session.id,
  );
  return JSON.stringify([snapshot.session.id, events.length, events.at(-1)?.id ?? null]);
}

async function contentHash(value: string) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

/** These tools only use the mounted visitor's committed conversation and controls. */
export function buildAgentResultTools(adapter: AgentResultAdapter): WebMcpTool[] {
  return [
    {
      name: 'list_agent_answers',
      title: 'Read verified answer history',
      description:
        'Read a page of verified final answers from the mounted Agent session, oldest first. All history is reachable by pagination. Each answer keeps its original source context; it is historical evidence even when its context matches the Dashboard. Supply history_revision on continuation pages. No analysis is started.',
      inputSchema: {
        type: 'object',
        properties: { ...pageProperties, history_revision: { type: 'string' } },
        additionalProperties: false,
      },
      annotations: readAnnotations,
      execute(input, { signal } = {}) {
        throwIfAborted(signal);
        const args = objectInput(input, ['offset', 'limit', 'history_revision']);
        const { offset, limit } = page(args);
        const snapshot = current(adapter);
        rootFor(adapter);
        const revision = historyRevision(snapshot);
        if (offset > 0 && args.history_revision === undefined)
          throw new Error('history_revision is required to continue a history page');
        if (
          args.history_revision !== undefined &&
          stringInput(args.history_revision, 'history_revision') !== revision
        )
          throw new Error('History changed. Read the first page again');
        const reports = snapshot.conversation.entries.filter(
          (entry) =>
            entry.event.session_id === snapshot.session.id &&
            entry.type === 'message' &&
            entry.role === 'assistant',
        );
        const answers = reports.filter((entry) => eligible(entry, snapshot));
        const paging = pagination(answers.length, offset, limit);
        return {
          session_id: snapshot.session.id,
          history_revision: revision,
          answers: answers.slice(offset, offset + limit).map((entry) => summary(entry, snapshot)),
          latest_verified_answer: answers.length
            ? {
                run_id: answers.at(-1)!.event.run_id,
                message_id: answers.at(-1)!.event.id,
              }
            : null,
          pagination: paging,
          omitted_ineligible_answers: reports.length - answers.length,
          eligibility:
            'Latest verified answer per completed run with its original recorded context',
          result_exports_available: false,
        };
      },
    },
    {
      name: 'read_agent_answer',
      title: 'Read a verified answer',
      description:
        'Read the rendered text of one verified answer in this session, including original source context and registered chart metadata. Text is paginated by UTF-16 character offsets. Supply content_sha256 on continuation pages. Current table-search omissions are explicit. Hidden files, raw receipts and result exports are not returned. This does not start or repeat analysis.',
      inputSchema: {
        type: 'object',
        properties: {
          ...answerProperties,
          offset: { type: 'integer', minimum: 0, default: 0 },
          limit: { type: 'integer', minimum: 1, maximum: 12000, default: 6000 },
          content_sha256: { type: 'string', pattern: '^[a-f0-9]{64}$' },
          chart_offset: { type: 'integer', minimum: 0, default: 0 },
          chart_limit: { type: 'integer', minimum: 1, maximum: 50, default: 20 },
        },
        required: ['run_id', 'message_id'],
        additionalProperties: false,
      },
      annotations: readAnnotations,
      async execute(input, { signal } = {}) {
        throwIfAborted(signal);
        const args = objectInput(
          input,
          [
            'run_id',
            'message_id',
            'offset',
            'limit',
            'content_sha256',
            'chart_offset',
            'chart_limit',
          ],
          ['run_id', 'message_id'],
        );
        const ref = reference(args);
        const { snapshot, entry } = selected(adapter, ref);
        const element = answerElement(rootFor(adapter), entry.event);
        const body = answerBody(element);
        const offset = args.offset === undefined ? 0 : integer(args.offset, 'offset', 0);
        const limit = args.limit === undefined ? 6000 : integer(args.limit, 'limit', 1, 12000);
        if (offset > 0 && args.content_sha256 === undefined)
          throw new Error('content_sha256 is required to continue an answer page');
        if (args.content_sha256 !== undefined && !hash(args.content_sha256))
          throw new Error('content_sha256 must be a SHA-256 digest');
        const digest = await contentHash(body);
        throwIfAborted(signal);
        const latest = selected(adapter, ref);
        if (
          latest.snapshot.session.id !== snapshot.session.id ||
          answerBody(answerElement(rootFor(adapter), latest.entry.event)) !== body
        )
          throw new Error('The visible answer changed. Read it again');
        if (args.content_sha256 !== undefined && args.content_sha256 !== digest)
          throw new Error('The visible answer changed. Read the first text page again');
        const chartOffset =
          args.chart_offset === undefined ? 0 : integer(args.chart_offset, 'chart_offset', 0);
        const chartLimit =
          args.chart_limit === undefined ? 20 : integer(args.chart_limit, 'chart_limit', 1, 50);
        const { charts, chart_registry_status, omitted_non_chart_or_unavailable_artifacts } =
          chartsFor(latest.entry.event);
        const tables = [...element.querySelectorAll<HTMLElement>('[data-answer-table-rows]')].map(
          (table) => ({
            total: Number(table.dataset.answerTableRows),
            visible: Number(table.dataset.answerTableVisibleRows),
          }),
        );
        return {
          session_id: snapshot.session.id,
          ...summary(latest.entry, latest.snapshot),
          content: body.slice(offset, offset + limit),
          content_format: 'rendered_text',
          content_sha256: digest,
          pagination: { ...pagination(body.length, offset, limit), unit: 'UTF-16 code units' },
          table_view: {
            total_tables: tables.length,
            tables_with_search_omissions: tables.filter((table) => table.total > table.visible)
              .length,
            omitted_rows_by_current_search: tables.reduce(
              (sum, table) => sum + table.total - table.visible,
              0,
            ),
          },
          charts: charts.slice(chartOffset, chartOffset + chartLimit).map(({ url, ...chart }) => ({
            ...chart,
            rendered: Boolean(chartButton(rootFor(adapter), latest.entry.event, url)),
          })),
          chart_pagination: pagination(charts.length, chartOffset, chartLimit),
          chart_registry_status,
          omitted_non_chart_or_unavailable_artifacts,
          evidence: {
            available_claims: claimsFor(latest.entry.event).claims.length,
            read_tool: 'read_agent_answer_evidence',
          },
          omissions: ['Hidden file links', 'Internal execution payloads', 'Reading controls'],
        };
      },
    },
    {
      name: 'read_agent_answer_evidence',
      title: 'Read answer evidence',
      description:
        'Read a page of source-bound numeric claims from a currently eligible answer in this session. Claims keep their stored value, execution ID, code hash and JSON path. Verification is the original report QA, not a fresh re-execution. Raw tool outputs and files are omitted. Use claim_id to select one exact claim.',
      inputSchema: {
        type: 'object',
        properties: {
          ...answerProperties,
          ...pageProperties,
          claim_id: { type: 'string', minLength: 1 },
        },
        required: ['run_id', 'message_id'],
        additionalProperties: false,
      },
      annotations: readAnnotations,
      execute(input, { signal } = {}) {
        throwIfAborted(signal);
        const args = objectInput(
          input,
          ['run_id', 'message_id', 'offset', 'limit', 'claim_id'],
          ['run_id', 'message_id'],
        );
        const { snapshot, entry } = selected(adapter, reference(args));
        const element = answerElement(rootFor(adapter), entry.event);
        const { offset, limit } = page(args);
        const evidence = claimsFor(entry.event);
        const id =
          args.claim_id === undefined
            ? undefined
            : stringInput(args.claim_id, 'claim_id', { minLength: 1 });
        const claims = id
          ? evidence.claims.filter((claim) => claim.claim_id === id)
          : evidence.claims;
        if (id && !claims.length)
          throw new Error('This claim is unavailable or ambiguous in the selected answer');
        return {
          session_id: snapshot.session.id,
          ...summary(entry, snapshot),
          claims: claims.slice(offset, offset + limit).map((claim) => ({
            ...claim,
            citation_visible: citationButtons(element, entry.event, claim).length > 0,
          })),
          pagination: pagination(claims.length, offset, limit),
          omitted_invalid_or_ambiguous_claims: evidence.omitted_invalid_or_ambiguous_claims,
          omissions: [
            'Raw execution payloads',
            'Claim labels and other non-numeric metadata',
            'Files and exports',
          ],
        };
      },
    },
    {
      name: 'navigate_agent_answer',
      title: 'Open an answer, citation or chart',
      description:
        'Navigate to an eligible answer, pin its visible source citation, or expand its registered chart using the existing Agent controls. This changes only the visible reading state and confirms it after rendering. Pass claim_id for evidence or chart_id from read_agent_answer for chart. It does not send a prompt, execute code or export results.',
      inputSchema: {
        type: 'object',
        properties: {
          ...answerProperties,
          target: { type: 'string', enum: ['answer', 'evidence', 'chart'] },
          claim_id: { type: 'string', minLength: 1 },
          chart_id: { type: 'string', pattern: '^chart-[1-9][0-9]*$' },
        },
        required: ['run_id', 'message_id', 'target'],
        additionalProperties: false,
      },
      annotations: { ...readAnnotations, readOnlyHint: false },
      async execute(input, { signal } = {}) {
        throwIfAborted(signal);
        const args = objectInput(
          input,
          ['run_id', 'message_id', 'target', 'claim_id', 'chart_id'],
          ['run_id', 'message_id', 'target'],
        );
        const ref = reference(args);
        const target = stringInput(args.target, 'target', {
          values: ['answer', 'evidence', 'chart'],
        });
        if (
          (target !== 'evidence' && args.claim_id !== undefined) ||
          (target !== 'chart' && args.chart_id !== undefined)
        )
          throw new Error('Only evidence accepts claim_id and only chart accepts chart_id');
        const { snapshot, entry } = selected(adapter, ref);
        const root = rootFor(adapter);
        const element = answerElement(root, entry.event);
        let button: HTMLButtonElement | undefined;
        let targetId: string | undefined;
        if (target === 'evidence') {
          targetId = stringInput(args.claim_id, 'claim_id', { minLength: 1 });
          const claim = claimsFor(entry.event).claims.find((item) => item.claim_id === targetId);
          if (!claim)
            throw new Error('This claim is unavailable or ambiguous in the selected answer');
          button = citationButtons(element, entry.event, claim)[0];
          if (!button)
            throw new Error('This claim has no visible citation in the current answer view');
        } else if (target === 'chart') {
          targetId = stringInput(args.chart_id, 'chart_id');
          const chart = chartsFor(entry.event).charts.find((item) => item.chart_id === targetId);
          if (!chart) throw new Error('This chart is no longer registered for the selected answer');
          button = chartButton(root, entry.event, chart.url);
          if (!button) throw new Error('This chart has no visible control in the selected answer');
        }
        const existingDialogs = [
          ...root.querySelectorAll<HTMLDialogElement>('dialog[data-answer-chart-dialog]'),
        ].filter(
          (dialog) =>
            dialog.open &&
            (target !== 'chart' || dialog.id !== button?.getAttribute('aria-controls')),
        );
        if (existingDialogs.length) {
          await commitUi(() => {
            selected(adapter, ref);
            for (const dialog of existingDialogs) {
              const close = dialog.querySelector<HTMLButtonElement>(
                'button[data-answer-chart-close]',
              );
              if (!close)
                throw new Error('The open chart cannot be closed with its visible control');
              close.click();
            }
          }, signal);
          if (existingDialogs.some((dialog) => dialog.isConnected && dialog.open))
            throw new Error('The previous chart did not close');
        }
        await commitUi(() => {
          selected(adapter, ref);
          const destination = button ?? element;
          destination.scrollIntoView({ behavior: 'instant', block: 'center' });
          destination.focus({ preventScroll: true });
          if (
            button &&
            (target === 'evidence'
              ? button.dataset.evidencePinned !== 'true'
              : button.getAttribute('aria-expanded') !== 'true')
          )
            button.click();
        }, signal);
        const latest = selected(adapter, ref);
        if (latest.snapshot.session.id !== snapshot.session.id || !element.isConnected)
          throw new Error('The selected answer changed during navigation');
        if (target === 'chart') {
          const chart = chartsFor(latest.entry.event).charts.find(
            (item) => item.chart_id === targetId,
          );
          if (!chart || chartButton(rootFor(adapter), latest.entry.event, chart.url) !== button)
            throw new Error('The chart was invalidated during navigation');
        } else if (target === 'evidence') {
          const claim = claimsFor(latest.entry.event).claims.find(
            (item) => item.claim_id === targetId,
          );
          if (!claim || !citationButtons(element, latest.entry.event, claim).includes(button!))
            throw new Error('The citation was invalidated during navigation');
        }
        if (target === 'answer') {
          if (element.ownerDocument.activeElement !== element)
            throw new Error('The answer could not receive focus');
        } else {
          const panelId = button?.getAttribute('aria-controls');
          const panel = panelId ? root.ownerDocument.getElementById(panelId) : null;
          if (
            !button?.isConnected ||
            button.getAttribute('aria-expanded') !== 'true' ||
            !panel ||
            !root.contains(panel) ||
            (target === 'chart' && !(panel as HTMLDialogElement).open) ||
            (target === 'evidence' && button.dataset.evidencePinned !== 'true')
          )
            throw new Error('The requested reading panel did not open');
        }
        return {
          status: 'opened',
          session_id: snapshot.session.id,
          ...summary(latest.entry, latest.snapshot),
          target,
          ...(targetId ? { target_id: targetId } : {}),
        };
      },
    },
  ];
}
