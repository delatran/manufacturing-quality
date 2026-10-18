import type { WorkspaceEvent } from './types';

type EventBatchScheduler = (publish: () => void) => () => void;
const scheduleEventBatch: EventBatchScheduler = (publish) => {
  // A timer also publishes when the Agent panel is hidden. This only coalesces
  // rendering work; every persisted event and its order remain in the history.
  const timer = setTimeout(publish, 16);
  return () => clearTimeout(timer);
};
const record = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value);
function persistedEventId(value: unknown): number | undefined {
  const id =
    typeof value === 'number'
      ? value
      : typeof value === 'string' && /^[1-9]\d*$/.test(value)
        ? Number(value)
        : NaN;
  return Number.isSafeInteger(id) && id > 0 ? id : undefined;
}

/** Receive one session's durable stream without rescanning its history on replay. */
export function createSessionEventStream(
  sessionId: string,
  history: readonly WorkspaceEvent[],
  onBatch: (events: WorkspaceEvent[]) => void,
  schedule: EventBatchScheduler = scheduleEventBatch,
) {
  const ids = new Set<number>();
  let cursor: number | undefined;
  for (const event of history) {
    const id = persistedEventId(event.id);
    if (event.session_id !== sessionId || id === undefined) continue;
    ids.add(id);
    cursor = Math.max(cursor ?? 0, id);
  }
  let pending: WorkspaceEvent[] = [];
  let cancelPublication: (() => void) | undefined;
  let disposed = false;
  const publish = () => {
    cancelPublication = undefined;
    if (disposed || !pending.length) return;
    const batch = pending;
    pending = [];
    onBatch(batch);
  };
  return {
    get cursor() {
      return cursor;
    },
    receive(value: unknown): number {
      if (disposed) return 0;
      const incoming = record(value) && 'events' in value ? value.events : [value];
      if (!Array.isArray(incoming)) throw new Error('An unreadable event was received');
      // Validate the whole packet before advancing the reconnect cursor. A bad
      // event must not hide the rest of a packet from a subsequent replay.
      const packet = incoming.map((event: unknown) => {
        if (!record(event)) throw new Error('An unreadable event was received');
        const id = persistedEventId(event.id);
        if (
          id === undefined ||
          event.session_id !== sessionId ||
          typeof event.type !== 'string' ||
          !event.type ||
          typeof event.time !== 'string' ||
          !record(event.data) ||
          (event.run_id != null && typeof event.run_id !== 'string')
        )
          throw new Error('An unreadable event was received');
        return { id, event: event as WorkspaceEvent };
      });
      let accepted = 0;
      for (const { id, event } of packet) {
        if (ids.has(id)) continue;
        ids.add(id);
        cursor = Math.max(cursor ?? 0, id);
        pending.push(event);
        accepted += 1;
      }
      if (accepted && !cancelPublication) cancelPublication = schedule(publish);
      return accepted;
    },
    dispose() {
      disposed = true;
      cancelPublication?.();
      cancelPublication = undefined;
      pending = [];
    },
  };
}

// The raw event history stays intact. This projection only joins lifecycle updates.
export function projectEvents(events: WorkspaceEvent[]): WorkspaceEvent[] {
  const eventIds = new Set<string>();
  const providerEventIds = new Set<string>();
  // Reconnects can replay already persisted events. Keep one display copy while
  // preserving canonical history and distinct provider sessions and turns.
  const unique = events.filter((event) => {
    const eventId = JSON.stringify([event.session_id, event.id]);
    if (eventIds.has(eventId)) return false;
    eventIds.add(eventId);
    const d = event.data;
    if (typeof d.event_id !== 'string' || !d.event_id) return true;
    const providerId = JSON.stringify([
      event.session_id,
      event.run_id,
      d.provider_api,
      d.provider_session_id ?? d.session_id,
      d.provider_turn_id ?? d.turn_id,
      event.type,
      d.event_id,
    ]);
    if (providerEventIds.has(providerId)) return false;
    providerEventIds.add(providerId);
    return true;
  });
  const result: WorkspaceEvent[] = [];
  const positions = new Map<string, number>();
  const lastStatuses = new Map<string, string | number>();
  unique.forEach((event) => {
    if (event.type === 'status') lastStatuses.set(event.run_id || 'session', event.id);
  });
  const latestStatus = [...unique].reverse().find((event) => event.type === 'status');
  let previous: WorkspaceEvent | undefined;
  let reasoningPosition: number | undefined;
  unique.forEach((event) => {
    const d = event.data;
    const toolId = d.tool_id || d.execution_id;
    const artifactId = d.path || d.sha256 || d.url;
    const run = event.run_id || 'session';
    const summaryScope = JSON.stringify([
      event.session_id,
      run,
      d.provider_session_id ?? d.session_id,
      d.provider_turn_id ?? d.turn_id,
    ]);
    if (event.type === 'status' && lastStatuses.get(run) !== event.id) return;
    if (event.type === 'status' && !event.run_id && d.state === 'queued' && latestStatus !== event)
      return;
    let key: string | undefined;
    if (event.type === 'todo' || event.type === 'status') key = `${event.type}:${run}`;
    else if (event.type === 'tool' && toolId) key = `tool:${run}:${String(toolId)}`;
    else if (event.type === 'artifact' && artifactId) key = `artifact:${run}:${String(artifactId)}`;
    else if (event.type === 'reasoning' && d.simulation) key = `reasoning:${run}:simulation`;
    else if (event.type === 'reasoning' && d.item_id !== undefined)
      key = `reasoning:${summaryScope}:${String(d.item_id)}:${String(d.summary_index ?? 0)}`;
    else if (event.type === 'reasoning' && (d.response_id || d.summary_id))
      key = `reasoning:${summaryScope}:${String(d.response_id || d.summary_id)}`;
    else if (event.type === 'tool_arguments_delta' || event.type === 'tool_arguments') {
      const toolCallKey = d.item_id
        ? `${summaryScope}:${String(d.item_id)}`
        : d.call_id
          ? `${run}:${String(d.call_id)}`
          : toolId
            ? `${run}:${String(toolId)}`
            : undefined;
      if (toolCallKey) key = `tool_arguments:${toolCallKey}`;
    }
    let mapped = event;
    if (event.type === 'tool_arguments_delta') {
      mapped = {
        ...event,
        type: 'tool_arguments',
        data: {
          ...d,
          phase: d.phase ?? 'delta',
          delta: d.delta ?? d.arguments ?? '',
        },
      };
    }
    let position = key ? positions.get(key) : undefined;
    if (
      event.type === 'reasoning' &&
      !key &&
      previous?.type === 'reasoning' &&
      previous.session_id === event.session_id &&
      previous.run_id === event.run_id &&
      (previous.data.provider_session_id ?? previous.data.session_id) ===
        (d.provider_session_id ?? d.session_id) &&
      (previous.data.provider_turn_id ?? previous.data.turn_id) ===
        (d.provider_turn_id ?? d.turn_id) &&
      previous.data.simulation === d.simulation
    )
      position = reasoningPosition;
    if (position !== undefined) {
      const prior = result[position];
      const data = { ...prior.data, ...d };
      if (event.type === 'reasoning') {
        const before = String(prior.data.text || '');
        const next = String(d.text || '');
        data.text = d.simulation
          ? before.includes(next)
            ? before
            : `${before}\n\n${next}`
          : d.final === true
            ? next
            : before + next;
      } else if (event.type === 'tool_arguments_delta' || event.type === 'tool_arguments') {
        const before = String(prior.data.delta || '');
        const next = String(d.delta ?? d.arguments ?? '');
        const final =
          d.phase === 'done' ||
          (d.arguments !== undefined &&
            d.phase !== 'delta' &&
            event.type !== 'tool_arguments_delta');
        if (prior.data.phase === 'done' && !final) return;
        data.delta = final ? String(d.arguments ?? d.delta ?? before) : before + next;
        if (final) data.phase = 'done';
        if (d.arguments !== undefined || prior.data.arguments !== undefined) {
          data.arguments = d.arguments !== undefined ? d.arguments : prior.data.arguments;
        }
      }
      result[position] = { ...mapped, id: prior.id, data };
    } else {
      position = result.length;
      if (key) positions.set(key, position);
      result.push(mapped);
    }
    reasoningPosition = event.type === 'reasoning' ? position : undefined;
    previous = event;
  });

  const embeddedImages = new Map<string, Set<string>>();
  const imageIdentity = (url: unknown) => {
    try {
      return new URL(String(url), 'http://workspace.local').href;
    } catch {
      return String(url);
    }
  };
  result.forEach((event) => {
    if (event.type !== 'message' || event.data.role === 'user') return;
    const run = event.run_id || 'session';
    const urls = embeddedImages.get(run) || new Set<string>();
    for (const match of String(event.data.text || '').matchAll(
      /!\[[^\]]*\]\(<?([^\s)>]+)>?(?:\s+[^)]*)?\)/g,
    ))
      urls.add(imageIdentity(match[1]));
    embeddedImages.set(run, urls);
  });

  // Charts embedded in the answer keep their file evidence in compact exports.
  const grouped: WorkspaceEvent[] = [];
  const groups = new Map<string, number>();
  result.forEach((event) => {
    if (
      event.type !== 'artifact' ||
      (String(event.data.mime || '').startsWith('image/') &&
        !embeddedImages.get(event.run_id || 'session')?.has(imageIdentity(event.data.url)))
    ) {
      grouped.push(event);
      return;
    }
    const run = event.run_id || 'session';
    const position = groups.get(run);
    if (position !== undefined) (grouped[position].data.items as WorkspaceEvent[]).push(event);
    else {
      groups.set(run, grouped.length);
      grouped.push({
        ...event,
        type: 'artifact_group',
        data: { items: [event] },
      });
    }
  });
  return grouped;
}
