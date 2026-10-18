import { useEffect, useLayoutEffect, useRef } from 'react';
import { flushSync } from 'react-dom';

export type WebMcpExecutionOptions = { signal?: AbortSignal };
export type WebMcpTool = {
  name: string;
  title?: string;
  description: string;
  inputSchema: Record<string, unknown>;
  annotations: {
    readOnlyHint: boolean;
    consequentialHint: boolean;
    untrustedContentHint: boolean;
  };
  execute(input: unknown, options: WebMcpExecutionOptions): unknown | Promise<unknown>;
};
export type WebMcpToolMetadata = Omit<WebMcpTool, 'execute'>;
export type WebMcpContext = {
  registerTool(tool: WebMcpTool, options?: { signal?: AbortSignal }): void | Promise<void>;
};

function reportRegistrationFailure(name: string, error: unknown) {
  const kind = error instanceof Error ? error.name : 'Error';
  // Registration diagnostics must not include request data, sessions or provider details.
  console.warn(`[WebMCP] Could not register ${name} (${kind}).`);
}

/** No cross-origin exposure options are supplied. Native same-origin defaults apply. */
export function registerWebMcpTools(
  context: WebMcpContext | undefined,
  definitions: readonly WebMcpToolMetadata[],
  currentTool: (name: string) => WebMcpTool | undefined,
  reportFailure: (name: string, error: unknown) => void = reportRegistrationFailure,
): () => void {
  const lifecycle = new AbortController();
  if (!context?.registerTool) return () => lifecycle.abort();
  for (const definition of definitions) {
    try {
      const registered = context.registerTool(
        {
          ...definition,
          async execute(input, options = {}) {
            if (lifecycle.signal.aborted) throw new Error('This tool is no longer available.');
            throwIfAborted(options.signal);
            const tool = currentTool(definition.name);
            if (!tool) throw new Error('This workspace tool is no longer available.');
            // Registration abort only removes availability. It does not cancel an invocation
            // already in progress, which may itself change the visible workspace.
            return await tool.execute(input, options);
          },
        },
        { signal: lifecycle.signal },
      );
      void Promise.resolve(registered).catch((error: unknown) => {
        if (!lifecycle.signal.aborted) reportFailure(definition.name, error);
      });
    } catch (error) {
      if (!lifecycle.signal.aborted) reportFailure(definition.name, error);
    }
  }
  return () => lifecycle.abort();
}

/** Keep registrations stable while dispatching only to the latest committed React state. */
export function useWebMcpTools(tools: readonly WebMcpTool[], enabled = true): void {
  const latest = useRef({ tools, enabled });
  useLayoutEffect(() => {
    latest.current = { tools, enabled };
  }, [tools, enabled]);
  const metadata = JSON.stringify(
    tools.map(({ name, title, description, inputSchema, annotations }) => ({
      name,
      title,
      description,
      inputSchema,
      annotations,
    })),
  );
  useEffect(() => {
    if (!enabled || typeof document === 'undefined') return;
    try {
      const context = (document as Document & { modelContext?: WebMcpContext }).modelContext;
      return registerWebMcpTools(context, JSON.parse(metadata) as WebMcpToolMetadata[], (name) =>
        latest.current.enabled
          ? latest.current.tools.find((tool) => tool.name === name)
          : undefined,
      );
    } catch (error) {
      reportRegistrationFailure('workspace tools', error);
    }
  }, [enabled, metadata]);
}

export function objectInput(
  input: unknown,
  allowedKeys: readonly string[],
  requiredKeys: readonly string[] = [],
): Record<string, unknown> {
  if (
    input === null ||
    typeof input !== 'object' ||
    Array.isArray(input) ||
    ![Object.prototype, null].includes(Object.getPrototypeOf(input))
  )
    throw new TypeError('Tool input must be a plain object.');
  const record = input as Record<string, unknown>;
  if (Reflect.ownKeys(record).some((key) => typeof key !== 'string' || !allowedKeys.includes(key)))
    throw new TypeError('Tool input contains an unknown property.');
  if (requiredKeys.some((key) => !Object.hasOwn(record, key)))
    throw new TypeError('Tool input is missing a required property.');
  return record;
}

export function stringInput(
  value: unknown,
  name: string,
  options: { minLength?: number; maxLength?: number; values?: readonly string[] } = {},
): string {
  if (
    typeof value !== 'string' ||
    value.length < (options.minLength ?? 0) ||
    (options.maxLength !== undefined && value.length > options.maxLength) ||
    (options.values !== undefined && !options.values.includes(value))
  )
    throw new TypeError(`${name} must be an allowed string value.`);
  return value;
}

export function throwIfAborted(signal?: AbortSignal): void {
  if (signal?.aborted) throw new DOMException('Tool invocation was aborted.', 'AbortError');
}

function nextUiFrame(signal?: AbortSignal): Promise<void> {
  throwIfAborted(signal);
  return new Promise<void>((resolve, reject) => {
    const abort = () => {
      cancelAnimationFrame(frame);
      reject(new DOMException('Tool invocation was aborted.', 'AbortError'));
    };
    const frame = requestAnimationFrame(() => {
      signal?.removeEventListener('abort', abort);
      if (signal?.aborted) abort();
      else resolve();
    });
    signal?.addEventListener('abort', abort, { once: true });
  });
}

/** Resolve only after synchronous React work commits and the UI receives a frame. */
export async function commitUi(action: () => void, signal?: AbortSignal): Promise<void> {
  throwIfAborted(signal);
  flushSync(action);
  await nextUiFrame(signal);
}

/** The caller checks mounted state and immutable context while waiting for async UI work. */
export async function waitForUi<T>(read: () => T | undefined, signal?: AbortSignal): Promise<T> {
  for (;;) {
    throwIfAborted(signal);
    const value = read();
    if (value !== undefined) return value;
    await nextUiFrame(signal);
  }
}
