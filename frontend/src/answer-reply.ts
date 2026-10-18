import type { Locale } from './i18n';
import type { Session, WorkspaceEvent } from './types';
import { eligibleAnswerFollowup } from './answer-followups';

export type AnswerReference = { run_id: string; message_id: number };
export type PromptAdmission = {
  text: string;
  analysis_context_id?: string;
  action_intent?: string;
  reply_to?: AnswerReference;
};
export type AnswerReply = AnswerReference & {
  sessionId: string;
  language: Locale;
  question?: string;
};

export function parseAnswerReply(value: unknown): AnswerReply | undefined {
  if (!value || typeof value !== 'object') return;
  const reply = value as Record<string, unknown>;
  if (
    typeof reply.sessionId !== 'string' ||
    !reply.sessionId ||
    typeof reply.run_id !== 'string' ||
    !/^[a-f0-9]{32}$/.test(reply.run_id) ||
    typeof reply.message_id !== 'number' ||
    !Number.isSafeInteger(reply.message_id) ||
    reply.message_id <= 0 ||
    (reply.language !== 'en' && reply.language !== 'vi')
  )
    return;
  return {
    sessionId: reply.sessionId,
    run_id: reply.run_id,
    message_id: reply.message_id,
    language: reply.language,
    ...(typeof reply.question === 'string' ? { question: reply.question } : {}),
  };
}

export function answerReplyAvailable(
  reply: AnswerReply | undefined,
  session: Session | null,
  events: readonly WorkspaceEvent[],
): boolean {
  if (!reply) return true;
  if (!session || session.id !== reply.sessionId) return false;
  const source = events.find(
    (event) =>
      event.id === reply.message_id &&
      event.run_id === reply.run_id &&
      event.session_id === reply.sessionId,
  );
  return Boolean(source && eligibleAnswerFollowup(source, events, session.runs));
}

export function questionForAnswer(
  report: WorkspaceEvent,
  events: readonly WorkspaceEvent[],
): WorkspaceEvent | undefined {
  // New runs record their exact admitted question. Older history keeps a neutral
  // source label rather than guessing from unrelated queued user messages.
  const started = events.find(
    (event) =>
      event.type === 'run_started' &&
      event.session_id === report.session_id &&
      event.run_id === report.run_id &&
      typeof event.data.question === 'string',
  );
  if (!started || !(started.data.question as string).trim()) return;
  return { ...started, type: 'message', data: { role: 'user', text: started.data.question } };
}

export function promptAcknowledges(
  event: WorkspaceEvent,
  pending: PromptAdmission | null,
): boolean {
  if (
    !pending ||
    event.type !== 'message' ||
    event.data.role !== 'user' ||
    event.data.text !== pending.text
  )
    return false;
  if (
    (event.data.analysis_context_id ?? null) !== (pending.analysis_context_id ?? null) ||
    (event.data.action_intent ?? 'none') !== (pending.action_intent ?? 'none')
  )
    return false;
  const actual = event.data.reply_to;
  if (!pending.reply_to) return actual === undefined || actual === null;
  return Boolean(
    actual &&
    typeof actual === 'object' &&
    !Array.isArray(actual) &&
    Object.keys(actual).length === 2 &&
    (actual as Record<string, unknown>).run_id === pending.reply_to.run_id &&
    (actual as Record<string, unknown>).message_id === pending.reply_to.message_id,
  );
}
