import { pendingPromptStorageKey, pendingPromptStoragePrefix } from '../src/agent-prompt-requests';

const keys = new Set([
  'analyst.workspace.session.v1',
  'analyst.workspace.draft.v2',
  pendingPromptStorageKey,
]);

// Browser fixtures exercise real storage without retaining or overwriting visitor state.
export function isolateAgentStorage() {
  const selectedKeys = (storage: Storage) =>
    Array.from({ length: storage.length }, (_, index) => storage.key(index)!).filter(
      (key) => keys.has(key) || key.startsWith(pendingPromptStoragePrefix),
    );
  const saved = [localStorage, sessionStorage].map((storage) => ({
    storage,
    entries: selectedKeys(storage).map((key) => [key, storage.getItem(key)!] as const),
  }));
  const clear = () => {
    for (const { storage } of saved)
      for (const key of selectedKeys(storage)) storage.removeItem(key);
  };
  clear();
  return {
    clear,
    restore() {
      clear();
      for (const { storage, entries } of saved)
        for (const [key, value] of entries) storage.setItem(key, value);
    },
  };
}
