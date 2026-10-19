import { splitSummaryPart, type FlowPart } from './agent-flow';

const displayedParts = new WeakMap<FlowPart, FlowPart>();

function legacySourceLanguage(text: string): 'en' | 'vi' {
  const prose = text.replace(/```[\s\S]*?```|`[^`\n]+`|"[^"\n]*"|“[^”\n]*”/g, ' ').normalize('NFC');
  return /[ăâđêôơưàáạảãằắặẳẵầấậẩẫèéẹẻẽềếệểễìíịỉĩòóọỏõồốộổỗờớợởỡùúụủũừứựửữỳýỵỷỹ]/i.test(prose)
    ? 'vi'
    : 'en';
}

/** Show the original public provider text, including partial stream and replay.
 * Historical translated snapshots remain in the event projection for auditing;
 * they do not replace the source when the interface language changes.
 */
export function flowSummaryDisplay(part: FlowPart): FlowPart {
  // Published parts are immutable and shared across conversation updates. Weak
  // keys reuse their display work without retaining discarded session history.
  const displayed = displayedParts.get(part);
  if (displayed) return displayed;
  const result = projectSummaryDisplay(part);
  displayedParts.set(part, result);
  return result;
}

function projectSummaryDisplay(part: FlowPart): FlowPart {
  const original = part.raw || (part.displaySource === 'provider_summary' ? part.displayText : '');
  if (original) {
    const summary = splitSummaryPart(original);
    // Original provider metadata takes precedence over a later translation's
    // language. Legacy snapshots without that metadata use their own heading
    // or prose, excluding quoted labels and code from the language signal.
    const sourceLanguage =
      part.sourceLanguage ??
      (part.displaySource === 'provider_summary' && part.displayLocalized !== true
        ? part.displayLanguage
        : undefined) ??
      legacySourceLanguage(summary.title || original);
    return {
      ...part,
      ...summary,
      displaySource: 'provider_summary',
      displayLanguage: sourceLanguage,
      displayLocalized: false,
    };
  }
  // A recorded task status remains explicitly labeled when no public summary
  // exists. Never manufacture a status to replace available provider text.
  if (part.displaySource === 'task_status') return part;
  return { ...part, title: '', body: '' };
}
