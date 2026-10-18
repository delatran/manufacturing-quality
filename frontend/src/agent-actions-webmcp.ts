import type { AnswerReference } from './answer-reply';
import { validClientRequestId } from './agent-prompt-requests';
import type { PromptRequest } from './agent-prompt-requests';
import { objectInput, stringInput, throwIfAborted } from './webmcp';
import type { WebMcpTool } from './webmcp';

export type AgentActionAdapter = {
  getStatus(): unknown;
  stagePrompt(text: string, contextId: string | null, signal?: AbortSignal): Promise<unknown>;
  stageFollowup(reference: AnswerReference, text: string, signal?: AbortSignal): Promise<unknown>;
  submit(request: PromptRequest, signal?: AbortSignal): Promise<unknown>;
  readReceipt(clientRequestId: string, signal?: AbortSignal): Promise<unknown>;
  cancel(runId: string, signal?: AbortSignal): Promise<unknown>;
};
const contextSchema = { type: ['string', 'null'], pattern: '^[a-f0-9]{16,64}$' };
const runSchema = { type: 'string', pattern: '^[a-f0-9]{32}$' };
const keySchema = {
  type: 'string',
  format: 'uuid',
  description:
    'Caller-created lowercase UUID. Reuse it for this exact request and for receipt readback. Never replace a key after uncertain delivery.',
};
const textSchema = { type: 'string', minLength: 1 };
const replySchema = {
  type: 'object',
  properties: { run_id: runSchema, message_id: { type: 'integer', minimum: 1 } },
  required: ['run_id', 'message_id'],
  additionalProperties: false,
};
const schema = (properties: Record<string, unknown>, required = Object.keys(properties)) => ({
  type: 'object',
  properties,
  required,
  additionalProperties: false,
});
const annotations = (readOnlyHint: boolean, consequentialHint = false) => ({
  readOnlyHint,
  consequentialHint,
  untrustedContentHint: true,
});
function requestId(value: unknown): string {
  if (!validClientRequestId(value))
    throw new TypeError('client_request_id must be a lowercase UUID');
  return value;
}
function contextId(value: unknown): string | null {
  if (value === null) return null;
  if (typeof value !== 'string' || !/^[a-f0-9]{16,64}$/.test(value))
    throw new TypeError('analysis_context_id must be a known Dashboard context ID or null');
  return value;
}
function runId(value: unknown): string {
  if (typeof value !== 'string' || !/^[a-f0-9]{32}$/.test(value))
    throw new TypeError('run_id must be a known run ID');
  return value;
}
function replyTo(value: unknown): AnswerReference {
  const reply = objectInput(value, ['run_id', 'message_id'], ['run_id', 'message_id']);
  const run_id = runId(reply.run_id);
  if (!Number.isSafeInteger(reply.message_id) || Number(reply.message_id) <= 0)
    throw new TypeError('message_id must be a positive safe integer');
  return { run_id, message_id: Number(reply.message_id) };
}
function promptText(value: unknown): string {
  const text = stringInput(value, 'text', { minLength: 1 }).trim();
  if (!text) throw new TypeError('text must contain a question or instruction');
  return text;
}

export function buildAgentActionTools(adapter: AgentActionAdapter): WebMcpTool[] {
  return [
    {
      name: 'read_agent_status',
      title: 'Read current Agent status',
      description:
        'Read the mounted visitor Agent session, current draft, immutable run context, pending request key and cancellation state. No analysis is started. A pending send or cancellation request is not completion.',
      inputSchema: schema({}),
      annotations: annotations(true),
      execute(input, { signal } = {}) {
        objectInput(input, []);
        throwIfAborted(signal);
        return adapter.getStatus();
      },
    },
    {
      name: 'stage_agent_prompt',
      title: 'Stage an Agent question',
      description:
        'Replace the visible Agent composer draft using its known current context. While a run is active, this stages an instruction for that immutable run. Does not submit, spend or start analysis. Read status and explicitly submit or steer after reviewing the staged text.',
      inputSchema: schema({ text: textSchema, analysis_context_id: contextSchema }),
      annotations: annotations(false),
      execute(input, { signal } = {}) {
        const args = objectInput(
          input,
          ['text', 'analysis_context_id'],
          ['text', 'analysis_context_id'],
        );
        throwIfAborted(signal);
        return adapter.stagePrompt(
          promptText(args.text),
          contextId(args.analysis_context_id),
          signal,
        );
      },
    },
    {
      name: 'stage_agent_answer_followup',
      title: 'Stage a follow-up on a verified answer',
      description:
        "Stage text in the visible composer tied to a verified answer in the mounted visitor session. Keeps that answer's original context and language. Does not submit. A later submission starts or queues a separate analysis. Rejected if the answer is no longer eligible.",
      inputSchema: schema({
        run_id: runSchema,
        message_id: { type: 'integer', minimum: 1 },
        text: textSchema,
      }),
      annotations: annotations(false),
      execute(input, { signal } = {}) {
        const args = objectInput(
          input,
          ['run_id', 'message_id', 'text'],
          ['run_id', 'message_id', 'text'],
        );
        throwIfAborted(signal);
        return adapter.stageFollowup(
          replyTo({ run_id: args.run_id, message_id: args.message_id }),
          promptText(args.text),
          signal,
        );
      },
    },
    {
      name: 'submit_agent_prompt',
      title: 'Submit the staged Agent question',
      description:
        'Submit the exact visible staged text/context/language to paid Agent analysis. Requires a caller-provided stable UUID. Include reply_to exactly when the staged draft references an answer. Returns admission or awaiting_acknowledgement, never model completion. Repeated identical keys resolve the original receipt before current admission guards; conflicting reuse fails. If uncertain, read the same key, do not create a replacement or automatically retry. Aborting this invocation after dispatch does not cancel the provider.',
      inputSchema: schema(
        {
          client_request_id: keySchema,
          text: textSchema,
          analysis_context_id: contextSchema,
          language: { type: 'string', enum: ['en', 'vi'] },
          reply_to: replySchema,
        },
        ['client_request_id', 'text', 'analysis_context_id', 'language'],
      ),
      annotations: annotations(false, true),
      execute(input, { signal } = {}) {
        const keys = ['client_request_id', 'text', 'analysis_context_id', 'language', 'reply_to'];
        const args = objectInput(input, keys, keys.slice(0, 4));
        throwIfAborted(signal);
        return adapter.submit(
          {
            client_request_id: requestId(args.client_request_id),
            text: promptText(args.text),
            analysis_context_id: contextId(args.analysis_context_id),
            language: stringInput(args.language, 'language', { values: ['en', 'vi'] }) as
              'en' | 'vi',
            action_intent: 'none',
            request_origin: 'webmcp',
            ...(args.reply_to !== undefined ? { reply_to: replyTo(args.reply_to) } : {}),
          },
          signal,
        );
      },
    },
    {
      name: 'steer_agent_run',
      title: 'Submit a staged instruction for the active run',
      description:
        'Submit the exact staged instruction to the named current active run using its recorded context, language and action intent. Requires a caller-provided stable UUID and may spend. Server admission can queue a separate run for an explicit language change. Returns the actual receipt or awaiting_acknowledgement, not analysis completion. Repeated keys resolve the original receipt; never retry uncertainty under a new key. Invocation cancellation after dispatch does not prove provider cancellation.',
      inputSchema: schema({
        client_request_id: keySchema,
        text: textSchema,
        run_id: runSchema,
        analysis_context_id: contextSchema,
        language: { type: 'string', enum: ['en', 'vi'] },
        action_intent: { type: 'string', enum: ['none', 'draft_create', 'draft_update'] },
      }),
      annotations: annotations(false, true),
      execute(input, { signal } = {}) {
        const keys = [
          'client_request_id',
          'text',
          'run_id',
          'analysis_context_id',
          'language',
          'action_intent',
        ];
        const args = objectInput(input, keys, keys);
        throwIfAborted(signal);
        return adapter.submit(
          {
            client_request_id: requestId(args.client_request_id),
            text: promptText(args.text),
            steer_run_id: runId(args.run_id),
            analysis_context_id: contextId(args.analysis_context_id),
            language: stringInput(args.language, 'language', { values: ['en', 'vi'] }) as
              'en' | 'vi',
            action_intent: stringInput(args.action_intent, 'action_intent', {
              values: ['none', 'draft_create', 'draft_update'],
            }) as PromptRequest['action_intent'],
            request_origin: 'webmcp',
          },
          signal,
        );
      },
    },
    {
      name: 'read_agent_prompt_receipt',
      title: 'Read an Agent submission receipt',
      description:
        'Read the durable receipt for a caller UUID within the mounted visitor session and reconcile its visible pending-send state. Does not submit or replay. Accepted means application admission only; reserved, uncertain or missing receipts never authorize a replacement key or prove provider cancellation.',
      inputSchema: schema({ client_request_id: keySchema }),
      annotations: annotations(false),
      execute(input, { signal } = {}) {
        const args = objectInput(input, ['client_request_id'], ['client_request_id']);
        throwIfAborted(signal);
        return adapter.readReceipt(requestId(args.client_request_id), signal);
      },
    },
    {
      name: 'request_agent_cancellation',
      title: 'Request cancellation of the active analysis',
      description:
        'Request cancellation of the named currently active run and update visible stopping state. A successful HTTP response only confirms a cancellation request. Read Agent status until a terminal run state is observed. Invocation abortion or request failure does not prove the provider stopped.',
      inputSchema: schema({ run_id: runSchema }),
      annotations: annotations(false, true),
      execute(input, { signal } = {}) {
        const args = objectInput(input, ['run_id'], ['run_id']);
        throwIfAborted(signal);
        return adapter.cancel(runId(args.run_id), signal);
      },
    },
  ];
}
