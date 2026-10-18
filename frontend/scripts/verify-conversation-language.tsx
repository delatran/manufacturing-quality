import assert from 'node:assert/strict';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { conversationEventLanguage, conversationRunLanguages } from '../src/conversation-language';
import { I18nProvider, I18nScope, useI18n } from '../src/i18n';
import type { WorkspaceEvent } from '../src/types';

Object.defineProperty(globalThis, 'React', { value: React });

const event = (run: string | undefined, language: unknown): WorkspaceEvent => ({
  id: `${run}-${String(language)}`,
  session_id: 'conversation',
  run_id: run,
  type: 'status',
  time: '',
  data: { language },
});
const events = [event('english', 'en'), event('vietnamese', 'vi'), event(undefined, 'en')];
const languages = conversationRunLanguages(events);
assert.equal(conversationEventLanguage(events[0], languages, 'vi'), 'en');
assert.equal(conversationEventLanguage(events[1], languages, 'en'), 'vi');
assert.equal(conversationEventLanguage(event('vietnamese', 'en'), languages, 'en'), 'vi');
assert.equal(conversationEventLanguage(event(undefined, 'vi'), languages, 'en'), 'vi');
assert.equal(conversationEventLanguage(event('legacy', null), languages, 'vi'), 'vi');
assert.equal(conversationEventLanguage(undefined, languages, 'en'), 'en');
assert.equal(languages.has('session'), false);

const restored = conversationRunLanguages(
  [event('restored', 'en')],
  [{ id: 'restored', state: 'completed', language: 'vi' }],
);
assert.equal(restored.get('restored'), 'vi');
assert.equal(conversationRunLanguages([event('invalid', 'fr')]).size, 0);

function Label() {
  const { t } = useI18n();
  return <span>{t('ui.next_steps')}</span>;
}
const markup = renderToStaticMarkup(
  <I18nProvider initialLocale="vi" persist={false}>
    <I18nScope locale="en">
      <Label />
    </I18nScope>
    <I18nScope locale="vi">
      <Label />
    </I18nScope>
    <Label />
  </I18nProvider>,
);
assert.match(markup, /<span>Next steps<\/span>/);
assert.equal((markup.match(/Bước tiếp theo/g) || []).length, 2);
console.log('Conversation language verification passed: per-run, restored and scoped labels.');
