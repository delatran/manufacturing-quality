import type { AnswerReference } from './answer-reply';
import type { Locale } from './i18n';

export type PromptRequest = {
  text: string;
  analysis_context_id?: string | null;
  language: Locale;
  action_intent: 'none' | 'draft_create' | 'draft_update';
  steer_run_id?: string | null;
  reply_to?: AnswerReference | null;
  client_request_id: string;
  request_origin: 'ui' | 'webmcp';
};
export type PromptReceipt = {
  client_request_id: string;
  session_id: string;
  status: 'reserved' | 'accepted' | 'rejected' | 'uncertain';
  run_id: string | null;
  action: 'start' | 'steer';
  request: Omit<PromptRequest, 'client_request_id'>;
  request_sha256: string;
  replayed: boolean;
  queued?: boolean;
  steering?: boolean;
  instruction_id?: string;
  code?: string;
  message?: string;
};
export type PendingPromptRequest = {
  session_id: string;
  request: PromptRequest;
};
export const pendingPromptStorageKey = 'analyst.workspace.pending-prompt.v1';
type PromptStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;
const volatileStorage = new Map<string, string>();
const fallbackStorage: PromptStorage = {
  getItem: (key) => volatileStorage.get(key) ?? null,
  setItem: (key, value) => {
    volatileStorage.set(key, value);
  },
  removeItem: (key) => {
    volatileStorage.delete(key);
  },
};

export function promptRequestStorage(): PromptStorage {
  try {
    return localStorage;
  } catch {
    return fallbackStorage;
  }
}
const isRecord = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value);

export function validClientRequestId(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value)
  );
}

export function createClientRequestId(
  source: Pick<Crypto, 'getRandomValues'> & Partial<Pick<Crypto, 'randomUUID'>> = crypto,
): string {
  if (source.randomUUID) return source.randomUUID();
  const bytes = source.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/** Canonical fields also returned by the server's durable admission receipt. */
export function canonicalPromptRequest(request: Omit<PromptRequest, 'client_request_id'>) {
  return {
    text: request.text.trim(),
    analysis_context_id: request.analysis_context_id ?? null,
    language: request.language,
    action_intent: request.action_intent,
    steer_run_id: request.steer_run_id ?? null,
    reply_to: request.reply_to
      ? { run_id: request.reply_to.run_id, message_id: request.reply_to.message_id }
      : null,
    request_origin: request.request_origin,
  };
}

function parseRequest(value: unknown): Omit<PromptRequest, 'client_request_id'> | undefined {
  if (!isRecord(value)) return;
  if (
    typeof value.text !== 'string' ||
    !value.text.trim() ||
    (value.analysis_context_id != null && typeof value.analysis_context_id !== 'string') ||
    (value.language !== 'en' && value.language !== 'vi') ||
    !['none', 'draft_create', 'draft_update'].includes(String(value.action_intent)) ||
    (value.steer_run_id != null &&
      (typeof value.steer_run_id !== 'string' || !/^[a-f0-9]{32}$/.test(value.steer_run_id))) ||
    (value.request_origin !== 'ui' && value.request_origin !== 'webmcp')
  )
    return;
  if (
    value.reply_to != null &&
    (!isRecord(value.reply_to) ||
      Object.keys(value.reply_to).length !== 2 ||
      typeof value.reply_to.run_id !== 'string' ||
      !/^[a-f0-9]{32}$/.test(value.reply_to.run_id) ||
      !Number.isSafeInteger(value.reply_to.message_id) ||
      Number(value.reply_to.message_id) <= 0 ||
      value.steer_run_id != null ||
      value.action_intent !== 'none')
  )
    return;
  return canonicalPromptRequest(value as Omit<PromptRequest, 'client_request_id'>);
}

export function parsePromptReceipt(value: unknown, sessionId: string): PromptReceipt | undefined {
  if (!isRecord(value)) return;
  const request = parseRequest(value.request);
  if (
    !request ||
    value.session_id !== sessionId ||
    !validClientRequestId(value.client_request_id) ||
    !['reserved', 'accepted', 'rejected', 'uncertain'].includes(String(value.status)) ||
    (value.run_id !== null &&
      (typeof value.run_id !== 'string' || !/^[a-f0-9]{32}$/.test(value.run_id))) ||
    (value.action !== 'start' && value.action !== 'steer') ||
    typeof value.request_sha256 !== 'string' ||
    !/^[a-f0-9]{64}$/.test(value.request_sha256) ||
    typeof value.replayed !== 'boolean' ||
    ['queued', 'steering'].some(
      (key) => value[key] !== undefined && typeof value[key] !== 'boolean',
    )
  )
    return;
  return {
    client_request_id: value.client_request_id,
    session_id: sessionId,
    status: value.status as PromptReceipt['status'],
    run_id: value.run_id,
    action: value.action,
    request,
    request_sha256: value.request_sha256,
    replayed: value.replayed,
    ...(typeof value.queued === 'boolean' ? { queued: value.queued } : {}),
    ...(typeof value.steering === 'boolean' ? { steering: value.steering } : {}),
    ...(typeof value.instruction_id === 'string' ? { instruction_id: value.instruction_id } : {}),
    ...(typeof value.code === 'string' ? { code: value.code } : {}),
    ...(typeof value.message === 'string' ? { message: value.message } : {}),
  };
}

export function samePromptRequest(
  left: Omit<PromptRequest, 'client_request_id'>,
  right: Omit<PromptRequest, 'client_request_id'>,
): boolean {
  return (
    JSON.stringify(canonicalPromptRequest(left)) === JSON.stringify(canonicalPromptRequest(right))
  );
}

export function readPendingPromptRequest(
  storage: Pick<Storage, 'getItem'>,
  sessionId: string,
): PendingPromptRequest | undefined {
  try {
    const value: unknown = JSON.parse(storage.getItem(pendingPromptStorageKey) || 'null');
    if (!isRecord(value) || value.session_id !== sessionId || !isRecord(value.request)) return;
    const request = parseRequest(value.request);
    const key = value.request.client_request_id;
    if (!request || !validClientRequestId(key)) return;
    return { session_id: sessionId, request: { ...request, client_request_id: key } };
  } catch {
    return;
  }
}

export function savePendingPromptRequest(
  storage: Pick<Storage, 'setItem' | 'getItem' | 'removeItem'>,
  pending: PendingPromptRequest,
): void {
  // Store before sending. The server remains the authority across tabs/restarts.
  try {
    storage.setItem(pendingPromptStorageKey, JSON.stringify(pending));
  } catch {
    // A tab without storage retains its key in memory and in visible readback.
  }
}

export function clearPendingPromptRequest(
  storage: Pick<Storage, 'getItem' | 'removeItem'>,
  sessionId: string,
  clientRequestId: string,
): void {
  try {
    const saved = readPendingPromptRequest(storage, sessionId);
    if (saved?.request.client_request_id === clientRequestId)
      storage.removeItem(pendingPromptStorageKey);
  } catch {
    // Another tab's pending request must not be removed.
  }
}
