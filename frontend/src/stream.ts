import type { WorkspaceEvent } from './types';

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
      }
      result[position] = { ...event, id: prior.id, data };
    } else {
      position = result.length;
      if (key) positions.set(key, position);
      result.push(event);
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
