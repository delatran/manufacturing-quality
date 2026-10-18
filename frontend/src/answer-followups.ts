import type { Locale } from './i18n';
import type { Session, WorkspaceEvent } from './types';

export type AnswerFollowup = { id: string; label: string; prompt: string };

const record = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value);
const text = (event: WorkspaceEvent) => {
  if (!record(event) || !record(event.data)) return '';
  const value = event.data.text ?? event.data.content;
  return typeof value === 'string' ? value : '';
};
const positiveId = (value: unknown): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value > 0;
const verified = (event: WorkspaceEvent) =>
  record(event) && record(event.data) && record(event.data.qa) && event.data.qa.verified === true;

/** Display eligibility only. The server revalidates the persisted source on Send. */
export function eligibleAnswerFollowup(
  report: WorkspaceEvent,
  events: readonly WorkspaceEvent[],
  savedRuns: Session['runs'] = [],
): boolean {
  if (
    !record(report) ||
    !record(report.data) ||
    !Array.isArray(events) ||
    report.type !== 'message' ||
    report.data.role !== 'assistant' ||
    !report.session_id ||
    !report.run_id ||
    !/^[a-f0-9]{32}$/.test(report.run_id) ||
    !positiveId(report.id) ||
    !verified(report) ||
    !text(report).trim()
  )
    return false;
  const ownEvents = events.filter(
    (event) =>
      record(event) &&
      record(event.data) &&
      event.session_id === report.session_id &&
      event.run_id === report.run_id,
  );
  const source = [...ownEvents]
    .reverse()
    .find((event) => event.type === 'message' && event.data.role === 'assistant');
  if (!source || source.id !== report.id || !verified(source) || text(source) !== text(report))
    return false;
  const status = [...ownEvents].reverse().find((event) => event.type === 'status');
  // Live events supersede the saved run snapshot loaded when this tab connected.
  const saved = Array.isArray(savedRuns)
    ? savedRuns.find((run) => record(run) && run.id === report.run_id)
    : undefined;
  const state = status ? status.data.state : saved?.state;
  return state === 'completed';
}

export function answerSourceQuestion(
  report: WorkspaceEvent,
  question?: WorkspaceEvent,
): string | undefined {
  if (
    !record(report) ||
    !record(report.data) ||
    !question ||
    !record(question) ||
    !record(question.data) ||
    question.type !== 'message' ||
    question.data.role !== 'user' ||
    question.session_id !== report.session_id ||
    (question.run_id && question.run_id !== report.run_id) ||
    !positiveId(question.id) ||
    !positiveId(report.id) ||
    question.id >= report.id
  )
    return;
  return text(question).trim() || undefined;
}

const plain = (value: string) =>
  value
    .normalize('NFD')
    .replace(/\p{Diacritic}/gu, '')
    .toLowerCase()
    .replace(/đ/g, 'd')
    .replace(/[‘’]/g, "'")
    .replace(/\s+/g, ' ')
    .trim();

function structure(value: string) {
  const lines: string[] = [];
  let fence: { character: string; length: number; root: boolean; content: boolean } | undefined;
  let code = false;
  for (const line of value.replace(/<!--[\s\S]*?-->/g, '').split(/\r?\n/)) {
    if (/^ {0,3}>/.test(line)) continue;
    if (fence) {
      const close = /^ {0,3}(`+|~+)\s*$/.exec(line);
      if (close && close[1][0] === fence.character && close[1].length >= fence.length) {
        code ||= fence.root && fence.content;
        fence = undefined;
      } else if (line.trim()) fence.content = true;
      continue;
    }
    const open = /^( {0,3})(`{3,}|~{3,})([^\r\n]*)$/.exec(line);
    if (open && (open[2][0] !== '`' || !open[3].includes('`'))) {
      fence = {
        character: open[2][0],
        length: open[2].length,
        root: open[1].length === 0,
        content: false,
      };
      continue;
    }
    if (!/^(?: {4}|\t)/.test(line)) lines.push(line);
  }
  const cells = (line: string) =>
    line
      .replace(/\\\|/g, '')
      .trim()
      .replace(/^\||\|$/g, '')
      .split('|');
  const table = lines.some((line, index) => {
    if (!line.includes('|') || index === 0 || index + 1 >= lines.length) return false;
    const columns = cells(line);
    return (
      columns.length >= 2 &&
      columns.every((cell) => /^\s*:?-{3,}:?\s*$/.test(cell)) &&
      cells(lines[index - 1]).length === columns.length &&
      cells(lines[index + 1]).length === columns.length &&
      cells(lines[index - 1]).some((cell) => cell.trim()) &&
      cells(lines[index + 1]).some((cell) => cell.trim())
    );
  });
  return { code, table, prose: lines.join('\n').trim(), incomplete: Boolean(fence) };
}

function registeredChart(report: WorkspaceEvent): boolean {
  const urls = report.data.chart_urls;
  const delivery = report.data.delivery;
  if (
    !Array.isArray(urls) ||
    !urls.length ||
    !urls.every((url) => typeof url === 'string' && url) ||
    !record(delivery) ||
    delivery.version !== 1 ||
    !Array.isArray(delivery.artifacts)
  )
    return false;
  return delivery.artifacts.some((artifact: unknown) => {
    if (
      !record(artifact) ||
      typeof artifact.url !== 'string' ||
      !artifact.url.startsWith('/') ||
      artifact.url.startsWith('//') ||
      typeof artifact.path !== 'string' ||
      artifact.path.includes('\\') ||
      artifact.path.split('/').some((part) => !part || part === '.' || part === '..') ||
      typeof artifact.mime_type !== 'string' ||
      !artifact.mime_type.startsWith('image/') ||
      typeof artifact.sha256 !== 'string' ||
      !/^[a-f0-9]{64}$/i.test(artifact.sha256)
    )
      return false;
    try {
      const url = new URL(artifact.url, 'https://workspace.invalid');
      const expected = `/api/sessions/${report.session_id}/runs/${report.run_id}/artifacts/${artifact.path}`;
      return (
        !url.search &&
        !url.hash &&
        decodeURIComponent(url.pathname).endsWith(expected) &&
        urls.some((selected: string) => {
          const allowed = new URL(selected, 'https://workspace.invalid');
          return (
            (allowed.protocol === 'http:' || allowed.protocol === 'https:') &&
            !allowed.search &&
            !allowed.hash &&
            decodeURIComponent(allowed.pathname).endsWith(expected)
          );
        })
      );
    } catch {
      return false;
    }
  });
}

function hasNumericEvidence(report: WorkspaceEvent): boolean {
  return (
    Array.isArray(report.data.claims) &&
    report.data.claims.some(
      (claim: unknown) =>
        record(claim) &&
        typeof claim.id === 'string' &&
        Boolean(claim.id.trim()) &&
        typeof claim.execution_id === 'string' &&
        Boolean(claim.execution_id.trim()) &&
        typeof claim.path === 'string' &&
        Boolean(claim.path.trim()) &&
        typeof claim.code_sha256 === 'string' &&
        /^[a-f0-9]{64}$/i.test(claim.code_sha256) &&
        typeof claim.value === 'number' &&
        Number.isFinite(claim.value),
    )
  );
}

const copy = {
  en: {
    code: [
      'Explain this example',
      'Explain the code example in this answer step by step. Distinguish what the example demonstrates from anything that was actually executed.',
    ],
    edge: [
      'Check edge cases',
      'Review the code example in this answer for assumptions and edge cases. Explain its limitations without executing code or changing data.',
    ],
    table: [
      'Explain this table',
      'Explain how to read the table in this answer, including the meaning of its columns and any stated limits. Use the existing values without inventing new results.',
    ],
    chart: [
      'Explain this chart',
      'Explain how to read the registered chart in this answer, including its axes, units and stated limits. Do not infer values that are not supported by the existing evidence.',
    ],
    finding: [
      'Explain the finding',
      'Explain the finding in this answer using its existing verified figures and evidence. Separate observations from interpretations.',
    ],
    limits: [
      'Check the limits',
      'Review the assumptions, coverage and limitations of this answer. Explain what the existing evidence supports and what remains uncertain.',
    ],
    next: [
      'Choose the next check',
      'Based on this answer, suggest the next useful check and explain what it could establish. Do not create, update or assign work, change filters, or claim that the check has been performed.',
    ],
    concept: [
      'Work through an example',
      'Explain the concept in this answer through a small, clearly labelled illustrative example. Keep the illustration separate from recorded results and do not change data.',
    ],
  },
  vi: {
    code: [
      'Giải thích ví dụ',
      'Giải thích từng bước ví dụ code trong câu trả lời này. Phân biệt điều ví dụ minh hoạ với những gì thực sự đã được chạy.',
    ],
    edge: [
      'Kiểm tra trường hợp biên',
      'Rà soát giả định và các trường hợp biên của ví dụ code trong câu trả lời này. Giải thích giới hạn mà không chạy code hoặc thay đổi dữ liệu.',
    ],
    table: [
      'Giải thích bảng',
      'Giải thích cách đọc bảng trong câu trả lời này, gồm ý nghĩa các cột và giới hạn đã nêu. Dùng các giá trị hiện có, không tạo kết quả mới.',
    ],
    chart: [
      'Giải thích biểu đồ',
      'Giải thích cách đọc biểu đồ đính kèm trong câu trả lời này, gồm trục, đơn vị và giới hạn đã nêu. Không suy đoán các giá trị thiếu căn cứ từ bằng chứng hiện có.',
    ],
    finding: [
      'Giải thích phát hiện',
      'Giải thích phát hiện trong câu trả lời này bằng số liệu và bằng chứng đã xác minh. Phân biệt quan sát với diễn giải.',
    ],
    limits: [
      'Xem giới hạn',
      'Rà soát giả định, độ bao phủ và giới hạn của câu trả lời này. Giải thích bằng chứng hiện có hỗ trợ điều gì và điều gì vẫn chưa chắc chắn.',
    ],
    next: [
      'Chọn bước kiểm tra tiếp',
      'Dựa trên câu trả lời này, đề xuất bước kiểm tra tiếp theo hữu ích và giải thích nó có thể làm rõ điều gì. Không tạo, sửa hoặc giao công việc, đổi bộ lọc hay khẳng định đã thực hiện bước kiểm tra.',
    ],
    concept: [
      'Xem ví dụ minh hoạ',
      'Giải thích khái niệm trong câu trả lời này bằng một ví dụ nhỏ, ghi rõ là minh hoạ. Tách ví dụ khỏi kết quả đã ghi nhận và không thay đổi dữ liệu.',
    ],
  },
} satisfies Record<Locale, Record<string, readonly [string, string]>>;

/** Conservative suggestions from the source answer, never a business-intent guess. */
export function answerFollowups(
  report: WorkspaceEvent,
  question: WorkspaceEvent | undefined,
  language: Locale,
): AnswerFollowup[] {
  if (
    !record(report) ||
    !record(report.data) ||
    report.type !== 'message' ||
    report.data.role !== 'assistant' ||
    !verified(report)
  )
    return [];
  const value = text(report);
  if (!value.trim()) return [];
  const features = structure(value);
  if (features.incomplete) return [];
  const prose = plain(
    features.prose
      .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
      .replace(/[`*_~]/g, '')
      .replace(/^[#\s]+/, ''),
  );
  const opening = prose.replace(/^(?:unfortunately|sorry)[,.]?\s*/, '');
  const questionText = plain(answerSourceQuestion(report, question) || '').replace(/[.!?,]+$/g, '');
  if (
    /^(?:(?:hi|hello|hey|chao|xin chao|alo|thanks|thank you|cam on|ok|okay|oke|yes|no)[!,.\s]*)+$/.test(
      questionText,
    ) ||
    /^(?:there (?:is|are) )?no (?:matching )?(?:data|rows|records|results)\b/.test(opening) ||
    /^(?:i (?:cannot|can't|could not|couldn't|am unable to) (?:help|assist|retrieve|access)|(?:khong|chua) co (?:du lieu|ban ghi|ket qua)|(?:toi|minh|tao) khong the (?:giup|ho tro|truy cap|lay du lieu))\b/.test(
      opening,
    ) ||
    /\b(?:could you clarify|please clarify|can you specify|what do you mean|which .{0,60}would you like|ban muon .{0,80}\?|vui long (?:lam ro|cho biet)|can ban (?:lam ro|xac nhan))/.test(
      prose,
    ) ||
    (!features.code && !features.table && /\?\s*$/.test(prose))
  )
    return [];
  const numeric = hasNumericEvidence(report);
  const keys: (keyof (typeof copy)['en'])[] = registeredChart(report)
    ? numeric
      ? ['chart', 'limits', 'next']
      : ['chart', 'limits']
    : features.code
      ? ['code', 'edge']
      : features.table
        ? ['table', 'limits']
        : numeric
          ? ['finding', 'limits', 'next']
          : /\b(?:what (?:is|are|does)|why|how (?:does|do|to)|explain|define|difference|distinction|giai thich|la gi|tai sao|vi sao|dinh nghia|khac nhau)\b/.test(
                questionText,
              )
            ? ['concept']
            : [];
  return keys.map((key) => ({
    id: key,
    label: copy[language][key][0],
    prompt: copy[language][key][1],
  }));
}
