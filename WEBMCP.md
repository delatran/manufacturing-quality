# Dashboard and Agent WebMCP

The application registers imperative browser tools through `document.modelContext.registerTool` when the browser provides that native API. Tools invoke the existing mounted React actions and canonical backend routes. Python remains the source of numerical results. Browsers without the API retain the ordinary Dashboard and Agent UI.

The registry in `frontend/src/webmcp.ts` registers stable metadata, dispatches to the latest committed component state, handles synchronous and asynchronous registration errors, and unregisters with a component lifecycle `AbortSignal`. Invocation cancellation is separate from registration cleanup. No cross-origin exposure option is supplied, so native same-origin defaults apply. There is no polyfill or server MCP transport.

Read current tool descriptions and schemas after changing workspace or opening the records panel. Availability follows the mounted UI. Inputs are validated again during execution, including unknown properties, allowlisted values, immutable context IDs, current session/run ownership and stale references. Annotation hints describe effects; they do not replace these checks.

## Tool coverage

Workspace tools are available through the root application. Dashboard tools require the visible Dashboard. The two records tools also require its records panel to be open. Agent action and answer tools require the mounted current visitor session.

| Tool | Effect and completion boundary |
| --- | --- |
| `read_workspace_state` | Reads visible workspace, UI language, current immutable Dashboard context, source/loading state, allowed filters, recommendation priorities and the last Dashboard staging receipt. |
| `navigate_workspace` | Opens Dashboard or Agent and returns after the selected UI is mounted. Discover tools again afterward. |
| `set_workspace_language` | Commits the EN/VI UI language. Stored answer text retains its original language. |
| `apply_dashboard_scope` | Applies allowlisted filter changes, opens Dashboard and returns the canonical backend result after the UI commits. Running Agent contexts remain immutable. |
| `refresh_dashboard_source` | Runs the existing refresh action and displays the resulting context. Retained stale source state remains explicit. |
| `stage_dashboard_analysis` | Opens Agent and prefills a context-bound analysis or current recommendation. Returns `staged` only after the composer confirms it, with `submitted: false`. |
| `read_dashboard_summary` | Reads current backend KPIs, priorities, freshness and available sections without changing scope. |
| `read_dashboard_panel` | Reads backend values supporting a named section. Each series includes pagination and omitted-row metadata; reading does not change chart controls. |
| `open_dashboard_panel` | Opens and focuses an existing section. Records accepts the existing exact date filter; evidence accepts a current priority or the whole view. Navigation can finish while records are still loading. |
| `request_dashboard_export` | Fetches the existing CSV or quality workbook and requests a browser download. Returns byte count, MIME and SHA-256, with `download_requested` and `save_confirmed: false`. |
| `read_dashboard_records` | Reads the current visible records page, exact date, sort, pagination and loading/error state. |
| `set_dashboard_records_view` | Applies allowlisted sorting or paging to the open table and waits for the replacement backend page to commit. |
| `read_agent_status` | Reads the current visitor session, staged draft, active immutable run context, pending request key and cancellation state. |
| `stage_agent_prompt` | Replaces the visible composer draft using its known context. During a run it stages a detail for that run. It does not submit. |
| `stage_agent_answer_followup` | Stages text linked to an eligible verified answer and its original context. It does not submit. |
| `submit_agent_prompt` | Submits the exact staged text/context/language using a caller-owned UUID. Returns admission or `awaiting_acknowledgement`, never analysis completion. |
| `steer_agent_run` | Submits the exact staged detail to the named active run using its recorded context, language and action intent. An explicit language change can queue a separate run. |
| `read_agent_prompt_receipt` | Reads the durable admission receipt and reconciles visible pending-send state. It never submits or replays a request. |
| `request_agent_cancellation` | Requests cancellation of the named current run and updates visible stopping state. A successful request does not establish that the provider has stopped. |
| `list_agent_answers` | Lists eligible completed answers in the current session with original context, stable pagination and explicit omissions. |
| `read_agent_answer` | Reads rendered answer text and registered chart metadata with source context and pagination. Hidden files and raw receipts are excluded. |
| `read_agent_answer_evidence` | Reads validated numeric claims with execution identity, JSON evidence path and code hash. Invalid or ambiguous claims are omitted explicitly. |
| `navigate_agent_answer` | Focuses an eligible answer or opens its existing citation/chart control and checks the resulting visible state. |

`submit_agent_prompt`, `steer_agent_run` and `request_agent_cancellation` are consequential mutations. Staging and navigation change visible UI state without submitting paid work. `read_agent_prompt_receipt` is marked as a mutation because it reconciles the visible pending state, even though its backend request is a read. Source data, user text and generated answers are treated as untrusted content.

## Scope and evidence

Dashboard section identifiers are `overview`, `trend`, `warnings`, `location`, `hotspots`, `cost`, `records`, `evidence` and `exports`. Scope changes use the values returned by current workspace state. Existing scenario identifiers are the empty canonical choice, `exclude_cost_10`, `ambiguous_dm` and `year_2024`; these are policy assumptions, not observed improvements. Retired Scenario, Operations and Investigation panels are not exposed.

Records support the existing exact `defect_date`, sort and page controls. There is no free-text or arbitrary-ID search API. Record tools return the cleaned records in the applied scope and their pagination state.

The filtered CSV uses the applied Dashboard context. Record-table date, sorting and pagination do not alter CSV scope. The quality workbook is the existing completed full-case workbook and does not adopt Dashboard filters. A download request confirms fetched response metadata and the browser handoff, not a saved file on disk.

Answer tools recheck current eligibility, the final answer message, completed run state, original `analysis_context`, current session ownership and late invalidation. Historical answers retain their original evidence and are explicitly labeled as stored evidence, even when their context ID matches the current Dashboard. They do not establish fresh Dashboard findings or authority for another action. Inline chart metadata is limited to registered charts in the validated answer's delivery artifacts. Result exports remain unavailable.

Answer-list pages contain at most 50 entries and require the returned `history_revision` for continuation. Answer text pages contain at most 12,000 UTF-16 code units and require `content_sha256` for continuation. Chart and claim pages have separate offsets and omission counts. These are read-response page sizes, not model, prompt, run or history limits. Complete stored conversation history is preserved.

## Durable prompt admission

The ordinary UI and WebMCP actions share the prompt path. Existing unkeyed UI/API callers retain their previous behavior. Keyed callers send these additional fields through either the existing WebSocket prompt message or HTTP `POST /api/sessions/{sid}/prompt`:

| Field | Contract |
| --- | --- |
| `client_request_id` | A caller-created canonical lowercase UUID. Mandatory for `request_origin: "webmcp"`. Keep the same key for the same deliberate submission and any readback. |
| `request_origin` | `ui` or `webmcp`. Defaults to `ui` for existing callers. It participates in payload identity. |

The canonical logical payload consists of trimmed `text`, `analysis_context_id`, `language`, `action_intent`, `steer_run_id`, `reply_to` and `request_origin`. Absent optional context/run/reply references become `null`. The stored canonical JSON is compared exactly; `request_sha256` is returned as an additional identity, not as a substitute for comparison. Transport fields such as the WebSocket `type` are excluded. Invalid types, unknown keyed body fields and malformed identities fail before reservation.

`prompt_requests` is an additive SQLite table keyed by `(session_id, client_request_id)`. `INSERT ... ON CONFLICT ... DO NOTHING` chooses one owner even across independent database connections. The existing request is checked before transient provider availability, provider-stop, run-target, answer-eligibility and context-integrity guards. Repeating the same payload returns its original receipt with `replayed: true`; a different payload returns HTTP 409 or a keyed WebSocket error with `code: "idempotency_conflict"`.

The direct HTTP response is the receipt. WebSocket acknowledgements are durable `prompt_receipt` events whose `data` holds the receipt. Their `id`, `time` and replay behavior follow the existing event stream. Keyed user-message events also include `client_request_id`. Structural errors retain the key in their error details, allowing the client to identify the affected submission.

Readback is `GET /api/sessions/{sid}/prompt-requests/{client_request_id}` with `Cache-Control: no-store`. It validates session ownership and returns 404 for an unknown session/request combination. The route remains under the existing `/api/sessions` Cloudflare visitor-container boundary.

| Receipt status | Meaning and caller behavior |
| --- | --- |
| `reserved` | This key is owned by an admission that has not finished. No duplicate admission is started. Read the same receipt to resolve it. |
| `accepted` | The application admitted the original request. A direct start includes its durable `run_id`; queued work has `queued: true` and can have `run_id: null`; same-run steering includes the target run and instruction ID. This is not a completed answer or a promise that provider execution will succeed. |
| `rejected` | A valid keyed request failed admission before a consequential side effect. The rejection is retained under the same key. No paid run is created by replaying it. |
| `uncertain` | A side effect or interrupted admission cannot be confirmed. The receipt remains uncertain and the original request is never automatically replayed. Do not create a replacement key to resolve uncertainty. |

Receipts include `session_id`, `client_request_id`, `request`, `request_sha256`, `created_at`, `status`, `action`, `run_id` and `replayed`, plus relevant queue/steering/error fields. They describe the original admission. They are not rewritten to claim later run completion; read Agent status and eligible answers for that outcome.

Reservations are committed before admission side effects. Fresh keyed runs get a durable run record before scheduling. Queued and same-run instructions carry an optional request key; legacy instruction rows remain intact. Instructions associated with reserved or uncertain requests are retained but excluded from automatic consumption and follow-up run selection. Restart recovery changes unresolved reservations to `uncertain` and preserves spending history and unresolved budget reservations.

`SessionStore` admission boundaries use the existing `CloudSessionStore._checkpoint` override. Reservation, pre-effect state and final admission receipt are synchronously included in the existing `SnapshotBridge` SQLite backup archive before the corresponding side effect or acknowledgement. Storage failures prevent a new confirmed acknowledgement. No separate database or persistence transport is introduced. As with all restored state, deduplication depends on retaining the workspace database and its acknowledged snapshots; deliberate database deletion discards that history.

## Invocation and cancellation

The UI generates one key per deliberate send and retains a pending key/payload for reload readback. Pending payloads have independent local-storage records keyed by session and request, while session storage retains this tab's session and pending request identity. Another tab's submission or acknowledgement cannot replace that recovery identity. The previous single pending record is migrated when read and retained until its matching receipt is confirmed. WebMCP callers must supply their own stable key. Re-executing identical tool arguments does not silently generate a new key. A send can return `awaiting_acknowledgement` after the visible pending state commits; this only confirms dispatch was attempted.

After dispatch, aborting a WebMCP invocation, closing a tab or losing the WebSocket does not prove that paid provider work was cancelled. Read the same receipt. To stop an active run, use `request_agent_cancellation` with its current run ID and observe a terminal run state. Do not retry an uncertain request automatically.

Dashboard source-refresh invocations forward cancellation to the HTTP request. Aborting clears the visible loading state while preserving the last committed Dashboard context; it does not undo source work that the server may already have performed.

All paid work retains `gpt-6-luna`, the managed Agents API, immutable run contexts, existing QA and prewrite authority checks, provider-stop behavior, and the owner-authorized unbounded application profile. Absent application limits stay `null`. Tools do not expose arbitrary URLs, file paths, SQL, shell or Python execution and do not alter original input data.

## Verification and current references

Backend admission checks are in `tests/test_webmcp_prompt_receipts.py`. They cover two-connection arbitration, canonical conflicts, HTTP/WebSocket receipts, completion and restart replay, late answer invalidation, queues, steering cancellation, unknown delivery, additive migration, SQLite backup readback, invalid input and Cloud checkpoint failure. They use synthetic runtimes and disposable state. Existing app, stream, context, follow-up, cloud persistence and authority checks remain relevant.

Frontend verification lives with the shared registry and each tool family. Native browser execution and live provider behavior require separate integration evidence; a mocked registry or an HTTP fixture does not establish those outcomes.

`verify-webmcp-workspace.tsx` mounts the real App and checks cancellation while a synthetic refresh response is blocked. `verify-agent-prompt-tabs.tsx` provides a mounted Agent controller for two separate same-origin browser tabs. Its session and pending-request storage are real browser storage; HTTP, sockets and tool registration are synthetic. Run the two-tab sequence with both tabs mounted before either send, then reload one tab before and after the other's acknowledgement, using both shared and distinct sessions. The deterministic storage cases also run in `verify-agent-actions-webmcp.ts`.

Primary references checked on 2026-10-19:

- [WebMCP specification](https://webmachinelearning.github.io/webmcp/): imperative registry, tool execution and cancellation semantics.
- [SQLite transactions](https://www.sqlite.org/lang_transaction.html) and [UPSERT](https://www.sqlite.org/lang_upsert.html): database arbitration and preserving an existing unique request.
- [SQLite online backup](https://www.sqlite.org/backup.html): the coherent backup mechanism already used by the cloud workspace.
- [Stripe idempotent requests](https://docs.stripe.com/api/idempotent_requests): an established client-key and parameter-comparison pattern. This application's no-expiry and no-uncertain-replay rules are its own contract.
