import { projectAgentFlow } from './agent-flow';
import type { FlowRun } from './agent-flow';
import { eligibleAnswerFollowup } from './answer-followups';
import { questionForAnswer } from './answer-reply';
import { conversationRunLanguages } from './conversation-language';
import { conversationRun, projectConversation } from './conversation-projection';
import type { Locale } from './i18n';
import type { Session, WorkspaceEvent } from './types';

const eventKey = (event: WorkspaceEvent) => JSON.stringify([event.session_id, event.id]);
const providerKey = (event: WorkspaceEvent): string | undefined => {
  const d = event.data;
  if (typeof d.event_id !== 'string' || !d.event_id) return;
  return JSON.stringify([
    event.session_id,
    event.run_id,
    d.provider_api,
    d.provider_session_id ?? d.session_id,
    d.provider_turn_id ?? d.turn_id,
    event.type,
    d.event_id,
  ]);
};
const isLanguage = (value: unknown): value is Locale => value === 'en' || value === 'vi';

export type ConversationEntry = {
  event: WorkspaceEvent;
  type: string;
  role: unknown;
  language?: Locale;
  flow?: FlowRun;
  context?: WorkspaceEvent;
  question?: WorkspaceEvent;
  eligible: boolean;
  position: number;
  offset: number;
  queued: boolean;
};
type RunProjection = {
  events: WorkspaceEvent[];
  flowStart?: number;
  flowPrompt?: WorkspaceEvent;
  displayStatus?: WorkspaceEvent;
  question?: WorkspaceEvent;
  flow?: FlowRun;
  entries: ConversationEntry[];
};

export type ConversationState = {
  events: WorkspaceEvent[];
  entries: ConversationEntry[];
  visibleEvents: WorkspaceEvent[];
  flowRuns: Map<string, FlowRun>;
  runContexts: Map<string, WorkspaceEvent>;
  runLanguages: Map<string, Locale>;
  actionIntents: Map<string, string>;
  lastStatus?: WorkspaceEvent;
  lastRunStatus?: WorkspaceEvent;
  lastQuestion?: WorkspaceEvent;
  hasConversation: boolean;
  providerStopped: boolean;
  savedRuns: Session['runs'];
  groups: Map<string, RunProjection>;
  positions: Map<string, number>;
  providerIds: Set<string>;
  pendingPrompt?: WorkspaceEvent;
  displayStatus?: WorkspaceEvent;
  sessionId?: string;
  fullProjection: boolean;
};

export function createConversationState(
  events: WorkspaceEvent[] = [],
  savedRuns: Session['runs'] = [],
): ConversationState {
  return appendConversationState(
    {
      events: [],
      entries: [],
      visibleEvents: [],
      flowRuns: new Map(),
      runContexts: new Map(),
      runLanguages: conversationRunLanguages([], savedRuns),
      actionIntents: new Map(),
      hasConversation: false,
      providerStopped: false,
      savedRuns,
      groups: new Map(),
      positions: new Map(),
      providerIds: new Set(),
      fullProjection: false,
    },
    events,
  );
}

function entryFor(
  event: WorkspaceEvent,
  state: ConversationState,
  ownEvents: WorkspaceEvent[],
  flow: FlowRun | undefined,
  question: WorkspaceEvent | undefined,
  position: number,
  offset: number,
): ConversationEntry {
  const type = event.type;
  const role = event.data.role;
  const report = type === 'message' && role === 'assistant';
  const context = event.run_id ? state.runContexts.get(event.run_id) : undefined;
  return {
    event,
    type,
    role,
    position,
    offset,
    language:
      (event.run_id ? state.runLanguages.get(event.run_id) : undefined) ??
      (isLanguage(event.data.language) ? event.data.language : undefined),
    flow: type === 'activity' ? flow : undefined,
    context: type === 'message' && role !== 'user' ? context : undefined,
    question: report ? question : undefined,
    eligible:
      report && Boolean(context) && eligibleAnswerFollowup(event, ownEvents, state.savedRuns),
    queued: type === 'status' && !event.run_id && event.data.state === 'queued',
  };
}

function sameFields<T extends object>(previous: T, current: T): boolean {
  const keys = Object.keys(current) as (keyof T)[];
  return (
    Object.keys(previous).length === keys.length &&
    keys.every((key) => Object.is(previous[key], current[key]))
  );
}

function reuseFlowRun(current: FlowRun | undefined, previous: FlowRun | undefined) {
  if (!current || !previous) return current;
  const oldSteps = new Map(previous.steps.map((step) => [step.id, step]));
  const steps = current.steps.map((step) => {
    const prior = oldSteps.get(step.id);
    if (!prior || prior.kind !== step.kind) return step;
    if (step.kind === 'thinking' && prior.kind === 'thinking') {
      const oldParts = new Map(prior.parts.map((part) => [part.index, part]));
      const parts = step.parts.map((part) => {
        const oldPart = oldParts.get(part.index);
        return oldPart && sameFields(oldPart, part) ? oldPart : part;
      });
      const shared = {
        ...step,
        parts:
          parts.length === prior.parts.length &&
          parts.every((part, index) => part === prior.parts[index])
            ? prior.parts
            : parts,
      };
      return sameFields(prior, shared) ? prior : shared;
    }
    return sameFields(prior, step) ? prior : step;
  });
  const shared = {
    ...current,
    steps:
      steps.length === previous.steps.length &&
      steps.every((step, index) => step === previous.steps[index])
        ? previous.steps
        : steps,
  };
  return sameFields(previous, shared) ? previous : shared;
}

function projectRun(
  group: RunProjection,
  previous: RunProjection | undefined,
  state: ConversationState,
) {
  const events = group.events;
  const start = group.flowStart;
  const flowEvents =
    start === undefined
      ? []
      : group.flowPrompt
        ? [...events.slice(0, start), group.flowPrompt, ...events.slice(start)]
        : events;
  // Source replay still resolves late aliases and amendments. Share only equal
  // display values so unrelated deltas do not rerender every completed step.
  group.flow = reuseFlowRun(projectAgentFlow(flowEvents).values().next().value, previous?.flow);
  const question = questionForAnswer(events[0], events);
  const oldQuestion = previous?.question;
  group.question =
    question &&
    oldQuestion &&
    question.id === oldQuestion.id &&
    question.time === oldQuestion.time &&
    question.session_id === oldQuestion.session_id &&
    question.run_id === oldQuestion.run_id &&
    question.data.text === oldQuestion.data.text
      ? oldQuestion
      : question;
  const projected = projectConversation(events);
  const reports = projected.filter(
    (event) => event.type === 'message' && event.data.role === 'assistant',
  );
  const lastReport = reports.at(-1);
  const reportPosition = lastReport ? state.positions.get(eventKey(lastReport)) : undefined;
  const previousEntries = new Map(previous?.entries.map((entry) => [entry.event.id, entry]));
  group.entries = projected.map((event, offset) => {
    let position = state.positions.get(eventKey(event)) ?? Infinity;
    if (event.type === 'activity') {
      position = group.displayStatus
        ? (state.positions.get(eventKey(group.displayStatus)) ?? Infinity)
        : Infinity;
      for (const item of event.data.items as WorkspaceEvent[])
        position = Math.min(position, state.positions.get(eventKey(item)) ?? Infinity);
    } else if (event.type === 'artifact' || event.type === 'artifact_group') {
      // A partial or unusual legacy history can have a chart without a surviving
      // report. Preserve the original global tail ordering in that case.
      if (reportPosition === undefined) state.fullProjection = true;
      else position = reportPosition;
    }
    const entry = entryFor(event, state, events, group.flow, group.question, position, offset);
    const prior = previousEntries.get(event.id);
    return prior &&
      prior.event === entry.event &&
      prior.flow === entry.flow &&
      prior.context === entry.context &&
      prior.question === entry.question &&
      prior.language === entry.language &&
      prior.eligible === entry.eligible &&
      prior.position === entry.position &&
      prior.offset === entry.offset
      ? prior
      : entry;
  });
}

/**
 * Append to an immutable snapshot. Only touched runs are reprojected; raw history
 * remains complete. This is safe in a React state updater, including a repeated
 * or abandoned render, because no published array, map or run is mutated.
 */
export function appendConversationState(
  previous: ConversationState,
  batch: WorkspaceEvent[],
): ConversationState {
  if (!batch.length) return previous;
  const state: ConversationState = {
    ...previous,
    events: [...previous.events, ...batch],
    groups: new Map(previous.groups),
    positions: new Map(previous.positions),
    providerIds: new Set(previous.providerIds),
    runContexts: new Map(previous.runContexts),
    runLanguages: new Map(previous.runLanguages),
    actionIntents: new Map(previous.actionIntents),
  };
  const touched = new Set<string>();
  batch.forEach((event, index) => {
    const run = conversationRun(event);
    if (!touched.has(run)) {
      const prior = previous.groups.get(run);
      state.groups.set(
        run,
        prior ? { ...prior, events: [...prior.events] } : { events: [], entries: [] },
      );
      touched.add(run);
    }
    const group = state.groups.get(run)!;
    group.events.push(event);
    const key = eventKey(event);
    const duplicate = state.positions.has(key);
    // These legacy shapes have global projection semantics. Keep the canonical
    // projector for them instead of guessing at a partition or dropping events.
    if (
      duplicate ||
      (state.sessionId !== undefined && state.sessionId !== event.session_id) ||
      event.run_id === '' ||
      event.run_id === 'session' ||
      event.run_id?.includes(':')
    )
      state.fullProjection = true;
    state.sessionId ??= event.session_id;
    if (!duplicate) state.positions.set(key, previous.events.length + index);
    const provider = providerKey(event);
    const displayEvent = !duplicate && (!provider || !state.providerIds.has(provider));
    if (!duplicate && provider) state.providerIds.add(provider);
    if (
      event.type === 'artifact_group' &&
      Array.isArray(event.data.items) &&
      (event.data.items as WorkspaceEvent[]).some((item) => conversationRun(item) !== run)
    )
      state.fullProjection = true;
    if (event.type === 'status') {
      state.lastStatus = event;
      if (event.run_id) state.lastRunStatus = event;
      if (event.data.state === 'provider_stopped') state.providerStopped = true;
      if (displayEvent) {
        state.displayStatus = event;
        group.displayStatus = event;
      }
    }
    const userMessage = event.type === 'message' && event.data.role === 'user';
    if (event.type === 'message') state.hasConversation = true;
    if (userMessage) {
      state.lastQuestion = event;
      if (!duplicate) state.pendingPrompt = event;
    } else if (event.run_id && !duplicate && group.flowStart === undefined) {
      group.flowStart = group.events.length - 1;
      group.flowPrompt = state.pendingPrompt;
    }
    if (event.run_id) {
      if (event.type === 'analysis_context') state.runContexts.set(event.run_id, event);
      if (isLanguage(event.data.language) && !state.runLanguages.has(event.run_id))
        state.runLanguages.set(event.run_id, event.data.language);
      if (
        ['run_started', 'status', 'analysis_context'].includes(event.type) &&
        typeof event.data.action_intent === 'string'
      )
        state.actionIntents.set(event.run_id, event.data.action_intent);
    }
  });
  if (!state.fullProjection)
    for (const run of touched) projectRun(state.groups.get(run)!, previous.groups.get(run), state);
  if (state.fullProjection) {
    state.flowRuns = projectAgentFlow(state.events);
    state.visibleEvents = projectConversation(state.events);
    state.entries = state.visibleEvents.map((event, index) =>
      entryFor(
        event,
        state,
        state.events,
        event.run_id ? state.flowRuns.get(event.run_id) : undefined,
        questionForAnswer(event, state.events),
        index,
        0,
      ),
    );
  } else {
    state.flowRuns = new Map();
    state.entries = [];
    for (const group of state.groups.values()) {
      if (group.flow) state.flowRuns.set(group.flow.runId, group.flow);
      state.entries.push(
        ...group.entries.filter((entry) => !entry.queued || entry.event === state.displayStatus),
      );
    }
    state.entries.sort(
      (left, right) => left.position - right.position || left.offset - right.offset,
    );
    state.visibleEvents = state.entries.map((entry) => entry.event);
  }
  return state;
}

export function eventsForReport(
  state: ConversationState,
  report: Pick<WorkspaceEvent, 'session_id' | 'run_id'>,
): WorkspaceEvent[] {
  return state.fullProjection
    ? state.events
    : (state.groups.get(conversationRun(report))?.events ?? []);
}
