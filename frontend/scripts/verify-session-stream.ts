import assert from 'node:assert/strict';
import { createSessionEventStream } from '../src/stream';
import type { WorkspaceEvent } from '../src/types';

const event = (
  id: number | string,
  type = 'status',
  data: Record<string, unknown> = { state: 'running' },
): WorkspaceEvent => ({
  id,
  session_id: 'stream-fixture',
  run_id: 'run-fixture',
  type,
  time: '2026-10-18T00:00:00Z',
  data,
});
function scheduler() {
  const pending = new Set<() => void>();
  let schedules = 0;
  return {
    pending,
    get schedules() {
      return schedules;
    },
    schedule(publish: () => void) {
      schedules += 1;
      pending.add(publish);
      return () => pending.delete(publish);
    },
    flush() {
      for (const publish of [...pending]) {
        pending.delete(publish);
        publish();
      }
    },
  };
}

const restored = Object.freeze([
  Object.freeze(event(7, 'message', { role: 'user', text: 'Repeated question' })),
  Object.freeze(event(9, 'status', { state: 'completed' })),
]);
const restoredSnapshot = JSON.stringify(restored);
const clock = scheduler();
const batches: WorkspaceEvent[][] = [];
const stream = createSessionEventStream(
  'stream-fixture',
  restored,
  (batch) => batches.push(batch),
  clock.schedule,
);
assert.equal(stream.cursor, 9);
assert.equal(stream.receive({ type: 'history', events: [...restored].reverse() }), 0);
assert.equal(stream.cursor, 9, 'An older replay cannot move the reconnect cursor backward.');
assert.equal(clock.schedules, 0, 'A pure replay cannot publish history or repeat UI side effects.');
assert.equal(batches.length, 0);

const command = Object.freeze(event(10, 'ui_command', { command_id: 'command-1' }));
assert.equal(stream.receive(command), 1);
assert.equal(
  stream.receive({ ...command, id: '10' }),
  0,
  'String and numeric durable IDs identify one event.',
);
assert.equal(stream.receive({ events: [event(12), event(11), event(12)] }), 2);
assert.equal(
  stream.cursor,
  12,
  'The cursor keeps the largest durable ID in an overlapping packet.',
);
assert.equal(clock.schedules, 1, 'A burst only schedules one history publication.');
assert.equal(batches.length, 0);
clock.flush();
assert.deepEqual(
  batches[0].map((item) => item.id),
  [10, 12, 11],
  'All fresh events retain their received order.',
);
assert.equal(
  batches[0][0],
  command,
  'The stream preserves event payloads without rewriting evidence.',
);
assert.equal(stream.receive(command), 0);
clock.flush();
assert.equal(
  batches.length,
  1,
  'A replayed UI command never reaches the side-effect consumer again.',
);

for (const value of [
  null,
  [],
  { events: null },
  { events: {} },
  event(0),
  event(-1),
  event(1.5),
  event(Number.MAX_SAFE_INTEGER + 1),
  event('not-a-cursor'),
  { ...event(13), session_id: 'another-session' },
  { ...event(13), type: '' },
  { ...event(13), time: null },
  { ...event(13), data: null },
  { ...event(13), data: [] },
  { ...event(13), run_id: {} },
  { events: [event(13), null] },
]) {
  assert.throws(() => stream.receive(value), /unreadable/);
  assert.equal(stream.cursor, 12, 'An invalid packet must not advance the replay cursor.');
  assert.equal(clock.pending.size, 0);
}
assert.equal(stream.receive({ events: [] }), 0);
assert.equal(
  stream.receive(event(13)),
  1,
  'A rejected packet did not consume the valid event before its bad entry.',
);
clock.flush();
assert.equal(batches.length, 2);
assert.equal(
  JSON.stringify(restored),
  restoredSnapshot,
  'Reception never changes the restored canonical history.',
);

assert.equal(stream.receive(event(14)), 1);
const latePublication = [...clock.pending][0];
stream.dispose();
assert.equal(clock.pending.size, 0, 'Disposal cancels the scheduled history publication.');
latePublication();
assert.equal(stream.receive(event(15)), 0, 'A disposed connection cannot accept late messages.');
assert.equal(stream.cursor, 14);
assert.equal(batches.length, 2, 'Even a queued callback after disposal cannot update React state.');

// Deterministic operation counts use the previous handler's exact ID scan and
// append behavior. These are synthetic frontend costs, not live provider timings.
const historySize = 10_000;
const packetCount = 1_000;
let historyIdReads = 0;
const history = Object.freeze(
  Array.from({ length: historySize }, (_, index) => {
    const source = event(index + 1);
    return Object.freeze({
      ...source,
      get id() {
        historyIdReads += 1;
        return source.id;
      },
    });
  }),
);
const replays = Array.from({ length: packetCount }, (_, index) => event(index + 1));
const burst = Array.from({ length: packetCount }, (_, index) => event(historySize + index + 1));
function oldHandler(packets: WorkspaceEvent[]) {
  let previous: readonly WorkspaceEvent[] = history;
  let idScans = 0;
  let publications = 0;
  for (const incoming of packets) {
    const ids = new Set(
      previous.map((item) => {
        idScans += 1;
        return String(item.id);
      }),
    );
    previous = [
      ...previous,
      ...[incoming].filter((item) => {
        const id = String(item.id);
        if (ids.has(id)) return false;
        ids.add(id);
        return true;
      }),
    ];
    publications += 1;
  }
  return { idScans, publications, events: previous };
}
const previousReplay = oldHandler(replays);
const previousBurst = oldHandler(burst);
assert.equal(previousReplay.idScans, historySize * packetCount);
assert.equal(
  previousBurst.idScans,
  historySize * packetCount + (packetCount * (packetCount - 1)) / 2,
);

const measureClock = scheduler();
const measuredBatches: WorkspaceEvent[][] = [];
historyIdReads = 0;
const measured = createSessionEventStream(
  'stream-fixture',
  history,
  (batch) => measuredBatches.push(batch),
  measureClock.schedule,
);
assert.equal(historyIdReads, historySize, 'The durable ID index is seeded once per session.');
historyIdReads = 0;
for (const item of replays) assert.equal(measured.receive(item), 0);
assert.equal(historyIdReads, 0, 'Replays do not rescan any restored history IDs.');
assert.equal(measureClock.schedules, 0);
for (const item of burst) assert.equal(measured.receive(item), 1);
assert.equal(historyIdReads, 0, 'A fresh burst does not rescan historical IDs either.');
assert.equal(measureClock.schedules, 1);
measureClock.flush();
assert.equal(measuredBatches.length, 1);
assert.deepEqual(measuredBatches[0], burst, 'Coalescing retains every accepted event.');
assert.deepEqual([...history, ...measuredBatches[0]], previousBurst.events);
assert.equal(measured.cursor, historySize + packetCount);
measured.dispose();

console.log(
  `Verified session stream: ${historySize} stored events / ${packetCount} incoming packets. Replay historical-ID scans ${previousReplay.idScans} -> 0 after one ${historySize}-ID seed; publications ${previousReplay.publications} -> 0. Burst historical-ID scans ${previousBurst.idScans} -> 0; publications ${previousBurst.publications} -> 1. Cursor, packet validation, replay side effects, disposal and canonical history checks passed.`,
);
