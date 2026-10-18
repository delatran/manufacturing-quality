import type { FlowPart } from './agent-flow';
import type { Locale } from './i18n';

function narrative(value: string): string {
  return value
    .replace(/`[^`]*(?:`|$)/g, '')
    .replace(/["“][^"”]*(?:["”]|$)/g, '')
    .trim();
}

// These are conservative display checks, not a translation or a language claim
// about the provider. Quoted labels cannot establish the narrative's language.
const vietnameseProse =
  /(?:^|[^\p{L}])(?:đang|đã|cần|sẽ|được|chưa|không|tôi|mình|dữ liệu|phạm vi|kết quả|yêu cầu|bằng chứng|đối chiếu|kiểm tra|phân tích|so sánh|báo cáo)(?=$|[^\p{L}])/iu;
const englishProse =
  /(?:^|[.!?\n]\s*)(?:(?:i|we|the|this|that|it|there|our|need|must|should|will|let|review|check|compare|prepare|analyze|analyse|assess|evaluate|inspect|ensure|consider|determine|use|plan|next|no)\b|[a-z]+ing\b)|\b(?:need to|have to|will be|should be|must be|the user|the data|in the|of the|for the|to the|with the)\b/i;

/** Project a part for the flow without changing its retained provider payload. */
export function flowSummaryDisplay(part: FlowPart, language: Locale): FlowPart {
  if ((!part.title && !part.body) || part.displaySource === 'task_status') return part;
  const body = narrative(part.body || part.title);
  const provenSnapshot =
    part.done && part.displaySource === 'provider_summary' && typeof part.displayText === 'string';
  const matchesLanguage =
    language === 'vi'
      ? vietnameseProse.test(body) && !englishProse.test(body)
      : !vietnameseProse.test(body);
  if (provenSnapshot && matchesLanguage) {
    const title = narrative(part.title);
    const matchesTitle =
      !title ||
      (language === 'vi'
        ? vietnameseProse.test(title) && !englishProse.test(title)
        : !vietnameseProse.test(title));
    return matchesTitle ? part : { ...part, title: '' };
  }
  const status =
    language === 'vi'
      ? 'Đang xem xét yêu cầu và bằng chứng để chuẩn bị câu trả lời.'
      : 'Reviewing the request and evidence to prepare the answer.';
  return {
    ...part,
    title: '',
    body: status,
    displayText: status,
    displaySource: 'task_status',
  };
}
