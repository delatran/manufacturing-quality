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
export const pendingPromptStoragePrefix = 'analyst.workspace.pending-prompt.v2:';
type PromptStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;
export type PromptRequestStorage = {
  records: PromptStorage;
  recovery: PromptStorage;
};
function memoryStorage(): PromptStorage {
  const values = new Map<string, string>();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => {
      values.set(key, value);
    },
    removeItem: (key) => {
      values.delete(key);
    },
  };
}
const fallbackRecords = memoryStorage();
const fallbackRecovery = memoryStorage();

function browserStorage(name: 'localStorage' | 'sessionStorage', fallback: PromptStorage) {
  try {
    return window[name];
  } catch {
    return fallback;
  }
}
export function promptRequestStorage(): PromptRequestStorage {
  return {
    records: browserStorage('localStorage', fallbackRecords),
    recovery: browserStorage('sessionStorage', fallbackRecovery),
  };
}
const recoveryKey = (sessionId: string) =>
  `${pendingPromptStoragePrefix}session:${encodeURIComponent(sessionId)}`;
const recordKey = (sessionId: string, requestId: string) =>
  `${pendingPromptStoragePrefix}request:${encodeURIComponent(sessionId)}:${requestId}`;
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

function readStoredPendingPrompt(
  storage: Pick<Storage, 'getItem'>,
  storageKey: string,
  sessionId: string,
): PendingPromptRequest | undefined {
  try {
    const value: unknown = JSON.parse(storage.getItem(storageKey) || 'null');
    if (!isRecord(value) || value.session_id !== sessionId || !isRecord(value.request)) return;
    const request = parseRequest(value.request);
    const key = value.request.client_request_id;
    if (!request || !validClientRequestId(key)) return;
    return { session_id: sessionId, request: { ...request, client_request_id: key } };
  } catch {
    return;
  }
}

export function readPendingPromptRequest(
  storage: PromptRequestStorage,
  sessionId: string,
): PendingPromptRequest | undefined {
  try {
    const requestId = storage.recovery.getItem(recoveryKey(sessionId));
    if (validClientRequestId(requestId)) {
      const pending = readStoredPendingPrompt(
        storage.records,
        recordKey(sessionId, requestId),
        sessionId,
      );
      return pending?.request.client_request_id === requestId ? pending : undefined;
    }
  } catch {
    // Storage restrictions do not authorize replaying a request.
  }
  const legacy = readStoredPendingPrompt(storage.records, pendingPromptStorageKey, sessionId);
  if (legacy) savePendingPromptRequest(storage, legacy);
  return legacy;
}

export function savePendingPromptRequest(
  storage: PromptRequestStorage,
  pending: PendingPromptRequest,
): void {
  // Independent records avoid cross-tab overwrites. Only this tab chooses its recovery key.
  // Store before sending. The server remains the authority on request admission.
  try {
    storage.records.setItem(
      recordKey(pending.session_id, pending.request.client_request_id),
      JSON.stringify(pending),
    );
    storage.recovery.setItem(recoveryKey(pending.session_id), pending.request.client_request_id);
  } catch {
    // A tab without storage retains its key in memory and in visible readback.
  }
}

export function clearPendingPromptRequest(
  storage: PromptRequestStorage,
  sessionId: string,
  clientRequestId: string,
): void {
  try {
    storage.records.removeItem(recordKey(sessionId, clientRequestId));
    const legacy = readStoredPendingPrompt(storage.records, pendingPromptStorageKey, sessionId);
    if (legacy?.request.client_request_id === clientRequestId)
      storage.records.removeItem(pendingPromptStorageKey);
  } catch {
    // A denied storage operation must not prevent applying a confirmed receipt.
  }
  try {
    if (storage.recovery.getItem(recoveryKey(sessionId)) === clientRequestId)
      storage.recovery.removeItem(recoveryKey(sessionId));
  } catch {
    // Another request's recovery identity must not be removed.
  }
}
