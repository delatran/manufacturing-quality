import { artifactUrl } from './api';
import { chartArtifact } from './EventView';
import { applicationUrl } from './app-url';
import { projectEvents } from './stream';
import type { WorkspaceEvent } from './types';

export const conversationRun = (event: Pick<WorkspaceEvent, 'session_id' | 'run_id'>) =>
  JSON.stringify([event.session_id, event.run_id ?? null]);
const hasField = (value: Record<string, unknown>, key: string) =>
  Object.prototype.hasOwnProperty.call(value, key);
const artifactRecord = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value);

function chartIdentity(data: Record<string, unknown>): string | undefined {
  const { path, sha256 } = data;
  if (
    typeof path !== 'string' ||
    !path ||
    path.includes('\\') ||
    path.includes(':') ||
    path.split('/').some((part) => !part || part === '.' || part === '..') ||
    typeof sha256 !== 'string' ||
    !/^[a-f0-9]{64}$/i.test(sha256)
  )
    return;
  return JSON.stringify([path, sha256]);
}

function selectedChartUrls(report: WorkspaceEvent): Map<string, string> {
  const selected = new Map<string, string>();
  const delivery = report.data.delivery;
  if (
    !report.run_id ||
    !artifactRecord(delivery) ||
    delivery.version !== 1 ||
    !Array.isArray(delivery.artifacts) ||
    !Array.isArray(delivery.source_execution_ids) ||
    delivery.source_execution_ids.some((id) => typeof id !== 'string' || !id.trim()) ||
    !hasField(delivery, 'scope') ||
    (delivery.scope !== null && !artifactRecord(delivery.scope))
  )
    return selected;
  for (const item of delivery.artifacts) {
    if (
      !artifactRecord(item) ||
      typeof item.name !== 'string' ||
      !item.name.trim() ||
      typeof item.mime_type !== 'string' ||
      !item.mime_type.trim() ||
      typeof item.size_bytes !== 'number' ||
      !Number.isSafeInteger(item.size_bytes) ||
      item.size_bytes < 0 ||
      (hasField(item, 'kind') && typeof item.kind !== 'string')
    )
      return new Map();
    const identity = chartIdentity(item);
    const url = artifactUrl(item.url);
    if (!identity || !url) return new Map();
    try {
      const address = new URL(url);
      const expected = applicationUrl(
        `/api/sessions/${report.session_id}/runs/${report.run_id}/artifacts/${item.path}`,
      );
      if (decodeURIComponent(address.pathname) !== expected || address.search || address.hash)
        return new Map();
      if (/\.(?:png|jpe?g|webp|svg|gif)$/i.test(item.name)) selected.set(identity, url);
    } catch {
      return new Map();
    }
  }
  return selected;
}

function projectChartEvents(events: WorkspaceEvent[]): WorkspaceEvent[] {
  const reports = new Set<string>();
  const controlled = new Set<string>();
  const selections = new Map<string, Map<string, string>>();
  for (const event of events) {
    const run = conversationRun(event);
    if (
      (event.type === 'run_started' || event.type === 'status') &&
      hasField(event.data, 'artifact_delivery_version')
    )
      controlled.add(run);
    if (event.type === 'artifact' && hasField(event.data, 'delivery_candidate'))
      controlled.add(run);
    if (event.type === 'artifact_group' && Array.isArray(event.data.items)) {
      for (const item of event.data.items as WorkspaceEvent[]) {
        if (hasField(item.data, 'delivery_candidate')) controlled.add(conversationRun(item));
      }
    }
    if (event.type === 'message' && event.data.role === 'assistant') {
      reports.add(run);
      if (hasField(event.data, 'delivery')) {
        controlled.add(run);
        selections.set(run, selectedChartUrls(event));
      } else selections.delete(run);
    }
  }
  // Deduplicate artifact versions before selecting them, so an older matching hash
  // cannot reappear after a newer event records different bytes at the same path.
  const projected = projectEvents(events);
  const allowed = new Set<WorkspaceEvent>();
  const urls = new Map<string, Map<string, string>>();
  for (const event of projected) {
    const candidates =
      event.type === 'artifact_group'
        ? ((Array.isArray(event.data.items) ? event.data.items : []) as WorkspaceEvent[])
        : event.type === 'artifact'
          ? [event]
          : [];
    for (const item of candidates) {
      const run = conversationRun(item);
      if (run !== conversationRun(event) || !reports.has(run)) continue;
      if (!chartArtifact(item)) continue;
      // Legacy reports without either marker retain their earlier charts.
      // A present empty, invalid or unsupported packet never falls back to inventory.
      if (controlled.has(run)) {
        const identity = chartIdentity(item.data);
        const url = artifactUrl(item.data.url);
        if (!identity || !url || selections.get(run)?.get(identity) !== url) continue;
        const values = urls.get(run) || new Map<string, string>();
        values.set(identity, url);
        urls.set(run, values);
      }
      allowed.add(item);
    }
  }
  return projected.flatMap((event) => {
    if (event.type === 'artifact') return allowed.has(event) ? [event] : [];
    if (event.type === 'artifact_group') {
      const items = (
        (Array.isArray(event.data.items) ? event.data.items : []) as WorkspaceEvent[]
      ).filter((item) => allowed.has(item));
      return items.length ? [{ ...event, data: { ...event.data, items } }] : [];
    }
    const run = conversationRun(event);
    if (event.type === 'message' && event.data.role === 'assistant' && controlled.has(run)) {
      // This field belongs only to the display copy, never the stored report.
      // An older report cannot display new bytes registered at its former URL.
      const ownSelection = selectedChartUrls(event);
      const currentSelection = urls.get(run);
      const chartUrls = [...ownSelection]
        .filter(([identity, url]) => currentSelection?.get(identity) === url)
        .map(([, url]) => url);
      return [{ ...event, data: { ...event.data, chart_urls: chartUrls } }];
    }
    return [event];
  });
}

// This is a display projection. Source events, reports and receipts are never changed.
export function projectConversation(events: WorkspaceEvent[]): WorkspaceEvent[] {
  const reasoningTypes = new Set([
    'reasoning',
    'reasoning_summary',
    'reasoning_part',
    'reasoning_alias',
  ]);
  const conversationEvents: WorkspaceEvent[] = [];
  const firstIds = new Map<string, Map<WorkspaceEvent['id'], boolean>>();
  let crossTypeDuplicate = false;
  for (const event of events) {
    const reasoning = reasoningTypes.has(event.type);
    let ids = firstIds.get(event.session_id);
    if (!ids) {
      ids = new Map();
      firstIds.set(event.session_id, ids);
    }
    const firstReasoning = ids.get(event.id);
    if (firstReasoning === undefined) ids.set(event.id, reasoning);
    else if (firstReasoning !== reasoning) crossTypeDuplicate = true;
    if (!reasoning) conversationEvents.push(event);
  }
  // Reasoning belongs to projectAgentFlow. Rebuilding its deltas here only to
  // discard them later repeats source parsing and allocation on every batch.
  // A malformed cross-type ID collision keeps the existing full projection's
  // first-event and chart-metadata behavior, instead of inventing a new winner.
  const projected = projectChartEvents(crossTypeDuplicate ? events : conversationEvents);
  const result: WorkspaceEvent[] = [];
  const activity = new Map<string, WorkspaceEvent>();
  const charts = new Map<string, WorkspaceEvent[]>();
  const lastReports = new Map<string, string | number>();
  const embeddedImages = new Map<string, Set<string>>();
  projected.forEach((event) => {
    if (event.type !== 'message' || event.data.role !== 'assistant') return;
    const run = conversationRun(event);
    lastReports.set(run, event.id);
    const images = embeddedImages.get(run) || new Set<string>();
    const allowedUrls = event.data.chart_urls;
    for (const match of String(event.data.text ?? event.data.content ?? '').matchAll(
      /!\[[^\]]*\]\(<?([^\s)>]+)>?(?:\s+[^)]*)?\)/g,
    )) {
      const url = artifactUrl(match[1]);
      if (
        url &&
        (!hasField(event.data, 'chart_urls') ||
          (Array.isArray(allowedUrls) && allowedUrls.includes(url)))
      )
        images.add(url);
    }
    embeddedImages.set(run, images);
  });
  projected.forEach((event) => {
    const run = conversationRun(event);
    if (event.type === 'artifact_group' || event.type === 'artifact') {
      const items =
        event.type === 'artifact_group'
          ? ((Array.isArray(event.data.items) ? event.data.items : []) as WorkspaceEvent[])
          : [event];
      for (const item of items) {
        const itemRun = conversationRun(item);
        const url = artifactUrl(item.data.url);
        if (chartArtifact(item) && (!url || !embeddedImages.get(itemRun)?.has(url)))
          charts.set(itemRun, [...(charts.get(itemRun) || []), item]);
      }
      return;
    }
    if (event.type === 'tool' || (event.type === 'status' && event.run_id)) {
      if (
        event.type === 'tool' &&
        /^(?:memory_|todo_|knowledge_)/.test(String(event.data.name || ''))
      )
        return;
      let group = activity.get(run);
      if (!group) {
        group = {
          ...event,
          id: 'activity-' + run,
          type: 'activity',
          data: { state: 'running', items: [] },
        };
        activity.set(run, group);
        result.push(group);
      }
      if (event.type === 'status') group.data.state = event.data.state;
      else (group.data.items as WorkspaceEvent[]).push(event);
      return;
    }
    if (
      event.type === 'message' ||
      event.type === 'error' ||
      (event.type === 'status' && event.data.state === 'queued')
    )
      result.push(event);
  });
  // Place chart previews after their report, without duplicating inline charts.
  const conversation: WorkspaceEvent[] = [];
  const appendCharts = (run: string) => {
    conversation.push(...(charts.get(run) || []));
    charts.delete(run);
  };
  result.forEach((event) => {
    conversation.push(event);
    if (event.type !== 'message' || event.data.role === 'user') return;
    const run = conversationRun(event);
    if (lastReports.get(run) !== event.id) return;
    appendCharts(run);
  });
  charts.forEach((_, run) => appendCharts(run));
  return conversation;
}
