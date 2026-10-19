import { splitSummaryPart, type FlowPart } from './agent-flow';
import type { Locale } from './i18n';

/** Show public provider text immediately, including partial stream and replay.
 * A translation requires explicit server metadata. Language heuristics cannot
 * establish that a generic status or paraphrase is the model's summary.
 */
export function flowSummaryDisplay(part: FlowPart, language: Locale): FlowPart {
  const translated =
    part.displaySource === 'translated_provider_summary' &&
    part.displayLocalized === true &&
    part.displayLanguage === language &&
    !!part.displayText?.trim();
  if (translated) return { ...part, ...splitSummaryPart(part.displayText!) };
  const original = part.raw || (part.displaySource === 'provider_summary' ? part.displayText : '');
  if (original) {
    return {
      ...part,
      ...splitSummaryPart(original),
      displaySource: 'provider_summary',
      displayLocalized: false,
    };
  }
  // A recorded task status remains explicitly labeled when no public summary
  // exists. Never manufacture a status to replace available provider text.
  if (part.displaySource === 'task_status') return part;
  return { ...part, title: '', body: '' };
}
