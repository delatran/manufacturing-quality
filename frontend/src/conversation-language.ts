import type { Locale } from './i18n';
import type { Session, WorkspaceEvent } from './types';

const isLocale = (value: unknown): value is Locale => value === 'en' || value === 'vi';

export function conversationRunLanguages(
  events: WorkspaceEvent[],
  savedRuns: Session['runs'] = [],
): Map<string, Locale> {
  const languages = new Map<string, Locale>();
  // Persisted run metadata survives a reload even when the event stream is partial.
  for (const run of savedRuns) {
    if (isLocale(run.language)) languages.set(run.id, run.language);
  }
  for (const event of events) {
    if (event.run_id && isLocale(event.data.language) && !languages.has(event.run_id))
      languages.set(event.run_id, event.data.language);
  }
  return languages;
}

export function conversationEventLanguage(
  event: WorkspaceEvent | undefined,
  languages: Map<string, Locale>,
  fallback: Locale,
): Locale {
  if (!event) return fallback;
  const runLanguage = event.run_id ? languages.get(event.run_id) : undefined;
  return runLanguage ?? (isLocale(event.data.language) ? event.data.language : fallback);
}
