import assert from 'node:assert/strict';
import { conversationScrollPosition } from '../src/conversation-scroll';

const desktop = {
  scrollY: 800,
  viewportTop: 0,
  viewportHeight: 900,
  composerTop: 650,
  tailBottom: 630,
  documentHeight: 2200,
  layoutHeight: 900,
};

// The meaningful tail is already clear of the fixed composer, even though the
// document includes extra bottom padding. Reaching document bottom is unnecessary.
assert.deepEqual(conversationScrollPosition(desktop), { nearBottom: true, target: 800 });

// Reading older content disables follow, including when the layout viewport's
// bottom would misleadingly put the new response inside the visible window.
assert.deepEqual(conversationScrollPosition({ ...desktop, tailBottom: 850 }), {
  nearBottom: false,
  target: 1020,
});
assert.equal(conversationScrollPosition({ ...desktop, tailBottom: 729 }).nearBottom, true);
assert.equal(conversationScrollPosition({ ...desktop, tailBottom: 731 }).nearBottom, false);

// The visual viewport shrinks and pans when the keyboard opens. Its lower edge
// bounds the reading area even if the composer still has its old layout position.
assert.deepEqual(
  conversationScrollPosition({
    ...desktop,
    viewportTop: 120,
    viewportHeight: 350,
    tailBottom: 630,
  }),
  { nearBottom: false, target: 980 },
);

// When the dock moves into the keyboard-safe area, use its top, with a reading gap.
assert.deepEqual(
  conversationScrollPosition({
    ...desktop,
    viewportTop: 120,
    viewportHeight: 350,
    composerTop: 390,
    tailBottom: 630,
  }),
  { nearBottom: false, target: 1060 },
);

// Requests remain within the actual document's scroll range on short pages,
// negative overscroll and a keyboard viewport occupied entirely by the composer.
assert.equal(conversationScrollPosition({ ...desktop, tailBottom: 100, scrollY: 0 }).target, 0);
assert.equal(conversationScrollPosition({ ...desktop, tailBottom: 5000 }).target, 1300);
assert.equal(
  conversationScrollPosition({
    ...desktop,
    viewportTop: 120,
    composerTop: 100,
    tailBottom: 400,
  }).target,
  1080,
);
assert.equal(conversationScrollPosition({ ...desktop, documentHeight: 500, scrollY: 0 }).target, 0);

console.log('Conversation scroll verification passed: reader position, composer and keyboard.');
