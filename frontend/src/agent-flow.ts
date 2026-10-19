import type { WorkspaceEvent } from './types';
import type { Locale } from './i18n';

// A read-only projection of the stored event history into the provider's own
// processing order. Every step is backed by an event the server recorded;
// terminal runs mark missing completion as unconfirmed. Source events stay intact.

export type FlowPart = {
  index: number;
  raw: string;
  title: string;
  body: string;
  done: boolean;
  displayText?: string;
  displaySource?: 'task_status' | 'provider_summary' | 'translated_provider_summary';
  displayLanguage?: Locale;
  displayLocalized?: boolean;
  translationState?: 'pending' | 'translated' | 'failed' | 'skipped' | 'cancelled';
};

export type FlowThinking = {
  kind: 'thinking';
  id: string;
  parts: FlowPart[];
  simulation: boolean;
  done: boolean;
  stopped?: boolean;
  durationMs?: number;
  startedAt?: number;
};
export type FlowCallStatus =
  'pending' | 'running' | 'completed' | 'failed' | 'cancelled' | 'unconfirmed';
export type FlowCall = {
  kind: 'call';
  id: string;
  name: string;
  command: string;
  streaming: boolean;
  status: FlowCallStatus;
  outcome: string;
  output: string;
  implementationSource?: string;
  executionId?: string;
  codeSha256?: string;
  durationMs?: number;
  rationale?: string;
};
export type FlowAnswer = { kind: 'answer'; id: string; simulation: boolean };
export type FlowDraft = {
  kind: 'draft';
  id: string;
  text: string;
  messagePhase?: string;
  done?: boolean;
};
export type FlowNote = {
  kind: 'note';
  id: string;
  tone: 'info' | 'warn';
  label: string;
  detail: string;
};
export type FlowFailure = { kind: 'failure'; id: string; message: string; code: string };
export type FlowStep = FlowThinking | FlowCall | FlowAnswer | FlowDraft | FlowNote | FlowFailure;

export const stoppedFlowStates = new Set([
  'failed',
  'error',
  'cancelled',
  'interrupted',
  'unverified',
  'budget_exceeded',
  'budget_exhausted',
  'provider_stopped',
  'refused',
  'incomplete',
  'timeout',
  'unavailable',
]);
export const completedFlowStates = new Set(['completed', 'complete', 'done', 'success', 'idle']);

export type FlowRun = {
  runId: string;
  sessionId: string;
  model: string;
  effort: string;
  background: boolean;
  state: string;
  prompt: string;
  language?: Locale;
  steps: FlowStep[];
  compactions: number;
};

const text = (value: unknown) => (typeof value === 'string' ? value : '');
const record = (value: unknown): Record<string, unknown> | undefined =>
  value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
const count = (value: unknown) => (typeof value === 'number' && Number.isFinite(value) ? value : 0);

// Reasoning summary parts are emitted as "**Title**\n\nbody" by the API.
export function splitSummaryPart(raw: string): { title: string; body: string } {
  const match = /^\s*\*\*(.+?)\*\*[ \t]*(?:\r?\n+|$)([\s\S]*)$/.exec(raw);
  if (match) return { title: match[1].trim(), body: match[2].trim() };
  const inline = /^\s*\*\*(.+?)\*\*[ \t]*(.*)$/.exec(raw);
  if (inline) return { title: inline[1].trim(), body: inline[2].trim() };
  return { title: '', body: raw.trim() };
}

function truncate(value: string, limit: number): string {
  const flat = value.replace(/\s+/g, ' ').trim();
  return flat.length > limit ? flat.slice(0, limit - 1) + '…' : flat;
}

// A compact one-line outcome taken from the receipt the tool actually returned.
export function callOutcome(result: unknown): string {
  const value = record(result);
  if (!value) return '';
  if (value.ok === false) return truncate(text(value.error) || 'error', 140);
  const facts: string[] = [];
  for (const [key, item] of Object.entries(value)) {
    if (key === 'ok' || item === null || item === undefined) continue;
    if (typeof item === 'number' && Number.isFinite(item))
      facts.push(`${key}=${Number.isInteger(item) ? item : item.toFixed(3)}`);
    else if (typeof item === 'boolean') facts.push(`${key}=${item}`);
    else if (typeof item === 'string' && item.length <= 48) facts.push(`${key}=${item}`);
    else if (Array.isArray(item)) facts.push(`${key}[${item.length}]`);
    else if (record(item)) facts.push(`${key}{${Object.keys(record(item)!).length}}`);
    if (facts.length >= 6) break;
  }
  return facts.join(' · ');
}

function stableJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 1) || '';
  } catch {
    return '';
  }
}

type Builder = FlowRun & {
  thinking: Map<string, FlowThinking>;
  calls: Map<string, FlowCall>;
  drafts: Map<string, FlowDraft>;
  messagePhases: Map<string, string>;
  plans: Map<string, FlowNote>;
  providerEvents: Set<string>;
  providerPhases: Map<string, FlowNote>;
  compactionIds: Set<string>;
  order: FlowStep[];
  thinkingStarts: Map<string, number>;
};

// Only these tool contracts execute code/query supplied as arguments. Product
// tool receipts store their implementation module in `code`; its text must
// never be mistaken for the model's invocation or tested by header heuristics.
const executesArgumentCode = (name: string): boolean =>
  ['local_python_repl', 'local_sql_engine'].includes(name);

function builder(event: WorkspaceEvent, runId: string): Builder {
  return {
    runId,
    sessionId: event.session_id,
    model: '',
    effort: '',
    background: false,
    state: 'running',
    prompt: '',
    steps: [],
    compactions: 0,
    thinking: new Map(),
    calls: new Map(),
    drafts: new Map(),
    messagePhases: new Map(),
    plans: new Map(),
    providerEvents: new Set(),
    providerPhases: new Map(),
    compactionIds: new Set(),
    order: [],
    thinkingStarts: new Map(),
  };
}

function note(id: string, label: string, detail: string, tone: 'info' | 'warn' = 'info'): FlowNote {
  return { kind: 'note', id, tone, label, detail };
}

function updateSummaryDisplay(
  part: FlowPart,
  data: Record<string, unknown>,
  previousRaw = part.raw,
) {
  // The server may supply a full public display snapshot while preserving the
  // provider's original delta in `raw`. Never append a fallback task status to
  // provider text, or reconstruct a translation from arbitrary partial strings.
  const retainedTranslation =
    part.displaySource === 'translated_provider_summary' && part.displayLocalized === true;
  if (retainedTranslation && data.display_source !== 'translated_provider_summary') {
    // A replay of the same source cannot downgrade a completed translation to
    // its earlier pending/original snapshot. Changed public text needs a new one.
    if (previousRaw.trim() === part.raw.trim()) return;
    part.displayText = undefined;
    part.displaySource = undefined;
    part.displayLanguage = undefined;
    part.displayLocalized = undefined;
    part.translationState = undefined;
  }
  if (data.display_mode === 'snapshot' && typeof data.display_text === 'string') {
    part.displayText = data.display_text;
    part.displaySource =
      data.display_source === 'task_status' ||
      data.display_source === 'provider_summary' ||
      data.display_source === 'translated_provider_summary'
        ? data.display_source
        : undefined;
    part.displayLanguage =
      data.display_language === 'en' || data.display_language === 'vi'
        ? data.display_language
        : undefined;
    part.displayLocalized = data.display_localized === true;
  }
  if (
    data.translation_state === 'pending' ||
    data.translation_state === 'translated' ||
    data.translation_state === 'failed' ||
    data.translation_state === 'skipped' ||
    data.translation_state === 'cancelled'
  ) {
    part.translationState = data.translation_state;
  }
  Object.assign(part, splitSummaryPart(part.displayText ?? part.raw));
}

function ensureThinkingBlock(
  run: Builder,
  itemId: string,
  simulation: boolean,
  time?: string,
): FlowThinking {
  let block = run.thinking.get(itemId);
  if (!block) {
    block = {
      kind: 'thinking',
      id: 'thinking-' + itemId,
      parts: [],
      simulation,
      done: false,
    };
    run.thinking.set(itemId, block);
    run.order.push(block);
  }
  block.simulation ||= simulation;
  const startedAt = time ? Date.parse(time) : NaN;
  if (Number.isFinite(startedAt) && !block.startedAt) block.startedAt = startedAt;
  return block;
}

function extractArgumentField(raw: string, field: string): unknown {
  if (!raw) return undefined;
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === 'object' && field in parsed) {
      return (parsed as Record<string, unknown>)[field];
    }
  } catch {
    const regex = new RegExp(
      `"${field}"\\s*:\\s*("(?:[^"\\\\]|\\\\.)*"|\\[[^\\]]*\\]|\\{[^\\}]*\\}|true|false|null|[0-9.]+)`,
    );
    const match = regex.exec(raw);
    if (match) {
      try {
        return JSON.parse(match[1]);
      } catch {
        /* ignore */
      }
    }
    const unclosedArray = new RegExp(`"${field}"\\s*:\\s*\\[([^\\]]*)`);
    const arrayMatch = unclosedArray.exec(raw);
    if (arrayMatch) {
      const items = [...arrayMatch[1].matchAll(/"((?:[^"\\\\]|\\\\.)*)"/g)].map((m) => {
        try {
          return JSON.parse(`"${m[1]}"`);
        } catch {
          return m[1];
        }
      });
      if (items.length) return items;
    }
    const unclosedString = new RegExp(`"${field}"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)`);
    const unclosedMatch = unclosedString.exec(raw);
    if (unclosedMatch) {
      try {
        return JSON.parse(`"${unclosedMatch[1]}"`);
      } catch {
        return unclosedMatch[1];
      }
    }
  }
  return undefined;
}

function formatPlan(value: unknown): string {
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (trimmed.includes('\n')) {
      const lines = trimmed
        .split(/\r?\n/)
        .map((s) => s.replace(/^\s*(?:[0-9]+[.)]|[-*•])\s*/, '').trim())
        .filter(Boolean);
      if (lines.length > 1) return lines.join(' → ');
    }
    return trimmed;
  }
  if (Array.isArray(value)) {
    return value
      .map((item) =>
        record(item)
          ? text(
              record(item)!.title ?? record(item)!.text ?? record(item)!.name ?? record(item)!.step,
            )
          : text(item),
      )
      .filter(Boolean)
      .join(' → ');
  }
  const rec = record(value);
  if (rec) {
    if (Array.isArray(rec.steps)) return formatPlan(rec.steps);
    if (Array.isArray(rec.items)) return formatPlan(rec.items);
    return text(rec.title ?? rec.text ?? rec.detail ?? rec.description);
  }
  return '';
}

function syncToolArguments(
  run: Builder,
  call: FlowCall,
  argumentsValue: unknown,
  commandText: string,
  eventId: string,
  directData?: Record<string, unknown>,
) {
  let argsRecord: Record<string, unknown> | undefined = record(argumentsValue);
  if (!argsRecord && typeof argumentsValue === 'string') {
    try {
      const parsed = JSON.parse(argumentsValue);
      if (record(parsed)) argsRecord = parsed as Record<string, unknown>;
    } catch {
      /* ignore */
    }
  }
  if (!argsRecord && commandText) {
    try {
      const parsed = JSON.parse(commandText);
      if (record(parsed)) argsRecord = parsed as Record<string, unknown>;
    } catch {
      /* ignore */
    }
  }

  // 1. Parse rationale
  let rationale =
    argsRecord && typeof argsRecord.rationale === 'string'
      ? argsRecord.rationale
      : typeof directData?.rationale === 'string'
        ? directData.rationale
        : undefined;
  if (!rationale && commandText) {
    const extracted = extractArgumentField(commandText, 'rationale');
    if (typeof extracted === 'string') rationale = extracted;
  }
  if (rationale && rationale.trim()) {
    call.rationale = rationale.trim();
  }

  // 2. Extract plan
  let planValue = argsRecord?.plan ?? directData?.plan;
  if (planValue === undefined && commandText) {
    planValue = extractArgumentField(commandText, 'plan');
  }
  if (planValue !== undefined) {
    const formatted = formatPlan(planValue);
    if (formatted) {
      const planKey = call.id || 'plan-' + eventId;
      let planNote = run.plans.get(planKey);
      if (planNote) {
        planNote.detail = formatted;
      } else {
        planNote = note('plan-' + planKey.replace(/^call-/, ''), 'plan', formatted);
        run.plans.set(planKey, planNote);
        const callIdx = run.order.indexOf(call);
        if (callIdx >= 0) {
          run.order.splice(callIdx, 0, planNote);
        } else {
          run.order.push(planNote);
        }
      }
    }
  }
}

/** Group the raw event history into one provider-ordered flow per run. */
export function projectAgentFlow(events: WorkspaceEvent[]): Map<string, FlowRun> {
  const runs = new Map<string, Builder>();
  const eventIds = new Set<string>();
  const reasoningAliases = new Map<string, { itemId: string; indices?: number[] }>();
  for (const event of events) {
    const d = event.data;
    if (
      event.type === 'reasoning_alias' &&
      event.run_id &&
      d.provider_api === 'agents' &&
      d.source === 'session_snapshot' &&
      d.reconciled === true &&
      text(d.session_id) &&
      text(d.turn_id) &&
      text(d.item_id) &&
      text(d.canonical_item_id)
    ) {
      const indices = d.canonical_summary_indices;
      if (
        indices !== undefined &&
        (!Array.isArray(indices) ||
          !indices.every((index) => Number.isSafeInteger(index) && index >= 0))
      )
        continue;
      reasoningAliases.set(JSON.stringify([event.run_id, d.session_id, d.turn_id, d.item_id]), {
        itemId: [d.session_id, d.turn_id, d.canonical_item_id].join(':'),
        indices,
      });
    }
  }
  let pendingPrompt = '';
  const open = (event: WorkspaceEvent): Builder | undefined => {
    const runId = event.run_id;
    if (!runId) return undefined;
    let run = runs.get(runId);
    if (!run) {
      run = builder(event, runId);
      run.prompt = pendingPrompt;
      runs.set(runId, run);
    }
    return run;
  };

  for (const event of events) {
    const eventKey = JSON.stringify([event.session_id, event.id]);
    if (eventIds.has(eventKey)) continue;
    eventIds.add(eventKey);
    const d = event.data || {};
    if (event.type === 'message' && d.role === 'user') {
      pendingPrompt = text(d.text ?? d.content);
      continue;
    }
    const run = open(event);
    if (!run) continue;
    // Run metadata is immutable. A later question or steering message must not
    // relabel work that was already recorded in another language.
    if (!run.language && (d.language === 'en' || d.language === 'vi')) run.language = d.language;
    const providerSessionId =
      text(d.provider_session_id) || (d.provider_api === 'agents' ? text(d.session_id) : '');
    const providerTurnId =
      text(d.provider_turn_id) || (d.provider_api === 'agents' ? text(d.turn_id) : '');
    const providerEventId = text(d.event_id);
    const aliasFor = (itemId: string) =>
      reasoningAliases.get(
        JSON.stringify([event.run_id, providerSessionId, providerTurnId, itemId]),
      );
    const originalItemId = (itemId: string) =>
      providerSessionId ? [providerSessionId, providerTurnId, itemId].join(':') : itemId;
    const scopedItemId = (itemId: string) => aliasFor(itemId)?.itemId || originalItemId(itemId);
    const summaryIdentity = (itemId: string, index: number) => {
      const alias = aliasFor(itemId);
      if (alias?.indices && alias.indices[index] === undefined) {
        return { itemId: originalItemId(itemId), index };
      }
      return { itemId: scopedItemId(itemId), index: alias?.indices?.[index] ?? index };
    };
    if (providerEventId) {
      const key = [providerSessionId, providerTurnId, event.type, providerEventId].join(':');
      if (run.providerEvents.has(key)) continue;
      run.providerEvents.add(key);
    }
    const id = String(event.id);
    switch (event.type) {
      case 'status': {
        const state = text(d.state);
        if (state) run.state = state;
        break;
      }
      case 'model_started': {
        run.model = text(d.model) || run.model;
        run.background = d.background === true || run.background;
        const reasoning = record(d.reasoning);
        if (reasoning) run.effort = text(reasoning.effort) || run.effort;
        break;
      }
      case 'reasoning_summary':
      case 'reasoning': {
        // One block per provider reasoning item; one part per summary/content index.
        const { itemId, index } = summaryIdentity(
          text(d.item_id) || text(d.response_id) || 'stream',
          count(d.summary_index ?? d.content_index),
        );
        const block = ensureThinkingBlock(run, itemId, d.simulation === true, event.time);
        if (event.time && !run.thinkingStarts.has(itemId)) {
          const t = new Date(event.time).getTime();
          if (Number.isFinite(t)) run.thinkingStarts.set(itemId, t);
        }
        if (count(d.duration_ms) > 0) block.durationMs = count(d.duration_ms);
        else if (count(d.elapsed_ms) > 0) block.durationMs = count(d.elapsed_ms);
        let part = block.parts.find((item) => item.index === index);
        if (!part) {
          part = { index, raw: '', title: '', body: '', done: false };
          block.parts.push(part);
          block.parts.sort((left, right) => left.index - right.index);
        }
        const raw = text(d.text ?? d.delta);
        // A done event carries the full part; deltas accumulate in order.
        const final = d.final === true || d.done === true;
        if ((part.done || block.done) && !final) break;
        const previousRaw = part.raw;
        if (!final || typeof d.text === 'string' || typeof d.delta === 'string') {
          part.raw = final ? raw : part.raw + raw;
        }
        updateSummaryDisplay(part, d, previousRaw);
        if (final) {
          part.done = true;
          if (event.time && run.thinkingStarts.has(itemId) && !block.durationMs) {
            const start = run.thinkingStarts.get(itemId)!;
            const end = new Date(event.time).getTime();
            if (Number.isFinite(end) && end >= start) block.durationMs = end - start;
          }
        }
        break;
      }
      case 'reasoning_part': {
        const { itemId, index } = summaryIdentity(
          text(d.item_id) || 'stream',
          count(d.summary_index),
        );
        const block = ensureThinkingBlock(run, itemId, d.simulation === true, event.time);
        let part = block.parts.find((item) => item.index === index);
        if (d.phase === 'added' && (block.done || part?.done)) break;
        if (!part) {
          part = { index, raw: '', title: '', body: '', done: false };
          block.parts.push(part);
          block.parts.sort((left, right) => left.index - right.index);
        }
        // Managed continuation streams replay existing parts with new event IDs.
        // A repeated part boundary restarts an unfinished snapshot; it is never
        // appended to the previous replay of the same provider part.
        if (part && d.phase === 'added') {
          part.raw = '';
          part.title = '';
          part.body = '';
          part.displayText = undefined;
          part.displaySource = undefined;
          part.displayLanguage = undefined;
          part.displayLocalized = undefined;
          part.translationState = undefined;
        }
        const previousRaw = part.raw;
        if (part && d.phase === 'done') {
          if (typeof d.text === 'string') {
            part.raw = d.text;
          }
          part.done = true;
        }
        updateSummaryDisplay(part, d, previousRaw);
        break;
      }
      case 'output_item': {
        const itemType = text(d.item_type);
        if (itemType === 'message' && text(d.item_id) && text(d.message_phase)) {
          const itemId = scopedItemId(text(d.item_id));
          run.messagePhases.set(itemId, text(d.message_phase));
          for (const [key, draft] of run.drafts) {
            if (key.startsWith(itemId + ':content:')) draft.messagePhase = text(d.message_phase);
          }
        }
        if (
          d.phase === 'added' &&
          [
            'function_call',
            'custom_tool_call',
            'mcp_call',
            'web_search_call',
            'file_search_call',
            'code_interpreter_call',
          ].includes(itemType)
        ) {
          if (!text(d.item_id)) break;
          const itemId = scopedItemId(text(d.item_id));
          const existing = run.calls.get(itemId) || run.calls.get(text(d.call_id));
          if (existing) {
            run.calls.set(itemId, existing);
            if (text(d.call_id)) run.calls.set(text(d.call_id), existing);
            break;
          }
          const call: FlowCall = {
            kind: 'call',
            id: 'call-' + itemId,
            name: text(d.name) || itemType,
            command: '',
            streaming: true,
            status: 'pending',
            outcome: '',
            output: '',
          };
          run.calls.set(itemId, call);
          if (text(d.call_id)) run.calls.set(text(d.call_id), call);
          run.order.push(call);
        }
        if (d.phase === 'done' && itemType !== 'reasoning') {
          const call =
            run.calls.get(scopedItemId(text(d.item_id))) || run.calls.get(text(d.call_id));
          if (call) {
            // Function completion confirms arguments, not the local execution.
            if (d.status === 'cancelled') {
              call.status = 'cancelled';
              call.streaming = false;
            } else if (['failed', 'incomplete'].includes(text(d.status))) {
              call.status = 'failed';
              call.streaming = false;
            } else if (!['function_call', 'custom_tool_call'].includes(itemType)) {
              call.status = d.status === 'completed' ? 'completed' : 'unconfirmed';
              call.streaming = false;
            }
          }
        }
        if (itemType === 'reasoning') {
          if (text(d.item_id)) {
            const itemId = scopedItemId(text(d.item_id));
            const block = ensureThinkingBlock(run, itemId, d.simulation === true, event.time);
            if (d.phase === 'done') {
              const alias = aliasFor(text(d.item_id));
              if (alias?.indices) {
                // A transient item can cover only some parts of one durable
                // item. Its completion cannot close later sibling parts.
                for (const part of block.parts) {
                  if (alias.indices.includes(part.index)) part.done = true;
                }
                break;
              }
              block.done = true;
              for (const part of block.parts) part.done = true;
              if (event.time && run.thinkingStarts.has(itemId) && !block.durationMs) {
                const start = run.thinkingStarts.get(itemId)!;
                const end = new Date(event.time).getTime();
                if (Number.isFinite(end) && end >= start) block.durationMs = end - start;
              }
            }
          }
        }
        break;
      }
      case 'tool_arguments_delta':
      case 'tool_arguments': {
        const itemId = text(d.item_id);
        const callId = text(d.call_id);
        const scopedId = scopedItemId(itemId || callId || id);
        let call =
          (itemId ? run.calls.get(scopedItemId(itemId)) : undefined) ||
          (callId ? run.calls.get(callId) : undefined);
        if (!call) {
          call = {
            kind: 'call',
            id: 'call-' + scopedId,
            name: text(d.name) || 'tool',
            command: '',
            streaming: true,
            status: 'pending',
            outcome: '',
            output: '',
          };
          if (itemId) run.calls.set(scopedItemId(itemId), call);
          if (callId) run.calls.set(callId, call);
          run.order.push(call);
        }
        const isDelta =
          event.type === 'tool_arguments_delta' ||
          d.phase === 'delta' ||
          (d.delta !== undefined && d.phase !== 'done');
        if (isDelta) {
          if (['completed', 'failed', 'cancelled', 'unconfirmed'].includes(call.status)) break;
          const delta = text(d.delta ?? d.arguments_delta ?? d.arguments);
          call.command += delta;
          call.streaming = true;
        }
        if (d.phase === 'done' || (event.type === 'tool_arguments' && d.arguments && !isDelta)) {
          if (d.arguments) {
            call.command = text(d.arguments) || call.command;
          }
          if (['completed', 'failed', 'cancelled', 'unconfirmed'].includes(call.status)) {
            call.streaming = false;
          } else {
            call.streaming = true;
          }
        }
        syncToolArguments(run, call, d.arguments, call.command, id, d);
        break;
      }
      case 'tool': {
        const callId = text(d.call_id);
        const toolId = text(d.tool_id) || text(d.execution_id) || id;
        let call = (callId && run.calls.get(callId)) || run.calls.get('tool:' + toolId);
        if (!call) {
          call = {
            kind: 'call',
            id: 'call-' + toolId,
            name: text(d.name),
            command: '',
            streaming: false,
            status: 'running',
            outcome: '',
            output: '',
          };
          run.order.push(call);
        }
        run.calls.set('tool:' + toolId, call);
        if (callId) run.calls.set(callId, call);
        call.name = text(d.name) || call.name;
        const status = text(d.status);
        call.status =
          status === 'completed'
            ? 'completed'
            : status === 'failed'
              ? 'failed'
              : status === 'cancelled'
                ? 'cancelled'
                : status === 'unconfirmed'
                  ? 'unconfirmed'
                  : 'running';
        if (
          call.status === 'completed' ||
          call.status === 'failed' ||
          call.status === 'cancelled'
        ) {
          call.streaming = false;
        }
        const command = text(d.code);
        if (executesArgumentCode(call.name)) {
          if (command) call.command = command;
        } else {
          const argumentsText =
            typeof d.arguments === 'string'
              ? d.arguments
              : record(d.arguments)
                ? stableJson(d.arguments)
                : '';
          if (argumentsText) call.command = argumentsText;
          // The app's running tool event carries invocation arguments in `code`.
          // Completed product receipts carry implementation source there instead.
          else if (!call.command && status === 'running' && command) call.command = command;
          if (command && status !== 'running' && command !== call.command)
            call.implementationSource = command;
        }
        syncToolArguments(run, call, d.arguments, call.command, id, d);
        if (d.result !== undefined && d.result !== null) {
          call.outcome = callOutcome(d.result);
          call.output = stableJson(d.result);
        }
        if (text(d.execution_id)) call.executionId = text(d.execution_id);
        if (text(d.code_sha256)) call.codeSha256 = text(d.code_sha256);
        if (count(d.duration_ms) > 0) call.durationMs = count(d.duration_ms);
        break;
      }
      case 'text_delta':
      case 'text_done': {
        const itemId = scopedItemId(text(d.item_id) || text(d.response_id) || 'stream');
        const partId = itemId + ':content:' + count(d.content_index);
        let draft = run.drafts.get(partId);
        if (!draft) {
          draft = {
            kind: 'draft',
            id: 'draft-' + partId,
            text: '',
            messagePhase: run.messagePhases.get(itemId),
          };
          run.drafts.set(partId, draft);
          run.order.push(draft);
        }
        if (text(d.message_phase)) draft.messagePhase = text(d.message_phase);
        if (draft.done && event.type === 'text_delta') break;
        draft.text = event.type === 'text_done' ? text(d.text) : draft.text + text(d.delta);
        if (event.type === 'text_done') draft.done = true;
        break;
      }
      case 'message': {
        if (d.role === 'user') break;
        run.order.push({
          kind: 'answer',
          id: 'answer-' + id,
          simulation: d.simulation === true,
        });
        break;
      }
      case 'usage': {
        // Older histories may identify the model only on a usage event.
        if (!run.model) run.model = text(d.model);
        break;
      }
      case 'compaction': {
        const compactionId = text(d.id) || text(d.item_id) || id;
        if (!run.compactionIds.has(compactionId)) {
          run.compactionIds.add(compactionId);
          run.compactions += 1;
        }
        break;
      }
      case 'todo': {
        const items = Array.isArray(d.items) ? d.items : [];
        run.order.push(
          note(
            'todo-' + id,
            'plan',
            items
              .map((item) =>
                record(item) ? text(record(item)!.title ?? record(item)!.text) : text(item),
              )
              .filter(Boolean)
              .join(' → '),
          ),
        );
        break;
      }
      case 'memory': {
        run.order.push(note('memory-' + id, text(d.action) || 'memory', text(d.key)));
        break;
      }
      case 'qa': {
        const issues = Array.isArray(d.issues) ? d.issues.map((item) => text(item)) : [];
        const detail = issues.length ? issues.join(' · ') : text(d.status);
        const tone = d.status === 'verified' ? 'info' : 'warn';
        const previous = run.order.at(-1);
        if (
          previous &&
          previous.kind === 'note' &&
          previous.label === 'review' &&
          previous.detail === detail &&
          previous.tone === tone
        ) {
          break;
        }
        run.order.push(note('qa-' + id, 'review', detail, tone));
        break;
      }
      case 'artifact': {
        const name = text(d.name) || text(d.path);
        if (name) run.order.push(note('artifact-' + id, 'file', name));
        break;
      }
      case 'steering_received': {
        run.order.push(
          note('steer-received-' + id, 'steering_received', truncate(text(d.message), 160)),
        );
        break;
      }
      case 'steering_submitted': {
        run.order.push(
          note('steer-submitted-' + id, 'steering_submitted', truncate(text(d.message), 160)),
        );
        break;
      }
      case 'steering_failed': {
        run.order.push(note('steer-failed-' + id, 'steering_failed', '', 'warn'));
        break;
      }
      case 'steering_applied': {
        run.order.push(note('steer-' + id, 'steering', truncate(text(d.message), 160)));
        break;
      }
      case 'transport_recovery': {
        run.order.push(
          note('recover-' + id, 'reconnected', text(d.mode) || text(d.response_id), 'warn'),
        );
        break;
      }
      case 'model_preflight_retry': {
        run.order.push(note('preflight-' + id, 'retry', text(d.phase), 'warn'));
        break;
      }
      case 'provider_cancel': {
        if (d.attempted === false) break;
        const confirmed =
          d.terminal_confirmed === true &&
          ['idle', 'completed', 'cancelled', 'failed'].includes(text(d.status));
        run.order.push(
          note(
            'cancel-' + id,
            confirmed ? 'stopped' : 'cancel_requested',
            confirmed ? (d.status === 'failed' ? 'failed' : '') : 'unconfirmed',
            'warn',
          ),
        );
        break;
      }
      case 'provider_phase': {
        // Provider idle/completed describes its turn, not application delivery QA.
        // The local status event remains authoritative for the visible run state.
        const phase = text(d.phase) || text(d.provider_status);
        const key = JSON.stringify([providerSessionId || 'provider', providerTurnId || 'session']);
        const previous = run.providerPhases.get(key);
        if (previous) previous.detail = phase;
        else {
          const activity = note('phase-' + id, 'provider', phase);
          run.providerPhases.set(key, activity);
          run.order.push(activity);
        }
        break;
      }
      case 'provider_error': {
        run.order.push({
          kind: 'failure',
          id: 'provider-' + id,
          message: text(d.message),
          code: text(d.code),
        });
        break;
      }
      case 'error': {
        run.order.push({
          kind: 'failure',
          id: 'error-' + id,
          message: text(d.message ?? d.text),
          code: text(d.code),
        });
        break;
      }
      default:
        break;
    }
  }

  const result = new Map<string, FlowRun>();
  for (const [runId, run] of runs) {
    const terminal = stoppedFlowStates.has(run.state) || completedFlowStates.has(run.state);
    if (terminal) {
      for (const step of run.order) {
        // A terminal run cannot keep animating. Missing receipts never become success.
        if (step.kind === 'thinking' && !step.done) step.stopped = true;
        if (step.kind === 'call' && (step.status === 'pending' || step.status === 'running')) {
          step.status = 'unconfirmed';
          step.streaming = false;
        }
      }
    }
    const hasAnswer = run.order.some((step) => step.kind === 'answer');
    result.set(runId, {
      runId: run.runId,
      sessionId: run.sessionId,
      model: run.model,
      effort: run.effort,
      background: run.background,
      state: run.state,
      prompt: run.prompt,
      language: run.language,
      compactions: run.compactions,
      // The accepted report is rendered once by EventView with delivery checks.
      steps: run.order.filter(
        (step) =>
          step.kind !== 'draft' ||
          (step.text &&
            step.messagePhase !== 'final_answer' &&
            !/^\s*(?:[[{]|```(?:json)?\s*[[{])/i.test(step.text) &&
            (!hasAnswer || step.messagePhase === 'commentary')),
      ),
    });
  }
  return result;
}
